"""Regression test for proposal_engine.py's portfolio-blending math.

Uses the exact funds/allocations/WM figures from the uploaded
"ArthVruksh MF Proposal_Priya Ravi.xlsx" template and asserts our
build_mode_projection() / build_swp_projection() reproduce the template's own
computed cells for a single horizon (the template originally showed 3/5/7Y
side-by-side; the app now supports one horizon at a time — the advisor's own
tenure — so each horizon is checked individually against the template's
per-column values). Also covers the zero-corpus/zero-return regression found
in production: default_swp_inputs() must return None, not 0, when a horizon
has no usable fund data, and proposal_routes.py must refuse to build in that
case (covered by the all_funds_excluded() unit test below; the route-level
422 itself needs a running server so isn't re-tested here).

Pure-math test — no network, no Mongo, no FastAPI.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from proposal_engine import (  # noqa: E402
    FundRow, _required_min_wm, all_funds_excluded, build_fund_row,
    build_mode_projection, build_swp_projection, default_swp_inputs,
)
from xirr_utils import lumpsum_xirr_to_wm, sip_xirr_to_wm, wm_to_xirr  # noqa: E402


def _make_funds(raw, horizon):
    """raw: list of (name, alloc, {horizon: (median_wm, p10_wm)})."""
    funds = []
    for name, alloc, hz in raw:
        median_wm, p10_wm = hz[horizon]
        funds.append(FundRow(
            scheme_code=0, scheme_name=name, category="Equity", allocation_pct=alloc,
            fqs=None, success_pct=None,
            median_wm=median_wm, xirr_median=None,
            p10_wm=p10_wm, xirr_p10=None,
            available=True,
        ))
    return funds


SIP_FUNDS_RAW = [
    ("Parag Parikh Flexicap", 0.25, {3: (1.29, 1.13), 5: (1.61, 1.32), 7: (2.02, 1.76)}),
    ("Kotak Midcap",          0.25, {3: (1.36, 1.05), 5: (1.75, 1.25), 7: (1.99, 1.67)}),
    ("Axis Smallcap",         0.40, {3: (1.30, 1.10), 5: (1.71, 1.28), 7: (2.18, 1.95)}),
    ("Nippon Largecap",       0.10, {3: (1.26, 1.08), 5: (1.51, 1.25), 7: (1.73, 1.44)}),
]

LUMPSUM_FUNDS_RAW = [
    ("Parag Parikh Flexicap", 0.25, {3: (1.66, 1.38), 5: (2.31, 1.72), 7: (3.24, 2.89)}),
    ("Kotak Midcap",          0.25, {3: (1.79, 1.10), 5: (2.43, 1.78), 7: (3.24, 2.82)}),
    ("Axis Smallcap",         0.40, {3: (1.65, 1.31), 5: (2.46, 1.73), 7: (3.55, 3.20)}),
    ("Nippon Largecap",       0.10, {3: (1.5646206749642433, 1.0886425132656288),
                                     5: (2.0170180052599638, 1.5488881015743772),
                                     7: (2.494808368878234, 2.1182820374375613)}),
]

# Expected per-horizon SIP aggregates (from the template's U/X columns)
SIP_EXPECTED = {
    3: dict(capital=3_600_000, wealth_med=4_710_600, wm_med=1.3085, xirr_med=0.17770,
            wealth_p10=3_934_800, wm_p10=1.0930, xirr_p10=0.06020),
    5: dict(capital=6_000_000, wealth_med=10_050_000, wm_med=1.6750, xirr_med=0.19558,
            wealth_p10=7_677_000, wm_p10=1.2795, xirr_p10=0.09672),
    7: dict(capital=8_400_000, wealth_med=17_199_000, wm_med=2.0475, xirr_med=0.18835,
            wealth_p10=14_964_600, wm_p10=1.7815, xirr_p10=0.15423),
}

LUMPSUM_EXPECTED = {
    3: dict(wealth_med=1_678_962, wm_med=1.6790, xirr_med=0.18854, wm_p10=1.2529, xirr_p10=0.07804),
    5: dict(wealth_med=2_370_702, wm_med=2.3707, xirr_med=0.18843, wm_p10=1.7219, xirr_p10=0.11481),
    7: dict(wealth_med=3_289_481, wm_med=3.2895, xirr_med=0.18543, wm_p10=2.9193, xirr_p10=0.16538),
}


@pytest.mark.parametrize("horizon", [3, 5, 7])
def test_sip_portfolio_matches_template(horizon):
    funds = _make_funds(SIP_FUNDS_RAW, horizon)
    proj = build_mode_projection(funds, investment_amount=100_000, horizon=horizon, mode="sip")
    a = proj.aggregate
    exp = SIP_EXPECTED[horizon]

    assert a.capital_invested == pytest.approx(exp["capital"], abs=1)
    assert a.expected_wealth_median == pytest.approx(exp["wealth_med"], abs=1)
    assert a.blended_wm_median == pytest.approx(exp["wm_med"], abs=1e-3)
    assert a.blended_xirr_median == pytest.approx(exp["xirr_med"], abs=1e-3)
    assert a.expected_wealth_p10 == pytest.approx(exp["wealth_p10"], abs=1)
    assert a.blended_wm_p10 == pytest.approx(exp["wm_p10"], abs=1e-3)
    assert a.blended_xirr_p10 == pytest.approx(exp["xirr_p10"], abs=1e-3)


@pytest.mark.parametrize("horizon", [3, 5, 7])
def test_lumpsum_portfolio_matches_template(horizon):
    funds = _make_funds(LUMPSUM_FUNDS_RAW, horizon)
    proj = build_mode_projection(funds, investment_amount=1_000_000, horizon=horizon, mode="lumpsum")
    a = proj.aggregate
    exp = LUMPSUM_EXPECTED[horizon]

    assert a.capital_invested == pytest.approx(1_000_000, abs=1)
    assert a.expected_wealth_median == pytest.approx(exp["wealth_med"], abs=5)
    assert a.blended_wm_median == pytest.approx(exp["wm_med"], abs=1e-3)
    assert a.blended_xirr_median == pytest.approx(exp["xirr_med"], abs=1e-3)
    assert a.blended_wm_p10 == pytest.approx(exp["wm_p10"], abs=1e-3)
    assert a.blended_xirr_p10 == pytest.approx(exp["xirr_p10"], abs=1e-3)


def test_swp_matches_template():
    swp = build_swp_projection(
        corpus=20_500_000, annual_return=0.15, inflation=0.06,
        monthly_withdrawal=200_000, years=10,
    )
    assert swp.withdrawal_with_inflation == pytest.approx(217_666.67, abs=1)
    assert len(swp.rows) == 120
    assert swp.rows[0].closing_balance == pytest.approx(20_535_862.5, abs=1)
    assert swp.rows[-1].closing_balance == pytest.approx(30_369_971.75, abs=1)


# ── Benchmark XIRR must not depend on the upstream analysis dict carrying
#    min_wm_required (regression: blank Benchmark XIRR on Review & Update
#    whenever a fund's numbers came through the Since-2010 / Separate
#    Management variant path) ────────────────────────────────────────────────

def _variant_horizon(horizon, median_wm=2.05, p10_wm=1.72, n_windows=30, fqs=72.0):
    """Shaped exactly like fqs_refresh.py's _build_fqs_by_horizon() output —
    the dict actually stored in Mongo for the plain / since_2010 / separate
    management precomputed variants. Deliberately has NO min_wm_required key,
    unlike a live analyze_fund() call's output."""
    return {
        "horizon": horizon,
        "fqs": fqs,
        "success_pct": 0.9,
        "blended_wm": median_wm,
        "median_wm": median_wm,
        "p10_wm": p10_wm,
        "n_windows": n_windows,
    }


def _variant_shaped_analysis(horizon=7):
    return {
        "scheme_name": "Toggle Path Fund",
        "category": "Equity",
        "horizons": [_variant_horizon(horizon)],
    }


@pytest.mark.parametrize("mode", ["sip", "lumpsum"])
def test_benchmark_xirr_populated_when_variant_dict_lacks_min_wm_required(mode):
    horizon = 7
    row = build_fund_row(1, "Toggle Path Fund", "Equity", 1.0,
                         _variant_shaped_analysis(horizon), horizon, mode)

    assert row.available is True
    # This used to be None because build_fund_row trusted a "min_wm_required"
    # key that _build_fqs_by_horizon() never writes — it's independently
    # derivable from (category, horizon, mode) via _required_min_wm(), so it
    # must be populated regardless of what the upstream dict contains.
    assert row.min_wm_required == pytest.approx(_required_min_wm("Equity", horizon, mode))
    assert row.benchmark_xirr is not None
    assert row.benchmark_xirr == pytest.approx(
        wm_to_xirr(mode, _required_min_wm("Equity", horizon, mode), horizon)
    )


def test_wm_xirr_roundtrip():
    for wm, h in [(1.5, 5), (2.2, 7), (1.05, 3)]:
        r_sip = wm_to_xirr("sip", wm, h)
        assert sip_xirr_to_wm(r_sip, h) == pytest.approx(wm, abs=1e-3)
        r_lump = wm_to_xirr("lumpsum", wm, h)
        assert lumpsum_xirr_to_wm(r_lump, h) == pytest.approx(wm, abs=1e-3)


def test_insufficient_history_excluded_not_silently_dropped():
    """A fund missing data at the chosen horizon should be excluded from the
    blend AND surfaced in `excluded_funds`, not silently zeroed."""
    funds = _make_funds(SIP_FUNDS_RAW, 7)
    funds[-1].available = False
    funds[-1].median_wm = None

    proj = build_mode_projection(funds, investment_amount=100_000, horizon=7, mode="sip")
    assert proj.aggregate.excluded_funds == ["Nippon Largecap"]
    assert not all_funds_excluded(proj)


def test_all_funds_excluded_blocks_swp_auto_derive():
    """Regression test for the production bug: when NONE of the selected
    funds have data at the chosen horizon (e.g. a 20Y horizon none of them
    have history for), default_swp_inputs() must return None/None — never a
    silent 0 corpus / 0% return that produces a Systematic Withdrawal Plan
    which just bleeds money with no warning."""
    funds = _make_funds(SIP_FUNDS_RAW, 7)
    for f in funds:
        f.available = False
        f.median_wm = None
        f.p10_wm = None

    proj = build_mode_projection(funds, investment_amount=100_000, horizon=20, mode="sip")
    assert all_funds_excluded(proj)
    assert proj.aggregate.expected_wealth_median == 0

    defaults = default_swp_inputs(proj)
    assert defaults["corpus"] is None
    assert defaults["annual_return"] is None
