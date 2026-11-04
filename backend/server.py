"""MF Analysis API — FastAPI backend.

Endpoints (all prefixed with /api):
  GET  /api/health
  GET  /api/funds/search?q=...             — AMFI master-list search (37k+ schemes)
  GET  /api/funds/{code}/analyze           — Full 1Y..50Y rolling analysis + FQS
        ?start_date=YYYY-MM-DD            — Optional NAV history filter
        &refresh=1                         — Bypass cache
        &years=N                           — User's rolling year (drives CR variant)
  GET  /api/funds/{code}/meta              — Quick fund metadata (no heavy compute)
  GET  /api/funds/recent                   — Recently analysed funds
  GET  /api/refresh/status                 — Last daily-refresh run info
  GET  /api/refresh/full-status            — NAV + FQS batch refresh combined status
  GET  /api/export/all-funds               — Bulk export: all funds + full metrics
  GET  /api/export/status                  — Whether export data is ready

A nightly background task refreshes the FQS scores for all indexed funds.
A second nightly task refreshes the analysis cache for recently-viewed funds.
"""

from __future__ import annotations

import os
import asyncio
import logging
import math
import re
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from typing import List, Optional, Dict, Any, Set, Tuple

from fastapi import FastAPI, APIRouter, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from pydantic import BaseModel
import batch_support
import nav_meta_indexer
import fqs_refresh as fqs_refresh_mod
import lumpsum_refresh as lumpsum_refresh_mod
import inspect

import amfi_client
import fund_lineage
import fund_status
import lineage_store
import lineage_build
from fqs_engine import analyze_fund, fqs_tier
from category_map import classify_fund, CATEGORY_TABLE, DEFAULT_CATEGORY, get_display_category
import lumpsum_engine

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("mf_api")

mongo_url = os.environ.get("MONGO_URL") or "mongodb://localhost:27017"
db_name   = os.environ.get("DB_NAME")   or "mf_analyst"
if "MONGO_URL" not in os.environ:
    logger.warning("MONGO_URL not set — falling back to localhost. Set it on Render.")

mongo = AsyncIOMotorClient(mongo_url)
db    = mongo[db_name]

# _refresh_task holds the SCHEDULER coroutine, which loops forever — so
# `not _refresh_task.done()` is true for the whole life of the process, and
# /api/refresh/full-status reported "NAV refresh running" 24/7 (confirmed in
# production: running:true alongside a last_run three days old). This flag
# tracks what anyone actually wants to know — whether a pass is executing —
# and is set inside _daily_refresh so BOTH callers (the endpoint and the
# in-process scheduler) are covered.
_daily_refresh_running:  bool = False

_refresh_task:           Optional[asyncio.Task] = None
_fqs_refresh_task:       Optional[asyncio.Task] = None
_fqs_scheduler_task:     Optional[asyncio.Task] = None
_lumpsum_scheduler_task: Optional[asyncio.Task] = None
_nav_index_task:         Optional[asyncio.Task] = None


async def refresh_successor_cache() -> int:
    """Load auto-discovered successor pairs from MongoDB into the amfi_client
    runtime cache (wound_up_code → successor_code). Curated pairs always take
    precedence; every discovered pair is still correlation-verified before use.

    Sources:
      • predecessor_map (status='auto'): successor_code + predecessor_codes —
        inverted so each wound-up predecessor points at its successor.
      • merger_discovery_log 'latest': HIGH/VERY HIGH confidence candidates.
    """
    discovered: Dict[int, int] = {}
    try:
        async for doc in db.predecessor_map.find(
            {"status": "auto"},
            {"successor_code": 1, "predecessor_codes": 1, "_id": 0},
        ):
            succ = doc.get("successor_code")
            for pred in doc.get("predecessor_codes") or []:
                if pred and succ:
                    discovered[int(pred)] = int(succ)
    except Exception as exc:
        logger.warning("predecessor_map load failed: %s", exc)

    try:
        log = await db.merger_discovery_log.find_one({"_id": "latest"}, {"_id": 0})
        for cand in (log or {}).get("candidates", []):
            if cand.get("tier") in ("HIGH", "VERY HIGH"):
                wu, su = cand.get("wu_code"), cand.get("cand_code")
                if wu and su:
                    discovered.setdefault(int(wu), int(su))
    except Exception as exc:
        logger.warning("merger_discovery_log load failed: %s", exc)

    amfi_client.set_discovered_successors(discovered)

    # Pairs backed by ISIN continuity (same security under two scheme codes).
    # These have no NAV overlap, so verify_stitch must be told to apply its
    # seam-continuity test rather than correlation — see fund_lineage.
    isin_pairs = []
    try:
        async for doc in db[lineage_store.SUCCESSOR_COLLECTION].find(
            {"status": "promoted"},
            {"_id": 0, "predecessor_code": 1, "successor_code": 1},
        ):
            p_, s_ = doc.get("predecessor_code"), doc.get("successor_code")
            if p_ and s_:
                isin_pairs.append((int(p_), int(s_)))
                discovered.setdefault(int(p_), int(s_))
    except Exception as exc:
        logger.warning("promoted successor load failed: %s", exc)
    amfi_client.set_discovered_successors(discovered)
    amfi_client.set_isin_verified_pairs(isin_pairs)

    return len(discovered)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _refresh_task, _fqs_scheduler_task, _lumpsum_scheduler_task

    try:
        n = await refresh_successor_cache()
        logger.info("Loaded %d auto-discovered successor pairs at startup", n)
    except Exception:
        logger.exception("Successor cache load failed at startup (continuing)")

    # Portfolio Overlap reads fund_holdings by (scheme_code, period); create
    # its indexes here rather than as a migration someone has to remember.
    # Non-fatal: a failure here degrades overlap query speed, it must not
    # stop the API from booting.
    try:
        import holdings_store
        await holdings_store.ensure_indexes(db)
    except Exception:
        logger.exception("fund_holdings index creation failed at startup (continuing)")

    # Indexes the universe queries filter/sort on. Idempotent, and the single
    # cheapest thing that keeps a small Render instance off full collection
    # scans (which is what actually spikes RAM, not the response itself).
    try:
        await fund_status.ensure_indexes(db)
    except Exception:
        logger.exception("fund_nav_meta index creation failed at startup (continuing)")

    # Collapse every known AMC change / merger / scheme transfer down to the
    # surviving fund, universe-wide, using the successor map just loaded
    # above. No NAV fetches and no re-crawl — see fund_status.sync_superseded.
    # Runs on every boot so a pair discovered since the last deploy is applied
    # without any manual step.
    try:
        stats = await fund_status.sync_superseded(db)
        logger.info("Fund status sync at startup: %s", stats)
    except Exception:
        logger.exception("Fund status sync failed at startup (continuing)")

    # A restart kills in-process bulk-Excel jobs; don't leave them "processing".
    try:
        import bulk_analysis_routes
        n_orphans = await bulk_analysis_routes.recover_orphaned_jobs(db)
        if n_orphans:
            logger.warning("Marked %d orphaned bulk-analysis job(s) as failed", n_orphans)
    except Exception:
        logger.exception("Bulk job recovery failed at startup (continuing)")

    _refresh_task           = asyncio.create_task(_refresh_scheduler())
    _fqs_scheduler_task     = asyncio.create_task(_fqs_refresh_scheduler())
    _lumpsum_scheduler_task = asyncio.create_task(_lumpsum_refresh_scheduler())
    try:
        yield
    finally:
        for t in [_refresh_task, _fqs_scheduler_task, _lumpsum_scheduler_task]:
            if t:
                t.cancel()
        mongo.close()


app = FastAPI(title="MF Analysis API", lifespan=lifespan)
api  = APIRouter(prefix="/api")

ANALYSIS_TTL        = timedelta(hours=24)
MIN_ROLLING_YEARS   = 1
MAX_ROLLING_YEARS   = 50
HISTORY_BUFFER_YEARS = 2

# Home screen's "Since 2010" toggle — forces every fund's rolling-window
# analysis to start no earlier than 2010-01-01 instead of its own inception.
# Backed by fqs_by_horizon_2010 / horizons_2010, a parallel field the nightly
# fqs_refresh.py / lumpsum_refresh.py jobs already populate alongside the
# normal since-inception numbers (see SINCE_2010 there). Funds younger than
# 2010 simply have no such field — they aren't excluded from this view, they
# just fall back to their normal since-inception numbers (see
# _select_horizon_map / _select_horizons_list below): 2010 doesn't apply to
# a fund that didn't exist yet, it isn't a reason to hide it.
#
# The actual selection logic lives in continuity_select.py (pure functions,
# no DB) — proposal_routes.py needs the SAME logic when building a proposal
# from funds selected while a toggle was on (previously it didn't, and would
# silently go back to each fund's plain since-inception numbers — see that
# module's docstring). These three names stay as thin aliases so every
# existing call site below is unchanged.
from continuity_select import (
    continuity_applies as _continuity_applies,
    select_horizon_map as _select_horizon_map,
    select_horizons_list as _select_horizons_list,
)

BONUS_HORIZON_THRESHOLD = 5    # legacy activation threshold (no longer gates
                                # Additional Recommendations — kept for reference)

# Additional Recommendations: history from 5Y up to just below Core; judged at
# the fund's LONGEST window (5Y..max_horizon) that has ≥10 rolling windows so
# its stats are robust; XIRR ≥ 15%. max_horizon is the Core cutoff, so a fund
# with more history than the tenure is still judged at its max available window.
#
# The rule itself lives in additional_rec.py because proposal_engine.py needs
# the SAME window-picking logic when one of these funds lands in a portfolio
# whose tenure is longer than the fund's history — see that module's docstring.
from additional_rec import (
    ADDITIONAL_REC_MIN_XIRR,
    ADDITIONAL_REC_MIN_HISTORY_YEARS,
    ADDITIONAL_REC_MIN_WINDOWS,
    select_additional_rec as _select_additional_rec,
)


# ── XIRR helpers (mirrors frontend src/utils/xirr.ts) ────────────────────────

# Canonical implementations live in xirr_utils.py (single source of truth,
# extracted from "ArthVruksh MF Proposal_Priya Ravi.xlsx"). Aliased here so
# the rest of server.py keeps its historical names.
from xirr_utils import (
    lumpsum_wm_to_xirr as _lumpsum_wm_to_xirr,
    sip_wm_to_xirr as _sip_wm_to_xirr,
)


# ─── helpers ─────────────────────────────────────────────────────────────────

def _log_nav_index_task_result(task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        logger.warning("NAV meta index task was cancelled")
    except Exception:
        logger.exception("NAV meta index task crashed")


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        raise HTTPException(status_code=400, detail="start_date must be YYYY-MM-DD")


async def _cached_analysis(scheme_code: int) -> Optional[Dict[str, Any]]:
    doc = await db.fund_analysis.find_one(
        {"scheme_code": scheme_code, "start_date_applied": None},
        {"_id": 0},
    )
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
    """Persist the analysis result and mirror all metrics into fund_nav_meta.

    The fund_nav_meta mirror means the export endpoint works immediately for
    any fund that has been viewed — not just after the nightly FQS batch.
    Full per-horizon metrics are stored (not just fqs+tier) so the export
    script gets blended_wm, median_wm, p10_wm, alpha_trend, stability_score,
    stress_score without needing a second collection join.
    """
    payload = {
        **payload,
        "scheme_code": scheme_code,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }

    # Only persist the default (no start-date filter) run to keep DB lean.
    if not payload.get("start_date_applied"):
        await db.fund_analysis.update_one(
            {"scheme_code": scheme_code, "start_date_applied": None},
            {"$set": payload},
            upsert=True,
        )

        # ── Mirror full metrics into fund_nav_meta ────────────────────────────
        horizons_list = payload.get("horizons") or []
        if horizons_list:
            fqs_by_horizon: Dict[str, Any] = {}
            for h in horizons_list:
                if h.get("fqs") is None:
                    continue
                fqs_by_horizon[str(h["horizon"])] = {
                    # Core FQS
                    "fqs":             h["fqs"],
                    "tier":            fqs_tier(h["fqs"]),
                    # Wealth multiples
                    "blended_wm":      h.get("blended_wm"),
                    "median_wm":       h.get("median_wm"),
                    "p10_wm":          h.get("p10_wm"),
                    # Window stats shown on home screen
                    "success_pct":     h.get("success_pct"),
                    "n_windows":       h.get("n_windows"),
                    "cv":              h.get("cv"),
                    # New per-horizon metrics
                    "alpha_trend":     h.get("alpha_trend"),
                    "stability_score": h.get("stability_score"),
                    "stress_score":    h.get("stress_score"),
                }

            if fqs_by_horizon:
                await db.fund_nav_meta.update_one(
                    {"scheme_code": scheme_code},
                    {
                        "$set": {
                            # Per-horizon (full)
                            "fqs_by_horizon": fqs_by_horizon,
                            # Cross-horizon aggregates
                            "headline_fqs":             payload.get("headline_fqs"),
                            "headline_tier":            payload.get("headline_tier"),
                            "headline_horizon":         payload.get("headline_horizon"),
                            "cycle_robustness":         payload.get("cycle_robustness"),
                            "cycle_robustness_variant": payload.get("cycle_robustness_variant"),
                            "last_fqs_refresh":         datetime.now(timezone.utc).isoformat(),
                        }
                    },
                )


async def _touch_recent(scheme_code: int, scheme_name: str) -> None:
    await db.fund_recent.update_one(
        {"scheme_code": scheme_code},
        {
            "$set": {
                "scheme_code": scheme_code,
                "scheme_name": scheme_name,
                "last_seen":   datetime.now(timezone.utc).isoformat(),
            }
        },
        upsert=True,
    )


async def _build_analysis(
    scheme_code: int,
    start_date: Optional[date] = None,
    user_years: Optional[int] = None,
    include_predecessor: bool = True,
    allow_direct: bool = False,
) -> Dict[str, Any]:
    # Fetch NAV and stitch verified successor(s) if wound up — single source of
    # truth shared with the lumpsum builder (amfi_client.load_analysis_nav).
    # `include_predecessor=False` is the "new management only" toggle on the
    # fund-detail screen — it drops any older AMC/pre-transfer NAV history
    # from the analysis (see amfi_client.load_analysis_nav docstring).
    loaded       = await amfi_client.load_analysis_nav(scheme_code, include_predecessor=include_predecessor)
    meta         = loaded["meta"]
    nav_history  = loaded["nav_history"]

    if not meta.get("scheme_name"):
        raise HTTPException(status_code=404, detail="Scheme not found on AMFI")

    # Direct plans are out of scope for every feature that RECOMMENDS a fund
    # (the Analyze screen, Portfolio Proposal) — an MFD earns no trail
    # commission on one, so there is nothing to advise. Portfolio Review is
    # different: it scores a client's EXISTING holdings, Direct ones
    # included, so the advisor can see — and make the case for switching —
    # with the fund's own real numbers rather than a blank cell. Callers opt
    # in explicitly; the default keeps every other caller's behaviour
    # unchanged.
    if "direct" in meta["scheme_name"].lower() and not allow_direct:
        raise HTTPException(
            status_code=400,
            detail="Direct plans are not supported. Please use the Regular plan.",
        )

    successor_used = loaded["successor_used"]

    category = classify_fund(meta["scheme_name"], meta.get("scheme_category", ""))

    analysis = analyze_fund(
        nav_history,
        category,
        start_date=start_date,
        user_years=user_years,
    )

    result = {
        "scheme_code":     int(meta["scheme_code"] or scheme_code),
        "scheme_name":     meta["scheme_name"],
        "fund_house":      meta.get("fund_house", ""),
        "scheme_type":     meta.get("scheme_type", ""),
        "scheme_category": meta.get("scheme_category", ""),
        # Wound-up / merger metadata (reflects the final, stitched series)
        "wound_up":          loaded["wound_up"],
        "wound_up_date":     loaded["wound_up_date"],   # ISO date string or None
        "merged_with":       successor_used,             # successor scheme code or None
        "predecessor_used":  loaded["predecessor_used"], # older code(s) prepended, oldest first
        "predecessor_available": loaded["predecessor_available"], # every verified predecessor found (merged or not)
        "include_predecessor_applied": include_predecessor,
        "history_note":      loaded["history_note"],     # honest disclosure or None
        **analysis,
    }

    # Only persist the default (no start-date filter, predecessor toggle at
    # its default True) run — mirrors the existing start_date_applied rule in
    # _save_analysis, so a "new management only" run never overwrites the
    # cached full-history analysis or gets served back out of that cache.
    if start_date is None and include_predecessor:
        await _save_analysis(scheme_code, result)
    await _touch_recent(scheme_code, meta["scheme_name"])
    return result


# ─── NAV meta reindex ────────────────────────────────────────────────────────

@api.post("/home/reindex")
async def trigger_nav_meta_reindex(token: Optional[str] = Query(None)):
    expected = os.environ.get("REFRESH_TOKEN")
    if expected and token != expected:
        raise HTTPException(status_code=401, detail="Invalid token")

    global _nav_index_task
    if _nav_index_task and not _nav_index_task.done():
        return {"status": "already_running"}

    try:
        fn = getattr(nav_meta_indexer, "build_nav_meta_index", None)
        if fn is None:
            raise RuntimeError(
                f"nav_meta_indexer.build_nav_meta_index is missing. "
                f"Module file: {getattr(nav_meta_indexer, '__file__', None)}"
            )
        if not inspect.iscoroutinefunction(fn):
            raise RuntimeError(
                f"build_nav_meta_index is not async. "
                f"type={type(fn)} file={getattr(nav_meta_indexer, '__file__', None)}"
            )
        _nav_index_task = asyncio.create_task(fn(db))
        _nav_index_task.add_done_callback(_log_nav_index_task_result)
        return {
            "status": "started",
            "nav_meta_indexer_file": getattr(nav_meta_indexer, "__file__", None),
        }
    except Exception as exc:
        logger.exception("Failed to start NAV meta reindex")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to start NAV meta reindex: {type(exc).__name__}: {exc}",
        )


@api.get("/home/reindex/status")
async def nav_meta_reindex_status():
    doc     = await db.nav_meta_refresh_log.find_one({"_id": "nav_meta"}, {"_id": 0})
    running = bool(_nav_index_task and not _nav_index_task.done())
    return {"running": running, "last_run": doc}


# ─── Fund variant dedup ───────────────────────────────────────────────────────
#
# AMFI gives every plan/payout option of the same underlying portfolio its own
# scheme_code (e.g. "... Growth Plan - Bonus Option" vs "... Growth Plan -
# Growth Option"). Since these track the same portfolio, their NAV-derived
# stats — blended WM, FQS, success %, window count, CV — come out identical
# (or the option is a straight re-listing with no distinct NAV history), so
# they show up in the ranked list as visually different but numerically
# duplicate cards. This collapses each such group down to one representative
# entry before the list is sorted/returned.
# Round each metric to the same precision the UI actually displays it at
# (e.g. "4.35x", "91.0", "100%", "16.1%") before comparing. Two funds whose
# raw floats differ in the 4th decimal place but are indistinguishable on
# screen are, from the advisor's point of view, exact duplicates — matching
# at full float precision (the previous behaviour) missed exactly this case,
# since independently-fetched NAV series for two plan options rarely produce
# bit-identical floats even when the portfolio — and every displayed stat —
# is the same.
_METRIC_DISPLAY_DP = {
    "blended_wm":  2,  # "4.35x"
    "fqs":         1,  # "91.0"
    "success_pct": 2,  # fraction; 2dp = whole percentage points ("100%")
    "n_windows":   0,  # already an integer count
    "cv":          3,  # fraction; 3dp = one-decimal percent ("16.1%")
    "p10_wm":      2,  # "4.72x"
}


def _dedupe_fund_variants(
    funds: list[Dict[str, Any]], metric_fields: list[str],
) -> list[Dict[str, Any]]:
    """Collapse funds whose `metric_fields` values are ALL identical AT
    DISPLAY PRECISION (and not all None — funds that both simply lack data
    are never collapsed just because they share a blank signature) into one
    representative per group. The kept representative prefers a plain
    "Growth" option over any Bonus/Dividend/IDCW/Payout/Reinvestment variant,
    then the alphabetically-first name, then the lowest scheme_code — for a
    stable, predictable pick.
    """
    groups: Dict[Any, list[Dict[str, Any]]] = {}
    order: list[Any] = []
    for f in funds:
        vals = []
        any_real = False
        for key in metric_fields:
            v = f.get(key)
            if v is not None:
                any_real = True
                if isinstance(v, float):
                    v = round(v, _METRIC_DISPLAY_DP.get(key, 6))
            vals.append(v)
        # No comparable data at all → never merge; keep this fund on its own.
        group_key = id(f) if not any_real else (f.get("category"), tuple(vals))
        if group_key not in groups:
            order.append(group_key)
        groups.setdefault(group_key, []).append(f)

    # Non-"Growth"-option tokens that should always lose to a plain Growth
    # option even though "Growth Plan - Bonus Option" also contains the word
    # "growth" (as the *plan*, not the *option*) — a naive "contains growth"
    # check alone can't tell those two apart.
    _NON_GROWTH_OPTION_TOKENS = ("bonus", "dividend", "idcw", "payout", "reinvestment")

    def _rank(f: Dict[str, Any]):
        name = (f.get("scheme_name") or "")
        lname = name.lower()
        has_non_growth_option = any(tok in lname for tok in _NON_GROWTH_OPTION_TOKENS)
        is_growth = "growth" in lname
        if is_growth and not has_non_growth_option:
            tier = 0
        elif not has_non_growth_option:
            tier = 1
        else:
            tier = 2
        return (tier, name, f.get("scheme_code") or 0)

    deduped = []
    for key in order:
        group = groups[key]
        deduped.append(min(group, key=_rank) if len(group) > 1 else group[0])
    return deduped


# ─── Search-result variant dedup ─────────────────────────────────────────────
#
# _dedupe_fund_variants above collapses funds whose COMPUTED METRICS match. It
# cannot be reused here: /funds/search runs over the raw AMFI master list and
# has no metrics at all — a fund is a name and a code until someone analyses it.
#
# The duplicates search actually returns are of two kinds, measured against the
# live list (12 210 schemes after fetch_fund_list drops Direct plans and IDCW):
#
#   A. 77 names that appear under SEVERAL DISTINCT scheme codes with BYTE-
#      IDENTICAL text — "Motilal Oswal Liquid Fund" is 16 separate codes. The
#      existing `seen` set doesn't touch these (it de-dupes repeats of the SAME
#      code), so the user reads the same string sixteen times with no way to
#      tell the rows apart.
#   B. 93 schemes listed under several plan/option spellings — "Edelweiss
#      Liquid Fund" / "… - Growth Option" / "… - Regular Plan - Growth".
#
# Grouping on lineage_store.name_stem (via bulk_analysis.match_key, which adds
# FOF and stray-word normalisation) collapses both, because identical names
# trivially share a stem. Verified against the live list: 156 stems have more
# than one variant, 276 rows are collapsible, and ZERO groups merge schemes
# that aren't stem-identical — i.e. no genuinely different fund is hidden.
#
# Home's dedup also keys on `category`; that guard was measured here and
# separated nothing (11 934 groups with or without it), so it is deliberately
# left out rather than carried over as cargo cult — a name stem is a far more
# specific key than a metrics tuple.

_SEARCH_OVERSCAN = 6   # candidates collected per requested result, so a query
                       # landing on a 16-variant scheme still fills the page

# bulk_analysis.match_key deliberately KEEPS payout words in the stem: that
# matcher resolves an advisor's spreadsheet row to ONE scheme code, and a Bonus
# option is a different code with a different NAV from the Growth option, so
# merging them there would be wrong.
#
# Search wants the opposite. 306 Bonus listings are still live in the master
# list, sitting directly beside their Growth twin ("Nippon India Growth Mid Cap
# Fund - Regular Plan - Bonus Option" next to "… - Growth Option"), and Home
# already hides them — _dedupe_fund_variants ranks Growth above every
# bonus/dividend/idcw/payout/reinvestment variant. Leaving them in search is
# exactly the inconsistency this change is meant to remove, so the payout words
# are stripped on top of match_key for grouping purposes only.
#
# The negative lookahead is load-bearing: "Dividend Yield" and "Dividend
# Opportunities" are STRATEGY names, not payout options, and stripping the word
# there would dissolve 22 real funds' identities. Measured against the live
# list, this stem produces 180 additional collapses, every one of which has a
# payout-word member — i.e. no group merges two genuinely different schemes.
# An ISIN is 2 letters + 9 alphanumerics + a check digit. Search treats a query
# of this shape as an exact-identifier lookup (see search_funds).
_ISIN_QUERY_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")

_SEARCH_PAYOUT_RE = re.compile(
    r"\b(bonus|idcw|payout|reinvestment|dividend(?!\s+(yield|opportunit)))\b"
)


def _search_stem(name: str) -> str:
    """Grouping key: match_key, with payout/option words additionally removed."""
    import bulk_analysis as _ba

    return re.sub(r"\s+", " ", _SEARCH_PAYOUT_RE.sub(" ", _ba.match_key(name))).strip()


def _search_variant_rank(row: Dict[str, Any]):
    """Which variant of one scheme to keep.

    Defers to bulk_analysis._variant_rank for the tier (plain Growth beats a
    lettered legacy plan beats a payout variant) — the SAME ordering the Home
    screen and the Excel matcher use, so a fund found here is the fund the
    rest of the app would show. Imported lazily to keep server.py's startup
    imports unchanged; sys.modules caches it after the first search.

    Three keys are added on top:
      · a wound-up listing NEVER represents a scheme that still has a live
        one. This outranks even the Growth preference: a user can act on a
        live Bonus option, and cannot act on a dead Growth option. It also
        stops active_only=true from dropping a whole scheme merely because
        the representative picked here happened to be the dead sibling —
        which would make a perfectly available fund unselectable in the Add
        Fund picker. `is_active` is only trustworthy once
        _annotate_fund_status has run, hence the strict `is False` test:
        absent/None means "not known to be dead", never "dead".
      · a lineage-annotated row wins an otherwise-equal tie, so the
        "formerly known as" explanation isn't dropped in favour of an
        identical plain row;
      · scheme_code last, because kind A duplicates tie on every name-based
        key and the pick must still be stable across requests.
    """
    import bulk_analysis as _ba

    tier, name_len, name = _ba._variant_rank(row.get("scheme_name") or "")
    dead = 1 if row.get("is_active") is False else 0
    has_lineage = 0 if row.get("matched_via") == "lineage" else 1
    return (dead, tier, has_lineage, name_len, name, row.get("scheme_code") or 0)


def _dedupe_search_variants(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One row per underlying scheme, first-seen order preserved."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for r in rows:
        name = r.get("scheme_name") or ""
        stem = _search_stem(name)
        # A name that normalises to nothing has no usable grouping key; keep
        # it on its own rather than merging every such row into one bucket.
        key = stem or f"__unstemmed__{r.get('scheme_code')}"
        if key not in groups:
            order.append(key)
        groups.setdefault(key, []).append(r)

    out: List[Dict[str, Any]] = []
    for key in order:
        group = groups[key]
        best = group[0] if len(group) == 1 else min(group, key=_search_variant_rank)
        if len(group) > 1:
            # Tell the UI the row stands for several listings, so a user who
            # expects to see "Motilal Oswal Liquid Fund" many times knows the
            # others were folded in rather than lost.
            best = {**best, "variant_count": len(group)}
        out.append(best)
    return out


# ─── Unified universe contract (SIP + Lumpsum) ───────────────────────────────
# /home/universe (SIP) and /lumpsum/universe emit the SAME per-fund shape and
# the SAME envelope, so one screen component renders both modes. What differs
# between the two is only WHICH ENGINE produced the numbers (fqs_engine's
# rolling-SIP windows vs lumpsum_engine's single-investment windows) — never
# what those numbers are called.
#
# Per-fund keys:  blended_wm, fqs, fqs_tier, success_pct, n_windows, cv,
#                 p10_wm, horizon, fqs_exact
# Envelope keys:  mode, tenure, resolved_horizon, required_history_years,
#                 total_funds_indexed, eligible_fund_count, eligible_funds,
#                 bonus_funds, benchmark_xirr, index_last_built,
#                 scores_last_refreshed
UNIVERSE_STAT_KEYS = ["blended_wm", "fqs", "success_pct", "n_windows", "cv"]


# BUG FIXED (2026-08-04): commit 207bf622 renamed the SIP universe-metrics
# contract (selected_fqs -> fqs, selected_blended_wm -> blended_wm, etc.) on
# both server.py and home.tsx in the same commit, with no backward
# compatibility. Proven root cause of "Windows/P10 WM/CV blank" reports:
# reconstructed both backends locally against the SAME Mongo data (via a git
# worktree at the pre-rename commit) and confirmed the two contracts share
# ZERO overlapping field names for the per-fund metrics — any deployment
# where the served frontend bundle and backend aren't from the exact same
# commit (stale CDN/OTA cache, non-atomic rolling deploy) deterministically
# reads `undefined` for every metric while scheme_name/category (unrenamed)
# still display, matching the report exactly. The lumpsum endpoint was never
# affected — its fields were already unprefixed both before and after that
# commit. Fix: emit the legacy `selected_*` keys alongside the current ones
# so a stale SIP-mode frontend bundle still reads correct values from any
# backend running this code, closing the skew window. Purely additive.
#
# Other consumers checked (2026-08-04): no admin dashboard, no external/
# third-party integration exists anywhere in this repo or its docs — the
# only client is frontend/, but it ships as THREE targets from one source
# (web, and standalone Android/iOS via `eas build`, per DEPLOY.md). The APK
# build bakes EXPO_PUBLIC_BACKEND_URL and all frontend logic in at build
# time (ARCHITECTURE.md) and is distributed manually ("share with friends"),
# not through an auto-updating store — an installed APK has no mechanism to
# ever pick up a newer bundle. So unlike a web deploy, this is not a brief
# rolling-release race window: an old APK can stay in active use, hitting
# the always-current backend, for as long as its owner keeps the app.
#
# TODO: legacy selected_* aliases retained for backward compatibility with
# any frontend build predating commit 207bf622. Do NOT remove on a normal
# "clean up old code" pass — removing them silently reintroduces this exact
# bug for anyone still on an old APK. Only remove once there's a way to
# confirm no such build is still in active use (e.g. a server-side build/
# version check, or a deliberate decision to stop supporting pre-rename
# clients), not on a calendar-time guess.
_LEGACY_ALIAS_KEYS = {
    "fqs": "selected_fqs", "fqs_tier": "selected_tier", "blended_wm": "selected_blended_wm",
    "success_pct": "selected_success_pct", "n_windows": "selected_n_windows",
    "cv": "selected_cv", "p10_wm": "selected_p10_wm", "horizon": "selected_horizon",
    "fqs_exact": "selected_fqs_exact",
}


def _universe_metrics(stats: Optional[Dict[str, Any]], horizon: int) -> Dict[str, Any]:
    """Normalise one horizon's stats block into the unified per-fund metrics.

    `stats` is an entry of a SIP fund's `fqs_by_horizon` map or an element of a
    lumpsum fund's `horizons` list — both already carry identically-named
    fields (fqs / blended_wm / success_pct / n_windows / cv / p10_wm), so a
    single extractor serves both. Returns the all-None block when the fund has
    no usable data at this horizon.
    """
    if not stats or stats.get("fqs") is None:
        block = {
            "fqs":         None,
            "fqs_tier":    None,
            "blended_wm":  None,
            "success_pct": None,
            "n_windows":   None,
            "cv":          None,
            "p10_wm":      None,
            "horizon":     horizon,
            "fqs_exact":   False,
        }
    else:
        block = {
            "fqs":         stats["fqs"],
            # SIP's fqs_by_horizon stores the tier; lumpsum's horizon rows don't,
            # so derive it from the same fqs_tier() the SIP writer used.
            "fqs_tier":    stats.get("tier") or fqs_tier(stats["fqs"]),
            "blended_wm":  stats.get("blended_wm"),
            "success_pct": stats.get("success_pct"),
            "n_windows":   stats.get("n_windows"),
            "cv":          stats.get("cv"),
            "p10_wm":      stats.get("p10_wm"),
            "horizon":     horizon,
            "fqs_exact":   True,
        }
    block.update({legacy: block[current] for current, legacy in _LEGACY_ALIAS_KEYS.items()})
    return block


# Cursor batch size for the universe queries. Motor buffers one batch at a
# time, so this — not the collection size — is what bounds peak RSS while the
# universe streams. Small enough to stay cheap on Render's free tier, large
# enough that a few thousand funds don't cost thousands of round-trips.
_UNIVERSE_BATCH_SIZE = 200

# "Total funds indexed" is a headline number on the universe screens, but the
# universe only changes when the nightly crawl runs — so counting the whole
# collection on every request was a full scan for a figure that is the same
# all day. Cached briefly; the crawl's own progress is reported separately by
# /api/home/reindex/status, so a stale count here can never hide it.
_TOTAL_INDEXED_TTL = timedelta(minutes=5)
_total_indexed_cache: Dict[str, Any] = {"value": None, "at": None}


async def _total_funds_indexed() -> int:
    now = datetime.now(timezone.utc)
    cached = _total_indexed_cache
    if cached["value"] is not None and cached["at"] and (now - cached["at"]) < _TOTAL_INDEXED_TTL:
        return cached["value"]
    value = await db.fund_nav_meta.count_documents({})
    _total_indexed_cache.update({"value": value, "at": now})
    return value


def _universe_projection(since_2010: bool, separate_management: bool) -> Dict[str, Any]:
    """Fields the SIP universe query needs, and only those.

    The Since-2010 and Separate-Management per-horizon maps are each roughly
    the same size as the main one, and each is read ONLY when its own toggle
    is on: `_select_horizon_map` never looks at `fqs_by_horizon_2010` unless
    `since_2010`, and `_continuity_applies` is short-circuited behind
    `separate_management` at every call site. So fetching them regardless
    tripled the bytes pulled off Mongo — and the RAM they occupy — for the
    default view, which is the one virtually every request uses.

    Conditional here rather than in the pipeline on purpose: this cannot
    change WHICH numbers are selected, only whether data that would have been
    ignored is transferred at all.
    """
    projection: Dict[str, Any] = {
        "_id": 0,
        "scheme_code":      1,
        "scheme_name":      1,
        "category":         1,
        "scheme_category":  1,
        "history_years":    1,
        "headline_fqs":     1,
        "headline_tier":    1,
        "headline_horizon": 1,
        "fqs_by_horizon":   1,
        "last_fqs_refresh": 1,
    }
    if since_2010:
        projection["fqs_by_horizon_2010"] = 1
    if separate_management:
        projection["fqs_by_horizon_continuity"] = 1
        projection["continuity_reset_date"] = 1
        projection["continuity_reason"] = 1
    return projection


# ─── Home universe ───────────────────────────────────────────────────────────

@api.get("/home/universe")
async def home_universe(
    years: int = Query(..., ge=MIN_ROLLING_YEARS, le=MAX_ROLLING_YEARS),
    since_2010: bool = Query(
        False, description="Force every fund's start date to 2010-01-01 instead of its own inception."
    ),
    separate_management: bool = Query(
        False,
        description="Score funds with a verified continuity break (merger/successor "
                    "change or a manually-curated management change) from that break "
                    "date forward instead of their full stitched history.",
    ),
):
    """Home screen universe for the user-selected rolling period.

    since_2010=true doesn't change WHICH funds are eligible (still
    history_years >= required_history, each fund's own since-inception
    depth) — it changes WHICH numbers are shown for funds old enough to have
    them: fqs_by_horizon_2010 (start forced to 2010-01-01) in place of
    fqs_by_horizon, via _select_horizon_map. A fund that started after 2010
    has no forced-2010 numbers and simply keeps its normal since-inception
    ones — it's still shown, not excluded.

    separate_management=true is the same kind of fallback, one level more
    specific: funds with a verified continuity break show fqs_by_horizon_continuity
    (start forced to that break date) instead; funds without one keep
    whatever since_2010 resolved to. See _select_horizon_map /
    _continuity_applies for the exact precedence, including the both-toggles
    edge case.
    """
    # 1Y tenure is an explicit exception to the +HISTORY_BUFFER_YEARS rule —
    # a fund only needs its own 1 year of history to be judged at 1Y, not 3.
    # Every other tenure keeps the normal buffer.
    required_history = 1 if years == 1 else years + HISTORY_BUFFER_YEARS
    selected_key     = str(years)

    index_stats = await db.nav_meta_refresh_log.find_one({"_id": "nav_meta"}, {"_id": 0})
    if not index_stats:
        raise HTTPException(
            status_code=503,
            detail="Fund universe index has not been built yet. Trigger POST /api/home/reindex first.",
        )

    # Dead / merged schemes are excluded at the DATA LAYER, so this rule is
    # identical everywhere in the app and a discontinued fund is never even
    # loaded into memory here. Historical documents are untouched — see
    # fund_status.py.
    cursor = db.fund_nav_meta.find(
        fund_status.merge_query({"history_years": {"$gte": required_history}}),
        _universe_projection(since_2010, separate_management),
    ).batch_size(_UNIVERSE_BATCH_SIZE)

    # MEMORY (Render free tier): each fund's fqs_by_horizon map carries every
    # horizon it has (up to ~50) x ~11 fields, and the old code kept all of
    # them, for every eligible fund, alive in one list — then returned them in
    # the response as well, even though no client reads them. Now each document
    # is reduced to the single horizon being displayed AS IT STREAMS, so peak
    # RSS is one cursor batch of full documents plus N small records, and the
    # response payload drops by roughly the same factor. Nothing about WHICH
    # numbers are selected changes: the same _continuity_applies /
    # _select_horizon_map calls run, on the same document, in the same order —
    # only the moment the big maps are released is different.
    eligible: list[Dict[str, Any]] = []
    async for doc in cursor:
        if amfi_client.is_idcw_scheme(doc.get("scheme_name")):
            continue
        applied = separate_management and _continuity_applies(doc, since_2010)
        fqs_by_horizon = _select_horizon_map(doc, since_2010, separate_management)
        stats = fqs_by_horizon.get(selected_key)
        # Drop the per-horizon maps before this record is retained: the
        # selected horizon's numbers have already been read out of them.
        for heavy in ("fqs_by_horizon", "fqs_by_horizon_2010", "fqs_by_horizon_continuity"):
            doc.pop(heavy, None)
        broad, sub = get_display_category(doc.get("scheme_category", ""))
        doc["broad_category"] = broad
        doc["amfi_sub"]       = sub
        doc.update(_universe_metrics(stats, years))
        # Only surface the break date/reason when it's actually driving this
        # fund's numbers — the "since Apr 2018" tag on the Home screen.
        doc["continuity_reset_date"] = doc.get("continuity_reset_date") if applied else None
        doc["continuity_reason"]     = doc.get("continuity_reason") if applied else None
        eligible.append(doc)

    total_indexed = await _total_funds_indexed()

    # Collapse plan/payout-option duplicates (same underlying portfolio, same
    # computed stats — e.g. "Growth Plan - Bonus Option" vs "Growth Plan -
    # Growth Option") down to one representative card before ranking.
    eligible = _dedupe_fund_variants(eligible, UNIVERSE_STAT_KEYS)

    # Default sort: highest blended WM first (best wealth creator at top).
    # Debt funds always rank last regardless of their WM score.
    # Funds without WM data sink to the second-to-last group.
    def _sort_key(f: Dict[str, Any]):
        wm  = f.get("blended_wm")
        cat = f.get("category", "")
        return (
            2 if cat == "Debt" else (0 if wm is not None else 1),
            -(wm if wm is not None else 0),
            f.get("scheme_name", ""),
        )

    eligible.sort(key=_sort_key)

    # Additional Recommendations: candidates with history from 5Y up to just
    # below Core (< tenure + HISTORY_BUFFER_YEARS); judged at their longest
    # window with ≥10 rolling windows, XIRR ≥ 15%. Same fallback as the Core
    # list above — uses fqs_by_horizon_2010 for a candidate old enough to
    # have it, its normal fqs_by_horizon otherwise.
    bonus_funds: list[Dict[str, Any]] = []
    benchmark_xirr: Optional[float]   = ADDITIONAL_REC_MIN_XIRR

    eligible_codes = {f["scheme_code"] for f in eligible}
    bonus_cursor = db.fund_nav_meta.find(
        fund_status.merge_query(
            {"history_years": {"$gte": ADDITIONAL_REC_MIN_HISTORY_YEARS,
                               "$lt": required_history}},
        ),
        {
            "_id": 0,
            "scheme_code":    1,
            "scheme_name":    1,
            "category":       1,
            "scheme_category": 1,
            "history_years":  1,
            "fqs_by_horizon": 1,
            "fqs_by_horizon_2010": 1,
            "fqs_by_horizon_continuity": 1,
            "continuity_reset_date": 1,
            "continuity_reason": 1,
        },
    ).batch_size(_UNIVERSE_BATCH_SIZE)
    async for doc in bonus_cursor:
        if doc["scheme_code"] in eligible_codes:
            continue
        if amfi_client.is_idcw_scheme(doc.get("scheme_name")):
            continue
        applied = separate_management and _continuity_applies(doc, since_2010)
        fqs_by_h = _select_horizon_map(doc, since_2010, separate_management)
        pick = _select_additional_rec(list(fqs_by_h.items()), required_history, _sip_wm_to_xirr)
        if pick is None:
            continue
        fund_xirr, best_h, best_data = pick
        broad, sub = get_display_category(doc.get("scheme_category", ""))
        bonus_funds.append({
            "scheme_code":    doc["scheme_code"],
            "scheme_name":    doc["scheme_name"],
            "category":       doc.get("category"),
            "broad_category": broad,
            "amfi_sub":       sub,
            "history_years":  doc.get("history_years"),
            "is_bonus":       True,
            "best_horizon":   best_h,
            "blended_xirr":   fund_xirr,
            "p10_xirr":       _sip_wm_to_xirr(best_data.get("p10_wm"), best_h),
            "continuity_reset_date": doc.get("continuity_reset_date") if applied else None,
            "continuity_reason":     doc.get("continuity_reason") if applied else None,
            # Bonus funds are judged at their OWN best horizon, not the
            # user's tenure — so `horizon` here echoes best_h.
            **_universe_metrics(best_data, best_h),
        })
    bonus_funds = _dedupe_fund_variants(bonus_funds, UNIVERSE_STAT_KEYS)
    # Descending by the rolling period the fund was judged at, then
    # descending XIRR as the tiebreak — matches the frontend's own re-sort.
    bonus_funds.sort(key=lambda f: (-(f.get("best_horizon") or 0), -(f.get("blended_xirr") or 0)))

    fqs_log = await db.fqs_refresh_log.find_one({"_id": "fqs_refresh"}, {"_id": 0})
    return {
        "mode":                   "sip",
        "since_2010":             since_2010,
        "separate_management":    separate_management,
        "tenure":                 years,
        "resolved_horizon":       years,
        "required_history_years": required_history,
        "total_funds_indexed":    total_indexed,
        "eligible_fund_count":    len(eligible),
        "eligible_funds":         eligible,
        "bonus_funds":            bonus_funds,
        "benchmark_xirr":         benchmark_xirr,
        "index_last_built":       index_stats.get("finished_at"),
        "scores_last_refreshed":  fqs_log.get("finished_at") if fqs_log else None,
    }


# ─── Lumpsum Universe ─────────────────────────────────────────────────────────

@api.get("/lumpsum/universe")
async def lumpsum_universe(
    tenure: int = Query(..., ge=1, le=MAX_ROLLING_YEARS),
    since_2010: bool = Query(
        False, description="Force every fund's start date to 2010-01-01 instead of its own inception."
    ),
    separate_management: bool = Query(
        False,
        description="Score funds with a verified continuity break (merger/successor "
                    "change or a manually-curated management change) from that break "
                    "date forward instead of their full stitched history.",
    ),
):
    """
    Lumpsum results universe for a user-entered tenure.

    Uses tenure directly as the horizon (no capping — lumpsum now supports
    all rolling periods 1–50Y, identical to SIP). Funds sorted by blended WM.

    Returns the SAME shape as /home/universe (see _universe_metrics) — only
    the engine behind the numbers differs (lumpsum_engine vs fqs_engine).

    since_2010=true mirrors home_universe: eligibility is unchanged (still
    each fund's own since-inception history_years) — only which numbers are
    shown changes, via _select_horizons_list falling back to a fund's normal
    horizons when it's too young to have horizons_2010.

    separate_management=true is the same kind of fallback, one level more
    specific — see home_universe's docstring and _continuity_applies for the
    exact precedence.
    """
    horizon_key      = str(tenure)
    # Same 1Y exception as home_universe — see its comment.
    required_history = 1 if tenure == 1 else tenure + HISTORY_BUFFER_YEARS
    index_stats      = await db.nav_meta_refresh_log.find_one({"_id": "nav_meta"}, {"_id": 0})
    lumpsum_log      = await db.lumpsum_refresh_log.find_one({"_id": "lumpsum_refresh"}, {"_id": 0})

    def _envelope(eligible_funds, bonus, count, total, benchmark):
        return {
            "mode":                   "lumpsum",
            "since_2010":             since_2010,
            "separate_management":    separate_management,
            "tenure":                 tenure,
            "resolved_horizon":       tenure,
            "required_history_years": required_history,
            "total_funds_indexed":    total,
            "eligible_fund_count":    count,
            "eligible_funds":         eligible_funds,
            "bonus_funds":            bonus,
            "benchmark_xirr":         benchmark,
            "index_last_built":       index_stats.get("finished_at") if index_stats else None,
            "scores_last_refreshed":  lumpsum_log.get("finished_at") if lumpsum_log else None,
        }

    # 1. Eligible funds from nav_meta index
    # Same data-layer dead-fund rule as /home/universe — see fund_status.py.
    cursor = db.fund_nav_meta.find(
        fund_status.merge_query({"history_years": {"$gte": required_history}}),
        {
            "_id": 0,
            "scheme_code":    1,
            "scheme_name":    1,
            "category":       1,
            "scheme_category": 1,
            "history_years":  1,
        },
    ).batch_size(_UNIVERSE_BATCH_SIZE)
    eligible = [
        doc async for doc in cursor
        if not amfi_client.is_idcw_scheme(doc.get("scheme_name"))
    ]
    total_indexed = await _total_funds_indexed()

    # Enrich with two-level display category
    for fund in eligible:
        broad, sub = get_display_category(fund.get("scheme_category", ""))
        fund["broad_category"] = broad
        fund["amfi_sub"]       = sub

    if not eligible:
        # Same envelope as the populated path — the screen reads bonus_funds /
        # benchmark_xirr unconditionally, so they must always be present.
        return _envelope([], [], 0, total_indexed, ADDITIONAL_REC_MIN_XIRR)

    scheme_codes = [f["scheme_code"] for f in eligible]

    # 2. Pull lumpsum analysis for all eligible funds in one query
    lumpsum_cursor = db.fund_lumpsum_analysis.find(
        {"scheme_code": {"$in": scheme_codes}},
        {
            "_id": 0, "scheme_code": 1, "horizons": 1, "horizons_2010": 1,
            "horizons_continuity": 1, "continuity_reset_date": 1, "continuity_reason": 1,
        },
    )
    lumpsum_by_code: Dict[int, Dict[str, Any]] = {}
    continuity_by_code: Dict[int, Dict[str, Any]] = {}
    async for doc in lumpsum_cursor:
        horizon_stats = next(
            (h for h in _select_horizons_list(doc, since_2010, separate_management)
             if str(h.get("horizon")) == horizon_key),
            None,
        )
        if horizon_stats and horizon_stats.get("fqs") is not None:
            lumpsum_by_code[doc["scheme_code"]] = horizon_stats
            applied = separate_management and _continuity_applies(doc, since_2010)
            continuity_by_code[doc["scheme_code"]] = {
                "continuity_reset_date": doc.get("continuity_reset_date") if applied else None,
                "continuity_reason":     doc.get("continuity_reason") if applied else None,
            }

    # 3. Merge lumpsum data into each fund record
    for fund in eligible:
        fund.update(_universe_metrics(lumpsum_by_code.get(fund["scheme_code"]), tenure))
        cont = continuity_by_code.get(fund["scheme_code"], {})
        fund["continuity_reset_date"] = cont.get("continuity_reset_date")
        fund["continuity_reason"]     = cont.get("continuity_reason")

    # Collapse plan/payout-option duplicates (same underlying portfolio, same
    # computed stats) down to one representative card before ranking.
    eligible = _dedupe_fund_variants(eligible, UNIVERSE_STAT_KEYS)

    # 4. Sort: ranked funds first by descending blended_wm.
    # Debt funds always rank last regardless of their WM score.
    eligible.sort(key=lambda f: (
        2 if f.get("category") == "Debt" else (0 if f["blended_wm"] is not None else 1),
        -(f["blended_wm"] or 0),
    ))

    # Additional Recommendations (same rule as SIP): history from 5Y up to just
    # below Core (< tenure + HISTORY_BUFFER_YEARS); judged at their longest
    # window with ≥10 rolling windows, XIRR ≥ 15%. Same fallback as the Core
    # list above for the Since-2010 view.
    bonus_funds: list[Dict[str, Any]] = []
    benchmark_xirr: Optional[float]   = ADDITIONAL_REC_MIN_XIRR

    eligible_codes = {f["scheme_code"] for f in eligible}
    bonus_meta_cursor = db.fund_nav_meta.find(
        fund_status.merge_query(
            {"history_years": {"$gte": ADDITIONAL_REC_MIN_HISTORY_YEARS,
                               "$lt": required_history}},
        ),
        {
            "_id": 0,
            "scheme_code":    1,
            "scheme_name":    1,
            "category":       1,
            "scheme_category": 1,
            "history_years":  1,
        },
    ).batch_size(_UNIVERSE_BATCH_SIZE)
    candidate_meta: Dict[int, Dict[str, Any]] = {}
    async for doc in bonus_meta_cursor:
        if (doc["scheme_code"] not in eligible_codes
                and not amfi_client.is_idcw_scheme(doc.get("scheme_name"))):
            candidate_meta[doc["scheme_code"]] = doc

    if candidate_meta:
        cand_cursor = db.fund_lumpsum_analysis.find(
            {"scheme_code": {"$in": list(candidate_meta.keys())}},
            {
                "_id": 0, "scheme_code": 1, "horizons": 1, "horizons_2010": 1,
                "horizons_continuity": 1, "continuity_reset_date": 1, "continuity_reason": 1,
            },
        )
        async for doc in cand_cursor:
            meta = candidate_meta.get(doc["scheme_code"])
            if not meta:
                continue
            applied  = separate_management and _continuity_applies(doc, since_2010)
            horizons = _select_horizons_list(doc, since_2010, separate_management)
            pick = _select_additional_rec(
                [(h.get("horizon"), h) for h in horizons], required_history, _lumpsum_wm_to_xirr,
            )
            if pick is None:
                continue
            fund_xirr, best_h, best_stats = pick
            broad, sub = get_display_category(meta.get("scheme_category", ""))
            bonus_funds.append({
                "scheme_code":    meta["scheme_code"],
                "scheme_name":    meta["scheme_name"],
                "category":       meta.get("category"),
                "broad_category": broad,
                "amfi_sub":       sub,
                "history_years":  meta.get("history_years"),
                "continuity_reset_date": doc.get("continuity_reset_date") if applied else None,
                "continuity_reason":     doc.get("continuity_reason") if applied else None,
                "is_bonus":       True,
                "best_horizon":   best_h,
                "blended_xirr":   fund_xirr,
                "p10_xirr":       _lumpsum_wm_to_xirr(best_stats.get("p10_wm"), best_h),
                # Judged at the fund's OWN best horizon, not the user's tenure.
                **_universe_metrics(best_stats, best_h),
            })
    bonus_funds = _dedupe_fund_variants(bonus_funds, UNIVERSE_STAT_KEYS)
    # Descending by the rolling period the fund was judged at, then
    # descending XIRR as the tiebreak.
    bonus_funds.sort(key=lambda f: (-(f.get("best_horizon") or 0), -(f.get("blended_xirr") or 0)))

    return _envelope(eligible, bonus_funds, len(eligible), total_indexed, benchmark_xirr)


# ─── Health ──────────────────────────────────────────────────────────────────

@api.get("/health")
async def health():
    db_ok = True
    try:
        await db.command("ping")
    except Exception as exc:
        logger.warning("health: mongo ping failed: %s", exc)
        db_ok = False
    return {
        "status": "ok",
        "db":     "ok" if db_ok else "down",
        "ts":     datetime.now(timezone.utc).isoformat(),
    }


# ─── Fund search ─────────────────────────────────────────────────────────────

async def _annotate_fund_status(
    rows: List[Dict[str, Any]], active_only: bool,
) -> List[Dict[str, Any]]:
    """Stamp `is_active` / `inactive_reason` onto search rows, and drop the
    dead ones when the caller asked for selectable funds only.

    Search is BOTH a lookup surface ("what happened to this old fund?") and a
    selection surface (the Add Fund picker). Hiding dead funds unconditionally
    would break the first; showing them unlabelled breaks the second. So the
    default keeps every result and merely labels it, and callers that are
    picking a fund to invest in pass active_only=true.

    One indexed query over just the codes on this page of results, projecting
    only the three status fields — never the fund universe.
    """
    if not rows:
        return rows
    codes = [r["scheme_code"] for r in rows]
    status: Dict[int, Dict[str, Any]] = {}
    try:
        async for doc in db.fund_nav_meta.find(
            {"scheme_code": {"$in": codes}},
            {"_id": 0, "scheme_code": 1, "superseded_by": 1, "merged_with": 1,
             "own_latest_nav_date": 1, "latest_nav_date": 1},
        ):
            status[doc["scheme_code"]] = doc
    except Exception as exc:
        # The status index is an enhancement, never a dependency — a search
        # that cannot reach it still returns results, exactly as before.
        logger.warning("fund status lookup failed for %d search results: %s", len(codes), exc)
        return rows

    out: List[Dict[str, Any]] = []
    for row in rows:
        doc = status.get(row["scheme_code"])
        if doc is None:
            # Not indexed yet (brand-new scheme) — no evidence it is dead.
            row.setdefault("is_active", True)
            row.setdefault("inactive_reason", None)
            out.append(row)
            continue
        active = fund_status.is_active(doc)
        if active_only and not active:
            continue
        row["is_active"] = active
        row["inactive_reason"] = fund_status.inactive_reason(doc)
        # A merged fund's surviving code, so the client can offer it instead.
        row["current_scheme_code"] = doc.get("superseded_by") or doc.get("merged_with")
        out.append(row)
    return out


@api.get("/funds/search")
async def search_funds(
    q: str = Query(..., min_length=2),
    limit: int = Query(
        25, ge=1, le=100,
        description="Page size. Bounded because the variant-collapse overscan "
                    "collects limit * _SEARCH_OVERSCAN candidates and status-"
                    "annotates them in one $in — an unbounded limit let a "
                    "single request pull and annotate the whole 12k master "
                    "list on a 512MB instance.",
    ),
    active_only: bool = Query(
        False,
        description="Return only funds that are still current investment "
                    "options (drops wound-up / merged schemes). Used by the "
                    "Add Fund picker; lookup surfaces leave it off so a "
                    "historical scheme can still be found.",
    ),
    dedupe_variants: bool = Query(
        True,
        description="Collapse plan/option listings of the same underlying "
                    "scheme into one row (see _dedupe_search_variants). On by "
                    "default — the raw AMFI list repeats a single scheme under "
                    "as many as 16 codes with identical names. Pass false for "
                    "an unfiltered view of every listing.",
    ),
):
    """Search the AMFI universe by current name, HISTORICAL name, code or ISIN.

    Two layers, lineage first:
      1. `lineage_store.resolve` — an indexed lookup over every identifier a
         fund has EVER had (old names, old scheme codes, current and historical
         ISINs). This is what makes "Reliance Small Cap" find the fund now
         called "Nippon India Small Cap", and what makes an ISIN searchable at
         all — mfapi.in carries no ISIN.
      2. The original substring scan over current names, unchanged, appended
         after any lineage hits and de-duplicated.

    Layer 2 is kept as a fallback rather than replaced: the lineage index only
    covers what the AMFI/archive crawl has seen, so a brand-new scheme could be
    present in mfapi's list before it is in the lineage table. Falling back
    means search can only gain results from this change, never lose them.
    """
    funds    = await amfi_client.fetch_fund_list()
    ql       = q.strip().lower()
    tokens   = [t for t in ql.split() if t]
    is_numeric = ql.isdigit()
    results: List[Dict[str, Any]] = []
    seen: Set[int] = set()

    # A query that IS an identifier — a scheme code, or an ISIN — asks for one
    # specific listing. Collapsing there would answer with a sibling variant's
    # code, i.e. hand back a different scheme from the one that was looked up,
    # so exact-identifier lookups are always returned uncollapsed.
    is_identifier = is_numeric or bool(_ISIN_QUERY_RE.match(ql.upper()))
    collapse = dedupe_variants and not is_identifier
    # Dedup runs AFTER collection, so collection has to over-fetch — stopping
    # at `limit` raw rows and then collapsing them would hand back 1 result for
    # a query that matched a 16-listing scheme.
    budget = limit * _SEARCH_OVERSCAN if collapse else limit

    async def _finish(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Status-annotate, then collapse, then trim to the page size.

        The order matters. _annotate_fund_status is what stamps `is_active`
        and, under active_only, drops the dead rows — so running it FIRST is
        what lets _search_variant_rank prefer a live listing, and what stops a
        scheme disappearing entirely because the variant chosen to represent
        it happened to be the wound-up one.

        Trimming last also means active_only now returns a full page instead
        of however many survived the filter after an early cut to `limit`.

        Cost: one indexed $in over the candidate codes (up to limit *
        _SEARCH_OVERSCAN) instead of `limit`, projecting five fields.
        """
        annotated = await _annotate_fund_status(rows, active_only)
        return (_dedupe_search_variants(annotated) if collapse else annotated)[:limit]

    # ── Layer 1: lineage resolution (historical identifiers) ────────────────
    by_code = {}
    for f in funds:
        c = f.get("schemeCode")
        if c is not None:
            try:
                by_code[int(c)] = (f.get("schemeName") or "").strip()
            except (TypeError, ValueError):
                continue
    try:
        codes = await lineage_store.resolve(db, q, limit=limit)
        lineage_docs = {}
        if codes:
            async for doc in db[lineage_store.LINEAGE_COLLECTION].find(
                {"scheme_code": {"$in": codes}}, {"_id": 0}
            ):
                lineage_docs[doc["scheme_code"]] = doc

        for code in codes:
            doc = lineage_docs.get(code)
            # Prefer the live AMFI name; fall back to lineage for a code that
            # is no longer in mfapi's list but is still reachable by history.
            name = by_code.get(code) or (doc or {}).get("current_name")
            if not name or code in seen:
                continue
            # An IDCW/dividend variant is excluded everywhere else in the app;
            # resolving one here would reintroduce it through the back door.
            if amfi_client.is_idcw_scheme(name):
                continue
            seen.add(code)

            # Tell the UI WHY this matched, so a user who typed an old name
            # sees the current fund with that old name acknowledged rather than
            # an apparently unrelated result.
            row: Dict[str, Any] = {"scheme_code": code, "scheme_name": name,
                                   "matched_via": "lineage"}
            if doc:
                exp = lineage_store.explain_record(q, doc)
                matched_old_name = next(
                    (n for n in exp["identity_history"]["names"]
                     if n != doc.get("current_name")
                     and (lineage_store.normalise_name(n) == lineage_store.normalise_name(q)
                          or lineage_store.name_stem(n) == lineage_store.name_stem(q))),
                    None)
                row.update({
                    "resolved_from": matched_old_name,
                    "formerly_known_as": exp["formerly_known_as"],
                    "lineage_confidence": exp["confidence"],
                    "lineage_reason": exp["reasons"][0] if exp["reasons"] else None,
                    "is_active": doc.get("active"),
                })
            results.append(row)
            if len(results) >= budget:
                return {"query": q, "results": await _finish(results)}
    except Exception as exc:
        # The lineage index is an enhancement, never a dependency. If it is
        # missing (fresh deploy, crawl not yet run) search must still work.
        logger.warning("lineage resolve failed for %r, falling back: %s", q, exc)

    # ── Layer 2: original current-name scan ─────────────────────────────────
    for f in funds:
        name = (f.get("schemeName") or "").strip()
        code = f.get("schemeCode")
        if not name or code is None:
            continue
        if int(code) in seen:
            continue
        matched = (str(code).startswith(ql) if is_numeric
                   else all(t in name.lower() for t in tokens))
        if matched:
            # mfapi's master list contains repeated entries for the same
            # schemeCode, so without recording what we emit the same fund is
            # returned twice — and the client keys its list by scheme_code,
            # which makes duplicates a React duplicate-key warning as well as
            # a visibly repeated row.
            seen.add(int(code))
            results.append({"scheme_code": int(code), "scheme_name": name})
        if len(results) >= budget:
            break
    return {"query": q, "results": await _finish(results)}


# ─── Recent funds ────────────────────────────────────────────────────────────

@api.get("/funds/recent")
async def recent_funds(limit: int = 20):
    cursor = db.fund_recent.find({}, {"_id": 0}).sort("last_seen", -1).limit(limit)
    rows   = [
        r async for r in cursor
        if not amfi_client.is_idcw_scheme(r.get("scheme_name"))
    ]
    codes  = [r["scheme_code"] for r in rows]
    analyses: Dict[int, Dict] = {}
    async for d in db.fund_analysis.find(
        {"scheme_code": {"$in": codes}, "start_date_applied": None},
        {
            "_id": 0, "scheme_code": 1, "category": 1,
            "headline_fqs": 1, "headline_tier": 1, "headline_horizon": 1,
            "headline_xirr": 1, "available_horizons": 1,
        },
    ):
        analyses[d["scheme_code"]] = d
    enriched = [{**r, **analyses.get(r["scheme_code"], {})} for r in rows]
    return {"count": len(enriched), "results": enriched}


# ─── Fund meta & analysis ────────────────────────────────────────────────────

@api.get("/funds/{scheme_code}/meta")
async def fund_meta(scheme_code: int):
    payload     = await amfi_client.fetch_fund_nav(scheme_code)
    meta        = amfi_client.meta_from_payload(payload)
    if not meta.get("scheme_name"):
        raise HTTPException(status_code=404, detail="Scheme not found on AMFI")
    category    = classify_fund(meta["scheme_name"], meta.get("scheme_category", ""))
    nav_history = amfi_client.parse_nav_history(payload)
    return {
        "scheme_code":       int(meta["scheme_code"] or scheme_code),
        "scheme_name":       meta["scheme_name"],
        "fund_house":        meta.get("fund_house", ""),
        "scheme_type":       meta.get("scheme_type", ""),
        "scheme_category":   meta.get("scheme_category", ""),
        "category":          category,
        "earliest_nav_date": nav_history[0]["date"].isoformat() if nav_history else None,
        "latest_nav_date":   nav_history[-1]["date"].isoformat() if nav_history else None,
        "nav_points":        len(nav_history),
    }


@api.get("/funds/{scheme_code}/history-debug")
async def fund_history_debug(scheme_code: int):
    """Inspect exactly how a fund's analysis history was assembled.

    Distinguishes the concepts that "earliest NAV date" conflates by default:
      - earliest_nav_date_own_code: this scheme code's own first NAV record
        (what a naive/buggy implementation would mistake for "inception").
      - earliest_nav_date_effective: first date in the final series actually
        used for analysis, after predecessor stitching (if any).
      - predecessor_chain / successor_chain: which older/newer scheme codes
        were verified (NAV return-correlation) and stitched in, oldest first.
      - stitch_signals: the correlation/gap evidence for every stitch attempt,
        accepted or rejected, so a rejected pair can be audited.
      - history_note: honest disclosure when even the fully-stitched series
        starts later than the fund's known true inception (data source gap
        that cannot be backfilled by stitching — e.g. HDFC Small Cap Fund's
        pre-2014 Morgan Stanley A.C.E. Fund history is not on the feed at all).
    """
    loaded = await amfi_client.load_analysis_nav(scheme_code)
    meta = loaded["meta"]
    if not meta.get("scheme_name"):
        raise HTTPException(status_code=404, detail="Scheme not found on AMFI")

    own_nav = loaded["original_nav"]
    final_nav = loaded["nav_history"]

    return {
        "scheme_code":      int(meta["scheme_code"] or scheme_code),
        "scheme_name":      meta["scheme_name"],
        "fund_house":       meta.get("fund_house", ""),
        # This scheme code's OWN first/last NAV — NOT necessarily inception.
        "earliest_nav_date_own_code": own_nav[0]["date"].isoformat() if own_nav else None,
        "latest_nav_date_own_code":   own_nav[-1]["date"].isoformat() if own_nav else None,
        "nav_points_own_code":        len(own_nav),
        # Effective analysis series after predecessor/successor stitching.
        "earliest_nav_date_effective": final_nav[0]["date"].isoformat() if final_nav else None,
        "latest_nav_date_effective":   final_nav[-1]["date"].isoformat() if final_nav else None,
        "nav_points_effective":        len(final_nav),
        "history_extended": len(final_nav) != len(own_nav),
        "predecessor_chain": loaded["predecessor_used"],   # older codes prepended, oldest first
        "successor_chain":   loaded["stitch_chain"],        # newer codes appended (forward stitch)
        "wound_up":          loaded["wound_up"],
        "wound_up_date":     loaded["wound_up_date"],
        "stitch_signals":    loaded["stitch_signals"],       # evidence for every accept/reject
        "history_note":      loaded["history_note"],         # disclosed gap, or None
    }


@api.get("/funds/{scheme_code}/analyze")
async def analyze(
    scheme_code: int,
    refresh: bool = False,
    start_date: Optional[str] = Query(None, description="YYYY-MM-DD"),
    years: Optional[int] = Query(
        None, ge=MIN_ROLLING_YEARS, le=MAX_ROLLING_YEARS,
        description="User's selected rolling period — controls Cycle Robustness variant",
    ),
    include_predecessor: bool = Query(
        True,
        description=(
            "False = analyse this scheme code's own NAV history only, excluding any "
            "older AMC/pre-transfer predecessor history that would otherwise be "
            "stitched in. Use when the fund changed hands and you want 'new "
            "management only' figures instead of the full continuous history."
        ),
    ),
):
    start_d = _parse_date(start_date)

    # The cache only ever holds the default run (no start_date filter, full
    # predecessor history included) — same rule _save_analysis enforces when
    # writing it, so a "new management only" request never reads or writes it.
    if start_d is None and not refresh and include_predecessor:
        cached = await _cached_analysis(scheme_code)
        if cached:
            cached.pop("_id", None)
            await _touch_recent(scheme_code, cached.get("scheme_name", ""))
            return cached

    try:
        return await _build_analysis(
            scheme_code,
            start_date=start_d,
            user_years=years,
            include_predecessor=include_predecessor,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("analyze failed")
        raise HTTPException(status_code=502, detail=f"Failed to analyse: {exc}") from exc


# ─── Lumpsum analysis ────────────────────────────────────────────────────────

LUMPSUM_TTL = timedelta(hours=24)


async def _cached_lumpsum(scheme_code: int) -> Optional[Dict[str, Any]]:
    doc = await db.fund_lumpsum_analysis.find_one(
        {"scheme_code": scheme_code, "start_date_applied": None},
        {"_id": 0},
    )
    if not doc:
        return None
    ts = doc.get("computed_at")
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            ts = None
    if not ts or (datetime.now(timezone.utc) - ts) > LUMPSUM_TTL:
        return None
    return doc


def _lumpsum_to_dict(result: lumpsum_engine.LumpsumAnalysis) -> Dict[str, Any]:
    """Serialise a LumpsumAnalysis dataclass to a plain dict for JSON/MongoDB."""
    horizons = []
    for h in result.horizons:
        windows = [
            {
                "start_date":       w.start_date,
                "end_date":         w.end_date,
                "starting_nav":     w.starting_nav,
                "ending_nav":       w.ending_nav,
                "total_invested":   w.total_invested,
                "final_value":      w.final_value,
                "wealth_multiple":  w.wealth_multiple,
                "good":             w.good,
            }
            for w in h.windows
        ]
        horizons.append({
            "horizon":            h.horizon,
            "n_windows":          h.n_windows,
            "min_wm_required":    h.min_wm_required,
            "cap":                h.cap,
            "min_wm":             h.min_wm,
            "max_wm":             h.max_wm,
            "mean_wm":            h.mean_wm,
            "median_wm":          h.median_wm,
            "p10_wm":             h.p10_wm,
            "p25_wm":             h.p25_wm,
            "p75_wm":             h.p75_wm,
            "p90_wm":             h.p90_wm,
            "std_dev":            h.std_dev,
            "cv":                 h.cv,
            "failure_pct":        h.failure_pct,
            "success_pct":        h.success_pct,
            "failure_score":      h.failure_score,
            "consistency_score":  h.consistency_score,
            "downside_sev_score": h.downside_sev_score,
            "cv_score":           h.cv_score,
            "risk_score":         h.risk_score,
            "adj_risk_score":     h.adj_risk_score,
            "blended_wm":         h.blended_wm,
            "blended_wm_score":   h.blended_wm_score,
            "downside_score":     h.downside_score,
            "es10":               h.es10,
            "es_score":           h.es_score,
            "fqs":                h.fqs,
            "fqs_rating":         h.fqs_rating,
            "alpha_trend":        h.alpha_trend,
            "stress_score":       h.stress_score,
            "windows":            windows,
        })

    return {
        "scheme_code":        result.scheme_code,
        "scheme_name":        result.scheme_name,
        "category":           result.category,
        "is_equity":          result.is_equity,
        "monthly_points":     result.monthly_points,
        "start_date_applied": result.start_date_applied,
        "available_horizons": result.available_horizons,
        "horizons":           horizons,
        "headline_fqs":       result.headline_fqs,
        "headline_horizon":   result.headline_horizon,
        "headline_fqs_rating": result.headline_fqs_rating,
        "cycle_robustness":   result.cycle_robustness,
        "cycle_variant":      result.cycle_variant,
        "min_xirr_req":       result.min_xirr_req,
        "cap":                result.cap,
    }


async def _build_lumpsum_analysis(
    scheme_code: int,
    start_date: Optional[date] = None,
    user_years: Optional[int] = None,
    include_predecessor: bool = True,
) -> Dict[str, Any]:
    """Full lumpsum analysis for one fund — NAV fetch, wound-up/merger
    stitching, scoring, cache write. Extracted out of the `analyze_lumpsum`
    endpoint so `proposal_routes.py` can reuse the exact same logic instead
    of re-implementing NAV fetch / merger-stitch a second time.

    `include_predecessor=False` mirrors `_build_analysis`'s toggle — see
    amfi_client.load_analysis_nav's docstring.
    """
    # Fetch NAV + verified successor stitching via the shared single-source
    # helper (identical behaviour to _build_analysis).
    loaded       = await amfi_client.load_analysis_nav(scheme_code, include_predecessor=include_predecessor)
    meta         = loaded["meta"]
    nav_history  = loaded["nav_history"]

    if not meta.get("scheme_name"):
        raise HTTPException(status_code=404, detail="Scheme not found on AMFI")

    if "direct" in meta["scheme_name"].lower():
        raise HTTPException(
            status_code=400,
            detail="Direct plans are not supported. Please use the Regular plan.",
        )

    wound_up_date  = loaded["wound_up_date"]
    successor_used = loaded["successor_used"]

    try:
        result = await lumpsum_engine.analyze_fund_lumpsum(
            scheme_code=scheme_code,
            scheme_name=meta["scheme_name"],
            raw_navs=[
                {"date": r["date"].isoformat(), "nav": str(r["nav"])}
                for r in nav_history
            ],
            start_date=start_date,
            user_years=user_years,
            scheme_category=meta.get("scheme_category", ""),
        )
    except Exception as exc:
        logger.exception("lumpsum analysis failed for %s", scheme_code)
        raise HTTPException(status_code=502, detail=f"Lumpsum analysis failed: {exc}") from exc

    if result is None:
        raise HTTPException(
            status_code=422,
            detail="Insufficient NAV history for lumpsum analysis (need ≥36 months).",
        )

    payload_dict = _lumpsum_to_dict(result)
    payload_dict["computed_at"]  = datetime.now(timezone.utc).isoformat()
    payload_dict["wound_up"]     = loaded["wound_up"]
    payload_dict["wound_up_date"] = wound_up_date
    payload_dict["merged_with"]  = successor_used
    payload_dict["predecessor_used"] = loaded["predecessor_used"]
    payload_dict["predecessor_available"] = loaded["predecessor_available"]
    payload_dict["include_predecessor_applied"] = include_predecessor
    payload_dict["history_note"] = loaded["history_note"]

    if start_date is None and include_predecessor:
        await db.fund_lumpsum_analysis.update_one(
            {"scheme_code": scheme_code, "start_date_applied": None},
            {"$set": {**payload_dict, "scheme_code": scheme_code}},
            upsert=True,
        )

    await _touch_recent(scheme_code, meta["scheme_name"])
    return payload_dict


@api.get("/funds/{scheme_code}/analyze/lumpsum")
async def analyze_lumpsum(
    scheme_code: int,
    refresh: bool = False,
    start_date: Optional[str] = Query(None, description="YYYY-MM-DD"),
    include_predecessor: bool = Query(
        True,
        description=(
            "False = analyse this scheme code's own NAV history only, excluding any "
            "older AMC/pre-transfer predecessor history that would otherwise be "
            "stitched in."
        ),
    ),
):
    """Lumpsum rolling-window analysis for a fund (1–50Y horizons)."""
    start_d = _parse_date(start_date)

    if start_d is None and not refresh and include_predecessor:
        cached = await _cached_lumpsum(scheme_code)
        # Only serve the cache when it carries per-window detail. The nightly
        # batch (lumpsum_refresh) strips window rows to keep docs small, which
        # left the fund screen showing an empty "ROLLING …Y LUMPSUM WINDOWS"
        # table ("Load this fund directly…") — unlike SIP, whose cache always
        # includes windows. Falling through recomputes with full windows,
        # making Lumpsum behave exactly like SIP.
        if cached and any(h.get("windows") for h in (cached.get("horizons") or [])):
            cached.pop("_id", None)
            await _touch_recent(scheme_code, cached.get("scheme_name", ""))
            return cached

    return await _build_lumpsum_analysis(scheme_code, start_date=start_d, include_predecessor=include_predecessor)


# ─── Categories ──────────────────────────────────────────────────────────────

@api.get("/categories")
async def categories():
    return {"categories": list(CATEGORY_TABLE.keys()), "default": DEFAULT_CATEGORY}


# ─── Refresh endpoints ───────────────────────────────────────────────────────

@api.get("/funds/{scheme_code}/lineage")
async def fund_lineage_explain(scheme_code: int, q: Optional[str] = Query(None)):
    """Why does this fund resolve the way it does?

    Returns the explanation object: matched identifiers, the corporate actions
    that changed the fund's identity, the weighted confidence and its evidence
    signals, the sources behind them, and the coverage caveat. Powers admin and
    search debugging, audit logs, and the manual-review queue — so that
    "why did this old scheme resolve to this new one?" is answerable from the
    API instead of by re-deriving the lineage by hand.
    """
    doc = await db[lineage_store.LINEAGE_COLLECTION].find_one(
        {"scheme_code": scheme_code}, {"_id": 0})
    if not doc:
        raise HTTPException(
            status_code=404,
            detail=f"No lineage record for scheme {scheme_code}. "
                   f"Run backend/scripts/build_lineage.py --persist to populate it.",
        )
    return lineage_store.explain_record(q or str(scheme_code), doc)


@api.get("/refresh/status")
async def refresh_status():
    doc = await db.refresh_log.find_one({"_id": "daily"}, {"_id": 0})
    return doc or {"last_run": None, "schemes_refreshed": 0, "duration_seconds": 0}


@api.post("/refresh/run")
async def refresh_run(token: Optional[str] = Query(None)):
    """Synchronous, unlike the fqs/lumpsum refresh endpoints — it returns only
    once the pass has finished. It is the narrow job (recently-viewed funds
    only, 185 on the last production run), so the caller's timeout budget is
    the bound; see .github/workflows/daily-refresh.yml."""
    expected = os.environ.get("REFRESH_TOKEN")
    if expected and token != expected:
        raise HTTPException(status_code=401, detail="Invalid token")
    n, dur = await _daily_refresh()
    return {"schemes_refreshed": n, "duration_seconds": dur}


@api.get("/refresh/full-status")
async def refresh_full_status():
    nav_doc     = await db.refresh_log.find_one({"_id": "daily"}, {"_id": 0})
    fqs_doc     = await db.fqs_refresh_log.find_one({"_id": "fqs_refresh"}, {"_id": 0})
    nav_running = _daily_refresh_running
    fqs_running = bool(_fqs_refresh_task and not _fqs_refresh_task.done())
    return {
        "nav": {
            "last_run":          nav_doc.get("last_run") if nav_doc else None,
            "schemes_refreshed": nav_doc.get("schemes_refreshed", 0) if nav_doc else 0,
            "running":           nav_running,
        },
        "fqs": {
            "last_run":  fqs_doc.get("finished_at") if fqs_doc else None,
            "refreshed": fqs_doc.get("refreshed", 0) if fqs_doc else 0,
            "total":     fqs_doc.get("total_in_index", 0) if fqs_doc else 0,
            "running":   fqs_running,
        },
    }


# ─── FQS batch refresh ───────────────────────────────────────────────────────

def _log_fqs_refresh_result(task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        logger.warning("FQS refresh task was cancelled")
    except Exception:
        logger.exception("FQS refresh task crashed")


@api.post("/fqs/refresh")
async def trigger_fqs_refresh(token: Optional[str] = Query(None)):
    """Manually trigger the FQS batch refresh (token-gated). Returns immediately."""
    expected = os.environ.get("REFRESH_TOKEN")
    if expected and token != expected:
        raise HTTPException(status_code=401, detail="Invalid token")

    global _fqs_refresh_task
    if _fqs_refresh_task and not _fqs_refresh_task.done():
        return {"status": "already_running"}

    _fqs_refresh_task = asyncio.create_task(fqs_refresh_mod.refresh_all_fqs(db))
    _fqs_refresh_task.add_done_callback(_log_fqs_refresh_result)
    return {"status": "started"}


@api.get("/fqs/refresh/status")
async def fqs_refresh_status():
    doc     = await db.fqs_refresh_log.find_one({"_id": "fqs_refresh"}, {"_id": 0})
    running = bool(_fqs_refresh_task and not _fqs_refresh_task.done())
    holder  = batch_support.current_job()
    return {
        "running":   running,
        "last_run":  doc,
        # Full-universe jobs are serialised (batch_support). A task can be
        # "running" while it is only queued behind the other one — say which,
        # so a waiting job isn't mistaken for a wedged one.
        "batch_lock_held_by": holder,
        "waiting_for_lock":   bool(running and holder not in (None, "fqs_refresh")),
    }


# ─── Lumpsum batch refresh ────────────────────────────────────────────────────

_lumpsum_refresh_task: Optional[asyncio.Task] = None


def _log_lumpsum_refresh_result(task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        logger.warning("Lumpsum refresh task was cancelled")
    except Exception:
        logger.exception("Lumpsum refresh task crashed")


@api.post("/lumpsum/refresh")
async def trigger_lumpsum_refresh(token: Optional[str] = Query(None)):
    """Manually trigger the lumpsum batch refresh (token-gated). Returns immediately."""
    expected = os.environ.get("REFRESH_TOKEN")
    if expected and token != expected:
        raise HTTPException(status_code=401, detail="Invalid token")

    global _lumpsum_refresh_task
    if _lumpsum_refresh_task and not _lumpsum_refresh_task.done():
        return {"status": "already_running"}

    _lumpsum_refresh_task = asyncio.create_task(lumpsum_refresh_mod.refresh_all_lumpsum(db))
    _lumpsum_refresh_task.add_done_callback(_log_lumpsum_refresh_result)
    return {"status": "started"}


@api.get("/lumpsum/refresh/status")
async def lumpsum_refresh_status():
    doc     = await db.lumpsum_refresh_log.find_one({"_id": "lumpsum_refresh"}, {"_id": 0})
    running = bool(_lumpsum_refresh_task and not _lumpsum_refresh_task.done())
    holder  = batch_support.current_job()
    return {
        "running":   running,
        "last_run":  doc,
        "batch_lock_held_by": holder,
        "waiting_for_lock":   bool(running and holder not in (None, "lumpsum_refresh")),
    }


# ─── Fund continuity events (manual curation for the "Separate Management
#     Periods" toggle) ────────────────────────────────────────────────────────
#
# Toggle 2 needs a break date for every fund whose management/strategy
# materially changed. Most of that is covered for free by data we already
# trust: merged/successor schemes (see fqs_refresh.py's continuity pass,
# which reuses amfi_client.load_analysis_nav(include_predecessor=False) —
# the exact mechanism behind the fund-detail screen's "new management only"
# toggle). This collection exists only for the residual case: SAME scheme
# code, no merger/successor record, but the advisor knows the fund changed
# hands or mandate materially (e.g. AMC internal manager reshuffle with a
# style shift). There is no reliable signal to auto-detect this, so it's a
# small manually-curated table — deliberately starts empty.
#
# One event per scheme_code; upserting always keeps the LATER break_date
# (the most recent known discontinuity is what should truncate history).

class ContinuityEventIn(BaseModel):
    scheme_code: int
    break_date: str          # YYYY-MM-DD
    reason: str              # short label, e.g. "Manager change", "Mandate shift"
    note: Optional[str] = None


@api.get("/continuity/events")
async def list_continuity_events():
    """Open read — small dataset, needed by the nightly refresh jobs and
    useful for the advisor to audit what's curated so far."""
    cursor = db.fund_continuity_events.find({}, {"_id": 0})
    return {"events": [doc async for doc in cursor]}


@api.post("/continuity/events")
async def upsert_continuity_event(event: ContinuityEventIn, token: Optional[str] = Query(None)):
    """Token-gated. Upserts by scheme_code, keeping the later break_date if
    an entry already exists (a more recent discontinuity supersedes an older
    one; it should never make a fund's usable history shorter than what's
    already been verified)."""
    expected = os.environ.get("REFRESH_TOKEN")
    if expected and token != expected:
        raise HTTPException(status_code=401, detail="Invalid token")

    parsed = _parse_date(event.break_date)
    if not parsed:
        raise HTTPException(status_code=400, detail="break_date must be YYYY-MM-DD")

    existing = await db.fund_continuity_events.find_one({"scheme_code": event.scheme_code})
    if existing and existing.get("break_date", "") > event.break_date:
        raise HTTPException(
            status_code=409,
            detail=f"Existing event has a later break_date ({existing['break_date']}); "
                   f"delete it first if you really want to move it earlier.",
        )

    await db.fund_continuity_events.update_one(
        {"scheme_code": event.scheme_code},
        {
            "$set": {
                "scheme_code": event.scheme_code,
                "break_date":  event.break_date,
                "reason":      event.reason,
                "note":        event.note,
                "updated_at":  datetime.now(timezone.utc).isoformat(),
            }
        },
        upsert=True,
    )
    return {"status": "ok"}


@api.delete("/continuity/events/{scheme_code}")
async def delete_continuity_event(scheme_code: int, token: Optional[str] = Query(None)):
    expected = os.environ.get("REFRESH_TOKEN")
    if expected and token != expected:
        raise HTTPException(status_code=401, detail="Invalid token")
    result = await db.fund_continuity_events.delete_one({"scheme_code": scheme_code})
    return {"status": "ok", "deleted": result.deleted_count}


# ─── Export endpoints ─────────────────────────────────────────────────────────

@api.get("/export/all-funds")
async def export_all_funds(
    min_history_years: float = Query(1.0, ge=0.5, le=20.0),
):
    """Bulk export — all indexed funds with full per-horizon metrics.

    Reads everything from fund_nav_meta. fqs_by_horizon contains the complete
    metric set written by fqs_refresh.py (nightly) or _save_analysis (on view).

    The export script (export_all_funds_fqs.py) calls this endpoint once and
    builds the full Excel locally — no per-fund HTTP calls needed.

    Returns funds sorted by category then headline_fqs descending.
    """
    cursor = db.fund_nav_meta.find(
        {
            "history_years":  {"$gte": min_history_years},
            "fqs_by_horizon": {"$exists": True, "$ne": {}},
        },
        {
            "_id": 0,
            "scheme_code":              1,
            "scheme_name":              1,
            "category":                 1,
            "history_years":            1,
            "headline_fqs":             1,
            "headline_tier":            1,
            "headline_horizon":         1,
            "stability_check":          1,
            "cycle_robustness":         1,
            "cycle_robustness_variant": 1,
            "fqs_by_horizon":           1,
            "last_fqs_refresh":         1,
        },
    )

    funds = [doc async for doc in cursor]

    if not funds:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_funds":  0,
            "funds":        [],
            "note": (
                "No funds have been scored yet. "
                "Run POST /api/fqs/refresh first, then try again."
            ),
        }

    funds.sort(key=lambda f: (f.get("category") or "", -(f.get("headline_fqs") or 0)))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_funds":  len(funds),
        "funds":        funds,
    }


@api.get("/export/status")
async def export_status():
    """Check whether the export endpoint has data ready to serve.

    Returns counts so the export script can warn the user if data is missing
    before attempting a full download.
    """
    total_indexed = await db.fund_nav_meta.count_documents({})
    total_scored  = await db.fund_nav_meta.count_documents(
        {"fqs_by_horizon": {"$exists": True, "$ne": {}}}
    )
    fqs_log = await db.fqs_refresh_log.find_one({"_id": "fqs_refresh"}, {"_id": 0})
    nav_log = await db.nav_meta_refresh_log.find_one({"_id": "nav_meta"}, {"_id": 0})

    fqs_running = bool(_fqs_refresh_task and not _fqs_refresh_task.done())
    nav_running = bool(_nav_index_task   and not _nav_index_task.done())

    return {
        "ready":               total_scored > 0,
        "total_indexed":       total_indexed,
        "total_scored":        total_scored,
        "pct_scored":          round(total_scored / total_indexed * 100, 1) if total_indexed else 0,
        "fqs_last_refreshed":  fqs_log.get("finished_at") if fqs_log else None,
        "index_last_built":    nav_log.get("finished_at") if nav_log else None,
        "fqs_refresh_running": fqs_running,
        "index_running":       nav_running,
    }


# ─── Daily NAV refresh ───────────────────────────────────────────────────────

async def _daily_refresh() -> tuple[int, float]:
    """Recompute analysis for every scheme viewed in the last 30 days."""
    global _daily_refresh_running
    _daily_refresh_running = True
    try:
        return await _daily_refresh_inner()
    finally:
        _daily_refresh_running = False


async def _daily_refresh_inner() -> tuple[int, float]:
    started = datetime.now(timezone.utc)
    cutoff  = (started - timedelta(days=30)).isoformat()
    cursor  = db.fund_recent.find({"last_seen": {"$gte": cutoff}}, {"_id": 0, "scheme_code": 1})
    codes   = [r["scheme_code"] async for r in cursor]
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
            "last_run":         started.isoformat(),
            "schemes_refreshed": completed,
            "total_attempted":  len(codes),
            "duration_seconds": round(duration, 2),
        }},
        upsert=True,
    )
    logger.info("Daily refresh finished: %d/%d in %.1fs", completed, len(codes), duration)
    return completed, duration


async def _refresh_scheduler():
    """Runs daily at 22:00 UTC (03:30 IST) — refreshes recently-viewed funds."""
    while True:
        now    = datetime.now(timezone.utc)
        target = now.replace(hour=22, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        delay = (target - now).total_seconds()
        logger.info("Next NAV refresh scheduled in %.0f minutes", delay / 60)
        try:
            await asyncio.sleep(delay)
            await _daily_refresh()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("refresh scheduler error")
            await asyncio.sleep(600)


async def _fqs_refresh_scheduler():
    """Runs daily at 22:30 UTC (04:00 IST) — SIP FQS scores for all indexed funds."""
    global _fqs_refresh_task
    while True:
        now    = datetime.now(timezone.utc)
        target = now.replace(hour=22, minute=30, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        delay = (target - now).total_seconds()
        logger.info("Next FQS refresh scheduled in %.0f minutes", delay / 60)
        try:
            await asyncio.sleep(delay)
            _fqs_refresh_task = asyncio.create_task(fqs_refresh_mod.refresh_all_fqs(db))
            _fqs_refresh_task.add_done_callback(_log_fqs_refresh_result)
            await _fqs_refresh_task
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("FQS refresh scheduler error")
            await asyncio.sleep(600)


async def _lumpsum_refresh_scheduler():
    """Runs daily at 23:00 UTC (04:30 IST) — lumpsum analysis for all indexed funds.
    Runs 30 min after SIP FQS refresh so NAV data is already current."""
    while True:
        now    = datetime.now(timezone.utc)
        target = now.replace(hour=23, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        delay = (target - now).total_seconds()
        logger.info("Next lumpsum refresh scheduled in %.0f minutes", delay / 60)
        try:
            await asyncio.sleep(delay)
            await lumpsum_refresh_mod.refresh_all_lumpsum(db)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Lumpsum refresh scheduler error")
            await asyncio.sleep(600)


# ─── Merger Discovery (admin) ────────────────────────────────────────────────

_merger_discovery_task:   Optional[asyncio.Task] = None
_merger_discovery_result: Optional[Dict[str, Any]] = None

_lineage_build_task:   Optional[asyncio.Task] = None
_lineage_build_result: Optional[Dict[str, Any]] = None

# ── Name helpers ──────────────────────────────────────────────────────────────

_MD_STOPWORDS = {
    "fund", "mutual", "scheme", "direct", "regular", "growth", "dividend",
    "idcw", "plan", "option", "reinvestment", "payout", "bonus", "the", "of",
    "and", "&", "india", "limited", "ltd", "-", "–",
}

def _md_keywords(name: str) -> Set[str]:
    tokens = name.lower().replace("-", " ").replace("&", " ").split()
    return {t for t in tokens if t not in _MD_STOPWORDS and len(t) > 2}

def _md_jaccard(a: str, b: str) -> float:
    ka, kb = _md_keywords(a), _md_keywords(b)
    if not ka or not kb:
        return 0.0
    return len(ka & kb) / len(ka | kb)

# ── Stats helpers ─────────────────────────────────────────────────────────────

def _md_daily_returns(series: Dict[date, float]) -> Dict[date, float]:
    dates = sorted(series)
    out: Dict[date, float] = {}
    for i in range(1, len(dates)):
        d0, d1 = dates[i - 1], dates[i]
        if series[d0] > 0:
            out[d1] = (series[d1] - series[d0]) / series[d0]
    return out

def _md_pearson(xs: List[float], ys: List[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx  = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy  = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)

def _md_return_correlation(
    series_a: Dict[date, float],
    series_b: Dict[date, float],
    overlap_start: Optional[date] = None,
    overlap_end:   Optional[date] = None,
    min_overlap: int = 10,
) -> Tuple[float, int]:
    ret_a = _md_daily_returns(series_a)
    ret_b = _md_daily_returns(series_b)
    common = sorted(ret_a.keys() & ret_b.keys())
    if overlap_start:
        common = [d for d in common if d >= overlap_start]
    if overlap_end:
        common = [d for d in common if d <= overlap_end]
    if len(common) < min_overlap:
        return 0.0, len(common)
    xs = [ret_a[d] for d in common]
    ys = [ret_b[d] for d in common]
    return _md_pearson(xs, ys), len(common)

# ── Core discovery task ───────────────────────────────────────────────────────

async def _run_merger_discovery() -> Dict[str, Any]:
    today  = date.today()
    cutoff = today - timedelta(days=180)

    logger.info("[merger-discovery] querying wound-up funds …")
    cursor = db.fund_nav_meta.find(
        {"latest_nav_date": {"$lt": cutoff.isoformat()}},
        {"scheme_code": 1, "scheme_name": 1, "fund_house": 1,
         "latest_nav_date": 1, "earliest_nav_date": 1},
    )
    wound_up = [doc async for doc in cursor]
    logger.info("[merger-discovery] %d wound-up candidates", len(wound_up))

    if not wound_up:
        return {
            "status": "done",
            "wound_up_count": 0,
            "candidates": [],
            "merger_map_snippet": {},
            "note": (
                "No wound-up funds found. Check that fund_nav_meta has "
                "'latest_nav_date' fields — run POST /api/home/reindex first."
            ),
        }

    # Active funds (last NAV within 60 days), grouped by house
    active_cursor = db.fund_nav_meta.find(
        {"latest_nav_date": {"$gte": (today - timedelta(days=60)).isoformat()}},
        {"scheme_code": 1, "scheme_name": 1, "fund_house": 1, "earliest_nav_date": 1},
    )
    # Group by CANONICAL house so AMC-level mergers (which change the fund-house
    # name, e.g. BNP Paribas → Baroda BNP Paribas) still land in the same bucket.
    # Same-house string grouping used to make cross-house successors undiscoverable.
    active_by_house: Dict[str, List[Dict]] = defaultdict(list)
    async for doc in active_cursor:
        house = (doc.get("fund_house") or "").strip()
        if house:
            active_by_house[fund_lineage.canonical_house(house)].append(doc)
    logger.info(
        "[merger-discovery] %d active funds across %d canonical houses",
        sum(len(v) for v in active_by_house.values()), len(active_by_house),
    )

    sem = asyncio.Semaphore(4)
    MIN_KEYWORD_JACCARD = 0.15
    TRANSITION_WINDOW   = 180

    # ── Pre-filter pairs in pure Python BEFORE creating any coroutines ─────────
    # asyncio.gather(*all_pairs) on millions of pairs causes OOM even before any
    # HTTP requests start (coroutine frame objects alone eat memory).
    # Apply cheap CPU filters first so only genuine merger candidates get tasks.
    PrePair = tuple  # (wu, cand, sim, delta, wu_last, cand_first)
    pre_filtered: List[Any] = []
    for wu in wound_up:
        wu_last_str = wu.get("latest_nav_date")
        if not wu_last_str:
            continue
        wu_last = date.fromisoformat(wu_last_str)
        wu_name = wu.get("scheme_name", "")
        house   = fund_lineage.canonical_house((wu.get("fund_house") or "").strip())

        for cand in active_by_house.get(house, []):
            if cand["scheme_code"] == wu["scheme_code"]:
                continue
            cand_first_str = cand.get("earliest_nav_date")
            if not cand_first_str:
                continue
            # Cheap timing filter first (no string ops)
            cand_first = date.fromisoformat(cand_first_str)
            delta = abs((cand_first - wu_last).days)
            if delta > TRANSITION_WINDOW:
                continue
            # Name similarity filter (still cheap — pure Python string ops)
            sim = _md_jaccard(wu_name, cand.get("scheme_name", ""))
            if sim < MIN_KEYWORD_JACCARD:
                continue
            pre_filtered.append((wu, cand, sim, delta, wu_last, cand_first_str))

    logger.info(
        "[merger-discovery] %d pairs after pre-filter (from %d wound-up funds)",
        len(pre_filtered), len(wound_up),
    )

    # ── Coroutine-per-pair: only pre-filtered pairs reach this stage ──────────
    async def check_pair(
        wu: Dict, cand: Dict, sim: float, delta: int,
        wu_last: date, cand_first_str: str,
    ) -> Optional[Dict]:
        wu_code       = wu["scheme_code"]
        cand_code     = cand["scheme_code"]
        wu_last_str   = wu.get("latest_nav_date", "")

        async with sem:
            try:
                wu_payload = await amfi_client.fetch_fund_nav(wu_code)
                wu_nav     = amfi_client.parse_nav_history(wu_payload)
            except Exception:
                return None
            try:
                cand_payload = await amfi_client.fetch_fund_nav(cand_code)
                cand_nav     = amfi_client.parse_nav_history(cand_payload)
            except Exception:
                return None

        wu_series   = {r["date"]: r["nav"] for r in wu_nav}
        cand_series = {r["date"]: r["nav"] for r in cand_nav}

        window_start = wu_last - timedelta(days=90)
        window_end   = wu_last + timedelta(days=90)
        corr, overlap = _md_return_correlation(wu_series, cand_series, window_start, window_end)

        # Free large NAV lists immediately after correlation is computed
        del wu_nav, cand_nav, wu_series, cand_series

        wu_payload_meta = amfi_client.meta_from_payload(wu_payload)
        wu_years = 0.0
        try:
            wu_hist_raw = wu_payload.get("data") or []
            if len(wu_hist_raw) >= 2:
                d0 = datetime.strptime(wu_hist_raw[-1]["date"], "%d-%m-%Y").date()
                d1 = datetime.strptime(wu_hist_raw[0]["date"],  "%d-%m-%Y").date()
                wu_years = abs((d0 - d1).days) / 365.25
        except Exception:
            pass
        del wu_payload, cand_payload

        if overlap >= 10:
            confidence = 0.60 * corr + 0.25 * sim + 0.15 * min(1.0, overlap / 60)
        elif corr == 0.0:
            timing_score = max(0.0, 1.0 - delta / TRANSITION_WINDOW)
            confidence   = 0.50 * sim + 0.50 * timing_score
        else:
            confidence = 0.0

        return {
            "wu_code":         wu_code,
            "wu_name":         wu.get("scheme_name", ""),
            "wu_last":         wu_last_str,
            "wu_years":        round(wu_years, 1),
            "cand_code":       cand_code,
            "cand_name":       cand.get("scheme_name", ""),
            "cand_first":      cand_first_str,
            "transition_days": delta,
            "overlap_days":    overlap,
            "correlation":     round(corr, 4),
            "name_sim":        round(sim, 3),
            "confidence":      round(confidence, 4),
            "fund_house":      wu.get("fund_house", ""),
            "tier": (
                "VERY HIGH" if confidence >= 0.80 else
                "HIGH"      if confidence >= 0.60 else
                "MEDIUM"    if confidence >= 0.40 else
                "LOW"
            ),
        }

    tasks = [check_pair(wu, cand, sim, delta, wu_last, cand_first_str)
             for wu, cand, sim, delta, wu_last, cand_first_str in pre_filtered]

    logger.info("[merger-discovery] running %d correlated-NAV checks …", len(tasks))
    results = await asyncio.gather(*tasks)

    raw = [r for r in results if r and r["confidence"] >= 0.30]
    raw.sort(key=lambda x: -x["confidence"])

    # Best match per wound-up fund
    seen_wu: Set[int] = set()
    deduped = []
    for r in raw:
        if r["wu_code"] not in seen_wu:
            seen_wu.add(r["wu_code"])
            deduped.append(r)

    merger_map_snippet = {
        str(c["wu_code"]): c["cand_code"]
        for c in deduped if c["confidence"] >= 0.60
    }

    result = {
        "status":              "done",
        "wound_up_count":      len(wound_up),
        "pairs_evaluated":     len(tasks),
        "candidates":          deduped,
        "merger_map_snippet":  merger_map_snippet,
        "completed_at":        datetime.now(timezone.utc).isoformat(),
    }
    logger.info(
        "[merger-discovery] done — %d candidates, %d HIGH+ confidence",
        len(deduped), len(merger_map_snippet),
    )
    # Persist to MongoDB so results survive a Render restart
    try:
        await db.merger_discovery_log.update_one(
            {"_id": "latest"},
            {"$set": result},
            upsert=True,
        )
    except Exception as exc:
        logger.warning("[merger-discovery] failed to persist results: %s", exc)
    return result


@api.post("/admin/merger-discovery")
async def start_merger_discovery(token: Optional[str] = Query(None)):
    """Start a background merger-discovery run (token-gated).

    Uses fund_nav_meta + mfapi.in to find wound-up funds and their successors.
    Poll GET /api/admin/merger-discovery/status for results.
    """
    global _merger_discovery_task, _merger_discovery_result
    expected = os.environ.get("REFRESH_TOKEN")
    if expected and token != expected:
        raise HTTPException(status_code=401, detail="Invalid token")

    if _merger_discovery_task and not _merger_discovery_task.done():
        return {"status": "already_running"}

    _merger_discovery_result = None

    # Mark in-progress in DB immediately so status survives a cold wake
    try:
        await db.merger_discovery_log.update_one(
            {"_id": "latest"},
            {"$set": {"status": "running", "started_at": datetime.now(timezone.utc).isoformat()}},
            upsert=True,
        )
    except Exception:
        pass

    async def _run():
        global _merger_discovery_result
        # Keepalive: ping health endpoint every 4 min so Render doesn't sleep
        base_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
        stop_ping = asyncio.Event()

        async def _ping():
            import httpx as _httpx
            async with _httpx.AsyncClient(timeout=10.0) as hc:
                while not stop_ping.is_set():
                    try:
                        await asyncio.wait_for(stop_ping.wait(), timeout=240)
                        break
                    except asyncio.TimeoutError:
                        pass
                    try:
                        await hc.get(f"{base_url}/api/health")
                    except Exception:
                        pass

        ping_task = asyncio.create_task(_ping()) if base_url else None
        try:
            _merger_discovery_result = await _run_merger_discovery()
            # Auto-apply: refresh the runtime successor cache so newly found
            # HIGH+ pairs take effect immediately — no redeploy, no manual
            # MERGER_MAP edit. Each pair is still correlation-verified at use.
            try:
                applied = await refresh_successor_cache()
                logger.info("[merger-discovery] applied — %d successor pairs live", applied)
                # Newly found pairs must also collapse the predecessor's own
                # listing entry, or the pre-merger fund keeps showing up
                # alongside its successor as a separate, selectable fund.
                stats = await fund_status.sync_superseded(db)
                logger.info("[merger-discovery] fund status sync: %s", stats)
            except Exception:
                logger.exception("[merger-discovery] cache refresh failed")
        except Exception as exc:
            err = {"status": "error", "error": str(exc)}
            _merger_discovery_result = err
            try:
                await db.merger_discovery_log.update_one(
                    {"_id": "latest"}, {"$set": err}, upsert=True
                )
            except Exception:
                pass
        finally:
            stop_ping.set()
            if ping_task:
                try:
                    await asyncio.wait_for(ping_task, timeout=3.0)
                except Exception:
                    pass

    _merger_discovery_task = asyncio.create_task(_run())
    return {"status": "started"}


@api.post("/admin/fund-status/sync")
async def sync_fund_status(token: Optional[str] = Query(None)):
    """Re-apply the generic AMC-change mapping across the whole fund universe.

    Idempotent and cheap (two Mongo round-trips, no NAV fetches) — the same
    thing startup does. Exposed so a newly curated or newly discovered
    successor pair can be applied without a redeploy.
    """
    expected = os.environ.get("REFRESH_TOKEN")
    if expected and token != expected:
        raise HTTPException(status_code=401, detail="Invalid token")
    stats = await fund_status.sync_superseded(db)
    return {"status": "ok", **stats}


@api.post("/admin/build-lineage")
async def trigger_lineage_build(
    token: Optional[str] = Query(None),
    quick: bool = Query(
        False, description="Sample ~6 archive captures for a fast smoke-test "
                            "run instead of the full crawl (minutes vs longer)."),
    hist_years: int = Query(20, ge=1, le=30),
):
    """Populate fund_lineage / fund_identifier_idx / fund_successor_map (see
    lineage_build.py) for the ENTIRE fund universe from AMFI's own historical
    records — archived NAVAll.txt captures (Internet Archive) for
    period-correct name/AMC history, AMFI's historical existence report back
    to ~2006, the live file for current identity, PLUS the source-verified
    fund_lineage.CURATED_AMC_CHANGES table applied on top (covers the
    dominant known AMC renames/acquisitions even on a run where the Internet
    Archive is unreachable — see that table's docstring).

    This is the ONE step that actually writes real lineage data for every
    fund into THIS deployment's database — nothing upstream of this endpoint
    can do that from outside the deployed app, since it needs the app's own
    MONGO_URL. Run it once after deploying this feature, then re-run
    whenever CURATED_AMC_CHANGES gains new entries or periodically to pick
    up new archive captures.

    Long-running (a full, non-quick crawl fetches many Wayback Machine
    captures + AMFI history windows — tens of minutes). Runs as a background
    task, same pattern as /api/home/reindex and /api/admin/merger-discovery;
    poll GET /api/admin/build-lineage/status for progress and the result.
    """
    expected = os.environ.get("REFRESH_TOKEN")
    if expected and token != expected:
        raise HTTPException(status_code=401, detail="Invalid token")

    global _lineage_build_task, _lineage_build_result
    if _lineage_build_task and not _lineage_build_task.done():
        return {"status": "already_running"}

    _lineage_build_result = None

    base_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    stop_ping = asyncio.Event()

    async def _ping():
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=10.0) as hc:
            while not stop_ping.is_set():
                try:
                    await asyncio.wait_for(stop_ping.wait(), timeout=240)
                    break
                except asyncio.TimeoutError:
                    pass
                try:
                    await hc.get(f"{base_url}/api/health")
                except Exception:
                    pass

    async def _run():
        global _lineage_build_result
        ping_task = asyncio.create_task(_ping()) if base_url else None
        try:
            summary = await lineage_build.build_lineage(
                db, quick=quick, persist=True, hist_years=hist_years,
                log=lambda msg: logger.info("[build-lineage] %s", msg),
            )
            _lineage_build_result = {"status": "ok", **summary}
            logger.info("[build-lineage] finished: %s", summary.get("persisted"))
            # Successor pairs (including any newly ISIN-verified ones the
            # derivation found) must be live immediately, and superseded
            # funds re-synced — same as after merger-discovery.
            try:
                applied = await refresh_successor_cache()
                stats = await fund_status.sync_superseded(db)
                logger.info(
                    "[build-lineage] successor cache refreshed (%d pairs), fund status sync: %s",
                    applied, stats,
                )
            except Exception:
                logger.exception("[build-lineage] post-build refresh failed")
        except Exception as exc:
            logger.exception("[build-lineage] failed")
            _lineage_build_result = {"status": "error", "error": str(exc)}
        finally:
            stop_ping.set()
            if ping_task:
                try:
                    await asyncio.wait_for(ping_task, timeout=3.0)
                except Exception:
                    pass

    _lineage_build_task = asyncio.create_task(_run())
    return {"status": "started", "quick": quick}


@api.get("/admin/build-lineage/status")
async def lineage_build_status():
    running = bool(_lineage_build_task and not _lineage_build_task.done())
    return {"running": running, "last_result": _lineage_build_result}


@api.get("/admin/fund-status")
async def fund_status_summary(limit: int = Query(50, ge=1, le=500)):
    """How many indexed funds are currently hidden as dead, and a sample of
    them with the reason — so the rule can be audited rather than trusted.

    Counts come from the database (count_documents), never by loading the
    universe.
    """
    total = await db.fund_nav_meta.count_documents({})
    active_filter = fund_status.active_query()
    active = await db.fund_nav_meta.count_documents(active_filter)
    sample: List[Dict[str, Any]] = []
    async for doc in db.fund_nav_meta.find(
        {"$nor": [active_filter]},
        {"_id": 0, "scheme_code": 1, "scheme_name": 1, "superseded_by": 1,
         "merged_with": 1, "own_latest_nav_date": 1, "latest_nav_date": 1},
    ).limit(limit):
        sample.append({
            "scheme_code": doc.get("scheme_code"),
            "scheme_name": doc.get("scheme_name"),
            "reason": fund_status.inactive_reason(doc),
        })
    return {
        "total_indexed": total,
        "active": active,
        "hidden": total - active,
        "stale_after_days": fund_status.STALE_AFTER_DAYS,
        "stale_cutoff": fund_status.stale_cutoff_iso(),
        "sample": sample,
    }


@api.get("/admin/merger-discovery/status")
async def merger_discovery_status(token: Optional[str] = Query(None)):
    """Poll for merger-discovery results.

    Returns {"status": "running"} while in progress,
    then full candidate list + MERGER_MAP snippet when done.
    Results are persisted in MongoDB so they survive Render restarts.
    """
    expected = os.environ.get("REFRESH_TOKEN")
    if expected and token != expected:
        raise HTTPException(status_code=401, detail="Invalid token")

    running = bool(_merger_discovery_task and not _merger_discovery_task.done())
    if running:
        return {"status": "running"}

    # In-memory result (same process, task finished)
    if _merger_discovery_result is not None:
        return _merger_discovery_result

    # Fallback: read from MongoDB (survives restarts)
    doc = await db.merger_discovery_log.find_one({"_id": "latest"}, {"_id": 0})
    if doc:
        return doc
    return {"status": "not_started"}


# ─── CORS + mount ────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api)

# ─── Portfolio Proposal feature ──────────────────────────────────────────────
# Reuses _build_analysis / _cached_analysis (SIP) and _build_lumpsum_analysis /
# _cached_lumpsum (Lumpsum) verbatim — proposal_routes.py has no fund-analysis
# logic of its own. See memory/PROPOSAL_FEATURE_SPEC.md.
import proposal_routes

app.include_router(
    proposal_routes.build_router(
        db,
        _build_analysis,
        _cached_analysis,
        _build_lumpsum_analysis,
        _cached_lumpsum,
    )
)

# ─── Rolling Analysis feature ────────────────────────────────────────────────
# Single-fund rolling-period comparison: pick one fund, an overall date range,
# and a rolling-window length; see how P10/Median/Blended WM, Success %, CV
# and FQS vary bucket-by-start-year instead of collapsing into one number.
import rolling_analysis_routes

app.include_router(rolling_analysis_routes.build_router(db))

# ─── Bulk Fund Analysis feature ──────────────────────────────────────────────
# Excel upload -> existing FQS engine -> Excel export. Reuses _build_analysis
# / _cached_analysis verbatim (same injection pattern as proposal_routes.py
# above) — bulk_analysis.py has no fund-analysis logic of its own.
import bulk_analysis_routes

app.include_router(bulk_analysis_routes.build_router(db, _build_analysis, _cached_analysis))

# ─── Portfolio Overlap feature ───────────────────────────────────────────────
# Compare the equity books of two funds: how much of a rupee in A is invested
# in the same securities as a rupee in B. Weight-based (Σ min(w_a, w_b)), not
# a count of shared names — see overlap_engine.py. Holdings come from AMC
# monthly portfolio disclosures ingested into `fund_holdings`; the router
# owns no math and no parsing of its own.
import overlap_routes

app.include_router(overlap_routes.build_router(db))
# ─── Portfolio Holding Review feature ────────────────────────────────────────
# Upload an earlier Portfolio Proposal PDF (or a previous review report), compare
# its stored FQS/WM/XIRR/CV with today's, and produce the Holding Review Report.
# Reuses _build_analysis/_cached_analysis (SIP) and the lumpsum equivalents
# verbatim — holding_review_routes.py has no scoring logic of its own.
import holding_review_routes

app.include_router(
    holding_review_routes.build_router(
        db,
        _build_analysis,
        _cached_analysis,
        _build_lumpsum_analysis,
        _cached_lumpsum,
    )
)
