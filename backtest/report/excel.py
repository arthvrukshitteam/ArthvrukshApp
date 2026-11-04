"""Build the client-facing Excel workbook from report_data.json + the raw
result CSVs. Every sheet is a direct rendering of already-computed numbers."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.chart import LineChart, Reference

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS = REPO_ROOT / "backtest_data" / "results"
OUT = REPO_ROOT / "backtest_data" / "report_out"

NAVY = "1B2A4A"
TEAL = "0E7C7B"
LIGHT = "EEF2F6"

HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
HEADER_FILL = PatternFill("solid", fgColor=NAVY)
TITLE_FONT = Font(bold=True, size=16, color=NAVY)
SUB_FONT = Font(italic=True, size=10, color="666666")
THIN = Side(style="thin", color="CCCCCC")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def _style_header(ws, row: int, ncols: int):
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER


def _autosize(ws, max_width=42):
    for col in ws.columns:
        length = max((len(str(c.value)) for c in col if c.value is not None), default=8)
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(max_width, max(10, length + 2))


def _write_df(ws, df: pd.DataFrame, start_row=1, start_col=1, round_floats=4):
    df = df.copy()
    for c in df.columns:
        if df[c].dtype.kind == "f":
            df[c] = df[c].round(round_floats)
    for j, col in enumerate(df.columns):
        ws.cell(row=start_row, column=start_col + j, value=str(col))
    _style_header(ws, start_row, len(df.columns))
    for i, (_, row) in enumerate(df.iterrows(), start=start_row + 1):
        for j, val in enumerate(row):
            cell = ws.cell(row=i, column=start_col + j, value=val)
            cell.border = BORDER
    return start_row + 1 + len(df)


def title_block(ws, title: str, subtitle: str, row=1):
    ws.cell(row=row, column=1, value=title).font = TITLE_FONT
    ws.cell(row=row + 1, column=1, value=subtitle).font = SUB_FONT
    return row + 3


def build() -> Path:
    data = json.loads((OUT / "report_data.json").read_text())
    comp = pd.read_parquet(RESULTS / "backtest_comparison.parquet")
    preds = pd.read_parquet(RESULTS / "backtest_predictions.parquet")
    fund_summary = pd.read_parquet(RESULTS / "fund_summary.parquet")
    dq = pd.read_parquet(RESULTS / "data_quality_report.parquet").drop(
        columns=["long_gaps", "splices"]
    )
    excl = pd.read_parquet(RESULTS / "exclusion_report.parquet")

    wb = Workbook()

    # ── 1. Cover / Methodology ────────────────────────────────────────────
    ws = wb.active
    ws.title = "Overview"
    r = title_block(ws, "AV FQS Model — Historical Out-of-Sample Validation",
                     "Point-in-time backtest of the ArthVruksh Fund Quality Score model")
    ms = data["model_summary"]
    u = data["universe"]
    rows = [
        ("Universe", f"{u['n_funds']} funds — equity core categories + Hybrid, ≥14 years effective (lineage-resolved) history"),
        ("Cutoffs tested", f"{u['n_cutoffs']} monthly cutoffs, {u['cutoff_range'][0]} → {u['cutoff_range'][1]}"),
        ("Rolling period tested", f"{ms['rolling_period_years']} years"),
        ("Validation horizon", f"{ms['validation_horizon_years']} years"),
        ("Minimum history required", f"{ms['min_history_years']} years (derived from server.py's HISTORY_BUFFER_YEARS)"),
        ("SIP amount", f"₹{ms['sip_amount']:,.0f}/month"),
        ("Predictions frozen", f"{u['n_predictions']:,}"),
        ("Validated (future known)", f"{u['n_validated']:,}"),
        ("Pending (future not yet elapsed)", f"{u['n_pending']:,}"),
        ("Stitching direction", ms["stitch_direction"] + " (predecessor history only — no forward look-ahead)"),
        ("Independent periods (validated span)", str(data["accuracy_summary"]["selection_accuracy"]["_meta"]["independent_periods"])),
    ]
    for label, val in rows:
        ws.cell(row=r, column=1, value=label).font = Font(bold=True)
        ws.cell(row=r, column=2, value=val)
        r += 1
    r += 1
    ws.cell(row=r, column=1, value="Methodology").font = Font(bold=True, size=13, color=NAVY)
    r += 1
    method_lines = [
        "1. At each monthly cutoff T, every fund's NAV history is truncated to date <= T (raw, unsanitised — "
        "splice/unit-change adjustment is re-run per cutoff, never on full history, to avoid a later-detected "
        "split leaking backwards).",
        "2. Eligibility and every AV FQS parameter (Median WM, P10 WM, CV, Success %, FQS, Blended WM) are computed "
        "using ONLY windows completed by T, via the model's own fqs_engine.score_horizon() — never reimplemented.",
        "3. The prediction is FROZEN. Only after freezing does the engine look forward.",
        "4. 'Actual' = the ONE realised forward SIP an investor starting the month after T would have completed by "
        "T+5Y, computed with the model's own window construction (so prediction and outcome share the same ruler).",
        "5. Predicted vs Actual is compared per fund per cutoff. Cross-sectional rank statistics are computed WITHIN "
        "each cutoff first, then averaged across cutoffs (Fama-MacBeth), with Newey-West standard errors "
        "(lag = horizon x 12 months) because monthly overlapping 5-year windows are ~98% autocorrelated.",
        "6. CV / Success % / FQS describe the SHAPE of a fund's historical window distribution, not a single "
        "outcome, so they are additionally validated by SELF-PERSISTENCE: does the value at cutoff T predict the "
        "same statistic re-measured once 5 more years of real data exist (cutoff T+5Y)? Both values are real "
        "model output at two different cutoffs — no new formula.",
    ]
    for line in method_lines:
        ws.cell(row=r, column=1, value=line).alignment = Alignment(wrap_text=True)
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
        ws.row_dimensions[r].height = 30
        r += 1
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 60

    # ── 2. Overall accuracy ────────────────────────────────────────────────
    ws2 = wb.create_sheet("Overall Accuracy")
    r = title_block(ws2, "Overall Model Accuracy — Full 164-Fund Universe",
                     "Point-forecast (single realised outcome) and self-persistence (distributional statistic) validation")
    pf_rows = []
    for metric, label in [("wm", "Median WM (point forecast)"), ("blended_wm", "Blended WM (point forecast)"),
                           ("xirr", "XIRR (point forecast)")]:
        s = data["overall"]["point_forecast"][metric]
        pf_rows.append({"Parameter": label, "N": s["n"], "Mean Predicted": s["mean_predicted"],
                         "Mean Actual": s["mean_actual"], "Bias": s["bias"], "Bias Direction": s["bias_direction"],
                         "MAE": s["mae"], "RMSE": s["rmse"], "MAPE": s["mape"], "Correlation (r)": s["correlation"]})
    r = _write_df(ws2, pd.DataFrame(pf_rows), r) + 1

    ws2.cell(row=r, column=1, value="Self-Persistence (distributional statistics: does the value today predict "
                                     "the same statistic in 5 years?)").font = Font(bold=True, size=12, color=NAVY)
    r += 2
    sp_rows = []
    for metric in ("median_wm", "p10_wm", "cv", "success_pct", "fqs", "blended_wm"):
        s = data["overall"]["self_persistence"][metric]
        sp_rows.append({"Parameter": metric, "N": s.get("n"), "Mean at T": s.get("mean_predicted"),
                         "Mean at T+5Y": s.get("mean_actual"), "Bias": s.get("bias"),
                         "MAE": s.get("mae"), "RMSE": s.get("rmse"), "MAPE": s.get("mape"),
                         "Correlation (r)": s.get("correlation")})
    r = _write_df(ws2, pd.DataFrame(sp_rows), r) + 1

    ws2.cell(row=r, column=1, value="Confidence Band Coverage").font = Font(bold=True, size=12, color=NAVY)
    r += 2
    cb = data["overall"]["confidence_band"]
    r = _write_df(ws2, pd.DataFrame([{
        "N": cb["n"], "% of outcomes within model's own P10-P90 band": cb["pct_within_p10_p90_band"],
        "% of outcomes above P10 (downside case)": cb["pct_above_p10"],
    }]), r) + 1
    _autosize(ws2)

    # ── 3. Rank quality & selection accuracy (from analytics.py, unmodified) ─
    ws3 = wb.create_sheet("Ranking & Selection")
    r = title_block(ws3, "Ranking Quality & Selection Accuracy",
                     "Straight from backtest/analytics.py — Spearman rank correlation (Fama-MacBeth, Newey-West), "
                     "and hit rates vs. baselines")
    rq = pd.DataFrame(data["accuracy_summary"]["rank_quality"])
    r = _write_df(ws3, rq, r) + 1
    sa = data["accuracy_summary"]["selection_accuracy"]
    sa_rows = []
    for k, label in [("model_blended_wm", "AV FQS (Blended WM rank)"), ("model_fqs", "AV FQS (headline score)"),
                      ("baseline_trailing_wm", "Naive baseline (last 5Y window only)"),
                      ("baseline_random", "Random chance")]:
        d = sa.get(k, {})
        sa_rows.append({"Strategy": label, **d})
    r = _write_df(ws3, pd.DataFrame(sa_rows), r) + 1
    _autosize(ws3)

    # ── 4. Fund 1 / Fund 2 detail sheets ──────────────────────────────────
    for code, label in [("140225", "Fund 1 - Edelweiss Mid Cap"), ("130502", "Fund 2 - HDFC Small Cap")]:
        f = data["funds"][code]
        ws_f = wb.create_sheet(label[:31])
        r = title_block(ws_f, f["scheme_name"], f"Scheme code {code} — {f['internal_category']}")
        dqf = f["data_quality"]
        info_rows = [
            ("Own-code inception (unstitched)", "see history-debug endpoint; own-code NAV starts later than effective"),
            ("Effective (lineage-stitched) inception", dqf.get("first")),
            ("Latest NAV", dqf.get("last")),
            ("Effective history span", f"{dqf.get('span_years')} years"),
            ("Predecessor scheme(s) stitched",
             "107301 - JPMorgan India Mid and Small Cap Fund" if code == "140225"
             else "108197 - Morgan Stanley A.C.E. Fund"),
            ("Duplicate dates", dqf.get("duplicate_dates")),
            ("Missing months", dqf.get("missing_months")),
            ("Long data gaps", dqf.get("n_long_gaps")),
            ("Predictions frozen", f["n_predictions"]),
            ("Validated (future realised)", f["n_validated"]),
            ("Pending", f["n_pending"]),
            ("Exclusion reasons (pre-eligibility cutoffs)", json.dumps(f["exclusion_reasons"])),
        ]
        for lbl, val in info_rows:
            ws_f.cell(row=r, column=1, value=lbl).font = Font(bold=True)
            ws_f.cell(row=r, column=2, value=val)
            r += 1
        r += 1

        ws_f.cell(row=r, column=1, value="Point-Forecast Accuracy (predicted at T vs. single realised 5Y outcome)").font = Font(bold=True, size=12, color=NAVY)
        r += 2
        pf_rows = []
        for metric, mlabel in [("wm", "Median WM"), ("blended_wm", "Blended WM"), ("xirr", "XIRR")]:
            s = f["point_forecast"][metric]
            pf_rows.append({"Parameter": mlabel, "N": s["n"], "Mean Predicted": s["mean_predicted"],
                             "Mean Actual": s["mean_actual"], "Bias": s["bias"], "Bias Direction": s["bias_direction"],
                             "MAE": s["mae"], "RMSE": s["rmse"], "MAPE": s["mape"], "Correlation (r)": s["correlation"]})
        r = _write_df(ws_f, pd.DataFrame(pf_rows), r) + 1

        ws_f.cell(row=r, column=1, value="Self-Persistence (value at T vs. same statistic at T+5Y)").font = Font(bold=True, size=12, color=NAVY)
        r += 2
        sp_rows = []
        for metric in ("median_wm", "p10_wm", "cv", "success_pct", "fqs", "blended_wm"):
            s = f["self_persistence"][metric]
            sp_rows.append({"Parameter": metric, "N": s.get("n"), "Mean at T": s.get("mean_predicted"),
                             "Mean at T+5Y": s.get("mean_actual"), "Bias": s.get("bias"),
                             "MAE": s.get("mae"), "RMSE": s.get("rmse"), "MAPE": s.get("mape"),
                             "Correlation (r)": s.get("correlation")})
        r = _write_df(ws_f, pd.DataFrame(sp_rows), r) + 1

        ws_f.cell(row=r, column=1, value="Confidence Band Coverage").font = Font(bold=True, size=12, color=NAVY)
        r += 2
        cb = f["confidence_band"]
        r = _write_df(ws_f, pd.DataFrame([{
            "N": cb["n"], "% within P10-P90 band": cb["pct_within_p10_p90_band"],
            "% above P10": cb["pct_above_p10"],
        }]), r) + 1

        ws_f.cell(row=r, column=1, value="Full Cutoff-by-Cutoff Predicted vs. Actual").font = Font(bold=True, size=12, color=NAVY)
        r += 2
        ts = pd.DataFrame(f["timeseries"])
        header_row = r
        r = _write_df(ws_f, ts, r)
        _autosize(ws_f, max_width=20)

        # Embedded line chart: predicted vs actual WM over time
        chart = LineChart()
        chart.title = "Predicted (Median WM) vs Actual WM"
        chart.style = 2
        chart.y_axis.title = "Wealth Multiple"
        chart.x_axis.title = "Cutoff"
        n = len(ts)
        cats = Reference(ws_f, min_col=1, min_row=header_row + 1, max_row=header_row + n)
        pred_col = ts.columns.get_loc("median_wm") + 1
        act_col = ts.columns.get_loc("actual_wm") + 1
        data_pred = Reference(ws_f, min_col=pred_col, min_row=header_row, max_row=header_row + n)
        data_act = Reference(ws_f, min_col=act_col, min_row=header_row, max_row=header_row + n)
        chart.add_data(data_pred, titles_from_data=True)
        chart.add_data(data_act, titles_from_data=True)
        chart.set_categories(cats)
        chart.width, chart.height = 24, 10
        ws_f.add_chart(chart, f"{get_column_letter(len(ts.columns) + 2)}{header_row}")

    # ── 5. Raw data sheets (Phase 10: Excel workbook must contain the
    # historical calculations, predicted values, actual values, accuracy calc)
    for name, df in [
        ("Raw - Comparison", comp),
        ("Raw - Predictions (sample)", preds.head(5000)),
        ("Raw - Fund Summary", fund_summary),
        ("Raw - Data Quality", dq),
        ("Raw - Exclusions (sample)", excl.head(5000)),
    ]:
        wsr = wb.create_sheet(name[:31])
        _write_df(wsr, df, 1)
        _autosize(wsr, max_width=28)

    path = OUT / "AV_FQS_Backtest_Report.xlsx"
    wb.save(path)
    return path


if __name__ == "__main__":
    p = build()
    print(f"Wrote {p}")
