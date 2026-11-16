"""A tiny synthetic jlcparts source database, built into a real index with the production code."""
import json
import sqlite3

import pytest

from jlcparts_mcp import db, live

T0 = 1_790_000_000  # snapshot time of the first index
DAY = 86400

RES = ("Resistors", "Chip Resistor - Surface Mount")
CAP = ("Capacitors", "Multilayer Ceramic Capacitors MLCC - SMD/SMT")
LDO = ("Power Management ICs", "Linear Voltage Regulators (LDO)")
XTAL = ("Crystals, Oscillators, Resonators", "Crystals")
OSC = ("Crystals, Oscillators, Resonators", "Oscillators")
USB = ("Interface ICs", "USB ICs")
ISO = ("Interface ICs", "Digital Isolators")

# lcsc, (category, subcategory), mfr, package, library_type, preferred, stock, price, attributes
PARTS = [
    (1001, RES, "0402WGF1002TCE", "0402", "base", 0, 100_000, "1-99:0.002,100-:0.001", {"Resistance": "10kΩ", "Tolerance": "±1%", "Power(Watts)": "62.5mW"}),
    (1002, RES, "RC0402JR-0710KL", "0402", "expand", 0, 5_000, "1-:0.0005", {"Resistance": "10kΩ", "Tolerance": "±5%", "Power(Watts)": "62.5mW"}),
    (1003, RES, "RC0402JR-0727RL", "0402", "expand", 0, 4, "1-:0.001", {"Resistance": "27Ω", "Tolerance": "±5%", "Power(Watts)": "62.5mW"}),
    (1004, RES, "0603WAF1002T5E", "0603", "expand", 1, 20_000, "1-:0.003", {"Resistance": "10kΩ", "Tolerance": "±1%", "Power(Watts)": "100mW"}),
    (1005, RES, "BADPRICE-10K", "0402", "expand", 0, 900, "call us", {"Resistance": "10kΩ", "Tolerance": "±1%"}),
    (2001, CAP, "CL05B104KO5NNNC", "0402", "base", 0, 9_000_000, "1-:0.004", {"Capacitance": "100nF", "Voltage Rating": "16V", "Temperature Coefficient": "X7R"}),
    (2002, CAP, "CC0402KRX7R9BB104", "0402", "expand", 0, 50_000, "1-:0.006", {"Capacitance": "100nF", "Voltage Rating": "50V", "Temperature Coefficient": "X7R"}),
    (2003, CAP, "CL05A105KA5NQNC", "0402", "base", 0, 700_000, "1-:0.008", {"Capacitance": "1uF", "Voltage Rating": "25V", "Temperature Coefficient": "X5R"}),
    (3001, LDO, "AP2112K-3.3TRG1", "SOT-23-5", "expand", 0, 12_000, "1-9:0.17,10-:0.15", {"Output Voltage": "3.3V", "Output Current": "600mA", "Voltage - Supply": "6V", "Voltage Dropout": "250mV@(600mA)"}),
    (3002, LDO, "DUAL-LDO", "SOT-23-6", "expand", 0, 800, "1-:0.5", {"Output Voltage": "3.3V, 1.8V", "Output Current": "250mA, 500mA", "Voltage - Supply": "9V"}),
    (3003, LDO, "ADJ-LDO", "SOT-23-5", "expand", 0, 3_000, "1-:0.3", {"Output Voltage": "1.2V~5V", "Output Current": "1A", "Voltage - Supply": "18V"}),
    (4001, XTAL, "SX25Y012288BK1T001", "SMD2520-4P", "expand", 0, 3_000, "1-:0.1", {"Frequency": "12.288MHz", "Load Capacitance": "20pF"}),
    (4002, OSC, "YM4012M28833001", "SMD2520-4P", "expand", 0, 2_000, "1-:0.33", {"Frequency": "12.288MHz", "Output Type": "CMOS", "Voltage - Supply": "3.3V"}),
    (5001, RES, "NEVER-STOCKED", "0402", "expand", 0, 0, "", {"Resistance": "1kΩ"}),
    # one subcategory names it "Supply Current", the other only has "Quiescent Supply Current"; 6001 has both
    (6001, USB, "USB-HUB", "SOIC-8", "expand", 0, 1_000, "1-:1", {"Supply Current": "12mA", "Quiescent Supply Current": "1uA"}),
    (6002, ISO, "ISO-1", "SOIC-8", "expand", 0, 1_000, "1-:1", {"Quiescent Supply Current": "0.5mA", "Single supply": "Yes"}),
    (6003, ISO, "ISO-2", "SOIC-8", "expand", 0, 1_000, "1-:1", {"Quiescent Supply Current": "5mA", "Single Supply": "Yes"}),
    # upstream rows without a category; the description names the subcategory (or not)
    (7001, ("", ""), "ERJ2RKF9312X", "0402", "expand", 0, 2_000, "1-:0.01", {"Resistance": "93.1kΩ"},
     "93.1kΩ ±1% 0402 Chip Resistor - Surface Mount ROHS"),
    (7002, ("", ""), "HEATSINK-1", "-", "expand", 0, 50, "1-:0.5", {}, "- Heat sink / cooling fin"),
]


def make_raw(path, parts=PARTS):
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE jlc_components (
        lcsc INTEGER PRIMARY KEY, present INTEGER, category TEXT, subcategory TEXT, mfr TEXT, package TEXT,
        joints INTEGER, manufacturer TEXT, library_type TEXT, preferred INTEGER, description TEXT, datasheet TEXT,
        stock INTEGER, price TEXT, attributes TEXT, rohs INTEGER, assembly_process TEXT, fetched_at INTEGER)""")
    for lcsc, (cat, sub), mfr, package, lib, preferred, stock, price, attrs, *rest in parts:
        desc = rest[0] if rest else " ".join(attrs.values()) + f" {package} {sub}"
        con.execute("INSERT INTO jlc_components VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (lcsc, 1, cat, sub, mfr, package, 2, "ACME", lib, preferred, desc, f"https://example.com/{lcsc}.pdf",
                     stock, price, json.dumps(attrs, ensure_ascii=False) if attrs is not None else None, 1, "SMT", T0 - (lcsc % 50) * DAY))   # checked 1-49 days before the snapshot
    con.commit()
    con.close()
    return path


@pytest.fixture
def raw(tmp_path):
    return make_raw(tmp_path / "raw.sqlite3")


@pytest.fixture
def index(tmp_path, raw, monkeypatch):
    """Build the index from the synthetic source and point the server at it."""
    path = tmp_path / "parts.sqlite3"
    db._build_slim(raw, path, source_time=T0)
    monkeypatch.setattr(db, "DB_PATH", path)
    return path


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never talk to JLCPCB; the ones about live data install `fake_jlc`."""
    monkeypatch.setattr(live, "ENABLED", False)
    monkeypatch.setattr(live, "find", lambda *a, **k: pytest.fail("network access in a test"))


def live_part(lcsc, stock, price="1-:0.2", tier=2, **extra):
    part = {"lcsc": lcsc, "category": "Power Management ICs", "subcategory": "Linear Voltage Regulators (LDO)",
            "mfr": f"LIVE-{lcsc}", "manufacturer": "ACME", "package": "SOT-23-5", "joints": 5, "tier": tier,
            "description": "", "datasheet": "", "stock": stock, "price": price, "attributes": "{}", "rohs": 1,
            "assembly_process": None, "checked": None, "live": True, "min_order": 1, "min_placement": 5}
    return {**part, **extra}


@pytest.fixture
def fake_jlc(monkeypatch):
    """A stand-in for JLCPCB: fill the dict with {lcsc: part | None | LiveError}; unknown numbers are not listed."""
    table = {}
    monkeypatch.setattr(live, "ENABLED", True)
    monkeypatch.setattr(live, "lookup_many", lambda numbers: {n: table.get(n) for n in dict.fromkeys(numbers)})
    monkeypatch.setattr(live, "find", lambda keyword, timeout=20: [p for p in table.values() if isinstance(p, dict)])
    return table
