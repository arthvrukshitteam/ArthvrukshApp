"""Generic, reusable AV FQS validation for ANY fund already present in the
backtest run (backtest_data/results/) — no fund-specific code, no hardcoded
dates. Call validate_fund(scheme_code) for any scheme code that has been
through the download + backtest pipeline (backtest/download_navs.py +
backtest/run_backtest.py); it works identically for a new fund the moment
that fund's data exists in nav_store and has been backtested — nothing here
changes per fund.

Every number is read from backtest_predictions.parquet / backtest_comparison
.parquet / data_quality_report.parquet / the raw nav_store JSON — all of
which are themselves produced entirely by the model's own, unmodified
production functions (fqs_engine.score_horizon, category_map.min_wm_for,
backtest_engine.rank/rank_actuals, actual_outcomes.realise). This module
performs NO new financial calculation — it selects, joins, and labels
already-computed production output.

Dynamic prediction-date rule (per fund, no hardcoded dates):
    prediction_date = the LATEST cutoff at which this fund both
      (a) met the model's own eligibility rule (>= horizon + HISTORY_BUFFER_
          YEARS of history, backend/server.py's rule, reused unmodified via
          backtest/eligibility.py), and
      (b) had a COMPLETE forward 5-year outcome, per the model's own
          admission rule (MODEL_AUDIT.md section 7.2's EOMONTH(B_k) test,
          reused unmodified via backtest/actual_outcomes.py's realise()).
    Both conditions are already exactly what "this fund has a validated row
    in backtest_comparison.parquet at this cutoff" means, so
    prediction_date = max(cutoff_date) among that fund's rows in
    backtest_comparison.parquet. No date is chosen or assumed here.

Future-statistics rule (CV / Success% / FQS / Median WM / Blended WM,
self-persistence, confirmed with the user): distributional statistics have
no single-outcome "actual", so their actual value is the SAME production
score_horizon() output, re-run at prediction_date + 5Y once that much real
future data exists — using the fund's full accumulated history, exactly as
the production app itself would compute it on that later date.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS = REPO_ROOT / "backtest_data" / "results"
NAV_STORE = REPO_ROOT / "backtest_data" / "nav_store"
OUT = REPO_ROOT / "backtest_data" / "report_out"
OUT.mkdir(parents=True, exist_ok=True)

_ROLLING_PERIOD_YEARS = 5   # this validation's scope, per the brief — not a
                            # hardcoded date, a stated scope (the model
                            # supports 1..50; this run tests the 5Y rule).


def _load() -> dict[str, pd.DataFrame]:
    return {
        "preds": pd.read_parquet(RESULTS / "backtest_predictions.parquet"),
        "comp": pd.read_parquet(RESULTS / "backtest_comparison.parquet"),
        "dq": pd.read_parquet(RESULTS / "data_quality_report.parquet"),
    }


def _pct_diff(pred, actual) -> Optional[float]:
    if pred is None or actual is None or pred == 0:
        return None
    return (actual - pred) / abs(pred)


def _accuracy_pct(pct_diff_: Optional[float]) -> Optional[float]:
    """Disclosed, not a production field: 100% - |percentage difference|,
    floored at 0% — a transparent way to express an already-computed error
    as an accuracy score."""
    if pct_diff_ is None:
        return None
    return max(0.0, 1.0 - abs(pct_diff_))


def _raw_nav_count(scheme_code: int, start, end) -> int:
    """Count of raw daily NAV rows in [start, end] from the SAME nav_store
    file the backtest engine truncates/scores — descriptive, not a formula."""
    path = NAV_STORE / f"{scheme_code}.json"
    d = json.loads(path.read_text())
    return sum(1 for iso, _ in d["nav"] if start <= iso <= end)


def dynamic_prediction_date(comp: pd.DataFrame, scheme_code: int) -> Optional[str]:
    rows = comp[comp.scheme_code == scheme_code]
    if rows.empty:
        return None
    return str(rows["cutoff_date"].max())


def validate_fund(scheme_code: int, horizon_years: int = _ROLLING_PERIOD_YEARS) -> dict:
    tables = _load()
    preds, comp, dq = tables["preds"], tables["comp"], tables["dq"]

    prediction_date = dynamic_prediction_date(comp, scheme_code)
    if prediction_date is None:
        raise RuntimeError(
            f"Scheme {scheme_code} has no validated cutoff in backtest_comparison.parquet — "
            "either it isn't in nav_store yet, or no cutoff for it has a complete 5Y future. "
            "Run backtest/download_navs.py + backtest/run_backtest.py for this fund first; "
            "this function needs no other change to work for it."
        )

    p_row = preds[(preds.scheme_code == scheme_code) & (preds.cutoff_date == prediction_date)]
    c_row = comp[(comp.scheme_code == scheme_code) & (comp.cutoff_date == prediction_date)]
    if p_row.empty or c_row.empty:
        raise RuntimeError(f"Inconsistent backtest output for {scheme_code} at {prediction_date}")
    p = p_row.iloc[0].to_dict()
    c = c_row.iloc[0].to_dict()
    dqf = dq[dq.scheme_code == scheme_code].iloc[0].to_dict()

    # Future cutoff: prediction_date + horizon_years, computed programmatically
    # (pandas DateOffset), never a literal date string.
    future_cutoff = str((pd.Timestamp(prediction_date) + pd.DateOffset(years=horizon_years)).date())
    f_row = preds[(preds.scheme_code == scheme_code) & (preds.cutoff_date == future_cutoff)]
    fut = f_row.iloc[0].to_dict() if not f_row.empty else None
    if fut is None:
        raise RuntimeError(
            f"No self-persistence row for {scheme_code} at {future_cutoff} — the future "
            "cutoff wasn't part of the backtest's cutoff grid for this fund; re-run "
            "backtest/run_backtest.py without a --limit that excludes it."
        )

    n_validated_at_cutoff = int((comp.cutoff_date == prediction_date).sum())
    inception = dqf["first"]
    validation_start = str((pd.Timestamp(prediction_date) + pd.Timedelta(days=1)).date())
    validation_end = future_cutoff

    hist_nav_count = _raw_nav_count(scheme_code, inception, prediction_date)
    val_nav_count = _raw_nav_count(scheme_code, validation_start, validation_end)
    # NAVs actually consumed by the self-persistence CV/Success%/FQS figures:
    # the FULL accumulated history from inception through the future cutoff
    # (score_horizon always scores the fund's whole history, never an
    # isolated slice) — distinct from val_nav_count, which is only the NAVs
    # strictly inside the validation window.
    accumulated_nav_count_at_future_cutoff = _raw_nav_count(scheme_code, inception, future_cutoff)

    def benchmark_block(wm_value: float, min_wm: float, source: str) -> dict:
        return {
            "benchmark_type": "Category minimum-WM hurdle (no benchmark FUND exists in production)",
            "benchmark_wm": min_wm,
            "benchmark_source": source,
            "benchmark_median_wm": None, "benchmark_blended_wm": None,
            "benchmark_cv": None, "benchmark_success_pct": None,
            "not_applicable_note": (
                "category_map.CATEGORY_TABLE defines a single numeric WM hurdle per "
                "category/horizon, not a benchmark fund — Median/Blended WM, CV, and "
                "Success % are properties of a FUND's window distribution and have no "
                "equivalent for a constant threshold."
            ),
            "cleared": bool(wm_value >= min_wm),
        }

    historical = {
        "fund_name": p["scheme_name"], "category": p["internal_category"],
        "amfi_category": p["amfi_category"],
        "fund_inception_date": inception,
        "prediction_date": prediction_date,
        "historical_nav_period": f"{inception} to {prediction_date}",
        "total_historical_nav_count": hist_nav_count,
        "monthly_points": int(p["monthly_points"]),
        "n_rolling_windows": int(p["n_rolling_observations"]),
        "available_history_years": float(p["available_history_years"]),
        "required_history_years": int(p["required_history_years"]),
        "eligible": bool(p["available_history_years"] >= p["required_history_years"]),
        "wm": p["median_wm"], "median_wm": p["median_wm"], "blended_wm": p["blended_wm"],
        "cv": p["cv"], "success_pct": p["success_pct"], "fqs": p["fqs"], "fqs_tier": p["fqs_tier"],
        "fund_rank": int(p["rank_blended_wm"]), "total_eligible_funds": int(p["total_eligible_funds"]),
        "recommendation": "Recommended (rank #1 by Blended WM)" if p["recommended_flag"]
                           else f"Not recommended (rank #{int(p['rank_blended_wm'])} by Blended WM)",
        "benchmark": benchmark_block(p["blended_wm"], p["benchmark_min_wm"], p["benchmark_source"]),
    }

    predicted = {
        "prediction_generated_on": prediction_date,
        "prediction_date": prediction_date,
        "predicted_rolling_period": f"{validation_start} to {validation_end}",
        "wm": p["median_wm"], "median_wm": p["median_wm"], "blended_wm": p["blended_wm"],
        "cv": p["cv"], "success_pct": p["success_pct"], "fqs": p["fqs"], "fqs_tier": p["fqs_tier"],
        "fund_rank": int(p["rank_blended_wm"]), "total_eligible_funds": int(p["total_eligible_funds"]),
        "recommendation": "Recommended (rank #1 by Blended WM)" if p["recommended_flag"]
                           else f"Not recommended (rank #{int(p['rank_blended_wm'])} by Blended WM)",
        "benchmark": benchmark_block(p["blended_wm"], p["benchmark_min_wm"], p["benchmark_source"]),
    }

    actual = {
        "actual_validation_duration": f"{validation_start} to {validation_end}",
        "total_validation_nav_count": val_nav_count,
        "accumulated_nav_count_at_future_cutoff": accumulated_nav_count_at_future_cutoff,
        # Single realised outcome (actual_outcomes.py realise()) — the
        # decision-relevant "what happened to the money" figure.
        "wm_single_realised_outcome": c["actual_wm"],
        # Self-persistence — same score_horizon() call, re-run at
        # prediction_date+5Y using the fund's full accumulated history.
        "wm": fut["median_wm"], "median_wm": fut["median_wm"], "blended_wm": fut["blended_wm"],
        "cv": fut["cv"], "success_pct": fut["success_pct"], "fqs": fut["fqs"], "fqs_tier": fut["fqs_tier"],
        "fund_rank": int(c["actual_rank"]), "total_funds_validated_at_cutoff": n_validated_at_cutoff,
        "recommendation": "Was the actual top performer" if c["actual_top_fund"]
                           else f"Actual rank #{int(c['actual_rank'])} of {n_validated_at_cutoff} validated",
        "benchmark": benchmark_block(fut["blended_wm"], fut["benchmark_min_wm"], fut["benchmark_source"]),
        "actual_xirr": c["actual_xirr"], "actual_wealth": c["actual_wealth"],
    }

    def cmp_row(pred_val, actual_val, is_pct=False):
        abs_diff = None if pred_val is None or actual_val is None else actual_val - pred_val
        pct = _pct_diff(pred_val, actual_val)
        return {
            "predicted": pred_val, "actual": actual_val, "abs_diff": abs_diff,
            "pct_diff": pct, "accuracy_pct": _accuracy_pct(pct),
            "match": (round(pred_val, 4) == round(actual_val, 4))
                     if pred_val is not None and actual_val is not None else None,
        }

    comparison = {
        "validation_period": f"{validation_start} to {validation_end}",
        "wm": cmp_row(historical["wm"], actual["wm_single_realised_outcome"]),
        "median_wm": cmp_row(predicted["median_wm"], actual["median_wm"]),
        "blended_wm": cmp_row(predicted["blended_wm"], actual["blended_wm"]),
        "cv": cmp_row(predicted["cv"], actual["cv"]),
        "success_pct": cmp_row(predicted["success_pct"], actual["success_pct"]),
        "fqs": cmp_row(predicted["fqs"], actual["fqs"]),
        "benchmark_wm": cmp_row(predicted["benchmark"]["benchmark_wm"], actual["benchmark"]["benchmark_wm"]),
        "fund_rank": {
            "predicted": predicted["fund_rank"], "predicted_of": predicted["total_eligible_funds"],
            "actual": actual["fund_rank"], "actual_of": actual["total_funds_validated_at_cutoff"],
            "abs_diff": actual["fund_rank"] - predicted["fund_rank"],
        },
        "recommendation": {
            "predicted": predicted["recommendation"], "actual": actual["recommendation"],
            "match": bool(p["recommended_flag"]) == bool(c["actual_top_fund"]),
        },
        "benchmark_cleared": {
            "predicted": predicted["benchmark"]["cleared"], "actual": actual["benchmark"]["cleared"],
            "match": predicted["benchmark"]["cleared"] == actual["benchmark"]["cleared"],
        },
    }

    return {
        "scheme_code": scheme_code,
        "historical": historical,
        "predicted": predicted,
        "actual": actual,
        "comparison": comparison,
        "checklist_evidence": {
            "prediction_date_source": "max(cutoff_date) in backtest_comparison.parquet for this "
                                       "scheme_code — dynamically derived, not hardcoded",
            "future_cutoff_source": "prediction_date + horizon_years via pandas DateOffset — "
                                     "dynamically derived, not hardcoded",
            "eligibility_source": "backtest/eligibility.py assess(), which reuses server.py's own "
                                   "history_years >= horizon + HISTORY_BUFFER_YEARS rule",
            "wm_source": "backend/fqs_engine.py score_horizon() via backtest/model_adapter.py predict()",
            "benchmark_source": p["benchmark_source"],
            "ranking_source": "backtest/backtest_engine.py rank() [predicted] / rank_actuals() [actual]",
            "actual_outcome_source": "backtest/actual_outcomes.py realise()",
        },
    }


def main() -> None:
    codes = {"113177": "Nippon India Small Cap Fund", "112932": "Mirae Asset Large & Mid Cap Fund"}
    out = {}
    for code in codes:
        out[code] = validate_fund(int(code))
    (OUT / "dynamic_validation_data.json").write_text(json.dumps(out, indent=2, default=str))
    print(f"Wrote {OUT / 'dynamic_validation_data.json'}")
    for code, label in codes.items():
        v = out[code]
        print(f"\n{'='*70}\n{label} ({code})")
        print(f"  Prediction date (dynamic): {v['historical']['prediction_date']}")
        print(f"  Historical period: {v['historical']['historical_nav_period']}")
        print(f"  Validation period: {v['actual']['actual_validation_duration']}")
        print(f"  Predicted FQS {v['predicted']['fqs']}  ->  Actual FQS {v['actual']['fqs']}")
        print(f"  Predicted rank {v['predicted']['fund_rank']}/{v['predicted']['total_eligible_funds']}"
              f"  ->  Actual rank {v['actual']['fund_rank']}/{v['actual']['total_funds_validated_at_cutoff']}")


if __name__ == "__main__":
    main()
