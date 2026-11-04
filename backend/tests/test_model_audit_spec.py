"""Clean-room validation of MODEL_AUDIT.md against the live FQS engine.

This is the Phase-3 gate for the point-in-time backtest: before any backtest
result can be trusted, the written specification must provably describe what
`fqs_engine.py` actually computes.

The functions below re-derive the rolling-SIP construction and every headline
parameter (Median WM, P10 WM, CV, Success %, Blended WM, FQS) from
MODEL_AUDIT.md **alone**. Nothing in the reference implementation imports the
scoring code under test — only `category_map` (which the audit documents as a
pure lookup table) and the engine's public entry point for comparison.

If a formula in MODEL_AUDIT.md is edited and these tests still pass, the audit
is still faithful. If someone changes fqs_engine.py without updating the audit,
these tests fail.

Additionally verifies the two structural properties the backtest engine relies
on:
  * §5.2  the instalment-count defect is real (WM denominator is hardcoded)
  * §7.2  rolling windows are strictly nested under NAV truncation, so
          point-in-time cutoffs are exact rather than approximate
"""
from __future__ import annotations

import calendar
import json
import math
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, for `backtest`

from category_map import (
    CATEGORY_TABLE,
    GENERIC_WM_SANITY_CAP,
    MAX_XIRR_FOR_DEBT,
    MAX_XIRR_FOR_GENERIC,
    classify_fund,
)
from fqs_engine import analyze_fund, compute_rolling_windows, extract_month_end_navs

SIP_AMOUNT = 10_000.0

# NAV fixtures: daily series for a spread of categories, 2019 → 2026.
NAV_FIXTURE = Path(__file__).resolve().parents[2] / "backtest_data" / "universe_nav.json"

# (scheme_code, scheme_name, AMFI scheme_category, expected internal category)
FUNDS = [
    ("130502", "HDFC Small Cap Fund - Growth",       "Equity Scheme - Small Cap Fund", "Small Cap"),
    ("107578", "Mirae Asset Large Cap Fund - Growth", "Equity Scheme - Large Cap Fund", "Large Cap"),
    ("114564", "Axis Midcap Fund - Growth",           "Equity Scheme - Mid Cap Fund",   "Mid Cap"),
    ("100822", "UTI Nifty 50 Index Fund - Growth",    "Other Scheme - Index Funds",     "Index Fund"),
    ("125494", "SBI Small Cap Fund - Growth",         "Equity Scheme - Small Cap Fund", "Small Cap"),
]

HORIZONS = [1, 3, 5]


# ── reference implementation, transcribed from MODEL_AUDIT.md ────────────────

def _eomonth(d: date, k: int = 0) -> date:
    """MODEL_AUDIT.md §5.1 — Excel EOMONTH."""
    y, m = d.year, d.month + k
    while m > 12:
        y += 1
        m -= 12
    while m < 1:
        y -= 1
        m += 12
    return date(y, m, calendar.monthrange(y, m)[1])


def _edate(d: date, k: int) -> date:
    """MODEL_AUDIT.md §5.1 — Excel EDATE."""
    y, m = d.year, d.month + k
    while m > 12:
        y += 1
        m -= 12
    while m < 1:
        y -= 1
        m += 12
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _month_ends(rows: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """MODEL_AUDIT.md §2.2 — last NAV present in each calendar month."""
    seen: dict[tuple[int, int], tuple[date, float]] = {}
    for d, nav in rows:
        seen[(d.year, d.month)] = (d, nav)
    return [seen[k] for k in sorted(seen)]


def _percentile_inc(values: list[float], p: float) -> float:
    """MODEL_AUDIT.md §6.2 — Excel PERCENTILE.INC."""
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] * (c - k) + s[c] * (k - f)


def _min_wm_required(category: str, horizon: int) -> float:
    """MODEL_AUDIT.md §6.6 — table anchors, else the closed-form SIP FV."""
    cat = CATEGORY_TABLE[category]
    anchored = cat["wm"].get(horizon)
    if anchored is not None:
        return anchored
    m = cat["xirr"] / 12.0
    n = horizon * 12
    return 1.0 if m == 0 else ((1 + m) ** n - 1) / (n * m)


def _max_plausible_wm(category: str, horizon: int) -> float:
    """MODEL_AUDIT.md §5.3 (2026-08-04: generic branch now horizon-scaled
    past ~20Y so genuine decades-long compounding on a 50Y horizon isn't
    mistaken for NAV corruption — see category_map.max_plausible_wm)."""
    if category == "Debt" and horizon > 0:
        return (1.0 + MAX_XIRR_FOR_DEBT) ** horizon
    if horizon > 0:
        return max(GENERIC_WM_SANITY_CAP, (1.0 + MAX_XIRR_FOR_GENERIC) ** horizon)
    return GENERIC_WM_SANITY_CAP


def _spec_window_wms(
    month_ends: list[tuple[date, float]], horizon: int, category: str
) -> tuple[list[float], list[int]]:
    """MODEL_AUDIT.md §5.1 — rolling SIP windows.

    Returns (wealth multiples, instalment counts) in chronological order.
    """
    n_months = horizon * 12
    if len(month_ends) < n_months + 1:
        return [], []

    invested = SIP_AMOUNT * n_months          # §5.2 — hardcoded denominator
    wm_ceiling = _max_plausible_wm(category, horizon)
    last_eom = _eomonth(month_ends[-1][0])
    dates = [d for d, _ in month_ends]

    a = _eomonth(month_ends[0][0]) + timedelta(days=1)
    wms: list[float] = []
    counts: list[int] = []

    while True:
        b = _edate(a, 12 * horizon)
        if _eomonth(b) > last_eom:
            break
        sip_idx = [i for i, d in enumerate(dates) if a <= d < b]   # half-open
        if not sip_idx:
            break
        end_candidates = [i for i, d in enumerate(dates) if d <= b]
        if not end_candidates:
            a = _eomonth(a, 1)
            continue
        units = sum(SIP_AMOUNT / month_ends[i][1] for i in sip_idx)
        wm = units * month_ends[end_candidates[-1]][1] / invested
        if wm <= wm_ceiling:
            wms.append(wm)
            counts.append(len(sip_idx))
        a = _eomonth(a, 1)

    return wms, counts


def _spec_metrics(wms: list[float], horizon: int, category: str) -> dict[str, float]:
    """MODEL_AUDIT.md §6 — every headline parameter."""
    n = len(wms)
    min_required = _min_wm_required(category, horizon)
    cap = float(CATEGORY_TABLE[category]["cap"])
    equity = CATEGORY_TABLE[category]["type"] == "equity"

    mean = sum(wms) / n
    median = _percentile_inc(wms, 0.50)                       # §6.2
    p10 = _percentile_inc(wms, 0.10)                          # §6.3
    p25 = _percentile_inc(wms, 0.25)
    std = statistics.pstdev(wms) if n >= 2 else 0.0
    cv = (std / mean) if mean else 0.0                        # §6.4

    success_pct = 1.0 - sum(1 for w in wms if w < min_required) / n   # §6.5, non-strict

    fail_score = (1.0 - success_pct) * 100.0
    downside_severity = min(100.0, 100.0 * min_required / p25) if p25 > 0 else 100.0
    cv_score = min(100.0, cv * 100.0)
    risk_score = 0.40 * fail_score + 0.25 * downside_severity + 0.35 * cv_score
    adj_risk = max(0.0, 100.0 - risk_score)

    blended_wm = 0.6 * median + 0.4 * p10                     # §6.7
    blended_wm_score = min(cap, blended_wm / min_required * 100.0)
    downside_score = min(cap, p10 / min_required * 100.0)
    consistency = sum(1 for w in wms if w > min_required) / n * 100.0   # strict >
    tail = [w for w in wms if w < p10]                                  # strict <
    es10 = (sum(tail) / len(tail)) if tail else min(wms)
    es_score = min(100.0, 100.0 * es10 / min_required)

    if equity:
        w_succ, w_arisk, w_bwm, w_cons, w_dn, w_es = 0.30, 0.20, 0.20, 0.15, 0.10, 0.05
    else:
        w_succ, w_arisk, w_bwm, w_cons, w_dn, w_es = 0.25, 0.35, 0.15, 0.10, 0.10, 0.05

    fqs = (
        w_succ * success_pct * 100.0
        + w_arisk * adj_risk
        + w_bwm * (blended_wm_score * 100.0 / cap)
        + w_dn * (downside_score * 100.0 / cap)
        + w_cons * consistency
        + w_es * es_score
    )

    return {
        "n_windows": n,
        "min_wm_required": min_required,
        "median_wm": median,
        "p10_wm": p10,
        "cv": cv,
        "success_pct": success_pct,
        "blended_wm": blended_wm,
        "fqs": max(0.0, min(100.0, fqs)),
    }


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def nav_series() -> dict[str, list[tuple[date, float]]]:
    if not NAV_FIXTURE.exists():
        pytest.skip(f"NAV fixture not present: {NAV_FIXTURE}")
    raw = json.loads(NAV_FIXTURE.read_text())
    out = {}
    for code, rows in raw.items():
        parsed = [(date.fromisoformat(d), float(v)) for d, v in rows]
        parsed.sort()
        out[code] = parsed
    return out


# ── tests ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("code,name,amfi_cat,expected_cat", FUNDS)
def test_classification_matches_audit_table(code, name, amfi_cat, expected_cat):
    """MODEL_AUDIT.md §6.6 — the category drives hurdle, cap and weight vector."""
    assert classify_fund(name, amfi_cat) == expected_cat


@pytest.mark.parametrize("code,name,amfi_cat,expected_cat", FUNDS)
def test_audit_formulas_reproduce_engine(nav_series, code, name, amfi_cat, expected_cat):
    """Every parameter in MODEL_AUDIT.md §5-6 matches fqs_engine exactly."""
    rows = nav_series[code]
    category = classify_fund(name, amfi_cat)

    engine = analyze_fund(
        [{"date": d, "nav": v} for d, v in rows],
        category,
        horizons=HORIZONS,
        include_windows=False,
    )
    assert engine["horizons"], f"engine produced no horizons for {name}"

    month_ends = _month_ends(rows)

    for h in engine["horizons"]:
        horizon = h["horizon"]
        wms, _ = _spec_window_wms(month_ends, horizon, category)
        assert wms, f"spec produced no windows at {horizon}Y but the engine did"
        spec = _spec_metrics(wms, horizon, category)

        assert h["n_windows"] == spec["n_windows"], f"{name} {horizon}Y n_windows"

        for field, tol in [
            ("min_wm_required", 1e-4),
            ("median_wm", 1e-4),
            ("p10_wm", 1e-4),
            ("cv", 1e-4),
            ("success_pct", 1e-4),
            ("blended_wm", 1e-4),
            ("fqs", 0.05),          # engine rounds FQS to 1 dp
        ]:
            assert h[field] == pytest.approx(spec[field], abs=tol), (
                f"{name} {horizon}Y {field}: engine={h[field]} spec={spec[field]}"
            )


def test_instalment_count_defect_is_real(nav_series):
    """MODEL_AUDIT.md §5.2 — `total_invested` is hardcoded to 10000 x 12H while
    the actual instalment count varies with calendar alignment.

    This is a known defect reproduced deliberately by the backtest, NOT fixed.
    If this test ever fails because counts are uniform, §5.2 must be revised.
    """
    month_ends = _month_ends(nav_series["130502"])
    _, counts = _spec_window_wms(month_ends, 5, "Small Cap")
    assert counts, "no windows produced"
    assert len(set(counts)) > 1, (
        "instalment count is uniform — MODEL_AUDIT.md §5.2 needs revisiting"
    )
    assert min(counts) < 60 <= max(counts), f"unexpected instalment spread: {set(counts)}"


def test_naive_end_date_filter_is_unsafe(nav_series):
    """MODEL_AUDIT.md §7.2 — regression guard for a corrected claim.

    An earlier revision of the audit said truncated windows equal the
    full-history windows with `end_date <= cutoff`. They do not: the model
    terminates on the *nominal* redemption month EOMONTH(B_k), which can be the
    month AFTER the one the end NAV is read from. This test pins the
    counter-example so the wrong rule cannot quietly come back.
    """
    rows = nav_series["130502"]
    horizon, category = 5, "Small Cap"
    min_required = _min_wm_required(category, horizon)
    cutoff = date(2024, 5, 31)

    nav_full = [{"date": d, "nav": v} for d, v in rows]
    nav_cut = [r for r in nav_full if r["date"] <= cutoff]

    full = compute_rolling_windows(
        extract_month_end_navs(nav_full), horizon, min_required, category
    )
    truncated = compute_rolling_windows(
        extract_month_end_navs(nav_cut), horizon, min_required, category
    )
    naive = [w for w in full if date.fromisoformat(w.end_date) <= cutoff]

    assert len(truncated) == 0, "expected the model to admit no window at this cutoff"
    assert len(naive) == 1, "expected the naive filter to over-admit exactly one"


def test_windows_are_nested_under_the_corrected_rule(nav_series):
    """MODEL_AUDIT.md §7.2 — the property the point-in-time engine depends on.

    Checked at EVERY cutoff, for a clean fund and for a synthetic 100x unit
    change that forces `sanitize_nav_history` to rebase a prefix of the series.
    If this breaks, every cutoff needs a full recomputation.
    """
    from backtest import point_in_time as pit

    rows = nav_series["130502"]
    cutoffs = pit.cutoffs_for_fund(rows, rows[-1][0], min_history_years=1)
    assert len(cutoffs) > 50, "fixture should span many cutoffs"

    holds, msg = pit.verify_nesting(rows, "Small Cap", 5, cutoffs)
    assert holds, f"clean series: {msg}"

    mid = len(rows) // 2
    spliced = [(d, n * 100.0 if i >= mid else n) for i, (d, n) in enumerate(rows)]
    assert pit.splice_dates(spliced), "synthetic splice was not detected"

    holds, msg = pit.verify_nesting(spliced, "Small Cap", 5, cutoffs)
    assert holds, f"spliced series: {msg}"


def test_duplicate_start_dates_exist(nav_series):
    """MODEL_AUDIT.md §7.2 — why B_k cannot be matched back by start_date.

    Data gaps make consecutive rolling windows share a first SIP date. If that
    ever stops being true the audit's second trap can be relaxed, but until then
    the replay in `build_all_windows` is load-bearing.
    """
    from backtest import point_in_time as pit

    windows, b_eoms = pit.build_all_windows(nav_series["130502"], "Small Cap", 5)
    assert len(windows) == len(b_eoms)
    starts = [w.start_date for w in windows]
    assert len(starts) != len(set(starts)), (
        "no duplicate start dates in the fixture — MODEL_AUDIT.md §7.2 "
        "second trap needs revisiting"
    )


def test_cycle_robustness_is_none_below_ten_years(nav_series):
    """MODEL_AUDIT.md §7 — CR is not defined under a 10Y user horizon, so it is
    not a testable parameter at the 5Y backtest horizon."""
    rows = nav_series["130502"]
    result = analyze_fund(
        [{"date": d, "nav": v} for d, v in rows],
        "Small Cap",
        horizons=[3, 5],
        include_windows=False,
        user_years=5,
    )
    assert result["cycle_robustness"] is None
    assert result["cycle_robustness_variant"] is None
