"""AMFI snapshot ingestion — the authoritative spine for fund lineage.

Why this module exists
──────────────────────
`mfapi.in` serves NAV history per scheme code, but it only ever reports a
scheme's CURRENT name and carries no ISIN at all. That makes three of the
corporate actions we care about invisible from mfapi alone:

  • scheme renames      (same code, different name over time)
  • fund-house renames  (Reliance → Nippon India, IDFC → Bandhan, …)
  • ISIN changes

AMFI publishes two files that close all three gaps:

  1. LIVE      https://www.amfiindia.com/spages/NAVAll.txt
     Today's snapshot of every ACTIVE scheme, grouped by AMC and scheme type,
     WITH ISINs. Format (6 fields, ';'-separated):
         Scheme Code;ISIN Growth;ISIN Reinvest;Scheme Name;NAV;Date
     Also carries the AMC name and SEBI scheme-type as section headers, which
     the historical file does NOT — this is our only machine-readable source
     for "which AMC owns this scheme code today".

  2. HISTORICAL  https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx
                 ?frmdt=DD-Mon-YYYY&todt=DD-Mon-YYYY
     Every scheme that published a NAV in the window, WITH the name and ISIN
     AS THEY STOOD ON THAT DATE. Format (8 fields, ';'-separated):
         Scheme Code;Scheme Name;ISIN Growth;ISIN Reinvest;Repurchase;Sale;NAV;Date
     NOTE the column order differs from the live file — name and ISIN are
     swapped. Parsing them with one routine would silently read names as ISINs.

  3. ARCHIVED  http://web.archive.org/web/<ts>id_/…/NAVAll.txt
     Internet Archive captures of file (1), in the SAME 6-field format.

CRITICAL: the historical report (2) does NOT preserve historical names
─────────────────────────────────────────────────────────────────────
It back-fills the CURRENT name onto historical rows for any scheme code that
still exists. Verified: scheme 118778 was "Reliance Small Cap Fund" until the
Oct-2019 house rename, yet the report returns "Nippon India Small Cap Fund"
for the Apr-2014 window. Across 9,220 codes present in both the Apr-2019 and
Apr-2020 windows, ZERO name differences appear — the rename is invisible.

Old names survive in (2) only for codes that were already WOUND UP at the time
(nothing current to back-fill to) — e.g. the Apr-2010 window still contains 105
"Reliance …" rows for schemes that died before 2019.

So name history for LIVE funds must come from (3). Archived captures are true
point-in-time: the 2017-10-12 capture reads "Reliance Small Cap Fund" for code
118778 with 1,274 "Reliance" rows and zero "Nippon"; the 2019-04-01 capture
reads "Nippon India …" with 449 "Nippon" rows. That is the rename, observed.

Source roles, therefore:
  (1) live      → current name / ISIN / AMC / scheme type, active flag
  (2) historical→ WHICH CODES EXISTED WHEN (back to ~2006) for wound-up dating,
                  plus genuine old names for already-dead codes
  (3) archived  → point-in-time names and ISINs for still-live codes, i.e. the
                  only source that can prove a rename of a surviving fund

Archive coverage is finite: 26 usable captures spanning 2016–2025, with no
captures in 2018, 2021 or 2023. A rename can therefore only be dated to the
gap between two captures, and renames before 2016 are not recoverable this
way at all. Callers must not present a capture date as an effective date.

Measured characteristics (verified against the live endpoints, Aug 2026)
───────────────────────────────────────────────────────────────────────
  • Live file: 14,234 schemes, 13,433 with a growth ISIN, 52 AMCs.
  • mfapi has 37,713 codes; only 14,222 overlap the live AMFI file. The other
    ~23,500 are wound-up/merged schemes — precisely the predecessors lineage
    needs, and precisely what the live file cannot see. Historical snapshots
    are the only way to reach them.
  • The historical report returns ONLY schemes that published a NAV inside the
    window, so a single day is a poor sample (01-Apr-2012 → 267 codes) while a
    7-day window is a good one (same quarter → 4,772 codes). A full month adds
    ~2% more codes for ~4x the bytes, so SNAPSHOT_WINDOW_DAYS = 7.
  • Coverage begins around 2006. Jan-2003 returns an empty report. Anything
    earlier than ~2006 is NOT available from this source and must not be
    presented as if it were — see LINEAGE_COVERAGE_START.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import httpx

logger = logging.getLogger("amfi_history")

LIVE_URL = "https://www.amfiindia.com/spages/NAVAll.txt"
HISTORY_URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"

# The historical report only returns schemes that printed a NAV inside the
# window; 7 days captures essentially every scheme active in that quarter.
SNAPSHOT_WINDOW_DAYS = 7

# AMFI's downloadable history does not reach back to 2000. Verified: Jan-2003
# returns an empty report, Apr-2006 returns ~1,900 schemes. Callers must treat
# anything before this as "not covered by this source" rather than "no change".
LINEAGE_COVERAGE_START = date(2006, 1, 1)

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _amfi_date(d: date) -> str:
    """AMFI wants DD-Mon-YYYY, e.g. 01-Apr-2016."""
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def _parse_nav_date(s: str) -> Optional[date]:
    s = s.strip()
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _clean(v: Optional[str]) -> str:
    """AMFI writes '-' and 'N.A.' for absent values; normalise them to ''."""
    v = (v or "").strip()
    return "" if v in {"-", "N.A.", "NA", "n.a."} else v


# Indian mutual-fund ISINs are 12 chars: 'INF' + 9 alphanumerics.
_ISIN_RE = __import__("re").compile(r"^INF[A-Z0-9]{9}$")


def is_valid_isin(v: str) -> bool:
    """True for a well-formed Indian MF ISIN.

    AMFI's own files contain occasional data-entry errors — observed in the
    Apr-2016 window: 'NF336L01FZ2' (dropped leading I) and 'IINF277K01Y4'
    (doubled I). We do NOT auto-repair these: 'NF336L01FZ2' could plausibly be
    corrected several ways and a wrong ISIN silently maps one fund's history
    onto another. Callers should route invalid values to manual review instead
    of indexing them as identifiers.
    """
    return bool(_ISIN_RE.match((v or "").strip().upper()))


@dataclass
class SchemeObservation:
    """One scheme as AMFI reported it on one date."""
    scheme_code: int
    scheme_name: str
    isin_growth: str
    isin_reinvest: str
    nav_date: Optional[str]      # ISO
    amc_name: str = ""           # live file only (historical report has no AMC)
    scheme_type: str = ""        # live file only

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── Parsers ──────────────────────────────────────────────────────────────────
# Deliberately two functions, not one with a flag: the live and historical
# files put NAME and ISIN in different columns, and a shared parser would read
# a scheme name into the ISIN field without ever failing loudly.

def parse_live_file(text: str) -> List[SchemeObservation]:
    """Parse NAVAll.txt (6 cols: code;isin_g;isin_r;name;nav;date).

    Non-';' lines are section headers: a SEBI scheme-type line (recognised by
    its 'Open Ended'/'Close Ended'/'Interval' prefix) or, otherwise, an AMC
    name. Tracking both is what gives each scheme its fund house.
    """
    out: List[SchemeObservation] = []
    amc = ""
    scheme_type = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if ";" not in line:
            if line.startswith(("Open Ended", "Close Ended", "Interval")):
                scheme_type = line
            else:
                amc = line
            continue
        parts = line.split(";")
        if len(parts) < 6 or not parts[0].strip().isdigit():
            continue  # the header row, or a malformed line
        d = _parse_nav_date(parts[5])
        out.append(SchemeObservation(
            scheme_code=int(parts[0].strip()),
            scheme_name=_clean(parts[3]),
            isin_growth=_clean(parts[1]),
            isin_reinvest=_clean(parts[2]),
            nav_date=d.isoformat() if d else None,
            amc_name=amc,
            scheme_type=scheme_type,
        ))
    return out


def parse_history_report(text: str) -> List[SchemeObservation]:
    """Parse DownloadNAVHistoryReport_Po (8 cols: code;name;isin_g;isin_r;...;date).

    Deduplicates within the window, keeping the LATEST observation per scheme
    code — a 7-day window prints the same scheme several times and we want the
    name/ISIN as they stood at the end of the window.
    """
    latest: Dict[int, Tuple[Optional[date], SchemeObservation]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if ";" not in line:
            continue
        parts = line.split(";")
        if len(parts) < 8 or not parts[0].strip().isdigit():
            continue
        code = int(parts[0].strip())
        d = _parse_nav_date(parts[7])
        obs = SchemeObservation(
            scheme_code=code,
            scheme_name=_clean(parts[1]),
            isin_growth=_clean(parts[2]),
            isin_reinvest=_clean(parts[3]),
            nav_date=d.isoformat() if d else None,
        )
        prev = latest.get(code)
        if prev is None or (d and prev[0] and d > prev[0]) or prev[0] is None:
            latest[code] = (d, obs)
    return [obs for _, obs in latest.values()]


# ── Fetchers ─────────────────────────────────────────────────────────────────

async def fetch_live(client: Optional[httpx.AsyncClient] = None) -> List[SchemeObservation]:
    """Today's active-scheme snapshot, with AMC and scheme type."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=90.0, follow_redirects=True)
    try:
        r = await client.get(LIVE_URL)
        r.raise_for_status()
        obs = parse_live_file(r.text)
        logger.info("AMFI live snapshot: %d schemes", len(obs))
        return obs
    finally:
        if own:
            await client.aclose()


async def fetch_window(
    start: date,
    end: date,
    client: Optional[httpx.AsyncClient] = None,
) -> List[SchemeObservation]:
    """Historical snapshot for [start, end]. Empty list when AMFI has no data."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=180.0, follow_redirects=True)
    try:
        r = await client.get(
            HISTORY_URL,
            params={"frmdt": _amfi_date(start), "todt": _amfi_date(end)},
        )
        r.raise_for_status()
        obs = parse_history_report(r.text)
        logger.info("AMFI window %s..%s: %d schemes", start, end, len(obs))
        return obs
    finally:
        if own:
            await client.aclose()


WAYBACK_CDX = "http://web.archive.org/cdx/search/cdx"
WAYBACK_SNAPSHOT = "http://web.archive.org/web/{ts}id_/https://www.amfiindia.com/spages/NAVAll.txt"


async def list_wayback_captures(
    limit: int = 400,
    client: Optional[httpx.AsyncClient] = None,
) -> List[str]:
    """Timestamps of archived NAVAll.txt captures, oldest first.

    `collapse=timestamp:6` keeps at most one capture per month — captures
    minutes apart carry identical content and would only cost bandwidth.
    """
    own = client is None
    client = client or httpx.AsyncClient(timeout=120.0, follow_redirects=True)
    try:
        r = await client.get(WAYBACK_CDX, params={
            "url": "amfiindia.com/spages/NAVAll.txt",
            "output": "json", "fl": "timestamp,statuscode",
            "filter": "statuscode:200", "collapse": "timestamp:6",
            "limit": str(limit),
        })
        r.raise_for_status()
        rows = r.json()
        stamps = sorted({row[0] for row in rows[1:]}) if len(rows) > 1 else []
        logger.info("Wayback: %d NAVAll.txt captures", len(stamps))
        return stamps
    except Exception as exc:
        logger.warning("Wayback CDX listing failed: %s", exc)
        return []
    finally:
        if own:
            await client.aclose()


async def fetch_wayback(
    timestamp: str,
    client: Optional[httpx.AsyncClient] = None,
) -> List[SchemeObservation]:
    """One archived NAVAll.txt capture, parsed with the LIVE-file layout.

    The `id_` modifier asks the archive for the raw original bytes with no
    rewriting banner injected, so the file parses exactly like the live one.
    """
    own = client is None
    client = client or httpx.AsyncClient(timeout=240.0, follow_redirects=True)
    try:
        r = await client.get(WAYBACK_SNAPSHOT.format(ts=timestamp))
        r.raise_for_status()
        obs = parse_live_file(r.text)
        logger.info("Wayback capture %s: %d schemes", timestamp, len(obs))
        return obs
    finally:
        if own:
            await client.aclose()


def wayback_date(timestamp: str) -> str:
    """'20171012144325' → '2017-10-12' (ISO), for keying snapshots."""
    return f"{timestamp[0:4]}-{timestamp[4:6]}-{timestamp[6:8]}"


async def crawl_wayback(
    timestamps: Sequence[str],
    concurrency: int = 2,
    on_progress=None,
) -> Dict[str, List[SchemeObservation]]:
    """Fetch archived captures, keyed by ISO capture date.

    Concurrency is 2 — the Internet Archive rate-limits aggressively and a
    failed capture just leaves a gap in name history rather than breaking it.
    """
    sem = asyncio.Semaphore(concurrency)
    out: Dict[str, List[SchemeObservation]] = {}

    async with httpx.AsyncClient(timeout=240.0, follow_redirects=True) as client:
        async def one(ts: str) -> None:
            async with sem:
                try:
                    obs = await fetch_wayback(ts, client)
                    if obs:
                        out[wayback_date(ts)] = obs
                except Exception as exc:
                    logger.warning("Wayback capture %s failed: %s", ts, exc)
                if on_progress:
                    on_progress(ts, len(out.get(wayback_date(ts), [])))

        await asyncio.gather(*[one(t) for t in timestamps])
    return out


def quarterly_windows(
    start: date = LINEAGE_COVERAGE_START,
    end: Optional[date] = None,
) -> List[Tuple[date, date]]:
    """One SNAPSHOT_WINDOW_DAYS window at the head of each quarter.

    ~4 windows/year. From 2006 to today that is ~80 requests — enough to place
    every rename to within a quarter, which is the resolution corporate-action
    records actually need.
    """
    end = end or date.today()
    windows: List[Tuple[date, date]] = []
    y, q = start.year, (start.month - 1) // 3
    while True:
        first = date(y, q * 3 + 1, 1)
        if first > end:
            break
        last = min(first + timedelta(days=SNAPSHOT_WINDOW_DAYS - 1), end)
        if first >= start:
            windows.append((first, last))
        q += 1
        if q == 4:
            q, y = 0, y + 1
    return windows


async def crawl_snapshots(
    windows: Iterable[Tuple[date, date]],
    concurrency: int = 3,
    on_progress=None,
) -> Dict[str, List[SchemeObservation]]:
    """Fetch many windows concurrently. Keyed by the window's start date (ISO).

    Concurrency is deliberately low — this is AMFI's public portal, and each
    response is ~1 MB. A failed window is logged and skipped rather than
    aborting the crawl, so a partial run still yields usable lineage.
    """
    sem = asyncio.Semaphore(concurrency)
    results: Dict[str, List[SchemeObservation]] = {}

    async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as client:
        async def one(w: Tuple[date, date]) -> None:
            start, end = w
            async with sem:
                try:
                    results[start.isoformat()] = await fetch_window(start, end, client)
                except Exception as exc:
                    logger.warning("AMFI window %s..%s failed: %s", start, end, exc)
                    results[start.isoformat()] = []
                if on_progress:
                    on_progress(start, len(results[start.isoformat()]))

        await asyncio.gather(*[one(w) for w in windows])
    return results
