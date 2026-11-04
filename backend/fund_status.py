"""Active / dead-fund status — the single data-layer rule for "is this fund
still a selectable, current investment option?".

WHY THIS MODULE EXISTS
----------------------
`fund_nav_meta` is a snapshot of the whole regular-plan universe, wound-up
schemes included: `nav_meta_indexer` indexes every scheme the AMFI master
list returns, and mfapi.in keeps serving a merged/discontinued scheme's
frozen NAV series forever. Nothing downstream filtered on that, so funds
that no longer exist in reality were still listed on Home / Lumpsum /
search as things an advisor could put in a client's portfolio.

Historical data is NOT deleted. A dead fund keeps its `fund_nav_meta`
document, its NAV history and its precomputed scores — every one of those
is still needed for backtesting, for a proposal built earlier, and (most
importantly) for the AMC-change stitching in `amfi_client.load_analysis_nav`,
which walks BACKWARD from a live code into its predecessors' history. This
module only decides what is *offered* to the user, never what is stored.

THE RULE (generic — no fund is named anywhere in this file)
-----------------------------------------------------------
A fund is hidden from current/selectable listings when either holds:

  1. SUPERSEDED — the scheme code has a verified successor (AMC merger,
     acquisition, scheme merger, code change). The surviving fund has its
     own `fund_nav_meta` document and is the one that should be listed;
     showing both is the "duplicate / incorrectly separated fund records
     after an AMC change" problem. Recorded as `superseded_by` (stamped by
     `sync_superseded` below) and as the pre-existing `merged_with`, which
     `nav_meta_indexer` already writes whenever a forward stitch fired.

  2. STALE — the scheme code's own NAV series stopped more than
     `amfi_client.WOUND_UP_THRESHOLD_DAYS` (180) days ago and no successor
     is known. That is exactly the app's existing wound-up definition
     (`amfi_client.wound_up_info`), reused rather than re-invented, so
     "wound up" means one thing everywhere.

Rule 2 is deliberately evaluated against the fund's OWN last NAV date, not
the stitched one: a wound-up predecessor that was successfully stitched
forward into its live successor ends up with a *fresh* `latest_nav_date`
(the successor's) even though its own code is dead. `own_latest_nav_date`
is written by nav_meta_indexer from `original_nav`. Documents indexed
before that field existed simply fall back to `latest_nav_date`, which is
correct for every fund that was NOT forward-stitched, and those that were
are already caught by rule 1 via `merged_with`. So this works on today's
data with no re-crawl, and gets strictly more precise after the next one.

FAIL-OPEN
---------
Every predicate here hides a fund only on positive evidence that it is
dead. A document missing all the relevant fields is treated as ACTIVE, and
`sync_superseded` refuses to mark a code whose own NAV feed is provably
still running — so even a wrong auto-discovered merger pair cannot hide a
fund that is visibly alive. Hiding a live fund is a much worse failure than
briefly showing a dead one.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from pymongo import UpdateOne

import amfi_client

logger = logging.getLogger("fund_status")

# One definition of "wound up", shared with amfi_client.wound_up_info.
STALE_AFTER_DAYS = amfi_client.WOUND_UP_THRESHOLD_DAYS

NAV_META_COLLECTION = "fund_nav_meta"


def stale_cutoff_iso(today: Optional[datetime] = None) -> str:
    """ISO date before which a last-NAV date means "this fund is dead"."""
    now = today or datetime.now(timezone.utc)
    return (now.date() - timedelta(days=STALE_AFTER_DAYS)).isoformat()


# ─── Mongo-side predicate (used in queries — never loads the universe) ───────

def active_query(today: Optional[datetime] = None) -> Dict[str, Any]:
    """Query fragment selecting only funds that are still current.

    Combine with `merge_query` rather than by hand, so the caller's own
    filters and this one can never clobber each other's `$and`/`$or`.
    """
    cutoff = stale_cutoff_iso(today)
    not_superseded = [
        {"superseded_by": {"$in": [None, ""]}},
        {"superseded_by": {"$exists": False}},
    ]
    not_merged = [
        {"merged_with": {"$in": [None, ""]}},
        {"merged_with": {"$exists": False}},
    ]
    # Fresh on its own code; or no own-code date recorded yet (pre-backfill
    # document) and the stitched series is fresh; or neither date recorded
    # at all, in which case we have no evidence and keep the fund.
    fresh = [
        {"own_latest_nav_date": {"$gte": cutoff}},
        {"own_latest_nav_date": {"$exists": False},
         "latest_nav_date": {"$gte": cutoff}},
        {"own_latest_nav_date": {"$exists": False},
         "latest_nav_date": {"$exists": False}},
    ]
    return {"$and": [{"$or": not_superseded}, {"$or": not_merged}, {"$or": fresh}]}


def merge_query(base: Optional[Dict[str, Any]], today: Optional[datetime] = None) -> Dict[str, Any]:
    """`base` AND `active_query()`, without either side losing conditions.

    A naive ``{**base, **active_query()}`` silently drops whichever `$and`
    the caller already had; this keeps both.
    """
    active = active_query(today)
    if not base:
        return active
    merged = dict(base)
    merged["$and"] = list(merged.get("$and") or []) + list(active["$and"])
    return merged


# ─── Python-side predicate (for already-fetched documents) ──────────────────

def is_superseded(doc: Dict[str, Any]) -> bool:
    return bool(doc.get("superseded_by") or doc.get("merged_with"))


def is_stale(doc: Dict[str, Any], today: Optional[datetime] = None) -> bool:
    cutoff = stale_cutoff_iso(today)
    own = doc.get("own_latest_nav_date")
    if own:
        return str(own) < cutoff
    latest = doc.get("latest_nav_date")
    if latest:
        return str(latest) < cutoff
    return False  # no evidence — fail open


def is_active(doc: Dict[str, Any], today: Optional[datetime] = None) -> bool:
    """True when this fund should still be offered as a current option."""
    return not is_superseded(doc) and not is_stale(doc, today)


def inactive_reason(doc: Dict[str, Any], today: Optional[datetime] = None) -> Optional[str]:
    """Short human-readable reason, or None when the fund is active. Used by
    the admin status endpoint, and by search — which LABELS a dead fund
    rather than hiding it, so looking up a historical scheme still works."""
    succ = doc.get("superseded_by") or doc.get("merged_with")
    if succ:
        return f"Merged into scheme {succ}"
    if is_stale(doc, today):
        last = doc.get("own_latest_nav_date") or doc.get("latest_nav_date")
        return f"Discontinued — no NAV since {last}" if last else "Discontinued"
    return None


def filter_active(
    docs: Iterable[Dict[str, Any]], today: Optional[datetime] = None
) -> List[Dict[str, Any]]:
    """Python-side equivalent of `active_query`, for lists that did not come
    straight out of a Mongo query."""
    return [d for d in docs if is_active(d, today)]


# ─── Generic AMC-change mapping, applied across the whole universe ───────────

def successor_pairs() -> List[Tuple[int, int]]:
    """Every (predecessor_code, successor_code) the app currently knows —
    curated, auto-discovered and ISIN-verified alike.

    Deliberately read from `amfi_client`'s live runtime maps rather than
    from any table of its own: `refresh_successor_cache()` in server.py
    loads `predecessor_map`, `merger_discovery_log` and the promoted
    `fund_successor_map` into exactly those maps at startup and after every
    discovery run. So this covers the ENTIRE fund universe automatically and
    grows whenever discovery finds a new pair — nothing here has to be
    edited for an individual fund or AMC.
    """
    pairs: Dict[int, int] = {}
    pairs.update({int(k): int(v) for k, v in amfi_client.MERGER_MAP.items()})
    for pred, succ in amfi_client._DISCOVERED_SUCCESSORS.items():  # noqa: SLF001
        pairs.setdefault(int(pred), int(succ))
    for pred, succ in amfi_client._ISIN_VERIFIED_PAIRS:            # noqa: SLF001
        pairs.setdefault(int(pred), int(succ))
    return sorted(pairs.items())


def resolve_current_code(scheme_code: int, max_depth: int = 4) -> int:
    """Follow the successor chain to the fund's CURRENT identity.

    Generic replacement for "handle this one known AMC change": any code,
    however many times its scheme has been merged or transferred, resolves
    to the single surviving code. Cycle- and depth-guarded, and returns the
    input unchanged when nothing is known about it.
    """
    code = int(scheme_code)
    seen = {code}
    for _ in range(max_depth):
        succ, _curated = amfi_client.resolve_successor(code)
        if not succ or int(succ) in seen:
            break
        code = int(succ)
        seen.add(code)
    return code


async def sync_superseded(db, today: Optional[datetime] = None) -> Dict[str, Any]:
    """Stamp `superseded_by` on every fund_nav_meta document whose scheme
    code has a known successor, and clear it where a pairing has been
    withdrawn.

    This is what makes AMC-change handling generic AND cheap: no NAV fetches
    and no re-crawl, just the successor map already in memory — so it can run
    at startup and after every discovery run, and the whole universe stays
    collapsed to one record per real-world fund.

    SAFETY: a code is marked only when its own NAV feed is not provably still
    running (`own_latest_nav_date` older than the wound-up cutoff, or not
    recorded at all). A fund that is visibly still publishing NAV daily can
    never be hidden by a wrong auto-discovered pairing.

    Two round-trips total (one bulk_write, one update_many), regardless of
    how many pairs are known.
    """
    cutoff = stale_cutoff_iso(today)
    pairs = successor_pairs()
    by_pred: Dict[int, int] = {}
    for pred, _succ in pairs:
        current = resolve_current_code(pred)
        if current != pred:
            by_pred[pred] = current

    if not by_pred:
        return {"pairs": 0, "marked": 0, "cleared": 0}

    ops = [
        UpdateOne(
            {
                "scheme_code": pred,
                "superseded_by": {"$ne": succ},
                "$or": [
                    {"own_latest_nav_date": {"$lt": cutoff}},
                    {"own_latest_nav_date": {"$exists": False}},
                ],
            },
            {"$set": {"superseded_by": succ, "is_active": False}},
        )
        for pred, succ in by_pred.items()
    ]
    res = await db[NAV_META_COLLECTION].bulk_write(ops, ordered=False)
    marked = res.modified_count

    # A pairing that was withdrawn (discovery downgraded it, curated entry
    # removed) must not leave a fund permanently hidden.
    cleared_res = await db[NAV_META_COLLECTION].update_many(
        {
            "superseded_by": {"$nin": [None, ""]},
            "scheme_code": {"$nin": list(by_pred.keys())},
        },
        {"$unset": {"superseded_by": "", "is_active": ""}},
    )

    stats = {"pairs": len(by_pred), "marked": marked, "cleared": cleared_res.modified_count}
    logger.info(
        "Fund status sync: %d successor pairs, %d newly superseded, %d cleared",
        stats["pairs"], stats["marked"], stats["cleared"],
    )
    return stats


async def ensure_indexes(db) -> None:
    """Indexes the universe queries rely on.

    Without these, every Home/Lumpsum request is a full collection scan —
    the single biggest avoidable cost on a small Render instance. Index
    creation is idempotent, so this is safe to call on every boot.
    """
    await db[NAV_META_COLLECTION].create_index("scheme_code")
    await db[NAV_META_COLLECTION].create_index(
        [("history_years", 1), ("own_latest_nav_date", 1)]
    )
    await db[NAV_META_COLLECTION].create_index("latest_nav_date")
