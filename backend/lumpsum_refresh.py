"""Lumpsum FQS Refresh Job — nightly batch that computes lumpsum analysis
for every fund in `fund_nav_meta` and writes results to `fund_lumpsum_analysis`.

After this job runs, `fund_lumpsum_analysis` documents contain per-horizon
metrics for all horizons 1–50Y (wherever sufficient history exists):

    {
        "scheme_code": 120503,
        "scheme_name": "Nippon India Small Cap Fund",
        "category": "Small Cap",

        # Headline (longest anchor: 15Y > 10Y > 5Y > 3Y)
        "headline_fqs": 94.7,
        "headline_horizon": 10,
        "headline_fqs_rating": "Elite",

        # Cycle Robustness
        "cycle_robustness": 81.74,
        "cycle_variant": "15Y",

        # Per-horizon array — same structure as fqs_engine output
        "horizons": [
            {
                "horizon": 5,
                "n_windows": 132,
                "fqs": 87.3,
                "fqs_rating": "Strong",
                "blended_wm": 2.14,
                "success_pct": 0.92,
                "cv": 0.18,
                ...
            },
            ...
        ],

        "computed_at": "2026-06-30T23:00:00Z"
    }

This mirrors fqs_refresh.py for the SIP model. Run after the SIP refresh
(23:00 UTC vs 22:30 UTC for SIP) so NAV data is already up-to-date.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional

import amfi_client
import batch_support
import continuity_floor
import lineage_store
from lumpsum_engine import analyze_fund_lumpsum

logger = logging.getLogger("lumpsum_refresh")

LUMPSUM_COLLECTION   = "fund_lumpsum_analysis"
NAV_META_COLLECTION  = "fund_nav_meta"
REFRESH_LOG_ID       = "lumpsum_refresh"
KEEPALIVE_INTERVAL_SECONDS = 300
BATCH_SIZE = 100  # coroutines created per asyncio.gather call — caps peak memory

# Mirrors fqs_refresh.py's SINCE_2010 — the Home screen's "Since 2010" toggle
# (server.py lumpsum_universe) reads a parallel `horizons_2010` field computed
# with start_date forced to this date, alongside the normal since-inception
# pass, off the same already-fetched NAV history (no extra network fetch).
SINCE_2010 = date(2010, 1, 1)

# Mirrors fqs_refresh.py's continuity pass — see the detailed comment there.
# Same two break-date sources (merger/successor via
# amfi_client.load_analysis_nav(include_predecessor=False), plus the manually
# curated fund_continuity_events collection), same "only compute it for funds
# that actually have a signal" cost control.


async def _self_ping_loop(base_url: str, stop_event: asyncio.Event) -> None:
    url = base_url.rstrip("/") + "/api/health"
    async with __import__("httpx").AsyncClient(timeout=15.0) as client:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=KEEPALIVE_INTERVAL_SECONDS)
                break
            except asyncio.TimeoutError:
                pass
            try:
                r = await client.get(url)
                logger.info("Lumpsum refresh keepalive -> %s (%s)", url, r.status_code)
            except Exception as exc:
                logger.warning("Lumpsum refresh keepalive failed: %s", exc)


def _horizon_to_dict(h) -> Dict[str, Any]:
    """Convert a LumpsumHorizonStats dataclass to a MongoDB-safe dict.
    Strips the per-window rows (too large for batch storage) but keeps the
    key as an empty list so the frontend can safely access h.windows.length
    without getting undefined.
    """
    d = asdict(h)
    d["windows"] = []   # keep key, clear content — frontend expects an array
    return d


async def refresh_all_lumpsum(db, concurrency: int = 4) -> Dict[str, Any]:
    """Nightly batch: compute lumpsum analysis for every fund in fund_nav_meta.

    Serialised against the other full-universe job (FQS) — see batch_support.
    The lock is taken here, around the real body, so a job that has to wait
    holds nothing but a coroutine frame while it waits.
    """
    async with batch_support.exclusive("lumpsum_refresh"):
        return await _refresh_all_lumpsum(db, concurrency)


async def _refresh_all_lumpsum(db, concurrency: int = 4) -> Dict[str, Any]:
    """The actual pass. Call refresh_all_lumpsum, not this — this one assumes
    the batch lock is already held."""
    started = datetime.now(timezone.utc)

    base_url = os.environ.get("RENDER_EXTERNAL_URL")
    stop_event = asyncio.Event()
    ping_task: Optional[asyncio.Task] = None
    if base_url:
        ping_task = asyncio.create_task(_self_ping_loop(base_url, stop_event))
        logger.info(
            "Lumpsum refresh keepalive enabled — pinging every %ds",
            KEEPALIVE_INTERVAL_SECONDS,
        )

    try:
        # All funds with at least 1Y of history (minimum for any lumpsum window).
        # Paginated rather than materialised — `[doc async for doc in cursor]`
        # held the whole collection for the entire multi-hour run. See
        # batch_support.iter_batches.
        universe_query = {"history_years": {"$gte": 1.0}}
        universe_projection = {"scheme_code": 1, "scheme_name": 1, "scheme_category": 1}
        total = await db[NAV_META_COLLECTION].count_documents(universe_query)
        logger.info("Lumpsum refresh: %d funds to process", total)

        # Manually-curated continuity breaks — small collection, load once.
        continuity_events: Dict[int, Dict[str, Any]] = {}
        async for ev in db.fund_continuity_events.find({}, {"_id": 0}):
            try:
                continuity_events[int(ev["scheme_code"])] = ev
            except Exception:
                continue

        # fund_lineage's AMC_CHANGE actions — see continuity_floor.py and the
        # matching comment in fqs_refresh.py. Generic across the whole
        # universe once scripts/build_lineage.py (POST /api/admin/build-
        # lineage) has run, not limited to individually curated funds.
        lineage_docs: Dict[int, Dict[str, Any]] = {}
        async for ldoc in db[lineage_store.LINEAGE_COLLECTION].find(
            {"corporate_actions.type": "AMC_CHANGE"},
            {"_id": 0, "scheme_code": 1, "corporate_actions": 1},
        ):
            try:
                lineage_docs[int(ldoc["scheme_code"])] = ldoc
            except Exception:
                continue

        semaphore = asyncio.Semaphore(concurrency)
        refreshed = failed = skipped = refreshed_2010 = refreshed_continuity = 0

        async def one(doc: Dict[str, Any]) -> None:
            nonlocal refreshed, failed, skipped, refreshed_2010, refreshed_continuity
            code = doc.get("scheme_code")
            name = doc.get("scheme_name", "")
            scheme_category = doc.get("scheme_category", "")
            if not code:
                skipped += 1
                return

            async with semaphore:
                try:
                    # Fetch NAV — stitch verified successor(s) for wound-up funds
                    # via the shared single-source helper (curated + discovered +
                    # correlation-gated + chained), identical to the live path.
                    loaded      = await amfi_client.load_analysis_nav(int(code))
                    nav_history = loaded["nav_history"]

                    if not nav_history:
                        skipped += 1
                        return

                    # Convert to the raw dict format expected by lumpsum_engine
                    raw_navs = [
                        {"date": r["date"].isoformat(), "nav": str(r["nav"])}
                        for r in nav_history
                    ]

                    result = await analyze_fund_lumpsum(
                        scheme_code=int(code),
                        scheme_name=name,
                        raw_navs=raw_navs,
                        scheme_category=scheme_category,
                    )

                    if result is None or not result.horizons:
                        skipped += 1
                        return

                    # Serialize horizons (strip windows to keep doc small)
                    horizons_payload = [_horizon_to_dict(h) for h in result.horizons]

                    # ── Since-2010 pass — same raw_navs already in hand, no
                    # extra network fetch. Only worth computing for funds that
                    # actually existed by then.
                    horizons_2010_payload: Optional[list] = None
                    if nav_history[0]["date"] <= SINCE_2010:
                        result_2010 = await analyze_fund_lumpsum(
                            scheme_code=int(code),
                            scheme_name=name,
                            raw_navs=raw_navs,
                            start_date=SINCE_2010,
                            scheme_category=scheme_category,
                        )
                        if result_2010 and result_2010.horizons:
                            horizons_2010_payload = [_horizon_to_dict(h) for h in result_2010.horizons]
                            refreshed_2010 += 1

                    # ── Continuity pass ("Separate Management Periods" toggle) —
                    # only bother for funds with an actual break signal. See
                    # continuity_floor.py for how the three independent
                    # signals combine.
                    predecessor_used = loaded.get("predecessor_used") or []
                    manual_event     = continuity_events.get(int(code))
                    own_nav          = loaded.get("original_nav") or []
                    lineage_doc      = continuity_floor.lineage_or_curated(
                        lineage_docs.get(int(code)),
                        (loaded.get("meta") or {}).get("fund_house"),
                        own_nav[0]["date"].isoformat() if own_nav else None,
                    )

                    horizons_continuity_payload: Optional[list] = None
                    continuity_reset_date: Optional[str] = None
                    continuity_reason: Optional[str] = None

                    if predecessor_used or manual_event or lineage_doc:
                        if predecessor_used:
                            # Own-code-only history — same fetch that powers the
                            # fund-detail screen's "new management only" toggle.
                            loaded_np = await amfi_client.load_analysis_nav(
                                int(code), include_predecessor=False
                            )
                            continuity_nav_history = loaded_np["nav_history"]
                        else:
                            continuity_nav_history = nav_history

                        own_start = continuity_nav_history[0]["date"] if continuity_nav_history else None
                        floor_date, reason = continuity_floor.compute_continuity_floor(
                            predecessor_used=predecessor_used,
                            own_code_history_start=own_start,
                            lineage_doc=lineage_doc,
                            manual_event=manual_event,
                        )

                        if continuity_nav_history:
                            continuity_raw_navs = [
                                {"date": r["date"].isoformat(), "nav": str(r["nav"])}
                                for r in continuity_nav_history
                            ]
                            result_cont = await analyze_fund_lumpsum(
                                scheme_code=int(code),
                                scheme_name=name,
                                raw_navs=continuity_raw_navs,
                                start_date=floor_date,
                                scheme_category=scheme_category,
                            )
                            if result_cont and result_cont.horizons:
                                horizons_continuity_payload = [
                                    _horizon_to_dict(h) for h in result_cont.horizons
                                ]
                                own_start = continuity_nav_history[0]["date"]
                                effective = max(own_start, floor_date) if floor_date else own_start
                                continuity_reset_date = effective.isoformat()
                                continuity_reason = reason
                                refreshed_continuity += 1

                    await db[LUMPSUM_COLLECTION].update_one(
                        {"scheme_code": int(code)},
                        {
                            "$set": {
                                "scheme_name":       name,
                                "category":          result.category,
                                "is_equity":         result.is_equity,
                                "monthly_points":    result.monthly_points,
                                "available_horizons": result.available_horizons,
                                "horizons":          horizons_payload,
                                # Same shape, computed with start_date=2010-01-01
                                # forced — powers the Home screen's "Since 2010"
                                # toggle. Null for funds that didn't exist by 2010.
                                "horizons_2010":     horizons_2010_payload,
                                # Same shape again, computed from the verified
                                # continuity break — powers the "Separate
                                # Management Periods" toggle. Null for funds with
                                # no known break.
                                "horizons_continuity": horizons_continuity_payload,
                                "continuity_reset_date": continuity_reset_date,
                                "continuity_reason":     continuity_reason,
                                "headline_fqs":      result.headline_fqs,
                                "headline_horizon":  result.headline_horizon,
                                "headline_fqs_rating": result.headline_fqs_rating,
                                "cycle_robustness":  result.cycle_robustness,
                                "cycle_variant":     result.cycle_variant,
                                "min_xirr_req":      result.min_xirr_req,
                                "cap":               result.cap,
                                "computed_at":       datetime.now(timezone.utc).isoformat(),
                            }
                        },
                        upsert=True,
                    )
                    refreshed += 1

                except Exception as exc:
                    failed += 1
                    logger.warning(
                        "Lumpsum refresh failed for %s (%s): %s", code, name, exc
                    )

        # One page at a time — each page's docs, coroutine frames and NAV
        # payloads are released before the next page is fetched, so peak
        # memory no longer scales with the size of the universe.
        async for page in batch_support.iter_batches(
            db[NAV_META_COLLECTION], universe_query, universe_projection, BATCH_SIZE,
        ):
            await asyncio.gather(*[one(d) for d in page])

        duration = (datetime.now(timezone.utc) - started).total_seconds()
        stats = {
            "total_in_index":   total,
            "refreshed":        refreshed,
            "refreshed_2010":   refreshed_2010,       # funds with a Since-2010 score too
            "refreshed_continuity": refreshed_continuity,  # funds with a continuity-break score too
            "failed":           failed,
            "skipped":          skipped,
            "duration_seconds": round(duration, 2),
            "finished_at":      datetime.now(timezone.utc).isoformat(),
        }
        await db.lumpsum_refresh_log.update_one(
            {"_id": REFRESH_LOG_ID}, {"$set": stats}, upsert=True
        )
        logger.info(
            "Lumpsum refresh finished: %d refreshed (%d since-2010, %d continuity), %d failed, %d skipped / %d total, %.1fs",
            refreshed, refreshed_2010, refreshed_continuity, failed, skipped, total, duration,
        )
        return stats

    finally:
        stop_event.set()
        if ping_task:
            try:
                await asyncio.wait_for(ping_task, timeout=5.0)
            except Exception:
                pass
