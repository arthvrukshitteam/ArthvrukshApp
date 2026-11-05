"""FQS Refresh Job — nightly batch that computes FQS + all horizon metrics
for every fund in `fund_nav_meta` and writes results back into the same document.

After this job runs, `fund_nav_meta` documents contain everything the export
script needs — no per-fund HTTP calls required at export time:

    {
        "scheme_code": 120503,
        "scheme_name": "Nippon India Small Cap Fund",
        "category": "Small Cap",
        "history_years": 15.2,

        # Headline (longest anchor horizon: 15Y > 10Y > 5Y > 3Y)
        "headline_fqs": 94.7,
        "headline_tier": "Elite",
        "headline_horizon": 10,

        # Cross-horizon aggregates
        "cycle_robustness": 81.74,        # weighted success% (see variant)
        "cycle_robustness_variant": "15Y",# "10Y", "15Y", or null

        # Per-horizon — full metrics, not just fqs+tier
        "fqs_by_horizon": {
            "3":  {"fqs": 66.0, "tier": "Average",
                   "blended_wm": 1.235, "median_wm": 1.363, "p10_wm": 1.043,
                   "alpha_trend": 1.044, "stability_score": 83.56, "stress_score": 59.04},
            "5":  {...},
            "10": {...},
            "15": {...},
            ...
        },

        "last_fqs_refresh": "2026-06-27T22:30:00Z"
    }
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

import amfi_client
import batch_support
import continuity_floor
import lineage_store
import nav_meta_indexer
from category_map import classify_fund
from fqs_engine import analyze_fund, fqs_tier

logger = logging.getLogger("fqs_refresh")

NAV_META_COLLECTION  = "fund_nav_meta"
FQS_REFRESH_LOG_ID   = "fqs_refresh"
KEEPALIVE_INTERVAL_SECONDS = 300
BATCH_SIZE = 100  # coroutines created per asyncio.gather call — caps peak memory

# The Home screen's "Since 2010" toggle (see server.py home_universe/
# lumpsum_universe) needs every fund scored with start_date forced to this
# date instead of its own inception — computed here, alongside the normal
# since-inception pass, so it costs one extra CPU-only analyze_fund() call
# per fund rather than a second full NAV-fetch crawl of the whole universe.
SINCE_2010 = date(2010, 1, 1)

# The Home screen's "Separate Management Periods" toggle needs every fund
# with a verified continuity break scored from that break date forward
# instead of its full stitched history. Two sources feed a break date, both
# handled below in `one()`:
#
#   1. Merger/successor stitching — `amfi_client.load_analysis_nav`'s
#      `predecessor_used` list tells us whether this fund's normal
#      nav_history already has an older AMC's NAV merged in. When it does,
#      re-fetching with include_predecessor=False (the exact "new
#      management only" toggle already proven on the fund-detail screen)
#      gives the own-code-only history — this DOES cost a second network
#      fetch, but only for the small subset of funds that actually have a
#      stitched predecessor, not the whole universe.
#   2. `fund_continuity_events` — a small manually-curated collection for
#      the residual "same code, no merger, but management materially
#      changed" case with no reliable auto-detection signal. Loaded once
#      per refresh run (see refresh_all_fqs), not per fund.
#
# Funds with neither signal get no fqs_by_horizon_continuity at all — same
# "not worth computing, not a reason to hide the fund" fallback pattern as
# the Since-2010 pass.


def _build_fqs_by_horizon(horizons_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Shared by both the since-inception and Since-2010 passes — same shape,
    just fed a different analyze_fund() horizons list."""
    fqs_by_horizon: Dict[str, Any] = {}
    for h in horizons_list:
        h_key = str(h["horizon"])
        fqs_by_horizon[h_key] = {
            # Core FQS
            "fqs":             h.get("fqs"),
            "tier":            fqs_tier(h["fqs"]) if h.get("fqs") is not None else None,
            # Wealth multiples
            "blended_wm":      h.get("blended_wm"),
            "median_wm":       h.get("median_wm"),
            "p10_wm":          h.get("p10_wm"),
            # Home-screen metrics
            "success_pct":     h.get("success_pct"),
            "n_windows":       h.get("n_windows"),
            "cv":              h.get("cv"),
            # Trend / tail / per-horizon quality metrics
            "alpha_trend":     h.get("alpha_trend"),
            "stability_score": h.get("stability_score"),
            "stress_score":    h.get("stress_score"),
        }
    return fqs_by_horizon


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
                logger.info("FQS refresh keepalive ping -> %s (%s)", url, r.status_code)
            except Exception as exc:
                logger.warning("FQS refresh keepalive ping failed: %s", exc)


async def refresh_all_fqs(db, concurrency: int = 6) -> Dict[str, Any]:
    """Nightly batch: compute full metrics for every fund in fund_nav_meta
    and write them back.  include_windows=False keeps memory low.

    Serialised against the other full-universe job (lumpsum) — see
    batch_support. The lock is taken here, around the real body, so a job that
    has to wait holds nothing but a coroutine frame while it waits.
    """
    async with batch_support.exclusive("fqs_refresh"):
        return await _refresh_all_fqs(db, concurrency)


async def _refresh_all_fqs(db, concurrency: int = 6) -> Dict[str, Any]:
    """The actual pass. Call refresh_all_fqs, not this — this one assumes the
    batch lock is already held."""
    started = datetime.now(timezone.utc)

    base_url = os.environ.get("RENDER_EXTERNAL_URL")
    stop_event = asyncio.Event()
    ping_task: Optional[asyncio.Task] = None
    if base_url:
        ping_task = asyncio.create_task(_self_ping_loop(base_url, stop_event))
        logger.info("FQS refresh keepalive enabled — pinging every %ds", KEEPALIVE_INTERVAL_SECONDS)

    try:
        # Pick up newly launched schemes (and re-check funds that were < 1y
        # old when indexed) before scoring. Cheap when nothing is new — the
        # crawler skips indexed schemes. Never allowed to block scoring.
        try:
            await nav_meta_indexer.build_nav_meta_index(db)
        except Exception:
            logger.exception("NAV-meta incremental index failed — continuing with existing index")

        # Paginated, not materialised: `docs = [doc async for doc in cursor]`
        # held the whole collection (~31MB at 37k funds) for the entire
        # multi-hour run, and twice that whenever the lumpsum job overlapped.
        # batch_support.iter_batches keeps one page resident. See that module.
        universe_query = {"history_years": {"$gte": 1}}
        universe_projection = {
            "scheme_code": 1, "scheme_name": 1, "scheme_category": 1, "category": 1,
        }
        total = await db[NAV_META_COLLECTION].count_documents(universe_query)
        logger.info("FQS refresh: %d funds to process", total)

        # Manually-curated continuity breaks — small collection, load once.
        continuity_events: Dict[int, Dict[str, Any]] = {}
        async for ev in db.fund_continuity_events.find({}, {"_id": 0}):
            try:
                continuity_events[int(ev["scheme_code"])] = ev
            except Exception:
                continue

        # fund_lineage's AMC_CHANGE actions — see continuity_floor.py. This is
        # what makes the continuity break generic across the WHOLE universe
        # instead of only the handful of funds someone has individually
        # curated in fund_continuity_events: scripts/build_lineage.py (run via
        # POST /api/admin/build-lineage) derives/asserts this for every fund
        # whose fund-house was ever renamed or acquired, same-code or not.
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
            if not code:
                skipped += 1
                return

            category = doc.get("category") or classify_fund(
                name, doc.get("scheme_category", "")
            )

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

                    # include_windows=False → small payload, all scores still computed
                    analysis = await asyncio.to_thread(
                        analyze_fund, nav_history, category, include_windows=False
                    )

                    fqs     = analysis.get("headline_fqs")
                    tier    = analysis.get("headline_tier")
                    horizon = analysis.get("headline_horizon")

                    horizons_list = analysis.get("horizons") or []
                    if not horizons_list and fqs is None:
                        skipped += 1
                        return

                    fqs_by_horizon = _build_fqs_by_horizon(horizons_list)

                    # ── Since-2010 pass — same NAV history already in hand, no
                    # extra network fetch. Only worth computing for funds that
                    # actually existed by then; funds younger than 2010 simply
                    # get no fqs_by_horizon_2010 (excluded from that view by
                    # server.py's earliest_nav_date filter).
                    fqs_by_horizon_2010: Optional[Dict[str, Any]] = None
                    if nav_history[0]["date"] <= SINCE_2010:
                        analysis_2010 = await asyncio.to_thread(
                            analyze_fund, nav_history, category,
                            start_date=SINCE_2010, include_windows=False,
                        )
                        horizons_2010 = analysis_2010.get("horizons") or []
                        if horizons_2010:
                            fqs_by_horizon_2010 = _build_fqs_by_horizon(horizons_2010)
                            refreshed_2010 += 1

                    # ── Continuity pass ("Separate Management Periods" toggle) —
                    # only bother for funds with an actual break signal. See
                    # continuity_floor.py for how the three independent
                    # signals (predecessor stitch, fund_lineage AMC_CHANGE,
                    # manual fund_continuity_events) combine.
                    predecessor_used = loaded.get("predecessor_used") or []
                    manual_event     = continuity_events.get(int(code))
                    own_nav          = loaded.get("original_nav") or []
                    lineage_doc      = continuity_floor.lineage_or_curated(
                        lineage_docs.get(int(code)),
                        (loaded.get("meta") or {}).get("fund_house"),
                        own_nav[0]["date"].isoformat() if own_nav else None,
                    )

                    fqs_by_horizon_continuity: Optional[Dict[str, Any]] = None
                    continuity_reset_date: Optional[str] = None
                    continuity_reason: Optional[str] = None

                    if predecessor_used or manual_event or lineage_doc:
                        if predecessor_used:
                            # Own-code-only history — same fetch that powers the
                            # fund-detail screen's "new management only" toggle.
                            loaded_np = await amfi_client.load_analysis_nav(
                                int(code), include_predecessor=False
                            )
                            continuity_history = loaded_np["nav_history"]
                        else:
                            continuity_history = nav_history

                        own_start = continuity_history[0]["date"] if continuity_history else None
                        floor_date, reason = continuity_floor.compute_continuity_floor(
                            predecessor_used=predecessor_used,
                            own_code_history_start=own_start,
                            lineage_doc=lineage_doc,
                            manual_event=manual_event,
                        )

                        if continuity_history:
                            analysis_cont = await asyncio.to_thread(
                                analyze_fund, continuity_history, category,
                                start_date=floor_date, include_windows=False,
                            )
                            horizons_cont = analysis_cont.get("horizons") or []
                            if horizons_cont:
                                fqs_by_horizon_continuity = _build_fqs_by_horizon(horizons_cont)
                                own_start = continuity_history[0]["date"]
                                effective = max(own_start, floor_date) if floor_date else own_start
                                continuity_reset_date = effective.isoformat()
                                continuity_reason = reason
                                refreshed_continuity += 1

                    await db[NAV_META_COLLECTION].update_one(
                        {"scheme_code": code},
                        {
                            "$set": {
                                # NAV-span fields (history_years, *_nav_date, own_*).
                                # Refreshed here because the crawler skips indexed
                                # schemes: without this they freeze at first-index
                                # time, which breaks eligibility and the dead-fund rule.
                                **nav_meta_indexer.span_fields(loaded),

                                # Headline
                                "headline_fqs":     fqs,
                                "headline_tier":    tier,
                                "headline_horizon": horizon,

                                # Cross-horizon aggregates
                                "cycle_robustness":         analysis.get("cycle_robustness"),
                                "cycle_robustness_variant": analysis.get("cycle_robustness_variant"),

                                # Full per-horizon metrics
                                "fqs_by_horizon": fqs_by_horizon,
                                # Same shape, computed with start_date=2010-01-01 forced
                                # for every fund — powers the Home screen's "Since 2010"
                                # toggle. Null for funds that didn't exist yet in 2010.
                                "fqs_by_horizon_2010": fqs_by_horizon_2010,
                                # Same shape again, computed from the verified
                                # continuity break (merger and/or manually-curated
                                # management change) — powers the "Separate
                                # Management Periods" toggle. Null for funds with
                                # no known break.
                                "fqs_by_horizon_continuity": fqs_by_horizon_continuity,
                                "continuity_reset_date": continuity_reset_date,
                                "continuity_reason":     continuity_reason,

                                "last_fqs_refresh": datetime.now(timezone.utc).isoformat(),
                            }
                        },
                    )
                    refreshed += 1
                except Exception as exc:
                    failed += 1
                    logger.warning("FQS refresh failed for %s (%s): %s", code, name, exc)

        # One page at a time: each batch's docs, coroutine frames and NAV
        # payloads are all released before the next page is even fetched.
        # gather over the full 37 000-fund list at once was the original OOM
        # trigger; holding the full list at all was the rest of it.
        async for page in batch_support.iter_batches(
            db[NAV_META_COLLECTION], universe_query, universe_projection, BATCH_SIZE,
        ):
            await asyncio.gather(*[one(d) for d in page])

        duration = (datetime.now(timezone.utc) - started).total_seconds()
        stats = {
            "total_in_index":  total,
            "refreshed":       refreshed,
            "refreshed_2010":  refreshed_2010,       # funds with a Since-2010 score too
            "refreshed_continuity": refreshed_continuity,  # funds with a continuity-break score too
            "failed":          failed,
            "skipped":         skipped,
            "duration_seconds": round(duration, 2),
            "finished_at":     datetime.now(timezone.utc).isoformat(),
        }
        await db.fqs_refresh_log.update_one(
            {"_id": FQS_REFRESH_LOG_ID}, {"$set": stats}, upsert=True
        )
        logger.info(
            "FQS refresh finished: %d refreshed (%d since-2010, %d continuity), %d failed, %d skipped / %d total, %.1fs",
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