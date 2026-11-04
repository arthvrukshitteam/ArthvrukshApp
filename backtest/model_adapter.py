"""Run the real AV FQS model on point-in-time data.

This module deliberately contains **no financial logic**. Scoring is done by
`fqs_engine.score_horizon` — the exact function the production app calls. All
this file does is hand it the right windows and unpack the result.

Two paths exist and must agree:

    slow  truncate NAVs → compute_rolling_windows → score_horizon
    fast  build windows once → slice → score_horizon

The fast path is what the backtest runs (MODEL_AUDIT.md §7.2). `cross_check()`
proves the two agree for a fund before its results are trusted.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Sequence

from . import config as C
from . import point_in_time as pit

import sys
sys.path.insert(0, str(C.BACKEND))
from fqs_engine import score_horizon  # noqa: E402  — the model itself


@dataclass
class Prediction:
    """One fund's frozen model output at one cutoff. MODEL_AUDIT.md §22."""
    cutoff_date: str
    scheme_code: int
    scheme_name: str
    internal_category: str
    amfi_category: str
    horizon: int
    sip_amount: float
    investment_mode: str

    historical_data_start: str
    historical_data_end: str
    available_history_years: float
    required_history_years: int
    monthly_points: int
    n_rolling_observations: int

    # The five headline parameters (MODEL_AUDIT.md §6)
    median_wm: float
    p10_wm: float
    cv: float
    success_pct: float
    fqs: float

    # The metric the app actually ranks on (MODEL_AUDIT.md §7.1)
    blended_wm: float

    # Benchmark — the model's only one (MODEL_AUDIT.md §6.6)
    benchmark_min_wm: float
    benchmark_source: str

    # Remaining model outputs, kept for debuggability
    mean_wm: float
    min_wm: float
    max_wm: float
    p25_wm: float
    p75_wm: float
    p90_wm: float
    std_wm: float
    failure_pct: float
    adj_risk_score: float
    blended_wm_score: float
    downside_score: float
    consistency_score: float
    es10: float
    es_score: float
    stress_score: float
    stability_score: float
    alpha_trend: float | None
    cap: int
    fqs_tier: str

    # Wealth projection for a 100%-allocation single-fund portfolio
    # (proposal_engine.py:274-279 with allocation_pct = 1.0)
    predicted_wealth_median: float
    predicted_wealth_p10: float
    predicted_xirr_median: float | None
    predicted_xirr_p10: float | None

    # ── Baselines. NOT model outputs — the yardsticks the model must beat.
    # Without these, "the top decile hit the real top decile X% of the time" is
    # uninterpretable (MODEL_AUDIT.md §12).
    #: The single most recent completed H-year SIP window. Ranking on this alone
    #: is the naive momentum baseline, and the model is largely a smoothed
    #: version of it — so this is the yardstick that matters most.
    trailing_wm: float
    #: Point-to-point NAV growth over the same lookback, annualised.
    trailing_cagr: float | None

    prediction_status: str = "COMPLETE"

    def to_row(self) -> dict:
        return asdict(self)


def _tier(fqs: float) -> str:
    from fqs_engine import fqs_tier

    return fqs_tier(fqs)


def predict(
    *,
    cutoff: date,
    scheme_code: int,
    scheme_name: str,
    internal_category: str,
    amfi_category: str,
    horizon: int,
    windows_full: Sequence,
    b_eoms: Sequence[date],
    last_month_end: date,
    history_years: float,
    monthly_points: int,
    first_nav: str,
    last_nav: str,
) -> Prediction | None:
    """Score this fund using only windows admissible at `cutoff`."""
    from xirr_utils import sip_wm_to_xirr

    n = pit.admissible_count(b_eoms, last_month_end)
    if n < 1:
        return None

    result = score_horizon(list(windows_full[:n]), horizon, internal_category)
    if result is None:
        return None

    months = horizon * 12
    invested = C.SIP_AMOUNT * months

    # Baselines, computed from the same admissible window set (no extra data).
    # The most recent completed window IS the naive "just look at the last H
    # years" strategy the model must beat.
    trailing_wm = windows_full[n - 1]._wm_raw
    trailing_cagr = sip_wm_to_xirr(trailing_wm, horizon) if trailing_wm > 0 else None

    return Prediction(
        cutoff_date=cutoff.isoformat(),
        scheme_code=scheme_code,
        scheme_name=scheme_name,
        internal_category=internal_category,
        amfi_category=amfi_category,
        horizon=horizon,
        sip_amount=C.SIP_AMOUNT,
        investment_mode=C.INVESTMENT_MODE,
        historical_data_start=first_nav,
        historical_data_end=last_nav,
        available_history_years=history_years,
        required_history_years=C.MIN_HISTORY_YEARS,
        monthly_points=monthly_points,
        n_rolling_observations=result.n_windows,
        median_wm=result.median_wm,
        p10_wm=result.p10_wm,
        cv=result.cv,
        success_pct=result.success_pct,
        fqs=result.fqs,
        blended_wm=result.blended_wm,
        benchmark_min_wm=result.min_wm_required,
        benchmark_source="category_map.CATEGORY_TABLE",
        mean_wm=result.mean_wm,
        min_wm=result.min_wm,
        max_wm=result.max_wm,
        p25_wm=result.p25_wm,
        p75_wm=result.p75_wm,
        p90_wm=result.p90_wm,
        std_wm=result.std_wm,
        failure_pct=result.failure_pct,
        adj_risk_score=result.adj_risk_score,
        blended_wm_score=result.blended_wm_score,
        downside_score=result.downside_score,
        consistency_score=result.consistency_score,
        es10=result.es10,
        es_score=result.es_score,
        stress_score=result.stress_score,
        stability_score=result.stability_score,
        alpha_trend=result.alpha_trend,
        cap=result.cap,
        fqs_tier=_tier(result.fqs),
        predicted_wealth_median=round(result.median_wm * invested, 2),
        predicted_wealth_p10=round(result.p10_wm * invested, 2),
        predicted_xirr_median=sip_wm_to_xirr(result.median_wm, horizon),
        predicted_xirr_p10=sip_wm_to_xirr(result.p10_wm, horizon),
        trailing_wm=trailing_wm,
        trailing_cagr=trailing_cagr,
    )


def cross_check(
    raw: Sequence[tuple[date, float]],
    category: str,
    horizon: int,
    cutoffs: Sequence[date],
) -> tuple[bool, str]:
    """Prove the fast path scores identically to a full recomputation.

    Compares every headline parameter, not just window counts — a window-count
    match with a scoring mismatch would be worse than either alone.
    """
    windows_full, b_eoms = pit.build_all_windows(raw, category, horizon)
    if not windows_full:
        return True, "no windows to compare"

    for cutoff in cutoffs:
        slow_windows, _ = pit.windows_at(raw, category, horizon, cutoff)
        slow = score_horizon(slow_windows, horizon, category) if slow_windows else None

        lme = pit.last_month_end_at(raw, cutoff)
        n = pit.admissible_count(b_eoms, lme) if lme else 0
        fast = score_horizon(list(windows_full[:n]), horizon, category) if n else None

        if (slow is None) != (fast is None):
            return False, f"{cutoff}: one path scored and the other did not"
        if slow is None:
            continue
        for field in ("n_windows", "median_wm", "p10_wm", "cv",
                      "success_pct", "blended_wm", "fqs"):
            a, b = getattr(slow, field), getattr(fast, field)
            if a != b:
                return False, f"{cutoff}: {field} slow={a!r} fast={b!r}"
    return True, "fast path matches full recomputation on every parameter"
