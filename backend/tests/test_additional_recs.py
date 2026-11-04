"""Additional Recommendations selection rule tests.

Rule (user spec):
  * Candidate funds have available history from ADDITIONAL_REC_MIN_HISTORY_YEARS
    (5) up to just below Core (history < tenure + buffer). Funds at/above the
    Core cutoff go to Core; funds under 5 years are excluded.
  * Each candidate is judged at the LONGEST window (5Y..Core cutoff) that has at
    least ADDITIONAL_REC_MIN_WINDOWS (10) rolling windows — its max available
    robust history, which may sit above the tenure. The second arg to
    _select_additional_rec is that ceiling (max_horizon = the Core cutoff).
  * The fund qualifies when that window's XIRR is >= 15%.
"""
import os

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")

from server import (
    ADDITIONAL_REC_MIN_HISTORY_YEARS,
    ADDITIONAL_REC_MIN_WINDOWS,
    ADDITIONAL_REC_MIN_XIRR,
    _select_additional_rec,
)
from xirr_utils import (
    sip_wm_to_xirr,
    sip_xirr_to_wm,
    lumpsum_wm_to_xirr,
    lumpsum_xirr_to_wm,
)


def _stats(wm, n=10):
    return {"blended_wm": wm, "n_windows": n}


def test_thresholds_match_spec():
    assert ADDITIONAL_REC_MIN_XIRR == 0.15
    assert ADDITIONAL_REC_MIN_HISTORY_YEARS == 5
    assert ADDITIONAL_REC_MIN_WINDOWS == 10


def test_thin_max_window_steps_down_to_a_10_window_horizon():
    # 9Y window has only 3 rolling windows (<10) -> not robust; 7Y window has
    # 20 (>=10). Must evaluate at 7Y, the LONGEST window with >=10 samples.
    wm_9 = sip_xirr_to_wm(0.30, 9)   # high XIRR but too few windows -> skipped
    wm_7 = sip_xirr_to_wm(0.16, 7)
    pick = _select_additional_rec(
        [("7", _stats(wm_7, 20)), ("9", _stats(wm_9, 3))], 10, sip_wm_to_xirr,
    )
    assert pick is not None
    xirr, h, _ = pick
    assert h == 7
    assert round(xirr, 3) == 0.16


def test_excluded_when_no_horizon_has_10_windows():
    # Every eligible horizon is too thin -> fund dropped entirely.
    wm_6 = sip_xirr_to_wm(0.25, 6)
    wm_8 = sip_xirr_to_wm(0.25, 8)
    assert _select_additional_rec(
        [("6", _stats(wm_6, 4)), ("8", _stats(wm_8, 2))], 10, sip_wm_to_xirr,
    ) is None


def test_sub_5y_window_not_used_even_with_many_windows():
    # Only the 4Y horizon clears 10 windows, but the 5Y floor forbids it.
    wm_4 = sip_xirr_to_wm(0.30, 4)
    assert _select_additional_rec([("4", _stats(wm_4, 30))], 10, sip_wm_to_xirr) is None


def test_uses_max_available_history():
    # Fund with 9Y history (T = 10). XIRR must be judged at 9Y, not 10Y.
    wm_9 = sip_xirr_to_wm(0.168, 9)
    pick = _select_additional_rec([("9", _stats(wm_9))], 10, sip_wm_to_xirr)
    assert pick is not None
    xirr, h, _ = pick
    assert h == 9
    assert round(xirr, 3) == 0.168


def test_picks_longest_horizon_ignoring_shorter_ones():
    # Fund exposes 5Y, 7Y and 9Y periods. Only the 9Y (max) period is used,
    # even though the 5Y period has a much higher XIRR.
    wm_5 = sip_xirr_to_wm(0.30, 5)   # would qualify, but must be ignored
    wm_7 = sip_xirr_to_wm(0.20, 7)
    wm_9 = sip_xirr_to_wm(0.16, 9)   # this is the one that decides
    pick = _select_additional_rec(
        [("5", _stats(wm_5)), ("7", _stats(wm_7)), ("9", _stats(wm_9))],
        10, sip_wm_to_xirr,
    )
    xirr, h, _ = pick
    assert h == 9
    assert round(xirr, 3) == 0.16


def test_all_metrics_come_from_the_same_max_history_window():
    # Every card metric must come from the SAME window used for XIRR (best_h),
    # never mixed across horizons. The returned stats object must be the exact
    # dict for the max horizon (9Y here), so all downstream fields are consistent.
    stats_5 = {"blended_wm": sip_xirr_to_wm(0.30, 5), "fqs": 5.0, "p10_wm": 1.1,
               "cv": 0.5, "success_pct": 0.6, "n_windows": 80, "tier": "T5"}
    stats_9 = {"blended_wm": sip_xirr_to_wm(0.16, 9), "fqs": 9.0, "p10_wm": 2.2,
               "cv": 0.2, "success_pct": 0.9, "n_windows": 12, "tier": "T9"}
    xirr, h, stats = _select_additional_rec(
        [("5", stats_5), ("9", stats_9)], 10, sip_wm_to_xirr,
    )
    assert h == 9
    assert stats is stats_9          # identity: the 9Y window, not the 5Y one
    assert stats["fqs"] == 9.0 and stats["p10_wm"] == 2.2 and stats["cv"] == 0.2
    assert round(xirr, 3) == 0.16    # XIRR from that same 9Y window's WM


def test_max_history_below_threshold_excluded_even_if_shorter_qualifies():
    # 9Y (max) XIRR = 12% -> excluded, despite 5Y period at 25%.
    wm_5 = sip_xirr_to_wm(0.25, 5)
    wm_9 = sip_xirr_to_wm(0.12, 9)
    assert _select_additional_rec(
        [("5", _stats(wm_5)), ("9", _stats(wm_9))], 10, sip_wm_to_xirr,
    ) is None


def test_boundary_inclusive_at_15pct():
    # Boundary is inclusive (>= 15%): just above qualifies, just below does not.
    wm_ok = sip_xirr_to_wm(0.1502, 8)
    wm_no = sip_xirr_to_wm(0.1498, 8)
    pick = _select_additional_rec([("8", _stats(wm_ok))], 10, sip_wm_to_xirr)
    assert pick is not None
    assert pick[1] == 8
    assert _select_additional_rec([("8", _stats(wm_no))], 10, sip_wm_to_xirr) is None


def test_horizons_above_ceiling_ignored():
    # A horizon beyond the Core cutoff (max_horizon) is never used; the largest
    # horizon <= ceiling decides. Ceiling 11 here (tenure 9 + buffer 2).
    wm_10 = sip_xirr_to_wm(0.16, 10)
    wm_12 = sip_xirr_to_wm(0.30, 12)   # above ceiling -> ignored
    pick = _select_additional_rec(
        [("10", _stats(wm_10)), ("12", _stats(wm_12))], 11, sip_wm_to_xirr,
    )
    xirr, h, _ = pick
    assert h == 10
    assert round(xirr, 3) == 0.16


def test_evaluates_above_tenure_up_to_core_cutoff():
    # Wider band: a fund with more history than the tenure (but below Core) is
    # judged at its max robust window, above the tenure. Tenure 9 -> ceiling 11,
    # a fund with a robust 10Y window is evaluated at 10Y (not capped at 9Y).
    wm_10 = sip_xirr_to_wm(0.17, 10)
    pick = _select_additional_rec([("10", _stats(wm_10, 20))], 11, sip_wm_to_xirr)
    assert pick is not None
    xirr, h, _ = pick
    assert h == 10
    assert round(xirr, 3) == 0.17


def test_lumpsum_mode_uses_lumpsum_conversion():
    wm_ok = lumpsum_xirr_to_wm(0.16, 6)   # 16% over 6Y -> qualifies
    wm_no = lumpsum_xirr_to_wm(0.14, 6)   # 14% over 6Y -> excluded
    assert _select_additional_rec([(6, _stats(wm_ok))], 10, lumpsum_wm_to_xirr) is not None
    assert _select_additional_rec([(6, _stats(wm_no))], 10, lumpsum_wm_to_xirr) is None


def test_missing_data_handled():
    assert _select_additional_rec([], 10, sip_wm_to_xirr) is None
    assert _select_additional_rec(
        [("7", {"blended_wm": None, "n_windows": 5})], 10, sip_wm_to_xirr,
    ) is None
    assert _select_additional_rec([("junk", _stats(3.0))], 10, sip_wm_to_xirr) is None
