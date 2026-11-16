"""Live stock and prices from JLCPCB's own parts search (the endpoint behind jlcpcb.com/parts).

The jlcparts mirror re-fetches each part only every few weeks and does not contain every part, so anything
that is about to be ordered should be confirmed here. The endpoint is unofficial and unauthenticated: keep the
request rate low (lookups are per part, a handful at a time).
"""
import json
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

URL = os.environ.get(
    "JLCPARTS_MCP_LIVE_URL",
    "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList/v2")
ENABLED = os.environ.get("JLCPARTS_MCP_LIVE", "1").lower() not in ("0", "false", "no", "off")
MAX_PARTS = 60      # per tool call
WORKERS = 4


class LiveError(RuntimeError):
    """JLCPCB could not be asked (network, HTTP status, unexpected payload)."""


def parse(row: dict) -> dict:
    """Turn one search result into the same shape as a row of the local index, flagged live."""
    tiers = []
    for t in row.get("componentPrices") or []:
        start, end, price = t.get("startNumber"), t.get("endNumber"), t.get("productPrice")
        if start is not None and price is not None:
            tiers.append(f"{start}-{'' if end in (None, -1) else end}:{price}")
    library = (row.get("componentLibraryType") or "").lower()
    attrs = {a["attribute_name_en"]: a.get("attribute_value_name") for a in row.get("attributes") or []
             if a.get("attribute_name_en") and a.get("attribute_value_name") not in (None, "", "-")}
    return {
        "lcsc": int(row["componentCode"][1:]),
        # JLCPCB's "first sort" is the specific type and "second sort" its parent, the reverse of the mirror's naming
        "category": row.get("secondSortName") or "", "subcategory": row.get("firstSortName") or "",
        "mfr": row.get("componentModelEn") or "", "manufacturer": row.get("componentBrandEn") or "",
        # pin count is not in this payload (encapsulationNumber is the reel quantity)
        "package": row.get("componentSpecificationEn") or "", "joints": None,
        "tier": 0 if library == "base" else 1 if row.get("preferredComponentFlag") else 2,
        "description": row.get("describe") or "",
        "datasheet": row.get("dataManualUrl") or row.get("dataManualOfficialLink") or "",
        "stock": int(row.get("stockCount") or 0), "price": ",".join(tiers),
        "attributes": json.dumps(attrs, ensure_ascii=False), "rohs": row.get("rohsFlag"),
        "assembly_process": None, "checked": None, "live": True,
        "min_order": row.get("minPurchaseNum"), "min_placement": row.get("leastPatchNumber"),
    }


def find(keyword: str, timeout: float = 20) -> list[dict]:
    """Parts JLCPCB's search returns for a keyword (an LCSC number or a part number), parsed. The search is fuzzy:
    callers must check `lcsc` / `mfr` for an exact hit."""
    body = json.dumps({
        "currentPage": 1, "pageSize": 25, "keyword": keyword, "searchSource": "search", "searchType": 2,
        "componentBrandList": [], "componentSpecificationList": [], "componentAttributeList": [], "paramList": [],
    }).encode()
    req = urllib.request.Request(URL, data=body, headers={
        "Accept": "application/json, text/plain, */*", "Content-Type": "application/json;charset=UTF-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except (OSError, ValueError) as e:      # URLError and timeouts are OSErrors; bad JSON is a ValueError
        raise LiveError(f"{type(e).__name__}: {e}") from None
    if data.get("code") != 200:
        raise LiveError(f"JLCPCB answered code {data.get('code')}: {str(data.get('message'))[:120]}")
    try:
        rows = ((data.get("data") or {}).get("componentPageInfo") or {}).get("list") or []
        return [parse(row) for row in rows
                if isinstance(row, dict) and str(row.get("componentCode") or "")[:1] == "C"
                and str(row.get("componentCode"))[1:].isdigit()]
    except (AttributeError, KeyError, TypeError, ValueError) as e:     # JLCPCB changed the payload
        raise LiveError(f"unexpected answer from JLCPCB ({type(e).__name__}: {e})") from None


def lookup(lcsc: int, timeout: float = 20) -> dict | None:
    """The part with exactly this LCSC number, or None if JLCPCB does not list it."""
    return next((p for p in find(f"C{lcsc}", timeout) if p["lcsc"] == lcsc), None)


def lookup_many(numbers: list[int]) -> dict[int, dict | None | LiveError]:
    """Look up several LCSC numbers (a few in parallel). Each value is the part, None, or the error for that part."""
    def one(n):
        try:
            return lookup(n)
        except LiveError as e:
            return e
        except Exception as e:  # never let one odd answer sink a whole BOM check
            return LiveError(f"{type(e).__name__}: {e}")
    numbers = list(dict.fromkeys(numbers))
    with ThreadPoolExecutor(WORKERS) as pool:
        return dict(zip(numbers, pool.map(one, numbers)))
