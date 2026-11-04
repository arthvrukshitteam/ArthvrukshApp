"""Fund Quality Score (FQS) engine.

Faithful replica of the Excel model — every formula from
RAW_NAV → MONTHLY_NAV → SIP_CASHFLOWS → ROLLING_SIP_{N}Y → DISTRIBUTION_METRICS → FQS.

Per-fund pipeline:
  1. Daily NAV history (already fetched from AMFI / mfapi.in)
  2. Apply optional start-date filter
  3. Extract month-end NAV (last NAV in each calendar month)
  4. Compute full-history XIRR (SIP_CASHFLOWS sheet)
  5. For every horizon n ∈ 1..20 (only where ≥ n×12+1 monthly points exist):
       a. Build every rolling SIP window
       b. Compute WM = final value / invested
       c. Per-window detail rows (date, end NAV, units, end value, WM, good/bad)
       d. Stats block (min, max, mean, median, P10/P25/P75/P90, σ, CV)
       e. Sub-scores (Success, Adj Risk, Blended WM, Downside, ES, Consistency)
       f. NEW: Alpha Trend, Stability Score (per-horizon), Stress Score
       g. FQS via equity / non-equity weight vector
  6. Aggregate:
       - Cycle Robustness — 3 variants based on user's rolling year input:
           <10Y  → None (not meaningful)
           10-14Y → 0.35×S3 + 0.35×S5 + 0.30×S10
           ≥15Y  → 0.30×S3 + 0.30×S5 + 0.25×S10 + 0.15×S15
       - Stability Check (cross-horizon) = Median_10Y / Median_5Y
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple, Iterable
import math
import statistics

from pyxirr import xirr


def _eomonth(d: date, months_ahead: int) -> date:
    """Excel EOMONTH(d, n) — last day of the month n months from d."""
    y, m = d.year, d.month + months_ahead
    while m > 12: y += 1; m -= 12
    while m < 1:  y -= 1; m += 12
    return date(y, m, calendar.monthrange(y, m)[1])


def _edate(d: date, months_ahead: int) -> date:
    """Excel EDATE(d, n) — same day-of-month n months ahead, capped to month length."""
    y, m = d.year, d.month + months_ahead
    while m > 12: y += 1; m -= 12
    while m < 1:  y -= 1; m += 12
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


from category_map import (
    min_wm_for, cap_for, is_equity, DEFAULT_CATEGORY, max_plausible_wm,
)

SIP_AMOUNT = 10_000  # ₹ per month — matches sheet B1

# Supported rolling periods. Horizons longer than a fund's NAV history yield
# zero windows and are skipped by compute_rolling_windows, so raising the cap
# adds capability without inventing data for funds that lack the history.
MAX_HORIZON_YEARS = 50
HORIZONS_FULL = list(range(1, MAX_HORIZON_YEARS + 1))


# ─── 1. month-end extraction ─────────────────────────────────────────────────

def extract_month_end_navs(
    nav_history: List[Dict[str, Any]],
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """Return one NAV per calendar month — the last NAV of that month.

    `end_date` (upper bound, inclusive) is the Rolling Analysis tab's "as of"
    date — it truncates the series the exact same way `start_date` narrows
    it from below, so a fund's rolling windows are computed using ONLY NAVs
    inside [start_date, end_date] when both are given.
    """
    if not nav_history:
        return []
    filtered = nav_history
    if start_date is not None:
        filtered = [r for r in filtered if r["date"] >= start_date]
    if end_date is not None:
        filtered = [r for r in filtered if r["date"] <= end_date]
    out: List[Dict[str, Any]] = []
    for i, row in enumerate(filtered):
        d: date = row["date"]
        nxt = filtered[i + 1]["date"] if i + 1 < len(filtered) else None
        is_month_end = (nxt is None) or (nxt.year != d.year or nxt.month != d.month)
        if is_month_end:
            out.append({"date": d, "nav": float(row["nav"])})
    return out


# ─── 2. headline XIRR (SIP_CASHFLOWS) ────────────────────────────────────────

def compute_full_history_xirr(monthly: List[Dict[str, Any]]) -> Optional[float]:
    if len(monthly) < 13:
        return None
    cashflows: List[Tuple[date, float]] = []
    units = 0.0
    for row in monthly[:-1]:
        units += SIP_AMOUNT / row["nav"]
        cashflows.append((row["date"], -SIP_AMOUNT))
    last = monthly[-1]
    units += SIP_AMOUNT / last["nav"]
    final_value = units * last["nav"]
    cashflows.append((last["date"], final_value - SIP_AMOUNT))
    try:
        rate = xirr([cf[0] for cf in cashflows], [cf[1] for cf in cashflows])
        if rate is None or math.isnan(rate) or math.isinf(rate):
            return None
        return float(rate)
    except Exception:
        return None


# ─── 3. rolling windows (ROLLING_SIP_{N}Y sheet) ─────────────────────────────

@dataclass
class RollingWindow:
    start_date: str
    end_date: str
    total_units: float
    end_nav: float
    end_value: float
    total_invested: float
    wealth_multiple: float
    good: bool
    _wm_raw: float = 0.0


def compute_rolling_windows(
    monthly: List[Dict[str, Any]],
    horizon_years: int,
    min_required: float,
    category: Optional[str] = None,
) -> List[RollingWindow]:
    """Excel-faithful rolling SIP windows."""
    wm_cap = max_plausible_wm(category or DEFAULT_CATEGORY, horizon_years)
    H = horizon_years
    N = H * 12
    if len(monthly) < N + 1:
        return []

    invested = SIP_AMOUNT * N
    last_eom = _eomonth(monthly[-1]["date"], 0)
    dates = [m["date"] for m in monthly]
    a = _eomonth(monthly[0]["date"], 0) + timedelta(days=1)
    out: List[RollingWindow] = []

    while True:
        b = _edate(a, 12 * H)
        if _eomonth(b, 0) > last_eom:
            break
        sip_idx = [i for i, d in enumerate(dates) if a <= d < b]
        if not sip_idx:
            break
        end_candidates = [i for i, d in enumerate(dates) if d <= b]
        if not end_candidates:
            a = _eomonth(a, 1)
            continue
        end_i = end_candidates[-1]
        units = sum(SIP_AMOUNT / monthly[i]["nav"] for i in sip_idx)
        end_nav = monthly[end_i]["nav"]
        end_value = units * end_nav
        wm = end_value / invested
        # Sanity cap: category-aware ceiling (see category_map.max_plausible_wm).
        # Root cause is corrupted NAV data (e.g. NAV stored as 0.1 instead of
        # 10.0), which inflates units ~100x → end_value ~100x → WM ~100x. A
        # flat 50x cutoff (the original guard) is far too loose for Debt —
        # 30-60x WM is just as impossible there as for any category, but was
        # sailing straight under it. Skip the window entirely so it doesn't
        # poison the distribution.
        if wm > wm_cap:
            a = _eomonth(a, 1)
            continue
        out.append(RollingWindow(
            start_date=monthly[sip_idx[0]]["date"].isoformat(),
            end_date=monthly[end_i]["date"].isoformat(),
            total_units=round(units, 4),
            end_nav=round(end_nav, 4),
            end_value=round(end_value, 2),
            total_invested=round(invested, 2),
            wealth_multiple=round(wm, 4),
            good=wm >= min_required,
            _wm_raw=wm,
        ))
        a = _eomonth(a, 1)
    return out


# ─── 4. stats helpers ─────────────────────────────────────────────────────────

def _percentile(values: List[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] * (c - k) + s[c] * (k - f)


def _safe_div(a: float, b: float) -> float:
    if b == 0 or math.isnan(b):
        return 0.0
    return a / b


# ─── 5. per-horizon scoring ───────────────────────────────────────────────────

@dataclass
class HorizonResult:
    horizon: int
    n_windows: int
    min_wm_required: float
    cap: int

    # Distribution
    min_wm: float
    max_wm: float
    mean_wm: float
    median_wm: float
    p10_wm: float
    p25_wm: float
    p75_wm: float
    p90_wm: float
    std_wm: float
    cv: float

    # Outcome rates
    failure_pct: float
    success_pct: float

    # Sub-scores
    fail_score: float
    downside_severity: float   # for Risk Score only: MIN(100, 100×min_wm/P25)
    cv_score: float
    risk_score: float
    adj_risk_score: float
    blended_wm: float
    blended_wm_score: float
    downside_score: float      # for FQS: MIN(cap, 100×P10/min_wm_required)
    consistency_score: float
    es10: float
    es_score: float

    fqs: float

    # ── NEW fields ────────────────────────────────────────────────────────────
    # Alpha Trend: avg(latest 30% WM) / avg(earliest 70% WM)
    # >1 = improving, <1 = deteriorating, None if fewer than 3 windows
    alpha_trend: Optional[float]

    # Stability Score (per-horizon): MAX(0, MIN(100, (1 - std/1.5) × 100))
    # Independent of min_wm_required; purely measures dispersion of WMs
    stability_score: float

    # Stress Score: composite tail-risk score
    # = 0.4×MIN(downside_score×0.85,100) + 0.4×MIN(es_score×0.8,100)
    #   + 0.2×MIN(100,MAX(20,((0.6+(min_wm-0.6)×0.75)−0.6)/(1.5−0.6)×80+20))
    # Where min_wm is the ACTUAL observed minimum WM (not the required threshold)
    stress_score: float

    windows: List[Dict[str, Any]] = field(default_factory=list)


def score_horizon(
    windows: List[RollingWindow],
    horizon: int,
    category: str,
) -> Optional[HorizonResult]:
    if not windows:
        return None

    min_required = min_wm_for(category, horizon)
    if min_required is None:
        min_required = _extrapolate_min_wm(category, horizon)
    cap = cap_for(category)
    equity = is_equity(category)

    wms = [w._wm_raw for w in windows]
    n = len(wms)
    mn, mx = min(wms), max(wms)
    mean = sum(wms) / n
    median = _percentile(wms, 0.5)
    p10 = _percentile(wms, 0.10)
    p25 = _percentile(wms, 0.25)
    p75 = _percentile(wms, 0.75)
    p90 = _percentile(wms, 0.90)
    std = statistics.pstdev(wms) if n >= 2 else 0.0
    cv = _safe_div(std, mean)

    failures = sum(1 for w in wms if w < min_required)
    failure_pct = failures / n
    success_pct = 1.0 - failure_pct

    fail_score = failure_pct * 100.0

    # Downside Severity — Excel ROLLING_SIP_NY!J13: MIN(100, 100×min_wm_req/P25)
    # Used only inside Risk Score calculation.
    downside_severity = min(100.0, 100.0 * min_required / p25) if p25 > 0 else 100.0

    cv_score = min(100.0, cv * 100.0)
    risk_score = 0.40 * fail_score + 0.25 * downside_severity + 0.35 * cv_score
    adj_risk = max(0.0, 100.0 - risk_score)

    blended_wm = 0.6 * median + 0.4 * p10
    blended_wm_score = min(float(cap), _safe_div(blended_wm, min_required) * 100.0)
    downside_score = min(float(cap), _safe_div(p10, min_required) * 100.0)

    # Excel: COUNTIF(WMs > min_required) — strict >
    consistency_count = sum(1 for w in wms if w > min_required)
    consistency_score = (consistency_count / n) * 100.0

    # ES10: AVERAGEIF(WMs < P10) — strict <
    tail = [w for w in wms if w < p10]
    es10 = sum(tail) / len(tail) if tail else mn
    es_score = min(100.0, 100.0 * _safe_div(es10, min_required))

    # ── FQS weights (verified against Excel DISTRIBUTION_METRICS) ────────────
    # Equity:     0.30 Succ + 0.20 AdjRisk + 0.20 BWMS + 0.10 DS + 0.15 Cons + 0.05 ES
    # Non-Equity: 0.25 Succ + 0.35 AdjRisk + 0.15 BWMS + 0.10 DS + 0.10 Cons + 0.05 ES
    if equity:
        w_succ, w_arisk, w_bwm, w_cons, w_dn, w_es = 0.30, 0.20, 0.20, 0.15, 0.10, 0.05
    else:
        w_succ, w_arisk, w_bwm, w_cons, w_dn, w_es = 0.25, 0.35, 0.15, 0.10, 0.10, 0.05

    fqs = (
        w_succ * (success_pct * 100.0)
        + w_arisk * adj_risk
        + w_bwm * (blended_wm_score * 100.0 / cap)
        + w_dn * (downside_score * 100.0 / cap)
        + w_cons * consistency_score
        + w_es * es_score
    )
    fqs = max(0.0, min(100.0, fqs))

    # ── NEW 1: Alpha Trend ────────────────────────────────────────────────────
    # avg(latest 30% WM windows) / avg(earliest 70% WM windows)
    # Windows are already in chronological order (start_date ascending)
    if n >= 3:
        split = max(1, int(n * 0.70))   # first 70% = old, remaining 30% = latest
        avg_old    = sum(wms[:split]) / split
        avg_latest = sum(wms[split:]) / (n - split)
        alpha_trend = round(_safe_div(avg_latest, avg_old), 4)
    else:
        alpha_trend = None

    # ── NEW 2: Stability Score (per-horizon) ──────────────────────────────────
    # MAX(0, MIN(100, (1 - std/1.5) × 100))
    # Measures how tight the WM distribution is; higher = more stable returns
    # Uses the SAME pstdev already computed above
    stability_score = max(0.0, min(100.0, (1.0 - std / 1.5) * 100.0))

    # ── NEW 3: Stress Score ───────────────────────────────────────────────────
    # Excel DISTRIBUTION_METRICS col S formula:
    #   0.4×MIN(H×0.85,100) + 0.4×MIN(Q×0.8,100)
    #   + 0.2×MIN(100, MAX(20, ((0.6+(C-0.6)×0.75)−0.6)/(1.5−0.6)×80+20))
    # H = downside_score (col H in Excel = MIN(P10/min_wm_req×100, cap))
    # Q = es_score
    # C = actual observed minimum WM in the window set (col C in Excel = MIN of G:G)
    # Note: C is mn (actual min), NOT min_required
    min_wm_actual = mn   # actual lowest WM observed across all windows
    stress_component_c = min(
        100.0,
        max(20.0, ((0.6 + (min_wm_actual - 0.6) * 0.75) - 0.6) / (1.5 - 0.6) * 80.0 + 20.0)
    )
    stress_score = round(
        0.4 * min(downside_score * 0.85, 100.0)
        + 0.4 * min(es_score * 0.8, 100.0)
        + 0.2 * stress_component_c,
        2
    )

    return HorizonResult(
        horizon=horizon, n_windows=n,
        min_wm_required=round(min_required, 4), cap=cap,
        min_wm=round(mn, 4), max_wm=round(mx, 4), mean_wm=round(mean, 4),
        median_wm=round(median, 4),
        p10_wm=round(p10, 4), p25_wm=round(p25, 4),
        p75_wm=round(p75, 4), p90_wm=round(p90, 4),
        std_wm=round(std, 4), cv=round(cv, 4),
        failure_pct=round(failure_pct, 4), success_pct=round(success_pct, 4),
        fail_score=round(fail_score, 2),
        downside_severity=round(downside_severity, 2),
        cv_score=round(cv_score, 2),
        risk_score=round(risk_score, 2),
        adj_risk_score=round(adj_risk, 2),
        blended_wm=round(blended_wm, 4),
        blended_wm_score=round(blended_wm_score, 2),
        downside_score=round(downside_score, 2),
        consistency_score=round(consistency_score, 2),
        es10=round(es10, 4),
        es_score=round(es_score, 2),
        fqs=round(fqs, 1),
        alpha_trend=alpha_trend,
        stability_score=round(stability_score, 2),
        stress_score=stress_score,
        windows=[
            {k: v for k, v in w.__dict__.items() if not k.startswith("_")}
            for w in windows
        ],
    )


def _extrapolate_min_wm(category: str, horizon: int) -> float:
    """Extrapolate min WM for horizons not in the anchor table using SIP FV formula."""
    from category_map import CATEGORY_TABLE
    cat = CATEGORY_TABLE.get(category) or CATEGORY_TABLE[DEFAULT_CATEGORY]
    xirr_rate = cat["xirr"]
    m = xirr_rate / 12.0
    N = horizon * 12
    if m == 0:
        return 1.0
    return ((1 + m) ** N - 1) / (N * m)


# ─── 6. headline / aggregate ──────────────────────────────────────────────────

def fqs_tier(score: float) -> str:
    if score >= 90: return "Elite"
    if score >= 80: return "Strong"
    if score >= 70: return "Good"
    if score >= 60: return "Average"
    return "Weak"


def analyze_fund(
    nav_history: List[Dict[str, Any]],
    category: str,
    horizons: Optional[Iterable[int]] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    include_windows: bool = True,
    user_years: Optional[int] = None,
) -> Dict[str, Any]:
    """End-to-end pipeline. Returns a dict ready for the API.

    user_years: the rolling period the user selected. Controls Cycle Robustness
                variant and whether CR is included at all:
                  None or <10  → cycle_robustness = None
                  10–14        → 0.35×S3 + 0.35×S5 + 0.30×S10
                  ≥15          → 0.30×S3 + 0.30×S5 + 0.25×S10 + 0.15×S15

    end_date: upper bound (inclusive) on NAV data used — the Rolling Analysis
              tab's "as of" date. None (default) means "use every NAV up to the
              latest available", unchanged from before this parameter existed.
    """
    if horizons is None:
        horizons = HORIZONS_FULL

    monthly = extract_month_end_navs(nav_history, start_date=start_date, end_date=end_date)
    headline_xirr = compute_full_history_xirr(monthly)

    results: List[HorizonResult] = []
    for h in sorted(set(horizons)):
        if h <= 0 or h > MAX_HORIZON_YEARS:
            continue
        min_required = min_wm_for(category, h) or _extrapolate_min_wm(category, h)
        windows = compute_rolling_windows(monthly, h, min_required, category)
        res = score_horizon(windows, h, category)
        if res is None:
            continue
        if not include_windows:
            res.windows = []
        results.append(res)

    # Headline FQS — anchor on the longest available horizon among 5/10/15
    anchor_pref = [15, 10, 5, 3]
    headline = None
    for pref in anchor_pref:
        for r in results:
            if r.horizon == pref:
                headline = r
                break
        if headline:
            break
    if headline is None and results:
        headline = results[-1]

    by_h = {r.horizon: r for r in results}

    # ── Cycle Robustness — 3 variants based on user_years ────────────────────
    # <10Y:  not computed (not meaningful for short horizons)
    # 10-14Y: 0.35×S3 + 0.35×S5 + 0.30×S10  (image 2 formula)
    # ≥15Y:  0.30×S3 + 0.30×S5 + 0.25×S10 + 0.15×S15  (Excel file formula)
    cycle_robustness: Optional[float] = None
    cycle_variant: Optional[str] = None

    effective_years = user_years if user_years is not None else (
        headline.horizon if headline else None
    )

    if effective_years is not None and effective_years >= 10:
        if effective_years >= 15 and 15 in by_h:
            # Full 4-term formula — requires all of 3Y/5Y/10Y/15Y
            weights_cr = {3: 0.30, 5: 0.30, 10: 0.25, 15: 0.15}
            cycle_variant = "15Y"
        else:
            # 3-term formula for 10–14Y goals
            weights_cr = {3: 0.35, 5: 0.35, 10: 0.30}
            cycle_variant = "10Y"

        cr_num = cr_den = 0.0
        for h, w in weights_cr.items():
            if h in by_h:
                cr_num += w * by_h[h].success_pct * 100.0
                cr_den += w
        if cr_den > 0:
            cycle_robustness = round(cr_num / cr_den, 2)

    # ── Stability Check (cross-horizon) = Median_10Y / Median_5Y ─────────────
    stability_check = None
    if 10 in by_h and 5 in by_h and by_h[5].median_wm:
        stability_check = round(by_h[10].median_wm / by_h[5].median_wm, 3)

    return {
        "category": category,
        "monthly_points": len(monthly),
        "first_month": monthly[0]["date"].isoformat() if monthly else None,
        "last_month": monthly[-1]["date"].isoformat() if monthly else None,
        "start_date_applied": start_date.isoformat() if start_date else None,
        "headline_xirr": headline_xirr,
        "headline_fqs": headline.fqs if headline else None,
        "headline_tier": fqs_tier(headline.fqs) if headline else "Weak",
        "headline_horizon": headline.horizon if headline else None,
        "cycle_robustness": cycle_robustness,
        "cycle_robustness_variant": cycle_variant,  # "10Y", "15Y", or None
        "stability_check": stability_check,
        "horizons": [asdict(r) for r in results],
        "available_horizons": [r.horizon for r in results],
    }