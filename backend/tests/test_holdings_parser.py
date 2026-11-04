"""holdings_parser.py — tests for reading SEBI monthly portfolio disclosures.

Rows are written as literal lists rather than fixture .xlsx files: the things
that actually break this parser are layout variations between AMCs (a deep
title block, a missing section heading, sub-totals mid-table), and those are
far clearer — and far easier to extend when a new AMC breaks something — as
inline rows than as binary fixtures.
"""
import os
import sys

import pytest

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from holdings_parser import (  # noqa: E402
    HoldingsParseError,
    _to_float,
    parse_rows,
)

HEADER = ["Name of the Instrument", "ISIN", "Industry", "Quantity",
          "Market value (Rs. in Lakhs)", "% to Net Assets"]


def row(name, isin="", industry="", qty="", value="", weight=""):
    return [name, isin, industry, qty, value, weight]


# ── Number parsing ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (8.35, 8.35), ("8.35", 8.35), ("8.35%", 8.35), ("1,234.5", 1234.5),
    ("(0.42)", -0.42), ("-", None), ("–", None), ("NA", None), ("", None),
    (None, None), ("garbage", None),
])
def test_to_float_handles_disclosure_number_formats(raw, expected):
    assert _to_float(raw) == expected


# ── Section handling ─────────────────────────────────────────────────────────

def test_equity_section_is_kept_and_debt_section_is_dropped():
    rows = [
        row("Equity & Equity Related"),
        row("(a) Listed / awaiting listing on the stock exchanges"),
        row("HDFC Bank Limited", "INE040A01034", "Banks", 1000, 500, 8.35),
        row("Reliance Industries Ltd", "INE002A01018", "Petroleum", 500, 400, 6.70),
        row("Sub Total"),
        row("Debt Instruments"),
        row("7.26% GOI 2033", "IN0020230085", "Sovereign", 100, 90, 1.50),
        row("TREPS"),
        row("Net Receivables / (Payables)", "", "", "", "", 0.45),
        row("GRAND TOTAL"),
    ]
    out = parse_rows(HEADER, rows)
    assert [h["name"] for h in out] == ["HDFC Bank Limited", "Reliance Industries Ltd"]
    assert out[0]["weight_pct"] == 8.35
    assert out[0]["isin"] == "INE040A01034"


def test_equity_subheadings_do_not_close_the_equity_section():
    """'(a) Listed...' and '(b) Unlisted' sit inside the equity block. Treating
    them as a new section was what previously truncated every parse to zero
    rows."""
    rows = [
        row("Equity & Equity Related"),
        row("(a) Listed / awaiting listing on the stock exchanges"),
        row("Alpha Ltd", "INE001A01036", "IT", 10, 10, 5.0),
        row("(b) Unlisted"),
        row("Beta Ltd", "INE002A01018", "IT", 10, 10, 3.0),
    ]
    out = parse_rows(HEADER, rows)
    assert len(out) == 2


def test_file_with_no_equity_heading_is_still_read():
    """Some AMCs open straight into the equity table with no section row."""
    rows = [
        row("Alpha Ltd", "INE001A01036", "IT", 10, 10, 5.0),
        row("Beta Ltd", "INE002A01018", "IT", 10, 10, 3.0),
    ]
    out = parse_rows(HEADER, rows)
    assert len(out) == 2


def test_rows_after_a_debt_heading_are_excluded_even_without_a_prior_equity_heading():
    rows = [
        row("Alpha Ltd", "INE001A01036", "IT", 10, 10, 5.0),
        row("Money Market Instruments"),
        row("Some CP", "INE999A01011", "CP", 10, 10, 2.0),
    ]
    out = parse_rows(HEADER, rows)
    assert [h["name"] for h in out] == ["Alpha Ltd"]


def test_reit_and_derivative_sections_are_excluded():
    rows = [
        row("Equity & Equity Related"),
        row("Alpha Ltd", "INE001A01036", "IT", 10, 10, 5.0),
        row("REITs & InvITs"),
        row("Embassy REIT", "INE041A01034", "REIT", 10, 10, 1.0),
        row("Derivatives"),
        row("NIFTY FUT", "", "", 10, 10, 2.0),
    ]
    out = parse_rows(HEADER, rows)
    assert [h["name"] for h in out] == ["Alpha Ltd"]


def test_totals_and_blank_spacers_are_skipped():
    rows = [
        row("Equity & Equity Related"),
        row("Alpha Ltd", "INE001A01036", "IT", 10, 10, 5.0),
        row("Sub Total", "", "", "", "", 5.0),
        row(""),
        row("Total", "", "", "", "", 5.0),
    ]
    out = parse_rows(HEADER, rows)
    assert len(out) == 1


# ── Column identification ────────────────────────────────────────────────────

@pytest.mark.parametrize("weight_header", [
    "% to Net Assets", "% of Net Assets", "% to NAV", "% to AUM",
    "  % TO NET ASSETS  ",
])
def test_weight_column_is_found_under_its_many_spellings(weight_header):
    header = ["Name of the Instrument", "ISIN", "Quantity", weight_header]
    rows = [["Alpha Ltd", "INE001A01036", 10, 5.0]]
    assert parse_rows(header, rows)[0]["weight_pct"] == 5.0


@pytest.mark.parametrize("name_header", [
    "Name of the Instrument", "Name of Instrument", "Security Name", "Particulars",
])
def test_name_column_is_found_under_its_many_spellings(name_header):
    header = [name_header, "ISIN", "% to Net Assets"]
    rows = [["Alpha Ltd", "INE001A01036", 5.0]]
    assert parse_rows(header, rows)[0]["name"] == "Alpha Ltd"


def test_header_without_a_weight_column_is_rejected_not_guessed():
    header = ["Name of the Instrument", "ISIN", "Quantity"]
    with pytest.raises(HoldingsParseError, match="recognisable"):
        parse_rows(header, [["Alpha Ltd", "INE001A01036", 10]])


def test_empty_equity_book_raises_rather_than_returning_nothing_quietly():
    rows = [row("Debt Instruments"), row("GOI", "IN0020230085", "Sov", 1, 1, 1.0)]
    with pytest.raises(HoldingsParseError, match="No equity holdings"):
        parse_rows(HEADER, rows)


# ── Row-level robustness ─────────────────────────────────────────────────────

def test_row_with_an_isin_is_a_security_even_if_its_name_looks_like_a_heading():
    """A company whose name trips a section pattern must not be able to close
    the equity section — the ISIN settles it."""
    rows = [
        row("Equity & Equity Related"),
        row("Cholamandalam Financial Holdings", "INE149A01033", "Finance", 10, 10, 2.0),
        row("Alpha Ltd", "INE001A01036", "IT", 10, 10, 5.0),
    ]
    out = parse_rows(HEADER, rows)
    assert len(out) == 2


def test_holding_without_a_weight_is_dropped():
    rows = [
        row("Equity & Equity Related"),
        row("Alpha Ltd", "INE001A01036", "IT", 10, 10, 5.0),
        row("Beta Ltd", "INE002A01018", "IT", 10, 10, "-"),
    ]
    out = parse_rows(HEADER, rows)
    assert [h["name"] for h in out] == ["Alpha Ltd"]


def test_short_rows_do_not_raise_index_errors():
    rows = [row("Equity & Equity Related"), ["Alpha Ltd", "INE001A01036"]]
    with pytest.raises(HoldingsParseError):
        parse_rows(HEADER, rows)


def test_quantity_and_value_are_carried_through_when_present():
    rows = [
        row("Equity & Equity Related"),
        row("Alpha Ltd", "INE001A01036", "IT", "1,000", "1,234.5", "5.0%"),
    ]
    h = parse_rows(HEADER, rows)[0]
    assert h["quantity"] == 1000.0 and h["value"] == 1234.5 and h["weight_pct"] == 5.0
