"""Clean, minimal version of the dynamic validation report: just Period 1
(historical) outcomes vs Period 2 (new) outcomes, side by side, per fund.
Reuses dynamic_validation_data.json as-is — no new calculation, just a
leaner presentation with the NAV-mechanics disclaimers stripped out."""
from __future__ import annotations

import json
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, HRFlowable,
)
from reportlab.lib.enums import TA_CENTER

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT = REPO_ROOT / "backtest_data" / "report_out"

NAVY = colors.HexColor("#1B2A4A")
TEAL = colors.HexColor("#0E7C7B")
GREY = colors.HexColor("#666666")
GREEN = colors.HexColor("#3E8E5A")
RED = colors.HexColor("#B23A48")

FUND_LABELS = {"113177": "Fund 1 — Nippon India Small Cap Fund", "112932": "Fund 2 — Mirae Asset Large & Mid Cap Fund"}


def _styles():
    ss = getSampleStyleSheet()
    ss.add(ParagraphStyle("H1", parent=ss["Heading1"], textColor=NAVY, fontSize=19, spaceAfter=8))
    ss.add(ParagraphStyle("H2", parent=ss["Heading2"], textColor=NAVY, fontSize=13, spaceBefore=12, spaceAfter=6))
    ss.add(ParagraphStyle("Body", parent=ss["BodyText"], fontSize=10, leading=14, spaceAfter=6))
    ss.add(ParagraphStyle("Small", parent=ss["BodyText"], fontSize=8.5, leading=12, textColor=GREY))
    ss.add(ParagraphStyle("Cover", parent=ss["Title"], fontSize=24, textColor=NAVY, spaceAfter=6))
    ss.add(ParagraphStyle("CoverSub", parent=ss["Normal"], fontSize=12, textColor=TEAL, alignment=TA_CENTER))
    return ss


def _table(rows, col_widths=None, fs=9.5):
    body = ParagraphStyle("B", fontSize=fs, leading=fs + 3, textColor=colors.black)
    head = ParagraphStyle("H", fontSize=fs, leading=fs + 3, textColor=colors.white, fontName="Helvetica-Bold")
    wrapped = [[Paragraph(str(c), head if i == 0 else body) for c in row] for i, row in enumerate(rows)]
    t = Table(wrapped, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CCCCCC")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F7FA")]),
    ]))
    return t


def _f(v, nd=3):
    if v is None:
        return "N/A"
    if isinstance(v, bool):
        return "Yes" if v else "No"
    return f"{v:,.{nd}f}" if isinstance(v, float) else str(v)


def _pct(v):
    return "N/A" if v is None else f"{v * 100:,.1f}%"


def _matchcell(match):
    if match is None:
        return "N/A"
    return "✓ MATCH" if match else "✗ NO MATCH"


def build_fund_section(story, ss, code: str, v: dict):
    h, p, a, c = v["historical"], v["predicted"], v["actual"], v["comparison"]
    period1 = f"{h['fund_inception_date']} → {h['prediction_date']}"
    period2 = a["actual_validation_duration"].replace(" to ", " → ")

    story.append(Paragraph(FUND_LABELS[code], ss["H1"]))
    story.append(Paragraph(f"{h['fund_name']}  ·  {h['category']}  ·  Scheme code {code}", ss["Small"]))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(f"<b>Period 1 (Historical):</b> {period1}", ss["Body"]))
    story.append(Paragraph(f"<b>Period 2 (New):</b> {period2}", ss["Body"]))
    story.append(Spacer(1, 4 * mm))

    def r(label, key, is_pct=False, nd=3):
        d = c[key]
        fmt = _pct if is_pct else lambda x: _f(x, nd)
        return [label, fmt(d["predicted"]), fmt(d["actual"]), fmt(d["abs_diff"]) if not is_pct else _pct(d["abs_diff"]),
                _pct(d["pct_diff"]), _matchcell(d["match"])]

    rows = [
        ["Parameter", "Period 1 (Historical)", "Period 2 (New)", "Abs. Diff", "% Diff", "Match"],
        r("Median WM", "median_wm"),
        r("Blended WM", "blended_wm"),
        r("CV", "cv"),
        r("Success %", "success_pct", is_pct=True),
        r("FQS", "fqs", nd=1),
        r("Benchmark WM", "benchmark_wm"),
    ]
    story.append(_table(rows, col_widths=[32 * mm, 34 * mm, 32 * mm, 26 * mm, 22 * mm, 24 * mm]))

    story.append(Spacer(1, 4 * mm))
    fr = c["fund_rank"]
    rows2 = [
        ["Parameter", "Period 1 (Historical)", "Period 2 (New)", "Match"],
        ["Fund Rank", f"#{fr['predicted']} of {fr['predicted_of']}", f"#{fr['actual']} of {fr['actual_of']}",
         f"Moved {fr['abs_diff']:+d} places"],
        ["Recommendation", c["recommendation"]["predicted"], c["recommendation"]["actual"],
         _matchcell(c["recommendation"]["match"])],
        ["Single Realised 5Y SIP Outcome (WM)", _f(p["wm"]), _f(a["wm_single_realised_outcome"]),
         _matchcell(round(p["wm"], 2) == round(a["wm_single_realised_outcome"], 2))],
    ]
    story.append(_table(rows2, col_widths=[48 * mm, 42 * mm, 42 * mm, 38 * mm]))

    n_match = sum(1 for k in ("median_wm", "blended_wm", "cv", "success_pct", "fqs") if c[k]["match"])
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(
        f"<b>Verdict:</b> {n_match}/5 headline parameters matched exactly between Period 1 and "
        f"Period 2. Fund rank moved from #{fr['predicted']} to #{fr['actual']} "
        f"({fr['abs_diff']:+d} places).", ss["Body"]))
    story.append(PageBreak())


def build() -> Path:
    data = json.loads((OUT / "dynamic_validation_data.json").read_text())
    ss = _styles()
    story = []

    story += [
        Spacer(1, 60 * mm),
        Paragraph("AV FQS Model", ss["Cover"]),
        Paragraph("Period 1 vs Period 2 Rolling Outcome Comparison", ss["CoverSub"]),
        Spacer(1, 4 * mm),
        Paragraph("5-Year Rolling Data &nbsp;|&nbsp; Nippon India Small Cap Fund &amp; "
                  "Mirae Asset Large &amp; Mid Cap Fund", ss["CoverSub"]),
        Spacer(1, 20 * mm),
        Paragraph(
            "Period 1 = 5-year rolling outcomes computed from the fund's historical NAV data. "
            "Period 2 = the same outcomes, computed the same way, once the next 5 years of real "
            "data existed. Every number below comes directly from the production AV FQS model.",
            ss["Body"]),
        Paragraph(
            "Note on \"Match\": this column is EXACT value equality. Continuous financial metrics "
            "essentially never land on the identical decimal twice, so 0/5 exact matches is "
            "expected, not a failure signal — the % Diff column is the meaningful read of how "
            "close Period 1 predicted Period 2.", ss["Small"]),
        PageBreak(),
    ]

    for code in data:
        build_fund_section(story, ss, code, data[code])

    story.append(Paragraph("Overall Conclusion", ss["H1"]))
    for code in data:
        v = data[code]
        c = v["comparison"]
        n_match = sum(1 for k in ("median_wm", "blended_wm", "cv", "success_pct", "fqs") if c[k]["match"])
        story.append(Paragraph(
            f"<b>{FUND_LABELS[code]}:</b> {n_match}/5 parameters matched exactly; "
            f"FQS moved from {v['predicted']['fqs']} to {v['actual']['fqs']}; "
            f"rank moved from #{c['fund_rank']['predicted']} to #{c['fund_rank']['actual']}.",
            ss["Body"]))
    story.append(Spacer(1, 6 * mm))
    story.append(HRFlowable(width="100%", color=GREY))

    doc = SimpleDocTemplate(
        str(OUT / "AV_FQS_Period_Comparison_Simple.pdf"), pagesize=A4,
        topMargin=20 * mm, bottomMargin=18 * mm, leftMargin=18 * mm, rightMargin=18 * mm,
        title="AV FQS — Period 1 vs Period 2 Comparison",
    )
    doc.build(story)
    return OUT / "AV_FQS_Period_Comparison_Simple.pdf"


if __name__ == "__main__":
    p = build()
    print(f"Wrote {p}")
