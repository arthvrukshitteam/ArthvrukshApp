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


# ─── Every break, not just the latest ─────────────────────────────────────────
#
# Scoring the "Separate Management Periods" view only ever needs the most
# recent break (above). Letting the advisor CHOOSE which management era to
# judge a fund from — e.g. 2018–now vs 2016–now vs 2010–now for a fund whose
# fund house changed three times — needs all of them.


def curated_amc_actions(
    fund_house: Optional[str],
    existed_from: Optional[str],
    events: List[Dict[str, Any]],
    canonical_house_fn,
) -> List[Dict[str, Any]]:
    """AMC_CHANGE actions implied by the curated house-level table
    (fund_lineage.CURATED_AMC_CHANGES) for one fund, WITHOUT needing the
    lineage build to have run.

    Same rule as lineage_store.apply_amc_changes: the fund's CURRENT house
    must canonicalise to the event's `to_house`, and the fund must have
    positive evidence of existing before the change (`existed_from` — its
    first NAV date — on or before the effective date). A fund launched after
    the change never went through it and gets nothing.
    """
    if not fund_house or not existed_from:
        return []
    current = canonical_house_fn(fund_house)
    if not current:
        return []
    out: List[Dict[str, Any]] = []
    for ev in events:
        eff = ev.get("effective_date")
        if not eff or existed_from > eff:
            continue
        if canonical_house_fn(ev.get("to_house", "")) != current:
            continue
        out.append({
            "type": ACTION_AMC_CHANGE,
            "effective_date": eff,
            "from": ev.get("from_house"),
            "to": ev.get("to_house"),
            "source": "CURATED",
        })
    return out


def lineage_or_curated(
    lineage_doc: Optional[Dict[str, Any]],
    fund_house: Optional[str],
    existed_from: Optional[str],
) -> Optional[Dict[str, Any]]:
    """The lineage doc when the lineage build produced one; otherwise a
    stand-in carrying the curated AMC changes that apply to this fund.

    The nightly continuity pass used to see AMC changes ONLY through
    fund_lineage, which exists only after an operator runs the (long,
    manual) lineage build. On a deployment where that never happened the
    "Management" filter showed no AMC change for any fund, Reliance→Nippon
    and IDFC→Bandhan included. The curated table is source-verified, so it
    is a safe floor for every fund, build or no build.
    """
    if lineage_doc:
        return lineage_doc
    import fund_lineage  # local: fund_lineage is heavy and only needed here
    curated = curated_amc_actions(
        fund_house, existed_from, fund_lineage.CURATED_AMC_CHANGES, fund_lineage.canonical_house,
    )
    return {"corporate_actions": curated} if curated else None


def all_breaks(
    *,
    lineage_doc: Optional[Dict[str, Any]] = None,
    curated_actions: Optional[List[Dict[str, Any]]] = None,
    manual_event: Optional[Dict[str, Any]] = None,
    merger_boundary: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Every distinct management break for one fund, newest first.

    Sources (the same three signals compute_continuity_floor combines, plus
    the curated table as a stand-in for an unbuilt lineage doc):
      * lineage AMC_CHANGE actions        -> source "lineage"
      * curated AMC changes (fallback)    -> source "curated"
      * manual fund_continuity_events     -> source "manual"
      * predecessor-stitch boundary       -> source "merger"

    One entry per date; when two sources name the same date the more
    descriptive one (with from/to houses) is kept. Pure — no I/O.
    """
    by_date: Dict[str, Dict[str, Any]] = {}

    def _add(d: Optional[str], entry: Dict[str, Any]) -> None:
        if not d or d == "live":
            return
        try:
            iso = date.fromisoformat(str(d)[:10]).isoformat()
        except (TypeError, ValueError):
            return
        cur = by_date.get(iso)
        if cur is None or (not (cur.get("from") and cur.get("to")) and entry.get("from") and entry.get("to")):
            by_date[iso] = {**entry, "date": iso}

    lineage_actions = [
        a for a in ((lineage_doc or {}).get("corporate_actions") or [])
        if a.get("type") == ACTION_AMC_CHANGE
    ]
    for a in lineage_actions:
        to = a.get("to")
        _add(a.get("effective_date"), {
            "reason": f"AMC change — fund house became {to}" if to else "AMC change",
            "from": a.get("from"), "to": to, "source": "lineage",
        })
    # The curated table is only a stand-in for a missing lineage doc; when the
    # lineage build HAS run it already folded these same events in.
    if not lineage_actions:
        for a in curated_actions or []:
            to = a.get("to")
            _add(a.get("effective_date"), {
                "reason": f"AMC change — fund house became {to}" if to else "AMC change",
                "from": a.get("from"), "to": to, "source": "curated",
            })
    if manual_event and manual_event.get("break_date"):
        _add(manual_event["break_date"], {
            "reason": manual_event.get("reason") or "Management change",
            "from": None, "to": None, "source": "manual",
        })
    if merger_boundary:
        _add(merger_boundary, {
            "reason": "Merger / successor scheme change",
            "from": None, "to": None, "source": "merger",
        })

    return sorted(by_date.values(), key=lambda b: b["date"], reverse=True)
