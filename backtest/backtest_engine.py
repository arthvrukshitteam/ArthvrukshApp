"""The backtest loop.

For every calendar month-end cutoff:

    1. rebuild each fund's data as it stood at that cutoff
    2. decide eligibility from that data alone
    3. run the real model, rank the survivors, FREEZE
    4. only then look forward and measure what actually happened
    5. compare

Ordering in `_run_cutoff` is load-bearing: predictions are fully materialised
before `actual_outcomes` is imported into the flow. If you refactor this, keep
the freeze boundary explicit.

Per-fund rolling windows are built once over full history and sliced per cutoff
(MODEL_AUDIT.md §7.2). That is an exact optimisation, not an approximation, and
`--cross-check` proves it fund by fund.
"""
from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Sequence

from . import actual_outcomes as outcomes
from . import config as C
from . import eligibility as elig
from . import model_adapter as adapter
from . import point_in_time as pit


@dataclass
class Fund:
    scheme_code: int
    scheme_name: str
    amfi_category: str
    internal_category: str
    nav: list[tuple[date, float]]          # raw, ascending, unsanitised

    # Built once, sliced per cutoff
    windows: list = field(default_factory=list, repr=False)
    b_eoms: list = field(default_factory=list, repr=False)

    @property
    def inception(self) -> date | None:
        return self.nav[0][0] if self.nav else None


@dataclass
class CutoffResult:
    cutoff: date
    predictions: list[dict]
    actuals: list[dict]
    exclusions: list[dict]
    n_eligible: int
    validation_status: str


# ── ranking ──────────────────────────────────────────────────────────────────

def rank(preds: list[dict], metric: str) -> None:
    """Attach `rank_<metric>` in place, 1 = best. Ties share the lower rank.

    Mirrors the app's ordering: descending on the metric. The app's Debt-last
    rule is not reproduced because Debt is out of scope (D2); if Debt is ever
    re-included, the group key from server.py:548-557 must be added here.
    """
    ordered = sorted(
        preds,
        key=lambda p: (-(p[metric] if p[metric] is not None else float("-inf")),
                       p["scheme_name"]),
    )
    key = f"rank_{metric}"
    prev_val, prev_rank = object(), 0
    for i, p in enumerate(ordered, start=1):
        val = p[metric]
        if val != prev_val:
            prev_rank, prev_val = i, val
        p[key] = prev_rank
        p["total_eligible_funds"] = len(preds)


def rank_actuals(rows: list[dict]) -> None:
    ordered = sorted(rows, key=lambda r: -r["actual_wm"])
    n = len(ordered)
    prev_val, prev_rank = object(), 0
    for i, r in enumerate(ordered, start=1):
        if r["actual_wm"] != prev_val:
            prev_rank, prev_val = i, r["actual_wm"]
        r["actual_rank"] = prev_rank
        r["actual_total_funds"] = n
        r["actual_percentile"] = 1.0 - (prev_rank - 1) / n if n > 1 else 1.0
        r["actual_top_performer"] = prev_rank == 1
        r["actual_top_10_pct"] = prev_rank <= max(1, round(n * 0.10))
        r["actual_top_20_pct"] = prev_rank <= max(1, round(n * 0.20))


# ── one cutoff ───────────────────────────────────────────────────────────────

def _run_cutoff(
    funds: Sequence[Fund], cutoff: date, horizon: int, validation_horizon: int
) -> CutoffResult:
    predictions: list[dict] = []
    exclusions: list[dict] = []

    # ── PHASE 1: prediction. Only data <= cutoff is touched. ─────────────────
    for f in funds:
        v = elig.assess(
            f.nav, cutoff, f.internal_category, horizon,
            b_eoms=f.b_eoms,
        )
        if not v.eligible:
            exclusions.append({
                "cutoff_date": cutoff.isoformat(),
                "scheme_code": f.scheme_code,
                "scheme_name": f.scheme_name,
                "fund_inception_date": f.inception.isoformat() if f.inception else None,
                "status": "excluded",
                "exclusion_reason": v.reason.value,
                "required_history_years": v.required_history_years,
                "available_history_years": v.available_history_years,
                "required_rolling_period": horizon,
                "available_rolling_observations": v.n_rolling_windows,
                "monthly_points": v.monthly_points,
            })
            continue

        lme = pit.eomonth(date.fromisoformat(v.last_nav_at_cutoff))
        p = adapter.predict(
            cutoff=cutoff,
            scheme_code=f.scheme_code,
            scheme_name=f.scheme_name,
            internal_category=f.internal_category,
            amfi_category=f.amfi_category,
            horizon=horizon,
            windows_full=f.windows,
            b_eoms=f.b_eoms,
            last_month_end=lme,
            history_years=v.available_history_years,
            monthly_points=v.monthly_points,
            first_nav=v.first_nav,
            last_nav=v.last_nav_at_cutoff,
        )
        if p is None:
            exclusions.append({
                "cutoff_date": cutoff.isoformat(),
                "scheme_code": f.scheme_code,
                "scheme_name": f.scheme_name,
                "fund_inception_date": f.inception.isoformat() if f.inception else None,
                "status": "excluded",
                "exclusion_reason": elig.Reason.NO_ROLLING_WINDOWS.value,
                "required_history_years": v.required_history_years,
                "available_history_years": v.available_history_years,
                "required_rolling_period": horizon,
                "available_rolling_observations": 0,
                "monthly_points": v.monthly_points,
            })
            continue
        predictions.append(p.to_row())

    if not predictions:
        return CutoffResult(cutoff, [], [], exclusions, 0, "NO_ELIGIBLE_FUNDS")

    rank(predictions, C.PRIMARY_RANK_METRIC)
    rank(predictions, C.SECONDARY_RANK_METRIC)
    n = len(predictions)
    top10 = max(1, round(n * 0.10))
    top20 = max(1, round(n * 0.20))
    primary = f"rank_{C.PRIMARY_RANK_METRIC}"
    for p in predictions:
        r = p[primary]
        p["recommended_flag"] = r == 1          # top-of-list; see MODEL_AUDIT.md §0 #3
        p["predicted_top_10_pct"] = r <= top10
        p["predicted_top_20_pct"] = r <= top20
        p["predicted_percentile"] = 1.0 - (r - 1) / n if n > 1 else 1.0

    # ══ PREDICTION FROZEN. Future data may be read past this line. ═══════════

    by_code = {f.scheme_code: f for f in funds}
    actuals: list[dict] = []
    for p in predictions:
        a = outcomes.realise(
            by_code[p["scheme_code"]].nav, cutoff, p["scheme_code"], validation_horizon
        )
        if a is not None:
            row = a.to_row()
            row["scheme_name"] = p["scheme_name"]
            actuals.append(row)

    if actuals:
        rank_actuals(actuals)

    if not actuals:
        status = "PENDING"
    elif len(actuals) == len(predictions):
        status = "VALIDATED"
    else:
        status = "PARTIAL"
    for p in predictions:
        p["validation_status"] = status

    return CutoffResult(cutoff, predictions, actuals, exclusions, n, status)


# ── full run ─────────────────────────────────────────────────────────────────

def prepare(funds: Iterable[Fund], horizon: int, log=print) -> list[Fund]:
    """Build each fund's full-history windows once. MODEL_AUDIT.md §7.2."""
    out, t0 = [], time.time()
    for i, f in enumerate(funds, 1):
        try:
            f.windows, f.b_eoms = pit.build_all_windows(f.nav, f.internal_category, horizon)
        except AssertionError as e:
            log(f"  !! {f.scheme_code} {f.scheme_name}: {e}")
            continue
        out.append(f)
        if i % 250 == 0:
            log(f"  prepared {i} funds  ({time.time()-t0:.0f}s)")
    log(f"  prepared {len(out)} funds in {time.time()-t0:.0f}s")
    return out


def run(
    funds: Sequence[Fund],
    cutoffs: Sequence[date],
    horizon: int = C.ROLLING_PERIOD_YEARS,
    validation_horizon: int = C.VALIDATION_HORIZON_YEARS,
    log=print,
) -> dict:
    """Execute every cutoff. Returns the assembled tables."""
    all_preds: list[dict] = []
    all_actuals: list[dict] = []
    all_excl: list[dict] = []
    period_rows: list[dict] = []
    t0 = time.time()

    for i, cutoff in enumerate(cutoffs, 1):
        res = _run_cutoff(funds, cutoff, horizon, validation_horizon)
        all_preds.extend(res.predictions)
        all_actuals.extend(res.actuals)
        all_excl.extend(res.exclusions)

        reasons = Counter(e["exclusion_reason"] for e in res.exclusions)
        period_rows.append({
            "cutoff_date": cutoff.isoformat(),
            "n_eligible": res.n_eligible,
            "n_excluded": len(res.exclusions),
            "n_validated": len(res.actuals),
            "validation_status": res.validation_status,
            "top_exclusion_reason": reasons.most_common(1)[0][0] if reasons else None,
            **{f"excl_{k}": v for k, v in reasons.items()},
        })

        if i % 12 == 0 or i == len(cutoffs):
            log(f"  {i}/{len(cutoffs)}  {cutoff}  eligible={res.n_eligible:<5} "
                f"validated={len(res.actuals):<5} {res.validation_status} "
                f"({time.time()-t0:.0f}s)")

    return {
        "predictions": all_preds,
        "actuals": all_actuals,
        "exclusions": all_excl,
        "period_summary": period_rows,
        "meta": {
            "n_cutoffs": len(cutoffs),
            "first_cutoff": cutoffs[0].isoformat() if cutoffs else None,
            "last_cutoff": cutoffs[-1].isoformat() if cutoffs else None,
            "n_funds_considered": len(funds),
            "rolling_period_years": horizon,
            "validation_horizon_years": validation_horizon,
            "min_history_years": C.MIN_HISTORY_YEARS,
            "sip_amount": C.SIP_AMOUNT,
            "primary_rank_metric": C.PRIMARY_RANK_METRIC,
            "runtime_seconds": round(time.time() - t0, 1),
        },
    }


def merge_comparison(predictions: list[dict], actuals: list[dict]) -> list[dict]:
    """Join frozen predictions to realised outcomes. MODEL_AUDIT.md §24."""
    idx = {(a["cutoff_date"], a["scheme_code"]): a for a in actuals}
    out = []
    for p in predictions:
        a = idx.get((p["cutoff_date"], p["scheme_code"]))
        if a is None:
            continue
        pr = p[f"rank_{C.PRIMARY_RANK_METRIC}"]
        out.append({
            "cutoff_date": p["cutoff_date"],
            "scheme_code": p["scheme_code"],
            "scheme_name": p["scheme_name"],
            "internal_category": p["internal_category"],
            "horizon": p["horizon"],

            "predicted_rank": pr,
            "predicted_rank_fqs": p[f"rank_{C.SECONDARY_RANK_METRIC}"],
            "actual_rank": a["actual_rank"],
            "rank_difference": a["actual_rank"] - pr,
            "total_funds": p["total_eligible_funds"],

            "blended_wm": p["blended_wm"],
            "median_wm": p["median_wm"],
            "p10_wm": p["p10_wm"],
            "cv": p["cv"],
            "success_pct": p["success_pct"],
            "fqs": p["fqs"],
            "trailing_wm": p["trailing_wm"],

            "predicted_wealth_median": p["predicted_wealth_median"],
            "actual_wealth": a["actual_wealth"],
            "wealth_difference": a["actual_wealth"] - p["predicted_wealth_median"],

            "predicted_xirr_median": p["predicted_xirr_median"],
            "actual_xirr": a["actual_xirr"],
            "return_difference": (
                a["actual_xirr"] - p["predicted_xirr_median"]
                if a["actual_xirr"] is not None and p["predicted_xirr_median"] is not None
                else None
            ),

            "predicted_wm_median": p["median_wm"],
            "actual_wm": a["actual_wm"],

            "predicted_top_fund": p["recommended_flag"],
            "actual_top_fund": a["actual_top_performer"],
            "predicted_top_10_pct": p["predicted_top_10_pct"],
            "actual_top_10_pct": a["actual_top_10_pct"],
            "predicted_top_20_pct": p["predicted_top_20_pct"],
            "actual_top_20_pct": a["actual_top_20_pct"],
            "predicted_percentile": p["predicted_percentile"],
            "actual_percentile": a["actual_percentile"],
        })
    return out
