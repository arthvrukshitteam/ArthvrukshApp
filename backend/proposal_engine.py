"""Portfolio Proposal engine.

Blends multiple funds' *already-computed* SIP or Lumpsum analysis (produced
by `fqs_engine.py` / `lumpsum_engine.py` via `server.py`'s `_build_analysis()`
/ `_build_lumpsum_analysis()`) into a weighted portfolio projection at a
SINGLE horizon — the advisor's own tenure — matching one of the sheets of the
"ArthVruksh MF Proposal" Excel template (SIP or Lumpsum, whichever mode the
advisor is in; never both in the same proposal).

IMPORTANT: this module contains no fund-analysis / scoring logic of its own.
It only reads {fqs, success_pct, median_wm, p10_wm} at one horizon out of
analysis dicts that already exist, and blends them by allocation %. All
NAV fetching, classification, and FQS math stays in fqs_engine.py /
lumpsum_engine.py / category_map.py exactly as before.

Formulas (verified cell-for-cell against the uploaded template — see
backend/tests/test_proposal_engine.py):

  Per-fund row (SIP or Lumpsum — same shape, different WM source):
    xirr_median = wm_to_xirr(median_wm, horizon)
    xirr_p10    = wm_to_xirr(p10_wm, horizon)

  Portfolio aggregate at the chosen horizon H (N funds, allocation a_i, investment amt):
    SIP:
      capital_invested   = amount * H * 12
      expected_wealth_x  = sum_i( a_i * amount * wm_x_i * H * 12 )   for x in {median, p10}
    Lumpsum:
      capital_invested   = amount
      expected_wealth_x  = sum_i( a_i * amount * wm_x_i )
    (both modes)
      blended_wm_x   = expected_wealth_x / capital_invested
      blended_xirr_x = wm_to_xirr(blended_wm_x, H)

  Short-history funds (Additional Recommendations) at a longer tenure:
    An Additional Recommendation qualifies at its own longest robust window
    (>=5Y, >=10 rolling windows) — say 7Y — but the advisor's tenure may be
    10Y. Previously such a fund had no row at horizon 10, so `available` was
    False, it was dropped from the blend entirely, and its allocation silently
    contributed Rs. 0 of wealth while still counting toward capital invested —
    understating the whole portfolio. Now:

      metrics_horizon h*  = longest robust window <= H   (7 in the example)
      DISPLAYED  fqs / success_pct / median_wm / p10_wm / xirr_median /
                 xirr_p10  ->  all read at h*, and ONLY at h*  (flagged with
                 `extrapolated: True` so the UI/PDF/XLSX can asterisk them)
      PROJECTED  proj_wm_median = xirr_to_wm(mode, xirr_median@h*, H)
                 proj_wm_p10    = xirr_to_wm(mode, xirr_p10@h*,    H)

    i.e. the 7-year XIRR is held constant and compounded out to the 10-year
    tenure, for both SIP and Lumpsum. The blend below uses proj_wm_*, so the
    fund contributes real wealth at H while never displaying a parameter it
    doesn't actually have data for. For a fund with full history at H,
    h* == H and proj_wm_* == median_wm/p10_wm, so nothing changes.

  SWP (independent — a withdrawal-phase amortisation table):
    withdrawal_with_inflation = monthly_withdrawal * (1 + inflation) / 12 + monthly_withdrawal
    month k: withdrawal = -withdrawal_with_inflation (constant every month)
             amount_after_withdrawal = opening_principal + withdrawal
             growth = amount_after_withdrawal * annual_return / 12
             closing_balance = amount_after_withdrawal + growth
             next month's opening_principal = this month's closing_balance

BUG FIXED (2026-07): `default_swp_inputs()` used to always read the LONGEST
of several horizons for its corpus/return defaults, with no check that the
horizon actually had data. If every selected fund lacked history at that
horizon, `expected_wealth_median` was silently 0 and `blended_xirr_median`
was silently `None -> 0.0`, producing a Systematic Withdrawal Plan with a
Rs. 0 corpus and 0% return — the schedule just bled money every month with
no warning. Now that a proposal has exactly one horizon (the advisor's own
tenure), and `default_swp_inputs()` returns `None` (not 0) when that horizon
has no valid data, so the caller (`proposal_routes.py`) can refuse to build
a misleading proposal instead of shipping one.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional

from additional_rec import select_robust_horizon
from xirr_utils import wm_to_xirr, xirr_to_wm
from category_map import min_wm_for, get_lumpsum_min_wm
from fqs_engine import _extrapolate_min_wm

MAX_FUNDS = 10
ALLOCATION_TOLERANCE = 0.01  # allocations must sum to 100% +/- 1%


def _required_min_wm(category: str, horizon: int, mode: str) -> Optional[float]:
    """The minimum Wealth Multiple bar for (category, horizon, mode) — the
    same category_map lookup fqs_engine.score_horizon() / lumpsum_engine's
    compute_horizon_stats() use to score every window, so it never depends on
    a specific fund's own NAV data.

    Recomputed here instead of trusted from the fund's analysis dict because
    fqs_refresh.py's _build_fqs_by_horizon() (shared by the plain,
    Since-2010 and Separate Management Periods precomputed variants) never
    stores min_wm_required at all. The PLAIN path happened to still work
    because proposal_routes.py's non-toggle path calls analyze_fund() fresh,
    whose output does include it — but the moment a fund's numbers came
    through the since_2010/separate_management variant path instead, this
    silently went missing and Benchmark XIRR rendered blank on the Review &
    Update screen with no indication why. Since it's fully derivable from
    (category, horizon, mode) alone, always computing it here removes the
    dependency on whichever upstream shape happened to carry it.
    """
    if mode == "lumpsum":
        return get_lumpsum_min_wm(category, horizon)
    required = min_wm_for(category, horizon)
    if required is None:
        required = _extrapolate_min_wm(category, horizon)
    return required


# ─── Data model ───────────────────────────────────────────────────────────────

@dataclass
class FundRow:
    scheme_code: int
    scheme_name: str
    category: str
    allocation_pct: float           # 0..1
    fqs: Optional[float]
    success_pct: Optional[float]    # 0..1
    median_wm: Optional[float]
    xirr_median: Optional[float]
    p10_wm: Optional[float]
    xirr_p10: Optional[float]
    available: bool                 # False if this fund had no usable window at all
    # ── Short-history (Additional Recommendation) support ────────────────────
    # metrics_horizon: the window ALL the displayed figures above come from.
    # Equals the portfolio horizon for a normal fund; smaller (e.g. 7 in a 10Y
    # proposal) for an Additional Recommendation. `extrapolated` is True only
    # in that second case — that's the flag the UI/PDF/XLSX asterisk on.
    metrics_horizon: Optional[int] = None
    extrapolated: bool = False
    # Wealth multiples AT THE PORTFOLIO HORIZON — what the blend actually uses.
    # Same as median_wm/p10_wm when not extrapolated.
    proj_wm_median: Optional[float] = None
    proj_wm_p10: Optional[float] = None
    # ── Consistency / benchmark (read at metrics_horizon, same as fqs/median_wm) ──
    # Coefficient of Variation (sigma / mean) of this fund's WM outcomes — lower
    # is more consistent. Same figure the fund-detail screen's CONSISTENCY
    # section shows (fund/[code].tsx, key "cv").
    cv: Optional[float] = None
    # Minimum WM this fund's category should produce at metrics_horizon — the
    # category benchmark (same figure as fund-detail's "Min WM Required").
    min_wm_required: Optional[float] = None
    # min_wm_required converted to an annualised XIRR via the same wm_to_xirr
    # used for xirr_median/xirr_p10 — "what XIRR would just meet the benchmark".
    benchmark_xirr: Optional[float] = None
    # How many rolling windows this fund's median/P10/success-% were computed
    # from, at metrics_horizon — a data-sufficiency signal (e.g. a fund with
    # only 8 windows is a thinner sample than one with 80) shown alongside the
    # figures it backs, same as fund-detail's "N windows" label.
    n_windows: Optional[int] = None


@dataclass
class PortfolioAggregate:
    horizon: int
    capital_invested: float
    expected_wealth_median: float
    expected_wealth_p10: float
    blended_wm_median: float
    blended_wm_p10: float
    blended_xirr_median: Optional[float]
    blended_xirr_p10: Optional[float]
    excluded_funds: List[str] = field(default_factory=list)  # scheme_names with no usable window at all
    # scheme_names shown at a SHORTER window than the portfolio horizon, with
    # their wealth extrapolated from that window's XIRR (the asterisked rows).
    extrapolated_funds: List[str] = field(default_factory=list)
    # Portfolio-level consistency/benchmark — allocation-weighted average of
    # each included fund's cv / min_wm_required (same weighting the wealth
    # blend above uses), min_wm_required then converted to an XIRR at `horizon`.
    blended_cv: Optional[float] = None
    benchmark_wm: Optional[float] = None
    benchmark_xirr: Optional[float] = None


@dataclass
class ModeProjection:
    mode: str                        # "sip" | "lumpsum"
    investment_amount: float         # monthly amount for sip, total for lumpsum (rupees)
    horizon: int
    funds: List[FundRow]
    aggregate: PortfolioAggregate


@dataclass
class SwpMonthRow:
    month: int
    opening_principal: float
    withdrawal: float
    principal_after_withdrawal: float
    growth: float
    closing_balance: float


@dataclass
class SwpProjection:
    corpus: float
    annual_return: float
    inflation: float
    monthly_withdrawal: float
    withdrawal_with_inflation: float
    years: int
    rows: List[SwpMonthRow]
    ending_balance: float
    corpus_auto_derived: bool
    annual_return_auto_derived: bool


# ─── Per-fund extraction ──────────────────────────────────────────────────────

def extract_fund_metrics(fund_analysis: Dict[str, Any], horizon: int) -> Dict[str, Any]:
    """Pull {fqs, success_pct, median_wm, p10_wm} out of a SIP or Lumpsum
    analysis dict, at the portfolio `horizon` when the fund has data there.

    FALLBACK (Additional Recommendation funds): when the fund has no row at
    `horizon` — its history is shorter than the advisor's tenure — drop back to
    its LONGEST statistically robust window <= horizon, using the exact same
    rule (>=5Y, >=10 rolling windows) that qualified it as an Additional
    Recommendation in the first place. So a 7Y fund in a 10Y proposal reports
    its 7Y numbers, and `metrics_horizon` says 7 instead of 10.

    Both `_build_analysis()` and `_build_lumpsum_analysis()` emit a
    `horizons: [...]` list with these same field names per entry, so one
    extractor works for both modes.
    """
    horizons = fund_analysis.get("horizons") or []
    row = next((h for h in horizons if h.get("horizon") == horizon), None)
    metrics_horizon: Optional[int] = horizon if row and row.get("median_wm") is not None else None

    if metrics_horizon is None:
        pick = select_robust_horizon([(h.get("horizon"), h) for h in horizons], horizon)
        if pick is not None:
            metrics_horizon, row = pick
        else:
            row = None

    return {
        "fqs": row.get("fqs") if row else None,
        "success_pct": row.get("success_pct") if row else None,
        "median_wm": row.get("median_wm") if row else None,
        "p10_wm": row.get("p10_wm") if row else None,
        "cv": row.get("cv") if row else None,
        "min_wm_required": row.get("min_wm_required") if row else None,
        "n_windows": row.get("n_windows") if row else None,
        "metrics_horizon": metrics_horizon,
    }


def build_fund_row(
    scheme_code: int,
    scheme_name: str,
    category: str,
    allocation_pct: float,
    fund_analysis: Dict[str, Any],
    horizon: int,
    mode: str,
) -> FundRow:
    m = extract_fund_metrics(fund_analysis, horizon)
    median_wm = m["median_wm"]
    p10_wm = m["p10_wm"]
    # Every displayed figure is read at metrics_horizon and ONLY at that window.
    mh = m["metrics_horizon"] or horizon
    extrapolated = median_wm is not None and mh != horizon

    xirr_median = wm_to_xirr(mode, median_wm, mh) if median_wm else None
    xirr_p10 = wm_to_xirr(mode, p10_wm, mh) if p10_wm else None

    # Recomputed from (category, mh, mode) rather than trusted off the
    # analysis dict — see _required_min_wm's docstring for why the stored
    # value silently went missing whenever since_2010/separate_management
    # supplied this fund's numbers. None when the fund has no usable data at
    # any horizon (median_wm is None), matching `extrapolated`'s guard.
    min_wm_required = _required_min_wm(category, mh, mode) if median_wm is not None else None
    # Benchmark XIRR: the annualised return that would just clear the
    # category's minimum WM bar at this fund's own metrics_horizon — same
    # wm_to_xirr conversion as xirr_median/xirr_p10, just fed the benchmark
    # WM instead of an actual outcome.
    benchmark_xirr = wm_to_xirr(mode, min_wm_required, mh) if min_wm_required else None

    if extrapolated:
        # Hold the shorter window's XIRR constant and compound it out to the
        # advisor's tenure — the only figures that move to `horizon`.
        proj_wm_median = xirr_to_wm(mode, xirr_median, horizon)
        proj_wm_p10 = xirr_to_wm(mode, xirr_p10, horizon)
    else:
        proj_wm_median, proj_wm_p10 = median_wm, p10_wm

    return FundRow(
        scheme_code=scheme_code,
        scheme_name=scheme_name,
        category=category,
        allocation_pct=allocation_pct,
        fqs=m["fqs"],
        success_pct=m["success_pct"],
        median_wm=median_wm,
        xirr_median=xirr_median,
        p10_wm=p10_wm,
        xirr_p10=xirr_p10,
        available=proj_wm_median is not None,
        metrics_horizon=mh if median_wm is not None else None,
        extrapolated=extrapolated,
        proj_wm_median=proj_wm_median,
        proj_wm_p10=proj_wm_p10,
        cv=m["cv"],
        min_wm_required=min_wm_required,
        benchmark_xirr=benchmark_xirr,
        n_windows=m["n_windows"],
    )


# ─── Portfolio blending ───────────────────────────────────────────────────────

def build_mode_projection(
    funds: List[FundRow],
    investment_amount: float,
    horizon: int,
    mode: str,
) -> ModeProjection:
    capital_invested = investment_amount * horizon * 12 if mode == "sip" else investment_amount

    exp_wealth_median = 0.0
    exp_wealth_p10 = 0.0
    excluded: List[str] = []
    extrapolated: List[str] = []
    # Allocation-weighted CV / benchmark WM, accumulated over the same
    # available-fund set the wealth blend uses (weights carried over from
    # `allocation_pct`, not renormalised — matches how the wealth blend
    # already leaves excluded funds' allocation "unspent" rather than
    # redistributing it).
    weighted_cv = 0.0
    weighted_cv_wt = 0.0
    weighted_benchmark_wm = 0.0
    weighted_benchmark_wt = 0.0

    for f in funds:
        if not f.available:
            excluded.append(f.scheme_name)
            continue
        if f.extrapolated:
            extrapolated.append(f.scheme_name)
        # proj_wm_* are the multiples AT `horizon` — identical to median_wm/
        # p10_wm for a full-history fund, and extrapolated from the shorter
        # window's XIRR for an Additional Recommendation. `or f.median_wm`
        # keeps rows built directly (e.g. in tests) working unchanged.
        wm_median = f.proj_wm_median if f.proj_wm_median is not None else f.median_wm
        wm_p10 = f.proj_wm_p10 if f.proj_wm_p10 is not None else f.p10_wm
        if mode == "sip":
            exp_wealth_median += f.allocation_pct * investment_amount * (wm_median or 0.0) * horizon * 12
            exp_wealth_p10 += f.allocation_pct * investment_amount * (wm_p10 or 0.0) * horizon * 12
        else:
            exp_wealth_median += f.allocation_pct * investment_amount * (wm_median or 0.0)
            exp_wealth_p10 += f.allocation_pct * investment_amount * (wm_p10 or 0.0)
        if f.cv is not None:
            weighted_cv += f.allocation_pct * f.cv
            weighted_cv_wt += f.allocation_pct
        if f.min_wm_required is not None:
            weighted_benchmark_wm += f.allocation_pct * f.min_wm_required
            weighted_benchmark_wt += f.allocation_pct

    blended_wm_median = (exp_wealth_median / capital_invested) if capital_invested else 0.0
    blended_wm_p10 = (exp_wealth_p10 / capital_invested) if capital_invested else 0.0
    blended_cv = (weighted_cv / weighted_cv_wt) if weighted_cv_wt > 0 else None
    benchmark_wm = (weighted_benchmark_wm / weighted_benchmark_wt) if weighted_benchmark_wt > 0 else None
    benchmark_xirr = wm_to_xirr(mode, benchmark_wm, horizon) if benchmark_wm else None

    aggregate = PortfolioAggregate(
        horizon=horizon,
        capital_invested=round(capital_invested, 2),
        expected_wealth_median=round(exp_wealth_median, 2),
        expected_wealth_p10=round(exp_wealth_p10, 2),
        blended_wm_median=round(blended_wm_median, 6),
        blended_wm_p10=round(blended_wm_p10, 6),
        blended_xirr_median=wm_to_xirr(mode, blended_wm_median, horizon),
        blended_xirr_p10=wm_to_xirr(mode, blended_wm_p10, horizon),
        excluded_funds=excluded,
        extrapolated_funds=extrapolated,
        blended_cv=round(blended_cv, 6) if blended_cv is not None else None,
        benchmark_wm=round(benchmark_wm, 6) if benchmark_wm is not None else None,
        benchmark_xirr=benchmark_xirr,
    )

    return ModeProjection(
        mode=mode,
        investment_amount=investment_amount,
        horizon=horizon,
        funds=funds,
        aggregate=aggregate,
    )


def all_funds_excluded(projection: ModeProjection) -> bool:
    """True when NONE of the selected funds had data at the chosen horizon —
    the signal that this proposal cannot be built honestly (see module
    docstring's BUG FIXED note). Callers should refuse to proceed rather than
    silently emitting a zero-value proposal."""
    return len(projection.funds) > 0 and len(projection.aggregate.excluded_funds) == len(projection.funds)


# ─── SWP (withdrawal phase) ───────────────────────────────────────────────────

def build_swp_projection(
    corpus: float,
    annual_return: float,
    inflation: float,
    monthly_withdrawal: float,
    years: int = 10,
    corpus_auto_derived: bool = False,
    annual_return_auto_derived: bool = False,
) -> SwpProjection:
    """Month-by-month withdrawal amortisation. Verified cell-for-cell against
    the template's SWP!B8:F127 (120 rows = 10 years)."""
    withdrawal_with_inflation = monthly_withdrawal * (1.0 + inflation) / 12.0 + monthly_withdrawal
    months = max(1, years * 12)

    rows: List[SwpMonthRow] = []
    principal = corpus
    for month in range(1, months + 1):
        withdrawal = -withdrawal_with_inflation
        after_withdrawal = principal + withdrawal
        growth = after_withdrawal * annual_return / 12.0
        closing = after_withdrawal + growth
        rows.append(SwpMonthRow(
            month=month,
            opening_principal=round(principal, 2),
            withdrawal=round(withdrawal, 2),
            principal_after_withdrawal=round(after_withdrawal, 2),
            growth=round(growth, 2),
            closing_balance=round(closing, 2),
        ))
        principal = closing

    return SwpProjection(
        corpus=corpus,
        annual_return=annual_return,
        inflation=inflation,
        monthly_withdrawal=monthly_withdrawal,
        withdrawal_with_inflation=round(withdrawal_with_inflation, 2),
        years=years,
        rows=rows,
        ending_balance=rows[-1].closing_balance if rows else corpus,
        corpus_auto_derived=corpus_auto_derived,
        annual_return_auto_derived=annual_return_auto_derived,
    )


def default_swp_inputs(mode_projection: ModeProjection) -> Dict[str, Optional[float]]:
    """Auto-derive rule ('auto-derive but editable'):
    corpus = the mode projection's expected median wealth at its (single)
    horizon; annual_return = its blended median XIRR at that horizon.

    Returns {"corpus": None, "annual_return": None} — NOT zeros — when the
    horizon has no usable data (see module docstring's BUG FIXED note), so
    callers can detect "cannot auto-derive" and refuse to build a misleading
    proposal instead of silently defaulting to Rs. 0 / 0%.
    """
    a = mode_projection.aggregate
    if all_funds_excluded(mode_projection) or a.expected_wealth_median <= 0:
        return {"corpus": None, "annual_return": None}
    return {
        "corpus": a.expected_wealth_median,
        "annual_return": a.blended_xirr_median,
    }


# ─── Serialisation ────────────────────────────────────────────────────────────

def mode_projection_to_dict(mp: ModeProjection) -> Dict[str, Any]:
    return {
        "mode": mp.mode,
        "investment_amount": mp.investment_amount,
        "horizon": mp.horizon,
        "funds": [
            {
                "scheme_code": f.scheme_code,
                "scheme_name": f.scheme_name,
                "category": f.category,
                "allocation_pct": f.allocation_pct,
                "fqs": f.fqs,
                "success_pct": f.success_pct,
                "median_wm": f.median_wm,
                "xirr_median": f.xirr_median,
                "p10_wm": f.p10_wm,
                "xirr_p10": f.xirr_p10,
                "available": f.available,
                "metrics_horizon": f.metrics_horizon,
                "extrapolated": f.extrapolated,
                "proj_wm_median": f.proj_wm_median,
                "proj_wm_p10": f.proj_wm_p10,
                "cv": f.cv,
                "min_wm_required": f.min_wm_required,
                "benchmark_xirr": f.benchmark_xirr,
                "n_windows": f.n_windows,
            }
            for f in mp.funds
        ],
        "aggregate": asdict(mp.aggregate),
    }


def swp_projection_to_dict(swp: SwpProjection, include_rows: bool = True) -> Dict[str, Any]:
    d = {
        "corpus": swp.corpus,
        "annual_return": swp.annual_return,
        "inflation": swp.inflation,
        "monthly_withdrawal": swp.monthly_withdrawal,
        "withdrawal_with_inflation": swp.withdrawal_with_inflation,
        "years": swp.years,
        "ending_balance": swp.ending_balance,
        "corpus_auto_derived": swp.corpus_auto_derived,
        "annual_return_auto_derived": swp.annual_return_auto_derived,
    }
    if include_rows:
        d["rows"] = [asdict(r) for r in swp.rows]
    return d
