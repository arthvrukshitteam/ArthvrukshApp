"""Generate publication-quality charts from report_data.json (compute.py's
output). Every chart plots real, already-computed numbers — nothing here
performs new financial calculations."""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT = REPO_ROOT / "backtest_data" / "report_out"
CHARTS = OUT / "charts"
CHARTS.mkdir(parents=True, exist_ok=True)

NAVY = "#1B2A4A"
TEAL = "#0E7C7B"
AMBER = "#C97B2E"
GREY = "#8A8F98"
RED = "#B23A48"
GREEN = "#3E8E5A"

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 10,
    "axes.edgecolor": "#444444",
    "axes.labelcolor": "#222222",
    "axes.titlesize": 12,
    "axes.titleweight": "bold",
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "savefig.facecolor": "white",
    "axes.grid": True,
    "grid.color": "#E5E5E5",
    "grid.linewidth": 0.7,
})


def _save(fig, name: str) -> str:
    path = CHARTS / f"{name}.png"
    fig.tight_layout()
    fig.savefig(path, dpi=170, bbox_inches="tight")
    plt.close(fig)
    return str(path)


def _short(name: str, n: int = 32) -> str:
    return name if len(name) <= n else name[: n - 1].rstrip() + "…"


def fund_rolling_window_timeline(fund_code: str, data: dict) -> str:
    rows = pd.DataFrame(data["funds"][fund_code]["rolling_window_growth"])
    rows["cutoff_date"] = pd.to_datetime(rows["cutoff_date"])
    name = _short(data["funds"][fund_code]["scheme_name"])

    fig, ax = plt.subplots(figsize=(8, 3.6))
    ax.plot(rows["cutoff_date"], rows["n_rolling_observations"], color=NAVY, linewidth=2)
    ax.fill_between(rows["cutoff_date"], rows["n_rolling_observations"], color=NAVY, alpha=0.08)
    ax.set_title(f"Rolling 5Y windows available over time — {name}")
    ax.set_ylabel("Number of rolling windows")
    ax.xaxis.set_major_locator(mdates.YearLocator(2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.set_ylim(bottom=0)
    return _save(fig, f"{fund_code}_window_timeline")


def fund_predicted_vs_actual_wm(fund_code: str, data: dict) -> str:
    ts = pd.DataFrame(data["funds"][fund_code]["timeseries"])
    ts["cutoff_date"] = pd.to_datetime(ts["cutoff_date"])
    name = _short(data["funds"][fund_code]["scheme_name"])

    fig, ax = plt.subplots(figsize=(8, 3.8))
    ax.plot(ts["cutoff_date"], ts["median_wm"], color=NAVY, linewidth=1.8, label="Predicted (Median WM, historical)")
    ax.fill_between(ts["cutoff_date"], ts["p10_wm"], ts["median_wm"], color=NAVY, alpha=0.08)
    ax.plot(ts["cutoff_date"], ts["actual_wm"], color=AMBER, linewidth=1.8, marker="o", markersize=3,
            label="Actual (realised forward 5Y SIP)")
    ax.set_title(f"Predicted vs. Actual Wealth Multiple — {name}")
    ax.set_ylabel("Wealth Multiple (x)")
    ax.legend(loc="upper left", fontsize=8, frameon=False)
    ax.xaxis.set_major_locator(mdates.YearLocator(2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    return _save(fig, f"{fund_code}_predicted_vs_actual_wm")


def fund_prediction_error(fund_code: str, data: dict) -> str:
    ts = pd.DataFrame(data["funds"][fund_code]["timeseries"])
    ts["cutoff_date"] = pd.to_datetime(ts["cutoff_date"])
    ts["error"] = ts["actual_wm"] - ts["median_wm"]
    name = _short(data["funds"][fund_code]["scheme_name"])

    fig, ax = plt.subplots(figsize=(8, 3.2))
    colors = [GREEN if e >= 0 else RED for e in ts["error"]]
    ax.bar(ts["cutoff_date"], ts["error"], color=colors, width=25)
    ax.axhline(0, color="#333333", linewidth=1)
    ax.set_title(f"Prediction error over time (Actual − Predicted WM) — {name}")
    ax.set_ylabel("Error (Wealth Multiple)")
    ax.xaxis.set_major_locator(mdates.YearLocator(2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    return _save(fig, f"{fund_code}_prediction_error")


def fund_fqs_trend(fund_code: str, data: dict) -> str:
    ts = pd.DataFrame(data["funds"][fund_code]["timeseries"])
    ts["cutoff_date"] = pd.to_datetime(ts["cutoff_date"])
    name = _short(data["funds"][fund_code]["scheme_name"])

    fig, ax1 = plt.subplots(figsize=(8, 3.6))
    ax1.plot(ts["cutoff_date"], ts["fqs"], color=TEAL, linewidth=2, label="FQS")
    ax1.set_ylabel("FQS (0-100)", color=TEAL)
    ax1.tick_params(axis="y", labelcolor=TEAL)
    ax1.set_ylim(0, 100)

    ax2 = ax1.twinx()
    ax2.plot(ts["cutoff_date"], ts["success_pct"] * 100, color=AMBER, linewidth=1.6,
             linestyle="--", label="Success %")
    ax2.set_ylabel("Success % of windows", color=AMBER)
    ax2.tick_params(axis="y", labelcolor=AMBER)
    ax2.set_ylim(0, 100)
    ax2.grid(False)

    ax1.set_title(f"FQS and Success % at each cutoff — {name}")
    ax1.xaxis.set_major_locator(mdates.YearLocator(2))
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    return _save(fig, f"{fund_code}_fqs_trend")


def fund_predicted_vs_actual_scatter(fund_code: str, data: dict) -> str:
    ts = pd.DataFrame(data["funds"][fund_code]["timeseries"])
    name = _short(data["funds"][fund_code]["scheme_name"])
    stats = data["funds"][fund_code]["point_forecast"]["wm"]

    fig, ax = plt.subplots(figsize=(5.6, 5.6))
    ax.scatter(ts["median_wm"], ts["actual_wm"], color=NAVY, alpha=0.6, s=28, edgecolor="white", linewidth=0.4)
    lo = min(ts["median_wm"].min(), ts["actual_wm"].min()) * 0.95
    hi = max(ts["median_wm"].max(), ts["actual_wm"].max()) * 1.05
    ax.plot([lo, hi], [lo, hi], color=GREY, linestyle="--", linewidth=1.2, label="Perfect prediction")
    ax.set_xlim(lo, hi)
    ax.set_ylim(lo, hi)
    ax.set_xlabel("Predicted (Median WM at cutoff)")
    ax.set_ylabel("Actual (realised WM)")
    ax.set_title(f"Predicted vs. Actual — {name}\nr = {stats['correlation']:.2f}, RMSE = {stats['rmse']:.3f}")
    ax.legend(loc="upper left", fontsize=8, frameon=False)
    ax.set_aspect("equal", adjustable="box")
    return _save(fig, f"{fund_code}_scatter")


def cross_fund_accuracy_bars(data: dict) -> str:
    codes = list(data["funds"].keys())
    names = [data["funds"][c]["scheme_name"][:28] for c in codes]
    rmse = [data["funds"][c]["point_forecast"]["wm"]["rmse"] for c in codes]
    mape = [data["funds"][c]["point_forecast"]["wm"]["mape"] * 100 for c in codes]
    corr = [data["funds"][c]["point_forecast"]["wm"]["correlation"] for c in codes]

    fig, axes = plt.subplots(1, 3, figsize=(11, 3.6))
    for ax, vals, title, color in zip(
        axes, (rmse, mape, corr),
        ("RMSE (Wealth Multiple)", "MAPE (%)", "Correlation (r)"),
        (NAVY, AMBER, TEAL),
    ):
        ax.bar(names, vals, color=color)
        ax.set_title(title)
        ax.tick_params(axis="x", rotation=20)
    fig.suptitle("Cross-Fund Accuracy Comparison — Predicted vs. Actual Wealth Multiple", fontweight="bold")
    return _save(fig, "cross_fund_accuracy")


def overall_selection_accuracy_bars(data: dict) -> str:
    sa = data["accuracy_summary"]["selection_accuracy"]
    strategies = ["model_blended_wm", "model_fqs", "baseline_trailing_wm", "baseline_random"]
    labels = ["AV FQS\n(Blended WM rank)", "AV FQS\n(headline score)", "Naive baseline\n(last 5Y only)", "Random\nchance"]
    top10 = [sa.get(s, {}).get("top10pct_hit_rate", 0) * 100 for s in strategies]
    top20 = [sa.get(s, {}).get("top20pct_hit_rate", 0) * 100 for s in strategies]

    x = range(len(strategies))
    fig, ax = plt.subplots(figsize=(7.5, 4))
    w = 0.35
    ax.bar([i - w / 2 for i in x], top10, width=w, color=NAVY, label="Top-10% hit rate")
    ax.bar([i + w / 2 for i in x], top20, width=w, color=TEAL, label="Top-20% hit rate")
    ax.axhline(10, color=RED, linestyle=":", linewidth=1)
    ax.axhline(20, color=RED, linestyle=":", linewidth=1, alpha=0.6)
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, fontsize=9)
    ax.set_ylabel("Hit rate (%)")
    ax.set_title("Selection Accuracy — Model vs. Baselines (full 164-fund universe)")
    ax.legend(loc="upper right", fontsize=8, frameon=False)
    return _save(fig, "overall_selection_accuracy")


def overall_rank_quality_bars(data: dict) -> str:
    rq = data["accuracy_summary"]["rank_quality"]
    df = pd.DataFrame(rq)
    fig, ax = plt.subplots(figsize=(8, 4))
    colors = [GREEN if r >= 0 else RED for r in df["mean_rho"]]
    ax.barh(df["metric"], df["mean_rho"], color=colors)
    ax.axvline(0, color="#333333", linewidth=1)
    ax.set_xlabel("Mean Spearman ρ (per-cutoff, averaged) — higher/right is better")
    ax.set_title("Does each parameter predict the fund's future rank?\n(full universe, all validated cutoffs)")
    return _save(fig, "overall_rank_quality")


def fund_timeline_overview(data: dict) -> str:
    """Fund-history timeline: own-code vs effective (stitched) inception, for
    every fund in the universe that required stitching, plus the two focus
    funds explicitly labelled."""
    fig, ax = plt.subplots(figsize=(9, 3.2))
    labels, own_start, eff_start, end = [], [], [], []
    for code, f in data["funds"].items():
        dq = f["data_quality"]
        labels.append(f["scheme_name"][:34])
        eff_start.append(pd.Timestamp(dq["first"]))
        end.append(pd.Timestamp(dq["last"]))

    for i, (lab, s, e) in enumerate(zip(labels, eff_start, end)):
        ax.barh(i, (e - s).days, left=s, color=TEAL, height=0.5)
    ax.set_yticks(range(len(labels)))
    ax.set_yticklabels(labels, fontsize=9)
    ax.set_title("Effective (lineage-stitched) history — Focus Funds 1 & 2")
    ax.xaxis.set_major_locator(mdates.YearLocator(2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    return _save(fig, "fund_timeline_overview")


def main() -> None:
    data = json.loads((OUT / "report_data.json").read_text())
    paths = {}
    for code in data["funds"]:
        paths[f"{code}_window_timeline"] = fund_rolling_window_timeline(code, data)
        paths[f"{code}_predicted_vs_actual_wm"] = fund_predicted_vs_actual_wm(code, data)
        paths[f"{code}_prediction_error"] = fund_prediction_error(code, data)
        paths[f"{code}_fqs_trend"] = fund_fqs_trend(code, data)
        paths[f"{code}_scatter"] = fund_predicted_vs_actual_scatter(code, data)
    paths["cross_fund_accuracy"] = cross_fund_accuracy_bars(data)
    paths["overall_selection_accuracy"] = overall_selection_accuracy_bars(data)
    paths["overall_rank_quality"] = overall_rank_quality_bars(data)
    paths["fund_timeline_overview"] = fund_timeline_overview(data)

    (OUT / "chart_index.json").write_text(json.dumps(paths, indent=2))
    print(f"Wrote {len(paths)} charts to {CHARTS}")


if __name__ == "__main__":
    main()
