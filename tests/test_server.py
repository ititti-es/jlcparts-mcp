import sqlite3

import pytest

from jlcparts_mcp import db, server
from jlcparts_mcp.server import AttrFilter as A, BomItem

from jlcparts_mcp.live import LiveError

from conftest import PARTS, T0, live_part, make_raw


def lcscs(result):
    return [r["lcsc"] for r in result["results"]]


def test_index_only_holds_stocked_parts(index):
    assert db.status()["n_parts"] == len([p for p in PARTS if p[6] > 0])
    assert server.get_part("C5001")["found"] is False


def test_parametric_search_ranks_basic_then_preferred_then_extended(index):
    r = server.search_parts(category="chip resistor", attributes=[A(name="Resistance", value="10k")])
    assert lcscs(r)[:2] == ["C1001", "C1004"]                 # basic, then preferred
    assert set(lcscs(r)) == {"C1001", "C1002", "C1004", "C1005"}
    # within a tier, a part with a comfortable stock buffer (10x quantity) comes before a nearly sold-out one
    r = server.search_parts(category="chip resistor", attributes=[A(name="Resistance", value="10k")], quantity=100)
    assert lcscs(r) == ["C1001", "C1004", "C1002", "C1005"]
    assert r["results"][0]["category"] == "Chip Resistor - Surface Mount"
    assert r["results"][0]["stock_checked"] == "mirror 2026-09-20"       # each row says how old its stock figure is


def test_filters_combine(index):
    r = server.search_parts(category="resistor", package="0402",
                            attributes=[A(name="resistance", value="10k"), A(name="Tolerance", op="lte", value=1)])
    assert set(lcscs(r)) == {"C1001", "C1005"}
    r = server.search_parts(category="mlcc", attributes=[A(name="Capacitance", value="100nF"),
                                                         A(name="Voltage Rating", op="gte", value="25V"),
                                                         A(name="Temperature Coefficient", value="x7r")])
    assert lcscs(r) == ["C2002"]


def test_quantity_drives_stock_and_price_tier(index):
    assert "C1003" in lcscs(server.search_parts(category="resistor", attributes=[A(name="Resistance", value=27)]))
    assert server.search_parts(category="resistor", attributes=[A(name="Resistance", value=27)], quantity=5)["results"] == []
    r = server.search_parts(part_number="0402WGF1002", quantity=200)["results"][0]
    assert r["unit_price_usd"] == 0.001 and r["line_total_usd"] == pytest.approx(0.2)
    assert server.search_parts(part_number="0402WGF1002")["results"][0]["unit_price_usd"] == 0.002


def test_malformed_price_does_not_break_search(index):
    r = server.search_parts(category="resistor", sort="price", max_unit_price=1)
    assert "C1005" not in lcscs(r)                             # no price -> cannot satisfy a price limit
    assert server.get_part("C1005")["parts"][0]["unit_price_usd"] is None


def test_range_and_list_attributes(index):
    r = server.search_parts(category="ldo", attributes=[A(name="Output Voltage", value="3.3V")])
    assert set(lcscs(r)) == {"C3001", "C3002", "C3003"}        # fixed, one of two outputs, inside adjustable range
    r = server.search_parts(category="ldo", attributes=[A(name="Output Current", op="gte", value="600mA")])
    assert set(lcscs(r)) == {"C3001", "C3003"}


def test_ambiguous_attribute_name_is_an_error(index):
    with pytest.raises(ValueError, match="ambiguous"):
        server.search_parts(category="ldo", attributes=[A(name="Voltage", op="gte", value=5)])
    r = server.search_parts(category="ldo", attributes=[A(name="supply", op="gte", value=9)])  # unambiguous substring
    assert set(lcscs(r)) == {"C3002", "C3003"} and "resolved to" in r["notes"][0]
    with pytest.raises(ValueError, match="Unknown attribute"):
        server.search_parts(category="ldo", attributes=[A(name="Capacitance", value="1uF")])


def test_free_text_and_category_tell_crystal_from_oscillator(index):
    r = server.search_parts(query="12.288MHz SMD2520")
    assert {x["lcsc"]: x["category"] for x in r["results"]} == {"C4001": "Crystals", "C4002": "Oscillators"}
    assert lcscs(server.search_parts(query="12.288MHz", category="oscillators")) == ["C4002"]
    assert server.search_parts(query="12 MHz 288")["results"] == []    # words are phrases, not loose tokens


def test_library_filter_and_unknown_category(index):
    assert lcscs(server.search_parts(category="resistor", library="basic")) == ["C1001"]
    assert set(lcscs(server.search_parts(category="resistor", library="basic_or_preferred"))) == {"C1001", "C1004"}
    with pytest.raises(ValueError, match="No category matches"):
        server.search_parts(category="flux capacitors")


def test_describe_and_list_categories(index):
    d = server.describe_category("ldo")["categories"][0]
    assert d["in_stock_parts"] == 3 and d["top_packages"] == {"SOT-23-5": 2, "SOT-23-6": 1}
    assert any(a.startswith("Output Voltage [3 parts, numeric 1.2..5") for a in d["attributes"])
    assert len(server.list_categories()) == 8      # incl. the uncategorised rows
    assert len(server.list_categories("oscillators")) == 1                 # subcategory names win ...
    assert len(server.list_categories("crystals, oscillators, resonators")) == 2   # ... an exact category selects all
    assert len(server.list_categories("power ldo")) == 1                   # words may span category and subcategory


def test_get_part_by_number_and_mpn(index):
    p = server.get_part("c3001", quantity=10)["parts"][0]
    assert p["mpn"] == "AP2112K-3.3TRG1" and p["unit_price_usd"] == 0.15
    assert p["price_tiers_usd"] == [{"from_qty": 1, "unit_price": 0.17}, {"from_qty": 10, "unit_price": 0.15}]
    assert server.get_part("ap2112k-3.3trg1")["parts"][0]["lcsc"] == "C3001"


def test_check_bom_merges_duplicate_lines(index):
    r = server.check_bom([BomItem(lcsc="C1001", quantity=60), BomItem(lcsc="c1001", quantity=60),
                          BomItem(lcsc="C1003", quantity=10), BomItem(lcsc="C999999"), BomItem(lcsc="R12")])
    by = {l["lcsc"]: l for l in r["lines"]}
    assert by["C1001"]["quantity"] == 120 and by["C1001"]["merged_lines"] == 2
    assert by["C1001"]["unit_price_usd"] == 0.001              # tier reached only by the merged quantity
    assert by["C1003"]["status"] == "insufficient stock (4)"
    assert "JLCPCB was not asked" in by["C999999"]["status"] and by["R12"]["status"] == "not an LCSC number"
    assert by["C1001"]["stock_checked"] == "mirror 2026-09-20" and r["lines_from_mirror"] == 2
    assert r["problems"] == 3 and r["extended_parts"] == 1


def test_find_alternatives(index):
    r = server.find_alternatives("C1002", must_match=["Resistance"])
    assert [a["lcsc"] for a in r["alternatives"]] == ["C1001", "C1005"]     # same package only; fewest differences first
    assert r["alternatives"][0]["differences"] == {"Tolerance": ["±5%", "±1%"]}
    assert r["alternatives"][1]["differences"]["Power(Watts)"] == ["62.5mW", None]
    assert {a["lcsc"] for a in server.find_alternatives("C1002", must_match=["Resistance"], same_package=False)["alternatives"]} \
        == {"C1001", "C1004", "C1005"}


def test_stale_data_is_flagged(index, monkeypatch):
    assert any("days old" in n for n in server.search_parts(category="ldo")["notes"])   # T0 is long ago
    monkeypatch.setattr(server.time, "time", lambda: T0 + 3600)
    assert "notes" not in server.search_parts(category="ldo")


def test_index_from_another_version_is_rejected(index):
    con = sqlite3.connect(index)
    con.execute("UPDATE meta SET value = '1' WHERE key = 'index_version'")
    con.commit()
    con.close()
    with pytest.raises(RuntimeError, match="another version"):
        server.search_parts(query="x")


def test_upstream_format_change_is_reported(tmp_path):
    raw = tmp_path / "raw.sqlite3"
    sqlite3.connect(raw).execute("CREATE TABLE jlc_components (lcsc INTEGER, stock INTEGER)").connection.commit()
    with pytest.raises(RuntimeError, match="format changed"):
        db._build_slim(raw, tmp_path / "out.sqlite3")


# --- live data from JLCPCB (faked) ---------------------------------------------------------------

def test_get_part_prefers_live_stock_and_price(index, fake_jlc):
    fake_jlc[3001] = live_part(3001, stock=7, price="1-:0.9", tier=1)
    p = server.get_part("C3001")["parts"][0]
    assert (p["stock"], p["unit_price_usd"], p["library"], p["stock_checked"]) == (7, 0.9, "preferred", "live")
    assert p["mpn"] == "AP2112K-3.3TRG1" and p["attributes"]["Output Voltage"] == "3.3V"    # the rest stays from the mirror
    assert server.get_part("C3001", live=False)["parts"][0]["stock"] == 12_000


def test_get_part_finds_parts_the_mirror_lacks(index, fake_jlc):
    fake_jlc[777] = live_part(777, stock=30_444, mfr="CA-IS3722LS")
    assert server.get_part("C777")["parts"][0]["stock"] == 30_444
    assert server.get_part("ca-is3722ls")["parts"][0]["lcsc"] == "C777"                     # by part number, via JLCPCB's search
    assert server.get_part("C778") == {"found": False, "message": server.get_part("C778")["message"]}
    assert "does not list" in server.get_part("C778")["message"]
    assert "was not asked" in server.get_part("C777", live=False)["message"]               # the mirror alone cannot say


def test_get_part_falls_back_to_the_mirror_when_jlcpcb_is_unreachable(index, fake_jlc):
    fake_jlc[3001] = LiveError("timed out")
    r = server.get_part("C3001")
    assert r["parts"][0]["stock"] == 12_000 and r["parts"][0]["stock_checked"].startswith("mirror 2026-")
    assert "could not be asked" in r["note"]


def test_check_bom_uses_live_figures(index, fake_jlc):
    fake_jlc.update({1001: live_part(1001, stock=50, price="1-:0.01", tier=0),      # mirror says 100,000
                     777: live_part(777, stock=30_444),                              # not in the mirror at all
                     3001: LiveError("HTTP 503")})                                   # 3002 is absent: JLCPCB does not list it
    r = server.check_bom([BomItem(lcsc="C1001", quantity=120), BomItem(lcsc="C777", quantity=5),
                          BomItem(lcsc="C3001", quantity=5), BomItem(lcsc="C3002", quantity=5), BomItem(lcsc="C4242")])
    by = {l["lcsc"]: l for l in r["lines"]}
    assert by["C1001"]["status"] == "insufficient stock (50)" and by["C1001"]["stock_checked"] == "live"
    assert by["C777"]["status"] == "ok" and by["C777"]["stock_checked"] == "live"
    assert by["C3001"]["status"] == "ok" and by["C3001"]["stock_checked"].startswith("mirror ")
    assert "does not list it now" in by["C3002"]["status"]
    assert by["C4242"]["status"] == "JLCPCB does not list this part"
    assert r["problems"] == 3 and r["lines_from_mirror"] == 2 and "could not be asked about 1 part" in r["notes"][0]


def test_search_can_recheck_its_results_live(index, fake_jlc):
    fake_jlc[3001] = live_part(3001, stock=3)
    r = server.search_parts(category="ldo", quantity=10, live=True)
    row = next(x for x in r["results"] if x["lcsc"] == "C3001")
    assert row["stock"] == 3 and row["stock_checked"] == "live" and "fewer than the 10 needed" in row["warning"]
    other = next(x for x in r["results"] if x["lcsc"] == "C3003")
    assert "does not list it now" in other["stock_checked"]


def test_find_alternatives_for_an_original_only_jlcpcb_knows(index, fake_jlc):
    fake_jlc[777] = live_part(777, stock=0, attributes='{"Output Voltage": "3.3V", "Output Current": "600mA"}')
    fake_jlc[3001] = live_part(3001, stock=9_000)
    r = server.find_alternatives("C777")
    assert [a["lcsc"] for a in r["alternatives"]][0] == "C3001" and r["alternatives"][0]["stock_checked"] == "live"
    assert r["original"]["stock"] == 0 and "came from JLCPCB live" in r["notes"][0]
    assert server.find_alternatives("C778")["found"] is False


# --- regressions from the pre-release review ------------------------------------------------------

def test_attribute_resolves_per_subcategory(index):
    # USB ICs resolve "Supply Current" exactly; Digital Isolators only by substring. C6001 has both keys and
    # must be tested on its own "Supply Current" (12 mA), not on its "Quiescent Supply Current" (1 uA).
    r = server.search_parts(category="Interface ICs", attributes=[A(name="Supply Current", op="lte", value="1mA")])
    assert lcscs(r) == ["C6002"]
    r = server.search_parts(category="Interface ICs", attributes=[A(name="Supply Current", op="gte", value="10mA")])
    assert lcscs(r) == ["C6001"]


def test_attribute_case_variants_are_both_used(index):
    r = server.search_parts(category="isolators", attributes=[A(name="Single Supply", value="yes")])
    assert set(lcscs(r)) == {"C6002", "C6003"}


def test_filter_values_report_how_they_were_read(index):
    r = server.search_parts(category="resistor", attributes=[A(name="Resistance", value="10kohm")])
    assert "C1001" in lcscs(r) and "Resistance eq 10000 (read from '10kohm')" in r["notes"]


def test_missing_categories_are_inferred_from_the_description(index):
    p = server.get_part("C7001", live=False)["parts"][0]
    assert p["category"] == "Resistors / Chip Resistor - Surface Mount"
    assert "C7001" in lcscs(server.search_parts(category="chip resistor", attributes=[A(name="Resistance", value="93.1k")]))
    st = db.status()
    assert st["n_category_inferred"] == 1 and st["n_uncategorized"] == 1


def test_check_bom_rejects_non_positive_quantities_and_flags_missing_prices(index):
    r = server.check_bom([BomItem(lcsc="C1001", quantity=0), BomItem(lcsc="C1002", quantity=-5),
                          BomItem(lcsc="C1005", quantity=10), BomItem(lcsc="C2001", quantity=10)], live=False)
    by = {l["lcsc"]: l for l in r["lines"]}
    assert by["C1001"]["status"] == by["C1002"]["status"] == "quantity must be at least 1"
    assert by["C1005"]["status"] == "ok" and "not included" in by["C1005"]["price_note"]
    assert r["problems"] == 2 and r["lines_without_price"] == 1 and r["parts_total_usd"] == 0.04


def test_check_bom_shows_the_minimum_order(index, fake_jlc):
    fake_jlc[3001] = live_part(3001, stock=10_000, min_order=50)
    line = server.check_bom([BomItem(lcsc="C3001", quantity=10)])["lines"][0]
    assert line["min_order"] == 50 and "sells at least 50" in line["price_note"]


def test_null_attributes_do_not_break_the_build(tmp_path):
    raw = make_raw(tmp_path / "raw.sqlite3", PARTS + [(8001, ("Resistors", "Chip Resistor - Surface Mount"), "NULL-ATTRS",
                                                       "0402", "expand", 0, 10, "1-:0.1", None, "x")])
    db._build_slim(raw, tmp_path / "out.sqlite3", source_time=T0)
    assert sqlite3.connect(tmp_path / "out.sqlite3").execute("SELECT attributes FROM parts WHERE lcsc = 8001").fetchone() == (None,)


def test_a_broken_index_can_be_rebuilt(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "parts.sqlite3")
    db.DB_PATH.write_bytes(b"")                                   # e.g. left behind by a crash
    assert "unreadable" in db.status()["index_error"]
    with pytest.raises(RuntimeError, match="force=true"):
        server.search_parts(query="x")

    class Reached(Exception):
        pass
    monkeypatch.setattr(db, "head_last_modified", lambda: "x")
    monkeypatch.setattr(db, "_download", lambda work: (_ for _ in ()).throw(Reached()))
    with pytest.raises(Reached):                                  # force goes straight to the download
        db.build(force=True)


def test_builds_in_other_processes_are_not_trampled(tmp_path, monkeypatch):
    fcntl = pytest.importorskip("fcntl")
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "head_last_modified", lambda: pytest.fail("should not start"))
    with open(tmp_path / "build.lock", "w") as other:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert db.build() == "Another process is already updating the database."
    assert not db.progress["running"] and db._build_lock.acquire(blocking=False)
    db._build_lock.release()


def test_tools_are_registered_async(index):
    import asyncio
    tool = server.mcp._tool_manager._tools["check_bom"]
    assert tool.is_async and set(tool.parameters["properties"]) == {"items", "live"}
    result = asyncio.run(server.mcp.call_tool("get_part", {"part": "C3001", "live": False}))
    assert "AP2112K-3.3TRG1" in str(result)
