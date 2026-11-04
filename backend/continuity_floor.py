"""Shared "what date should truncate this fund's history for the Separate
Management Periods toggle" logic.

Previously computed independently, and near-identically, inside
fqs_refresh.py's and lumpsum_refresh.py's continuity pass — duplicated code
that could silently drift (exactly what continuity_select.py's own docstring
warns about for a different pair of call sites). Pulled out so both jobs
call one function and can never show two different break dates for the same
fund, and so the logic is unit-testable without a DB or a NAV fetch.

Three independent signals can each supply a continuity floor date. This
function does not pick one signal and ignore the others — it takes ALL that
apply and uses whichever gives the MOST RECENT date, because "Separate
Management Periods" means "since the fund's CURRENT identity took over", and
the most recent verified break is definitionally that date:

  1. PREDECESSOR STITCH BOUNDARY — amfi_client.load_analysis_nav's
     `predecessor_used` means this code's own NAV series has an older,
     DIFFERENT scheme code's history merged in ahead of it (AMC merger, true
     code change: JPMorgan -> Edelweiss, Morgan Stanley -> HDFC, etc). The
     floor is this code's own first NAV date — everything before it belongs
     to the predecessor code, not this one.

  2. FUND_LINEAGE AMC_CHANGE — scripts/build_lineage.py observes (from
     archived AMFI captures) or asserts (from fund_lineage.CURATED_AMC_CHANGES,
     source-verified) that this SAME scheme code was reported under a
     different fund-house name before some date — a rename/acquisition that
     did NOT change the scheme code (Reliance -> Nippon India, IDFC ->
     Bandhan, BOI AXA -> Bank of India, etc — the dominant real-world case).
     Stored as Action.AMC_CHANGE entries in fund_lineage.corporate_actions,
     covering the WHOLE universe generically once the lineage build has run —
     nothing here is fund-specific.

  3. FUND_CONTINUITY_EVENTS — the small manually-curated collection, kept as
     the escape hatch for a break with no automatic signal at all (e.g. a
     manager change with no AMC/code change).

A fund can have gone through several of these across its life — that full
timeline is what GET /api/funds/{code}/lineage exposes for the advisor to
inspect (see fund/[code].tsx's AMC History section). Scoring only ever needs
the CURRENT period's numbers, so only the latest matters here.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional, Tuple

ACTION_AMC_CHANGE = "AMC_CHANGE"


def amc_change_floor(lineage_doc: Optional[Dict[str, Any]]) -> Tuple[Optional[date], Optional[str]]:
    """Latest AMC_CHANGE action's date/reason from a fund_lineage document,
    or (None, None) if it has none. Pure — takes an already-fetched doc."""
    if not lineage_doc:
        return None, None
    best_date: Optional[date] = None
    best_to: Optional[str] = None
    for action in lineage_doc.get("corporate_actions", []) or []:
        if action.get("type") != ACTION_AMC_CHANGE:
            continue
        raw = action.get("effective_date")
        if not raw or raw == "live":
            continue
        try:
            d = date.fromisoformat(raw)
        except (TypeError, ValueError):
            continue
        if best_date is None or d > best_date:
            best_date, best_to = d, action.get("to")
    if best_date is None:
        return None, None
    return best_date, f"AMC change — fund house is now {best_to}" if best_to else "AMC change"


def compute_continuity_floor(
    *,
    predecessor_used: List[int],
    own_code_history_start: Optional[date],
    lineage_doc: Optional[Dict[str, Any]] = None,
    manual_event: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[date], Optional[str]]:
    """Combine all three signals into the single (floor_date, reason) the
    continuity-truncated score should use for THIS fund.

    `own_code_history_start` is the first date of the NAV series actually
    being scored (own-code-only — see amfi_client.load_analysis_nav(...,
    include_predecessor=False)); only meaningful, and only included as a
    candidate, when `predecessor_used` is non-empty (a fund with no
    predecessor stitch has no "boundary" — its own history simply starts
    when it started, which is not itself a management-change signal).
    """
    candidates: List[Tuple[date, str]] = []

    if predecessor_used and own_code_history_start:
        candidates.append((own_code_history_start, "Merger / successor scheme change"))

    amc_date, amc_reason = amc_change_floor(lineage_doc)
    if amc_date and amc_reason:
        candidates.append((amc_date, amc_reason))

    if manual_event and manual_event.get("break_date"):
        try:
            d = date.fromisoformat(manual_event["break_date"])
            candidates.append((d, manual_event.get("reason") or "Management change"))
        except (TypeError, ValueError):
            pass

    if not candidates:
        return None, None
    return max(candidates, key=lambda c: c[0])
