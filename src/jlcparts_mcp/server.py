"""MCP server: find the most suitable JLCPCB-assemblable parts from the jlcparts database."""
import argparse
import difflib
import functools
import json
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict
from contextlib import closing
from functools import lru_cache
from typing import Literal

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import db, live as jlc, units

mcp = FastMCP(
    "jlcparts",
    instructions=(
        "Find parts for JLCPCB assembly (prices in USD). search_parts and describe_category query a local mirror "
        "of the catalogue: fast and parametric, but each part's stock and price were last fetched on its "
        "stock_checked date, often weeks ago, and the mirror does not contain every part. get_part and check_bom "
        "ask JLCPCB live by default: use them to confirm stock before recommending or ordering anything, and never "
        "call a part out of stock or nonexistent on the mirror's word alone. "
        "Workflow: describe_category to learn a category's exact attribute names and example values, then "
        "search_parts with attribute filters (values are unit-aware: '10k', '100nF', '3.3V'), then check_bom or "
        "get_part on the candidates. Results rank basic parts first, then preferred, then extended; JLCPCB "
        "charges a per-part setup fee for extended parts only. find_alternatives replaces a part. "
        "Attributes are JLCPCB's own data and are sometimes wrong or missing, and similar-looking parts can sit "
        "in different categories (a crystal and an oscillator share frequency and package): check each result's "
        "category, and confirm pinout and ratings in the datasheet (get_part gives the link)."
    ),
)

LOCAL = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
LIVE = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
TIER_NAMES = {0: "basic", 1: "preferred", 2: "extended"}
Library = Literal["any", "basic_or_preferred", "basic"]
STALE_DAYS = 3


# --- SQL helpers -------------------------------------------------------------------------------

@lru_cache(maxsize=200_000)
def _tiers(price: str) -> tuple[tuple[int, float], ...]:
    """'1-9:0.5,10-:0.4' -> ((1, 0.5), (10, 0.4)); malformed chunks are skipped."""
    out = []
    for chunk in (price or "").split(","):
        try:
            rng, p = chunk.split(":")
            out.append((int(rng.split("-")[0]), float(p)))
        except ValueError:
            continue
    return tuple(sorted(out))


def _unit_price(price: str, qty: int) -> float | None:
    tiers = _tiers(price)
    if not tiers:
        return None
    best = tiers[0][1]
    for q, p in tiers:
        if q <= qty:
            best = p
    return best


def _attr_ok(text, op, val) -> int:
    if text is not None and not isinstance(text, str):
        text = str(text)   # json_extract returns numbers for unquoted JSON values
    if isinstance(val, float):
        return int(units.numeric_match(text, op, val))
    return int(units.text_match(text, op, val))


def _connect():
    con = db.connect()
    con.create_function("unit_price", 2, _unit_price, deterministic=True)
    con.create_function("attr_ok", 3, _attr_ok, deterministic=True)
    return con


def _like(s: str) -> str:
    return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _fts_query(query: str) -> str | None:
    """Each whitespace-separated word becomes a prefix phrase, so '12.288MHz' matches the adjacent tokens
    '12' '288mhz...' rather than any part that has a 12 and a 288 somewhere."""
    words = [w.replace('"', "") for w in query.split()]
    words = [w for w in words if re.search(r"\w", w)]
    return " ".join(f'"{w}"*' for w in words) or None


def _resolve_categories(con, category: str) -> list[sqlite3.Row]:
    """Subcategories selected by `category` (case-insensitive words).

    An exact top-level category name selects all of its subcategories. Otherwise every word must appear in the
    subcategory name; only if no subcategory matches are the words looked for in 'category / subcategory'.
    Preferring the subcategory keeps 'oscillators' from also selecting 'Crystals, Oscillators, Resonators / Crystals'."""
    rows = con.execute("SELECT category, subcategory, n, n_basic, n_preferred FROM cats "
                       "ORDER BY category, subcategory").fetchall()
    wanted = " ".join(category.lower().split())
    words = wanted.split()
    hits = [r for r in rows if r["category"].lower() == wanted]
    hits = hits or [r for r in rows if all(w in r["subcategory"].lower() for w in words)]
    hits = hits or [r for r in rows if all(w in f"{r['category']} / {r['subcategory']}".lower() for w in words)]
    if not hits:
        names = [f"{r['category']} / {r['subcategory']}" for r in rows]
        close = difflib.get_close_matches(category, names, n=5, cutoff=0.3)
        raise ValueError(f"No category matches {category!r}. Closest: {close or 'none'}. Use list_categories to browse.")
    return hits


def _resolve_attr_names(con, subcats: list[str], name: str) -> dict[str, list[str]]:
    """Map a user-supplied attribute name to the exact JSON keys of each subcategory: {subcategory: [keys]}.

    Per subcategory: a case-insensitive exact match wins, and all its case variants are kept ('Single supply' and
    'Single Supply' both exist in one category); otherwise an unambiguous substring. A substring that hits several
    different attributes of one subcategory ('Voltage' -> 'Output Voltage', 'Voltage - Supply') is an error,
    because picking one silently would filter on the wrong thing. Subcategories without the attribute are left out."""
    by_sub: dict[str, list[str]] = defaultdict(list)
    marks = ",".join("?" * len(subcats))
    for sub, key in con.execute(f"SELECT subcategory, name FROM attr_info WHERE subcategory IN ({marks})", subcats):
        by_sub[sub].append(key)
    low = name.casefold()
    out: dict[str, list[str]] = {}
    for sub, known in by_sub.items():
        hits = [k for k in known if k.casefold() == low]
        if not hits:
            hits = [k for k in known if low in k.casefold()]
            if len({k.casefold() for k in hits}) > 1:
                raise ValueError(f"Attribute {name!r} is ambiguous in {sub!r}: it matches {sorted(hits)}. "
                                 "Use one of these exact names.")
        if hits:
            out[sub] = sorted(hits)
    if not out:
        known = sorted({k for ks in by_sub.values() for k in ks})
        raise ValueError(f"Unknown attribute {name!r} for these categories. Available: {known[:60]}")
    return out


def _attr_expr(keys_by_sub: dict[str, list[str]]) -> tuple[str, list]:
    """SQL (and its parameters) giving the attribute value each part's own subcategory resolved to."""
    def one(keys):
        expr = ",".join("json_extract(p.attributes, ?)" for _ in keys)
        return (f"COALESCE({expr})" if len(keys) > 1 else expr), [f'$."{k}"' for k in keys]
    variants = {tuple(k) for k in keys_by_sub.values()}
    if len(variants) == 1:
        return one(list(variants.pop()))
    sql, params = ["CASE p.subcategory"], []
    for sub, keys in keys_by_sub.items():
        expr, ps = one(keys)
        sql.append(f"WHEN ? THEN {expr}")
        params += [sub, *ps]
    return " ".join(sql) + " END", params


def _attr_value(attrs: dict, keys: list[str]):
    return next((attrs[k] for k in keys if attrs.get(k) is not None), None)


def _stale_note(con) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key = 'source_time'").fetchone()
    if row:
        age = (time.time() - int(row[0])) / 86400
        if age > STALE_DAYS:
            return f"the local mirror snapshot is {age:.0f} days old; call update_database to refresh it"
    return None


def _short(v, n=90):
    if v is None:
        return None
    s = str(v)
    return s if len(s) <= n else s[: n - 1] + "…"


def _date(epoch) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(int(epoch)))


def _lcsc_number(text: str) -> int | None:
    m = re.fullmatch(r"[Cc](\d+)", text.strip())
    return int(m.group(1)) if m else None


# --- live refresh --------------------------------------------------------------------------------

def _live_parts(numbers: list[int], live: bool) -> tuple[dict, str | None]:
    """Ask JLCPCB about these LCSC numbers: {number: part | None | LiveError}, plus a note for the caller when live
    figures were wanted but are not (all) available."""
    numbers = list(dict.fromkeys(numbers))
    if not live or not numbers:
        return {}, None
    if not jlc.ENABLED:
        return {}, "live lookups are switched off (JLCPARTS_MCP_LIVE=0): figures are from the mirror"
    found = jlc.lookup_many(numbers[:jlc.MAX_PARTS])
    notes = []
    errors = [e for e in found.values() if isinstance(e, jlc.LiveError)]
    if errors:
        notes.append(f"JLCPCB could not be asked about {len(errors)} part(s) ({errors[0]}): those figures are from the mirror")
    if len(numbers) > jlc.MAX_PARTS:
        notes.append(f"only the first {jlc.MAX_PARTS} parts were checked live; the rest are from the mirror")
    return found, "; ".join(notes) or None


def _refresh(row, fresh) -> dict | None:
    """An index row with JLCPCB's current stock, price and library type laid over it. A part only JLCPCB knows is
    returned as is; a part JLCPCB no longer lists keeps its mirror figures and is marked."""
    row = dict(row) if row is not None else None
    if isinstance(fresh, dict):
        if row is None:
            return fresh
        return {**row, **{k: fresh[k] for k in ("stock", "price", "tier", "live", "min_order", "min_placement")}}
    if row is not None and fresh is None:
        row["unlisted"] = True
    return row


def _checked(r: dict) -> str:
    if r.get("live"):
        return "live"
    when = f"mirror {_date(r['checked'])}" if r.get("checked") else "mirror"
    return when + (", and JLCPCB's search does not list it now" if r.get("unlisted") else "")


def _row_out(r, qty: int, full: bool = False) -> dict:
    r = dict(r)
    up = _unit_price(r["price"], qty)
    out = {
        "lcsc": f"C{r['lcsc']}", "mpn": r["mfr"], "manufacturer": r["manufacturer"], "category": r["subcategory"],
        "package": r["package"], "library": TIER_NAMES[r["tier"]], "stock": r["stock"], "stock_checked": _checked(r),
        "unit_price_usd": up,
    }
    if up is not None and qty > 1:
        out["line_total_usd"] = round(up * qty, 4)
    attrs = json.loads(r["attributes"] or "{}")
    out["attributes"] = attrs if full else {k: _short(v) for k, v in attrs.items()}
    if full:
        out.update(category=f"{r['category']} / {r['subcategory']}", description=r["description"],
                   datasheet=r["datasheet"], pins=r["joints"], rohs=bool(r["rohs"]),
                   assembly_process=r["assembly_process"],
                   price_tiers_usd=[{"from_qty": q, "unit_price": p} for q, p in _tiers(r["price"])],
                   lcsc_url=f"https://www.lcsc.com/product-detail/C{r['lcsc']}.html")
        for key in ("min_order", "min_placement"):
            if r.get(key):
                out[key] = r[key]
    return out


# --- tools -------------------------------------------------------------------------------------

class AttrFilter(BaseModel):
    name: str = Field(description="Attribute name, e.g. 'Resistance', 'Capacitance', 'Voltage Rating' "
                                  "(case-insensitive; an unambiguous substring is accepted; see describe_category)")
    op: Literal["eq", "ne", "gte", "lte", "gt", "lt", "contains"] = Field(
        "eq", description="eq/ne: equal (numeric if value parses as a number, else text); "
                          "gte/lte/gt/lt: numeric; contains: text substring. For a range attribute such as "
                          "'1.8V~3.6V', eq means the range covers the value, gte/lte compare its upper/lower end.")
    value: str | float = Field(description="e.g. '10k', '100nF', '16V', 3.3, 'X7R'. SI prefixes are understood.")


def search_parts(
    query: str | None = None,
    category: str | None = None,
    attributes: list[AttrFilter] | None = None,
    package: str | None = None,
    manufacturer: str | None = None,
    part_number: str | None = None,
    library: Library = "any",
    quantity: int = 1,
    min_stock: int | None = None,
    max_unit_price: float | None = None,
    sort: Literal["suitability", "price", "stock"] = "suitability",
    limit: int = 10,
    live: bool = False,
) -> dict:
    """Find JLCPCB parts matching requirements in the local mirror, best candidates first.

    query: free-text words (all must match; each is a prefix match) over part number, manufacturer, package,
      category, description and attribute values.
    category: words that must all appear in the subcategory name (e.g. 'chip resistor', 'mlcc', 'ldo'), or an
      exact top-level category name for all of its subcategories. See list_categories.
    attributes: parametric filters, e.g. [{"name":"Resistance","value":"10k"},{"name":"Tolerance","op":"lte","value":1}].
      Use describe_category first to get exact attribute names.
    package: case-insensitive substring of the package name ('0402', 'SOT-23', 'QFN-20').
    library: 'basic' or 'basic_or_preferred' avoids JLCPCB's extended-part setup fee.
    quantity: units needed (per board x boards). Parts with less stock are excluded, and the unit price
      is taken from the matching price tier. min_stock overrides the stock requirement.
    sort: 'suitability' = basic, then preferred, then extended, then cheapest at `quantity`.
    live: also ask JLCPCB for the current stock and price of the returned parts (one request each).

    Matching and ranking use the mirror, whose stock figures date from each row's stock_checked. A part missing
    here may still exist at JLCPCB (get_part asks directly). Each result carries its category: check it, since
    different part types can share attributes and packages.
    """
    quantity = max(1, quantity)
    with closing(_connect()) as con:
        where, params = ["p.stock >= ?"], [max(1, min_stock if min_stock is not None else quantity)]
        subcats: list[str] = []
        notes = []

        if category:
            cats = _resolve_categories(con, category)
            subcats = list(dict.fromkeys(r["subcategory"] for r in cats))   # one name can sit under several categories
            where.append(f"p.subcategory IN ({','.join('?' * len(subcats))})")
            params += subcats
            if len(cats) > 1:
                notes.append(f"category matched {len(cats)}: {[r['category'] + ' / ' + r['subcategory'] for r in cats][:12]}")
        fts = _fts_query(query) if query else None
        if fts:
            where.append("p.lcsc IN (SELECT rowid FROM fts WHERE fts MATCH ?)")
            params.append(fts)
        if part_number:
            where.append("p.mfr LIKE ? ESCAPE '\\'")
            params.append(_like(part_number))
        if manufacturer:
            where.append("p.manufacturer LIKE ? ESCAPE '\\'")
            params.append(_like(manufacturer))
        if package:
            where.append("p.package LIKE ? ESCAPE '\\'")
            params.append(_like(package))
        if library != "any":
            where.append("p.tier <= ?")
            params.append(0 if library == "basic" else 1)
        if max_unit_price is not None:
            where.append("unit_price(p.price, ?) <= ?")
            params += [quantity, max_unit_price]
        for f in attributes or []:
            number = None if f.op == "contains" else units.parse_number(f.value)
            if f.op in ("gte", "lte", "gt", "lt") and number is None:
                raise ValueError(f"Filter on {f.name!r}: op {f.op} needs a numeric value, got {f.value!r}")
            val = number if number is not None else str(f.value)
            if subcats:
                keys_by_sub = _resolve_attr_names(con, subcats, f.name)
                keys = sorted({k for ks in keys_by_sub.values() for k in ks})
                if keys != [f.name]:
                    notes.append(f"attribute {f.name!r} resolved to {keys}")
            else:
                keys_by_sub = {"": [f.name]}
                notes.append(f"no category given: attribute {f.name!r} matched by exact key; pass category to resolve names")
            if number is not None and isinstance(f.value, str):
                notes.append(f"{f.name} {f.op} {number:g} (read from {f.value!r})")
            expr, expr_params = _attr_expr(keys_by_sub)
            where.append(f"attr_ok({expr}, ?, ?)")
            params += expr_params + [f.op, val]

        # within a library tier, parts with a comfortable stock buffer (10x) outrank near-sold-out ones
        order = {"suitability": "p.tier, p.stock < ?, COALESCE(up, 1e9), p.stock DESC",
                 "price": "COALESCE(up, 1e9), p.tier", "stock": "p.stock DESC"}[sort]
        sql = (f"SELECT p.*, unit_price(p.price, ?) AS up, COUNT(*) OVER() AS total FROM parts p "
               f"WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?")
        tail = [quantity * 10] if sort == "suitability" else []
        rows = con.execute(sql, [quantity] + params + tail + [max(1, min(limit, 50))]).fetchall()
        stale = _stale_note(con)

    fresh, live_note = _live_parts([r["lcsc"] for r in rows], live)
    results = []
    for r in rows:
        merged = _refresh(r, fresh.get(r["lcsc"], jlc.LiveError()) if fresh else jlc.LiveError())
        out = _row_out(merged, quantity)
        if merged.get("live") and merged["stock"] < quantity:
            out["warning"] = f"JLCPCB now has {merged['stock']}, fewer than the {quantity} needed"
        results.append(out)
    result = {"total_matches": rows[0]["total"] if rows else 0, "results": results}
    notes += [n for n in (live_note, stale) if n]
    if notes:
        result["notes"] = notes
    if not rows:
        result["hint"] = ("No matches in the mirror. Loosen filters, lower quantity, or check attribute values with "
                          "describe_category. The mirror is incomplete: a known part number can be looked up with get_part.")
    return result


def describe_category(category: str, top_packages: int = 12) -> dict:
    """Show a category's attributes (exact names, numeric ranges, common values), packages and stock counts.
    Call this before search_parts so attribute filters use real names and units."""
    with closing(_connect()) as con:
        cats = _resolve_categories(con, category)
        if len(cats) > 6:
            return {"message": f"{len(cats)} categories match; narrow the name.",
                    "matches": [f"{r['category']} / {r['subcategory']} ({r['n']:,})" for r in cats[:40]]}
        out = []
        for c in cats:
            sub = c["subcategory"]
            attrs = []
            for a in con.execute("SELECT * FROM attr_info WHERE subcategory = ? AND n >= 0.05 * ? ORDER BY n DESC",
                                 (sub, c["n"])):
                ex = "; ".join(_short(v, 40) for v in json.loads(a["examples"])[: 5 if a["numeric"] else 8])
                rng = f", numeric {a['vmin']:g}..{a['vmax']:g} (SI base units)" if a["numeric"] else ""
                attrs.append(f"{a['name']} [{a['n']:,} parts{rng}] e.g. {ex}")
            pk = con.execute("SELECT package, count(*) c FROM parts WHERE subcategory = ? "
                             "GROUP BY 1 ORDER BY c DESC LIMIT ?", (sub, top_packages)).fetchall()
            out.append({"category": f"{c['category']} / {sub}", "in_stock_parts": c["n"], "basic": c["n_basic"],
                        "preferred": c["n_preferred"], "attributes": attrs,
                        "top_packages": {r["package"]: r["c"] for r in pk}})
        return {"categories": out}


def list_categories(query: str | None = None, limit: int = 60) -> list[str]:
    """List 'Category / Subcategory' names with in-stock part counts, optionally filtered by words."""
    with closing(_connect()) as con:
        rows = _resolve_categories(con, query) if query else con.execute(
            "SELECT category, subcategory, n, n_basic, n_preferred FROM cats ORDER BY category, subcategory").fetchall()
        out = [f"{r['category']} / {r['subcategory']}: {r['n']:,} parts ({r['n_basic']} basic, {r['n_preferred']} preferred)"
               for r in rows[:limit]]
        if len(rows) > limit:
            out.append(f"... {len(rows) - limit} more; pass query words or a higher limit")
        return out


def get_part(part: str, quantity: int = 1, live: bool = True) -> dict:
    """Full details for one part: an LCSC number ('C25804') or an exact manufacturer part number ('STM32F407VGT6').
    Includes price tiers, datasheet link, attributes and stock. With live=true (default) stock, price and library
    type come from JLCPCB right now, and parts the mirror does not contain are still found."""
    quantity = max(1, quantity)
    number = _lcsc_number(part)
    with closing(_connect()) as con:
        if number is not None:
            rows = con.execute("SELECT * FROM parts WHERE lcsc = ?", (number,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM parts WHERE mfr = ? COLLATE NOCASE ORDER BY tier, stock DESC LIMIT 5",
                               (part.strip(),)).fetchall()
    rows = [dict(r) for r in rows]
    note = None
    if number is not None:
        fresh, note = _live_parts([number], live)
        merged = _refresh(rows[0] if rows else None, fresh.get(number, jlc.LiveError()))
        parts = [merged] if merged else []
    else:
        fresh, note = _live_parts([r["lcsc"] for r in rows], live)
        parts = [_refresh(r, fresh.get(r["lcsc"], jlc.LiveError())) for r in rows]
        if not parts and live and jlc.ENABLED:   # the mirror is incomplete: let JLCPCB's own search try the part number
            try:
                parts = [p for p in jlc.find(part.strip()) if p["mfr"].casefold() == part.strip().casefold()][:5]
            except jlc.LiveError as e:
                note = f"JLCPCB could not be asked ({e})"
    if not parts:
        asked = live and jlc.ENABLED and not note
        message = ("JLCPCB does not list a part with exactly this number." if asked else
                   "Not in the local mirror, which is incomplete; JLCPCB itself was not asked, so this says nothing "
                   "about whether the part exists or is in stock.")
        out = {"found": False, "message": message + " search_parts with part_number does a substring match."}
    else:
        out = {"found": True, "parts": [_row_out(p, quantity, full=True) for p in parts]}
    if note:
        out["note"] = note
    return out


class BomItem(BaseModel):
    lcsc: str = Field(description="LCSC part number, e.g. 'C25804'")
    quantity: int = Field(1, description="Total units needed")


def check_bom(items: list[BomItem], live: bool = True) -> dict:
    """Check a list of LCSC numbers: stock vs. quantity, unit price at that quantity, library type, line totals and
    the number of extended parts (each carries a JLCPCB setup fee). With live=true (default) every part is checked
    at JLCPCB right now; each line's stock_checked says 'live' or the date of the mirror figure it fell back to.
    Lines with the same LCSC number are merged, so stock and price tier use the total quantity."""
    wanted: dict[str, list[int]] = {}
    for it in items:
        number = _lcsc_number(it.lcsc)
        wanted.setdefault(f"C{number}" if number is not None else it.lcsc.strip(), []).append(it.quantity)
    numbers = [n for n in map(_lcsc_number, wanted) if n is not None]
    with closing(_connect()) as con:
        marks = ",".join("?" * len(numbers))
        index = {r["lcsc"]: r for r in con.execute(f"SELECT * FROM parts WHERE lcsc IN ({marks})", numbers)}
        stale = _stale_note(con)
    fresh, note = _live_parts(numbers, live)

    lines, total, extended, problems, unverified, unpriced = [], 0.0, 0, 0, 0, 0
    for key, quantities in wanted.items():
        qty = sum(quantities)
        number = _lcsc_number(key)
        if qty <= 0:
            lines.append({"lcsc": key, "quantity": qty, "status": "quantity must be at least 1"})
            problems += 1
            continue
        r = _refresh(index.get(number), fresh.get(number, jlc.LiveError())) if number is not None else None
        if r is None:
            if number is None:
                status = "not an LCSC number"
            elif number in fresh and fresh[number] is None:
                status = "JLCPCB does not list this part"
            else:
                status = "not in the mirror, and JLCPCB was not asked: unknown"
            lines.append({"lcsc": key, "quantity": qty, "status": status})
            problems += 1
            continue
        up = _unit_price(r["price"], qty)
        if r["stock"] >= qty and not r.get("unlisted"):
            status = "ok"
        elif r.get("unlisted"):
            status = "JLCPCB's search does not list it now (the mirror did)"
        else:
            status = f"insufficient stock ({r['stock']})" if r["stock"] else "out of stock"
        line = {"lcsc": key, "mpn": r["mfr"], "package": r["package"], "library": TIER_NAMES[r["tier"]],
                "quantity": qty, "stock": r["stock"], "stock_checked": _checked(r), "unit_price_usd": up, "status": status}
        if len(quantities) > 1:
            line["merged_lines"] = len(quantities)
        if up is not None:
            line["line_total_usd"] = round(up * qty, 4)
            total += up * qty
        else:
            unpriced += 1
            line["price_note"] = "no price listed; not included in parts_total_usd"
        if r.get("min_order") and qty < r["min_order"]:
            line["min_order"] = r["min_order"]
            line["price_note"] = f"JLCPCB sells at least {r['min_order']}; the price is for {qty}"
        extended += r["tier"] == 2
        problems += status != "ok"
        unverified += not r.get("live")
        lines.append(line)
    out = {"lines": lines, "parts_total_usd": round(total, 4), "extended_parts": extended, "problems": problems}
    if unverified:
        out["lines_from_mirror"] = unverified
    if unpriced:
        out["lines_without_price"] = unpriced
    notes = [n for n in (note, stale if unverified else None) if n]
    if notes:
        out["notes"] = notes
    return out


def find_alternatives(
    lcsc: str,
    quantity: int = 1,
    same_package: bool = True,
    must_match: list[str] | None = None,
    library: Library = "any",
    limit: int = 10,
    live: bool = True,
) -> dict:
    """Find drop-in candidates for a part: same subcategory (and package by default), ranked by how many
    attributes are identical, then library tier, then price. `differences` lists each attribute that differs
    ([original, candidate]) so you can judge compatibility. must_match: attribute names that must be identical.
    Candidates come from the mirror; with live=true (default) the returned ones are re-checked at JLCPCB, and an
    original the mirror does not contain is fetched from JLCPCB."""
    quantity = max(1, quantity)
    number = _lcsc_number(lcsc)
    if number is None:
        return {"found": False, "message": f"{lcsc!r} is not an LCSC number such as 'C25804'."}
    with closing(_connect()) as con:
        orig = con.execute("SELECT * FROM parts WHERE lcsc = ?", (number,)).fetchone()
        notes = []
        if orig is None:
            fresh, note = _live_parts([number], live)
            orig = fresh.get(number) if isinstance(fresh.get(number), dict) else None
            if orig is None:
                return {"found": False, "message": f"{lcsc} is not in the mirror" + (f" ({note})" if note else
                        " and JLCPCB does not list it" if fresh else "") + "; describe what you need with search_parts instead."}
            notes.append("the original is not in the mirror; its details came from JLCPCB live")
        orig = dict(orig)
        oattrs = json.loads(orig["attributes"] or "{}")
        match_keys = [_resolve_attr_names(con, [orig["subcategory"]], name)[orig["subcategory"]] for name in must_match or []]
        where, params = ["subcategory = ?", "lcsc != ?", "stock >= ?"], [orig["subcategory"], orig["lcsc"], quantity]
        if same_package:
            where.append("package = ?")
            params.append(orig["package"])
        if library != "any":
            where.append("tier <= ?")
            params.append(0 if library == "basic" else 1)
        cands = []
        for r in con.execute(f"SELECT * FROM parts WHERE {' AND '.join(where)}", params):
            a = json.loads(r["attributes"] or "{}")
            if any(_attr_value(a, ks) != _attr_value(oattrs, ks) for ks in match_keys):
                continue
            diff = {k: [_short(oattrs.get(k)), _short(a.get(k))] for k in oattrs.keys() | a.keys() if oattrs.get(k) != a.get(k)}
            price = _unit_price(r["price"], quantity)
            cands.append((len(diff), r["tier"], price if price is not None else 1e9, r, diff))
    cands.sort(key=lambda c: c[:3])
    top = cands[: max(1, min(limit, 30))]
    fresh, note = _live_parts([r["lcsc"] for _, _, _, r, _ in top], live)
    out = []
    for _, _, _, r, diff in top:
        merged = _refresh(r, fresh.get(r["lcsc"], jlc.LiveError()))
        row = _row_out(merged, quantity)
        row.pop("attributes")
        row["differences"] = diff
        if merged.get("live") and merged["stock"] < quantity:
            row["warning"] = f"JLCPCB now has {merged['stock']}, fewer than the {quantity} needed"
        out.append(row)
    result = {"found": True, "original": _row_out(orig, quantity), "candidates_considered": len(cands), "alternatives": out}
    notes += [note] if note else []
    if notes:
        result["notes"] = notes
    return result


def database_status(check_upstream: bool = False) -> dict:
    """When the local mirror index was built, how old its snapshot is, and whether an update is running.
    check_upstream=true also asks the jlcparts mirror whether a newer snapshot exists (one small request)."""
    return db.status(check_upstream)


def update_database(force: bool = False) -> str:
    """Refresh the local index from the jlcparts mirror (published several times a day). Runs in the background
    (download ~250 MB; a minute or two). Searches keep using the old index until it finishes; poll database_status."""
    if db.progress["running"]:
        return "An update is already running; see database_status."
    threading.Thread(target=_safe_build, args=(force,), daemon=True).start()
    return "Update started in the background. Check database_status for progress."


def _safe_build(force: bool = False) -> None:
    try:
        db.build(force)
    except Exception:
        pass  # recorded in db.progress['error']


def _auto_update(hours: float) -> None:
    while True:
        try:
            age = db.snapshot_age_days() if db.DB_PATH.exists() else None
        except Exception:
            age = None
        if age is None or age * 24 >= hours:
            _safe_build()
        time.sleep(max(hours, 0.25) * 3600)


# --- registration: tools run in a worker thread, so a slow live lookup does not stall other requests ---

def _threaded(fn):
    @functools.wraps(fn)
    async def run(*args, **kwargs):
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
    return run


mcp.tool(annotations=LIVE)(_threaded(search_parts))
mcp.tool(annotations=LOCAL)(_threaded(describe_category))
mcp.tool(annotations=LOCAL)(_threaded(list_categories))
mcp.tool(annotations=LIVE)(_threaded(get_part))
mcp.tool(annotations=LIVE)(_threaded(check_bom))
mcp.tool(annotations=LIVE)(_threaded(find_alternatives))
mcp.tool(annotations=LIVE)(_threaded(database_status))
mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))(_threaded(update_database))


def main() -> None:
    parser = argparse.ArgumentParser(prog="jlcparts-mcp", description=__doc__)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the MCP server on stdio (default)")
    build = sub.add_parser("build", help="download the parts data and (re)build the local index")
    build.add_argument("--force", action="store_true", help="rebuild even if upstream has not changed")
    sub.add_parser("status", help="print the state of the local index as JSON")
    args = parser.parse_args()
    if args.command == "build":
        print(db.build(force=args.force))
    elif args.command == "status":
        print(json.dumps(db.status(check_upstream=True), indent=2))
    else:
        hours = os.environ.get("JLCPARTS_MCP_AUTO_UPDATE_HOURS")
        if hours:
            threading.Thread(target=_auto_update, args=(float(hours),), daemon=True).start()
        mcp.run()


if __name__ == "__main__":
    main()
