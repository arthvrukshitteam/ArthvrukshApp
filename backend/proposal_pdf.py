"""PDF exporter for Portfolio Proposals.

Single mode (SIP-only or Lumpsum-only, matching the advisor's Home mode),
single horizon (the advisor's own tenure) — one merged fund table (identity +
allocation + performance, no duplicate "composition" table), a portfolio
summary, a terminology primer, and a legally-binding disclaimer + client
acceptance block up front (page 1/2).

Density/attractiveness notes (2026-07 second pass — first redesign fixed the
sparse/unbalanced look but the client flagged it as still too many pages for
the amount of information, and not visually "attractive"):
  - The old cover page had a separate "Portfolio composition" table (Fund /
    Category / Allocation) immediately followed by an "Investment Plan" table
    that repeated the same Fund/Allocation columns plus performance figures.
    These are now ONE table — same information, half the vertical space, and
    no confusing "why are funds listed twice" duplication.
  - A brand-colored "Key Highlights" hero strip sits on the cover, right
    under the client/plan cards, so the client sees the headline numbers
    (capital, expected wealth, return, horizon) before anything else — the
    part of the document people actually want to read first.
  - The SWP year-by-year table used to print every single year (up to 20
    rows for a long horizon). It now prints at most 6 evenly-spaced
    milestone years (always including year 1 and the final year), with
    cumulative withdrawn/growth figures — a client reads 6 milestones far
    more easily than a wall of near-identical rows, and it's a fraction of
    the height. The full month-by-month numbers remain in the Excel export.
  - Terminology is a two-column-of-two-columns grid instead of one tall
    7-row table, roughly halving its footprint.
  - Card/section padding and inter-section spacing were tightened throughout.
  - Section breaks use `CondPageBreak` (only breaks if the section wouldn't
    fit in the remaining space of the current page), not an unconditional
    `PageBreak` after every section.
  - A brand band + footer (with page numbers) are drawn on every page via a
    canvas callback (`_decorate`) instead of repeating a logo/title Paragraph
    in the story.
  - Allocation bars and metric cards cycle through a small accent palette
    instead of a single flat blue, so the document has some visual variety
    instead of reading as a monochrome spreadsheet dump.

Long fund names are wrapped inside table cells via reportlab Paragraphs —
plain strings in a fixed-width Table column silently overflow into the next
column instead of wrapping.

Compliance note: COMPANY_LEGAL_NAME and ARN_NUMBER are read from environment
variables (ARTHVRUKSH_COMPANY_NAME / ARTHVRUKSH_ARN) rather than hardcoded.
These are real regulatory identifiers (AMFI Registration Number, registered
legal entity name) for an AMFI-registered Mutual Fund Distributor — they must
never be guessed or filled with plausible-looking placeholder data. Defaults
below reflect what the client explicitly supplied (2026-07-07); swap the ARN
via the env var once the real AMFI number is available.
"""

from __future__ import annotations

import indian_format

import logging
import os
from functools import lru_cache
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("mf_api.proposal_pdf")

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    CondPageBreak, Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer,
    Table, TableStyle,
)
from reportlab.platypus.flowables import HRFlowable

_ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
LOGO_PATH = os.path.join(_ASSETS_DIR, "arthvruksh_logo.png")


@lru_cache(maxsize=1)
def _logo() -> Optional[Tuple[Any, float]]:
    """The header logo, trimmed of its transparent padding, plus its aspect
    ratio (width / height).

    The source PNG is 500x500 with the artwork occupying only the middle
    ~316x261, so drawing it raw into the 14mm header band wasted roughly 40%
    of the available space and left the mark too small to read. Trimming to
    the opaque bounding box lets the same tile show a much larger logo.

    Pillow ships as a reportlab dependency, but this degrades gracefully to
    the untrimmed file (and then to no logo at all) rather than failing a
    proposal export over artwork.
    """
    try:
        from PIL import Image as PILImage
        from reportlab.lib.utils import ImageReader

        img = PILImage.open(LOGO_PATH)
        box = img.getchannel("A").getbbox() if img.mode in ("RGBA", "LA") else img.getbbox()
        if box:
            img = img.crop(box)
        w, h = img.size
        return ImageReader(img), (w / h if h else 1.0)
    except Exception:
        logger.warning("proposal_pdf: could not trim logo, falling back to raw file", exc_info=True)
        try:
            return LOGO_PATH, 1.0
        except Exception:
            return None

# Defaults per the client. The real AMFI Registration Number (ARN 365554) and
# legal entity name are used as defaults; both remain overridable via the
# ARTHVRUKSH_ARN / ARTHVRUKSH_COMPANY_NAME env vars.
COMPANY_LEGAL_NAME = os.environ.get("ARTHVRUKSH_COMPANY_NAME") or "ArthVruksh MFDGuru LLP"
ARN_NUMBER = os.environ.get("ARTHVRUKSH_ARN") or "365554"

BRAND = colors.HexColor("#0B1D38")
AZURE = colors.HexColor("#2563EB")
TEAL = colors.HexColor("#0D9488")
GREEN = colors.HexColor("#16A34A")
PURPLE = colors.HexColor("#7C3AED")
AMBER = colors.HexColor("#D97706")
ROSE = colors.HexColor("#E11D48")
ACCENT_PALETTE = [AZURE, TEAL, PURPLE, AMBER, ROSE, GREEN]

LIGHT = colors.HexColor("#F1F5F9")
CARD_BG = colors.HexColor("#F8FAFC")
BORDER = colors.HexColor("#CBD5E1")
HAIRLINE = colors.HexColor("#E2E8F0")
MUTED = colors.HexColor("#64748B")

PAGE_W, PAGE_H = landscape(A4)
MARGIN = 14 * mm
TOP_MARGIN = 22 * mm
BOTTOM_MARGIN = 15 * mm
CONTENT_WIDTH = PAGE_W - 2 * MARGIN
BAND_HEIGHT = 14 * mm

styles = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=styles["Title"], textColor=BRAND, fontSize=20, alignment=TA_LEFT, spaceAfter=1)
H2 = ParagraphStyle("H2", parent=styles["Heading2"], textColor=BRAND, fontSize=12.5, spaceBefore=8, spaceAfter=1)
H3 = ParagraphStyle("H3", parent=styles["Heading3"], textColor=AZURE, fontSize=10.5, spaceBefore=4, spaceAfter=2)
BODY = styles["BodyText"]
SMALL = ParagraphStyle("Small", parent=styles["BodyText"], fontSize=8, textColor=MUTED)
SMALL_RIGHT = ParagraphStyle("SmallRight", parent=SMALL, alignment=TA_RIGHT, textColor=BRAND)
CELL = ParagraphStyle("Cell", parent=styles["BodyText"], fontSize=8.3, leading=10.6)
# A Paragraph fills its whole column and wraps left-aligned by default —
# the table's own ALIGN=CENTER TableStyle command only positions the
# Paragraph flowable within the cell, it does not center the text inside
# it. Value columns need this style (not the table-level ALIGN) to actually
# render centered.
CELL_CENTER = ParagraphStyle("CellCenter", parent=CELL, alignment=TA_CENTER)
# Deliberately smaller than CELL, and centered — this is the Investment Plan
# table's header row (see _investment_plan_section). At CELL's 8.3pt, three
# of the twelve column headers ("Windows", "CV (Stability)", and
# "Benchmark XIRR") don't fit their column even as a single word on its own
# line, so reportlab force-splits them mid-word ("Wind-" / "ows") instead of
# wrapping — a real bug, verified with reportlab's own stringWidth() against
# every header/column pair. 7.6pt + the explicit <br/> breaks below (rather
# than relying on automatic wrapping) is what fixes it: every header now has
# >12pt of margin to spare in its narrowest column at this size — see the
# fracs comment in _investment_plan_section for the exact numbers.
CELL_HEADER = ParagraphStyle(
    "CellHeader", parent=CELL, textColor=colors.white, fontName="Helvetica-Bold",
    fontSize=7.6, leading=9.2, alignment=TA_CENTER,
)
CARD_LABEL = ParagraphStyle("CardLabel", parent=SMALL, fontSize=7.4)
CARD_VALUE = ParagraphStyle("CardValue", parent=styles["Heading3"], textColor=BRAND, fontSize=12, spaceBefore=1, spaceAfter=0)
HERO_LABEL = ParagraphStyle("HeroLabel", parent=SMALL, fontSize=7.6, textColor=colors.HexColor("#AFC2E8"))
HERO_VALUE = ParagraphStyle("HeroValue", parent=styles["Heading1"], textColor=colors.white, fontSize=16.5, leading=19, spaceBefore=1, spaceAfter=0)

MODE_LABEL = {"sip": "SIP", "lumpsum": "Lumpsum"}


def _pct(v) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def _num(v, dp=2) -> str:
    return "—" if v is None else f"{v:,.{dp}f}"


def _rupees(v) -> str:
    """Full amount, Indian digit grouping (Rs. 64,80,806 — not Rs. 6,480,806).

    reportlab's built-in Helvetica (WinAnsi encoding) has no glyph for the
    Rupee sign (U+20B9) — it silently renders as a black box. "Rs." is the
    safe ASCII fallback that reads correctly in every PDF viewer without
    embedding a custom Unicode font, so the shared formatter is called with
    that symbol rather than the default.
    """
    return indian_format.rupees(v, ascii_symbol=True)


def _rupees_lakh(v) -> str:
    """Compact form for the hero strip — "Rs. 64.8 Lakh" reads faster than the
    full "Rs. 64,80,806" at a glance, while the exact figure still appears
    in the detailed Portfolio Summary further down."""
    return indian_format.rupees_compact(v, ascii_symbol=True)


def _cell(text: str, header: bool = False, center: bool = False) -> Paragraph:
    """Wrap arbitrary text (fund names in particular) so it never overflows
    a fixed-width table column into its neighbour. `center` renders the text
    centered within its column (used for value/number columns) — headers are
    always centered regardless via CELL_HEADER."""
    style = CELL_HEADER if header else (CELL_CENTER if center else CELL)
    return Paragraph(str(text), style)


def _grid_style(header_rows: int = 1) -> TableStyle:
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, header_rows - 1), BRAND),
        ("FONTSIZE", (0, 0), (-1, -1), 8.3),
        ("ROWBACKGROUNDS", (0, header_rows), (-1, -1), [colors.white, LIGHT]),
        ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3.4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ])


def _section_title(text: str, accent=AZURE) -> List:
    """A section heading with a coloured rule underneath, spanning the full
    content width — reads as a clean divider rather than a bare label."""
    return [
        Paragraph(text, H2),
        HRFlowable(width="100%", thickness=1.3, color=accent, spaceBefore=1, spaceAfter=5),
    ]


ZERO_PAD_CMDS: List[tuple] = [
    ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
]


def _zero_pad(style_extra: Optional[List[tuple]] = None) -> TableStyle:
    base = list(ZERO_PAD_CMDS)
    if style_extra:
        base += style_extra
    return TableStyle(base)


def _card_row(cards: List[Table], widths: List[float], gap: float = 5 * mm) -> Table:
    """Lay out pre-built card Tables side by side with a thin gap between —
    used for the Client/Plan info cards, the hero strip, and metric grids."""
    cells: List[Any] = []
    colw: List[float] = []
    for i, (card, w) in enumerate(zip(cards, widths)):
        cells.append(card)
        colw.append(w)
        if i < len(cards) - 1:
            cells.append("")
            colw.append(gap)
    row = Table([cells], colWidths=colw)
    row.setStyle(_zero_pad())
    return row


def _info_card(title: str, rows: List[Tuple[str, str]], width: float) -> Table:
    inner_rows = [[Paragraph(f"<b>{label}</b>", CELL), Paragraph(str(value), CELL)] for label, value in rows]
    inner = Table(inner_rows, colWidths=[width * 0.40, width * 0.60])
    inner.setStyle(TableStyle([
        ("TOPPADDING", (0, 0), (-1, -1), 2.6), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.6),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, HAIRLINE),
    ]))
    content = [Paragraph(title, H3), inner]
    outer = Table([[content]], colWidths=[width])
    outer.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.8, BORDER),
        ("BACKGROUND", (0, 0), (-1, -1), CARD_BG),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
    ]))
    return outer


def _metric_card(label: str, value: str, width: float, value_color=None) -> Table:
    style = CARD_VALUE if value_color is None else ParagraphStyle(f"CardValue_{id(value_color)}", parent=CARD_VALUE, textColor=value_color)
    content = [Paragraph(label, CARD_LABEL), Paragraph(value, style)]
    outer = Table([[content]], colWidths=[width])
    outer.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.7, BORDER),
        ("BACKGROUND", (0, 0), (-1, -1), CARD_BG),
        ("TOPPADDING", (0, 0), (-1, -1), 5.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
    ]))
    return outer


def _metric_grid(items: List[Tuple[str, str]], width: float, ncols: int = 4, value_colors=None) -> List:
    """value_colors: a single color, a list (cycled per item), or None (uses
    the default CARD_VALUE navy for every card)."""
    story: List = []
    gap = 4.5 * mm
    for i in range(0, len(items), ncols):
        chunk = items[i:i + ncols]
        n = len(chunk)
        card_w = (width - gap * (n - 1)) / n
        cards = []
        for j, (label, value) in enumerate(chunk):
            idx = i + j
            if isinstance(value_colors, list):
                color = value_colors[idx % len(value_colors)]
            else:
                color = value_colors
            cards.append(_metric_card(label, value, card_w, color))
        story.append(_card_row(cards, [card_w] * n, gap))
        story.append(Spacer(1, 2.5 * mm))
    return story


def _portfolio_summary_grid(agg: Dict[str, Any], width: float) -> List:
    """Portfolio Summary in the advisor-specified three-column layout:

        [ Capital Invested ]  [ Expected Wealth (P10) ]  [ Expected Wealth (Median) ]
        [                  ]  [ WM (P10)              ]  [ WM (Median)              ]
        [                  ]  [ XIRR (P10)            ]  [ XIRR (Median)            ]

    Capital Invested sits alone in column 1 because it is the one input figure
    here — everything to its right is an outcome, with the conservative (P10)
    case in the middle column and the median case on the right, so the two
    scenarios read straight down as a pair. Colours are unchanged from the
    previous flat 4-across grid: BRAND for capital, AZURE for wealth, TEAL for
    the multiples, GREEN for the returns.
    """
    gap = 4.5 * mm
    col_w = (width - 2 * gap) / 3
    widths = [col_w] * 3

    rows = [
        # (left card, P10 card, Median card, value colour for the pair)
        (_metric_card("Capital Invested", _rupees(agg.get("capital_invested")), col_w, BRAND),
         ("Expected Wealth (P10)", _rupees(agg.get("expected_wealth_p10"))),
         ("Expected Wealth (Median)", _rupees(agg.get("expected_wealth_median"))),
         AZURE),
        ("",
         ("WM (P10)", _num(agg.get("blended_wm_p10")) + "x"),
         ("WM (Median)", _num(agg.get("blended_wm_median")) + "x"),
         TEAL),
        ("",
         ("XIRR (P10)", _pct(agg.get("blended_xirr_p10"))),
         ("XIRR (Median)", _pct(agg.get("blended_xirr_median"))),
         GREEN),
    ]

    story: List = []
    for left, p10, median, color in rows:
        cards = [
            left,  # "" renders as an empty spacer cell, keeping columns aligned
            _metric_card(p10[0], p10[1], col_w, color),
            _metric_card(median[0], median[1], col_w, color),
        ]
        story.append(_card_row(cards, widths, gap))
        story.append(Spacer(1, 2.5 * mm))
    return story


def _hero_card(label: str, value: str, width: float) -> Table:
    content = [Paragraph(label, HERO_LABEL), Paragraph(value, HERO_VALUE)]
    outer = Table([[content]], colWidths=[width])
    outer.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BRAND),
        ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 11), ("RIGHTPADDING", (0, 0), (-1, -1), 11),
    ]))
    return outer


def _hero_strip(items: List[Tuple[str, str]], width: float) -> Table:
    gap = 4 * mm
    n = len(items)
    card_w = (width - gap * (n - 1)) / n
    cards = [_hero_card(label, value, card_w) for label, value in items]
    return _card_row(cards, [card_w] * n, gap)


def _checkpoint_years(n_years: int, max_points: int = 6) -> List[int]:
    """Up to `max_points` evenly-spaced years (always including year 1 and
    the final year) — used to keep the SWP schedule to a readable handful of
    milestones instead of printing every single year."""
    if n_years <= 0:
        return []
    if n_years <= max_points:
        return list(range(1, n_years + 1))
    step = (n_years - 1) / (max_points - 1)
    pts = {max(1, min(n_years, round(1 + step * i))) for i in range(max_points)}
    pts.add(1)
    pts.add(n_years)
    return sorted(pts)


TERMINOLOGY = [
    ("SIP", "Systematic Investment Plan — a fixed amount invested every month."),
    ("SWP", "Systematic Withdrawal Plan — a fixed amount withdrawn every month from an existing corpus."),
    ("XIRR", "Extended Internal Rate of Return — the annualised return of an investment with irregular cash flows."),
    ("FQS", "Fund Quality Score — ArthVruksh's composite 0–100 score blending consistency, downside protection and outcomes."),
    ("P10", "The average of all WMs achieved in the worst 10% of rolling outcomes."),
    ("Median", "Expected Median Outcome — the middle result across all historical rolling periods, not a best case."),
    ("WM", "Wealth Multiplier — final value divided by amount invested (e.g. 2.0x means the investment doubled)."),
    ("CV", "Coefficient of Variation — standard deviation divided by mean outcome; lower means more consistent returns."),
    ("Benchmark XIRR", "The annualised return that would just meet the fund's category minimum Wealth Multiple bar."),
    ("Windows", "How many historical rolling periods the fund's Median/P10/Success % figures were computed from — more windows means a sturdier sample."),
]


def _terminology_section() -> List:
    """Two side-by-side columns of Term/Meaning pairs instead of one tall
    single-column table — roughly halves the vertical footprint."""
    story: List = [CondPageBreak(45 * mm)]
    story += _section_title("Terminology")
    story.append(Paragraph(
        "Short forms used throughout this proposal, explained once here to keep the tables below compact.",
        SMALL,
    ))
    story.append(Spacer(1, 2.5 * mm))

    mid = (len(TERMINOLOGY) + 1) // 2
    halves = [TERMINOLOGY[:mid], TERMINOLOGY[mid:]]
    half_w = (CONTENT_WIDTH - 5 * mm) / 2
    tables = []
    for half in halves:
        rows = [[_cell("Term", True), _cell("Meaning", True)]]
        rows += [[_cell(term), _cell(defn)] for term, defn in half]
        t = Table(rows, colWidths=[0.19 * half_w, 0.81 * half_w])
        t.setStyle(_grid_style())
        tables.append(t)
    story.append(_card_row(tables, [half_w, half_w], 5 * mm))
    story.append(Spacer(1, 3 * mm))
    return story


def _cover(doc: Dict[str, Any]) -> List:
    mode = doc.get("mode", "sip")
    plan = doc.get("plan", {})
    horizon = doc.get("horizon")
    agg = plan.get("aggregate", {})
    amount_label = "Monthly SIP amount" if mode == "sip" else "Lumpsum amount"
    client_name = doc.get("client_name") or "—"
    proposal_date = doc.get("proposal_date") or "—"

    story: List = []

    title_left = [
        Paragraph("Portfolio Proposal", H1),
        Paragraph(f"{COMPANY_LEGAL_NAME} &nbsp;·&nbsp; AMFI Registration Number (ARN): {ARN_NUMBER}", SMALL),
    ]
    title_right = [
        Paragraph(f"<b>Prepared for:</b> {client_name}", SMALL_RIGHT),
        Paragraph(f"<b>Date:</b> {proposal_date}", SMALL_RIGHT),
    ]
    title_row = Table([[title_left, title_right]], colWidths=[0.62 * CONTENT_WIDTH, 0.38 * CONTENT_WIDTH])
    title_row.setStyle(_zero_pad([("VALIGN", (0, 0), (-1, -1), "BOTTOM")]))
    story.append(title_row)
    story.append(Spacer(1, 2.5 * mm))
    story.append(HRFlowable(width="100%", thickness=1.4, color=BRAND, spaceBefore=0, spaceAfter=6))

    gap = 5 * mm
    card_w = (CONTENT_WIDTH - gap) / 2
    client_card = _info_card("Client", [
        ("Client Name", client_name),
        ("Phone Number", doc.get("client_phone") or "—"),
        ("PAN", doc.get("client_pan") or "—"),
        ("Recommended By", COMPANY_LEGAL_NAME),
    ], card_w)
    plan_card = _info_card("Plan", [
        ("Date", proposal_date),
        ("Investment Mode", MODE_LABEL.get(mode, mode)),
        ("Horizon", f"{horizon}Y" if horizon else "—"),
        (amount_label, _rupees(plan.get("investment_amount"))),
    ], card_w)
    story.append(_card_row([client_card, plan_card], [card_w, card_w], gap))
    story.append(Spacer(1, 5 * mm))

    hero_items = [
        ("Capital Invested", _rupees_lakh(agg.get("capital_invested"))),
        (f"Expected Wealth in {horizon}Y (Median)", _rupees_lakh(agg.get("expected_wealth_median"))),
        ("XIRR (Median)", _pct(agg.get("blended_xirr_median"))),
        ("Conservative Case (P10)", _rupees_lakh(agg.get("expected_wealth_p10"))),
    ]
    story.append(_hero_strip(hero_items, CONTENT_WIDTH))
    story.append(Spacer(1, 4 * mm))
    return story


def _investment_plan_section(doc: Dict[str, Any]) -> List:
    """The single merged fund table: identity (name/category), allocation
    (with an inline bar), and performance figures — replaces what used to be
    two separate tables (a "composition" table on the cover, then a near-
    identical "investment plan" table later) with one."""
    mode = doc.get("mode", "sip")
    plan = doc.get("plan", {})
    horizon = doc.get("horizon")
    funds = plan.get("funds", [])
    agg = plan.get("aggregate", {})

    story: List = [CondPageBreak(60 * mm)]
    story += _section_title(f"{MODE_LABEL.get(mode, mode)} Investment Plan — {horizon}-Year Horizon")

    # Explicit <br/> breaks (not automatic word-wrap) for every multi-word
    # header — deterministic two-line layout instead of leaving it to
    # reportlab to decide where a line breaks, which is what produced the
    # mid-word truncation this replaced (see CELL_HEADER's comment).
    header = [
        "Fund", "Category", "Benchmark<br/>XIRR", "Allocation", "Windows", "FQS",
        "Success %", "Median<br/>WM", "XIRR<br/>(Med)", "P10<br/>WM", "XIRR<br/>(P10)", "CV<br/>(Stability)",
    ]
    rows = [[_cell(h, True) for h in header]]
    # Column widths, in points, were chosen (not eyeballed) so every header
    # — even the single WIDEST word/line in a 2-line header — has real
    # margin to spare at CELL_HEADER's 7.6pt bold, verified with reportlab's
    # stringWidth(): "Windows" needs ~33pt, gets ~51pt; "(Stability)" needs
    # ~35pt, gets ~52pt; "Benchmark" needs ~41pt, gets ~61pt. Fund keeps 20%
    # for long scheme names (already wraps across 2-3 lines for the longest
    # ones, which is fine — that's not what was truncating).
    fracs = [0.2000, 0.0816, 0.0956, 0.0879, 0.0823, 0.0527, 0.0781, 0.0703, 0.0604, 0.0492, 0.0576, 0.0844]
    has_extrapolated = False

    # Group same-category funds together (Small Cap with Small Cap, Large
    # Cap with Large Cap, ...) instead of leaving them in whatever order they
    # were added — alphabetical by category, then most rolling windows
    # (sturdiest data sample) first within each category.
    funds = sorted(funds, key=lambda f: (f.get("category") or "", -(f.get("n_windows") or 0)))

    for f in funds:
        if not f.get("available", True):
            rows.append([_cell(f["scheme_name"]), _cell(f.get("category", ""), center=True), _cell("—", center=True),
                         _cell(_pct(f["allocation_pct"]), center=True),
                         _cell("—", center=True), _cell("—", center=True), _cell("—", center=True),
                         _cell("—", center=True), _cell("—", center=True),
                         _cell("—", center=True), _cell("—", center=True), _cell("—", center=True)])
            continue
        # Additional Recommendation funds whose history is shorter than the
        # tenure: every figure in this row is measured at metrics_horizon, so
        # the row carries an asterisk explained in the footnote below.
        name = f["scheme_name"]
        if f.get("extrapolated"):
            has_extrapolated = True
            name = f"{name} *"
        rows.append([
            _cell(name), _cell(f.get("category", ""), center=True),
            _cell(_pct(f.get("benchmark_xirr")), center=True),
            _cell(_pct(f["allocation_pct"]), center=True),
            _cell(str(f["n_windows"]) if f.get("n_windows") is not None else "—", center=True),
            _cell(_num(f.get("fqs"), 1), center=True), _cell(_pct(f.get("success_pct")), center=True),
            _cell(_num(f.get("median_wm")), center=True), _cell(_pct(f.get("xirr_median")), center=True),
            _cell(_num(f.get("p10_wm")), center=True), _cell(_pct(f.get("xirr_p10")), center=True),
            _cell(_pct(f.get("cv")), center=True),
        ])
    t = Table(rows, colWidths=[f * CONTENT_WIDTH for f in fracs])
    t.setStyle(_grid_style())
    story.append(t)
    story.append(Spacer(1, 2 * mm))

    if has_extrapolated:
        marked = ", ".join(
            f"{f['scheme_name']} ({f.get('metrics_horizon')}Y)"
            for f in funds if f.get("extrapolated")
        )
        story.append(Paragraph(
            f"* {marked} — this fund has a shorter track record than the {horizon}-year horizon. "
            f"FQS, Success %, Median WM, P10 WM and both XIRRs above are its actual figures at its own "
            f"rolling period, not at {horizon} years. The portfolio's expected wealth (median and P10) "
            f"applies that period's XIRR to the full {horizon}-year horizon.",
            SMALL,
        ))
    story.append(Spacer(1, 4 * mm))

    story.append(CondPageBreak(40 * mm))
    story.append(Paragraph("Portfolio Summary", H3))
    story.append(Spacer(1, 1.5 * mm))
    story += _portfolio_summary_grid(agg, CONTENT_WIDTH)

    # Consistency — allocation-weighted across the included funds (see
    # proposal_engine.build_mode_projection's blended_cv). Portfolio-level
    # Benchmark XIRR removed — the per-fund Benchmark XIRR column above
    # already covers this per fund. Positioned to start under the "Expected
    # Wealth" column (not "Capital Invested") to match _portfolio_summary_grid's
    # three-column layout directly above it — an empty spacer card the same
    # width as column 1 keeps the CV card's left edge aligned with column 2.
    cv_gap = 4.5 * mm
    cv_col_w = (CONTENT_WIDTH - 2 * cv_gap) / 3
    cv_card = _metric_card("CV (portfolio)", _pct(agg.get("blended_cv")), 2 * cv_col_w + cv_gap, PURPLE)
    story.append(_card_row(["", cv_card], [cv_col_w, 2 * cv_col_w + cv_gap], cv_gap))
    story.append(Spacer(1, 2.5 * mm))
    return story


def _swp_section(swp: Dict[str, Any]) -> List:
    story: List = [CondPageBreak(60 * mm)]
    story += _section_title("Systematic Withdrawal Plan (SWP)")

    corpus_note = " (auto)" if swp.get("corpus_auto_derived") else ""
    return_note = " (auto)" if swp.get("annual_return_auto_derived") else ""
    assumption_items = [
        ("Corpus" + corpus_note, _rupees(swp.get("corpus"))),
        ("Assumed annual return" + return_note, _pct(swp.get("annual_return"))),
        ("Inflation", _pct(swp.get("inflation"))),
        ("Monthly withdrawal", _rupees(swp.get("monthly_withdrawal"))),
        ("Withdrawal (inflation-adj.)", _rupees(swp.get("withdrawal_with_inflation"))),
    ]
    story += _metric_grid(assumption_items, CONTENT_WIDTH, ncols=5, value_colors=BRAND)
    story.append(Spacer(1, 1.5 * mm))

    rows_all = swp.get("rows") or []
    n_years = len(rows_all) // 12 if rows_all else 0
    checkpoints = _checkpoint_years(n_years, max_points=5)

    header = ["Year", "Total Withdrawn to Date", "Total Growth to Date", "Balance"]
    rows = [[_cell(h, True) for h in header]]
    for y in checkpoints:
        upto = rows_all[:y * 12]
        withdrawn = sum(-r["withdrawal"] for r in upto)
        growth = sum(r["growth"] for r in upto)
        closing = upto[-1]["closing_balance"]
        rows.append([_cell(f"Year {y}"), _cell(_rupees(withdrawn), center=True),
                     _cell(_rupees(growth), center=True), _cell(_rupees(closing), center=True)])
    fracs = [0.14, 0.28, 0.28, 0.30]
    t2 = Table(rows, colWidths=[f * CONTENT_WIDTH for f in fracs])
    t2.setStyle(_grid_style())

    # KeepTogether moves the label+table to the next page as a single unit if
    # it doesn't fit in the remaining space, instead of splitting the table
    # awkwardly and leaving a couple of orphan rows alone on a near-blank page.
    label = Paragraph(
        f"Balance at key milestones out of {n_years} years "
        f"(full month-by-month schedule is in the Excel export):", SMALL,
    )
    story.append(KeepTogether([label, Spacer(1, 2 * mm), t2]))
    return story


# Verbatim, legally-binding disclaimer text as supplied by the client (an
# AMFI-registered Mutual Fund Distributor). Do not paraphrase or shorten this
# — it is compliance content, not marketing copy.
DISCLAIMER_INTRO = (
    "Mutual Fund investments are subject to market risks, read all scheme related documents "
    "carefully."
)

DISCLAIMER_SECTIONS = [
    ("1. Nature of Services", [
        ("Distribution Only", "The presentation or proposal is for informational purposes only."),
        ("No Advice", "It does not constitute formal investment, legal, or tax advice."),
        ("Distributor Status", "The sender operates strictly as an AMFI-registered Mutual Fund Distributor."),
        ("Commission Disclosure", "We receive commissions from Asset Management Companies (AMCs) for distribution."),
        ("Scheme Details", "Scheme-specific commission structures are available upon request."),
    ]),
    ("2. Risk and Performance Acknowledgement", [
        ("Market Risk", "Principal amounts invested can fluctuate based on market forces."),
        ("No Guarantees", "Past performance does not guarantee or indicate future results."),
        ("No Assured Returns", "We do not offer assured returns on any mutual fund scheme."),
        ("Document Review", "Clients must read the Scheme Information Document (SID) before investing."),
        ("Key Information", "Review the Statement of Additional Information (SAI) and Key Information Memorandum (KIM)."),
    ]),
    ("3. Limitation of Liability", [
        ("Client Discretion", "All final investment choices are made solely by the client."),
        ("No Liability", "The MFD accepts no liability for financial losses incurred."),
        ("Data Accuracy", "Content relies on an in-house model called AV-FQS."),
    ]),
    ("4. Bank Charges", [
        ("Mandate Setup Charges", "Your bank may charge Mandate Setup Charges. ArthVruksh or CRESO has no control or say over such charges."),
        ("Sufficient Balance", "Always maintain sufficient balance in your ArthVruksh-linked bank account to ensure that your SIPs do not bounce. ArthVruksh or CRESO shall not be liable for any charges levied by your bank due to a failed or bounced SIP payment."),
    ]),
]

# Client acceptance. This proposal is delivered electronically and is never
# physically signed, so there is no Name/Signature/Date block to fill in —
# acceptance is deemed on delivery, and this sentence is what states that.
# It REPLACES the former signature table; do not reintroduce one alongside it,
# or the document would assert two different acceptance mechanisms.
# Verbatim client-supplied compliance wording — keep in sync with the
# identical constant in proposal_xlsx.py.
ACCEPTANCE_STATEMENT = (
    "On receiving this proposal via any mode of communication "
    "(Email/Whatsapp/Any other electronic media), it is deemed fit that you "
    "accept the proposal."
)

DISCLAIMER_SMALL = ParagraphStyle("DisclaimerSmall", parent=SMALL, fontSize=7.4, leading=9.6)
DISCLAIMER_LABEL = ParagraphStyle("DisclaimerLabel", parent=DISCLAIMER_SMALL, fontName="Helvetica-Bold", textColor=BRAND)
DISCLAIMER_HEAD = ParagraphStyle("DisclaimerHead", parent=styles["Heading3"], textColor=AZURE, fontSize=9, spaceBefore=4, spaceAfter=1.5)


def _disclaimer_and_acceptance(doc: Dict[str, Any]) -> List:
    """Legally-binding disclaimer (verbatim, client-supplied text) followed by
    the Client Acceptance panel. Kept near the top of the document (right
    after the cover) so the acceptance section lands on page 1 or 2, per the
    client's requirement. Boxed so it reads as a self-contained legal panel
    rather than loose running text.

    The acceptance panel used to be a Name / Signature / Date table with ruled
    lines to sign on. It isn't any more: the proposal is delivered
    electronically and never physically signed, so those blank lines were
    asking for something that never happens and implying the proposal wasn't
    in force until they were filled. ACCEPTANCE_STATEMENT states the actual
    mechanism instead. `doc` is still taken (unchanged signature, unchanged
    call site) but no longer needs the client's name for this panel — it is
    already on the cover page."""
    body: List = [Paragraph(DISCLAIMER_INTRO, DISCLAIMER_LABEL), Spacer(1, 1.5 * mm)]
    for heading, lines in DISCLAIMER_SECTIONS:
        body.append(Paragraph(heading, DISCLAIMER_HEAD))
        for label, text in lines:
            body.append(Paragraph(f"<b>{label}:</b> {text}", DISCLAIMER_SMALL))

    box = Table([[body]], colWidths=[CONTENT_WIDTH])
    box.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.8, BORDER),
        ("BACKGROUND", (0, 0), (-1, -1), CARD_BG),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 11), ("RIGHTPADDING", (0, 0), (-1, -1), 11),
    ]))

    at = Paragraph(ACCEPTANCE_STATEMENT, DISCLAIMER_LABEL)
    acceptance_box = Table([[[Paragraph("Client Acceptance", H3), Spacer(1, 1.5 * mm), at]]], colWidths=[CONTENT_WIDTH])
    acceptance_box.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.8, BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 11), ("RIGHTPADDING", (0, 0), (-1, -1), 11),
    ]))

    # The heading, box and acceptance panel are wrapped as ONE atomic unit —
    # if the whole thing doesn't fit in what's left of the current page, it
    # moves together to the next page, instead of the heading landing alone
    # at the bottom of one page (orphaned) with the box stranded on the next.
    block: List = _section_title("Disclaimer")
    block.append(box)
    block.append(Spacer(1, 4 * mm))
    block.append(acceptance_box)
    return [KeepTogether(block)]


def _decorate(canvas, pdf_doc) -> None:
    """Brand band + footer drawn on every page (instead of repeating a logo
    Paragraph in the story). Reads client name/date off `pdf_doc._meta`,
    stashed onto the SimpleDocTemplate instance before build()."""
    meta = getattr(pdf_doc, "_meta", {})
    canvas.saveState()

    canvas.setFillColor(BRAND)
    canvas.rect(0, PAGE_H - BAND_HEIGHT, PAGE_W, BAND_HEIGHT, stroke=0, fill=1)

    # The logo is navy-on-white artwork, so painting it straight onto the navy
    # band made it disappear. Give it its own white rounded tile to sit on —
    # the mark then reads clearly on every page and in any PDF viewer.
    pad = 1.1 * mm
    tile = 11.6 * mm
    tile_x = MARGIN
    tile_y = PAGE_H - BAND_HEIGHT + (BAND_HEIGHT - tile) / 2.0
    canvas.setFillColor(colors.white)
    canvas.roundRect(tile_x, tile_y, tile, tile, 1.6 * mm, stroke=0, fill=1)

    logo = _logo()
    if logo is not None:
        src, aspect = logo
        # Fit inside the tile's padded box without distortion, then centre it.
        box = tile - 2 * pad
        w, h = (box, box / aspect) if aspect >= 1 else (box * aspect, box)
        try:
            canvas.drawImage(
                src, tile_x + (tile - w) / 2.0, tile_y + (tile - h) / 2.0,
                width=w, height=h, mask="auto", preserveAspectRatio=True,
            )
        except Exception:
            logger.warning("proposal_pdf: logo draw failed", exc_info=True)

    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", 12.5)
    canvas.drawString(tile_x + tile + 3.5 * mm, PAGE_H - BAND_HEIGHT + 4.3 * mm, "ArthVruksh")
    canvas.setFont("Helvetica", 7.8)
    canvas.drawRightString(
        PAGE_W - MARGIN, PAGE_H - BAND_HEIGHT + 4.6 * mm,
        f"{meta.get('doc_label', 'Portfolio Proposal')} · {meta.get('client_name', '—')} · {meta.get('proposal_date', '')}",
    )

    canvas.setStrokeColor(HAIRLINE)
    canvas.line(MARGIN, BOTTOM_MARGIN - 3.5 * mm, PAGE_W - MARGIN, BOTTOM_MARGIN - 3.5 * mm)
    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica", 7.3)
    canvas.drawString(MARGIN, BOTTOM_MARGIN - 7.5 * mm, "Generated by ArthVruksh Team")
    canvas.drawRightString(PAGE_W - MARGIN, BOTTOM_MARGIN - 7.5 * mm, f"Page {canvas.getPageNumber()}")
    canvas.restoreState()


def build_pdf(doc: Dict[str, Any]) -> bytes:
    buf = BytesIO()
    pdf = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=MARGIN, rightMargin=MARGIN, topMargin=TOP_MARGIN, bottomMargin=BOTTOM_MARGIN,
        title="ArthVruksh Portfolio Proposal",
        # Machine-readable reference so a later Portfolio Holding Review can
        # load this exact proposal instead of re-reading its text. Older PDFs
        # without it are still parsed from their text (review_pdf_parser).
        subject="ArthVruksh Portfolio Proposal",
        keywords=f"arthvruksh:proposal:{doc['proposal_id']}:v1" if doc.get("proposal_id") else "",
    )
    pdf._meta = {
        "client_name": doc.get("client_name") or "—",
        "proposal_date": doc.get("proposal_date") or "",
    }
    story: List = []
    story += _cover(doc)
    # Terminology (compact) comes right after the hero strip — it's short
    # enough to fill the rest of page 1 under the highlights instead of
    # leaving it blank, before the (longer, boxed) Disclaimer/Acceptance
    # panel which usually needs a fresh page of its own.
    story += _terminology_section()
    story += _disclaimer_and_acceptance(doc)
    story += _investment_plan_section(doc)
    if doc.get("swp"):  # SWP is optional — render the section only when present
        story += _swp_section(doc["swp"])
    pdf.build(story, onFirstPage=_decorate, onLaterPages=_decorate)
    return buf.getvalue()
