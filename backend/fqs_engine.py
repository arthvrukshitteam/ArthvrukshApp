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
       f. FQS via equity / non-equity weight vector
  6. Aggregate: Cycle Robustness (success% across 3/5/10/15), Stability Check (Med10/Med5)
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
    min_wm_for, cap_for, is_equity, DEFAULT_CATEGORY,
)

SIP_AMOUNT = 10_000  # ₹ per month — matches sheet B1
HORIZONS_FULL = list(range(1, 21))  # 1Y .. 20Y


# ─── 1. month-end extraction ────────────────────────────────────────────────

def extract_month_end_navs(
    nav_history: List[Dict[str, Any]],
    start_date: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """Return one NAV per calendar month — the last NAV of that month.

    Mirrors RAW_NAV column D (D=1 when next row's yyyy-mm differs).
    Optional start_date filters BEFORE month-end extraction (so the first
    month-end falls on/after start_date).
    """
    if not nav_history:
        return []

    filtered = nav_history
    if start_date is not None:
        filtered = [r for r in nav_history if r["date"] >= start_date]

    out: List[Dict[str, Any]] = []
    for i, row in enumerate(filtered):
        d: date = row["date"]
        nxt = filtered[i + 1]["date"] if i + 1 < len(filtered) else None
        is_month_end = (nxt is None) or (nxt.year != d.year or nxt.month != d.month)
        if is_month_end:
            out.append({"date": d, "nav": float(row["nav"])})
    return out


# ─── 2. headline XIRR (SIP_CASHFLOWS) ───────────────────────────────────────

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


# ─── 3. rolling windows (ROLLING_SIP_{N}Y sheet) ────────────────────────────

@dataclass
class RollingWindow:
    """One row of the ROLLING_SIP_{N}Y sheet."""
    start_date: str           # column A — first SIP instalment date
    end_date: str             # column B — redemption date (last instalment)
    total_units: float        # column C
    end_nav: float            # column D
    end_value: float          # column E
    total_invested: float     # column F
    wealth_multiple: float    # column G (rounded for display only)
    good: bool                # column H — WM ≥ min required
    _wm_raw: float = 0.0      # full-precision WM used for stats (not serialized to JSON)


def compute_rolling_windows(
    monthly: List[Dict[str, Any]],
    horizon_years: int,
    min_required: float,
) -> List[RollingWindow]:
    """Excel-faithful rolling SIP windows — date-based filtering matching
    the ROLLING_SIP_NY!A3..G(n) formulas exactly:

      A_1 = EOMONTH(first_month_end, 0) + 1 day
      A_k = EOMONTH(A_{k-1}, 1)        for k >= 2
      B_k = EDATE(A_k, 12*H)

    For each window k:
      SIPs    = month-end NAVs with date d satisfying  A_k <= d < B_k
      End NAV = NAV at the latest month-end date <= B_k   (Excel XLOOKUP -1)

    Termination: while EOMONTH(B_k, 0) <= EOMONTH(last_month_end, 0).
    """
    H = horizon_years
    N = H * 12
    if len(monthly) < N + 1:
        return []

    invested = SIP_AMOUNT * N
    last_eom = _eomonth(monthly[-1]["date"], 0)
    dates = [m["date"] for m in monthly]

    a = _eomonth(monthly[0]["date"], 0) + timedelta(days=1)  # A_1
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
            break
        end_i = end_candidates[-1]
        units = sum(SIP_AMOUNT / monthly[i]["nav"] for i in sip_idx)
        end_nav = monthly[end_i]["nav"]
        end_value = units * end_nav
        wm = end_value / invested
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
        a = _eomonth(a, 1)  # advance to next month
    return out


# ─── 4. stats helpers ───────────────────────────────────────────────────────

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


# ─── 5. per-horizon scoring (DISTRIBUTION_METRICS row) ──────────────────────

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
    downside_severity: float
    cv_score: float
    risk_score: float
    adj_risk_score: float
    blended_wm: float
    blended_wm_score: float
    downside_score: float
    consistency_score: float
    es10: float
    es_score: float

    fqs: float
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
        # If category has no benchmark for this horizon, extrapolate from XIRR
        # using compound growth: WM ≈ (1+r)^n × (some discount for SIP timing).
        # Simpler: take category's 15Y benchmark and pro-rate.
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
    downside_severity = min(100.0, 100.0 * min_required / p25) if p25 > 0 else 100.0
    cv_score = min(100.0, cv * 100.0)
    risk_score = 0.40 * fail_score + 0.25 * downside_severity + 0.35 * cv_score
    adj_risk = 100.0 - risk_score

    blended_wm = 0.6 * median + 0.4 * p10
    blended_wm_score = min(cap, _safe_div(blended_wm, min_required) * 100.0)
    downside_score = min(cap, _safe_div(p10, min_required) * 100.0)
    # Excel ROLLING_SIP_NY!J14 uses strict ">" against min_required.
    consistency_count = sum(1 for w in wms if w > min_required)
    consistency_score = (consistency_count / n) * 100.0

    # Excel DISTRIBUTION_METRICS!P6 = AVERAGEIF(g_range, "<" & P10) — strict.
    tail = [w for w in wms if w < p10]
    es10 = sum(tail) / len(tail) if tail else mn
    es_score = min(100.0, 100.0 * _safe_div(es10, min_required))

    if equity:
        w_succ, w_arisk, w_bwm, w_cons, w_dn, w_es = 0.35, 0.25, 0.20, 0.00, 0.10, 0.10
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
        windows=[
            {k: v for k, v in w.__dict__.items() if not k.startswith("_")}
            for w in windows
        ],
    )


def _extrapolate_min_wm(category: str, horizon: int) -> float:
    """Linear-interp / extrapolate Min WM between the known anchors (3/5/10/15Y)."""
    from category_map import CATEGORY_TABLE
    cat = CATEGORY_TABLE.get(category) or CATEGORY_TABLE[DEFAULT_CATEGORY]
    table = cat["wm"]
    xirr = cat["xirr"]

    # Use compound model: WM(n) ≈ ((1+r)^n - 1) / (n*r) for an immediate annuity ratio,
    # but for SIP at month-end we already have explicit anchors. Simplest: derive from XIRR
    # using a closed-form expression for SIP FV/Invested:
    # WM = ((1+m)^N - 1) / (N * m)  where m = monthly rate, N = months.
    m = xirr / 12.0
    N = horizon * 12
    if m == 0:
        return 1.0
    return ((1 + m) ** N - 1) / (N * m)


# ─── 6. headline / aggregate ────────────────────────────────────────────────

def fqs_tier(score: float) -> str:
    if score >= 90:
        return "Elite"
    if score >= 80:
        return "Strong"
    if score >= 70:
        return "Good"
    if score >= 60:
        return "Average"
    return "Weak"


def analyze_fund(
    nav_history: List[Dict[str, Any]],
    category: str,
    horizons: Optional[Iterable[int]] = None,
    start_date: Optional[date] = None,
    include_windows: bool = True,
) -> Dict[str, Any]:
    """End-to-end pipeline. Returns a dict ready for the API.

    `horizons` defaults to 1..20. Horizons without enough data are silently skipped.
    `start_date` (inclusive) filters NAV history before everything else.
    `include_windows=False` returns horizons without the per-window detail
    (used by daily refresh / list endpoints to keep payloads small).
    """
    if horizons is None:
        horizons = HORIZONS_FULL

    monthly = extract_month_end_navs(nav_history, start_date=start_date)
    headline_xirr = compute_full_history_xirr(monthly)

    results: List[HorizonResult] = []
    for h in sorted(set(horizons)):
        if h <= 0 or h > 20:
            continue
        min_required = min_wm_for(category, h) or _extrapolate_min_wm(category, h)
        windows = compute_rolling_windows(monthly, h, min_required)
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

    # Aggregate: Cycle Robustness (weighted Success% across 3/5/10/15)
    by_h = {r.horizon: r for r in results}
    cycle_w = {3: 0.30, 5: 0.30, 10: 0.25, 15: 0.15}
    cycle_num, cycle_den = 0.0, 0.0
    for h, w in cycle_w.items():
        if h in by_h:
            cycle_num += w * by_h[h].success_pct * 100.0
            cycle_den += w
    cycle_robustness = round(cycle_num / cycle_den, 2) if cycle_den else None

    # Stability Check = Median WM (10Y) / Median WM (5Y)
    stability = None
    if 10 in by_h and 5 in by_h and by_h[5].median_wm:
        stability = round(by_h[10].median_wm / by_h[5].median_wm, 3)

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
        "stability_check": stability,
        "horizons": [asdict(r) for r in results],
        "available_horizons": [r.horizon for r in results],
    }
