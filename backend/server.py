"""MF Analysis API — FastAPI backend.

Endpoints (all prefixed with /api):
  GET  /api/health
  GET  /api/funds/search?q=...          — AMFI master-list search (37k+ schemes)
  GET  /api/funds/{code}/analyze        — Full 1Y..20Y rolling analysis + FQS
        ?start_date=YYYY-MM-DD          — Optional NAV history filter
        &refresh=1                       — Bypass cache
  GET  /api/funds/{code}/meta           — Quick fund metadata (no heavy compute)
  GET  /api/funds/recent                — Recently analysed funds
  GET  /api/refresh/status              — Last daily-refresh run info

A nightly background task refreshes the analysis cache for every fund that's
been analysed in the past 30 days.
"""

from __future__ import annotations

import os
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, APIRouter, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

import amfi_client
from fqs_engine import analyze_fund
from category_map import classify_fund, CATEGORY_TABLE, DEFAULT_CATEGORY

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("mf_api")

mongo_url = os.environ.get("MONGO_URL") or "mongodb://localhost:27017"
db_name = os.environ.get("DB_NAME") or "mf_analyst"
if "MONGO_URL" not in os.environ:
    logger.warning("MONGO_URL not set — falling back to localhost. Set it on Render.")

mongo = AsyncIOMotorClient(mongo_url)
db = mongo[db_name]

_refresh_task: Optional[asyncio.Task] = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _refresh_task
    _refresh_task = asyncio.create_task(_refresh_scheduler())
    try:
        yield
    finally:
        if _refresh_task:
            _refresh_task.cancel()
        mongo.close()


app = FastAPI(title="MF Analysis API", lifespan=lifespan)
api = APIRouter(prefix="/api")

# Cache TTL — bypassed when start_date param is set (always computes fresh).
ANALYSIS_TTL = timedelta(hours=24)


# ─── helpers ────────────────────────────────────────────────────────────────

def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        raise HTTPException(status_code=400, detail="start_date must be YYYY-MM-DD")


async def _cached_analysis(scheme_code: int) -> Optional[Dict[str, Any]]:
    """Return cached analysis (default no-filter run) if recent enough."""
    try:
        doc = await db.fund_analysis.find_one(
            {"scheme_code": scheme_code, "start_date_applied": None},
            {"_id": 0},
        )
    except Exception:
        logger.warning("cache lookup failed for %s; falling back to fresh compute", scheme_code)
        return None
    if not doc:
        return None
    ts = doc.get("computed_at")
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            ts = None
    if not ts or (datetime.now(timezone.utc) - ts) > ANALYSIS_TTL:
        return None
    return doc


async def _save_analysis(scheme_code: int, payload: Dict[str, Any]) -> None:
    payload = {
        **payload,
        "scheme_code": scheme_code,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
    # Only persist the default (no start-date filter) run to keep DB lean.
    if not payload.get("start_date_applied"):
        try:
            await db.fund_analysis.update_one(
                {"scheme_code": scheme_code, "start_date_applied": None},
                {"$set": payload},
                upsert=True,
            )
        except Exception:
            logger.warning("cache write failed for %s; serving uncached result", scheme_code)


async def _touch_recent(scheme_code: int, scheme_name: str) -> None:
    try:
        await db.fund_recent.update_one(
            {"scheme_code": scheme_code},
            {
                "$set": {
                    "scheme_code": scheme_code,
                    "scheme_name": scheme_name,
                    "last_seen": datetime.now(timezone.utc).isoformat(),
                }
            },
            upsert=True,
        )
    except Exception:
        logger.warning("recent-funds update failed for %s", scheme_code)


async def _build_analysis(
    scheme_code: int,
    start_date: Optional[date] = None,
    include_predecessors: bool = False,
) -> Dict[str, Any]:
    if include_predecessors and scheme_code in amfi_client.PREDECESSOR_MAP:
        meta, nav_history = await amfi_client.fetch_nav_with_predecessors(scheme_code)
    else:
        payload = await amfi_client.fetch_fund_nav(scheme_code)
        meta = amfi_client.meta_from_payload(payload)
        nav_history = amfi_client.parse_nav_history(payload)

    if not meta.get("scheme_name"):
        raise HTTPException(status_code=404, detail="Scheme not found on AMFI")

    category = classify_fund(meta["scheme_name"], meta.get("scheme_category", ""))
    analysis = analyze_fund(nav_history, category, start_date=start_date)
    
    # Block Direct plans completely
    if "direct" in meta["scheme_name"].lower():
        raise HTTPException(
            status_code=400,
            detail="Direct plans are not supported. Please use the Regular plan."
        )

    result = {
        "scheme_code": int(meta["scheme_code"] or scheme_code),
        "scheme_name": meta["scheme_name"],
        "fund_house": meta.get("fund_house", ""),
        "scheme_type": meta.get("scheme_type", ""),
        "scheme_category": meta.get("scheme_category", ""),
        "predecessor_available": scheme_code in amfi_client.PREDECESSOR_MAP,
        "predecessors_included": include_predecessors,
        **analysis,
    }
    # Only cache the default run (no predecessors, no start-date filter)
    if not include_predecessors:
        await _save_analysis(scheme_code, result)
    await _touch_recent(scheme_code, meta["scheme_name"])
    return result


# ─── endpoints ──────────────────────────────────────────────────────────────

@api.get("/health")
async def health():
    """Lightweight liveness — also pings Mongo so a failed DB shows up here."""
    db_ok = True
    try:
        await db.command("ping")
    except Exception as exc:
        logger.warning("health: mongo ping failed: %s", exc)
        db_ok = False
    return {
        "status": "ok",
        "db": "ok" if db_ok else "down",
        "ts": datetime.now(timezone.utc).isoformat(),
    }


@api.get("/funds/search")
async def search_funds(q: str = Query(..., min_length=2), limit: int = 25):
    """Search the AMFI master list by scheme name or scheme code."""
    funds = await amfi_client.fetch_fund_list()
    ql = q.strip().lower()
    tokens = [t for t in ql.split() if t]
    is_numeric = ql.isdigit()
    results: List[Dict[str, Any]] = []
    for f in funds:
        name = (f.get("schemeName") or "").strip()
        code = f.get("schemeCode")
        if not name or code is None:
            continue
        if is_numeric:
            if str(code).startswith(ql):
                results.append({"scheme_code": int(code), "scheme_name": name})
        else:
            ln = name.lower()
            if all(t in ln for t in tokens):
                results.append({"scheme_code": int(code), "scheme_name": name})
        if len(results) >= limit:
            break
    return {"query": q, "results": results}


@api.get("/funds/recent")
async def recent_funds(limit: int = 20):
    cursor = db.fund_recent.find({}, {"_id": 0}).sort("last_seen", -1).limit(limit)
    rows = [r async for r in cursor]
    # Enrich with cached FQS if available
    codes = [r["scheme_code"] for r in rows]
    analyses = {}
    async for d in db.fund_analysis.find(
        {"scheme_code": {"$in": codes}, "start_date_applied": None},
        {"_id": 0, "scheme_code": 1, "category": 1, "headline_fqs": 1,
         "headline_tier": 1, "headline_horizon": 1, "headline_xirr": 1,
         "available_horizons": 1},
    ):
        analyses[d["scheme_code"]] = d
    enriched = []
    for r in rows:
        a = analyses.get(r["scheme_code"], {})
        enriched.append({**r, **a})
    return {"count": len(enriched), "results": enriched}


@api.get("/funds/{scheme_code}/meta")
async def fund_meta(scheme_code: int):
    """Cheap metadata lookup — no NAV computation."""
    payload = await amfi_client.fetch_fund_nav(scheme_code)
    meta = amfi_client.meta_from_payload(payload)
    if not meta.get("scheme_name"):
        raise HTTPException(status_code=404, detail="Scheme not found on AMFI")
    category = classify_fund(meta["scheme_name"], meta.get("scheme_category", ""))
    nav_history = amfi_client.parse_nav_history(payload)
    earliest = nav_history[0]["date"].isoformat() if nav_history else None
    latest = nav_history[-1]["date"].isoformat() if nav_history else None
    return {
        "scheme_code": int(meta["scheme_code"] or scheme_code),
        "scheme_name": meta["scheme_name"],
        "fund_house": meta.get("fund_house", ""),
        "scheme_type": meta.get("scheme_type", ""),
        "scheme_category": meta.get("scheme_category", ""),
        "category": category,
        "earliest_nav_date": earliest,
        "latest_nav_date": latest,
        "nav_points": len(nav_history),
        "predecessor_available": scheme_code in amfi_client.PREDECESSOR_MAP,
    }


@api.get("/funds/{scheme_code}/analyze")
async def analyze(
    scheme_code: int,
    refresh: bool = False,
    start_date: Optional[str] = Query(None, description="YYYY-MM-DD"),
    include_predecessors: bool = Query(False),
):
    start_d = _parse_date(start_date)
    # Use cache only for the default run (no filter, no predecessor stitching)
    if start_d is None and not refresh and not include_predecessors:
        cached = await _cached_analysis(scheme_code)
        if cached:
            cached.pop("_id", None)
            # Inject live predecessor_available so map updates reflect immediately
            cached["predecessor_available"] = scheme_code in amfi_client.PREDECESSOR_MAP
            await _touch_recent(scheme_code, cached.get("scheme_name", ""))
            return cached
    try:
        return await _build_analysis(
            scheme_code, start_date=start_d, include_predecessors=include_predecessors,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("analyze failed")
        raise HTTPException(status_code=502, detail=f"Failed to analyse: {exc}") from exc


@api.get("/categories")
async def categories():
    return {"categories": list(CATEGORY_TABLE.keys()), "default": DEFAULT_CATEGORY}


@api.get("/refresh/status")
async def refresh_status():
    doc = await db.refresh_log.find_one({"_id": "daily"}, {"_id": 0})
    return doc or {"last_run": None, "schemes_refreshed": 0, "duration_seconds": 0}


@api.post("/refresh/run")
async def refresh_run(token: Optional[str] = Query(None)):
    """Trigger the daily refresh task.

    If REFRESH_TOKEN env var is set, the caller must supply ?token=<that value>.
    External cron schedulers (GitHub Actions / cron-job.org) use this.
    """
    expected = os.environ.get("REFRESH_TOKEN")
    if expected and token != expected:
        raise HTTPException(status_code=401, detail="Invalid token")
    n, dur = await _daily_refresh()
    return {"schemes_refreshed": n, "duration_seconds": dur}


# ─── daily refresh background task ──────────────────────────────────────────

async def _daily_refresh() -> tuple[int, float]:
    """Recompute analysis for every scheme touched in the last 30 days."""
    started = datetime.now(timezone.utc)
    cutoff = (started - timedelta(days=30)).isoformat()
    cursor = db.fund_recent.find({"last_seen": {"$gte": cutoff}}, {"_id": 0, "scheme_code": 1})
    codes = [r["scheme_code"] async for r in cursor]
    logger.info("Daily refresh starting for %d schemes", len(codes))

    semaphore = asyncio.Semaphore(4)
    completed = 0

    async def one(code: int):
        nonlocal completed
        async with semaphore:
            try:
                await _build_analysis(code, start_date=None)
                completed += 1
            except Exception as exc:
                logger.warning("Refresh failed for %s: %s", code, exc)

    await asyncio.gather(*[one(c) for c in codes])
    duration = (datetime.now(timezone.utc) - started).total_seconds()
    await db.refresh_log.update_one(
        {"_id": "daily"},
        {"$set": {
            "last_run": started.isoformat(),
            "schemes_refreshed": completed,
            "total_attempted": len(codes),
            "duration_seconds": round(duration, 2),
        }},
        upsert=True,
    )
    logger.info("Daily refresh finished: %d/%d in %.1fs", completed, len(codes), duration)
    return completed, duration


async def _refresh_scheduler():
    """Sleep until next 03:30 IST (22:00 UTC previous day) and run."""
    while True:
        now = datetime.now(timezone.utc)
        # 22:00 UTC = 03:30 IST next day
        target = now.replace(hour=22, minute=0, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)
        delay = (target - now).total_seconds()
        logger.info("Next NAV refresh scheduled in %.0f minutes", delay / 60)
        try:
            await asyncio.sleep(delay)
            await _daily_refresh()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("refresh scheduler error")
            await asyncio.sleep(600)  # back off 10 minutes on unexpected error


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # required when allow_origins is wildcard
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api)
