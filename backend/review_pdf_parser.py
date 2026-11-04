"""Read a Portfolio Proposal PDF (as produced by `proposal_pdf.build_pdf`) back
into structured data, for the Portfolio Holding Review feature.

Pure module — no DB, no network. Two sources, tried in order:

  1. EMBEDDED REFERENCE. Proposal PDFs generated since this feature shipped
     carry `arthvruksh:proposal:<id>:v1` in the PDF Keywords metadata (see
     `proposal_pdf.build_pdf`). `read_reference()` surfaces it so the caller
     can load the stored proposal document — exact numbers, no text parsing.
  2. TEXT/TABLE EXTRACTION (`parse_proposal_pdf`) — works on every proposal
     PDF ever generated, including those with no metadata. It locates the
     fund table by its HEADER NAMES (not by position or page), so a different
     number of funds/pages, extra whitespace, or a missing value does not
     break it.

Nothing is invented: a value that cannot be read confidently is returned as
None and listed in `needs_review`/`missing`, never defaulted to 0.
Scanned/image-only PDFs have no text layer and raise `PdfParseError`.
"""

from __future__ import annotations

import io
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

REFERENCE_RE = re.compile(r"arthvruksh:(proposal|review):([A-Za-z0-9_-]+):v(\d+)")
MAX_PAGES = 60

# Which proposal-table header ⇒ which field. Matched on the header with ALL
# whitespace removed and lower-cased, so "Median\nWM", "Median WM" and
# "MedianWM" are the same header.
_HEADER_FIELDS: List[Tuple[str, str]] = [
    ("fund", "fund_name"),
    ("category", "category"),
    ("benchmarkxirr", "benchmark_xirr"),
    ("allocation", "allocation"),
    ("windows", "n_windows"),
    ("fqs", "fqs"),
    ("success", "success_pct"),
    ("medianwm", "wm_med"),
    ("xirr(med)", "irr_med"),
    ("p10wm", "wm_p10"),
    ("xirr(p10)", "irr_p10"),
    ("cv", "cv"),
]
# fields stored as percentages in the PDF → fractions internally
_PERCENT_FIELDS = {"benchmark_xirr", "allocation", "success_pct", "irr_med", "irr_p10", "cv"}
METRIC_FIELDS = ["fqs", "wm_p10", "wm_med", "irr_p10", "irr_med", "cv"]


class PdfParseError(ValueError):
    """The file cannot be used as a proposal baseline (message is user-facing)."""


def _squash(s: Any) -> str:
    return re.sub(r"\s+", "", str(s or "")).lower()


def _clean(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def parse_number(raw: Any, percent: bool = False) -> Optional[float]:
    """'1.86' / '19.3%' / '2.92x' / 'Rs. 2,40,000' → float; '—', '-', '--', '' → None.
    Percent fields are returned as fractions (19.3% → 0.193)."""
    t = _clean(raw)
    if not t or t in {"—", "-", "--", "–", "n/a", "N/A", "NA"}:
        return None
    t = re.sub(r"(?i)rs\.?|₹|x$|%|,", "", t).strip()
    try:
        v = float(t)
    except ValueError:
        return None
    return round(v / 100.0, 8) if percent else v


def parse_date(raw: str) -> Optional[date]:
    t = _clean(raw)
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d-%b-%y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def read_reference(pdf_bytes: bytes) -> Optional[Dict[str, Any]]:
    """The embedded {kind, id, version} reference, or None for older PDFs."""
    try:
        from pypdf import PdfReader
        meta = PdfReader(io.BytesIO(pdf_bytes)).metadata or {}
    except Exception:
        return None
    blob = " ".join(str(meta.get(k) or "") for k in ("/Keywords", "/Subject"))
    m = REFERENCE_RE.search(blob)
    return {"kind": m.group(1), "id": m.group(2), "version": int(m.group(3))} if m else None


def _open(pdf_bytes: bytes):
    import pdfplumber
    try:
        return pdfplumber.open(io.BytesIO(pdf_bytes))
    except Exception as exc:
        raise PdfParseError("This file could not be read as a PDF (it may be corrupt or password-protected).") from exc


def _map_header(row: List[Any]) -> Optional[Dict[int, str]]:
    """column index → field, when `row` looks like the fund-table header."""
    cols: Dict[int, str] = {}
    for i, cell in enumerate(row):
        h = _squash(cell)
        for key, field in _HEADER_FIELDS:
            if h == key or (key != "fund" and h.startswith(key)):
                cols[i] = field
                break
    if "fund_name" in cols.values() and "fqs" in cols.values():
        return cols
    return None


def _pdf_text(page) -> str:
    return page.extract_text() or ""


def _footnote_horizons(full_text: str) -> Dict[str, int]:
    """{normalised fund name: rolling years} from the '* A (6Y), B (5Y) — this
    fund has a shorter track record…' footnote."""
    flat = re.sub(r"\s+", " ", full_text)
    end = re.search(r"\s—\s(?:this fund|these funds)", flat)
    out: Dict[str, int] = {}
    if not end:
        return out
    # The footnote starts at the LAST "* " before its closing sentence (earlier
    # asterisks belong to fund-table rows).
    start = flat.rfind("* ", 0, end.start())
    if start < 0:
        return out
    for nm, yrs in re.findall(r"(.+?)\s\((\d+)Y\)(?:,\s*|$)", flat[start + 2:end.start()]):
        out[_squash(nm)] = int(yrs)
    return out


def _find(pattern: str, text: str, flags: int = re.I) -> Optional[str]:
    m = re.search(pattern, text, flags)
    return _clean(m.group(1)) if m else None


def parse_proposal_pdf(pdf_bytes: bytes) -> Dict[str, Any]:
    """Structured data from a proposal PDF.

    Returns {client, plan, funds, warnings, needs_review}. Raises PdfParseError
    for corrupt / image-only / non-proposal files.
    """
    pdf = _open(pdf_bytes)
    warnings: List[str] = []
    with pdf:
        if len(pdf.pages) > MAX_PAGES:
            raise PdfParseError(f"This PDF has {len(pdf.pages)} pages; a proposal is expected to be far shorter.")
        texts = [_pdf_text(p) for p in pdf.pages]
        full = "\n".join(texts)
        if len(re.sub(r"\s+", "", full)) < 40:
            raise PdfParseError(
                "This PDF has no readable text (it looks scanned or image-only). "
                "Upload the original proposal PDF generated by ArthVruksh."
            )

        header_map: Optional[Dict[int, str]] = None
        raw_rows: List[Dict[str, Any]] = []
        for page in pdf.pages:
            try:
                tables = page.extract_tables()
            except Exception:
                tables = []
            for tbl in tables:
                if not tbl:
                    continue
                first = _map_header(tbl[0])
                body = tbl[1:] if first else tbl
                if first:
                    header_map = first
                elif header_map is None or len(tbl[0]) != max(header_map) + 1:
                    continue   # not a continuation of the fund table
                for r in body:
                    if not r or not any(_clean(c) for c in r):
                        continue
                    rec = {field: r[i] if i < len(r) else None for i, field in header_map.items()}
                    if _clean(rec.get("fund_name")):
                        raw_rows.append(rec)

    if not raw_rows:
        raise PdfParseError(
            "No fund table was found in this PDF. Upload a Portfolio Proposal PDF generated by ArthVruksh."
        )

    client_name = _find(r"Client Name\s+(.+?)\s+Date\s+\d", full) or _find(r"Prepared for:\s*(.+)", full)
    pan = _find(r"\bPAN\s+(\S+)\s+Horizon", full)
    phone = _find(r"Phone Number\s+(\S+)\s+Investment Mode", full)
    prop_date = parse_date(_find(r"\bDate\s+(\d{4}-\d{2}-\d{2})", full) or "") or None
    mode_raw = _find(r"Investment Mode\s+(SIP|Lumpsum)", full)
    horizon_raw = _find(r"Horizon\s+(\d+)\s*Y", full) or _find(r"(\d+)-Year Horizon", full)
    amount_raw = _find(r"(?:Monthly SIP amount|Lumpsum amount)\s+Rs\.?\s*([\d,\.]+)", full)
    arn = _find(r"\(ARN\):\s*(\d+)", full) or _find(r"ARN\s*#?\s*(\d+)", full)

    def _dash(v: Optional[str]) -> Optional[str]:
        return None if v is None or v in {"—", "-", "–", ""} else v

    mode = mode_raw.lower() if mode_raw else None
    horizon = int(horizon_raw) if horizon_raw else None
    needs_review: List[str] = []
    if not client_name:
        needs_review.append("client.name")
    if prop_date is None:
        needs_review.append("proposal_date")
    if horizon is None:
        needs_review.append("plan.horizon")
    if mode is None:
        needs_review.append("plan.mode")

    foot = _footnote_horizons(full)
    funds: List[Dict[str, Any]] = []
    for rec in raw_rows:
        name_raw = _clean(rec.get("fund_name"))
        extrapolated = name_raw.endswith("*")
        name = name_raw.rstrip("*").strip()
        rolling = foot.get(_squash(name)) if extrapolated else horizon
        status_notes: List[str] = []
        if extrapolated and rolling is None:
            status_notes.append("rolling_year")        # asterisk but footnote unreadable
        row: Dict[str, Any] = {
            "fund_name": name,
            "category": _clean(rec.get("category")) or None,
            "rolling_year": rolling,
            "allocation": parse_number(rec.get("allocation"), percent=True),
            "benchmark_xirr": parse_number(rec.get("benchmark_xirr"), percent=True),
            "n_windows": (int(v) if (v := parse_number(rec.get("n_windows"))) is not None else None),
            "previous": {},
            "extrapolated": extrapolated,
        }
        bad: List[str] = []
        for f in METRIC_FIELDS:
            raw = rec.get(f)
            val = parse_number(raw, percent=f in _PERCENT_FIELDS)
            if val is None and _clean(raw) not in {"", "—", "-", "–", "--"}:
                bad.append(f)                           # something is there but unreadable
            row["previous"][f] = val
        missing = [f for f in METRIC_FIELDS if row["previous"][f] is None and f not in bad]
        row["unreadable"] = bad + status_notes
        row["missing"] = missing
        funds.append(row)

    if len(funds) > 0 and not any(f["previous"]["fqs"] is not None for f in funds):
        warnings.append("No FQS values could be read from the fund table — please verify the figures.")

    return {
        "client": {
            "name": client_name, "pan": _dash(pan), "phone": _dash(phone),
        },
        "plan": {
            "mode": mode, "goal_years": horizon,
            "amount": parse_number(amount_raw) if amount_raw else None,
            "arn": arn,
        },
        "proposal_date": prop_date.isoformat() if prop_date else None,
        "funds": funds,
        "warnings": warnings,
        "needs_review": needs_review,
    }
