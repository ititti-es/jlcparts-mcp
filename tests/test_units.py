import pytest

from jlcparts_mcp.units import numeric_match, parse_intervals, parse_number, text_match


@pytest.mark.parametrize("text, expected", [
    ("10kΩ", 1e4), ("27Ω", 27), ("4.7uF", 4.7e-6), ("4.7µF", 4.7e-6), ("100nF", 1e-7), ("62.5mW", 0.0625),
    ("12.288MHz", 12.288e6), ("600mA", 0.6), ("±1%", 1), ("±100ppm/℃", 100), ("250mV@(600mA)", 0.25),
    ("64KB", 64e3), ("10k", 1e4), ("100n", 1e-7), ("16V", 16), ("3.3", 3.3), ("500mcd", 500), ("32 Bit", 32),
    # fractions, thousands separators, shorthand, exponents, lower-case units
    ("1/16W", 0.0625), ("1/2W", 0.5), ("100,000 cycles", 1e5), ("1,000,000", 1e6),
    ("4k7", 4700), ("2n2", 2.2e-9), ("3V3", 3.3), ("0R1", 0.1), ("1e-6", 1e-6),
    ("10uf", 1e-5), ("100nf", 1e-7), ("100ma", 0.1), ("10kOhm", 1e4), ("10mhz", 1e7), ("10MHz", 1e7),
])
def test_single_values(text, expected):
    assert parse_number(text) == pytest.approx(expected)


def test_prefix_needs_a_unit_after_it():
    assert parse_number("100ppm") == 100       # not 100 pico-"pm"
    assert parse_number("10mil") == 10         # not 10 milli-"il"
    assert parse_number("5pcs") == 5


def test_ranges_and_lists():
    assert parse_intervals("1.8V~3.6V") == ((1.8, 3.6),)
    assert parse_intervals("-40℃~+85℃") == ((-40, 85),)
    assert parse_intervals("3.3V, 1.8V") == ((3.3, 3.3), (1.8, 1.8))
    assert parse_intervals("X7R") == ()
    assert parse_number("1.8V~3.6V") is None
    assert parse_number("X7R") is None
    assert parse_number(3) == 3.0


def test_numeric_match():
    assert numeric_match("100nF", "eq", 1e-7)            # float noise between 100e-9 and 1e-7
    assert numeric_match("1.8V~3.6V", "eq", 3.3)         # a range equals what it covers
    assert not numeric_match("1.8V~3.6V", "eq", 5)
    assert numeric_match("-40℃~+85℃", "gte", 85) and numeric_match("-40℃~+85℃", "lte", -40)
    assert not numeric_match("-40℃~+85℃", "gte", 105)
    assert numeric_match("3.3V, 1.8V", "eq", 1.8)
    assert not numeric_match("3.3V, 1.8V", "ne", 3.3)    # ne is the negation of eq, not "some item differs"
    assert numeric_match("3.3V, 1.8V", "ne", 5)
    assert numeric_match("50V", "gt", 16) and not numeric_match("16V", "gt", 16)
    assert not numeric_match(None, "eq", 1) and not numeric_match("Thick Film", "eq", 1)


def test_text_match():
    assert text_match("X7R", "eq", "x7r")
    assert text_match("Over Current Protection, Thermal shutdown", "eq", "thermal shutdown")
    assert text_match("Over Current Protection, Thermal shutdown", "contains", "current")
    assert text_match("X5R", "ne", "X7R") and not text_match("X7R", "ne", "X7R")
    assert not text_match(None, "ne", "X7R")


def test_lists_are_still_split_on_comma_space():
    assert parse_intervals("250mA, 500mA") == ((0.25, 0.25), (0.5, 0.5))
    assert parse_number("10mHz") == pytest.approx(0.01)        # exact "mHz" stays millihertz
    assert parse_intervals("1/0W") == ()
