"""Download full daily NAV history for the backtest universe.

RUN THIS ON THE DEVELOPER MACHINE. The analysis sandbox cannot reach
api.mfapi.in (the proxy returns 403), and MONGO_URL points at localhost.

    cd <repo>
    python -m backtest.download_navs

Resumable: a scheme already present in backtest_data/nav_store/ is skipped, so
the command can be interrupted and re-run freely. Roughly 2-4 hours on a first
full run depending on universe size and network.

    python -m backtest.download_navs --limit 200      # quick smoke test
    python -m backtest.download_navs --refresh        # re-fetch everything
    python -m backtest.download_navs --consolidate    # rebuild parquet only

Writes:
    backtest_data/universe.json        scheme metadata after filtering
    backtest_data/nav_store/<code>.json   {"meta": {...}, "nav": [[date, nav], ...]}
    backtest_data/nav_store.parquet    consolidated long-format NAV table
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from . import config as C

sys.path.insert(0, str(C.BACKEND))
from amfi_client import (  # noqa: E402
    is_idcw_scheme, fetch_fund_nav, meta_from_payload, load_analysis_nav,
)
from category_map import classify_fund, get_display_category  # noqa: E402
from fund_lineage import stitch as lineage_stitch  # noqa: E402

MFAPI_LIST = "https://api.mfapi.in/mf"
MFAPI_FUND = "https://api.mfapi.in/mf/{code}"


# ── raw (unsanitised) per-code fetch ─────────────────────────────────────────
# amfi_client.parse_nav_history() runs sanitize_nav_history() on the FULL
# series before any truncation exists — fine for a live request, wrong for a
# NAV store that point_in_time.truncate() must sanitise per cutoff (a splice
# detected in year 18 must not rebase year 1's NAVs before cutoff logic ever
# sees them, MODEL_AUDIT.md L1). So this store must hold genuinely raw NAV:
# parsed but never passed through sanitize_nav_history. load_analysis_nav()
# is still the single source of truth for WHICH codes belong in a fund's
# chain (curated + discovered, correlation/seam-gated) — only the NAV VALUES
# it returns are unsuitable here, not its chain-resolution decision.

def _parse_raw(payload: dict) -> list[dict]:
    """[{date, nav}] ascending, sanitisation deliberately NOT applied."""
    out = []
    for r in payload.get("data", []) or []:
        try:
            d = datetime.strptime(r["date"], "%d-%m-%Y").date()
            nav = float(r["nav"])
        except Exception:
            continue
        if nav > 0:
            out.append({"date": d, "nav": nav})
    out.sort(key=lambda x: x["date"])
    return out


async def fetch_stitched_raw_nav(scheme_code: int) -> tuple[dict, list[dict], dict]:
    """Raw (unsanitised) NAV for `scheme_code`, stitched across its lineage chain.

    Returns (meta_of_queried_code, stitched_raw_rows, provenance). Provenance
    carries predecessor_used / successor_used / stitch_chain / wound_up exactly
    as load_analysis_nav reported them, plus each own-code segment's own first
    NAV date, so the backtest report can show effective vs own-code inception
    without re-deriving it (Phase 9 of the validation report).
    """
    resolved = await load_analysis_nav(scheme_code)   # chain-resolution only; NAV discarded below
    own_payload = await fetch_fund_nav(scheme_code)
    meta = meta_from_payload(own_payload)
    own_raw = _parse_raw(own_payload)

    stitched = list(own_raw)
    own_code_first = own_raw[0]["date"].isoformat() if own_raw else None

    for pred_code in resolved.get("predecessor_used") or []:      # oldest-first
        pred_payload = await fetch_fund_nav(pred_code)
        pred_raw = _parse_raw(pred_payload)
        if pred_raw:
            stitched = lineage_stitch(pred_raw, stitched)

    for succ_code in resolved.get("stitch_chain") or []:          # forward, in order
        succ_payload = await fetch_fund_nav(succ_code)
        succ_raw = _parse_raw(succ_payload)
        if succ_raw:
            stitched = lineage_stitch(stitched, succ_raw)

    provenance = {
        "predecessor_used": resolved.get("predecessor_used") or [],
        "successor_used": resolved.get("successor_used"),
        "stitch_chain": resolved.get("stitch_chain") or [],
        "wound_up": resolved.get("wound_up", False),
        "own_code_first_nav_date": own_code_first,
        "effective_first_nav_date": stitched[0]["date"].isoformat() if stitched else None,
        "history_extended": bool(resolved.get("predecessor_used") or resolved.get("stitch_chain")),
    }
    return meta, stitched, provenance


# ── universe selection ───────────────────────────────────────────────────────

def _in_scope(scheme_name: str, scheme_category: str) -> tuple[bool, str]:
    """Apply the audited eligibility filters. Returns (keep, reason_if_dropped).

    Mirrors MODEL_AUDIT.md §4 plus decisions D2/D3. Deliberately keeps funds
    whose last NAV is old — wound-up funds must stay in the historical universe
    (survivorship, MODEL_AUDIT.md L7).
    """
    if "direct" in scheme_name.lower():
        return False, "direct_plan"           # amfi_client.py:156
    if is_idcw_scheme(scheme_name):
        return False, "idcw_variant"          # amfi_client.py:37-48
    internal = classify_fund(scheme_name, scheme_category)
    if internal in C.EXCLUDED_INTERNAL_CATEGORIES:
        return False, f"excluded_category:{internal}"   # D2
    broad, sub = get_display_category(scheme_category)
    if sub in C.CORE_SUBS:
        return True, ""
    if C.INCLUDE_BROAD_HYBRID and broad == "Hybrid":
        return True, ""
    return False, f"out_of_universe:{broad}/{sub}"      # D3


async def _fetch_json(session, url: str):
    for attempt in range(C.DOWNLOAD_RETRIES):
        try:
            async with session.get(url, timeout=C.DOWNLOAD_TIMEOUT_S) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
                if r.status == 404:
                    return None
        except Exception:
            pass
        await asyncio.sleep(1.5 * (attempt + 1))
    return None


async def build_universe(session, limit: int | None) -> dict:
    print("Fetching master scheme list …")
    listing = await _fetch_json(session, MFAPI_LIST)
    if not listing:
        raise SystemExit("Could not fetch the master scheme list from mfapi.in")
    print(f"  {len(listing):,} schemes returned")

    # The master list has no scheme_category, so scope is finalised after the
    # per-fund fetch (which carries meta.scheme_category). Here we only drop
    # what is decidable from the name.
    candidates, dropped = [], {}
    for row in listing:
        code = row.get("schemeCode")
        name = (row.get("schemeName") or "").strip()
        if not code or not name:
            dropped["malformed"] = dropped.get("malformed", 0) + 1
            continue
        if "direct" in name.lower():
            dropped["direct_plan"] = dropped.get("direct_plan", 0) + 1
            continue
        if is_idcw_scheme(name):
            dropped["idcw_variant"] = dropped.get("idcw_variant", 0) + 1
            continue
        candidates.append({"scheme_code": int(code), "scheme_name": name})

    print(f"  {len(candidates):,} candidates after name-level filters")
    for k, v in sorted(dropped.items(), key=lambda x: -x[1]):
        print(f"    dropped {v:>6,}  {k}")

    if limit:
        candidates = candidates[:limit]
        print(f"  --limit {limit}: truncated to {len(candidates):,}")
    return {"fetched_at": datetime.utcnow().isoformat(), "candidates": candidates}


# ── per-fund download ────────────────────────────────────────────────────────

async def _download_one(session, sem, entry: dict, stats: dict) -> None:
    code = entry["scheme_code"]
    path = C.NAV_STORE / f"{code}.json"
    async with sem:
        try:
            meta, stitched, prov = await fetch_stitched_raw_nav(code)
        except Exception as exc:
            logging.getLogger("backtest.download_navs").warning(
                "fetch/stitch failed for %s: %s", code, exc
            )
            stats["failed"] += 1
            return

    if not stitched:
        stats["failed"] += 1
        return

    name = (meta.get("scheme_name") or entry["scheme_name"]).strip()
    scat = (meta.get("scheme_category") or "").strip()

    keep, reason = _in_scope(name, scat)
    if not keep:
        stats["out_of_scope"] += 1
        stats.setdefault("scope_reasons", {})
        stats["scope_reasons"][reason] = stats["scope_reasons"].get(reason, 0) + 1
        return

    rows = [[r["date"].isoformat(), r["nav"]] for r in stitched]
    if not rows:
        stats["empty"] += 1
        return

    path.write_text(json.dumps({
        "scheme_code": code,
        "scheme_name": name,
        "scheme_category": scat,
        "fund_house": meta.get("fund_house", ""),
        "scheme_type": meta.get("scheme_type", ""),
        "internal_category": classify_fund(name, scat),
        "nav": rows,
        # Lineage provenance — NOT read by data_loader.load_store() (extra keys
        # are ignored), kept here so the client report can cite effective vs
        # own-code inception without re-resolving the chain.
        "lineage": prov,
    }))
    if prov.get("history_extended"):
        stats["stitched"] = stats.get("stitched", 0) + 1
    stats["saved"] += 1


async def run(limit: int | None, refresh: bool) -> None:
    try:
        import aiohttp
    except ImportError:
        raise SystemExit("pip install aiohttp")

    C.NAV_STORE.mkdir(parents=True, exist_ok=True)
    universe = None

    async with aiohttp.ClientSession() as session:
        universe = await build_universe(session, limit)
        C.UNIVERSE_FILE.write_text(json.dumps(universe, indent=2))

        todo = universe["candidates"]
        if not refresh:
            have = {int(p.stem) for p in C.NAV_STORE.glob("*.json")}
            todo = [e for e in todo if e["scheme_code"] not in have]
            print(f"\n{len(have):,} already downloaded, {len(todo):,} to fetch")

        stats = {"saved": 0, "failed": 0, "empty": 0, "out_of_scope": 0}
        sem = asyncio.Semaphore(C.DOWNLOAD_CONCURRENCY)
        t0 = time.time()

        for i in range(0, len(todo), 200):
            batch = todo[i:i + 200]
            await asyncio.gather(*(_download_one(session, sem, e, stats) for e in batch))
            done = min(i + 200, len(todo))
            rate = done / max(time.time() - t0, 1e-9)
            eta = (len(todo) - done) / max(rate, 1e-9)
            print(f"  {done:>6,}/{len(todo):,}  saved={stats['saved']:,} "
                  f"scope={stats['out_of_scope']:,} fail={stats['failed']:,} "
                  f"| {rate:.1f}/s  ETA {eta/60:.0f}m", flush=True)

    print(f"\nDone in {(time.time()-t0)/60:.1f} min")
    for k, v in stats.items():
        if k != "scope_reasons":
            print(f"  {k:<14} {v:,}")
    for k, v in sorted(stats.get("scope_reasons", {}).items(), key=lambda x: -x[1])[:12]:
        print(f"    out_of_scope  {v:>6,}  {k}")

    consolidate()


# ── populate nav_store from a scripts/build_long_history_universe.py output ──
# That script already crawls the whole equity-scope universe correctly
# (stitched, via amfi_client.load_analysis_nav) and caches every raw payload
# it touches at backtest_data/nav_cache/<code>.json. Re-pointing
# amfi_client.fetch_fund_nav at that same cache means this path costs zero
# extra network calls for anything the crawl already fetched — only genuinely
# new codes (there shouldn't be any, since fetch_stitched_raw_nav's own
# predecessor/successor lookups use the same codes the crawl already walked)
# hit the network.

def _install_read_through_cache(cache_dir: Path) -> None:
    import amfi_client as _ac

    real_fetch = _ac.fetch_fund_nav

    async def cached_fetch(scheme_code: int):
        path = cache_dir / f"{int(scheme_code)}.json"
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:
                path.unlink(missing_ok=True)
        payload = await real_fetch(scheme_code)
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload))
        except Exception:
            pass
        return payload

    _ac.fetch_fund_nav = cached_fetch


async def run_from_universe(
    universe_json: Path, cache_dir: Path, limit: int | None, refresh: bool,
) -> None:
    C.NAV_STORE.mkdir(parents=True, exist_ok=True)
    _install_read_through_cache(cache_dir)

    payload = json.loads(universe_json.read_text())
    funds = payload.get("funds") or []
    print(f"{len(funds):,} funds in {universe_json.name} "
          f"(criteria: {payload.get('criteria')})")

    candidates = [{"scheme_code": f["scheme_code"], "scheme_name": f["scheme_name"]}
                  for f in funds]
    if limit:
        candidates = candidates[:limit]

    if not refresh:
        have = {int(p.stem) for p in C.NAV_STORE.glob("*.json")}
        candidates = [e for e in candidates if e["scheme_code"] not in have]
        print(f"{len(have):,} already in nav_store, {len(candidates):,} to build")

    stats = {"saved": 0, "failed": 0, "empty": 0, "out_of_scope": 0}
    sem = asyncio.Semaphore(C.DOWNLOAD_CONCURRENCY)
    t0 = time.time()

    for i in range(0, len(candidates), 100):
        batch = candidates[i:i + 100]
        await asyncio.gather(*(_download_one(None, sem, e, stats) for e in batch))
        done = min(i + 100, len(candidates))
        rate = done / max(time.time() - t0, 1e-9)
        print(f"  {done:>6,}/{len(candidates):,}  saved={stats['saved']:,} "
              f"stitched={stats.get('stitched', 0):,} scope={stats['out_of_scope']:,} "
              f"fail={stats['failed']:,} | {rate:.1f}/s", flush=True)

    print(f"\nDone in {(time.time()-t0)/60:.1f} min")
    for k, v in stats.items():
        if k != "scope_reasons":
            print(f"  {k:<14} {v:,}")

    consolidate()


# ── consolidation ────────────────────────────────────────────────────────────

def consolidate() -> None:
    """Collapse the per-fund JSON store into one parquet for fast loading."""
    try:
        import pandas as pd
    except ImportError:
        print("pandas not installed — skipping parquet consolidation")
        return

    files = sorted(C.NAV_STORE.glob("*.json"))
    if not files:
        print("nav_store is empty — nothing to consolidate")
        return

    print(f"\nConsolidating {len(files):,} funds → {C.NAV_PARQUET.name} …")
    frames, meta_rows = [], []
    for p in files:
        d = json.loads(p.read_text())
        nav = d["nav"]
        frames.append(pd.DataFrame({
            "scheme_code": d["scheme_code"],
            "date": [r[0] for r in nav],
            "nav": [r[1] for r in nav],
        }))
        meta_rows.append({
            "scheme_code": d["scheme_code"],
            "scheme_name": d["scheme_name"],
            "scheme_category": d["scheme_category"],
            "internal_category": d["internal_category"],
            "fund_house": d.get("fund_house", ""),
            "first_nav": nav[0][0],
            "last_nav": nav[-1][0],
            "n_points": len(nav),
        })

    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    df.sort_values(["scheme_code", "date"], inplace=True)
    df.to_parquet(C.NAV_PARQUET, index=False)

    meta = pd.DataFrame(meta_rows)
    meta.to_parquet(C.DATA_DIR / "universe_meta.parquet", index=False)

    print(f"  {len(df):,} NAV rows across {meta['scheme_code'].nunique():,} funds")
    print(f"  earliest {df['date'].min().date()}  latest {df['date'].max().date()}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None,
                    help="only consider the first N schemes (smoke test)")
    ap.add_argument("--refresh", action="store_true",
                    help="re-download funds already present")
    ap.add_argument("--consolidate", action="store_true",
                    help="rebuild the parquet from the existing store and exit")
    ap.add_argument("--from-universe-json", default=None,
                    help="path to scripts/build_long_history_universe.py's "
                         "long_history_universe.json — populate nav_store from "
                         "exactly that (already lineage-resolved) fund list "
                         "instead of re-crawling the whole mfapi.in scheme list")
    ap.add_argument("--cache-dir", default=None,
                    help="raw-payload disk cache to read through "
                         "(default: backtest_data/nav_cache, shared with "
                         "build_long_history_universe.py)")
    a = ap.parse_args()

    if a.consolidate:
        consolidate()
        return
    if a.from_universe_json:
        cache_dir = Path(a.cache_dir) if a.cache_dir else C.DATA_DIR / "nav_cache"
        asyncio.run(run_from_universe(
            Path(a.from_universe_json), cache_dir, a.limit, a.refresh
        ))
        return
    asyncio.run(run(a.limit, a.refresh))


if __name__ == "__main__":
    main()
