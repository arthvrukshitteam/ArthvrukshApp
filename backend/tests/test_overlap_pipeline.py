"""End-to-end: a real .xlsx disclosure → parse_workbook → compute_overlap.

The unit suites cover each half against literal rows. This one builds actual
workbooks with openpyxl and pushes them through the whole path, because the
failure this guards against is the two halves drifting apart at their seam —
the parser emitting a key the engine doesn't read, or vice versa. That mistake
passes both unit suites and produces a 0% overlap in production.
"""
import io
import os
import sys

import pytest
from openpyxl import Workbook

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from holdings_parser import HoldingsParseError, parse_workbook  # noqa: E402
from overlap_engine import compute_overlap                      # noqa: E402

HEADER = ["Name of the Instrument", "ISIN", "Industry / Rating", "Quantity",
          "Market value (Rs. in Lakhs)", "% to Net Assets"]


def disclosure(sheet_name, scheme_title, equity, extras=True, title_depth=4):
    """A workbook shaped like a real AMC monthly disclosure: a deep title
    block, the SEBI header, an equity section with a sub-heading, sub-totals,
    then debt/cash sections that must not leak into the equity book."""
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name

    for i in range(title_depth):
        ws.append([scheme_title if i == 1 else ""])
    ws.append(["Monthly Portfolio Statement as on 31-Aug-2026"])
    ws.append(HEADER)
    ws.append(["Equity & Equity Related"])
    ws.append(["(a) Listed / awaiting listing on the stock exchanges"])
    for name, isin, wt in equity:
        ws.append([name, isin, "Misc", 1000, wt * 100, wt])
    ws.append(["Sub Total", "", "", "", "", sum(w for _, _, w in equity)])
    if extras:
        ws.append(["Debt Instruments"])
        ws.append(["7.26% GOI 2033", "IN0020230085", "Sovereign", 500, 480, 4.10])
        ws.append(["Money Market Instruments"])
        ws.append(["91 Day T-Bill", "IN002023X011", "Sovereign", 100, 95, 0.90])
        ws.append(["TREPS / Reverse Repo"])
        ws.append(["TREPS", "", "", "", 200, 1.80])
        ws.append(["Net Receivables / (Payables)", "", "", "", "", "(0.42)"])
    ws.append(["GRAND TOTAL", "", "", "", "", 100.00])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def holdings_of(content):
    sheets = parse_workbook(content)
    assert len(sheets) == 1
    return sheets[0]["holdings"]


# ── The seam ─────────────────────────────────────────────────────────────────

def test_parsed_workbook_feeds_straight_into_the_engine():
    a = disclosure("Flexi Cap", "Alpha Flexi Cap Fund", [
        ("HDFC Bank Limited",        "INE040A01034", 9.0),
        ("Reliance Industries Ltd",  "INE002A01018", 7.0),
        ("Infosys Limited",          "INE009A01021", 5.0),
        ("Some Unique Co Ltd",       "INE111A01011", 4.0),
    ])
    b = disclosure("Large Cap", "Beta Large Cap Fund", [
        ("HDFC Bank Ltd.",           "INE040A01034", 8.0),
        ("Reliance Industries",      "INE002A01018", 6.0),
        ("Tata Motors Ltd",          "INE155A01022", 5.0),
    ])

    ha, hb = holdings_of(a), holdings_of(b)

    # Debt / T-bill / TREPS / receivables rows must not have survived.
    assert len(ha) == 4 and len(hb) == 3
    assert all(h["isin"] != "IN0020230085" for h in ha)

    r = compute_overlap(ha, hb)
    assert r["fund_a_holdings"] == 4
    assert r["fund_b_holdings"] == 3
    assert r["common_count"] == 2                       # HDFC Bank + Reliance
    assert all(c["matched_on"] == "isin" for c in r["common"])

    # A's equity sleeve is 25%, B's is 19%. Rebased: HDFC 36.0 vs 42.1,
    # Reliance 28.0 vs 31.6 → overlap is min-sum of the rebased pair.
    assert 60.0 < r["overlap_pct"] < 70.0
    # Sorted by contribution: HDFC (9.0/8.0) outranks Reliance (7.0/6.0).
    assert [c["name"] for c in r["common"]] == [
        "HDFC Bank Limited",        # longer of "HDFC Bank Limited" / "HDFC Bank Ltd."
        "Reliance Industries Ltd",  # longer of "...Ltd" / "Reliance Industries"
    ]
    assert r["common"][0]["contribution"] >= r["common"][1]["contribution"]


def test_identical_disclosures_round_trip_to_100_pct():
    equity = [("HDFC Bank Limited", "INE040A01034", 9.0),
              ("Infosys Limited",   "INE009A01021", 6.0)]
    a = disclosure("S1", "Alpha Fund", equity)
    b = disclosure("S2", "Beta Fund", equity)
    r = compute_overlap(holdings_of(a), holdings_of(b))
    assert r["overlap_pct"] == 100.0
    assert r["common_count"] == 2


def test_engine_reads_every_key_the_parser_writes():
    """Guards the seam explicitly: the engine must tolerate the parser's full
    row shape, including the quantity/value keys it does not use."""
    h = holdings_of(disclosure("S", "Alpha Fund",
                               [("HDFC Bank Limited", "INE040A01034", 9.0)]))
    assert set(h[0]) == {"name", "isin", "weight_pct", "quantity", "value"}
    assert compute_overlap(h, h)["overlap_pct"] == 100.0


# ── Multi-scheme workbooks ───────────────────────────────────────────────────

def test_multi_sheet_workbook_yields_one_entry_per_scheme():
    wb = Workbook()
    wb.remove(wb.active)
    for i, scheme in enumerate(["Alpha Flexi Cap Fund", "Beta Large Cap Fund"]):
        ws = wb.create_sheet(f"Scheme{i}")
        ws.append([scheme])
        ws.append(HEADER)
        ws.append(["Equity & Equity Related"])
        ws.append([f"Stock {i}", f"INE{i:03d}A01036", "IT", 10, 10, 5.0])
    buf = io.BytesIO(); wb.save(buf)

    sheets = parse_workbook(buf.getvalue())
    assert len(sheets) == 2
    assert "Alpha Flexi Cap Fund" in sheets[0]["scheme_hint"]


def test_notes_and_disclaimer_sheets_are_skipped_not_fatal():
    wb = Workbook()
    wb.remove(wb.active)
    notes = wb.create_sheet("Disclaimer")
    notes.append(["This document is for information purposes only."])
    real = wb.create_sheet("Portfolio")
    real.append(["Alpha Flexi Cap Fund"])
    real.append(HEADER)
    real.append(["Equity & Equity Related"])
    real.append(["Stock A", "INE001A01036", "IT", 10, 10, 5.0])
    buf = io.BytesIO(); wb.save(buf)

    sheets = parse_workbook(buf.getvalue())
    assert [s["sheet"] for s in sheets] == ["Portfolio"]


def test_workbook_with_no_readable_portfolio_raises():
    wb = Workbook()
    wb.active.append(["Just some notes"])
    buf = io.BytesIO(); wb.save(buf)
    with pytest.raises(HoldingsParseError, match="No sheet"):
        parse_workbook(buf.getvalue())


# ── Cash-drag realism ────────────────────────────────────────────────────────

def test_cash_heavy_fund_compares_fairly_after_rebasing():
    """Same three stocks in both books, but B runs 30% cash. Rebased to the
    equity sleeve this is a 100% overlap; on raw disclosed weights it would
    read ~70% and wrongly suggest the books differ."""
    a = disclosure("A", "Alpha Fund", [
        ("HDFC Bank Limited", "INE040A01034", 40.0),
        ("Infosys Limited",   "INE009A01021", 30.0),
        ("Tata Motors Ltd",   "INE155A01022", 30.0),
    ], extras=False)
    b = disclosure("B", "Beta Fund", [
        ("HDFC Bank Limited", "INE040A01034", 28.0),
        ("Infosys Limited",   "INE009A01021", 21.0),
        ("Tata Motors Ltd",   "INE155A01022", 21.0),
    ], extras=False)
    r = compute_overlap(holdings_of(a), holdings_of(b))
    assert r["overlap_pct"] == 100.0
    assert r["overlap_pct_raw"] == 70.0
    assert r["equity_sleeve_b"] == 70.0
