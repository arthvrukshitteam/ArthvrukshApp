"""holdings_ingest.py — fetch AMC monthly portfolio disclosures into
``fund_holdings``.

Pipeline, per disclosure file:

    URL → bytes → holdings_parser.parse_workbook → [sheet, scheme_hint,
    holdings] → scheme code → holdings_store.save_holdings

    ── Scheme-code resolution is NOT re-implemented here ──
Mapping a prose scheme name ("Parag Parikh Flexi Cap Fund - Regular Growth")
to an AMFI scheme code is the exact problem `bulk_analysis.match_fund` already
solves for advisor Excel uploads, including the code/ISIN/normalised-name/
name-stem tier ladder and the plan-variant tie-break. This module calls it
rather than growing a second, worse matcher that would drift from it. A sheet
whose scheme cannot be resolved is REPORTED, never guessed at — storing a
portfolio under the wrong scheme code would silently corrupt every overlap
number computed from it.

    ── Memory ──
The service this runs on has 512MB and is already under memory pressure, so:
`MAX_FILE_BYTES` caps any single download; files are fetched and parsed one
at a time (never gathered); each file's bytes are dropped before the next is
fetched; and sheets are stored as they are resolved rather than accumulated.
Ingestion is deliberately sequential and slow — it is a monthly background
job, and throughput is worth nothing if the process is OOM-killed halfway.

    ── On scraping AMC sites ──
AMFI's disclosure index links out to ~40 AMC websites, each with its own page
structure, and those pages change without notice. `discover_amfi_sources`
does a best-effort scrape of the index for links that look like disclosure
workbooks, but the durable path is `AMC_SOURCES`: an explicit registry of
direct workbook URL templates, which an operator fills in and which survives
an AMC redesigning its site. Ingestion works from any URL, so a broken
discovery never blocks a manual ingest.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

import bulk_analysis as ba
import holdings_store
from holdings_parser import HoldingsParseError, parse_workbook

logger = logging.getLogger(__name__)

AMFI_DISCLOSURE_INDEX = (
    "https://www.amfiindia.com/research-information/other-data/monthly-portfolio-disclosures"
)

MAX_FILE_BYTES = 40 * 1024 * 1024      # a large AMC's full-house file
FETCH_TIMEOUT  = 120.0
BETWEEN_FILES_SECONDS = 1.0            # be a polite client to AMC servers

# Direct workbook URLs per AMC. `{period}` is substituted with YYYY-MM and
# `{mon}`/`{year}` with e.g. "aug"/"2026" — AMCs use both conventions.
# Intentionally empty at rest: a wrong URL guessed here would fail silently
# every month, which is worse than an operator adding the real one once.
AMC_SOURCES: Dict[str, str] = {}

_WORKBOOK_LINK = re.compile(r'href="([^"]+\.(?:xlsx|xls|zip))"', re.IGNORECASE)


def current_period() -> str:
    """Disclosures are published for the *previous* month, so the period an
    operator most likely wants is last month, not today's."""
    now = datetime.now(timezone.utc)
    year, month = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    return f"{year:04d}-{month:02d}"


def normalize_period(raw: Optional[str]) -> str:
    """Accept 'YYYY-MM' and reject anything else — a malformed period would
    silently create an unqueryable bucket that nothing ever reads back."""
    if not raw:
        return current_period()
    s = str(raw).strip()
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", s):
        raise ValueError(f"period must be YYYY-MM, got {raw!r}")
    return s


async def fetch_workbook(url: str) -> bytes:
    """Download one disclosure workbook, refusing anything oversized.

    The size check runs against the streamed byte count rather than trusting
    Content-Length, since a server that lies (or omits it) would otherwise
    walk straight past the cap and into the OOM this whole module is written
    to avoid.
    """
    chunks: List[bytes] = []
    size = 0
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise ValueError(
                        f"Disclosure file exceeds {MAX_FILE_BYTES // (1024 * 1024)}MB cap: {url}"
                    )
                chunks.append(chunk)
    return b"".join(chunks)


async def discover_amfi_sources() -> List[str]:
    """Best-effort list of workbook URLs linked from AMFI's disclosure index.

    Returns absolute URLs only. This is a convenience for an operator, not a
    dependable feed — see the module docstring. Failure returns an empty list
    rather than raising, so a scheduled run that finds nothing logs and moves
    on instead of taking the job down.
    """
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            resp = await client.get(AMFI_DISCLOSURE_INDEX)
            resp.raise_for_status()
            html = resp.text
    except Exception as exc:
        logger.warning("AMFI disclosure index fetch failed: %s", exc)
        return []

    urls: List[str] = []
    for href in _WORKBOOK_LINK.findall(html):
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = "https://www.amfiindia.com" + href
        elif not href.lower().startswith("http"):
            continue
        urls.append(href)
    return sorted(set(urls))


async def ingest_workbook_bytes(
    db,
    content: bytes,
    period: str,
    *,
    amc: str = "",
    source_url: str = "",
    indexes: Optional[Tuple[Dict, Dict, Dict]] = None,
) -> Dict[str, Any]:
    """Parse one workbook and store every sheet it can resolve to a scheme.

    `indexes` is the (by_code, by_norm_name, by_stem) triple from
    `bulk_analysis.build_master_index`. It is passed in so a multi-file run
    builds the 37k-fund master index ONCE rather than per file — rebuilding
    it per file is both slow and the kind of repeated large allocation that
    tips this service over its memory limit.
    """
    if indexes is None:
        indexes = await ba.build_master_index()
    by_code, by_norm_name, by_stem = indexes

    sheets = parse_workbook(content)

    stored: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []

    for sheet in sheets:
        hint = sheet.get("scheme_hint") or sheet.get("sheet") or ""
        code = await ba.match_fund(
            db, by_code, by_norm_name, by_stem, name_val=hint,
        )
        if code is None:
            # Sheet tab names are often the scheme name too, and sometimes
            # cleaner than the title block (which can carry "Monthly
            # Portfolio Statement as on 31-Aug-2026" noise). Worth one more
            # try before giving up on the sheet.
            code = await ba.match_fund(
                db, by_code, by_norm_name, by_stem, name_val=sheet.get("sheet"),
            )
        if code is None:
            unresolved.append({
                "sheet":        sheet.get("sheet"),
                "scheme_hint":  hint[:160],
                "equity_count": len(sheet["holdings"]),
            })
            continue

        summary = await holdings_store.save_holdings(
            db,
            scheme_code=code,
            period=period,
            holdings=sheet["holdings"],
            scheme_name=by_code.get(code, hint)[:300],
            amc=amc,
            source_url=source_url,
        )
        stored.append(summary)

    return {
        "period":       period,
        "sheets_found": len(sheets),
        "stored":       stored,
        "unresolved":   unresolved,
    }


async def ingest_url(
    db,
    url: str,
    period: Optional[str] = None,
    *,
    amc: str = "",
    indexes: Optional[Tuple[Dict, Dict, Dict]] = None,
) -> Dict[str, Any]:
    """Fetch + ingest one disclosure URL."""
    per = normalize_period(period)
    content = await fetch_workbook(url)
    try:
        return await ingest_workbook_bytes(
            db, content, per, amc=amc, source_url=url, indexes=indexes
        )
    finally:
        del content        # drop several MB before the caller's next fetch


async def ingest_many(
    db, urls: List[str], period: Optional[str] = None
) -> Dict[str, Any]:
    """Ingest a list of disclosure URLs sequentially.

    Sequential on purpose (see module docstring): concurrency here buys
    little — the job runs monthly — and costs peak memory this service does
    not have. One failing AMC never aborts the run; it is recorded and the
    next URL proceeds.
    """
    per = normalize_period(period)
    indexes = await ba.build_master_index()

    results: List[Dict[str, Any]] = []
    failures: List[Dict[str, str]] = []
    for i, url in enumerate(urls):
        try:
            res = await ingest_url(db, url, per, indexes=indexes)
            results.append({"url": url, **{k: v for k, v in res.items() if k != "stored"},
                            "stored_count": len(res["stored"])})
            logger.info("Ingested %s — %d schemes stored", url, len(res["stored"]))
        except (HoldingsParseError, ValueError, httpx.HTTPError) as exc:
            failures.append({"url": url, "error": str(exc)[:300]})
            logger.warning("Ingest failed for %s: %s", url, exc)
        except Exception as exc:                                  # pragma: no cover
            failures.append({"url": url, "error": f"unexpected: {exc}"[:300]})
            logger.exception("Unexpected ingest failure for %s", url)
        if i + 1 < len(urls):
            await asyncio.sleep(BETWEEN_FILES_SECONDS)

    return {
        "period":    per,
        "attempted": len(urls),
        "succeeded": len(results),
        "failed":    len(failures),
        "results":   results,
        "failures":  failures,
    }
