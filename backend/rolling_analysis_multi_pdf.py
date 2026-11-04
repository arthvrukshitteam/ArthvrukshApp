"""PDF exporter for a multi-fund Rolling Analysis consistency ranking run.

Sibling to rolling_analysis_pdf.py (single-fund) — same brand styling, same
"advisor/research document, not a sales proposal" scope. Page 1 is a RANKING
across all funds (Rank, Fund, Category, cross-period CV, WM spread, overall
Blended WM/FQS, periods scored) plus the server-generated key points
summarising the pool. Every fund after that gets its own full detail
section — title, meta line, key points, period-by-period table — the exact
same section single-fund Rolling Analysis produces, one fund after another
(one per page), via build_fund_detail_flowables.
"""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, PageBreak, SimpleDocTemplate, Spacer, Table
from reportlab.platypus.flowables import HRFlowable

from proposal_pdf import AZURE, H1, H2, SMALL, BODY, _cell, _grid_style
from rolling_analysis_pdf import build_fund_detail_flowables

PAGE_W, PAGE_H = landscape(A4)
MARGIN = 14 * mm
CONTENT_WIDTH = PAGE_W - 2 * MARGIN

MODE_LABEL = {"sip": "SIP", "lumpsum": "Lumpsum"}


def _pct(v) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def _wm(v) -> str:
    return "—" if v is None else f"{v:,.2f}x"


def _num(v, dp=0) -> str:
    return "—" if v is None else f"{v:,.{dp}f}"


def build_rolling_multi_pdf(doc: Dict[str, Any]) -> bytes:
    """`doc` is a `rolling_multi_runs` document (see rolling_analysis_routes.py)
    — a pool of funds, each already period-bucketed and scored, ranked by
    cross-period consistency."""
    buf = BytesIO()
    pdf = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=MARGIN, rightMargin=MARGIN, topMargin=18 * mm, bottomMargin=15 * mm,
        title="ArthVruksh Rolling Consistency Ranking",
    )

    mode = doc.get("mode", "sip")
    funds: List[Dict[str, Any]] = doc.get("funds") or []
    key_points: List[str] = doc.get("key_points") or []

    story: List[Any] = []

    story.append(Paragraph("ArthVruksh · Rolling Analysis — Consistency Ranking", H1))
    story.append(Paragraph(
        f"{MODE_LABEL.get(mode, mode)} · {doc.get('window_years')}Y rolling window · "
        f"NAV data {doc.get('start_date')} to {doc.get('end_date')} · {doc.get('n_periods')} comparison periods · "
        f"{doc.get('scored_count')} of {doc.get('candidate_count')} candidate funds scored",
        SMALL,
    ))
    story.append(Spacer(1, 2 * mm))
    story.append(HRFlowable(width="100%", thickness=1.2, color=AZURE, spaceBefore=1, spaceAfter=6))

    if key_points:
        story.append(Paragraph("Key Points", H2))
        for pt in key_points:
            story.append(Paragraph(f"•&nbsp;&nbsp;{pt}", BODY))
        story.append(Spacer(1, 4 * mm))

    story.append(Paragraph("Ranked by Cross-Period Consistency", H2))
    header = ["Rank", "Fund", "Category", "Periods Scored", "Cross-Period CV", "WM Spread", "Overall Blended WM", "Overall FQS"]
    fracs = [0.05, 0.30, 0.14, 0.10, 0.12, 0.10, 0.10, 0.09]
    rows = [[_cell(h, True) for h in header]]
    for f in funds:
        overall = f.get("overall") or {}
        name = f.get("scheme_name", "") + (" ★" if f.get("manually_added") else "")
        rows.append([
            _cell(str(f.get("rank", ""))),
            _cell(name),
            _cell(f.get("category", "")),
            _cell(str(f.get("periods_scored", 0))),
            _cell(_pct(f.get("cross_period_cv"))),
            _cell(_wm(f.get("wm_spread"))),
            _cell(_wm(overall.get("blended_wm"))),
            _cell(_num(overall.get("fqs"), 0)),
        ])
    t = Table(rows, colWidths=[frac * CONTENT_WIDTH for frac in fracs])
    t.setStyle(_grid_style())
    story.append(t)

    star_note = " ★ = added manually via search, not from the auto-selected pool." if any(
        f.get("manually_added") for f in funds
    ) else ""
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(
        "Cross-Period CV = standard deviation ÷ mean of a fund's Blended WM across its scored comparison "
        "periods — lower means the fund performed more similarly period-to-period. WM Spread is the simple gap "
        "between its best and worst period's Blended WM. Funds with fewer than 2 scored periods are shown but "
        f"excluded from the ranking itself (sorted last).{star_note}",
        SMALL,
    ))

    # One full detail section per fund — same title/meta/key-points/table a
    # single-fund Rolling Analysis PDF produces, stacked one after another
    # (one fund per page) in ranked order.
    date_range_line = (
        f"{MODE_LABEL.get(mode, mode)} · {doc.get('window_years')}Y rolling period · "
        f"NAV data {doc.get('start_date')} to {doc.get('end_date')} · "
        f"{'custom periods' if doc.get('custom_buckets') else 'auto (1 year each)'}"
    )
    for f in funds:
        story.append(PageBreak())
        rank = f.get("rank")
        rank_label = f"#{rank}" if f.get("cross_period_cv") is not None else "Unranked"
        star = " ★" if f.get("manually_added") else ""
        title = f"ArthVruksh · Rolling Analysis — {rank_label} · {f.get('scheme_name', '')}{star}"
        meta_line = f"{f.get('category', '')} · {date_range_line}"
        story.extend(build_fund_detail_flowables(
            title=title,
            meta_line=meta_line,
            periods=f.get("periods") or [],
            overall=f.get("overall"),
            key_points=f.get("key_points") or [],
            start_date=doc.get("start_date", ""),
            end_date=doc.get("end_date", ""),
        ))

    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph(
        f"Generated by ArthVruksh Team · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        SMALL,
    ))

    pdf.build(story)
    return buf.getvalue()
