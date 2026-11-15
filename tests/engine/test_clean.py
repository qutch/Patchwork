"""G2: cleaners (contracts V18). A value that does not fit is VALIDATION_FAILED, never a guess."""

from __future__ import annotations

import pytest

from runner.clean import CLEANERS, clean_cast, clean_value, parse_amount
from runner.lib import Quarantine


@pytest.mark.parametrize("raw,spec,want", [("$1,299.00", "money", 1299.0), ("(12.50)", "money", -12.5),
    ("1.299,00 €", {"money": {"decimal": ","}}, 1299.0), ("03 Nov 2026", {"date": ["%d %b %Y"]}, "2026-11-03"),
    ("Austin, TX", {"split": {"sep": ",", "part": 1}}, "TX")])
def test_cleaners(raw, spec, want):
    assert clean_value(raw, spec, "f") == want


@pytest.mark.parametrize("raw,want", [
    ("USD 1,299.00", 1299.0), ("1,299.00 EUR", 1299.0), ("-$5.00", -5.0), ("$-5.00", -5.0), ("5.00-", -5.0),
    ("($1,000)", -1000.0), ("£0.25", 0.25), ("42", 42.0), ("1,234,567.891", 1234567.891), (12, 12.0), (3.5, 3.5),
])
def test_money_forms(raw, want):
    assert clean_value(raw, "money", "f") == want


@pytest.mark.parametrize("raw", ["1,29", "12,34.00", "1.299,00", "abc", "", "1.2.3", "--5", "(-5)", "$", "1 299"])
def test_money_refuses_what_it_cannot_read(raw):
    with pytest.raises(Quarantine) as exc:
        clean_value(raw, "money", "f")
    assert exc.value.code == "VALIDATION_FAILED" and exc.value.detail.startswith("f: ")


def test_money_with_comma_decimal():
    assert clean_value("1.299,50", {"money": {"decimal": ","}}, "f") == 1299.5
    assert clean_value("12,5", {"money": {"decimal": ","}}, "f") == 12.5
    with pytest.raises(Quarantine):
        clean_value("1,299.50", {"money": {"decimal": ","}}, "f")


def test_ambiguous_date_and_money_are_held_not_guessed():
    with pytest.raises(Quarantine, match="ambiguous: 2026-03-04 or 2026-04-03"):
        clean_value("03/04/2026", {"date": ["%m/%d/%Y", "%d/%m/%Y"]}, "f")
    with pytest.raises(Quarantine, match="VALIDATION_FAILED|not a money"):
        clean_value("1.299,00", "money", "f")


def test_wrong_decimal_names_both_readings():
    with pytest.raises(Quarantine) as exc:
        clean_value("1.299,00", "money", "f")
    assert exc.value.code == "VALIDATION_FAILED"
    assert "decimal '.'" in exc.value.detail and "decimal ','" in exc.value.detail and "1,299.00" in exc.value.detail


def test_dates():
    assert clean_value("13/04/2026", {"date": ["%m/%d/%Y", "%d/%m/%Y"]}, "f") == "2026-04-13"  # only one fits
    assert clean_value("04/04/2026", {"date": ["%m/%d/%Y", "%d/%m/%Y"]}, "f") == "2026-04-04"  # both agree
    assert clean_value(" 2026-11-03 ", {"date": ["%Y-%m-%d"]}, "f") == "2026-11-03"
    with pytest.raises(Quarantine, match="does not match"):
        clean_value("Nov 3", {"date": ["%Y-%m-%d"]}, "f")


def test_number_split_strip_upper_lower():
    assert clean_value("1,200", "number", "f") == 1200 and isinstance(clean_value("1,200", "number", "f"), int)
    assert clean_value("-2.5", "number", "f") == -2.5
    assert clean_value("1.200,5", {"number": {"decimal": ","}}, "f") == 1200.5
    with pytest.raises(Quarantine):
        clean_value("$5", "number", "f")
    assert clean_value("  a b ", "strip", "f") == "a b"
    assert clean_value("ab", "upper", "f") == "AB" and clean_value("AB", "lower", "f") == "ab"
    with pytest.raises(Quarantine, match="no part 3"):
        clean_value("a,b", {"split": {"sep": ",", "part": 3}}, "f")


def test_unknown_cleaner_is_an_error():
    with pytest.raises(Quarantine):
        clean_value("x", "guess", "f")
    assert "money" in CLEANERS


def test_clean_then_cast():
    assert clean_cast("$3.00", "money", "str", "f") == 3.0
    assert clean_cast("$3.00", "money", "int", "f") == 3
    assert clean_cast("2", None, "int", "f") == 2
    assert clean_cast("2", "number", "float", "f") == 2.0
    with pytest.raises(Quarantine, match="not a whole number"):
        clean_cast("$3.50", "money", "int", "f")


@pytest.mark.parametrize("raw,spec,readings", [
    ("€1.299", "money", ("1.299", "1,299.00")),
    ("1.299", {"money": {"decimal": "."}}, ("1.299", "1,299.00")),
    ("1,299", {"money": {"decimal": ","}}, ("1.299", "1,299.00")),
    ("-12,500 EUR", {"money": {"decimal": ","}}, ("-12.5", "-12,500.00")),
])
def test_money_with_three_fraction_digits_and_no_grouping_is_ambiguous(raw, spec, readings):
    """Important 1: one mark followed by exactly 3 digits reads two ways; it is held, never guessed."""
    with pytest.raises(Quarantine) as exc:
        clean_value(raw, spec, "f")
    assert exc.value.code == "VALIDATION_FAILED" and "ambiguous" in exc.value.detail
    for reading in readings:
        assert reading in exc.value.detail


@pytest.mark.parametrize("raw,spec,want", [
    ("1,299", "money", 1299.0),                              # grouped: unambiguous with decimal '.'
    ("1.299", {"money": {"decimal": ","}}, 1299.0),          # grouped with decimal ','
    ("1,299.000", "money", 1299.0),                          # grouped and a fraction
    ("0.125", "money", 0.125), ("0,125", {"money": {"decimal": ","}}, 0.125),  # a leading 0 never groups
    ("1234.567", "money", 1234.567),                         # 4 digits cannot be a thousands group
    ("1.29", "money", 1.29), ("1.2990", "money", 1.299),
])
def test_money_readings_that_stay_unambiguous(raw, spec, want):
    assert clean_value(raw, spec, "f") == want


def test_parse_amount_is_lenient_and_never_raises():
    assert parse_amount("$4.00") == 4 and parse_amount("(1,000.50)") == -1000.5
    assert parse_amount("Sum") is None and parse_amount("") is None and parse_amount("2026-11-03") is None
