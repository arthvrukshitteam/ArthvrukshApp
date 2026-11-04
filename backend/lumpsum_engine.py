"""
Lumpsum FQS Engine
==================
Mirrors fqs_engine.py for lumpsum (one-time investment) mode.

The ONLY difference from the SIP model is how WM is calculated:

  SIP:     Monthly ₹10,000 investments. Each month buys units at that month's NAV.
           WM = (total_units_accumulated × final_NAV) / (N × ₹10,000)

  Lumpsum: Single ₹10,000 investment. Units bought once at start NAV.
           WM = ending_nav / starting_nav

All other metrics (FQS weights, consistency, CV, alpha_trend, stress_score,
cycle robustness, headline FQS) are IDENTICAL to fqs_engine.py.

Supports all horizons 1–50Y (same as SIP). The min_wm benchmark uses the
compound formula (1 + xirr_req)^H, appropriate for a single lumpsum investment.
"""

from __future__ import annotations

import asyncio

import statistics
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from category_map import (
    classify_fund, get_lumpsum_min_wm, get_lumpsum_cap, is_equity_category, max_plausible_wm,
)

# ── Constants ──────────────────────────────────────────────────────────────────

# All horizons — same range as SIP model (1–50Y)
ALL_HORIZONS = list(range(1, 51))

# FQS component weights for equity — IDENTICAL to fqs_engine.py
EQUITY_WEIGHTS = {
    "success":     0.30,
    "adj_risk":    0.20,
    "blended_wm":  0.20,
    "downside":    0.10,
    "consistency": 0.15,
    "es":          0.05,
}

# FQS component weights for non-equity — IDENTICAL to fqs_engine.py
NON_EQUITY_WEIGHTS = {
    "success":     0.25,
    "adj_risk":    0.35,
    "blended_wm":  0.15,
    "downside":    0.10,
    "consistency": 0.10,
    "es":          0.05,
}

FQS_RATINGS = [
    (90, "Elite"),
    (80, "Strong"),
    (70, "Good"),
    (60, "Average"),
    (0,  "Weak"),
]

# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class LumpsumWindow:
    start_date: str
    end_date: str
    starting_nav: float
    ending_nav: float
    total_invested: float    # always 10_000
    final_value: float
    wealth_multiple: float   # ending_nav / starting_nav
    good: bool               # WM >= min_wm_required


@dataclass
class LumpsumHorizonStats:
    horizon: int
    n_windows: int
    min_wm_required: float
    cap: float

    # WM distribution
    min_wm: float
    max_wm: float
    mean_wm: float
    median_wm: float
    p10_wm: float
    p25_wm: float
    p75_wm: float
    p90_wm: float
    std_dev: float
    cv: float                 # coefficient of variation = std / mean

    # Success / failure
    failure_pct: float
    success_pct: float
    failure_score: float
    consistency_score: float  # strict >: count(WM > min_wm) / n × 100

    # Risk
    downside_sev_score: float
    cv_score: float
    risk_score: float
    adj_risk_score: float

    # Wealth / quality
    blended_wm: float
    blended_wm_score: float
    downside_score: float
    es10: float
    es_score: float

    # Per-horizon FQS
    fqs: float
    fqs_rating: str

    # Trend / tail metrics — IDENTICAL formulas to fqs_engine.py
    alpha_trend: Optional[float]   # avg(latest 30% WMs) / avg(earliest 70% WMs)
    stress_score: float            # composite tail-risk score

    windows: list[LumpsumWindow] = field(default_factory=list)


@dataclass
class LumpsumAnalysis:
    scheme_code: int
    scheme_name: str
    category: str
    is_equity: bool
    monthly_points: int
    start_date_applied: Optional[str]
    available_horizons: list[int]
    horizons: list[LumpsumHorizonStats]

    # Headline (longest-anchor, same strategy as SIP: 15 > 10 > 5 > 3)
    headline_fqs: Optional[float]
    headline_horizon: int
    headline_fqs_rating: Optional[str]

    # Cycle Robustness — same user_years-dependent formula as SIP
    cycle_robustness: Optional[float]
    cycle_variant: Optional[str]   # "10Y" | "15Y" | None

    # Category parameters
    min_xirr_req: float
    cap: float


# ── Helpers ────────────────────────────────────────────────────────────────────

def _percentile(sorted_vals: list[float], p: float) -> float:
    """PERCENTILE.INC — matches Excel's PERCENTILE.INC exactly."""
    n = len(sorted_vals)
    if n == 0:
        return float("nan")
    if n == 1:
        return sorted_vals[0]
    rank = p * (n - 1)
    lo = int(rank)
    hi = lo + 1
    if hi >= n:
        return sorted_vals[-1]
    frac = rank - lo
    return sorted_vals[lo] + frac * (sorted_vals[hi] - sorted_vals[lo])


def _stdev_p(vals: list[float]) -> float:
    """Population standard deviation — matches Excel's STDEV.P."""
    n = len(vals)
    if n < 2:
        return 0.0
    mean = sum(vals) / n
    return (sum((x - mean) ** 2 for x in vals) / n) ** 0.5


def _safe_div(a: float, b: float) -> float:
    return a / b if b != 0 else 0.0


def _lookup_nav(
    nav_dates: list[date],
    nav_values: list[float],
    target: date,
) -> Optional[float]:
    """Return NAV for target date; fall back to most recent prior month-end."""
    idx = bisect_right(nav_dates, target) - 1
    if idx < 0:
        return None
    return nav_values[idx]


def _eomonth(d: date) -> date:
    import datetime as dt
    if d.month == 12:
        return date(d.year + 1, 1, 1) - dt.timedelta(days=1)
    return date(d.year, d.month + 1, 1) - dt.timedelta(days=1)


def _edate(d: date, months: int) -> date:
    import calendar
    m = d.month - 1 + months
    year = d.year + m // 12
    month = m % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(d.day, last_day))


def _fqs_rating(fqs: float) -> str:
    for threshold, label in FQS_RATINGS:
        if fqs >= threshold:
            return label
    return "Weak"


# ── Rolling window builder ─────────────────────────────────────────────────────

def compute_rolling_lumpsum_windows(
    nav_dates: list[date],
    nav_values: list[float],
    horizon_years: int,
    min_wm: float,
    category: str = "",
) -> list[LumpsumWindow]:
    """
    Build rolling lumpsum windows for a given horizon.

    Invest ₹10,000 at the start of each rolling period; redeem after
    exactly horizon_years × 12 months.

    WM = ending_nav / starting_nav
       = (₹10,000 / starting_nav × ending_nav) / ₹10,000

    This is the ONLY place lumpsum differs from SIP: SIP accumulates N
    monthly investments; here we invest once and track the NAV ratio.
    Windows are appended in chronological order (earliest first).

    A window whose WM exceeds category_map.max_plausible_wm() is dropped as
    a NAV data artifact (rebase/face-value/decimal glitch in the upstream
    feed) rather than treated as real performance — this previously had NO
    sanity check at all, unlike the SIP engine's flat 50x guard, which let
    Debt-category funds show impossible 30-60x lumpsum multiples.
    """
    if not nav_dates:
        return []

    import datetime as dt

    wm_cap = max_plausible_wm(category, horizon_years)
    last_me = _eomonth(nav_dates[-1])
    first_me = _eomonth(nav_dates[0])
    start = first_me + dt.timedelta(days=1)

    windows: list[LumpsumWindow] = []
    lumpsum = 10_000.0

    while True:
        end = _edate(start, 12 * horizon_years)
        if _eomonth(end) > last_me:
            break

        s_nav = _lookup_nav(nav_dates, nav_values, start)
        e_nav = _lookup_nav(nav_dates, nav_values, end)

        if s_nav is None or e_nav is None or s_nav <= 0:
            start = _eomonth(start) + dt.timedelta(days=1)
            continue

        wm = e_nav / s_nav
        if wm > wm_cap:
            start = _eomonth(start) + dt.timedelta(days=1)
            continue
        final_value = (lumpsum / s_nav) * e_nav

        windows.append(LumpsumWindow(
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            starting_nav=round(s_nav, 4),
            ending_nav=round(e_nav, 4),
            total_invested=lumpsum,
            final_value=round(final_value, 4),
            wealth_multiple=round(wm, 6),
            good=wm >= min_wm,
        ))

        # Advance one month-end forward
        start = _eomonth(_edate(start, 1))

    return windows


# ── Per-horizon stats ──────────────────────────────────────────────────────────

def compute_horizon_stats(
    windows: list[LumpsumWindow],
    horizon: int,
    min_wm: float,
    cap: float,
) -> Optional[LumpsumHorizonStats]:
    """
    Compute all per-horizon statistics.
    Formulas IDENTICAL to fqs_engine.py's score_horizon().
    """
    if not windows:
        return None

    n = len(windows)
    # Chronological WMs for alpha_trend (windows are already in chronological order)
    wms_chrono = [w.wealth_multiple for w in windows]
    # Sorted WMs for percentile / distribution stats
    wms = sorted(wms_chrono)

    mean_wm   = statistics.mean(wms)
    median_wm = statistics.median(wms)
    std_dev   = _stdev_p(wms)
    cv        = _safe_div(std_dev, mean_wm)

    p10 = _percentile(wms, 0.10)
    p25 = _percentile(wms, 0.25)
    p75 = _percentile(wms, 0.75)
    p90 = _percentile(wms, 0.90)

    # Failure / Success
    failure_count = sum(1 for w in wms if w < min_wm)
    failure_pct   = failure_count / n
    success_pct   = 1.0 - failure_pct
    failure_score = failure_pct * 100

    # Consistency — strict > (IDENTICAL to SIP: COUNTIF(WMs > min_wm))
    consistency_count = sum(1 for w in wms if w > min_wm)
    consistency_score = (consistency_count / n) * 100.0

    # Risk
    downside_sev_score = min(100.0, 100.0 * min_wm / p25) if p25 > 0 else 100.0
    cv_score    = min(100.0, cv * 100.0)
    risk_score  = 0.40 * failure_score + 0.25 * downside_sev_score + 0.35 * cv_score
    adj_risk_score = 100.0 - risk_score

    # Wealth quality
    blended_wm       = 0.6 * median_wm + 0.4 * p10
    blended_wm_score = min(_safe_div(blended_wm, min_wm) * 100.0, cap)
    downside_score   = min(_safe_div(p10, min_wm) * 100.0, cap)

    # ES10 — average of WMs strictly below P10 (IDENTICAL to SIP)
    below_p10 = [w for w in wms if w < p10]
    es10      = statistics.mean(below_p10) if below_p10 else min(wms)
    es_score  = min(100.0, 100.0 * _safe_div(es10, min_wm))

    # ── Alpha Trend — IDENTICAL to fqs_engine.py ────────────────────────────
    # avg(latest 30% chronological windows) / avg(earliest 70%)
    if n >= 3:
        split      = max(1, int(n * 0.70))
        avg_old    = sum(wms_chrono[:split]) / split
        avg_latest = sum(wms_chrono[split:]) / (n - split)
        alpha_trend: Optional[float] = round(_safe_div(avg_latest, avg_old), 4)
    else:
        alpha_trend = None

    # ── Stress Score — IDENTICAL to fqs_engine.py ───────────────────────────
    min_wm_actual = min(wms)
    stress_c = min(
        100.0,
        max(20.0,
            ((0.6 + (min_wm_actual - 0.6) * 0.75) - 0.6) / (1.5 - 0.6) * 80.0 + 20.0)
    )
    stress_score = round(
        0.4 * min(downside_score * 0.85, 100.0)
        + 0.4 * min(es_score * 0.8, 100.0)
        + 0.2 * stress_c,
        2,
    )

    return LumpsumHorizonStats(
        horizon=horizon,
        n_windows=n,
        min_wm_required=round(min_wm, 6),
        cap=cap,
        min_wm=round(min(wms), 6),
        max_wm=round(max(wms), 6),
        mean_wm=round(mean_wm, 6),
        median_wm=round(median_wm, 6),
        p10_wm=round(p10, 6),
        p25_wm=round(p25, 6),
        p75_wm=round(p75, 6),
        p90_wm=round(p90, 6),
        std_dev=round(std_dev, 6),
        cv=round(cv, 6),
        failure_pct=round(failure_pct, 6),
        success_pct=round(success_pct, 6),
        failure_score=round(failure_score, 4),
        consistency_score=round(consistency_score, 4),
        downside_sev_score=round(downside_sev_score, 4),
        cv_score=round(cv_score, 4),
        risk_score=round(risk_score, 4),
        adj_risk_score=round(adj_risk_score, 4),
        blended_wm=round(blended_wm, 6),
        blended_wm_score=round(blended_wm_score, 4),
        downside_score=round(downside_score, 4),
        es10=round(es10, 6),
        es_score=round(es_score, 4),
        fqs=0.0,
        fqs_rating="",
        alpha_trend=alpha_trend,
        stress_score=stress_score,
        windows=windows,
    )


def compute_horizon_fqs(stats: LumpsumHorizonStats, is_equity: bool) -> float:
    """FQS formula — IDENTICAL weights to fqs_engine.py."""
    w   = EQUITY_WEIGHTS if is_equity else NON_EQUITY_WEIGHTS
    cap = stats.cap

    bwm_norm = _safe_div(stats.blended_wm_score, cap) * 100
    ds_norm  = _safe_div(stats.downside_score, cap) * 100

    fqs = (
        w["success"]     * stats.success_pct * 100
        + w["adj_risk"]  * stats.adj_risk_score
        + w["blended_wm"] * bwm_norm
        + w["downside"]  * ds_norm
        + w["consistency"] * stats.consistency_score
        + w["es"]        * stats.es_score
    )
    return round(max(0.0, min(100.0, fqs)), 4)


# ── Month-end NAV extraction ───────────────────────────────────────────────────

def extract_month_end_navs(
    raw_navs: list[dict],
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> tuple[list[date], list[float]]:
    """Convert raw NAV list → (sorted dates, values) keeping only month-end NAVs.

    `end_date` (inclusive) mirrors `start_date` — the Rolling Analysis tab's
    "as of" upper bound. See fqs_engine.extract_month_end_navs for the SIP-side twin.
    """
    from collections import defaultdict
    import datetime as _dt

    monthly: dict[tuple[int, int], tuple[date, float]] = defaultdict(lambda: (date.min, 0.0))

    for item in raw_navs:
        try:
            d_str = item["date"]
            try:
                d = date.fromisoformat(d_str)
            except ValueError:
                d = _dt.datetime.strptime(d_str, "%d-%b-%Y").date()
            nav = float(item["nav"])
        except Exception:
            continue

        if start_date and d < start_date:
            continue
        if end_date and d > end_date:
            continue

        key = (d.year, d.month)
        if d > monthly[key][0]:
            monthly[key] = (d, nav)

    if not monthly:
        return [], []

    pairs = sorted(monthly.values(), key=lambda x: x[0])
    return [p[0] for p in pairs], [p[1] for p in pairs]


# ── Main entry point ───────────────────────────────────────────────────────────

async def analyze_fund_lumpsum(
    scheme_code: int,
    scheme_name: str,
    raw_navs: list[dict],
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    user_years: Optional[int] = None,
    scheme_category: str = "",
) -> Optional[LumpsumAnalysis]:
    """Async facade over the (entirely CPU-bound) implementation below.

    This function has always been `async def` but contains no awaits at all —
    which meant `await analyze_fund_lumpsum(...)` blocked the event loop for
    the full duration of the computation, exactly like a synchronous call. On
    the nightly full-universe batch that is hundreds of milliseconds per fund
    across ~37 000 funds, and Render's health check (5s timeout) fails because
    /api/health cannot get a turn; on the request path it stalls every other
    in-flight request for the same reason.

    Handing the work to a worker thread does not make it faster — the GIL
    still serialises it — but the interpreter switches back to the event loop
    every few milliseconds, so the service stays responsive while it runs.

    The signature is unchanged and every existing caller still awaits this
    name; the real implementation is _analyze_fund_lumpsum_sync.
    """
    return await asyncio.to_thread(
        _analyze_fund_lumpsum_sync,
        scheme_code, scheme_name, raw_navs,
        start_date, end_date, user_years, scheme_category,
    )


def _analyze_fund_lumpsum_sync(
    scheme_code: int,
    scheme_name: str,
    raw_navs: list[dict],
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    user_years: Optional[int] = None,
    scheme_category: str = "",
) -> Optional[LumpsumAnalysis]:
    """
    Full lumpsum analysis for a fund. Mirrors analyze_fund() in fqs_engine.py
    except WM is computed as ending_nav / starting_nav (single investment).

    user_years: controls Cycle Robustness variant (same logic as SIP):
        None or < 10 → cycle_robustness = None
        10–14        → 0.35×S3 + 0.35×S5 + 0.30×S10
        ≥ 15         → 0.30×S3 + 0.30×S5 + 0.25×S10 + 0.15×S15

    end_date: upper bound (inclusive) on NAV data used — the Rolling Analysis
        tab's "as of" date. None (default) preserves prior behaviour.

    scheme_category: AMFI canonical category string (e.g. "Equity Scheme - Small Cap Fund").
        Passed through to classify_fund() so Pass 1 (AMFI canonical) is used instead of
        falling back to name-keyword matching. Always provide this from fund_nav_meta.
    """
    nav_dates, nav_values = extract_month_end_navs(raw_navs, start_date, end_date)

    if len(nav_dates) < 13:   # need at least 1Y of monthly data
        return None

    category = classify_fund(scheme_name, scheme_category)
    equity   = is_equity_category(category)
    cap      = get_lumpsum_cap(category)

    from category_map import LUMPSUM_XIRR_REQ, DEFAULT_CATEGORY
    xirr_req = LUMPSUM_XIRR_REQ.get(category, LUMPSUM_XIRR_REQ[DEFAULT_CATEGORY])

    computed_horizons: list[LumpsumHorizonStats] = []
    available: list[int] = []

    for h in ALL_HORIZONS:
        min_wm_h = get_lumpsum_min_wm(category, h)
        windows  = compute_rolling_lumpsum_windows(nav_dates, nav_values, h, min_wm_h, category)
        if not windows:
            continue

        stats = compute_horizon_stats(windows, h, min_wm_h, cap)
        if stats is None:
            continue

        stats.fqs        = compute_horizon_fqs(stats, equity)
        stats.fqs_rating = _fqs_rating(stats.fqs)

        computed_horizons.append(stats)
        available.append(h)

    if not computed_horizons:
        return None

    by_h = {s.horizon: s for s in computed_horizons}

    # Headline FQS — anchor on longest available (15 > 10 > 5 > 3), same as SIP
    anchor_pref = [15, 10, 5, 3]
    headline: Optional[LumpsumHorizonStats] = None
    for pref in anchor_pref:
        if pref in by_h:
            headline = by_h[pref]
            break
    if headline is None:
        headline = computed_horizons[-1]

    # Cycle Robustness — IDENTICAL logic to fqs_engine.py
    cycle_robustness: Optional[float] = None
    cycle_variant: Optional[str] = None

    effective_years = user_years if user_years is not None else headline.horizon

    if effective_years >= 15 and 15 in by_h:
        weights_cr  = {3: 0.30, 5: 0.30, 10: 0.25, 15: 0.15}
        cycle_variant = "15Y"
    elif effective_years >= 10:
        weights_cr  = {3: 0.35, 5: 0.35, 10: 0.30}
        cycle_variant = "10Y"
    else:
        weights_cr = {}

    if weights_cr:
        cr_num = cr_den = 0.0
        for h, w in weights_cr.items():
            if h in by_h:
                cr_num += w * by_h[h].success_pct * 100.0
                cr_den += w
        if cr_den > 0:
            cycle_robustness = round(cr_num / cr_den, 2)

    return LumpsumAnalysis(
        scheme_code=scheme_code,
        scheme_name=scheme_name,
        category=category,
        is_equity=equity,
        monthly_points=len(nav_dates),
        start_date_applied=start_date.isoformat() if start_date else None,
        available_horizons=available,
        horizons=computed_horizons,
        headline_fqs=round(headline.fqs, 4) if headline.fqs is not None else None,
        headline_horizon=headline.horizon,
        headline_fqs_rating=_fqs_rating(headline.fqs) if headline.fqs is not None else None,
        cycle_robustness=cycle_robustness,
        cycle_variant=cycle_variant,
        min_xirr_req=xirr_req,
        cap=cap,
    )
