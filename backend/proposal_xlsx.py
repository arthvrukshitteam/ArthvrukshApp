"""Excel exporter for Portfolio Proposals.

Single mode (SIP-only or Lumpsum-only, matching the advisor's Home mode),
single horizon (the advisor's own tenure). Since there's only one horizon,
the sheet is a simple one-row-per-fund table (no more 3-column groups from
the original template) followed by a vertical portfolio-summary table, plus
a Cover sheet (client details, logo, company name/ARN), a Disclaimer sheet
(legally-binding text + client acceptance, placed right after Cover to match
the PDF's page-1 ordering), a Terminology sheet, the plan sheet, and the SWP
sheet (kept as live Excel formulas so an advisor can try a different
withdrawal amount and watch the 120-month schedule recalculate).

SIP/Lumpsum cells are written as plain computed VALUES (already verified
against the template's own formulas in proposal_engine.py / the test suite)
— these numbers come from the app's fund analysis, not from manual entry.

Compliance note: see proposal_pdf.py's module docstring — COMPANY_LEGAL_NAME
and ARN_NUMBER are env-configured, never hardcoded/guessed.
"""

from __future__ import annotations

import indian_format

import os
from io import BytesIO
from typing import Any, Dict, List

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

_ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
LOGO_PATH = os.path.join(_ASSETS_DIR, "arthvruksh_logo.png")

# Defaults per the client. The real AMFI Registration Number (ARN 365554) and
# legal entity name are used as defaults; both remain overridable via the
# ARTHVRUKSH_ARN / ARTHVRUKSH_COMPANY_NAME env vars.
COMPANY_LEGAL_NAME = os.environ.get("ARTHVRUKSH_COMPANY_NAME") or "ArthVruksh MFDGuru LLP"
ARN_NUMBER = os.environ.get("ARTHVRUKSH_ARN") or "365554"

YELLOW = PatternFill("solid", fgColor="FFFFFF00")
BRAND_FILL = PatternFill("solid", fgColor="FF0B1D38")
BRAND_FONT = Font(color="FFFFFFFF", bold=True, size=16)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT_WRAP = Alignment(horizontal="left", vertical="center", wrap_text=True)
BOLD = Font(bold=True)
MODE_LABEL = {"sip": "SIP", "lumpsum": "Lumpsum"}

TERMINOLOGY = [
    ("SIP", "Systematic Investment Plan — a fixed amount invested every month."),
    ("SWP", "Systematic Withdrawal Plan — a fixed amount withdrawn every month from an existing corpus."),
    ("XIRR", "Extended Internal Rate of Return — the annualised return of an investment with irregular or repeated cash flows."),
    ("FQS", "Fund Quality Score — ArthVruksh's composite 0–100 score blending consistency, downside protection and long-term outcomes."),
    ("P10", "The average of all WMs achieved in the worst 10% of rolling outcomes."),
    ("Median", "Expected Median Outcome — the middle result across all historical rolling periods; a typical, not best-case, outcome."),
    ("WM", "Wealth Multiplier — final value divided by amount invested (e.g. 2.0x means the investment doubled)."),
    ("CV", "Coefficient of Variation — standard deviation divided by mean outcome; lower means more consistent returns."),
    ("Benchmark XIRR", "The annualised return that would just meet the fund's category minimum Wealth Multiple bar."),
    ("Windows", "How many historical rolling periods the fund's Median/P10/Success % figures were computed from — more windows means a sturdier sample."),
]

# Verbatim, legally-binding disclaimer text as supplied by the client (an
# AMFI-registered Mutual Fund Distributor). Do not paraphrase or shorten this
# — it is compliance content, not marketing copy. Mirrors proposal_pdf.py.
DISCLAIMER_INTRO = (
    "Mutual Fund investments are subject to market risks, read all scheme related documents "
    "carefully."
)

# Client acceptance. The proposal is delivered electronically and is never
# physically signed, so there is no Name/Signature/Date block — acceptance is
# deemed on delivery and this sentence states that. Verbatim client-supplied
# compliance wording; keep in sync with the identical constant in
# proposal_pdf.py (same convention as DISCLAIMER_INTRO / DISCLAIMER_SECTIONS
# above, which are duplicated between the two renderers).
ACCEPTANCE_STATEMENT = (
    "On receiving this proposal via any mode of communication "
    "(Email/Whatsapp/Any other electronic media), it is deemed fit that you "
    "accept the proposal."
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


def _write_cover(wb: Workbook, doc: Dict[str, Any]) -> None:
    ws = wb.active
    ws.title = "Cover"
    ws.sheet_view.showGridLines = False
    for col, w in zip("ABCDEFGH", [4, 26, 40, 4, 4, 4, 4, 4]):
        ws.column_dimensions[col].width = w

    ws.row_dimensions[2].height = 20
    ws.row_dimensions[3].height = 20
    ws.merge_cells("B2:G3")
    c = ws["B2"]
    c.value = "ArthVruksh — Portfolio Proposal"
    c.font = BRAND_FONT
    c.fill = BRAND_FILL
    c.alignment = CENTER
    for row in ws["B2:G3"]:
        for cell in row:
            cell.fill = BRAND_FILL

    try:
        img = XLImage(LOGO_PATH)
        img.width = 60
        img.height = 60
        ws.add_image(img, "B2")
    except Exception:
        pass

    ws.cell(row=4, column=2, value=f"{COMPANY_LEGAL_NAME}   ·   AMFI Registration Number (ARN): {ARN_NUMBER}")
    ws.cell(row=4, column=2).font = Font(italic=True, size=9, color="FF64748B")

    mode = doc.get("mode", "sip")
    plan = doc.get("plan", {})
    amount_label = "Monthly SIP amount" if mode == "sip" else "Lumpsum amount"

    rows = [
        ("Client Name", doc.get("client_name") or "—"),
        ("Phone Number", doc.get("client_phone") or "—"),
        ("PAN", doc.get("client_pan") or "—"),
        ("Recommended By", COMPANY_LEGAL_NAME),
        ("Date", doc.get("proposal_date") or "—"),
        ("Investment Mode", MODE_LABEL.get(mode, mode)),
        ("Horizon", f"{doc.get('horizon')}Y" if doc.get("horizon") else "—"),
        (amount_label, indian_format.rupees(plan.get("investment_amount", 0))),
        ("Funds in portfolio", str(len(plan.get("funds", [])))),
    ]
    r = 7
    for label, value in rows:
        ws.cell(row=r, column=2, value=label).font = BOLD
        ws.cell(row=r, column=3, value=value)
        r += 1

    r += 1
    ws.cell(row=r, column=2, value=(
        "Generated automatically from ArthVruksh's fund analysis engine. "
        "See the Disclaimer sheet for the legally-binding terms of this proposal."
    )).font = Font(italic=True, size=9, color="FF64748B")


def _write_terminology(wb: Workbook) -> None:
    ws = wb.create_sheet("Terminology")
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 12
    ws.column_dimensions["B"].width = 90

    ws["A1"] = "Term"
    ws["B1"] = "Meaning"
    for coord in ("A1", "B1"):
        ws[coord].fill = BRAND_FILL
        ws[coord].font = Font(bold=True, color="FFFFFFFF")

    for i, (term, defn) in enumerate(TERMINOLOGY, start=2):
        ws.cell(row=i, column=1, value=term).font = BOLD
        cell = ws.cell(row=i, column=2, value=defn)
        cell.alignment = LEFT_WRAP


def _write_plan_sheet(wb: Workbook, doc: Dict[str, Any]) -> None:
    mode = doc.get("mode", "sip")
    plan = doc.get("plan", {})
    horizon = doc.get("horizon")
    funds: List[Dict[str, Any]] = plan.get("funds", [])
    agg = plan.get("aggregate", {})

    ws = wb.create_sheet(MODE_LABEL.get(mode, mode))
    ws.sheet_view.showGridLines = False

    ws.merge_cells("A1:H1")
    ws["A1"] = f"{MODE_LABEL.get(mode, mode)} Investment Plan — {horizon}-Year Horizon"
    ws["A1"].font = Font(bold=True, size=13, color="FF0B1D38")

    headers = ["Fund", "% Allocation", "FQS", "Success %", "Median WM", "XIRR (Median)", "P10 WM", "XIRR (P10)", "CV", "Benchmark XIRR", "Windows"]
    for i, h in enumerate(headers, start=1):
        cell = ws.cell(row=3, column=i, value=h)
        cell.fill = YELLOW
        cell.font = BOLD
        cell.alignment = CENTER

    r = 4
    extrapolated = [f for f in funds if f.get("extrapolated")]
    for f in funds:
        # Asterisk marks a fund whose track record is shorter than the horizon:
        # every figure on this row is measured at its own rolling period
        # (metrics_horizon), while the portfolio wealth extrapolates that
        # period's XIRR out to the full horizon. Footnote written below.
        label = f["scheme_name"] + (" *" if f.get("extrapolated") else "")
        ws.cell(row=r, column=1, value=label).alignment = LEFT_WRAP
        ws.cell(row=r, column=2, value=f["allocation_pct"]).number_format = "0%"
        ws.cell(row=r, column=3, value=f.get("fqs")).number_format = "0.0"
        ws.cell(row=r, column=4, value=f.get("success_pct")).number_format = "0%"
        ws.cell(row=r, column=5, value=f.get("median_wm")).number_format = "0.00"
        ws.cell(row=r, column=6, value=f.get("xirr_median")).number_format = "0.0%"
        ws.cell(row=r, column=7, value=f.get("p10_wm")).number_format = "0.00"
        ws.cell(row=r, column=8, value=f.get("xirr_p10")).number_format = "0.0%"
        ws.cell(row=r, column=9, value=f.get("cv")).number_format = "0.0%"
        ws.cell(row=r, column=10, value=f.get("benchmark_xirr")).number_format = "0.0%"
        ws.cell(row=r, column=11, value=f.get("n_windows")).number_format = "0"
        for c in range(3, 12):
            ws.cell(row=r, column=c).alignment = CENTER
        r += 1

    if extrapolated:
        marked = ", ".join(f"{f['scheme_name']} ({f.get('metrics_horizon')}Y)" for f in extrapolated)
        note = ws.cell(
            row=r, column=1,
            value=(
                f"* {marked} — shorter track record than the {horizon}-year horizon. FQS, Success %, "
                f"Median WM, P10 WM and both XIRRs above are that fund's actual figures at its own "
                f"rolling period, not at {horizon} years. Portfolio expected wealth (Median and P10) "
                f"applies that period's XIRR to the full {horizon}-year horizon."
            ),
        )
        note.alignment = LEFT_WRAP
        note.font = Font(italic=True, size=9, color="FF64748B")
        r += 2

    r += 1
    ws.cell(row=r, column=1, value="Portfolio Summary").font = Font(bold=True, size=12, color="FF0B1D38")
    r += 1
    summary_rows = [
        ("Capital Invested", agg.get("capital_invested"), "#,##0"),
        ("Expected Wealth (Median)", agg.get("expected_wealth_median"), "#,##0"),
        ("Expected Wealth (P10)", agg.get("expected_wealth_p10"), "#,##0"),
        # Label wording kept in sync with the PDF's Portfolio Summary.
        ("Blended WM (Median)", agg.get("blended_wm_median"), "0.0000"),
        ("WM (P10)", agg.get("blended_wm_p10"), "0.0000"),
        ("Blended XIRR (Median)", agg.get("blended_xirr_median"), "0.0%"),
        ("XIRR (P10)", agg.get("blended_xirr_p10"), "0.0%"),
        ("Blended CV (portfolio)", agg.get("blended_cv"), "0.0%"),
    ]
    for label, value, fmt in summary_rows:
        ws.cell(row=r, column=1, value=label).fill = YELLOW
        ws.cell(row=r, column=1).font = BOLD
        cell = ws.cell(row=r, column=2, value=value)
        cell.number_format = fmt
        cell.font = Font(bold=True, color="FF2563EB")
        r += 1

    ws.column_dimensions["A"].width = 46
    for col in "BCDEFGHIJK":
        ws.column_dimensions[col].width = 14


def _write_swp_sheet(wb: Workbook, swp: Dict[str, Any]) -> None:
    ws = wb.create_sheet("SWP")
    ws.sheet_view.showGridLines = False

    ws["B1"] = "CORPUS"
    ws["C1"] = swp.get("corpus", 0)
    ws["B2"] = "RETURN"
    ws["C2"] = swp.get("annual_return", 0)
    ws["C2"].number_format = "0.0%"
    ws["B3"] = "INFLATION"
    ws["C3"] = swp.get("inflation", 0)
    ws["C3"].number_format = "0.0%"
    ws["B4"] = "WITHDRAWAL PER MONTH"
    ws["C4"] = swp.get("monthly_withdrawal", 0)
    ws["B5"] = "WITHDRAWAL PER MONTH WITH INFLATION"
    ws["C5"] = "=C4*(1+C3)/12+C4"

    for coord in ("B1", "B2", "B3", "B4", "B5"):
        ws[coord].fill = YELLOW
        ws[coord].font = BOLD

    note = "corpus auto-derived from the portfolio projection" if swp.get("corpus_auto_derived") else "corpus set by advisor"
    ws["E1"] = note
    ws["E1"].font = Font(italic=True, size=9, color="FF64748B")

    headers = ["Months", "Principal", "Withdrawal per Month with Inflation", "Amount", "Return", "Final Amount"]
    for i, h in enumerate(headers):
        ws.cell(row=7, column=1 + i, value=h).font = BOLD

    years = int(swp.get("years", 10))
    months = max(1, years * 12)
    for m in range(1, months + 1):
        r = 7 + m
        ws.cell(row=r, column=1, value=m)
        if m == 1:
            ws.cell(row=r, column=2, value="=C1")
        else:
            ws.cell(row=r, column=2, value=f"=F{r - 1}")
        ws.cell(row=r, column=3, value="=-$C$5")
        ws.cell(row=r, column=4, value=f"=B{r}+C{r}")
        ws.cell(row=r, column=5, value=f"=D{r}*$C$2/12")
        ws.cell(row=r, column=6, value=f"=D{r}+E{r}")
        for col in (2, 3, 4, 5, 6):
            ws.cell(row=r, column=col).number_format = "#,##0"

    ws.column_dimensions["A"].width = 8
    for col in "BCDEF":
        ws.column_dimensions[col].width = 15


def _write_disclaimer(wb: Workbook, doc: Dict[str, Any]) -> None:
    """Legally-binding disclaimer (verbatim, client-supplied text) followed by
    the Client Acceptance panel — placed right after Cover so it matches the
    PDF's page-1 ordering.

    The panel is a statement, not a form: the former "Accepted by (Client
    Name) / Signature / Date" rows were removed because this proposal is
    delivered electronically and never physically signed. See
    ACCEPTANCE_STATEMENT above and the matching change in proposal_pdf.py."""
    ws = wb.create_sheet("Disclaimer")
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 32
    ws.column_dimensions["B"].width = 100

    ws.merge_cells("A1:B1")
    ws["A1"] = "Disclaimer"
    ws["A1"].font = Font(bold=True, size=13, color="FF0B1D38")

    r = 3
    ws.merge_cells(f"A{r}:B{r}")
    ws.cell(row=r, column=1, value=DISCLAIMER_INTRO).font = Font(bold=True, size=9.5)
    ws.row_dimensions[r].height = 18
    r += 2

    for heading, lines in DISCLAIMER_SECTIONS:
        ws.merge_cells(f"A{r}:B{r}")
        hc = ws.cell(row=r, column=1, value=heading)
        hc.font = Font(bold=True, size=10.5, color="FF0B1D38")
        r += 1
        for label, text in lines:
            ws.cell(row=r, column=1, value=label).font = BOLD
            cell = ws.cell(row=r, column=2, value=text)
            cell.alignment = LEFT_WRAP
            r += 1
        r += 1

    r += 1

    ws.merge_cells(f"A{r}:B{r}")
    ws.cell(row=r, column=1, value="Client Acceptance").font = Font(bold=True, size=12, color="FF0B1D38")
    r += 2
    ws.merge_cells(f"A{r}:B{r}")
    acc = ws.cell(row=r, column=1, value=ACCEPTANCE_STATEMENT)
    acc.font = Font(bold=True, size=9.5)
    acc.alignment = LEFT_WRAP
    ws.row_dimensions[r].height = 30
    r += 3
    ws.cell(row=r, column=1, value="Generated by ArthVruksh Team").font = Font(italic=True, size=9, color="FF64748B")


def build_workbook(doc: Dict[str, Any]) -> bytes:
    """doc is a saved `portfolio_proposals` document (see proposal_routes.py)."""
    wb = Workbook()
    _write_cover(wb, doc)
    _write_disclaimer(wb, doc)
    _write_terminology(wb)
    _write_plan_sheet(wb, doc)
    if doc.get("swp"):  # SWP is optional — add the sheet only when present
        _write_swp_sheet(wb, doc["swp"])

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
