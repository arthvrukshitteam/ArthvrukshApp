"""holdings_parser.py — read a SEBI monthly portfolio disclosure into
normalised equity holdings.

Every AMC must publish a monthly portfolio in the SEBI-prescribed layout, so
while the *styling* of these workbooks varies wildly between AMCs, the
substance does not: a header row naming the instrument, its ISIN, its
quantity, its market value and its "% to Net Assets", with rows grouped under
section headings ("Equity & Equity Related", "Debt Instruments", "Money
Market Instruments", …).

This module keys off that substance and ignores the styling. It does NOT try
to be a per-AMC parser with a rule for every house — that never survives the
next month's file. Instead it:

  1. finds the header row by looking for the columns it needs, anywhere in
     the first `HEADER_SCAN_ROWS` rows (AMCs put 2–10 rows of title block,
     scheme name and fund-manager notes above it);
  2. tracks which *section* each row is under, and keeps only the equity one;
  3. drops the rows that are structure rather than securities — sub-totals,
     grand totals, section sub-headings, blank spacers.

Anything it cannot read confidently it reports as an error rather than
guessing, because a silently half-parsed portfolio produces a plausible-
looking overlap number that is wrong — the worst possible failure for this
feature.

Memory note: workbooks are opened `read_only=True, data_only=True` and rows
are consumed through an iterator, the same discipline `bulk_analysis.py`
uses. These files run to several MB and the service they land on has 512MB
total, so nothing here materialises a whole workbook of cell objects.
"""

from __future__ import annotations

import io
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from openpyxl import load_workbook

logger = logging.getLogger(__name__)

HEADER_SCAN_ROWS = 30      # AMC title blocks are deep; 30 clears every one seen

# ── Column identification ────────────────────────────────────────────────────
# Matched as substrings against the normalised header cell, because AMCs
# decorate these labels endlessly ("Market value (Rs. in Lakhs)", "% to Net
# Assets", "% to NAV", "Name of the Instrument / Issuer").

_NAME_HINTS   = ("name of the instrument", "name of instrument", "instrument",
                 "security name", "name of the security", "particulars")
_ISIN_HINTS   = ("isin",)
_WEIGHT_HINTS = ("% to net assets", "% of net assets", "% to nav", "% of nav",
                 "% to net asset", "percentage of net assets", "% to aum")
_QTY_HINTS    = ("quantity", "no. of shares", "units")
_VALUE_HINTS  = ("market value", "market/fair value", "fair value", "amount")

# ── Section detection ────────────────────────────────────────────────────────
# A row whose instrument cell matches one of these — and which carries no
# ISIN — is a section heading, not a security.

_EQUITY_SECTION = re.compile(
    r"equity\s*(&|and)?\s*equity\s*related|^equity(\s+shares?)?$|equity instruments",
    re.IGNORECASE,
)
# Anything that starts a non-equity block. Hitting one of these closes the
# equity section — the rows below it are debt, cash or derivatives.
_OTHER_SECTION = re.compile(
    r"debt instrument|money market|government securit|corporate (bond|debt)|"
    r"treasury bill|certificate of deposit|commercial paper|"
    r"mutual fund unit|units? of|reit|invit|"
    r"derivativ|futures|options|"
    r"cash (&|and) cash equivalent|trep|repo|net receivable|net current asset|"
    r"margin|fixed deposit|securitis|other current asset|preference share",
    re.IGNORECASE,
)
# Sub-headings *inside* the equity block — structure, not securities, but they
# must not close the section (that was the bug that made every parse stop at
# the first line of the equity table).
_EQUITY_SUBHEAD = re.compile(
    r"listed\s*/?\s*awaiting listing|unlisted|awaiting listing on|"
    r"^\(?[a-z]\)?[\.\)]?\s*$",
    re.IGNORECASE,
)
# Totals and spacers.
_TOTAL_ROW = re.compile(
    r"^\s*(sub\s*total|total|grand total|net asset|portfolio total)",
    re.IGNORECASE,
)


class HoldingsParseError(ValueError):
    """Raised when a workbook cannot be read into holdings with confidence."""


def _norm(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v if v is not None else "")).strip()


def _norm_header(v: Any) -> str:
    return _norm(v).lower()


def _find_columns(row: List[Any]) -> Optional[Dict[str, Optional[int]]]:
    """0-indexed positions of the columns we need, or None if this isn't a
    header row. Name + weight are mandatory: without a weight there is no
    weighted overlap to compute, and the caller should move on to the next
    candidate row rather than accept a partial header."""
    pos: Dict[str, Optional[int]] = {
        "name": None, "isin": None, "weight": None, "qty": None, "value": None,
    }
    for idx, raw in enumerate(row):
        h = _norm_header(raw)
        if not h:
            continue
        if pos["isin"] is None and any(k == h or k in h for k in _ISIN_HINTS):
            pos["isin"] = idx
            continue
        # Weight before value: "% to Net Assets" would also match nothing in
        # _VALUE_HINTS, but "market value" columns sometimes read "value (%)".
        if pos["weight"] is None and any(k in h for k in _WEIGHT_HINTS):
            pos["weight"] = idx
            continue
        if pos["name"] is None and any(k in h for k in _NAME_HINTS):
            pos["name"] = idx
            continue
        if pos["qty"] is None and any(k in h for k in _QTY_HINTS):
            pos["qty"] = idx
            continue
        if pos["value"] is None and any(k in h for k in _VALUE_HINTS):
            pos["value"] = idx
            continue
    if pos["name"] is None or pos["weight"] is None:
        return None
    return pos


def _to_float(v: Any) -> Optional[float]:
    """Parse a disclosed number. Handles '8.35%', '1,234.5', '(0.42)' and the
    en-dash/hyphen placeholders AMCs use for nil."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = _norm(v).replace(",", "").replace("%", "").strip()
    if not s or s in {"-", "--", "–", "—", "NA", "N.A.", "nil", "Nil", "NIL"}:
        return None
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    try:
        f = float(s)
    except ValueError:
        return None
    return -f if neg else f


def _classify(name_cell: str, isin_cell: str) -> str:
    """What kind of row is this? 'equity_section', 'other_section',
    'subhead', 'total', 'blank' or 'security'.

    An ISIN is the strongest possible signal that a row is a security, so it
    is checked first — some AMCs name a holding something that would
    otherwise look like a section heading (e.g. a company with "Finance" in
    its name sitting near a "Debt Instruments" pattern).
    """
    if isin_cell:
        return "security"
    if not name_cell:
        return "blank"
    if _TOTAL_ROW.match(name_cell):
        return "total"
    if _EQUITY_SECTION.search(name_cell):
        return "equity_section"
    if _OTHER_SECTION.search(name_cell):
        return "other_section"
    if _EQUITY_SUBHEAD.search(name_cell):
        return "subhead"
    return "security"


def parse_rows(header: List[Any], rows: List[List[Any]]) -> List[Dict[str, Any]]:
    """Turn an already-located header + body into equity holdings.

    Split out from the workbook plumbing so it can be tested against
    hand-built row lists — the layout variations that actually break parsing
    are cheap to express as literal rows and impossible to express as
    fixture .xlsx files checked into the repo.
    """
    pos = _find_columns(header)
    if pos is None:
        raise HoldingsParseError(
            "Header row has no recognisable instrument-name and %-to-net-assets columns."
        )

    def cell(row: List[Any], key: str) -> Any:
        i = pos[key]
        return row[i] if i is not None and i < len(row) else None

    out: List[Dict[str, Any]] = []
    in_equity = False
    seen_any_section = False

    for row in rows:
        name = _norm(cell(row, "name"))
        isin = _norm(cell(row, "isin"))
        kind = _classify(name, isin)

        if kind == "equity_section":
            in_equity = True
            seen_any_section = True
            continue
        if kind == "other_section":
            in_equity = False
            seen_any_section = True
            continue
        if kind in ("subhead", "total", "blank"):
            continue

        # kind == "security"
        if not in_equity:
            # Some AMCs omit the "Equity & Equity Related" heading entirely
            # and open straight into the equity table, so before ANY section
            # heading has been seen a security row is taken as equity. Once
            # the file has shown it uses headings — of any kind — that
            # default is off: an unheaded row then belongs to whichever
            # section is actually open, which is not equity.
            if seen_any_section:
                continue
        weight = _to_float(cell(row, "weight"))
        if weight is None:
            continue
        out.append({
            "name":       name,
            "isin":       isin or None,
            "weight_pct": weight,
            "quantity":   _to_float(cell(row, "qty")),
            "value":      _to_float(cell(row, "value")),
        })

    if not out:
        raise HoldingsParseError("No equity holdings found in this sheet.")
    return out


def _iter_sheet(ws) -> Tuple[Optional[List[Any]], List[List[Any]]]:
    """Locate the header row in a worksheet and return (header, body rows)."""
    rows_iter = ws.iter_rows(values_only=True)
    lead: List[List[Any]] = [
        list(r) if r else [] for _, r in zip(range(HEADER_SCAN_ROWS), rows_iter)
    ]
    header_at = None
    for i, r in enumerate(lead):
        if _find_columns(r) is not None:
            header_at = i
            break
    if header_at is None:
        return None, []

    body = [r for r in lead[header_at + 1:] if any(v not in (None, "") for v in r)]
    body.extend(list(r) for r in rows_iter if r and any(v not in (None, "") for v in r))
    return lead[header_at], body


def _scheme_name_above(ws, header_row_values: List[Any]) -> str:
    """Best-effort scheme name from the title block above the header.

    Multi-scheme workbooks put each scheme's name in a merged cell over its
    table; a single-scheme file puts it in the title block. Either way the
    longest text-only row above the header is overwhelmingly the scheme name.
    Returned as a hint for the operator to map to a scheme code — never used
    as an identity by itself.
    """
    best = ""
    for row in ws.iter_rows(min_row=1, max_row=HEADER_SCAN_ROWS, values_only=True):
        if row is None or list(row) == list(header_row_values):
            continue
        text = " ".join(_norm(v) for v in row if v not in (None, ""))
        if "portfolio" in text.lower() or "scheme" in text.lower() or "fund" in text.lower():
            if len(text) > len(best):
                best = text
    return best[:300]


def parse_workbook(content: bytes) -> List[Dict[str, Any]]:
    """Parse every sheet of a disclosure workbook.

    Returns one entry per sheet that yielded holdings::

        {"sheet": str, "scheme_hint": str, "holdings": [...]}

    Multi-scheme workbooks (the norm for the larger AMCs — one file, one
    sheet per scheme) therefore come back as several entries, and the caller
    maps each to a scheme code. Sheets that yield nothing are skipped
    silently: an AMC file legitimately contains index/notes/disclaimer
    sheets, and those are not errors.
    """
    wb = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    try:
        results: List[Dict[str, Any]] = []
        for ws in wb.worksheets:
            try:
                header, body = _iter_sheet(ws)
                if header is None or not body:
                    continue
                holdings = parse_rows(header, body)
            except HoldingsParseError:
                continue
            except Exception as exc:                      # pragma: no cover
                logger.warning("sheet %r failed to parse: %s", ws.title, exc)
                continue
            results.append({
                "sheet":       ws.title,
                "scheme_hint": _scheme_name_above(ws, header),
                "holdings":    holdings,
            })
        if not results:
            raise HoldingsParseError(
                "No sheet in this workbook contained a readable equity portfolio."
            )
        return results
    finally:
        wb.close()
