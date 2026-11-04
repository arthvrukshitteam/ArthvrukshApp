"""Unit tests for continuity_floor — the shared "which date truncates this
fund's history for Separate Management Periods" logic.

Run: cd backend && python3 -m pytest tests/test_continuity_floor.py -v
"""

from datetime import date

import continuity_floor as cf


# ── amc_change_floor ─────────────────────────────────────────────────────────

def test_amc_change_floor_none_for_missing_doc():
    assert cf.amc_change_floor(None) == (None, None)


def test_amc_change_floor_none_when_no_amc_change_actions():
    doc = {"corporate_actions": [{"type": "WOUND_UP", "effective_date": "2020-01-01"}]}
    assert cf.amc_change_floor(doc) == (None, None)


def test_amc_change_floor_single_action():
    doc = {"corporate_actions": [
        {"type": "AMC_CHANGE", "effective_date": "2019-09-28",
         "from": "Reliance Mutual Fund", "to": "Nippon India Mutual Fund"},
    ]}
    d, reason = cf.amc_change_floor(doc)
    assert d == date(2019, 9, 28)
    assert "Nippon India Mutual Fund" in reason


def test_amc_change_floor_picks_latest_of_multiple():
    """A fund with two AMC changes across its life (e.g. Fidelity -> L&T ->
    HSBC) must use the MOST RECENT one for the current-management score."""
    doc = {"corporate_actions": [
        {"type": "AMC_CHANGE", "effective_date": "2012-06-01", "to": "L&T Mutual Fund"},
        {"type": "AMC_CHANGE", "effective_date": "2022-11-25", "to": "HSBC Mutual Fund"},
    ]}
    d, reason = cf.amc_change_floor(doc)
    assert d == date(2022, 11, 25)
    assert "HSBC Mutual Fund" in reason


def test_amc_change_floor_ignores_live_placeholder_date():
    """"live" is derive_lineage's placeholder for "today", not a real date —
    it must never be read as a continuity break date."""
    doc = {"corporate_actions": [
        {"type": "AMC_CHANGE", "effective_date": "live", "to": "Some Mutual Fund"},
    ]}
    assert cf.amc_change_floor(doc) == (None, None)


def test_amc_change_floor_ignores_malformed_date():
    doc = {"corporate_actions": [
        {"type": "AMC_CHANGE", "effective_date": "not-a-date", "to": "X"},
    ]}
    assert cf.amc_change_floor(doc) == (None, None)


# ── compute_continuity_floor ─────────────────────────────────────────────────

def test_compute_no_signals_returns_none():
    d, reason = cf.compute_continuity_floor(
        predecessor_used=[], own_code_history_start=None,
        lineage_doc=None, manual_event=None,
    )
    assert d is None and reason is None


def test_compute_predecessor_only():
    d, reason = cf.compute_continuity_floor(
        predecessor_used=[107301], own_code_history_start=date(2016, 11, 28),
        lineage_doc=None, manual_event=None,
    )
    assert d == date(2016, 11, 28)
    assert "successor" in reason.lower() or "merger" in reason.lower()


def test_compute_amc_change_only_no_predecessor():
    """The dominant real-world case: same-code rename, no scheme-code
    change at all — predecessor_used is empty, only fund_lineage has a
    signal. This is exactly the gap that made the toggle silently do nothing
    for most funds before this module existed."""
    lineage_doc = {"corporate_actions": [
        {"type": "AMC_CHANGE", "effective_date": "2023-03-13",
         "from": "IDFC Mutual Fund", "to": "Bandhan Mutual Fund"},
    ]}
    d, reason = cf.compute_continuity_floor(
        predecessor_used=[], own_code_history_start=None,
        lineage_doc=lineage_doc, manual_event=None,
    )
    assert d == date(2023, 3, 13)
    assert "Bandhan Mutual Fund" in reason


def test_compute_manual_event_only():
    d, reason = cf.compute_continuity_floor(
        predecessor_used=[], own_code_history_start=None,
        lineage_doc=None, manual_event={"break_date": "2021-01-15", "reason": "Manager change"},
    )
    assert d == date(2021, 1, 15)
    assert reason == "Manager change"


def test_compute_takes_latest_across_all_three_signals():
    """A fund could in principle have all three signals disagree — the
    LATEST one always wins, since that's the current management period."""
    lineage_doc = {"corporate_actions": [
        {"type": "AMC_CHANGE", "effective_date": "2015-01-01", "to": "Middle House"},
    ]}
    d, reason = cf.compute_continuity_floor(
        predecessor_used=[999], own_code_history_start=date(2010, 1, 1),
        lineage_doc=lineage_doc,
        manual_event={"break_date": "2024-06-01", "reason": "Recent manager change"},
    )
    assert d == date(2024, 6, 1)
    assert reason == "Recent manager change"


def test_compute_amc_change_beats_older_predecessor_boundary():
    lineage_doc = {"corporate_actions": [
        {"type": "AMC_CHANGE", "effective_date": "2022-11-25", "to": "HSBC Mutual Fund"},
    ]}
    d, reason = cf.compute_continuity_floor(
        predecessor_used=[123], own_code_history_start=date(2012, 6, 1),
        lineage_doc=lineage_doc, manual_event=None,
    )
    assert d == date(2022, 11, 25)
    assert "HSBC Mutual Fund" in reason


def test_compute_predecessor_without_own_start_contributes_nothing():
    """Defensive: predecessor_used set but no own_code_history_start given
    (shouldn't happen in practice, but must not crash or fabricate a date)."""
    d, reason = cf.compute_continuity_floor(
        predecessor_used=[107301], own_code_history_start=None,
        lineage_doc=None, manual_event=None,
    )
    assert d is None and reason is None
