"""proposal_routes.py — tests for variant_analysis_from_doc, the pure
(no-DB) piece that lets Proposal Builder use the SAME Since-2010 /
Separate Management Periods numbers the Home screen showed for a fund,
instead of silently reverting to its plain since-inception data.

Mirrors test_continuity_toggle.py's fixtures — same fund_nav_meta document
shape, same toggle precedence — since this function is the proposal-side
consumer of select_horizon_map/select_horizons_list.
"""
import os
import sys

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from proposal_routes import variant_analysis_from_doc  # noqa: E402


def _sip_doc(**overrides):
    doc = {
        "scheme_name": "Alpha Equity Fund - Growth",
        "category": "Equity",
        "fqs_by_horizon": {"10": {"fqs": 50.0, "median_wm": 2.0, "p10_wm": 1.6, "success_pct": 0.6, "n_windows": 40, "cv": 0.3}},
        "fqs_by_horizon_2010": {"10": {"fqs": 60.0, "median_wm": 2.2, "p10_wm": 1.8, "success_pct": 0.7, "n_windows": 35, "cv": 0.28}},
        "fqs_by_horizon_continuity": {"10": {"fqs": 70.0, "median_wm": 2.5, "p10_wm": 2.0, "success_pct": 0.8, "n_windows": 20, "cv": 0.25}},
        "continuity_reset_date": "2018-04-12",
        "continuity_reason": "Merger / successor scheme change",
    }
    doc.update(overrides)
    return doc


def _lumpsum_doc(**overrides):
    doc = {
        "scheme_name": "Beta Debt Fund - Growth",
        "category": "Debt",
        "horizons": [{"horizon": 10, "fqs": 50.0, "median_wm": 2.0, "p10_wm": 1.6, "success_pct": 0.6}],
        "horizons_2010": [{"horizon": 10, "fqs": 60.0, "median_wm": 2.2, "p10_wm": 1.8, "success_pct": 0.7}],
        "horizons_continuity": [{"horizon": 10, "fqs": 70.0, "median_wm": 2.5, "p10_wm": 2.0, "success_pct": 0.8}],
        "continuity_reset_date": "2018-04-12",
        "continuity_reason": "Manager change",
    }
    doc.update(overrides)
    return doc


# ── No doc at all — must fall back, never crash ───────────────────────────

def test_variant_analysis_none_when_doc_missing():
    assert variant_analysis_from_doc(None, "sip", True, False) is None


# ── SIP: dict -> horizons-list reshape, with toggle precedence ────────────

def test_variant_analysis_sip_plain_when_no_toggles():
    result = variant_analysis_from_doc(_sip_doc(), "sip", since_2010=False, separate_management=False)
    assert result["scheme_name"] == "Alpha Equity Fund - Growth"
    assert result["category"] == "Equity"
    row = next(h for h in result["horizons"] if h["horizon"] == 10)
    assert row["fqs"] == 50.0


def test_variant_analysis_sip_since_2010():
    result = variant_analysis_from_doc(_sip_doc(), "sip", since_2010=True, separate_management=False)
    row = next(h for h in result["horizons"] if h["horizon"] == 10)
    assert row["fqs"] == 60.0


def test_variant_analysis_sip_continuity_wins():
    result = variant_analysis_from_doc(_sip_doc(), "sip", since_2010=False, separate_management=True)
    row = next(h for h in result["horizons"] if h["horizon"] == 10)
    assert row["fqs"] == 70.0
    # median_wm/p10_wm/success_pct — everything build_fund_row needs — must
    # survive the dict->list reshape, not just fqs.
    assert row["median_wm"] == 2.5
    assert row["p10_wm"] == 2.0
    assert row["success_pct"] == 0.8


def test_variant_analysis_sip_horizon_key_present_for_every_entry():
    # extract_fund_metrics() matches rows by h.get("horizon") — the reshape
    # from a {horizon_str: {...}} dict must add that key onto every entry.
    doc = _sip_doc(fqs_by_horizon={
        "5": {"fqs": 40.0}, "10": {"fqs": 50.0},
    })
    result = variant_analysis_from_doc(doc, "sip", since_2010=False, separate_management=False)
    horizons = {h["horizon"]: h["fqs"] for h in result["horizons"]}
    assert horizons == {5: 40.0, 10: 50.0}


def test_variant_analysis_sip_none_when_no_data_at_all():
    doc = {"scheme_name": "Empty Fund", "category": "Equity"}
    assert variant_analysis_from_doc(doc, "sip", since_2010=False, separate_management=False) is None


# ── Lumpsum: list shape passes through, same toggle precedence ────────────

def test_variant_analysis_lumpsum_plain_when_no_toggles():
    result = variant_analysis_from_doc(_lumpsum_doc(), "lumpsum", since_2010=False, separate_management=False)
    row = next(h for h in result["horizons"] if h["horizon"] == 10)
    assert row["fqs"] == 50.0


def test_variant_analysis_lumpsum_since_2010():
    result = variant_analysis_from_doc(_lumpsum_doc(), "lumpsum", since_2010=True, separate_management=False)
    row = next(h for h in result["horizons"] if h["horizon"] == 10)
    assert row["fqs"] == 60.0


def test_variant_analysis_lumpsum_continuity_wins():
    result = variant_analysis_from_doc(_lumpsum_doc(), "lumpsum", since_2010=False, separate_management=True)
    row = next(h for h in result["horizons"] if h["horizon"] == 10)
    assert row["fqs"] == 70.0


def test_variant_analysis_lumpsum_both_toggles_break_before_2010_falls_back():
    doc = _lumpsum_doc(continuity_reset_date="2003-01-01")
    result = variant_analysis_from_doc(doc, "lumpsum", since_2010=True, separate_management=True)
    row = next(h for h in result["horizons"] if h["horizon"] == 10)
    assert row["fqs"] == 60.0  # falls back to Since-2010, same rule as Home


def test_variant_analysis_lumpsum_none_when_no_data_at_all():
    doc = {"scheme_name": "Empty Fund", "category": "Debt"}
    assert variant_analysis_from_doc(doc, "lumpsum", since_2010=False, separate_management=False) is None


# ── Fund with NO variant data at all for the requested toggle, but has the
#    plain data — must still return the plain variant, not None (the same
#    "not having a variant is never a reason to hide the fund" rule Home
#    screen's own _select_horizon_map docstring states). ────────────────────

def test_variant_analysis_sip_falls_back_to_plain_when_no_2010_variant():
    doc = {
        "scheme_name": "Young Fund",
        "category": "Equity",
        "fqs_by_horizon": {"10": {"fqs": 45.0}},
        # no fqs_by_horizon_2010 — fund started after 2010
    }
    result = variant_analysis_from_doc(doc, "sip", since_2010=True, separate_management=False)
    row = next(h for h in result["horizons"] if h["horizon"] == 10)
    assert row["fqs"] == 45.0
