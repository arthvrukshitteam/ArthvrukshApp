"""Background indexer: builds a lightweight {scheme_code -> NAV history span}
metadata index for the entire regular-plan fund universe.

This exists because api.mfapi.in's master list (/mf) gives no history info —
finding each fund's earliest/latest NAV date requires fetching /mf/{code}
individually. Doing that live, per user request, across thousands of schemes
isn't feasible — so we crawl once (or periodically) and cache the result here.
This index powers Home-screen eligibility checks (Step 2 of the rolling
return flow) without re-crawling mfapi.in on every request.

RESUMABILITY: any scheme_code already present in `fund_nav_meta` is skipped
on the next run. If the crawl dies partway (deploy, restart, Render free-tier
spin-down, crash), re-triggering /api/home/reindex picks up roughly where it
left off instead of re-crawling everything from scratch.

KEEPALIVE: while a crawl is running, we self-ping our own /api/health
endpoint every 5 minutes via the public URL (RENDER_EXTERNAL_URL, auto-set
by Render for web services). Render's free tier spins down after ~15 minutes
of *inbound* traffic silence — it has no visibility into background work
happening inside the process, so a long crawl with no incoming requests can
get killed mid-run otherwise. If RENDER_EXTERNAL_URL isn't set (e.g. local
dev, or a different host), keepalive is silently skipped — it does not block
or fail the crawl.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

import amfi_client
from category_map import classify_fund

logger = logging.getLogger("nav_meta_indexer")

NAV_META_COLLECTION = "fund_nav_meta"
REFRESH_LOG_ID = "nav_meta"
KEEPALIVE_INTERVAL_SECONDS = 300  # 5 min — comfortably under Render's 15-min idle window


def _safe_code(fund: Dict[str, Any]) -> Optional[int]:
    """Best-effort int scheme code, or None if missing/malformed."""
    code = fund.get("schemeCode")
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def span_fields(loaded: Dict[str, Any]) -> Dict[str, Any]:
    """The NAV-span fields of a `fund_nav_meta` document, derived from an
    `amfi_client.load_analysis_nav` result.

    Single definition shared by the crawler (new schemes) and the nightly FQS
    refresh (existing schemes), so the two can never disagree. `latest_nav_date`
    /`history_years` are the STITCHED span; `own_*` are this scheme code's own
    NAVs, which `fund_status` uses to tell a discontinued fund from a live one.
    Returns {} when there is no NAV history.
    """
    history = loaded.get("nav_history") or []
    if not history:
        return {}
    earliest, latest = history[0]["date"], history[-1]["date"]
    own = loaded.get("original_nav") or []
    return {
        "earliest_nav_date": earliest.isoformat(),
        "latest_nav_date": latest.isoformat(),
        "nav_points": len(history),
        "history_years": round((latest - earliest).days / 365.25, 2),
        "own_earliest_nav_date": own[0]["date"].isoformat() if own else None,
        "own_latest_nav_date": own[-1]["date"].isoformat() if own else None,
        "own_nav_points": len(own),
        "merged_with": loaded.get("successor_used"),
        "predecessor_used": loaded.get("predecessor_used") or [],
        "history_note": loaded.get("history_note"),
    }


async def _self_ping_loop(base_url: str, stop_event: asyncio.Event) -> None:
    """Pings our own /api/health every KEEPALIVE_INTERVAL_SECONDS until
    `stop_event` is set. Runs only for the duration of an active crawl.
    """
    url = base_url.rstrip("/") + "/api/health"
    async with httpx.AsyncClient(timeout=15.0) as client:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=KEEPALIVE_INTERVAL_SECONDS)
                break  # stop_event fired during the wait — crawl finished, exit cleanly
            except asyncio.TimeoutError:
                pass  # interval elapsed — time to ping
            try:
                r = await client.get(url)
                logger.info("Keepalive ping -> %s (%s)", url, r.status_code)
            except Exception as exc:
                logger.warning("Keepalive ping failed (will retry next interval): %s", exc)


async def build_nav_meta_index(db, concurrency: int = 8) -> Dict[str, Any]:
    """Crawl the regular-plan universe and (re)build the NAV-span index,
    skipping schemes already indexed from a prior (possibly interrupted) run.
    """
    started = datetime.now(timezone.utc)

    base_url = os.environ.get("RENDER_EXTERNAL_URL")
    stop_event = asyncio.Event()
    ping_task: Optional[asyncio.Task] = None
    if base_url:
        ping_task = asyncio.create_task(_self_ping_loop(base_url, stop_event))
        logger.info("Keepalive enabled — pinging %s/api/health every %ds during crawl",
                    base_url.rstrip("/"), KEEPALIVE_INTERVAL_SECONDS)
    else:
        logger.warning(
            "RENDER_EXTERNAL_URL not set — keepalive disabled. On Render free "
            "tier this risks the service spinning down mid-crawl. Safe to "
            "ignore in local dev."
        )

    try:
        funds = await amfi_client.fetch_fund_list()  # Direct plans already excluded
        total_universe = len(funds)

        # Skip schemes already indexed — EXCEPT those that had < 1 year of
        # history when indexed: the nightly FQS refresh only revisits funds
        # with history_years >= 1, so a young fund would otherwise stay frozen
        # below that line forever. Spans of every other indexed fund are kept
        # fresh by fqs_refresh (see span_fields).
        already_indexed = set(await db[NAV_META_COLLECTION].distinct(
            "scheme_code", {"history_years": {"$gte": 1}}
        ))
        pending = [f for f in funds if _safe_code(f) not in already_indexed]
        skipped = total_universe - len(pending)
        logger.info(
            "NAV meta index: %d total regular-plan schemes, %d already indexed "
            "(skipped — resuming), %d remaining this run",
            total_universe, skipped, len(pending),
        )

        semaphore = asyncio.Semaphore(concurrency)
        indexed = 0
        failed = 0

        async def one(fund: Dict[str, Any]) -> None:
            nonlocal indexed, failed
            code = _safe_code(fund)
            name = fund.get("schemeName") or ""
            if code is None:
                return
            # IDCW/dividend-option variants are excluded from the app entirely
            # (fetch_fund_list already drops them; kept here as belt-and-braces
            # for older cached fund lists).
            if amfi_client.is_idcw_scheme(name):
                return
            async with semaphore:
                try:
                    # Use the shared stitching helper (curated + discovered +
                    # correlation-verified predecessor/successor chains), not a
                    # raw single-code fetch. Otherwise a fund whose current code
                    # is itself the successor of an older code (AMC acquisition,
                    # true merger, code change) gets its pre-transition history
                    # silently dropped here — understating history_years, which
                    # gates Home-screen recommendation eligibility below.
                    loaded = await amfi_client.load_analysis_nav(code)
                    meta = loaded["meta"]
                    history = loaded["nav_history"]
                    if not history:
                        failed += 1
                        return

                    category = classify_fund(
                        meta.get("scheme_name") or name,
                        meta.get("scheme_category", ""),
                    )

                    doc = {
                        "scheme_code": code,
                        "scheme_name": meta.get("scheme_name") or name,
                        "fund_house": meta.get("fund_house", ""),
                        "scheme_type": meta.get("scheme_type", ""),
                        "scheme_category": meta.get("scheme_category", ""),
                        "category": category,
                        **span_fields(loaded),
                        "indexed_at": datetime.now(timezone.utc).isoformat(),
                    }
                    await db[NAV_META_COLLECTION].update_one(
                        {"scheme_code": code}, {"$set": doc}, upsert=True,
                    )
                    indexed += 1
                except Exception as exc:
                    failed += 1
                    logger.warning("NAV meta index failed for %s (%s): %s", code, name, exc)

        await asyncio.gather(*[one(f) for f in pending])

        duration = (datetime.now(timezone.utc) - started).total_seconds()
        total_indexed_now = await db[NAV_META_COLLECTION].count_documents({})
        stats = {
            "total_schemes": total_universe,
            "already_indexed_at_start": skipped,
            "attempted_this_run": len(pending),
            "indexed_this_run": indexed,
            "failed_this_run": failed,
            "total_indexed_now": total_indexed_now,
            "duration_seconds": round(duration, 2),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        await db.nav_meta_refresh_log.update_one(
            {"_id": REFRESH_LOG_ID}, {"$set": stats}, upsert=True,
        )
        logger.info(
            "NAV meta index finished: %d/%d indexed this run (%d already done, "
            "%d failed), %d total indexed now, %.1fs",
            indexed, len(pending), skipped, failed, total_indexed_now, duration,
        )
        return stats
    finally:
        stop_event.set()
        if ping_task:
            try:
                await asyncio.wait_for(ping_task, timeout=5.0)
            except Exception:
                pass