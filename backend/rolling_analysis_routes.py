"""Rolling Analysis API — the "Rolling Analysis" tab.

Takes ONE fund and asks: how STABLE are its rolling-window numbers across
different stretches of history — rather than the single blended figure
Home/fund-detail report.

Mechanically it reuses the exact same rolling-window builders the rest of
the app already uses (`fqs_engine.compute_rolling_windows` /
`lumpsum_engine.compute_rolling_lumpsum_windows`) to generate every monthly-
sliding window of length `window_years` inside [start_date, end_date] — the
same windows Home would blend into one P10/Median/Blended-WM/CV figure. The
only new step here is grouping those windows by the START DATE they fall in
and running the same per-horizon statistics (`fqs_engine.score_horizon` /
`lumpsum_engine.compute_horizon_stats`) independently on each group instead
of once across the whole range.

Two ways to define the groups (`buckets` on the request):

  - Omitted → one bucket per calendar year (the original behaviour): every
    window that started sometime in a given Jan-Dec is one row. Fine
    granularity, but with a fixed window_years each bucket only has ~12
    windows feeding its P10/CV, which is a thin sample.

  - Advisor-supplied list of {start_date, end_date} → e.g. "2010-01-01 to
    2017-12-31" and "2018-01-01 to 2026-12-31". Fewer, wider buckets means
    more windows land in each one (an 8-year bucket with a 5Y window gets
    ~3x the windows a 1-year bucket does), so the percentiles/CV are
    statistically sturdier — at the cost of coarser granularity. This is the
    advisor's call, not something we should guess at, hence fully custom
    boundaries rather than an auto-split.

A bucket's label always describes the START-DATE range used to group windows
into it (not the investment horizon those windows ran for) — e.g. a bucket
spanning 2010-2017 with a 5Y window includes windows that started anywhere
in 2010-2017 and therefore ENDED anywhere in 2015-2022; the label reflects
the grouping criterion, not every individual window's own span.

A bucket with zero windows (e.g. a custom range narrower than window_years,
or one that simply has no NAV data) is still returned — with `n_windows: 0`
and null metrics — rather than silently dropped, so the advisor can see it
was considered and why it's empty, instead of wondering if it was missed.

An "overall" row (every window across the full range, in one bucket) is
included too, as the baseline every period row is implicitly compared
against — it's exactly what `/funds/{code}/analyze[/lumpsum]` would report
for this horizon over the same bounded range.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import uuid
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

import amfi_client
import fqs_engine
import lumpsum_engine
from category_map import classify_fund, get_lumpsum_min_wm, get_lumpsum_cap, is_equity_category
from rolling_analysis_pdf import build_rolling_analysis_pdf
from rolling_analysis_multi_pdf import build_rolling_multi_pdf

logger = logging.getLogger("mf_api.rolling_analysis")

MIN_WINDOW_YEARS = 1
MAX_WINDOW_YEARS = 30
MAX_CUSTOM_BUCKETS = 20  # sane upper bound — this is a live-scored request, not a batch job

# ── Multi-fund consistency ranking ──────────────────────────────────────────
# Debt/Gold-Silver always sort last on Home regardless of WM (server.py's
# home_universe._sort_key) — excluded from the AUTO pool for the same reason
# the Backtest tab used to exclude them: they'd otherwise crowd equity funds
# out of the live-fetch budget on "most NAV history" sorting alone. Still
# reachable one at a time via scheme_codes (hand-picked additions).
EXCLUDED_POOL_CATEGORIES = {"Debt", "Gold/Silver"}
MAX_POOL_SIZE = 80
MAX_HAND_PICKED = 80
MAX_TOTAL_FUNDS = 80        # combined cap — this is a live-scored request per click
FETCH_CONCURRENCY = 8       # bounded parallelism against mfapi.in, same as Backtest used

# The fields shown per period — identical set to the Backtest tab and to the
# fund-detail CONSISTENCY section, on purpose (same numbers, just sliced
# differently). Nullable here: an empty bucket reports n_windows=0 and None
# for the rest rather than being dropped from the response.
PERIOD_FIELDS = ("n_windows", "p10_wm", "median_wm", "blended_wm", "success_pct", "cv", "min_wm_required", "fqs")


class BucketDef(BaseModel):
    """One advisor-defined comparison period. `label` is optional — if
    omitted, one is derived from the dates (e.g. "2010–2017")."""
    start_date: str
    end_date: str
    label: Optional[str] = None


class RollingAnalysisRequest(BaseModel):
    scheme_code: int
    start_date: str = Field(..., description="YYYY-MM-DD — overall lower bound on NAV data used")
    end_date: str = Field(..., description="YYYY-MM-DD — overall upper bound on NAV data used")
    window_years: int = Field(5, ge=MIN_WINDOW_YEARS, le=MAX_WINDOW_YEARS)
    mode: str = Field("sip", pattern="^(sip|lumpsum)$")
    buckets: Optional[List[BucketDef]] = Field(
        None,
        description=(
            "Advisor-defined comparison periods, e.g. [{start_date:2010-01-01,end_date:2017-12-31}, "
            "{start_date:2018-01-01,end_date:2026-12-31}]. Omit for the default: one bucket per "
            "calendar year across the full range."
        ),
    )


def _parse_iso_date(s: str, field: str) -> date:
    try:
        return date.fromisoformat(s)
    except Exception:
        raise HTTPException(status_code=400, detail=f"{field} must be YYYY-MM-DD")


def _auto_year_buckets(start_d: date, end_d: date) -> List[Tuple[date, date, Optional[str]]]:
    """Default grouping: one bucket per calendar year, clipped to the overall
    range at both ends (so the first/last bucket may be a partial year)."""
    out: List[Tuple[date, date, Optional[str]]] = []
    y = start_d.year
    while date(y, 1, 1) <= end_d:
        b_start = max(start_d, date(y, 1, 1))
        b_end = min(end_d, date(y, 12, 31))
        out.append((b_start, b_end, None))
        y += 1
    return out


def _resolve_buckets(
    req_buckets: Optional[List[BucketDef]], start_d: date, end_d: date
) -> List[Tuple[date, date, Optional[str]]]:
    if not req_buckets:
        return _auto_year_buckets(start_d, end_d)

    if len(req_buckets) > MAX_CUSTOM_BUCKETS:
        raise HTTPException(status_code=422, detail=f"At most {MAX_CUSTOM_BUCKETS} custom periods per request.")

    parsed: List[Tuple[date, date, Optional[str]]] = []
    for i, b in enumerate(req_buckets):
        bs = _parse_iso_date(b.start_date, f"buckets[{i}].start_date")
        be = _parse_iso_date(b.end_date, f"buckets[{i}].end_date")
        if be <= bs:
            raise HTTPException(
                status_code=422,
                detail=f"Period {i + 1} ({b.start_date} → {b.end_date}): end_date must be after start_date.",
            )
        parsed.append((bs, be, b.label.strip() if b.label and b.label.strip() else None))

    parsed.sort(key=lambda t: t[0])
    return parsed


def _bucket_label(start_d: date, end_d: date, override: Optional[str]) -> str:
    if override:
        return override
    return str(start_d.year) if start_d.year == end_d.year else f"{start_d.year}–{end_d.year}"


def _windows_in_range(windows: List[Any], start_d: date, end_d: date) -> List[Any]:
    return [w for w in windows if start_d <= date.fromisoformat(w.start_date) <= end_d]


def _build_periods(
    windows: List[Any],
    bucket_specs: List[Tuple[date, date, Optional[str]]],
    scorer: Callable[[List[Any]], Optional[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for bs, be, label_override in bucket_specs:
        bucket_windows = _windows_in_range(windows, bs, be)
        scored = scorer(bucket_windows) if bucket_windows else None
        row: Dict[str, Any] = {
            "label": _bucket_label(bs, be, label_override),
            "start_date": bs.isoformat(),
            "end_date": be.isoformat(),
        }
        if scored is None:
            row.update({k: (0 if k == "n_windows" else None) for k in PERIOD_FIELDS})
        else:
            row.update({k: scored.get(k) for k in PERIOD_FIELDS})
        rows.append(row)
    return rows


# ── Client-facing key points ───────────────────────────────────────────────
# Plain-English bullets comparing consecutive periods (chronological order),
# generated server-side so the on-screen list and the exported PDF always
# say exactly the same thing. Empty (n_windows=0) periods are skipped in the
# comparison chain — there's nothing to compare — but still shown in the
# table itself.

def _fmt_wm(v: float) -> str:
    return f"{v:.2f}x"


def _fmt_pct(v: float) -> str:
    return f"{v * 100:.1f}%"


def _fmt_fqs(v: float) -> str:
    return f"{v:.0f}"


def _trend_word(delta: float, threshold: float, up: str, down: str, flat: str) -> str:
    if delta > threshold:
        return up
    if delta < -threshold:
        return down
    return flat


def _generate_key_points(
    periods: List[Dict[str, Any]], overall: Optional[Dict[str, Any]]
) -> List[str]:
    scored = [p for p in periods if p.get("n_windows")]
    points: List[str] = []

    if len(scored) < 2:
        if scored:
            p = scored[0]
            points.append(
                f"Only one comparison period had enough rolling windows to score ({p['label']}: "
                f"Blended WM {_fmt_wm(p['blended_wm'])}, Success {_fmt_pct(p['success_pct'])}). "
                "Add another period, or widen this one, to compare consistency across time."
            )
        return points

    for prev, curr in zip(scored, scored[1:]):
        word = _trend_word(curr["blended_wm"] - prev["blended_wm"], 0.03, "rose", "fell", "held roughly steady")
        points.append(
            f"Blended WM {word} from {_fmt_wm(prev['blended_wm'])} in {prev['label']} to "
            f"{_fmt_wm(curr['blended_wm'])} in {curr['label']}."
        )

        word = _trend_word(
            curr["success_pct"] - prev["success_pct"], 0.02, "improved", "dropped", "stayed about the same"
        )
        points.append(
            f"Success rate (share of windows beating the category benchmark) {word}, from "
            f"{_fmt_pct(prev['success_pct'])} in {prev['label']} to {_fmt_pct(curr['success_pct'])} in {curr['label']}."
        )

        word = _trend_word(
            curr["cv"] - prev["cv"], 0.01,
            "became less consistent (higher CV)", "became more consistent (lower CV)", "stayed similarly consistent",
        )
        points.append(
            f"Outcome consistency {word}: CV moved from {_fmt_pct(prev['cv'])} in {prev['label']} to "
            f"{_fmt_pct(curr['cv'])} in {curr['label']}."
        )

        word = _trend_word(curr["p10_wm"] - prev["p10_wm"], 0.03, "improved", "worsened", "held steady")
        points.append(
            f"Worst-case outcome (P10 WM, the bottom 10% of windows) {word}, from "
            f"{_fmt_wm(prev['p10_wm'])} in {prev['label']} to {_fmt_wm(curr['p10_wm'])} in {curr['label']}."
        )

        word = _trend_word(curr["fqs"] - prev["fqs"], 2.0, "improved", "declined", "held steady")
        points.append(
            f"Overall Fund Quality Score (FQS) {word}, from {_fmt_fqs(prev['fqs'])} in {prev['label']} to "
            f"{_fmt_fqs(curr['fqs'])} in {curr['label']}."
        )

    if overall and overall.get("n_windows"):
        points.append(
            f"Across the full history scored, Blended WM is {_fmt_wm(overall['blended_wm'])} with a "
            f"{_fmt_pct(overall['success_pct'])} success rate and {_fmt_pct(overall['cv'])} CV — "
            "the baseline each period above is being compared against."
        )
    return points


# ── Shared per-fund computation ─────────────────────────────────────────────
# Used by both the single-fund /run endpoint and the multi-fund /rank
# endpoint below — one fund's NAV history in, its category + period-bucketed
# stats + overall baseline out. Kept fetch-free (nav_history is already in
# hand) so it can run inside a bounded-concurrency loop without any single
# fund's network fetch blocking another's CPU work.

def _compute_fund_periods(
    nav_history: List[Dict[str, Any]],
    meta: Dict[str, Any],
    start_d: date,
    end_d: date,
    bucket_specs: List[Tuple[date, date, Optional[str]]],
    window_years: int,
    mode: str,
) -> Optional[Dict[str, Any]]:
    """Returns {category, periods, overall} or None if this fund simply
    doesn't have enough NAV history in [start_d, end_d] for even one
    window_years-long window."""
    category = classify_fund(meta["scheme_name"], meta.get("scheme_category", ""))

    if mode == "sip":
        monthly = fqs_engine.extract_month_end_navs(nav_history, start_date=start_d, end_date=end_d)
        min_required = fqs_engine.min_wm_for(category, window_years) or fqs_engine._extrapolate_min_wm(
            category, window_years
        )
        windows = fqs_engine.compute_rolling_windows(monthly, window_years, min_required, category)
        if not windows:
            return None

        def scorer(ws: List[Any]) -> Optional[Dict[str, Any]]:
            res = fqs_engine.score_horizon(ws, window_years, category)
            return None if res is None else {k: getattr(res, k) for k in PERIOD_FIELDS}

    else:
        raw_navs = [{"date": r["date"].isoformat(), "nav": str(r["nav"])} for r in nav_history]
        nav_dates, nav_values = lumpsum_engine.extract_month_end_navs(raw_navs, start_d, end_d)
        min_wm_h = get_lumpsum_min_wm(category, window_years)
        cap = get_lumpsum_cap(category)
        equity = is_equity_category(category)
        windows = lumpsum_engine.compute_rolling_lumpsum_windows(nav_dates, nav_values, window_years, min_wm_h, category)
        if not windows:
            return None

        def scorer(ws: List[Any]) -> Optional[Dict[str, Any]]:
            stats = lumpsum_engine.compute_horizon_stats(ws, window_years, min_wm_h, cap)
            if stats is None:
                return None
            stats.fqs = lumpsum_engine.compute_horizon_fqs(stats, equity)
            return {k: getattr(stats, k) for k in PERIOD_FIELDS}

    periods = _build_periods(windows, bucket_specs, scorer)
    overall = scorer(windows)
    return {"category": category, "periods": periods, "overall": overall}


# ── Cross-period consistency metric ─────────────────────────────────────────
# How much does a fund's Blended WM swing between comparison periods? Lower
# = steadier performer across different stretches of history. Needs at least
# 2 scored (non-empty) periods to mean anything — a fund with 0 or 1 gets
# reported but left out of the ranking itself.

def _consistency_metrics(periods: List[Dict[str, Any]]) -> Dict[str, Any]:
    scored = [p for p in periods if p.get("n_windows")]
    if len(scored) < 2:
        return {
            "periods_scored": len(scored),
            "cross_period_cv": None,
            "wm_spread": None,
            "wm_min_period": None,
            "wm_max_period": None,
        }
    wms = [p["blended_wm"] for p in scored]
    mean_wm = statistics.mean(wms)
    std = statistics.pstdev(wms)
    return {
        "periods_scored": len(scored),
        "cross_period_cv": round(std / mean_wm, 6) if mean_wm else None,
        "wm_spread": round(max(wms) - min(wms), 6),
        "wm_min_period": min(scored, key=lambda p: p["blended_wm"])["label"],
        "wm_max_period": max(scored, key=lambda p: p["blended_wm"])["label"],
    }


class MultiRollingAnalysisRequest(BaseModel):
    start_date: str = Field(..., description="YYYY-MM-DD — overall lower bound on NAV data used")
    end_date: str = Field(..., description="YYYY-MM-DD — overall upper bound on NAV data used")
    window_years: int = Field(5, ge=MIN_WINDOW_YEARS, le=MAX_WINDOW_YEARS)
    mode: str = Field("sip", pattern="^(sip|lumpsum)$")
    buckets: Optional[List[BucketDef]] = None
    pool_size: int = Field(
        30, ge=0, le=MAX_POOL_SIZE,
        description="Auto-selected candidates (excl. Debt/Gold-Silver), sorted by NAV history depth. 0 = auto-pool disabled.",
    )
    scheme_codes: Optional[List[int]] = Field(
        None, description="Hand-picked scheme codes to include on top of (or instead of) the auto pool."
    )


class AddRankedFundRequest(BaseModel):
    scheme_code: int


def _rank_rows(rows: List[Dict[str, Any]]) -> None:
    """In-place sort: rankable funds (>=2 scored periods) first, most
    consistent (lowest cross_period_cv) at the top, ties broken by higher
    overall Blended WM; funds without enough periods to assess trail at the
    end, sorted by overall Blended WM so they're not simply alphabetical."""
    def key(r: Dict[str, Any]):
        cv = r.get("cross_period_cv")
        overall = r.get("overall") or {}
        overall_wm = overall.get("blended_wm")
        return (
            0 if cv is not None else 1,
            cv if cv is not None else 0.0,
            -(overall_wm if overall_wm is not None else 0.0),
            r.get("scheme_name", ""),
        )
    rows.sort(key=key)
    for i, r in enumerate(rows, start=1):
        r["rank"] = i


def _generate_multi_key_points(rows: List[Dict[str, Any]], window_years: int, n_periods: int) -> List[str]:
    rankable = [r for r in rows if r.get("cross_period_cv") is not None]
    points: List[str] = []
    points.append(
        f"{len(rows)} funds scored across {n_periods} comparison periods at a {window_years}Y rolling window; "
        f"{len(rankable)} had enough periods with data to assess cross-period consistency."
    )
    if len(rankable) >= 1:
        top = rankable[0]
        points.append(
            f"Most consistent: {top['scheme_name']} — Blended WM ranged from its {top['wm_min_period']} period to "
            f"its {top['wm_max_period']} period, a cross-period CV of {top['cross_period_cv'] * 100:.1f}%."
        )
    if len(rankable) >= 2:
        bottom = rankable[-1]
        points.append(
            f"Least consistent (of those rankable): {bottom['scheme_name']} — cross-period CV of "
            f"{bottom['cross_period_cv'] * 100:.1f}%, swinging between its {bottom['wm_min_period']} and "
            f"{bottom['wm_max_period']} periods."
        )
    unrankable = len(rows) - len(rankable)
    if unrankable:
        points.append(
            f"{unrankable} fund{'s' if unrankable != 1 else ''} had fewer than 2 periods with data — shown, "
            "but excluded from the consistency ranking itself."
        )
    return points


def build_router(db) -> APIRouter:
    router = APIRouter(prefix="/api/rolling-analysis", tags=["rolling-analysis"])

    @router.post("/run")
    async def run_rolling_analysis(req: RollingAnalysisRequest) -> Dict[str, Any]:
        start_d = _parse_iso_date(req.start_date, "start_date")
        end_d = _parse_iso_date(req.end_date, "end_date")
        if end_d <= start_d:
            raise HTTPException(status_code=422, detail="end_date must be after start_date")
        if (end_d - start_d).days < req.window_years * 365:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"A {req.window_years}-year rolling period needs at least {req.window_years} years "
                    f"between start_date and end_date."
                ),
            )

        bucket_specs = _resolve_buckets(req.buckets, start_d, end_d)

        try:
            loaded = await amfi_client.load_analysis_nav(req.scheme_code)
        except Exception as exc:
            logger.warning("rolling-analysis: NAV fetch failed for %s: %s", req.scheme_code, exc)
            raise HTTPException(status_code=502, detail="Could not fetch NAV history for this fund.")
        meta = loaded["meta"]
        nav_history = loaded["nav_history"]
        if not meta.get("scheme_name") or not nav_history:
            raise HTTPException(status_code=404, detail="No NAV history found for this fund.")

        computed = _compute_fund_periods(nav_history, meta, start_d, end_d, bucket_specs, req.window_years, req.mode)
        if computed is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Not enough NAV history for a {req.window_years}Y window between "
                    f"{req.start_date} and {req.end_date}."
                ),
            )
        category = computed["category"]
        periods = computed["periods"]
        overall = computed["overall"]

        key_points = _generate_key_points(periods, overall)
        analysis_id = uuid.uuid4().hex[:16]
        doc = {
            "_id": analysis_id,
            "analysis_id": analysis_id,
            "scheme_code": req.scheme_code,
            "scheme_name": meta["scheme_name"],
            "category": category,
            "mode": req.mode,
            "window_years": req.window_years,
            "start_date": start_d.isoformat(),
            "end_date": end_d.isoformat(),
            "custom_buckets": bool(req.buckets),
            "periods": periods,
            "overall": overall,
            "key_points": key_points,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await db.rolling_analysis_runs.update_one({"_id": analysis_id}, {"$set": doc}, upsert=True)
        return {k: v for k, v in doc.items() if k != "_id"}

    # ── Multi-fund consistency ranking ──────────────────────────────────────
    # Same period-bucketed analysis as /run, but across many funds at once —
    # an auto-selected pool, hand-picked scheme_codes, or both together —
    # ranked by how STABLE each fund's Blended WM is across periods rather
    # than inspected one at a time.

    async def _score_one_pool_fund(
        scheme_code: int, start_d: date, end_d: date, bucket_specs, window_years: int, mode: str,
    ) -> Optional[Dict[str, Any]]:
        try:
            loaded = await amfi_client.load_analysis_nav(scheme_code)
        except Exception as exc:
            logger.warning("rolling-analysis/rank: NAV fetch failed for %s: %s", scheme_code, exc)
            return None
        meta = loaded["meta"]
        nav_history = loaded["nav_history"]
        if not meta.get("scheme_name") or not nav_history:
            return None
        if "direct" in meta["scheme_name"].lower():
            return None

        computed = _compute_fund_periods(nav_history, meta, start_d, end_d, bucket_specs, window_years, mode)
        if computed is None:
            return None

        row: Dict[str, Any] = {
            "scheme_code": scheme_code,
            "scheme_name": meta["scheme_name"],
            "category": computed["category"],
            "periods": computed["periods"],
            "overall": computed["overall"],
            # Same per-fund key points single-fund /run generates — lets the
            # multi-fund PDF export show each fund's full detail section
            # (identical to the single-fund Rolling screen/PDF), not just its
            # ranking-table row.
            "key_points": _generate_key_points(computed["periods"], computed["overall"]),
        }
        row.update(_consistency_metrics(computed["periods"]))
        return row

    @router.post("/rank/run")
    async def run_multi_rolling_analysis(req: MultiRollingAnalysisRequest) -> Dict[str, Any]:
        start_d = _parse_iso_date(req.start_date, "start_date")
        end_d = _parse_iso_date(req.end_date, "end_date")
        if end_d <= start_d:
            raise HTTPException(status_code=422, detail="end_date must be after start_date")
        if (end_d - start_d).days < req.window_years * 365:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"A {req.window_years}-year rolling period needs at least {req.window_years} years "
                    f"between start_date and end_date."
                ),
            )
        hand_picked = list(dict.fromkeys(req.scheme_codes or []))  # dedupe, keep order
        if len(hand_picked) > MAX_HAND_PICKED:
            raise HTTPException(status_code=422, detail=f"At most {MAX_HAND_PICKED} hand-picked funds per request.")
        if req.pool_size == 0 and not hand_picked:
            raise HTTPException(
                status_code=422,
                detail="Set pool_size above 0, or provide scheme_codes — nothing to score otherwise.",
            )

        bucket_specs = _resolve_buckets(req.buckets, start_d, end_d)

        candidate_codes: List[int] = list(hand_picked)
        pool_codes: List[int] = []
        if req.pool_size > 0:
            cursor = db.fund_nav_meta.find(
                {
                    "earliest_nav_date": {"$lte": start_d.isoformat()},
                    "category": {"$nin": list(EXCLUDED_POOL_CATEGORIES)},
                },
                {"_id": 0, "scheme_code": 1},
            ).sort("nav_points", -1).limit(req.pool_size)
            pool_codes = [int(c["scheme_code"]) async for c in cursor if int(c["scheme_code"]) not in candidate_codes]
            candidate_codes += pool_codes

        if not candidate_codes:
            raise HTTPException(status_code=422, detail=f"No indexed funds have NAV history back to {start_d.isoformat()}.")
        candidate_codes = candidate_codes[:MAX_TOTAL_FUNDS]
        hand_picked_set = set(hand_picked)

        sem = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def _bounded(code: int) -> Optional[Dict[str, Any]]:
            async with sem:
                return await _score_one_pool_fund(code, start_d, end_d, bucket_specs, req.window_years, req.mode)

        scored = await asyncio.gather(*[_bounded(c) for c in candidate_codes])
        rows = [r for r in scored if r is not None]
        for r in rows:
            r["manually_added"] = r["scheme_code"] in hand_picked_set
        _rank_rows(rows)

        multi_id = uuid.uuid4().hex[:16]
        doc = {
            "_id": multi_id,
            "multi_id": multi_id,
            "start_date": start_d.isoformat(),
            "end_date": end_d.isoformat(),
            "window_years": req.window_years,
            "mode": req.mode,
            "custom_buckets": bool(req.buckets),
            "bucket_specs": [
                {"start_date": bs.isoformat(), "end_date": be.isoformat(), "label": lbl}
                for bs, be, lbl in bucket_specs
            ],
            "n_periods": len(bucket_specs),
            "candidate_count": len(candidate_codes),
            "scored_count": len(rows),
            "funds": rows,
            "key_points": _generate_multi_key_points(rows, req.window_years, len(bucket_specs)),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await db.rolling_multi_runs.update_one({"_id": multi_id}, {"$set": doc}, upsert=True)
        return {k: v for k, v in doc.items() if k != "_id"}

    @router.post("/rank/{multi_id}/add-fund")
    async def add_ranked_fund(multi_id: str, req: AddRankedFundRequest) -> Dict[str, Any]:
        """Hand-add one more fund (by scheme_code, e.g. picked via search) to
        an existing ranked run — same escape hatch Backtest had for funds the
        auto-pool left out."""
        doc = await db.rolling_multi_runs.find_one({"_id": multi_id})
        if not doc:
            raise HTTPException(status_code=404, detail="Ranking run not found")

        start_d = date.fromisoformat(doc["start_date"])
        end_d = date.fromisoformat(doc["end_date"])
        bucket_specs = [
            (date.fromisoformat(b["start_date"]), date.fromisoformat(b["end_date"]), b.get("label"))
            for b in doc["bucket_specs"]
        ]

        row = await _score_one_pool_fund(req.scheme_code, start_d, end_d, bucket_specs, doc["window_years"], doc["mode"])
        if row is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Not enough NAV history for this fund between {doc['start_date']} and "
                    f"{doc['end_date']} to compute a {doc['window_years']}Y window."
                ),
            )
        row["manually_added"] = True

        funds: List[Dict[str, Any]] = [f for f in (doc.get("funds") or []) if f["scheme_code"] != req.scheme_code]
        funds.append(row)
        _rank_rows(funds)

        key_points = _generate_multi_key_points(funds, doc["window_years"], doc.get("n_periods") or 0)
        await db.rolling_multi_runs.update_one(
            {"_id": multi_id},
            {"$set": {"funds": funds, "scored_count": len(funds), "key_points": key_points}},
        )
        return row

    @router.get("/rank/{multi_id}")
    async def get_ranked_run(multi_id: str) -> Dict[str, Any]:
        doc = await db.rolling_multi_runs.find_one({"_id": multi_id}, {"_id": 0})
        if not doc:
            raise HTTPException(status_code=404, detail="Ranking run not found")
        return doc

    @router.get("/rank/{multi_id}/export.pdf")
    async def export_ranked_run_pdf(multi_id: str) -> Response:
        doc = await db.rolling_multi_runs.find_one({"_id": multi_id}, {"_id": 0})
        if not doc:
            raise HTTPException(status_code=404, detail="Ranking run not found")

        content = build_rolling_multi_pdf(doc)
        fname = f"RollingConsistencyRanking_{doc['mode'].upper()}_{doc['window_years']}Y.pdf"
        return Response(
            content=content,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @router.get("/{analysis_id}")
    async def get_rolling_analysis(analysis_id: str) -> Dict[str, Any]:
        doc = await db.rolling_analysis_runs.find_one({"_id": analysis_id}, {"_id": 0})
        if not doc:
            raise HTTPException(status_code=404, detail="Rolling analysis run not found")
        return doc

    @router.get("/{analysis_id}/export.pdf")
    async def export_rolling_analysis_pdf(analysis_id: str) -> Response:
        doc = await db.rolling_analysis_runs.find_one({"_id": analysis_id}, {"_id": 0})
        if not doc:
            raise HTTPException(status_code=404, detail="Rolling analysis run not found")

        content = build_rolling_analysis_pdf(doc)
        fname = f"RollingAnalysis_{doc['scheme_code']}_{doc['mode'].upper()}_{doc['window_years']}Y.pdf"
        return Response(
            content=content,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    return router
