"""Compute every number the client report cites, from the real backtest run
in backtest_data/results/. This is the SINGLE source of truth both the PDF
and Excel builders read from — nothing is computed twice, nothing is invented
here either: every statistic is either read directly from the model's own
frozen output (backtest/model_adapter.py's Prediction, backtest/actual_outcomes.py's
Actual) or a standard, named arithmetic aggregation (RMSE/MAE/MAPE/Pearson r/
bias/coverage) over those numbers.

Two DIFFERENT, clearly-labelled comparisons are computed, because the five
headline parameters are not all validate-able the same way:

  POINT-FORECAST vs SINGLE REALISED OUTCOME (blended_wm, median_wm, p10_wm and
  their XIRR equivalents): the model's prediction at cutoff T is a point
  estimate; the "actual" is the ONE real SIP an investor who started at T
  would have realised by T+5y. This is what backtest/actual_outcomes.py
  computes and is the decision-relevant comparison for a real investor.

  SELF-PERSISTENCE (cv, success_pct, fqs, plus median_wm/p10_wm again for
  cross-reference): these describe the SHAPE of the fund's historical
  rolling-window distribution, not a single outcome — there is no "actual CV"
  a single realised SIP could ever produce. The only honest backtest for a
  distributional statistic is: does the value measured at cutoff T predict the
  SAME statistic re-measured once 5 more years of data exist (cutoff T+5y)?
  Both values already exist in backtest_predictions.parquet for every fund
  that has predictions at both T and T+5y — this reuses real computed model
  output, it does not invent a new formula.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS = REPO_ROOT / "backtest_data" / "results"
OUT = REPO_ROOT / "backtest_data" / "report_out"
OUT.mkdir(parents=True, exist_ok=True)

FOCUS_FUNDS = [140225, 130502]   # Edelweiss Mid Cap, HDFC Small Cap

# ── generic stat helpers (standard definitions, not model-specific) ──────────

def _rmse(err: pd.Series) -> float | None:
    e = err.dropna()
    return float(math.sqrt((e ** 2).mean())) if len(e) else None


def _mae(err: pd.Series) -> float | None:
    e = err.dropna()
    return float(e.abs().mean()) if len(e) else None


def _mape(pred: pd.Series, actual: pd.Series) -> float | None:
    df = pd.DataFrame({"p": pred, "a": actual}).dropna()
    df = df[df["a"] != 0]
    return float((( (df["a"] - df["p"]).abs() / df["a"].abs())).mean()) if len(df) else None


def _bias(err: pd.Series) -> float | None:
    e = err.dropna()
    return float(e.mean()) if len(e) else None


def _pearson(x: pd.Series, y: pd.Series) -> float | None:
    df = pd.DataFrame({"x": x, "y": y}).dropna()
    if len(df) < 3 or df["x"].std() == 0 or df["y"].std() == 0:
        return None
    return float(df["x"].corr(df["y"]))


def point_forecast_stats(pred: pd.Series, actual: pd.Series) -> dict:
    err = actual - pred
    pct_err = (err / pred.abs()).where(pred != 0)
    return {
        "n": int(pd.DataFrame({"p": pred, "a": actual}).dropna().shape[0]),
        "mean_predicted": float(pred.mean()) if pred.notna().any() else None,
        "mean_actual": float(actual.mean()) if actual.notna().any() else None,
        "mean_abs_error": _mae(err),
        "mean_pct_error": float(pct_err.dropna().mean()) if pct_err.notna().any() else None,
        "rmse": _rmse(err),
        "mae": _mae(err),
        "mape": _mape(pred, actual),
        "bias": _bias(err),
        "bias_direction": (
            "over-predicts" if (_bias(err) or 0) < 0
            else "under-predicts" if (_bias(err) or 0) > 0
            else "unbiased"
        ),
        "correlation": _pearson(pred, actual),
    }


def self_persistence_stats(preds: pd.DataFrame, metric: str, horizon_months: int = 60) -> dict:
    """Join each fund's (cutoff T, cutoff T+5y) prediction rows on `metric`."""
    df = preds[["scheme_code", "cutoff_date", metric]].copy()
    df["cutoff_date"] = pd.to_datetime(df["cutoff_date"])
    df["future_date"] = df["cutoff_date"] + pd.DateOffset(months=horizon_months)

    merged = df.merge(
        df[["scheme_code", "cutoff_date", metric]].rename(
            columns={"cutoff_date": "future_date", metric: f"{metric}_future"}
        ),
        on=["scheme_code", "future_date"], how="inner",
    )
    if merged.empty:
        return {"n": 0}
    pred = merged[metric]
    actual = merged[f"{metric}_future"]
    stats = point_forecast_stats(pred, actual)
    stats["n"] = int(len(merged))
    return stats


def confidence_band_coverage(comp: pd.DataFrame, preds: pd.DataFrame) -> dict:
    """Share of realised WM outcomes falling inside the model's own P10-P90
    historical band at the SAME cutoff — an empirical coverage check, not a
    parametric confidence interval (P10/P90 are empirical percentiles of the
    historical window distribution, fqs_engine.py score_horizon)."""
    # comp already carries p10_wm/median_wm (backtest_engine.merge_comparison);
    # only p90_wm needs to be pulled in from the raw predictions table.
    p = preds[["scheme_code", "cutoff_date", "p90_wm"]]
    m = comp.merge(p, on=["scheme_code", "cutoff_date"], how="left")
    m = m.dropna(subset=["p10_wm", "p90_wm", "actual_wm"])
    if m.empty:
        return {"n": 0}
    within = ((m["actual_wm"] >= m["p10_wm"]) & (m["actual_wm"] <= m["p90_wm"])).mean()
    above_p10 = (m["actual_wm"] >= m["p10_wm"]).mean()
    return {
        "n": int(len(m)),
        "pct_within_p10_p90_band": float(within),
        "pct_above_p10": float(above_p10),
        "note": "P10-P90 is an empirical band from the historical window "
                "distribution, not a parametric confidence interval.",
    }


def main() -> None:
    comp = pd.read_parquet(RESULTS / "backtest_comparison.parquet")
    preds = pd.read_parquet(RESULTS / "backtest_predictions.parquet")
    acts = pd.read_parquet(RESULTS / "backtest_actuals.parquet")
    excl = pd.read_parquet(RESULTS / "exclusion_report.parquet")
    fund_summary = pd.read_parquet(RESULTS / "fund_summary.parquet")
    dq = pd.read_parquet(RESULTS / "data_quality_report.parquet")
    period_summary = pd.read_parquet(RESULTS / "period_summary.parquet")
    model_summary = json.loads((RESULTS / "model_summary.json").read_text())
    accuracy_summary = json.loads((RESULTS / "accuracy_summary.json").read_text())

    report: dict = {
        "model_summary": model_summary,
        "accuracy_summary": accuracy_summary,
        "universe": {
            "n_funds": int(len(dq)),
            "n_cutoffs": int(model_summary["n_cutoffs"]),
            "cutoff_range": [model_summary["first_cutoff"], model_summary["last_cutoff"]],
            "n_predictions": int(model_summary["n_predictions"]),
            "n_validated": int(model_summary["n_validated"]),
            "n_pending": int(model_summary["n_pending"]),
        },
        "overall": {
            "point_forecast": {
                "wm": point_forecast_stats(comp["median_wm"], comp["actual_wm"]),
                "blended_wm": point_forecast_stats(comp["blended_wm"], comp["actual_wm"]),
                "xirr": point_forecast_stats(comp["predicted_xirr_median"], comp["actual_xirr"]),
            },
            "self_persistence": {
                m: self_persistence_stats(preds, m)
                for m in ("median_wm", "p10_wm", "cv", "success_pct", "fqs", "blended_wm")
            },
            "confidence_band": confidence_band_coverage(comp, preds),
        },
        "funds": {},
    }

    for code in FOCUS_FUNDS:
        fc = comp[comp.scheme_code == code].sort_values("cutoff_date")
        fp = preds[preds.scheme_code == code].sort_values("cutoff_date")
        fe = excl[excl.scheme_code == code]
        fdq = dq[dq.scheme_code == code]
        fs = fund_summary[fund_summary.scheme_code == code]

        report["funds"][str(code)] = {
            "scheme_name": fc["scheme_name"].iloc[0] if len(fc) else fp["scheme_name"].iloc[0],
            "internal_category": fc["internal_category"].iloc[0] if len(fc) else None,
            "data_quality": fdq.drop(columns=["long_gaps", "splices"]).to_dict("records")[0]
                            if len(fdq) else {},
            "n_predictions": int(len(fp)),
            "n_validated": int(len(fc)),
            "n_pending": int(len(fp)) - int(len(fc)),
            "exclusion_reasons": fe["exclusion_reason"].value_counts().to_dict(),
            "rolling_window_growth": fp[["cutoff_date", "n_rolling_observations",
                                          "available_history_years"]].to_dict("records"),
            "fund_summary": fs.to_dict("records")[0] if len(fs) else {},
            "point_forecast": {
                "wm": point_forecast_stats(fc["median_wm"], fc["actual_wm"]),
                "blended_wm": point_forecast_stats(fc["blended_wm"], fc["actual_wm"]),
                "xirr": point_forecast_stats(fc["predicted_xirr_median"], fc["actual_xirr"]),
            },
            "self_persistence": {
                m: self_persistence_stats(fp, m)
                for m in ("median_wm", "p10_wm", "cv", "success_pct", "fqs", "blended_wm")
            },
            "confidence_band": confidence_band_coverage(fc, fp),
            "timeseries": fc[[
                "cutoff_date", "median_wm", "blended_wm", "p10_wm", "cv", "success_pct", "fqs",
                "actual_wm", "predicted_xirr_median", "actual_xirr", "predicted_rank",
                "actual_rank", "total_funds",
            ]].to_dict("records"),
        }

    (OUT / "report_data.json").write_text(json.dumps(report, indent=2, default=str))
    print(f"Wrote {OUT / 'report_data.json'}")

    # Flat CSV exports (Phase 10 deliverable)
    comp.to_csv(OUT / "backtest_comparison.csv", index=False)
    preds.to_csv(OUT / "backtest_predictions.csv", index=False)
    acts.to_csv(OUT / "backtest_actuals.csv", index=False)
    excl.to_csv(OUT / "exclusion_report.csv", index=False)
    fund_summary.to_csv(OUT / "fund_summary.csv", index=False)
    dq.drop(columns=["long_gaps", "splices"]).to_csv(OUT / "data_quality_report.csv", index=False)
    period_summary.to_csv(OUT / "period_summary.csv", index=False)
    print(f"Wrote CSV exports to {OUT}")


if __name__ == "__main__":
    main()
