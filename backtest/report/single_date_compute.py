"""Extract everything needed for the 31-Dec-2019 / 5Y single-cutoff report for
two named funds, from the ALREADY-COMPLETED full backtest run
(backtest_data/results/). No new prediction is computed here — this cutoff and
these two funds were already part of that 164-fund, 160-cutoff run, so their
row is simply looked up and re-shaped into the report's requested structure.

Every field traces to one of:
  backend/fqs_engine.py            score_horizon()      — WM/Median/Blended/CV/Success%/FQS
  backend/category_map.py          min_wm_for()          — the numeric benchmark hurdle
  backtest/backtest_engine.py      rank() / rank_actuals — predicted & actual ranking
  backtest/actual_outcomes.py      realise()             — the realised 5Y outcome
No formula is reimplemented; this script only selects and relabels existing
columns from backtest_predictions.parquet / backtest_comparison.parquet.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS = REPO_ROOT / "backtest_data" / "results"
OUT = REPO_ROOT / "backtest_data" / "report_out"
OUT.mkdir(parents=True, exist_ok=True)

CUTOFF = "2019-12-31"
FUNDS = {
    "113177": "Nippon India Small Cap Fund",
    "112932": "Mirae Asset Large & Mid Cap Fund",
}


def pct_diff(pred, actual):
    if pred is None or actual is None or pred == 0:
        return None
    return (actual - pred) / abs(pred)


def accuracy_from_pct_diff(pd_: float | None) -> float | None:
    """100% - |percentage difference|, floored at 0%. A direct, transparent
    accuracy score — not a production field, just this report's stated,
    disclosed way of turning an already-computed error into a 0-100% score."""
    if pd_ is None:
        return None
    return max(0.0, 1.0 - abs(pd_))


FUTURE_CUTOFF = "2024-12-31"   # prediction_date + 5Y — used ONLY for the
# distributional statistics (CV, Success %, FQS, and as a cross-check Median/
# Blended WM) that have no single-outcome "actual" by construction. Same
# production score_horizon() call, same fund, just re-run once that much real
# future data genuinely exists — a self-persistence check, not a new formula.


def build_fund_block(code: str) -> dict:
    preds = pd.read_parquet(RESULTS / "backtest_predictions.parquet")
    comp = pd.read_parquet(RESULTS / "backtest_comparison.parquet")

    prow = preds[(preds.scheme_code == int(code)) & (preds.cutoff_date == CUTOFF)]
    crow = comp[(comp.scheme_code == int(code)) & (comp.cutoff_date == CUTOFF)]
    frow = preds[(preds.scheme_code == int(code)) & (preds.cutoff_date == FUTURE_CUTOFF)]
    if prow.empty:
        raise RuntimeError(f"No prediction row for {code} at {CUTOFF} — check nav_store/eligibility.")
    p = prow.iloc[0].to_dict()
    c = crow.iloc[0].to_dict() if not crow.empty else None
    fut = frow.iloc[0].to_dict() if not frow.empty else None

    # n_validated_at_cutoff — the actual-rank denominator (funds whose 5Y
    # future was ALSO known at this cutoff; smaller than total_eligible_funds
    # since some eligible funds' 5Y future hadn't happened yet even as of the
    # dataset's latest NAV date). Disclosed explicitly in the report, not
    # silently treated as the same denominator as predicted_rank.
    n_validated = int((comp.cutoff_date == CUTOFF).sum())

    fund_history_years = round(
        (pd.Timestamp(p["historical_data_end"]) - pd.Timestamp(p["historical_data_start"])).days / 365.25, 2
    )

    block = {
        "scheme_code": code,
        "scheme_name": p["scheme_name"],
        "internal_category": p["internal_category"],
        "amfi_category": p["amfi_category"],
        "prediction_date": CUTOFF,
        "historical_data_start": p["historical_data_start"],
        "historical_data_used": f"{p['historical_data_start']} to {p['historical_data_end']}",
        "hidden_data": "2020-01-01 to 2026-08-05 (dataset's latest available NAV)",
        "rolling_period_years": int(p["horizon"]),
        "min_history_required_years": int(p["required_history_years"]),
        "available_history_years": float(p["available_history_years"]),
        "eligible": bool(p["available_history_years"] >= p["required_history_years"]),
        "total_nav_count": None,  # filled by caller from data_quality_report
        "monthly_points": int(p["monthly_points"]),
        "n_rolling_windows": int(p["n_rolling_observations"]),

        "predicted": {
            "wm": p["median_wm"], "median_wm": p["median_wm"], "blended_wm": p["blended_wm"],
            "cv": p["cv"], "success_pct": p["success_pct"], "fqs": p["fqs"], "fqs_tier": p["fqs_tier"],
            "benchmark_min_wm": p["benchmark_min_wm"], "benchmark_source": p["benchmark_source"],
            "rank_blended_wm": int(p["rank_blended_wm"]), "rank_fqs": int(p["rank_fqs"]),
            "total_eligible_funds": int(p["total_eligible_funds"]),
            "recommended_flag": bool(p["recommended_flag"]),
            "predicted_top_10_pct": bool(p["predicted_top_10_pct"]),
            "benchmark_cleared": bool(p["blended_wm"] >= p["benchmark_min_wm"]),
            "trailing_wm": p["trailing_wm"],
            "predicted_xirr_median": p["predicted_xirr_median"],
        },
        "actual": None,
        "comparison": None,
        "n_validated_at_cutoff": n_validated,
    }

    if c is not None:
        block["actual"] = {
            # The ONE realised outcome: a single SIP starting the month after
            # the cutoff, run for 5 years, using data that did not exist at
            # prediction time (backtest/actual_outcomes.py realise()).
            "wm": c["actual_wm"],
            # CV / Success % / FQS describe the SHAPE of a distribution of
            # MANY rolling windows — a single realised outcome cannot produce
            # them. These are instead the SAME production statistics,
            # re-computed by the SAME score_horizon() call, once real data
            # through prediction_date+5Y genuinely exists (self-persistence).
            # Median WM / Blended WM are shown BOTH ways for cross-check.
            "median_wm_single_outcome": c["actual_wm"],
            "blended_wm_single_outcome": c["actual_wm"],
            "median_wm_recomputed_at_future_cutoff": fut["median_wm"] if fut else None,
            "blended_wm_recomputed_at_future_cutoff": fut["blended_wm"] if fut else None,
            "cv": fut["cv"] if fut else None,
            "success_pct": fut["success_pct"] if fut else None,
            "fqs": fut["fqs"] if fut else None,
            "fqs_tier": fut["fqs_tier"] if fut else None,
            "benchmark_min_wm": p["benchmark_min_wm"],  # same static hurdle, category+horizon constant
            "actual_rank": int(c["actual_rank"]), "actual_total_funds_validated": n_validated,
            "actual_top_fund": bool(c["actual_top_fund"]),
            "actual_top_10_pct": bool(c["actual_top_10_pct"]),
            "benchmark_cleared": bool(c["actual_wm"] >= p["benchmark_min_wm"]),
            "actual_xirr": c["actual_xirr"],
            "actual_wealth": c["actual_wealth"],
            "future_cutoff_used_for_distributional_stats": FUTURE_CUTOFF,
        }
        block["comparison"] = {
            # Point-forecast vs the single realised outcome — the
            # decision-relevant comparison ("did the number roughly match
            # what actually happened to the money").
            "wm": {
                "predicted": p["median_wm"], "actual": c["actual_wm"],
                "abs_diff": c["actual_wm"] - p["median_wm"],
                "pct_diff": pct_diff(p["median_wm"], c["actual_wm"]),
            },
            "blended_wm": {
                "predicted": p["blended_wm"], "actual": c["actual_wm"],
                "abs_diff": c["actual_wm"] - p["blended_wm"],
                "pct_diff": pct_diff(p["blended_wm"], c["actual_wm"]),
            },
            "xirr": {
                "predicted": p["predicted_xirr_median"], "actual": c["actual_xirr"],
                "abs_diff": c["actual_xirr"] - p["predicted_xirr_median"],
                "pct_diff": pct_diff(p["predicted_xirr_median"], c["actual_xirr"]),
            },
            # Self-persistence — same distributional statistic, same
            # production formula, recomputed once real data through
            # prediction_date+5Y exists. The only valid "actual" for a
            # distribution-shape statistic.
            "cv": {
                "predicted": p["cv"], "actual": fut["cv"] if fut else None,
                "abs_diff": (fut["cv"] - p["cv"]) if fut else None,
                "pct_diff": pct_diff(p["cv"], fut["cv"] if fut else None),
            },
            "success_pct": {
                "predicted": p["success_pct"], "actual": fut["success_pct"] if fut else None,
                "abs_diff": (fut["success_pct"] - p["success_pct"]) if fut else None,
                "pct_diff": pct_diff(p["success_pct"], fut["success_pct"] if fut else None),
            },
            "fqs": {
                "predicted": p["fqs"], "actual": fut["fqs"] if fut else None,
                "abs_diff": (fut["fqs"] - p["fqs"]) if fut else None,
                "pct_diff": pct_diff(p["fqs"], fut["fqs"] if fut else None),
            },
            "rank": {
                "predicted": int(p["rank_blended_wm"]), "predicted_of": int(p["total_eligible_funds"]),
                "actual": int(c["actual_rank"]), "actual_of": n_validated,
            },
            "recommendation": {
                "predicted": bool(p["recommended_flag"]), "actual": bool(c["actual_top_fund"]),
                "match": bool(p["recommended_flag"]) == bool(c["actual_top_fund"]),
            },
            "benchmark": {
                "predicted_cleared": bool(p["blended_wm"] >= p["benchmark_min_wm"]),
                "actual_cleared": bool(c["actual_wm"] >= p["benchmark_min_wm"]),
                "match": (p["blended_wm"] >= p["benchmark_min_wm"]) == (c["actual_wm"] >= p["benchmark_min_wm"]),
            },
        }
        for m in ("wm", "blended_wm", "xirr", "cv", "success_pct", "fqs"):
            block["comparison"][m]["accuracy_pct"] = accuracy_from_pct_diff(block["comparison"][m]["pct_diff"])

    return block


def main() -> None:
    dq = pd.read_parquet(RESULTS / "data_quality_report.parquet")
    report = {"prediction_date": CUTOFF, "hidden_period": "2020-01-01 to 2026-08-05", "funds": {}}
    for code, label in FUNDS.items():
        block = build_fund_block(code)
        d = dq[dq.scheme_code == int(code)].iloc[0]
        block["total_nav_count"] = int(d["n_points"])
        block["data_quality"] = {
            "first_nav": d["first"], "last_nav": d["last"], "span_years": float(d["span_years"]),
            "duplicate_dates": int(d["duplicate_dates"]), "missing_months": int(d["missing_months"]),
            "n_long_gaps": int(d["n_long_gaps"]),
        }
        report["funds"][code] = block

    (OUT / "single_date_report_data.json").write_text(json.dumps(report, indent=2, default=str))
    print(f"Wrote {OUT / 'single_date_report_data.json'}")
    print(json.dumps(report, indent=2, default=str)[:3000])


if __name__ == "__main__":
    main()
