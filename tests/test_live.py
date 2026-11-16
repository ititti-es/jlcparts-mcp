import io
import json
from pathlib import Path

import pytest

from jlcparts_mcp import live
from jlcparts_mcp.server import _tiers

SAMPLE = Path(__file__).parent / "data" / "live_C7503141.json"


@pytest.fixture
def jlc_answers(monkeypatch):
    """Serve canned JSON instead of calling JLCPCB (overrides the autouse network block for live.find)."""
    monkeypatch.undo()
    answers = []
    def urlopen(req, timeout=None):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(answer if isinstance(answer, bytes) else json.dumps(answer).encode())
    monkeypatch.setattr(live.urllib.request, "urlopen", urlopen)
    return answers


def test_parse_real_payload(jlc_answers):
    jlc_answers.append(SAMPLE.read_bytes())
    p = live.lookup(7503141)
    assert (p["mfr"], p["package"], p["tier"], p["live"]) == ("YM4012M28833001", "SMD2520-4P", 2, True)
    assert (p["category"], p["subcategory"]) == ("Crystals, Oscillators, Resonators", "Crystal Oscillators")
    assert p["stock"] == 2237 and _tiers(p["price"])[0] == (1, 0.3306) and _tiers(p["price"])[-1][0] == 1000
    assert json.loads(p["attributes"])["Output Type"] == "CMOS"
    assert p["joints"] is None                 # the payload's encapsulationNumber is the reel size, not pins


def test_fuzzy_results_are_not_mistaken_for_the_part(jlc_answers):
    jlc_answers.append(SAMPLE.read_bytes())
    assert live.lookup(999) is None            # JLCPCB's search returns near matches for unknown numbers


def test_failures_become_live_errors(jlc_answers):
    jlc_answers += [OSError("network down"), b"<html>blocked</html>", {"code": 500, "message": "busy"}]
    for _ in range(3):
        with pytest.raises(live.LiveError):
            live.find("C1")
    jlc_answers += [OSError("network down"), SAMPLE.read_bytes()]
    result = live.lookup_many([1, 1])
    assert list(result) == [1] and isinstance(result[1], live.LiveError)   # one error does not raise


def test_odd_payloads_become_live_errors(jlc_answers):
    row = json.loads(SAMPLE.read_bytes())["data"]["componentPageInfo"]["list"][0]
    jlc_answers += [{"code": 200, "data": {"componentPageInfo": {"list": [{**row, "stockCount": "N/A"}]}}},
                    {"code": 200, "data": {"componentPageInfo": {"list": [{**row, "componentPrices": [None]}]}}}]
    for _ in range(2):
        with pytest.raises(live.LiveError, match="unexpected answer"):
            live.lookup(7503141)
