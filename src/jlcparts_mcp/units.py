"""Parse JLCPCB attribute strings ("10kΩ", "100nF", "1.8V~3.6V", "180mV@(150mA)", "1/16W") into numbers."""
import re
from functools import lru_cache

_PREFIX = {
    "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3,
    "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12,
}
# A prefix letter only counts when what follows is a unit (or nothing), so "ppm" and "mil" stay unscaled.
# Units are matched case-insensitively ("10uf", "100ma", "10kohm"); the prefix itself is case-sensitive (m vs M).
_UNITS = tuple(u.casefold() for u in ("Ω", "Ω", "ohm", "Hz", "Ah", "bps", "bit", "F", "H", "V", "A", "W", "s", "B", "m"))
_NUMBER = r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"
_ENDPOINT = re.compile(rf"[\s±+@(]*({_NUMBER})(?:\s*/\s*(\d+(?:\.\d+)?))?\s*(.*)", re.S)
# "4k7" = 4.7k, "2n2" = 2.2n, "0R1" = 0.1, "3V3" = 3.3
_SHORTHAND = re.compile(r"[\s±+]*(-?\d+)([pnuµμmkKMGTRrVv])(\d+)(.*)", re.S)
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")    # "100,000 cycles"; list items are separated by ", "


def _scale(rest: str) -> float:
    if rest and rest[0] in _PREFIX:
        tail = rest[1:].casefold()
        if tail == "" or tail.startswith(_UNITS):
            if rest[0] == "m" and tail.startswith("hz") and not rest.startswith("mHz"):
                return 1e6          # "10mhz" means megahertz; millihertz is never meant for parts
            return _PREFIX[rest[0]]
    return 1.0


def _endpoint(text: str) -> float | None:
    m = _SHORTHAND.fullmatch(text)
    if m and (m.group(4).strip() == "" or m.group(4).strip().casefold().startswith(_UNITS)):
        letter = m.group(2)
        factor = 1.0 if letter in "RrVv" else _PREFIX[letter]
        whole = m.group(1)
        sign = -1.0 if whole.startswith("-") else 1.0
        return sign * float(f"{whole.lstrip('-')}.{m.group(3)}") * factor
    m = _ENDPOINT.match(text)
    if not m:
        return None
    value = float(m.group(1))
    if m.group(2):
        denominator = float(m.group(2))
        if denominator == 0:
            return None
        value /= denominator
    return value * _scale(m.group(3).strip())


@lru_cache(maxsize=500_000)
def parse_intervals(text: str) -> tuple[tuple[float, float], ...]:
    """'3.3V, 1.8V' -> ((3.3, 3.3), (1.8, 1.8)); '1.8V~3.6V' -> ((1.8, 3.6),); '100,000 cycles' -> ((1e5, 1e5),)."""
    out = []
    for item in _THOUSANDS.sub("", text).split(","):
        ends = [_endpoint(e) for e in item.split("~", 1)]
        if ends[0] is None:
            continue
        hi = ends[1] if len(ends) > 1 and ends[1] is not None else ends[0]
        out.append((min(ends[0], hi), max(ends[0], hi)))
    return tuple(out)


def parse_number(value) -> float | None:
    """Parse a user-supplied filter value ('10k', '100nF', 3.3) to a float, or None if not numeric."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    iv = parse_intervals(str(value).strip())
    return iv[0][0] if len(iv) == 1 and iv[0][0] == iv[0][1] else None


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-6 * max(abs(a), abs(b), 1e-30)


def numeric_match(text: str | None, op: str, x: float) -> bool:
    """Compare an attribute string with a number. A range ('1.8V~3.6V') equals every value it covers;
    gte/gt look at its upper end and lte/lt at its lower end. A list ('3.3V, 1.8V') matches if any item does."""
    if not text:
        return False
    if op == "ne":   # the negation of eq, so '3.3V, 1.8V' is not "ne 3.3"
        return not numeric_match(text, "eq", x)
    for lo, hi in parse_intervals(text):
        if op == "eq" and (lo - 1e-9 * abs(lo) <= x <= hi + 1e-9 * abs(hi) or _close(lo, x) or _close(hi, x)):
            return True
        if op == "gte" and (hi >= x or _close(hi, x)):
            return True
        if op == "lte" and (lo <= x or _close(lo, x)):
            return True
        if op == "gt" and hi > x:
            return True
        if op == "lt" and lo < x:
            return True
    return False


def text_match(text: str | None, op: str, s: str) -> bool:
    if text is None:
        return False
    t, s = text.casefold(), s.casefold()
    if op == "contains":
        return s in t
    items = [i.strip() for i in t.split(",")]
    return (s in items or t == s) if op == "eq" else (s not in items and t != s)
