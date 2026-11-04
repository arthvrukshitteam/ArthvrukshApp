"""PDF exporter for a Rolling Analysis run — a client-shareable snapshot.

Reuses proposal_pdf.py's brand styling (colours, table style) but stays
lightweight (no client details, no SWP, no disclaimer/acceptance block —
this is an advisor/research document, not a sales proposal). Mirrors the
on-screen expanded fund view exactly: the server-generated plain-English key
points, a "By Period" section (one labelled mini-table per period, Overall
highlighted — the PDF's answer to the on-screen PeriodCard), then the
consolidated "Comparison Table" (all periods + Overall in one grid).
"""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table
from reportlab.platypus.flowables import HRFlowable

from proposal_pdf import AZURE, H1, H2, H3, SMALL, BODY, _cell, _grid_style

PAGE_W, PAGE_H = landscape(A4)
MARGIN = 14 * mm
CONTENT_WIDTH = PAGE_W - 2 * MARGIN

MODE_LABEL = {"sip": "SIP", "lumpsum": "Lumpsum"}


def _pct(v) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def _num(v, dp=2) -> str:
    return "—" if v is None else f"{v:,.{dp}f}"


def _wm(v) -> str:
    return "—" if v is None else f"{v:,.2f}x"


_PERIOD_BLOCK_COLS = ["P10 WM", "Median WM", "Blended WM", "Success %", "CV", "Min WM Req.", "FQS"]


def _period_block(label: str, dates: str, p: Dict[str, Any], highlight: bool) -> Any:
    """One period's stats as its own labelled mini-table — the PDF's answer
    to the on-screen PeriodCard. `highlight=True` for the Overall (full
    range) baseline block, styled the same way its table row is elsewhere."""
    n_windows = p.get("n_windows", 0)
    header = Paragraph(f"{label} &nbsp;·&nbsp; {dates} &nbsp;·&nbsp; {n_windows} windows", H3)

    if not n_windows:
        empty = Paragraph("No rolling windows start inside this period — try a wider range.", SMALL)
        return KeepTogether([header, empty, Spacer(1, 3 * mm)])

    vals = [
        _wm(p.get("p10_wm")), _wm(p.get("median_wm")), _wm(p.get("blended_wm")),
        _pct(p.get("success_pct")), _pct(p.get("cv")), _wm(p.get("min_wm_required")),
        _num(p.get("fqs"), 0),
    ]
    rows = [[_cell(c, True) for c in _PERIOD_BLOCK_COLS], [_cell(v) for v in vals]]
    t = Table(rows, colWidths=[CONTENT_WIDTH / len(_PERIOD_BLOCK_COLS)] * len(_PERIOD_BLOCK_COLS))
    style = _grid_style()
    if highlight:
        style.add("BACKGROUND", (0, 0), (-1, 0), AZURE)
        style.add("BOX", (0, 0), (-1, -1), 1.2, AZURE)
    t.setStyle(style)
    return KeepTogether([header, t, Spacer(1, 3 * mm)])


def build_fund_detail_flowables(
    title: str,
    meta_line: str,
    periods: List[Dict[str, Any]],
    overall: Optional[Dict[str, Any]],
    key_points: List[str],
    start_date: str,
    end_date: str,
) -> List[Any]:
    """The exact single-fund Rolling Analysis section (title, meta line, key
    points, period-by-period table with the Overall row highlighted, and the
    methodology footnote) — as its own building block so a multi-fund PDF can
    stack this same section once per fund, unchanged. Used by both
    build_rolling_analysis_pdf below and rolling_analysis_multi_pdf.py."""
    story: List[Any] = []

    story.append(Paragraph(title, H1))
    story.append(Paragraph(meta_line, SMALL))
    story.append(Spacer(1, 2 * mm))
    story.append(HRFlowable(width="100%", thickness=1.2, color=AZURE, spaceBefore=1, spaceAfter=6))

    if key_points:
        story.append(Paragraph("Key Points", H2))
        for pt in key_points:
            story.append(Paragraph(f"•&nbsp;&nbsp;{pt}", BODY))
        story.append(Spacer(1, 4 * mm))

    if periods or overall:
        story.append(Paragraph("By Period", H2))
        if overall:
            story.append(_period_block("Overall (Full Range Baseline)", f"{start_date} → {end_date}", overall, True))
        for p in periods:
            story.append(_period_block(
                p.get("label", ""), f"{p.get('start_date', '')} → {p.get('end_date', '')}", p, False,
            ))
        story.append(Spacer(1, 2 * mm))

    story.append(Paragraph("Comparison Table", H2))
    header = ["Period", "Dates", "Windows", "P10 WM", "Median WM", "Blended WM", "Success %", "CV", "Min WM Req.", "FQS"]
    fracs = [0.11, 0.16, 0.07, 0.09, 0.10, 0.10, 0.10, 0.08, 0.10, 0.07]
    rows = [[_cell(h, True) for h in header]]

    def _row(label: str, dates: str, p: Dict[str, Any]) -> List[Any]:
        return [
            _cell(label),
            _cell(dates),
            _cell(str(p.get("n_windows", 0))),
            _cell(_wm(p.get("p10_wm"))),
            _cell(_wm(p.get("median_wm"))),
            _cell(_wm(p.get("blended_wm"))),
            _cell(_pct(p.get("success_pct"))),
            _cell(_pct(p.get("cv"))),
            _cell(_wm(p.get("min_wm_required"))),
            _cell(_num(p.get("fqs"), 0)),
        ]

    for p in periods:
        rows.append(_row(p.get("label", ""), f"{p.get('start_date', '')} → {p.get('end_date', '')}", p))
    if overall:
        rows.append(_row("Overall", f"{start_date} → {end_date}", overall))

    t = Table(rows, colWidths=[frac * CONTENT_WIDTH for frac in fracs])
    style = _grid_style()
    if overall:
        # Highlight the Overall baseline row (last row) so it reads as the
        # reference line the periods above are being compared against.
        style.add("BACKGROUND", (0, len(rows) - 1), (-1, len(rows) - 1), AZURE)
        style.add("TEXTCOLOR", (0, len(rows) - 1), (-1, len(rows) - 1), colors.white)
    t.setStyle(style)
    story.append(t)

    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(
        "Each period pools every rolling window whose START date falls inside that period's dates — a window's "
        "own end date can fall outside the period (e.g. a 5Y window starting in 2017 ends in 2022). Figures use "
        "ONLY NAV data inside the overall date range above, not the fund's full live history.",
        SMALL,
    ))
    return story


def build_rolling_analysis_pdf(doc: Dict[str, Any]) -> bytes:
    """`doc` is a `rolling_analysis_runs` document (see
    rolling_analysis_routes.py) — one fund, its period-by-period stats, and
    the server-generated key points, all already computed."""
    buf = BytesIO()
    pdf = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=MARGIN, rightMargin=MARGIN, topMargin=18 * mm, bottomMargin=15 * mm,
        title="ArthVruksh Rolling Analysis",
    )

    mode = doc.get("mode", "sip")
    meta_line = (
        f"{doc.get('category', '')} · {MODE_LABEL.get(mode, mode)} · {doc.get('window_years')}Y rolling period · "
        f"NAV data {doc.get('start_date')} to {doc.get('end_date')} · "
        f"{'custom periods' if doc.get('custom_buckets') else 'auto (1 year each)'}"
    )
    story = build_fund_detail_flowables(
        title=f"ArthVruksh · Rolling Analysis — {doc.get('scheme_name', '')}",
        meta_line=meta_line,
        periods=doc.get("periods") or [],
        overall=doc.get("overall"),
        key_points=doc.get("key_points") or [],
        start_date=doc.get("start_date", ""),
        end_date=doc.get("end_date", ""),
    )

    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph(
        f"Generated by ArthVruksh Team · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        SMALL,
    ))

    pdf.build(story)
    return buf.getvalue()
