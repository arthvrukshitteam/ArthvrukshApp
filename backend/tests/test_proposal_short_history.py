"""Additional Recommendation funds inside a LONGER-tenure portfolio.

Scenario from the spec: a fund qualifies as an Additional Recommendation at 7Y
(>=10 rolling windows, ~15% median XIRR) but the advisor's tenure is 10Y.

Expected behaviour (both SIP and Lumpsum):
  * the fund is NOT dropped from the portfolio;
  * every DISPLAYED parameter (fqs / success_pct / median_wm / p10_wm and both
    XIRRs) is its 7Y figure, and `metrics_horizon == 7`;
  * `extrapolated` is True so the PDF / XLSX / app can asterisk the row;
  * expected Median and P10 wealth use the 7Y XIRR compounded out to 10Y.

Pure-math test — no network, no Mongo, no FastAPI.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from proposal_engine import (  # noqa: E402
    build_fund_row, build_mode_projection, extract_fund_metrics,
)
from xirr_utils import wm_to_xirr, xirr_to_wm  # noqa: E402

HORIZON = 10  # user input
SHORT = 7     # fund's own longest robust window


def _hz(horizon, median_wm, p10_wm, n_windows=30, fqs=72.0):
    return {
        "horizon": horizon,
        "fqs": fqs,
        "success_pct": 0.9,
        "blended_wm": median_wm,
        "median_wm": median_wm,
        "p10_wm": p10_wm,
        "n_windows": n_windows,
    }


def _short_history_analysis(median_wm=2.05, p10_wm=1.72):
    """A fund with data at 5,6,7Y only — nothing at the 10Y tenure."""
    return {
        "scheme_name": "Seven Year Fund",
        "category": "Equity",
        "horizons": [
            _hz(5, 1.60, 1.35),
            _hz(6, 1.80, 1.50),
            _hz(SHORT, median_wm, p10_wm),
        ],
    }


def _full_history_analysis():
    return {
        "scheme_name": "Ten Year Fund",
        "category": "Equity",
        "horizons": [_hz(SHORT, 2.10, 1.80), _hz(HORIZON, 2.90, 2.30)],
    }


# ── window selection ──────────────────────────────────────────────────────────

def test_falls_back_to_longest_robust_window():
    m = extract_fund_metrics(_short_history_analysis(), HORIZON)
    assert m["metrics_horizon"] == SHORT
    assert m["median_wm"] == 2.05
    assert m["p10_wm"] == 1.72


def test_full_history_fund_is_unaffected():
    m = extract_fund_metrics(_full_history_analysis(), HORIZON)
    assert m["metrics_horizon"] == HORIZON
    assert m["median_wm"] == 2.90


def test_thin_windows_are_never_used():
    """A 7Y window with <10 rolling windows has no real distribution, so the
    fund falls back further (6Y here) rather than using it."""
    a = _short_history_analysis()
    a["horizons"][-1]["n_windows"] = 4
    assert extract_fund_metrics(a, HORIZON)["metrics_horizon"] == 6


def test_no_window_at_all_stays_unavailable():
    a = {"horizons": [_hz(3, 1.3, 1.1)]}  # below the 5Y floor
    row = build_fund_row(1, "X", "Equity", 1.0, a, HORIZON, "sip")
    assert row.available is False
    assert row.metrics_horizon is None


# ── displayed parameters stay at the short window ─────────────────────────────

@pytest.mark.parametrize("mode", ["sip", "lumpsum"])
def test_displayed_parameters_are_all_at_the_short_window(mode):
    row = build_fund_row(1, "Seven Year Fund", "Equity", 1.0,
                         _short_history_analysis(), HORIZON, mode)

    assert row.available is True
    assert row.extrapolated is True
    assert row.metrics_horizon == SHORT

    # Not a single displayed figure is computed at HORIZON.
    assert row.median_wm == 2.05
    assert row.p10_wm == 1.72
    assert row.xirr_median == pytest.approx(wm_to_xirr(mode, 2.05, SHORT))
    assert row.xirr_p10 == pytest.approx(wm_to_xirr(mode, 1.72, SHORT))


@pytest.mark.parametrize("mode", ["sip", "lumpsum"])
def test_wealth_multiples_are_extrapolated_to_the_user_horizon(mode):
    row = build_fund_row(1, "Seven Year Fund", "Equity", 1.0,
                         _short_history_analysis(), HORIZON, mode)

    assert row.proj_wm_median == pytest.approx(xirr_to_wm(mode, row.xirr_median, HORIZON))
    assert row.proj_wm_p10 == pytest.approx(xirr_to_wm(mode, row.xirr_p10, HORIZON))
    # Extrapolating a positive XIRR over 3 more years must grow the multiple.
    assert row.proj_wm_median > row.median_wm
    assert row.proj_wm_p10 > row.p10_wm


@pytest.mark.parametrize("mode", ["sip", "lumpsum"])
def test_full_history_fund_projects_its_own_multiples_unchanged(mode):
    row = build_fund_row(2, "Ten Year Fund", "Equity", 1.0,
                         _full_history_analysis(), HORIZON, mode)
    assert row.extrapolated is False
    assert row.metrics_horizon == HORIZON
    assert row.proj_wm_median == row.median_wm
    assert row.proj_wm_p10 == row.p10_wm


# ── portfolio blend ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode,amount", [("sip", 10_000.0), ("lumpsum", 1_000_000.0)])
def test_short_history_fund_contributes_to_portfolio_wealth(mode, amount):
    short = build_fund_row(1, "Seven Year Fund", "Equity", 0.4,
                           _short_history_analysis(), HORIZON, mode)
    full = build_fund_row(2, "Ten Year Fund", "Equity", 0.6,
                          _full_history_analysis(), HORIZON, mode)

    proj = build_mode_projection([short, full], amount, HORIZON, mode)
    agg = proj.aggregate

    # Regression: it used to be silently excluded, contributing Rs. 0 wealth
    # while still counting toward capital invested.
    assert agg.excluded_funds == []
    assert agg.extrapolated_funds == ["Seven Year Fund"]

    def contribution(row, wm):
        return row.allocation_pct * amount * wm * (HORIZON * 12 if mode == "sip" else 1)

    expected_median = contribution(short, short.proj_wm_median) + contribution(full, full.proj_wm_median)
    assert agg.expected_wealth_median == pytest.approx(round(expected_median, 2), rel=1e-9)

    expected_p10 = contribution(short, short.proj_wm_p10) + contribution(full, full.proj_wm_p10)
    assert agg.expected_wealth_p10 == pytest.approx(round(expected_p10, 2), rel=1e-9)


def test_blend_uses_extrapolated_not_raw_multiples():
    """Guard against a regression where the blend reads median_wm (the 7Y
    multiple) instead of proj_wm_median (the 10Y one)."""
    mode, amount = "sip", 10_000.0
    short = build_fund_row(1, "Seven Year Fund", "Equity", 1.0,
                           _short_history_analysis(), HORIZON, mode)
    agg = build_mode_projection([short], amount, HORIZON, mode).aggregate

    raw = amount * short.median_wm * HORIZON * 12
    assert agg.expected_wealth_median > raw
    # And the blended XIRR at 10Y round-trips back to the fund's 7Y XIRR.
    assert agg.blended_xirr_median == pytest.approx(short.xirr_median, abs=1e-6)
