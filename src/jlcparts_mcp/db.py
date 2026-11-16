"""Download the jlcparts cache, build a slim searchable index of in-stock parts, and open it for queries."""
import json
import os
try:
    import fcntl
except ImportError:     # Windows: no cross-process lock, the in-process one still applies
    fcntl = None
import shutil
import sqlite3
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import Counter, defaultdict
from email.utils import parsedate_to_datetime
from pathlib import Path

from . import units

DATA_URL = os.environ.get("JLCPARTS_DATA_URL", "https://yaqwsx.github.io/jlcparts/data")
DATA_DIR = Path(os.environ.get("JLCPARTS_MCP_DATA", Path.home() / ".cache" / "jlcparts-mcp"))
DB_PATH = DATA_DIR / "parts.sqlite3"
INDEX_VERSION = 2          # bump when the index schema changes; older indexes are rebuilt on the next update

_build_lock = threading.Lock()
progress = {"running": False, "stage": "idle", "error": None}


def log(msg: str) -> None:
    print(f"[jlcparts-mcp] {msg}", file=sys.stderr, flush=True)


def _stage(name: str) -> None:
    progress["stage"] = name
    log(name)


def _ro_uri(path: Path) -> str:
    return f"file:{urllib.parse.quote(str(path))}?mode=ro"


# --- download + extract ------------------------------------------------------------------------

def head_last_modified() -> str | None:
    req = urllib.request.Request(f"{DATA_URL}/cache.zip", method="HEAD")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.headers.get("Last-Modified")


def _download(dest_dir: Path) -> list[Path]:
    """cache.zip is the last part of a split archive; cache.z01, z02, ... precede it."""
    names = ["cache.zip"]
    i = 1
    while True:
        name = f"cache.z{i:02d}"
        try:
            urllib.request.urlopen(urllib.request.Request(f"{DATA_URL}/{name}", method="HEAD"), timeout=30).close()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                break
            raise
        names.insert(i - 1, name)
        i += 1
    paths = []
    for n, name in enumerate(names, 1):
        _stage(f"downloading {name} ({n}/{len(names)})")
        path = dest_dir / name
        with urllib.request.urlopen(f"{DATA_URL}/{name}", timeout=60) as r, open(path, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        paths.append(path)
    return paths


class ArchiveError(RuntimeError):
    """The downloaded archive is inconsistent (usually: upstream republished while we were downloading)."""


def _extract(parts: list[Path], out: Path) -> None:
    """Stream the single deflated entry out of a (possibly split) zip, verifying its CRC."""
    last = parts[-1].read_bytes()
    eocd = last.rfind(b"PK\x05\x06")
    if eocd < 0:
        raise ArchiveError("cache.zip has no end-of-central-directory record")
    cd = struct.unpack("<L", last[eocd + 16:eocd + 20])[0]  # offset within the last disk
    if last[cd:cd + 4] != b"PK\x01\x02":
        cd = last.rfind(b"PK\x01\x02", 0, eocd)             # zip64 archives store 0xFFFFFFFF as the offset
        if cd < 0:
            raise ArchiveError("cache.zip central directory not found")
    expected_crc, _, expected_size = struct.unpack("<LLL", last[cd + 16:cd + 28])
    del last

    def chunks():
        for p in parts:
            with open(p, "rb") as f:
                while b := f.read(1 << 20):
                    yield b

    it = chunks()
    try:
        buf = b""
        while len(buf) < 34:
            buf += next(it)
        if buf[:4] == b"PK\x07\x08":  # spanned-archive marker
            buf = buf[4:]
        sig, _, _, method, *_rest, nlen, elen = struct.unpack("<4s5H3L2H", buf[:30])
        if sig != b"PK\x03\x04" or method != 8:
            raise ArchiveError(f"unexpected zip layout (sig={sig!r}, method={method})")
        while len(buf) < 30 + nlen + elen:
            buf += next(it)
        buf = buf[30 + nlen + elen:]
        dec, crc, size = zlib.decompressobj(-15), 0, 0
        with open(out, "wb") as o:
            while True:
                data = dec.decompress(buf)
                o.write(data)
                crc = zlib.crc32(data, crc)
                size += len(data)
                if dec.eof:
                    break
                buf = next(it)
    except (StopIteration, zlib.error) as e:
        raise ArchiveError(f"archive is truncated or corrupt ({type(e).__name__})") from None
    # a zip64 entry stores 0xFFFFFFFF as its 32-bit size; the CRC still covers it
    if crc != expected_crc or (expected_size != 0xFFFFFFFF and size != expected_size):
        raise ArchiveError("extracted database failed its CRC/size check")


# --- slim index --------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE parts (
    lcsc INTEGER PRIMARY KEY, category TEXT, subcategory TEXT, mfr TEXT, manufacturer TEXT,
    package TEXT, joints INTEGER, tier INTEGER, description TEXT, datasheet TEXT, stock INTEGER,
    price TEXT, attributes TEXT, rohs INTEGER, assembly_process TEXT,
    checked INTEGER   -- when the mirror last fetched this part's stock and price from JLCPCB (epoch)
);
CREATE TABLE cats (category TEXT, subcategory TEXT, n INTEGER, n_basic INTEGER, n_preferred INTEGER);
CREATE TABLE attr_info (subcategory TEXT, name TEXT, n INTEGER, numeric INTEGER, vmin REAL, vmax REAL, examples TEXT);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""
RAW_COLUMNS = {"lcsc", "category", "subcategory", "mfr", "manufacturer", "package", "joints", "library_type",
               "preferred", "description", "datasheet", "stock", "price", "attributes", "rohs", "assembly_process",
               "present", "fetched_at"}


def _build_slim(raw: Path, out: Path, source_time: int | None = None) -> int:
    source_time = source_time or int(time.time())
    if out.exists():
        out.unlink()
    con = sqlite3.connect(f"file:{urllib.parse.quote(str(out))}", uri=True)
    con.executescript(SCHEMA)
    con.execute("ATTACH DATABASE ? AS raw", (_ro_uri(raw),))
    have = {r[1] for r in con.execute("PRAGMA raw.table_info(jlc_components)")}
    if not RAW_COLUMNS <= have:
        raise RuntimeError("upstream jlcparts database format changed (jlc_components is missing "
                           f"{sorted(RAW_COLUMNS - have) or 'entirely'}); this version of jlcparts-mcp cannot read it")
    _stage("copying in-stock parts")
    con.execute("""
        INSERT INTO parts
        SELECT lcsc, category, subcategory, mfr, manufacturer, package, joints,
               CASE WHEN library_type = 'base' THEN 0 WHEN preferred = 1 THEN 1 ELSE 2 END,
               description, datasheet, stock, price, attributes, rohs, assembly_process, fetched_at
        FROM raw.jlc_components WHERE present = 1 AND stock > 0""")
    con.commit()
    con.execute("DETACH DATABASE raw")
    count, fresh = con.execute("SELECT count(*), coalesce(sum(checked > ?), 0) FROM parts",
                               (source_time - 30 * 86400,)).fetchone()
    inferred = _infer_categories(con)
    uncategorized = con.execute("SELECT count(*) FROM parts WHERE coalesce(subcategory, '') = ''").fetchone()[0]

    _stage("indexing")
    con.executescript("""
        CREATE INDEX parts_subcat ON parts(subcategory);
        CREATE INDEX parts_mfr ON parts(mfr COLLATE NOCASE);
        INSERT INTO cats SELECT category, subcategory, count(*), sum(tier = 0), sum(tier = 1)
            FROM parts GROUP BY 1, 2;
        CREATE VIRTUAL TABLE fts USING fts5(mfr, manufacturer, package, subcategory, category, description, attrs,
                                            content='', tokenize='unicode61');
        INSERT INTO fts(rowid, mfr, manufacturer, package, subcategory, category, description, attrs)
            SELECT lcsc, mfr, manufacturer, package, subcategory, category, description,
                   (SELECT group_concat(value, ' ') FROM json_each(attributes)) FROM parts;
    """)

    _stage("profiling attributes")
    values: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for sub, attrs in con.execute("SELECT subcategory, attributes FROM parts"):
        try:
            for k, v in json.loads(attrs).items():
                values[(sub, k)][str(v)] += 1
        except (ValueError, AttributeError, TypeError):   # malformed or NULL attributes
            pass
    rows = []
    for (sub, name), counter in values.items():
        n = sum(counter.values())
        nums = [(units.parse_intervals(v), c) for v, c in counter.items()]
        numeric = sum(c for iv, c in nums if iv)
        flat = [x for iv, _ in nums for lo, hi in iv for x in (lo, hi)]
        is_num = numeric >= 0.8 * n and flat
        rows.append((sub, name, n, int(bool(is_num)), min(flat) if is_num else None,
                     max(flat) if is_num else None, json.dumps([v for v, _ in counter.most_common(12)], ensure_ascii=False)))
    con.executemany("INSERT INTO attr_info VALUES (?,?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO meta VALUES (?,?)", [
        ("index_version", str(INDEX_VERSION)), ("built_at", str(int(time.time()))), ("source_time", str(int(source_time))),
        ("n_parts", str(count)), ("n_fresh_30d", str(fresh)),
        ("n_category_inferred", str(inferred)), ("n_uncategorized", str(uncategorized))])
    con.commit()
    con.close()
    return count


def _infer_categories(con: sqlite3.Connection) -> int:
    """About 30% of upstream rows have no category. Their description usually ends with the subcategory name
    ('... 0402 Chip Resistor - Surface Mount ROHS'), so take it from there; returns the number of rows filled in."""
    owner: dict[str, str] = {}   # subcategory -> the category holding most of its parts
    for cat, sub, _ in con.execute("SELECT category, subcategory, count(*) FROM parts WHERE coalesce(subcategory, '') != '' "
                                   "GROUP BY 1, 2 ORDER BY 3"):
        owner[sub] = cat
    by_last_word: dict[str, list[str]] = defaultdict(list)
    for sub in owner:
        by_last_word[sub.split()[-1]].append(sub)
    for subs in by_last_word.values():
        subs.sort(key=len, reverse=True)
    updates = []
    for lcsc, desc in con.execute("SELECT lcsc, description FROM parts WHERE coalesce(subcategory, '') = ''"):
        d = (desc or "").strip()
        if d.endswith(" ROHS"):
            d = d[:-5].rstrip()
        if not d:
            continue
        for sub in by_last_word.get(d.split()[-1], ()):
            if d.endswith(sub) and (len(d) == len(sub) or d[-len(sub) - 1] == " "):
                updates.append((owner[sub], sub, lcsc))
                break
    con.executemany("UPDATE parts SET category = ?, subcategory = ? WHERE lcsc = ?", updates)
    con.commit()
    return len(updates)


def _parse_http_date(value: str | None) -> int | None:
    try:
        return int(parsedate_to_datetime(value).timestamp())
    except (TypeError, ValueError):
        return None


def build(force: bool = False) -> str:
    """Download and rebuild the index if upstream changed. Returns a human-readable outcome."""
    if not _build_lock.acquire(blocking=False):
        return "A database update is already running."
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(DATA_DIR / "build.lock", "w")
    if fcntl:
        try:   # other server processes (one per MCP client session) and the CLI share the data directory
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            _build_lock.release()
            return "Another process is already updating the database."
    progress.update(running=True, error=None)
    work = DATA_DIR / f"work-{os.getpid()}"
    tmp = DATA_DIR / f"parts.sqlite3.new-{os.getpid()}"
    try:
        upstream = head_last_modified()
        if not force:
            st = status()
            if st.get("index_version") == INDEX_VERSION and st.get("source_last_modified") == upstream:
                return f"Already up to date (upstream last modified {upstream})."
        raw = work / "cache.sqlite3"
        for attempt in (1, 2):
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir()
            parts = _download(work)
            _stage("extracting")
            try:
                _extract(parts, raw)
                break
            except ArchiveError as e:   # upstream republishes several times a day; a mixed download fails its CRC
                if attempt == 2:
                    raise
                log(f"{e}; downloading again")
                upstream = head_last_modified()
        for p in parts:
            p.unlink()
        count = _build_slim(raw, tmp, _parse_http_date(upstream))
        con = sqlite3.connect(tmp)
        con.execute("INSERT OR REPLACE INTO meta VALUES ('source_last_modified', ?)", (upstream or "",))
        con.commit()
        con.close()
        os.replace(tmp, DB_PATH)  # atomic: running searches keep the old file open
        return f"Database updated: {count:,} in-stock parts (upstream last modified {upstream})."
    except Exception as e:
        progress["error"] = f"{type(e).__name__}: {e}"
        log(f"build failed: {progress['error']}")
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
        tmp.unlink(missing_ok=True)
        progress.update(running=False, stage="idle")
        lock.close()   # releases the flock
        _build_lock.release()


def _meta() -> dict:
    con = sqlite3.connect(_ro_uri(DB_PATH), uri=True)
    try:
        return dict(con.execute("SELECT key, value FROM meta"))
    finally:
        con.close()


def status(check_upstream: bool = False) -> dict:
    out = {"database": str(DB_PATH), "exists": DB_PATH.exists(), **progress}
    if DB_PATH.exists():
        try:
            meta = _meta()
        except sqlite3.DatabaseError as e:
            out["index_error"] = f"the index file is unreadable ({e}); update_database(force=true) rebuilds it"
            return out
        fmt = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(t)))
        source_time = meta.get("source_time") or _parse_http_date(meta.get("source_last_modified")) or meta.get("built_at")
        out.update(index_version=int(meta.get("index_version", 1)), n_parts=int(meta.get("n_parts", 0)),
                   n_parts_checked_within_30_days=int(meta.get("n_fresh_30d", 0)),
                   n_category_inferred=int(meta.get("n_category_inferred", 0)),
                   n_uncategorized=int(meta.get("n_uncategorized", 0)),
                   source_last_modified=meta.get("source_last_modified"),
                   built_at=fmt(meta["built_at"]) if "built_at" in meta else None,
                   snapshot_age_days=round((time.time() - int(source_time)) / 86400, 1) if source_time else None,
                   freshness="the mirror re-checks each part at JLCPCB only every few weeks; every result carries "
                             "its own stock_checked date, and get_part / check_bom ask JLCPCB live")
        if out["index_version"] != INDEX_VERSION:
            out["note"] = "index was built by another version of jlcparts-mcp; update_database will rebuild it"
    if check_upstream:
        try:
            upstream = head_last_modified()
            out.update(upstream_last_modified=upstream, update_available=upstream != out.get("source_last_modified"))
        except OSError as e:
            out["upstream_error"] = str(e)
    return out


def snapshot_age_days() -> float | None:
    return status().get("snapshot_age_days")


def connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise RuntimeError("Parts database not built yet. Call update_database (takes a minute or two) "
                           "or run `jlcparts-mcp build` in a shell.")
    con = sqlite3.connect(_ro_uri(DB_PATH), uri=True)
    con.row_factory = sqlite3.Row
    try:
        version = con.execute("SELECT value FROM meta WHERE key = 'index_version'").fetchone()
    except sqlite3.DatabaseError as e:
        con.close()
        raise RuntimeError(f"The parts index is unreadable ({e}). Call update_database with force=true "
                           "(or run `jlcparts-mcp build --force`) to rebuild it.") from None
    if not version or int(version[0]) != INDEX_VERSION:
        con.close()
        raise RuntimeError("The parts index was built by another version of jlcparts-mcp. Call update_database "
                           "(or run `jlcparts-mcp build`) to rebuild it.")
    return con
