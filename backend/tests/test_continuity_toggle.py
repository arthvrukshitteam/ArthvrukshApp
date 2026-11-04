"""Management / Fund Continuity Toggle ("Separate Management Periods") —
selection-logic tests for _select_horizon_map / _select_horizons_list /
_continuity_applies in server.py.

These are pure functions over already-computed fund_nav_meta /
fund_lumpsum_analysis documents — no DB or network needed, mirroring the
existing Since-2010 toggle's test coverage.
"""
import os

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")

from server import _select_horizon_map, _select_horizons_list, _continuity_applies


def _sip_doc(**overrides):
    doc = {
        "fqs_by_horizon": {"5": {"fqs": 50.0, "tier": "Average"}},
        "fqs_by_horizon_2010": {"5": {"fqs": 60.0, "tier": "Strong"}},
        "fqs_by_horizon_continuity": {"5": {"fqs": 70.0, "tier": "Strong"}},
        "continuity_reset_date": "2018-04-12",
        "continuity_reason": "Merger / successor scheme change",
    }
    doc.update(overrides)
    return doc


# ── _continuity_applies ───────────────────────────────────────────────────

def test_continuity_applies_no_data():
    assert _continuity_applies({}, since_2010=False) is False


def test_continuity_applies_no_since_2010():
    doc = _sip_doc()
    assert _continuity_applies(doc, since_2010=False) is True


def test_continuity_applies_since_2010_break_after_2010():
    doc = _sip_doc(continuity_reset_date="2018-04-12")
    assert _continuity_applies(doc, since_2010=True) is True


def test_continuity_applies_since_2010_break_before_2010():
    # The deliberate simplification: a verified break older than 2010 falls
    # back to the plain Since-2010 numbers when BOTH toggles are on, so the
    # explicit Since-2010 request is never silently dropped.
    doc = _sip_doc(continuity_reset_date="2005-06-01")
    assert _continuity_applies(doc, since_2010=True) is False
    # ... but with Since-2010 off, the continuity break still applies as-is.
    assert _continuity_applies(doc, since_2010=False) is True


# ── _select_horizon_map (SIP) precedence ──────────────────────────────────

def test_select_horizon_map_precedence_normal():
    doc = _sip_doc()
    result = _select_horizon_map(doc, since_2010=False, separate_management=False)
    assert result["5"]["fqs"] == 50.0


def test_select_horizon_map_precedence_since_2010():
    doc = _sip_doc()
    result = _select_horizon_map(doc, since_2010=True, separate_management=False)
    assert result["5"]["fqs"] == 60.0


def test_select_horizon_map_precedence_continuity_wins():
    doc = _sip_doc()
    result = _select_horizon_map(doc, since_2010=False, separate_management=True)
    assert result["5"]["fqs"] == 70.0


def test_select_horizon_map_both_toggles_break_after_2010():
    doc = _sip_doc(continuity_reset_date="2018-04-12")
    result = _select_horizon_map(doc, since_2010=True, separate_management=True)
    assert result["5"]["fqs"] == 70.0  # continuity wins — break already >= 2010


def test_select_horizon_map_both_toggles_break_before_2010():
    doc = _sip_doc(continuity_reset_date="2005-06-01")
    result = _select_horizon_map(doc, since_2010=True, separate_management=True)
    assert result["5"]["fqs"] == 60.0  # falls back to Since-2010 numbers


def test_select_horizon_map_no_continuity_data_falls_back():
    # Fund has no verified break at all — separate_management=True is a no-op.
    doc = {
        "fqs_by_horizon": {"5": {"fqs": 50.0}},
        "fqs_by_horizon_2010": {"5": {"fqs": 60.0}},
    }
    result = _select_horizon_map(doc, since_2010=False, separate_management=True)
    assert result["5"]["fqs"] == 50.0
    result_2010 = _select_horizon_map(doc, since_2010=True, separate_management=True)
    assert result_2010["5"]["fqs"] == 60.0


def test_select_horizon_map_young_fund_no_2010_but_has_continuity():
    # Fund started after 2010 (no fqs_by_horizon_2010) but has a continuity
    # break — continuity should still apply even without a 2010 variant.
    doc = _sip_doc(fqs_by_horizon_2010=None)
    result = _select_horizon_map(doc, since_2010=True, separate_management=True)
    assert result["5"]["fqs"] == 70.0


# ── _select_horizons_list (Lumpsum) — same precedence, list shape ─────────

def _lumpsum_doc(**overrides):
    doc = {
        "horizons": [{"horizon": 5, "fqs": 50.0}],
        "horizons_2010": [{"horizon": 5, "fqs": 60.0}],
        "horizons_continuity": [{"horizon": 5, "fqs": 70.0}],
        "continuity_reset_date": "2018-04-12",
        "continuity_reason": "Manager change",
    }
    doc.update(overrides)
    return doc


def test_select_horizons_list_precedence():
    doc = _lumpsum_doc()
    normal = _select_horizons_list(doc, since_2010=False, separate_management=False)
    since_2010 = _select_horizons_list(doc, since_2010=True, separate_management=False)
    continuity = _select_horizons_list(doc, since_2010=False, separate_management=True)
    assert normal[0]["fqs"] == 50.0
    assert since_2010[0]["fqs"] == 60.0
    assert continuity[0]["fqs"] == 70.0


def test_select_horizons_list_both_toggles_break_before_2010():
    doc = _lumpsum_doc(continuity_reset_date="2003-01-01")
    result = _select_horizons_list(doc, since_2010=True, separate_management=True)
    assert result[0]["fqs"] == 60.0  # falls back to Since-2010
