"""Bulk Fund Analysis — Excel upload -> existing FQS/XIRR engine -> Excel export.

Orchestration only. This module never computes an FQS, XIRR, or rolling
window itself — every number written into the output workbook comes from
one of two places, both already the single source of truth elsewhere in the
app:

  1. `fund_nav_meta.fqs_by_horizon["10"]` — the nightly batch cache written
     by fqs_refresh.py (same collection the Home screen reads).
  2. `_build_analysis()` / `_cached_analysis()` in server.py — the exact
     functions GET /api/funds/{code}/analyze uses, injected in here from
     server.py (see bulk_analysis_routes.build_router), mirroring how
     proposal_routes.py reuses them for the Portfolio Proposal feature.

The only genuinely new logic this feature introduces is: matching an Excel
row to a fund, picking which of the two sources above to read, and the
Remarks classification (get_fund_remark).

THE DOWNLOAD
────────────
One sheet: the advisor's own uploaded sheet with our analysis merged into it,
rendered in ArthVruksh styling so it can go to a client as-is. Nothing they
uploaded is dropped, a column of theirs that means the same as one of ours is
filled rather than duplicated, and our columns sit left-to-right in the order
the client specified. See APP_COLUMNS for the exact contract and
build_output_workbook for why the sheet is rebuilt rather than edited in
place.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

import amfi_client
import fund_status
import lineage_store
import proposal_engine
from xirr_utils import sip_wm_to_xirr

logger = logging.getLogger("bulk_analysis")

JOBS_COLLECTION = "bulk_analysis_jobs"

# Fixed by the feature spec — "FQS based on 10 Y Rolling period".
ROLLING_HORIZON_YEARS = 10

# ─── The application's own columns ───────────────────────────────────────────
#
# THE OUTPUT CONTRACT, in one place.
#
# The download is ONE sheet: the advisor's uploaded sheet with our analysis
# merged into it. Three rules, in this order:
#
#   1. Nothing the advisor uploaded is lost. Every column they sent survives,
#      with its own header text and its own values.
#   2. No column is duplicated. When a column they already have corresponds to
#      one of ours, we use THEIRS — we do not add a second one beside it. The
#      matching is by keyword, not exact text, because real sheets say "FQS
#      BASED ON 10 YR ROLLING" and "Weightage", not our canonical wording.
#   3. Our columns appear in the fixed order below, left to right; their
#      remaining columns follow, in the order they sent them.
#
# The first eleven names and their order are the client's specification and
# must not be reordered. "Remarks" is ours and sits last so the eleven stay
# contiguous and in the specified sequence.
APP_COLUMNS: List[str] = [
    "Fund Name",
    "Category",
    "Benchmark XIRR",
    "Allocation",
    "No of Windows",
    "FQS",
    "Success%",
    "Median WM",
    "XIRR (Med)",
    "P10 WM",
    "P10 XIRR",
    "CV (Stability)",
    "Remarks",
]

# Columns where the UPLOAD is the authority and we only fill blanks.
#
# These describe the advisor's client and their portfolio, not the fund: the
# name as they refer to it, the category wording their firm uses, and the
# allocation they actually hold. Overwriting any of those would silently
# restate their client's portfolio, so the app's value is used ONLY where the
# cell is empty. Every other column below is ours and is always (re)written —
# that is what makes re-uploading a previously processed file refresh its
# numbers instead of leaving last month's.
USER_OWNED_COLUMNS = {"Fund Name", "Category", "Allocation"}

# Which existing header counts as "the same column as ours".
#
# Each entry is a list of alternatives; an alternative matches when EVERY one
# of its keywords appears in the normalised header. Keyword sets beat exact
# text because advisors' own templates never use our wording — a real upload
# had "FQS BASED ON 10 YR ROLLING" and "MEDIAN XIRR". Deliberately chosen so
# no header can satisfy two different app columns:
#   "Median XIRR"    -> XIRR (Med)      (has "med" + "xirr", no "wm")
#   "Median WM"      -> Median WM       (has "median" + "wm", no "xirr")
#   "P10 XIRR"       -> neither         (no "wm", no "med") — kept as the
#                                        advisor's own column, untouched
#   "Benchmark XIRR" -> Benchmark XIRR  (matched on "benchmark" alone)
_APP_COLUMN_KEYWORDS: Dict[str, Tuple[Tuple[str, ...], ...]] = {
    "Benchmark XIRR": (("benchmark",),),
    "No of Windows":  (("window",),),
    "FQS":            (("fqs",),),
    "Success%":       (("success",),),
    "Median WM":      (("median", "wm"),),
    "XIRR (Med)":     (("xirr", "med"),),
    "P10 WM":         (("p10", "wm"),),
    "P10 XIRR":       (("p10", "xirr"),),
    "CV (Stability)": (("cv",), ("stability",)),
    "Remarks":        (("remark",),),
}

# Short keywords are matched as whole words. "cv" as a bare substring would
# claim any header that happens to contain those two letters; "wm" likewise.
_WORD_BOUNDARY_KEYWORDS = {"cv", "wm", "p10"}

# App columns matched against a set of EXACT normalised headers instead of
# keywords — these are the ones whose real-world spellings are well known and
# where a loose keyword ("name", "category") would swallow unrelated columns
# such as "Client Name" or "Category of Risk".
_APP_COLUMN_EXACT: Dict[str, set] = {}   # populated below, after the sets exist

REVIEW_SHEET_TITLE = "Portfolio Review"

# Number formats mirror proposal_xlsx.py's plan sheet — the app's existing
# Excel-export convention, so both deliverables read the same.
APP_NUMBER_FORMAT: Dict[str, str] = {
    "Benchmark XIRR": "0.0%",
    "Allocation":     "0.0%",
    "No of Windows":  "0",
    "FQS":            "0.0",
    "Success%":       "0.0%",
    "Median WM":      "0.00",
    "XIRR (Med)":     "0.0%",
    "P10 WM":         "0.00",
    "P10 XIRR":       "0.0%",
    "CV (Stability)": "0.0%",
}

APP_COLUMN_WIDTH: Dict[str, float] = {
    "Fund Name":      44.0,
    "Category":       18.0,
    "Benchmark XIRR": 15.0,
    "Allocation":     12.0,
    "No of Windows":  13.0,
    # Wide enough for the short-history annotation ("71.2 (7 YR)"), not just
    # the bare score — right-aligned text that doesn't fit overflows LEFT and
    # collides with the previous column.
    "FQS":            13.0,
    "Success%":       11.0,
    "Median WM":      12.0,
    "XIRR (Med)":     12.0,
    "P10 WM":         11.0,
    "P10 XIRR":       12.0,
    "CV (Stability)": 14.0,
    "Remarks":        34.0,
}

# Columns read as prose rather than figures — left-aligned like the fund name.
_TEXT_COLUMNS = {"Fund Name", "Category", "Remarks"}


NAME_HEADER_CANDIDATES = {"fund name", "scheme name", "fund", "scheme", "name"}
CODE_HEADER_CANDIDATES = {"scheme code", "amfi code", "fund code", "code", "amc code"}
ISIN_HEADER_CANDIDATES = {"isin", "isin code", "isin no", "isin number"}

# Columns the REVIEW SHEET carries across from the upload rather than
# computing. Deliberately kept out of _find_identifier_columns: those three
# decide which fund a row IS (a wrong guess matches the wrong fund), whereas
# these two are descriptive only — a wrong guess costs a wrong label, and the
# app's own value is used whenever the upload has nothing usable.
CATEGORY_HEADER_CANDIDATES = {
    "category", "fund category", "scheme category", "asset class",
    "sub category", "sub-category", "subcategory", "fund type",
}
ALLOCATION_HEADER_CANDIDATES = {
    "allocation", "allocation %", "allocation%", "% allocation", "allocation (%)",
    "alloc", "alloc %", "weight", "weightage", "weight %", "% weight",
    "weight (%)", "% of portfolio", "portfolio %",
}

# Now that the candidate sets above exist, wire them into the app-column
# matcher: these three are the columns an upload most often already has.
_APP_COLUMN_EXACT.update({
    "Fund Name":  NAME_HEADER_CANDIDATES,
    "Category":   CATEGORY_HEADER_CANDIDATES,
    "Allocation": ALLOCATION_HEADER_CANDIDATES,
})


# ─── ArthVruksh branding ─────────────────────────────────────────────────────
# Imported from proposal_xlsx rather than restated, so the Portfolio Review
# download and the Portfolio Proposal carry the same legal name, the same ARN
# and the same logo file. A compliance detail that differs between two
# deliverables from the same firm is a defect, so there is one source.
from proposal_xlsx import (  # noqa: E402
    ARN_NUMBER,
    COMPANY_LEGAL_NAME,
    LOGO_PATH,
)

BRAND_NAVY   = "FF0B1D38"
BRAND_AZURE  = "FF2563EB"
BAND_FILL    = "FFEEF2F7"   # light banding; still legible printed mono
BORDER_GREY  = "FFD8DEE8"
MUTED_TEXT   = "FF64748B"

_TIER_COLOUR = {            # matches get_fund_remark's own thresholds
    "bad":  "FFB91C1C",
    "mid":  "FFB45309",
    "good": "FF15803D",
}

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10MB — generous for a fund-list spreadsheet, keeps job docs well under Mongo's 16MB doc limit
MAX_PREVIEW_FUNDS = 25
ROW_CONCURRENCY = 6   # mirrors fqs_refresh.py's per-job NAV-fetch concurrency
PROGRESS_BATCH = 25   # rows processed between progress-doc updates


# ─── Remarks (the one piece of new business logic) ───────────────────────────

# Single source of the FQS action thresholds — also read by the Portfolio
# Holding Review observation engine so the two features can never disagree.
FQS_BAD_BELOW = 75
FQS_GOOD_FROM = 85
REMARK_BAD = "Bad — Should Discontinue"
REMARK_MID = "Not Bad, but better options available"
REMARK_GOOD = "Good — Should Continue"
# The upload correctly identified the client's exact holding (by ISIN, which
# survives AMC renames and distinguishes plan variants that share a name
# stem) — it is not unmatched and not missing data. _build_analysis() still
# refuses to score it, same as GET /funds/{code}/analyze would for this
# scheme: Direct plans are out of scope for this MFD tool (no trail
# commission, so nothing to advise on). Without this remark the row looks
# identical to a genuine "Calculation unavailable" — insufficient NAV
# history — which misleads the advisor into thinking the fund (or its data)
# couldn't be found, when actually it was found precisely and excluded on
# purpose. See get_fund_result's except-branch for how `reason` is set.
REMARK_DIRECT_PLAN = "Direct Plan — not scored (ArthVruksh rates Regular plans only); ask client to switch to Regular"


def remark_for_too_new(months: Optional[int], first_month: Optional[str]) -> str:
    """A fund that computed cleanly (real NAV, no exception) but still has
    zero rolling windows — too young for even the shortest horizon, not a
    matching problem. See get_fund_result's reason="too_new" branch."""
    if first_month:
        return f"Fund too new — NAV history only since {first_month}, not enough for a rolling window yet"
    if months is not None:
        return f"Fund too new — only {months} month(s) of NAV history, not enough for a rolling window yet"
    return "Fund too new — not enough NAV history for a rolling window yet"


def get_fund_remark(fqs: Optional[float]) -> str:
    """Single source of truth for the Remarks column.

    FQS < 75            -> "Bad — Should Discontinue"
    75 <= FQS < 85       -> "Not Bad, but better options available"
    FQS >= 85            -> "Good — Should Continue"
    FQS is None          -> "Calculation unavailable" (never classified as Bad —
                             the fund wasn't scored, that's not the same as scoring poorly)
    """
    if fqs is None:
        return "Calculation unavailable"
    if fqs < FQS_BAD_BELOW:
        return REMARK_BAD
    if fqs < FQS_GOOD_FROM:
        return REMARK_MID
    return REMARK_GOOD


# ─── Small helpers ────────────────────────────────────────────────────────────

def _norm_header(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def safe_filename(name: str) -> str:
    """Filename-safe token — mirrors proposal_routes.py's _slug helper.
    Never exposes or depends on any filesystem path."""
    base = "".join(c if c.isalnum() or c in (" ", "_", "-", ".") else "" for c in str(name or "")).strip()
    return base or "upload.xlsx"


def convert_xls_to_xlsx_bytes(content: bytes) -> bytes:
    """Legacy .xls is a completely different binary format that openpyxl
    cannot read at all — xlrd is the only lightweight reader for it. Cell
    VALUES only (no styles/formulas) are transcribed into a fresh .xlsx
    workbook; this is a one-time upload-time conversion, not a change to any
    file the user has. Acceptable trade-off: legacy-format uploads lose
    their original formatting but keep every value."""
    import xlrd  # lazy import — only needed on the .xls path

    book = xlrd.open_workbook(file_contents=content)
    sheet = book.sheet_by_index(0)
    wb = Workbook()
    ws = wb.active
    for r in range(sheet.nrows):
        for c in range(sheet.ncols):
            ws.cell(row=r + 1, column=c + 1, value=sheet.cell_value(r, c))
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _find_identifier_columns(header_row: List[Any]) -> Dict[str, Optional[int]]:
    """0-indexed column positions for the identifier columns this feature
    understands. Missing ones stay None — never guessed."""
    positions: Dict[str, Optional[int]] = {"name": None, "code": None, "isin": None}
    for idx, raw in enumerate(header_row):
        h = _norm_header(raw)
        if not h:
            continue
        if positions["name"] is None and h in NAME_HEADER_CANDIDATES:
            positions["name"] = idx
        if positions["code"] is None and h in CODE_HEADER_CANDIDATES:
            positions["code"] = idx
        if positions["isin"] is None and h in ISIN_HEADER_CANDIDATES:
            positions["isin"] = idx
    return positions


# How far down to look for the header row. Deep enough for a title block and
# a couple of blank spacers, shallow enough that a sheet with no identifier
# column fails fast with a clear message instead of scanning 10,000 rows.
HEADER_SCAN_ROWS = 10

# Stamped into the workbook's document properties by build_output_workbook,
# and read back by read_source_sheet. It says one thing: "this file came out
# of this feature."
#
# It exists so re-uploading our own download works. That download ends with a
# blank row and a couple of merged footnote lines; without a way to recognise
# them they parse as two more fund rows ("Fund not found" ×2, and a progress
# bar that counts to the wrong total). Guessing from row shape alone would be
# wrong on the advisor's own files — a sheet with only a Fund Name column and
# blank rows between AMC groups looks identical to a footnote. So the trailing
# notes are stripped ONLY when this marker proves the file is ours, and an
# unmarked file is never second-guessed.
WORKBOOK_MARKER = "arthvruksh-portfolio-review"


def find_header_row(rows: List[List[Any]]) -> int:
    """Index of the row that is actually the header, within the first
    HEADER_SCAN_ROWS rows. Returns 0 when nothing better is found, so the
    caller's own validation reports the problem.

    Row 1 is NOT assumed to be the header. Two things break that assumption,
    and both are ordinary:

      • Advisors' own portfolio exports frequently open with a title, a client
        name and a blank spacer before the real header.
      • THIS FEATURE'S OWN DOWNLOAD does — it has a branded title block above
        the table (see build_output_workbook). Re-uploading last month's
        reviewed file to refresh its numbers is an obvious thing to do, and
        assuming row 1 made it fail with "No fund identifier column found".

    A row qualifies as the header when it names a column we can identify a
    fund by — that is the same condition the rest of the job needs anyway, so
    a header we accept here is always one we can actually use.
    """
    for idx, row in enumerate(rows[:HEADER_SCAN_ROWS]):
        if not row or all(v in (None, "") for v in row):
            continue
        cols = _find_identifier_columns(list(row))
        if any(cols[k] is not None for k in ("name", "code", "isin")):
            return idx
    return 0


def parse_allocation(value: Any) -> Optional[float]:
    """An allocation cell as a 0..1 fraction, or None when it isn't a number.

    Advisors' own sheets write the same thing three ways — 0.25, 25, and
    "25%" — and Excel itself stores a percent-formatted 25% as 0.25. Reading
    them all as-is would put a 25 into a cell formatted "0.0%" and render
    "2500.0%". The rule: anything above 1 is read as a percentage and scaled;
    1 and below is already a fraction. 100%-in-one-fund (a single-fund
    portfolio) is the one ambiguous input and resolves correctly to 1.0
    either way.
    """
    if value in (None, ""):
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        had_percent = text.endswith("%")
        text = text.rstrip("%").strip()
        if not text:
            return None
        try:
            num = float(text)
        except ValueError:
            return None
        if had_percent:
            return num / 100.0
    else:
        try:
            num = float(value)
        except (TypeError, ValueError):
            return None
    if num > 1:
        return num / 100.0
    return num


def _keyword_hit(header_norm: str, keyword: str) -> bool:
    """Whether one keyword is present in a normalised header.

    Short keywords are matched as whole words (see _WORD_BOUNDARY_KEYWORDS):
    a bare "cv" substring would claim any header containing those letters.
    """
    if keyword in _WORD_BOUNDARY_KEYWORDS:
        return re.search(rf"\b{re.escape(keyword)}\b", header_norm) is not None
    return keyword in header_norm


def _header_matches_app_column(header_norm: str, app_column: str) -> bool:
    """Whether an uploaded header IS our `app_column` under another name."""
    if not header_norm:
        return False
    exact = _APP_COLUMN_EXACT.get(app_column)
    if exact is not None:
        return header_norm in exact
    for alternative in _APP_COLUMN_KEYWORDS.get(app_column, ()):
        if all(_keyword_hit(header_norm, kw) for kw in alternative):
            return True
    return False


def plan_columns(header_row: List[Any]) -> Dict[str, Any]:
    """Work out the download's column layout from the uploaded header row.

    Returns:
        order    — list of output columns, left to right. Each is
                   {"name": header text to print,
                    "app": app column name or None,
                    "source": 0-indexed column in the upload, or None}
        app_at   — {app column name: index into `order`}

    The layout rule (see APP_COLUMNS' comment): our columns first, in the
    specified order, each bound to the advisor's existing column when they
    already have one; then every column of theirs we did not claim, in their
    original order.

    A claimed column keeps the advisor's OWN header text — the brief says not
    to rename their columns, so "FQS BASED ON 10 YR ROLLING" stays exactly
    that and simply gets filled in. Only a column we create ourselves is
    labelled with our canonical name.
    """
    normed = [_norm_header(v) for v in header_row]
    claimed: Dict[int, str] = {}       # source index -> app column that took it
    bound: Dict[str, Optional[int]] = {}

    for app_column in APP_COLUMNS:
        found: Optional[int] = None
        for idx, h in enumerate(normed):
            if idx in claimed or not h:
                continue
            if _header_matches_app_column(h, app_column):
                found = idx
                break
        if found is not None:
            claimed[found] = app_column
        bound[app_column] = found

    order: List[Dict[str, Any]] = []
    app_at: Dict[str, int] = {}
    for app_column in APP_COLUMNS:
        source = bound[app_column]
        label = str(header_row[source]).strip() if source is not None and header_row[source] not in (None, "") else app_column
        app_at[app_column] = len(order)
        order.append({"name": label, "app": app_column, "source": source})

    # Everything of theirs we did not claim, in the order they sent it.
    for idx, raw in enumerate(header_row):
        if idx in claimed:
            continue
        label = str(raw).strip() if raw not in (None, "") else ""
        if not label:
            continue      # an unlabelled column carries no meaning to preserve
        order.append({"name": label, "app": None, "source": idx})

    return {"order": order, "app_at": app_at}


def _select_worksheet(wb) -> Worksheet:
    """Real portfolio-tracker exports commonly ship as multi-sheet workbooks
    (e.g. "Summary" / "Holdings" / "Transactions") where only one sheet
    actually lists funds. Prefer the active sheet (what the user was looking
    at) if it has a usable identifier column; otherwise fall back to the
    first sheet that does, rather than silently processing the wrong one."""
    # A workbook this tool produced is a single "Portfolio Review" sheet, so
    # re-uploading one is a legitimate round trip: it still carries every
    # column of the advisor's, and processing it just refreshes our figures.
    # But a workbook that has BOTH — e.g. they pasted our output alongside
    # their own tab — should be read from their tab, since ours is derived.
    candidates = [ws for ws in wb.worksheets if ws.title != REVIEW_SHEET_TITLE] or list(wb.worksheets)
    active = wb.active if wb.active in candidates else candidates[0]
    header = [c.value for c in next(active.iter_rows(min_row=1, max_row=1), [])]
    id_cols = _find_identifier_columns(header)
    if id_cols["name"] is not None or id_cols["code"] is not None or id_cols["isin"] is not None:
        return active
    for ws in candidates:
        if ws is active:
            continue
        header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1), [])]
        id_cols = _find_identifier_columns(header)
        if id_cols["name"] is not None or id_cols["code"] is not None or id_cols["isin"] is not None:
            return ws
    return active  # nothing better found — let the caller's own validation report the problem


# ─── Preview (parse-only, no matching/calculation) ───────────────────────────

def _is_our_own_workbook(wb) -> bool:
    """Whether this workbook was produced by build_output_workbook."""
    try:
        return WORKBOOK_MARKER in (wb.properties.keywords or "")
    except Exception:
        return False


def _strip_trailing_notes(rows: List[List[Any]]) -> List[List[Any]]:
    """Drop our own footnote lines from the end of a re-uploaded download.

    Only ever called on a file carrying WORKBOOK_MARKER, where the trailing
    block is known to be merged single-cell note rows. Anything with a value
    in a second column is a fund row and stops the scan immediately.
    """
    end = len(rows)
    while end > 0:
        row = rows[end - 1]
        populated = [i for i, v in enumerate(row) if v not in (None, "")]
        if populated == [0]:
            end -= 1
            continue
        break
    return rows[:end]


def read_source_sheet(workbook_bytes: bytes) -> Dict[str, Any]:
    """The uploaded sheet as plain values: {"header": [...], "rows": [[...]]}.

    read_only mode, and the workbook is closed before this returns. The
    earlier implementation kept the whole styled workbook alive for the entire
    job — every cell an object — while it made thousands of network calls, and
    then saved from it. Reading the values out first and rebuilding the output
    at the end holds a fraction of that, which is what matters on Render's
    free tier.

    Rows above the header (a title block, in the advisor's own export or in
    ours) are dropped, as are blank rows: neither carries anything to
    preserve, and dropping blanks keeps `processed`/`total` honest.
    """
    wb = load_workbook(io.BytesIO(workbook_bytes), data_only=True, read_only=True)
    try:
        ws = _select_worksheet(wb)
        rows_iter = ws.iter_rows(values_only=True)
        lead = [list(r) if r else [] for _, r in zip(range(HEADER_SCAN_ROWS), rows_iter)]
        if not lead:
            raise ValueError("Couldn't find a header row in this file.")
        header_at = find_header_row(lead)
        header = lead[header_at]
        if not header or all(v in (None, "") for v in header):
            raise ValueError("Couldn't find a header row in this file.")
        rows = [r for r in lead[header_at + 1:] if any(v not in (None, "") for v in r)]
        rows.extend(
            list(r) for r in rows_iter if r and any(v not in (None, "") for v in r)
        )
        if _is_our_own_workbook(wb) and len(header) > 1:
            rows = _strip_trailing_notes(rows)
        return {"header": header, "rows": rows}
    finally:
        wb.close()


def parse_preview(workbook_bytes: bytes) -> Dict[str, Any]:
    """Header + fund-name preview shown to the user before they hit "Process
    Excel" — confirms the file was read correctly without running anything.

    Reads through read_source_sheet so the preview and the job can never
    disagree about which row is the header or how many fund rows there are:
    `total` here is what the progress bar counts up to, and a divergence
    would show as a job that finishes at 47/50.
    """
    source = read_source_sheet(workbook_bytes)
    header_row = source["header"]
    rows = source["rows"]

    id_cols = _find_identifier_columns(header_row)
    if id_cols["name"] is None and id_cols["code"] is None and id_cols["isin"] is None:
        raise ValueError(
            'No fund identifier column found. Add a "Fund Name", "Scheme Code", '
            'or "ISIN" column so funds can be matched.'
        )
    if not rows:
        raise ValueError("This file has a header row but no fund rows under it.")

    name_col = id_cols["name"]
    preview_names: List[str] = []
    if name_col is not None:
        for row in rows:
            if len(preview_names) >= MAX_PREVIEW_FUNDS:
                break
            if name_col < len(row) and row[name_col] not in (None, ""):
                preview_names.append(str(row[name_col]).strip())

    return {
        "total": len(rows),
        "preview_funds": preview_names,
        "has_name_column": name_col is not None,
    }


# ─── Fund matching ────────────────────────────────────────────────────────────

# "Fund of Fund(s)" and "FOF" are the same words. AMFI writes one, advisors
# write the other, and neither normalise_name nor name_stem bridges them —
# name_stem strips "fund" as plan noise, so "Gold and Silver ETF Fund of Fund"
# reduces to "...etf of" while AMFI's "Gold and Silver ETF FOF" reduces to
# "...etf fof". Two spellings of one scheme that can never meet.
_FOF_RE = re.compile(r"\bfunds?\s+of\s+funds?\b|\bf\s*o\s*f\b", re.IGNORECASE)
# Left over once "fund" has been stripped as noise; carries no meaning.
_STRAY_WORDS_RE = re.compile(r"\b(of|the)\b")
# Legacy lettered share classes ("Plan B", "Plan C") — closed variants of a
# scheme whose plain Regular plan is the one an advisor means.
_LETTERED_PLAN_RE = re.compile(r"\bplan\s+[b-z]\b", re.IGNORECASE)


def match_key(name: str) -> str:
    """The key this matcher indexes scheme names under.

    `lineage_store.name_stem` with two extra normalisations layered on top.
    Deliberately NOT changed inside lineage_store: name_stem feeds the lineage
    build's own identity keys, and altering it would change `lineage_version`
    and invalidate the verified successor mappings built on it (see
    LINEAGE_VALIDATION_REPORT.md). This key is local to Excel matching, where
    a looser key costs nothing — every candidate it produces still has to be
    UNIQUE, or resolvable to one plan variant of one scheme, before it is
    accepted.
    """
    s = _FOF_RE.sub(" fof ", (name or "").lower())
    s = lineage_store.name_stem(s)
    s = _STRAY_WORDS_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _variant_rank(name: str) -> Tuple[int, int, str]:
    """Preference among plan/option variants of ONE scheme.

    Same ordering server.py's _dedupe_fund_variants uses for the Home screen,
    so a fund resolved here is the same one the app would show. A plain Growth
    option beats a lettered legacy plan, which beats a payout variant.
    """
    lname = (name or "").lower()
    payout = any(tok in lname for tok in ("bonus", "dividend", "idcw", "payout", "reinvestment"))
    lettered = _LETTERED_PLAN_RE.search(lname) is not None
    growth = "growth" in lname
    if growth and not payout and not lettered:
        tier = 0
    elif not payout and not lettered:
        tier = 1
    elif not payout:
        tier = 2
    else:
        tier = 3
    return (tier, len(name or ""), name or "")


def resolve_variants(codes: List[int], by_code: Dict[int, str]) -> Optional[int]:
    """One scheme code from candidates that are all variants of the SAME
    scheme, or None when the choice isn't clear.

    Only ever called with candidates that share a match_key — i.e. one
    underlying scheme under several plan/option names. Picking between those
    is safe and is what the Home screen already does; picking between
    genuinely different schemes is not, and never happens here.

    Returns None on a tie at the best rank: two equally plausible variants is
    a real ambiguity, and Rule 7 says report "Fund not found" rather than
    guess.
    """
    unique = sorted(set(codes))
    if not unique:
        return None
    if len(unique) == 1:
        return unique[0]
    ranked = sorted(unique, key=lambda c: _variant_rank(by_code.get(c, "")))
    best = _variant_rank(by_code.get(ranked[0], ""))
    runner_up = _variant_rank(by_code.get(ranked[1], ""))
    return ranked[0] if best[0] < runner_up[0] else None


async def build_master_index() -> Tuple[Dict[int, str], Dict[str, List[int]], Dict[str, List[int]]]:
    """Current AMFI master list, indexed for O(1) lookups. Built once per
    job, not per row — 37k+ funds, re-scanning per row would be O(n*m).

    Also builds a name-STEM index (plan/option noise words dropped — see
    lineage_store.name_stem). This exists because real AMFI scheme names
    carry plan/option wording an advisor's own sheet usually omits — e.g.
    AMFI's "quant Multi Cap Fund-GROWTH OPTION - Regular Plan" vs an
    Excel's plain "Quant Multi Cap Fund Growth". normalise_name alone
    (case/punctuation only) does NOT bridge that gap; only stem removal
    does. Confirmed against a real advisor sheet: 25 of 28 real holdings
    failed to match on normalised name alone despite being live, current
    schemes with valid ISINs. match_fund() only trusts a stem hit when it
    is UNIQUE, so this stays a deterministic fallback, never a guess."""
    funds = await amfi_client.fetch_fund_list()
    by_code: Dict[int, str] = {}
    by_norm_name: Dict[str, List[int]] = {}
    by_stem: Dict[str, List[int]] = {}
    for f in funds:
        code = f.get("schemeCode")
        name = (f.get("schemeName") or "").strip()
        if code is None or not name:
            continue
        try:
            code_i = int(code)
        except (TypeError, ValueError):
            continue
        by_code[code_i] = name
        norm = lineage_store.normalise_name(name)
        by_norm_name.setdefault(norm, []).append(code_i)
        stem = match_key(name)
        if stem:
            by_stem.setdefault(stem, []).append(code_i)
    return by_code, by_norm_name, by_stem


async def match_fund(
    db,
    by_code: Dict[int, str],
    by_norm_name: Dict[str, List[int]],
    by_stem: Optional[Dict[str, List[int]]] = None,
    *,
    code_val: Any = None,
    isin_val: Any = None,
    name_val: Any = None,
) -> Optional[int]:
    """Identify one Excel row's fund.

    Preference order (per feature spec): unique scheme code > ISIN > the
    app's own historical-identifier index (lineage_store — covers renamed
    schemes and old codes) > exact/normalized fund name > name STEM against
    the current live master list (plan/option noise words dropped — e.g.
    "Quant Multi Cap Fund Growth" vs AMFI's own "quant Multi Cap
    Fund-GROWTH OPTION - Regular Plan").

    Every tier is tried in order and each is INDEPENDENT: a tier that comes
    back ambiguous falls through to the next rather than ending the search.
    A tier is confident when it yields one code, or several codes that are
    plan/option variants of a single scheme — `resolve_variants` then picks
    the same variant the Home screen would show. Anything else falls through;
    running out of tiers returns None and the row is reported as "Fund not
    found", never silently matched to the wrong fund (Rule 7).
    """
    # 1. Scheme / AMFI code — the only fully unambiguous identifier.
    if code_val not in (None, ""):
        code_i: Optional[int] = None
        try:
            code_i = int(float(str(code_val).strip()))
        except (TypeError, ValueError):
            code_i = None
        if code_i is not None:
            if code_i in by_code:
                return code_i
            # Might be a historical/wound-up code — resolve via the app's
            # own lineage index (old code -> current code). Best-effort for
            # the same reason as the name tier below.
            try:
                hits = await lineage_store.resolve(db, str(code_i), limit=5)
            except Exception as exc:
                logger.warning("Bulk analysis: lineage code lookup failed for %s: %s", code_i, exc)
                hits = []
            if len(hits) == 1:
                return hits[0]

    # 2. ISIN.
    if isin_val not in (None, ""):
        isin = str(isin_val).strip()
        if isin:
            try:
                hits = await lineage_store.resolve(db, isin, limit=5)
            except Exception as exc:
                logger.warning("Bulk analysis: lineage ISIN lookup failed for %r: %s", isin, exc)
                hits = []
            if len(hits) == 1:
                return hits[0]

    # 3. Fund name — exact/normalized match against the CURRENT master list.
    # normalise_name handles case, whitespace and punctuation safely without
    # collapsing genuinely different plan/option variants (see its docstring
    # in lineage_store.py) — this is both the "exact name" and "normalized
    # name" tiers from the spec, since normalization only removes harmless
    # formatting noise.
    if name_val not in (None, ""):
        name = str(name_val).strip()
        if name:
            # Each name tier is INDEPENDENT and tried in order, most precise
            # first. An earlier tier that comes back ambiguous no longer stops
            # the later ones from being tried.
            #
            # It used to. The stem tier sat behind `if not lineage_hits`, so
            # it only ran when the lineage index found NOTHING — meaning the
            # better the lineage index got, the fewer funds this matched.
            # With a fully built index (126k identifier records) an ambiguous
            # multi-hit lineage result is the norm, and it silently blocked
            # the tier that would have resolved the fund. Confirmed against a
            # universe snapshot: "Edelweiss Large Cap Fund Growth", "Motilal
            # Oswal Large and Midcap Fund Regular Growth" and "ICICI
            # Prudential Dividend Yield Equity Fund Growth" all resolve
            # UNIQUELY on stem, and all three were reported "Fund not found".
            norm = lineage_store.normalise_name(name)

            # 3. Exact/normalised name against the current master list.
            picked = resolve_variants(by_norm_name.get(norm) or [], by_code)
            if picked is not None:
                return picked

            # 4. The lineage index — covers historical names, so a fund the
            # AMC renamed is still found by an Excel using its old name.
            # Requires scripts/build_lineage.py --persist against this DB; if
            # that hasn't run, resolve() just returns [].
            #
            # The index is an ENHANCEMENT, never a dependency (the same
            # stance /api/funds/search takes). It is reached on every name now
            # rather than only when the exact tier missed, so a missing
            # collection or a Mongo hiccup would otherwise turn a fund the
            # stem tier can resolve into "Fund not found".
            try:
                lineage_hits = await lineage_store.resolve(db, name, limit=5)
            except Exception as exc:
                logger.warning("Bulk analysis: lineage lookup failed for %r: %s", name, exc)
                lineage_hits = []
            if len(lineage_hits) == 1:
                return lineage_hits[0]

            # 5. Name stem against the CURRENT live master list — needs no DB
            # index, works purely off the AMFI fetch this job already did.
            # Covers the ordinary case where an advisor's sheet omits the
            # "Regular Plan" / "Growth Option" wording AMFI's name carries,
            # and (via match_key) the FOF / "Fund of Fund" spelling split.
            if by_stem:
                picked = resolve_variants(by_stem.get(match_key(name)) or [], by_code)
                if picked is not None:
                    return picked
    return None


# ─── Retrieving the existing engine's FQS number ─────────────────────────────
#
# Primary rule: score at exactly the 10Y rolling horizon. Fallback (per the
# advisor's own existing manual sheet — a real reference file with entries
# like "85.5 (6 YR)"): a fund without 10 years of history yet is scored at
# its longest available horizon below 10Y instead of being left blank, and
# the FQS cell is annotated with that horizon so it's never mistaken for a
# true 10Y score. Remarks are still derived from whichever number is shown.

BuildAnalysisFn = Callable[..., Awaitable[Dict[str, Any]]]
CachedAnalysisFn = Callable[[int], Awaitable[Optional[Dict[str, Any]]]]

FqsPick = Tuple[Dict[str, Any], int]  # (horizon metrics block, horizon years actually used)


def _pick_horizon_from_map(fqs_by_horizon: Dict[str, Any]) -> Optional[FqsPick]:
    """From a {horizon_str: metrics} map (fund_nav_meta's shape), prefer
    exactly 10Y; otherwise the longest available horizon below 10Y."""
    entry10 = fqs_by_horizon.get(str(ROLLING_HORIZON_YEARS))
    if entry10 and entry10.get("fqs") is not None:
        return entry10, ROLLING_HORIZON_YEARS

    candidates: List[Tuple[int, Dict[str, Any]]] = []
    for k, v in fqs_by_horizon.items():
        if not v or v.get("fqs") is None:
            continue
        try:
            hy = int(k)
        except (TypeError, ValueError):
            continue
        if hy < ROLLING_HORIZON_YEARS:
            candidates.append((hy, v))
    if not candidates:
        return None
    hy, v = max(candidates, key=lambda t: t[0])
    return v, hy


def _pick_horizon_from_list(horizons: List[Dict[str, Any]]) -> Optional[FqsPick]:
    """Same picker, fed analyze_fund()'s list shape (server._build_analysis /
    _cached_analysis) instead of fund_nav_meta's dict shape."""
    by_h: Dict[str, Any] = {}
    for h in horizons or []:
        hy = h.get("horizon")
        if hy is None or h.get("fqs") is None:
            continue
        by_h[str(hy)] = h
    return _pick_horizon_from_map(by_h)


async def get_fund_result(
    db,
    scheme_code: int,
    build_analysis_fn: BuildAnalysisFn,
    cached_analysis_fn: CachedAnalysisFn,
) -> Dict[str, Any]:
    """Everything one Excel row needs about a matched fund, from ONE pass over
    the same cheapest-first lookup `get_fqs_result` documents below.

    Returns {"picked": FqsPick|None, "scheme_name": str|None,
             "category": str|None}. The name and category ride along from
    whichever source answered — they are already in every one of those
    documents, so carrying them costs nothing, while looking them up
    separately would mean a second Mongo round-trip (or a second NAV fetch)
    per row purely to fill two cells of the review sheet.

    `category` is the app's own internal bucket (the same string classify_fund
    produces and the Home cards show), which is also what the Benchmark XIRR
    lookup is keyed on — so it must come from the app, not from the upload,
    even when the review sheet ends up displaying the advisor's own wording.
    """
    doc = await db.fund_nav_meta.find_one(
        {"scheme_code": scheme_code},
        {"_id": 0, "fqs_by_horizon": 1, "scheme_name": 1, "category": 1},
    )
    if doc and doc.get("fqs_by_horizon"):
        picked = _pick_horizon_from_map(doc["fqs_by_horizon"])
        if picked:
            return {"picked": picked,
                    "scheme_name": doc.get("scheme_name"),
                    "category": doc.get("category")}

    name = (doc or {}).get("scheme_name")
    category = (doc or {}).get("category")

    cached = await cached_analysis_fn(scheme_code)
    if cached:
        picked = _pick_horizon_from_list(cached.get("horizons") or [])
        if picked:
            return {"picked": picked,
                    "scheme_name": cached.get("scheme_name") or name,
                    "category": cached.get("category") or category}
        name = cached.get("scheme_name") or name
        category = cached.get("category") or category

    try:
        # allow_direct=True: Portfolio Review scores a client's EXISTING
        # holdings, Direct plans included — unlike the Analyze screen and
        # Portfolio Proposal (which recommend NEW investments, where a Direct
        # plan is genuinely out of scope for an MFD), there is no reason to
        # leave a real, identified holding blank here. See server.py's
        # _build_analysis docstring for the full rationale; this is the one
        # caller that opts in.
        result = await build_analysis_fn(
            scheme_code, user_years=ROLLING_HORIZON_YEARS, allow_direct=True,
        )
    except Exception as exc:
        logger.warning("Bulk analysis: live compute failed for scheme %s: %s", scheme_code, exc)
        # Kept as a fallback for any OTHER rejection a build_analysis_fn
        # might still raise (e.g. a caller wired without allow_direct
        # support) — distinguishes it from a genuine data failure so the
        # row's Remark tells the advisor why, instead of looking identical
        # to "no NAV history yet".
        detail = str(getattr(exc, "detail", exc)).lower()
        reason = "direct_plan" if "direct plan" in detail else None
        return {"picked": None, "scheme_name": name, "category": category, "reason": reason}

    picked = _pick_horizon_from_list(result.get("horizons") or [])
    out = {
        "picked": picked,
        "scheme_name": result.get("scheme_name") or name,
        "category": result.get("category") or category,
    }
    if picked is None:
        # A genuine computation (no exception, real NAV fetched) that still
        # has zero usable rolling windows — not a matching problem and not
        # the Direct-plan rule, just a fund too young for even the shortest
        # horizon (a 1Y window needs 13 months of month-end NAVs). Surfaced
        # as its own reason so the Remark says so instead of repeating the
        # same "Calculation unavailable" a matching failure would show.
        out["reason"] = "too_new"
        out["months_of_history"] = result.get("monthly_points")
        out["first_month"] = result.get("first_month")
    return out


async def get_fqs_result(
    db,
    scheme_code: int,
    build_analysis_fn: BuildAnalysisFn,
    cached_analysis_fn: CachedAnalysisFn,
) -> Optional[FqsPick]:
    """The existing engine's FQS/median_wm/p10_wm/success_pct block for one
    fund, at 10Y or the best available fallback horizon. Tries cheapest-first,
    never recomputes anything itself:

      1. fund_nav_meta.fqs_by_horizon — the nightly batch cache (same
         collection/shape the Home screen reads via _select_horizon_map);
         already carries every horizon, not just 10Y.
      2. The 24h per-fund analysis cache (_cached_analysis) — populated by
         anyone who has viewed this fund's detail page recently.
      3. A live call to _build_analysis(scheme_code, user_years=10) — the
         exact function GET /funds/{code}/analyze uses; computes the full
         horizon set regardless of user_years, and self-caches (see
         server.py), so a fund uploaded once in bulk is fast next time.

    Returns None only when the fund has no usable rolling-window data at
    ANY horizon — get_fund_remark(None) reports that as "Calculation
    unavailable", never "Bad".

    Thin wrapper over get_fund_result so there is exactly ONE implementation
    of the three-tier lookup; kept as its own name because it is the shape
    _write_row_result and the existing tests speak.
    """
    return (await get_fund_result(db, scheme_code, build_analysis_fn, cached_analysis_fn))["picked"]


# ─── Our values for one row ───────────────────────────────────────────────────

def build_app_values(
    *,
    fund_result: Optional[Dict[str, Any]],
    remark: str,
) -> Dict[str, Any]:
    """The app's own value for each APP_COLUMN, for one uploaded row.

    Fund Name / Category / Allocation are produced here too, but only get
    used where the advisor's cell is empty (see USER_OWNED_COLUMNS) — the
    upload is the authority on those.

    Benchmark XIRR is derived from (category, horizon) through
    proposal_engine._required_min_wm — the SAME lookup the Portfolio Proposal
    uses for its own Benchmark XIRR column — rather than read off the fund's
    stats, because the nightly fqs_by_horizon cache does not store
    min_wm_required at all (see that function's docstring). Deriving it keeps
    the two deliverables' Benchmark XIRR identical for the same fund.
    """
    picked = (fund_result or {}).get("picked")
    values: Dict[str, Any] = {name: None for name in APP_COLUMNS}
    values["Fund Name"] = (fund_result or {}).get("scheme_name")
    values["Category"] = (fund_result or {}).get("category")
    values["Remarks"] = remark

    if picked is None:
        return values

    stats, horizon_years = picked
    values["No of Windows"] = stats.get("n_windows")
    values["Success%"] = stats.get("success_pct")
    values["CV (Stability)"] = stats.get("cv")

    median_wm = stats.get("median_wm")
    p10_wm = stats.get("p10_wm")
    values["Median WM"] = median_wm
    values["P10 WM"] = p10_wm
    # Both XIRRs are converted at the horizon the WM was actually measured
    # over — a fixed 10 would overstate a fund scored at a shorter fallback
    # horizon.
    values["XIRR (Med)"] = sip_wm_to_xirr(median_wm, horizon_years) if median_wm is not None else None
    values["P10 XIRR"] = sip_wm_to_xirr(p10_wm, horizon_years) if p10_wm is not None else None

    fqs = stats.get("fqs")
    if fqs is not None:
        # A fund without 10 years of history is scored at its longest
        # available horizon and the cell is annotated (e.g. "85.5 (6 YR)"),
        # matching the advisor's own existing sheet, so a short-history score
        # is never mistaken for a true 10Y one.
        values["FQS"] = round(fqs, 1) if horizon_years == ROLLING_HORIZON_YEARS \
            else f"{round(fqs, 1)} ({horizon_years} YR)"

    category = (fund_result or {}).get("category")
    if category:
        try:
            min_wm = proposal_engine._required_min_wm(category, horizon_years, "sip")
        except Exception:   # unknown category — leave blank, never guess
            min_wm = None
        if min_wm:
            values["Benchmark XIRR"] = sip_wm_to_xirr(min_wm, horizon_years)
    return values


# ─── The branded workbook ────────────────────────────────────────────────────

_THIN = Side(style="thin", color=BORDER_GREY)
_CELL_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


def _fqs_tier_colour(value: Any) -> Optional[str]:
    """Font colour for an FQS cell, on the same thresholds get_fund_remark
    uses — so the colour and the Remarks verdict can never disagree."""
    if value is None:
        return None
    try:
        score = float(str(value).split("(")[0].strip())
    except (TypeError, ValueError):
        return None
    if score < 75:
        return _TIER_COLOUR["bad"]
    if score < 85:
        return _TIER_COLOUR["mid"]
    return _TIER_COLOUR["good"]


def _resolve_cell(col: Dict[str, Any], source_row: List[Any], app_values: Dict[str, Any]) -> Any:
    """The value for one output cell — the whole preserve/fill/update rule.

      their column, not ours   -> their value, always
      ours, they own it        -> their value; ours only where theirs is blank
      ours                     -> ours, always (so a re-upload refreshes)
    """
    src_idx = col["source"]
    theirs = source_row[src_idx] if src_idx is not None and src_idx < len(source_row) else None
    app_column = col["app"]

    if app_column is None:
        return theirs
    if app_column in USER_OWNED_COLUMNS:
        if theirs not in (None, ""):
            # Allocation is stored as a fraction so the % format renders it;
            # "25%", "25" and 0.25 all mean the same holding (see
            # parse_allocation). Unparseable text is kept verbatim.
            if app_column == "Allocation":
                parsed = parse_allocation(theirs)
                return parsed if parsed is not None else theirs
            return theirs
        return app_values.get(app_column)
    return app_values.get(app_column)


def _width_of(col: Dict[str, Any]) -> float:
    """Rendered width of an output column, in Excel's character units."""
    if col["app"]:
        return APP_COLUMN_WIDTH.get(col["app"], 14.0)
    return 20.0   # the auto-sized default for one of the advisor's own columns


def _wrapped_line_count(text: str, width: float) -> int:
    """How many lines `text` takes at `width` characters. Approximate on
    purpose — it only has to bound the row height, and over-estimating costs
    a little whitespace while under-estimating clips the text."""
    usable = max(6.0, width - 2.0)
    return max(1, math.ceil(len(text) / usable))


def _write_brand_header(ws: Worksheet, width: int, subtitle: str) -> int:
    """Title block. Returns the row number the column headers go on."""
    last = get_column_letter(max(width, 3))
    navy = PatternFill("solid", fgColor=BRAND_NAVY)

    ws.merge_cells(f"A1:{last}1")
    ws.merge_cells(f"A2:{last}2")
    for row in (1, 2):
        for col in range(1, width + 1):
            ws.cell(row=row, column=col).fill = navy

    title = ws.cell(row=1, column=1, value="ArthVruksh — Portfolio Review")
    title.font = Font(bold=True, size=16, color="FFFFFFFF")
    title.alignment = Alignment(horizontal="left", vertical="center", indent=5)

    sub = ws.cell(row=2, column=1, value=subtitle)
    sub.font = Font(size=9, color="FFCBD5E1")
    sub.alignment = Alignment(horizontal="left", vertical="center", indent=5)

    ws.row_dimensions[1].height = 30
    ws.row_dimensions[2].height = 16
    ws.row_dimensions[3].height = 6      # thin breathing space under the band

    # The logo sits over the navy band. A missing or unreadable asset must
    # never fail the export — the sheet is still a valid deliverable without it.
    try:
        img = XLImage(LOGO_PATH)
        img.width = 38
        img.height = 38
        ws.add_image(img, "A1")
    except Exception:
        logger.debug("Portfolio Review: logo not embedded", exc_info=True)

    return 4


def build_output_workbook(
    header_row: List[Any],
    source_rows: List[List[Any]],
    app_values_by_row: Dict[int, Dict[str, Any]],
    *,
    fallback_note: bool = False,
) -> bytes:
    """The client-ready download: the advisor's own sheet, merged with our
    analysis, in ArthVruksh styling.

    The sheet is REBUILT rather than edited in place. Every value the advisor
    uploaded is carried across (that is the contract), but the presentation
    becomes ours — branded header, one consistent typeface, borders, banding,
    real number formats. Editing their sheet in place cannot produce that:
    their own fills, fonts and merged cells would show through, which is
    exactly what "looks like a raw data export" means.

    `app_values_by_row` is keyed by index into `source_rows`.
    """
    plan = plan_columns(header_row)
    order = plan["order"]
    width = len(order)

    wb = Workbook()
    ws = wb.active
    ws.title = REVIEW_SHEET_TITLE
    ws.sheet_view.showGridLines = False

    subtitle = (
        f"{COMPANY_LEGAL_NAME}   ·   AMFI Registration Number (ARN): {ARN_NUMBER}"
        f"   ·   FQS from {ROLLING_HORIZON_YEARS}-year rolling periods"
        f"   ·   {datetime.now(timezone.utc).date().isoformat()}"
    )
    header_line = _write_brand_header(ws, width, subtitle)

    head_fill = PatternFill("solid", fgColor=BRAND_NAVY)
    head_font = Font(bold=True, size=10, color="FFFFFFFF")
    head_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for i, col in enumerate(order, start=1):
        cell = ws.cell(row=header_line, column=i, value=col["name"])
        cell.fill = head_fill
        cell.font = head_font
        cell.alignment = head_align
        cell.border = Border(
            left=_THIN, right=_THIN, top=_THIN,
            bottom=Side(style="medium", color=BRAND_AZURE),
        )
    ws.row_dimensions[header_line].height = 30

    band = PatternFill("solid", fgColor=BAND_FILL)
    # indent keeps values off the cell borders — without it every column reads
    # as one run-on block of text.
    left_align = Alignment(horizontal="left", vertical="center", wrap_text=True, indent=1)
    right_align = Alignment(horizontal="right", vertical="center", indent=1)

    first_data = header_line + 1
    row_no = first_data
    for r, source_row in enumerate(source_rows):
        app_values = app_values_by_row.get(r, {})
        shaded = (row_no - first_data) % 2 == 1
        wrapped_lines = 1
        for i, col in enumerate(order, start=1):
            value = _resolve_cell(col, source_row, app_values)
            cell = ws.cell(row=row_no, column=i, value=value)
            cell.border = _CELL_BORDER
            if shaded:
                cell.fill = band

            app_column = col["app"]
            numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
            fmt = APP_NUMBER_FORMAT.get(app_column) if app_column else None
            if fmt and numeric:
                cell.number_format = fmt
            elif app_column is None and numeric and float(value).is_integer() and abs(value) >= 1000:
                # One of their own numeric columns (amount invested, current
                # value, units). We never change the VALUE, but a raw 1240000
                # in a client-facing sheet is unreadable — give whole numbers
                # a thousands separator and leave anything with decimals alone.
                cell.number_format = "#,##0"

            if app_column in _TEXT_COLUMNS or (app_column is None and not numeric):
                cell.alignment = left_align
            else:
                cell.alignment = right_align

            if app_column == "FQS":
                colour = _fqs_tier_colour(value)
                if colour:
                    cell.font = Font(bold=True, color=colour)

            if cell.alignment.wrap_text and isinstance(value, str):
                wrapped_lines = max(wrapped_lines, _wrapped_line_count(value, _width_of(col)))

        # Excel auto-fits a wrapped row ONLY while its height is unset, and an
        # explicit height silently clips or overlaps instead. A fund name is
        # routinely longer than its column, so the height is computed rather
        # than pinned — a flat 18 made every long name overlap the row below.
        ws.row_dimensions[row_no].height = max(18.0, wrapped_lines * 13.5 + 4)
        row_no += 1

    last_data = row_no - 1
    last_col = get_column_letter(width)
    if last_data >= first_data:
        ws.auto_filter.ref = f"A{header_line}:{last_col}{last_data}"
    # Freeze the title block AND the fund-name column: with the advisor's own
    # columns preserved the sheet is often wider than a screen, and scrolling
    # right without the fund name visible makes every row unreadable.
    ws.freeze_panes = f"B{first_data}"

    for i, col in enumerate(order, start=1):
        letter = get_column_letter(i)
        if col["app"]:
            ws.column_dimensions[letter].width = APP_COLUMN_WIDTH.get(col["app"], 14.0)
        else:
            longest = max([len(str(col["name"]))] + [
                len(str(r[col["source"]])) for r in source_rows
                if col["source"] is not None and col["source"] < len(r) and r[col["source"]] is not None
            ] or [10])
            # Kept in step with _width_of's assumption for these columns, so
            # the row-height estimate matches what is actually rendered.
            ws.column_dimensions[letter].width = max(10.0, min(38.0, longest + 3))

    note_row = last_data + 2
    notes = [
        f"Generated by ArthVruksh from its own fund-scoring engine. FQS, Success%, "
        f"Median WM, XIRR (Med), P10 WM and CV are measured over "
        f"{ROLLING_HORIZON_YEARS}-year rolling periods.",
    ]
    if fallback_note:
        notes.append(
            "An FQS annotated e.g. \"85.5 (6 YR)\" is that fund's score at its longest "
            "available rolling period — it does not yet have a full "
            f"{ROLLING_HORIZON_YEARS}-year record."
        )
    for offset, text in enumerate(notes):
        ws.merge_cells(start_row=note_row + offset, start_column=1,
                       end_row=note_row + offset, end_column=max(width, 3))
        cell = ws.cell(row=note_row + offset, column=1, value=text)
        cell.font = Font(italic=True, size=8.5, color=MUTED_TEXT)
        cell.alignment = left_align

    # Print setup — the sheet is meant to be shared or printed as-is.
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = f"1:{header_line}"

    # See WORKBOOK_MARKER: lets a re-upload of this very file be recognised.
    wb.properties.keywords = WORKBOOK_MARKER
    wb.properties.creator = COMPANY_LEGAL_NAME
    wb.properties.title = "ArthVruksh — Portfolio Review"

    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    out = buf.getvalue()
    buf.close()
    return out


# ─── Nifty 50 benchmark row ───────────────────────────────────────────────────
#
# An index has no NAV of its own on AMFI, so the benchmark is scored through
# a Nifty 50 INDEX FUND's NAV — the closest investable proxy (it trails the
# index by its expense ratio and tracking error, which the remark says). The
# scheme is chosen at run time from the live master list rather than
# hard-coded: the regular-plan growth Nifty 50 index fund with the longest
# history in the app's own index, so it is never a guessed scheme code.

NIFTY50_LABEL = "Nifty 50 (Benchmark)"
_NIFTY50_RE = re.compile(r"\bnifty\s*50\b", re.IGNORECASE)
_INDEX_RE = re.compile(r"\bindex\b", re.IGNORECASE)
_NIFTY50_EXCLUDE = re.compile(
    r"\b(direct|idcw|dividend|payout|etf|fof|fund of funds?|next|equal|value|quality|momentum|"
    r"low\s*vol\w*|alpha|arbitrage|shariah|sensex|bharat|midcap|smallcap|largemidcap|"
    r"\d+\s*:\s*\d+|top\s*\d+|g-?sec|sdl|bond|debt)\b",
    re.IGNORECASE,
)


def nifty50_candidates(by_code: Dict[int, str]) -> List[int]:
    """Scheme codes whose AMFI name reads as a plain Nifty 50 index fund,
    regular plan, growth option."""
    out = []
    for code, name in by_code.items():
        if not (_NIFTY50_RE.search(name) and _INDEX_RE.search(name)):
            continue
        if _NIFTY50_EXCLUDE.search(name) or amfi_client.is_idcw_scheme(name):
            continue
        out.append(code)
    return out


async def resolve_nifty50_proxy(db, by_code: Dict[int, str]) -> Optional[int]:
    """The candidate with the longest history in fund_nav_meta, active only."""
    candidates = nifty50_candidates(by_code)
    if not candidates:
        return None
    best: Optional[Tuple[float, int]] = None
    async for d in db.fund_nav_meta.find(
        {"scheme_code": {"$in": candidates}},
        {"_id": 0, "scheme_code": 1, "history_years": 1, "superseded_by": 1, "merged_with": 1,
         "own_latest_nav_date": 1, "latest_nav_date": 1},
    ):
        if fund_status.is_superseded(d) or fund_status.is_stale(d):
            continue
        key = (float(d.get("history_years") or 0), -int(d["scheme_code"]))
        if best is None or key > best:
            best = key
    return -best[1] if best else None


async def nifty50_benchmark(
    db, by_code: Dict[int, str],
    build_analysis_fn: BuildAnalysisFn, cached_analysis_fn: CachedAnalysisFn,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """(app values for the review row, summary for the job status). The row
    is None when no proxy could be resolved or scored — the summary says why."""
    code = await resolve_nifty50_proxy(db, by_code)
    if code is None:
        return None, {"status": "unavailable", "label": NIFTY50_LABEL,
                      "reason": "No Nifty 50 index fund found in the fund index."}
    try:
        result = await get_fund_result(db, code, build_analysis_fn, cached_analysis_fn)
    except Exception as exc:
        logger.warning("Nifty 50 benchmark (scheme %s) failed: %s", code, exc)
        return None, {"status": "unavailable", "label": NIFTY50_LABEL, "scheme_code": code,
                      "reason": "The index fund's NAV history couldn't be scored right now."}
    proxy_name = result.get("scheme_name") or by_code.get(code) or str(code)
    values = build_app_values(
        fund_result=result,
        remark=f"Benchmark — Nifty 50, measured through {proxy_name} (an index fund; "
               f"trails the index by its costs)",
    )
    values["Fund Name"] = NIFTY50_LABEL
    values["Category"] = "Index (Benchmark)"
    picked = result.get("picked")
    stats, horizon = picked if picked else ({}, None)
    summary = {
        "status": "ok" if picked else "unavailable",
        "label": NIFTY50_LABEL,
        "scheme_code": code,
        "proxy_name": proxy_name,
        "horizon": horizon,
        "fqs": stats.get("fqs"),
        "success_pct": stats.get("success_pct"),
        "xirr_med": values.get("XIRR (Med)"),
        "p10_xirr": values.get("P10 XIRR"),
        "median_wm": stats.get("median_wm"),
    }
    if not picked:
        summary["reason"] = "The index fund has no scored rolling window yet."
    return values, summary


# ─── The batch job ────────────────────────────────────────────────────────────

async def process_bulk_job(
    job_id: str,
    db,
    workbook_bytes: bytes,
    build_analysis_fn: BuildAnalysisFn,
    cached_analysis_fn: CachedAnalysisFn,
    include_nifty50: bool = False,
) -> None:
    """Background task kicked off by POST /bulk-analysis/{job_id}/process.

    Three phases: read the upload's values, score each row against the app's
    existing engine, then build the branded download. One row failing (fund
    not found, calculation error) never stops the batch — every row still gets
    a result and a Remark, and the job doc's counters are updated as it goes
    so /status is never fake progress.
    """
    jobs = db[JOBS_COLLECTION]
    try:
        # Reading and writing the workbook are the two synchronous, CPU-bound
        # steps here. This coroutine shares its event loop with every HTTP
        # request the process is serving, so doing them inline freezes the
        # whole API for seconds on a large upload. Both go to a worker thread;
        # everything between is I/O-bound and already yields properly.
        source = await asyncio.to_thread(read_source_sheet, workbook_bytes)
        header_row: List[Any] = source["header"]
        source_rows: List[List[Any]] = source["rows"]

        id_cols = _find_identifier_columns(header_row)
        by_code, by_norm_name, by_stem = await build_master_index()

        processed = matched = not_found = errors = 0
        # Every unmatched row, so a person can go add these funds manually
        # instead of hunting through the downloaded Excel for "Fund not
        # found" rows — surfaced via GET /status and shown on the summary
        # card. Small (name/code/isin strings only), well within Mongo's
        # per-doc limit even for a large upload.
        not_found_funds: List[Dict[str, Any]] = []
        # Our values per uploaded row, keyed by index into source_rows.
        # Scalars only — no NAV series, no horizon maps.
        app_values_by_row: Dict[int, Dict[str, Any]] = {}
        saw_fallback_horizon = False
        semaphore = asyncio.Semaphore(ROW_CONCURRENCY)

        def _at(row: List[Any], col0: Optional[int]) -> Any:
            if col0 is None or col0 >= len(row):
                return None
            return row[col0]

        async def handle_row(index: int) -> None:
            nonlocal matched, not_found, errors, saw_fallback_horizon
            row = source_rows[index]
            name_val = _at(row, id_cols["name"])
            code_val = _at(row, id_cols["code"])
            isin_val = _at(row, id_cols["isin"])
            # Excel's own row number, for the "not found" list a human reads.
            sheet_row = index + 2

            async with semaphore:
                scheme_code = await match_fund(
                    db, by_code, by_norm_name, by_stem,
                    code_val=code_val, isin_val=isin_val, name_val=name_val,
                )
                if scheme_code is None:
                    not_found += 1
                    label = None
                    for v in (name_val, isin_val, code_val):
                        if v not in (None, ""):
                            label = str(v).strip()
                            break
                    not_found_funds.append({
                        "row": sheet_row,
                        "name": str(name_val).strip() if name_val not in (None, "") else None,
                        "isin": str(isin_val).strip() if isin_val not in (None, "") else None,
                        "code": str(code_val).strip() if code_val not in (None, "") else None,
                        "label": label or f"Row {sheet_row}",
                    })
                    app_values_by_row[index] = build_app_values(
                        fund_result=None, remark="Fund not found",
                    )
                    return

                try:
                    fund_result = await get_fund_result(
                        db, scheme_code, build_analysis_fn, cached_analysis_fn,
                    )
                except Exception as exc:
                    logger.warning("Bulk analysis: row %d (scheme %s) errored: %s",
                                   sheet_row, scheme_code, exc)
                    fund_result = {"picked": None, "scheme_name": None, "category": None}

                picked = fund_result["picked"]
                if picked is None:
                    errors += 1
                else:
                    matched += 1
                    if picked[1] != ROLLING_HORIZON_YEARS:
                        saw_fallback_horizon = True
                fqs = picked[0].get("fqs") if picked else None
                # A row matched (by ISIN/lineage) to a Direct-plan scheme, or
                # to a fund too young for any rolling window, is correctly
                # identified and correctly unscored for a KNOWN reason — say
                # which one, rather than the generic "Calculation unavailable"
                # that would otherwise cover both plus any other failure.
                reason = fund_result.get("reason")
                if reason == "direct_plan":
                    remark = REMARK_DIRECT_PLAN
                elif reason == "too_new":
                    remark = remark_for_too_new(
                        fund_result.get("months_of_history"), fund_result.get("first_month"),
                    )
                else:
                    remark = get_fund_remark(fqs)
                app_values_by_row[index] = build_app_values(
                    fund_result=fund_result, remark=remark,
                )

        for i in range(0, len(source_rows), PROGRESS_BATCH):
            batch = range(i, min(i + PROGRESS_BATCH, len(source_rows)))
            # Concurrent within a batch (bounded by ROW_CONCURRENCY via the
            # semaphore inside handle_row), sequential across batches — same
            # chunking rationale as fqs_refresh.py: bounds peak memory and
            # NAV fetches in flight while still overlapping network-bound rows.
            await asyncio.gather(*[handle_row(r) for r in batch])
            processed += len(batch)
            await jobs.update_one(
                {"_id": job_id},
                {"$set": {"processed": processed, "matched": matched,
                          "not_found": not_found, "errors": errors,
                          "not_found_funds": sorted(not_found_funds, key=lambda f: f["row"])}},
            )

        benchmark: Optional[Dict[str, Any]] = None
        if include_nifty50:
            bench_values, benchmark = await nifty50_benchmark(
                db, by_code, build_analysis_fn, cached_analysis_fn,
            )
            if bench_values is not None:
                # Appended as the sheet's last row, so the advisor's own rows
                # keep their order and the benchmark reads as a footer line.
                bench_row: List[Any] = [None] * len(header_row)
                if id_cols["name"] is not None and id_cols["name"] < len(bench_row):
                    bench_row[id_cols["name"]] = NIFTY50_LABEL
                source_rows.append(bench_row)
                app_values_by_row[len(source_rows) - 1] = bench_values

        output_bytes = await asyncio.to_thread(
            build_output_workbook, header_row, source_rows, app_values_by_row,
            fallback_note=saw_fallback_horizon,
        )
        # Release the inputs before handing a second large object to Mongo.
        source_rows.clear()
        app_values_by_row.clear()
        del source

        await jobs.update_one(
            {"_id": job_id},
            {
                "$set": {
                    "status": "completed",
                    "processed": processed, "matched": matched,
                    "not_found": not_found, "errors": errors,
                    "not_found_funds": sorted(not_found_funds, key=lambda f: f["row"]),
                    "benchmark": benchmark,
                    "output_bytes": output_bytes,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                },
                "$unset": {"workbook_bytes": ""},
            },
        )
        del output_bytes
        logger.info(
            "Bulk analysis job %s finished: %d processed (%d matched, %d not found, %d errors)",
            job_id, processed, matched, not_found, errors,
        )
    except Exception:
        logger.exception("Bulk analysis job %s failed", job_id)
        await jobs.update_one(
            {"_id": job_id},
            {"$set": {"status": "failed", "finished_at": datetime.now(timezone.utc).isoformat()}},
        )
