"""Discover fund merger pairs for MERGER_MAP in amfi_client.py.

Usage:
    # Requires fund_nav_meta to be populated (run reindex first)
    python scripts/merger_discovery.py --mongo mongodb://localhost:27017 --db mf_analyst

What it does:
1. Queries fund_nav_meta for "wound-up" funds — any fund whose last_nav_date is
   > 180 days before today.  These are candidates for true mergers.
2. For each wound-up fund, searches for active funds from the SAME fund house
   whose first_nav_date is within ±180 days of the wound-up fund's last_nav_date,
   AND whose name shares keywords with the wound-up fund.
3. Fetches both funds' NAV histories, computes return correlation over the
   transition period.  High correlation (≥ 0.90) in 20+ overlapping trading days
   is strong evidence of a genuine merger.
4. Prints ranked candidate pairs ordered by confidence score, with ready-to-paste
   MERGER_MAP entries.

After reviewing output, add confirmed pairs to amfi_client.py MERGER_MAP.
"""

import argparse
import asyncio
import math
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx
from motor.motor_asyncio import AsyncIOMotorClient

MFAPI_BASE        = "https://api.mfapi.in/mf"
WOUND_UP_DAYS     = 180     # last NAV older than this → wound-up candidate
TRANSITION_WINDOW = 180     # ±days around the wound-up date to look for successor's start
MIN_OVERLAP_DAYS  = 10      # minimum overlapping trading days to compute correlation
MIN_CORR          = 0.85    # minimum Pearson r to consider a match
MIN_KEYWORD_JACCARD = 0.15  # minimum name similarity


# ── Name helpers ──────────────────────────────────────────────────────────────

STOPWORDS = {
    "fund", "mutual", "scheme", "direct", "regular", "growth", "dividend",
    "idcw", "plan", "option", "reinvestment", "payout", "bonus", "the", "of",
    "and", "&", "india", "limited", "ltd", "-", "–",
}


def name_keywords(name: str) -> Set[str]:
    tokens = name.lower().replace("-", " ").replace("&", " ").split()
    return {t for t in tokens if t not in STOPWORDS and len(t) > 2}


def jaccard(a: str, b: str) -> float:
    ka, kb = name_keywords(a), name_keywords(b)
    if not ka or not kb:
        return 0.0
    return len(ka & kb) / len(ka | kb)


# ── NAV helpers ───────────────────────────────────────────────────────────────

async def fetch_nav(client: httpx.AsyncClient, code: int) -> List[Dict[str, Any]]:
    """Return sorted list of {date: date, nav: float}."""
    try:
        r = await client.get(f"{MFAPI_BASE}/{code}", timeout=30.0)
        r.raise_for_status()
        raw = r.json().get("data", []) or []
        out = []
        for row in raw:
            try:
                d = datetime.strptime(row["date"], "%d-%m-%Y").date()
                nav = float(row["nav"])
                if nav > 0:
                    out.append({"date": d, "nav": nav})
            except Exception:
                pass
        out.sort(key=lambda x: x["date"])
        return out
    except Exception as exc:
        print(f"  [WARN] fetch_nav({code}) failed: {exc}", file=sys.stderr)
        return []


def nav_to_series(history: List[Dict[str, Any]]) -> Dict[date, float]:
    return {r["date"]: r["nav"] for r in history}


def daily_returns(series: Dict[date, float]) -> Dict[date, float]:
    dates = sorted(series)
    out = {}
    for i in range(1, len(dates)):
        d0, d1 = dates[i - 1], dates[i]
        if series[d0] > 0:
            out[d1] = (series[d1] - series[d0]) / series[d0]
    return out


def pearson(xs: List[float], ys: List[float]) -> float:
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


def return_correlation(
    series_a: Dict[date, float],
    series_b: Dict[date, float],
    overlap_start: Optional[date] = None,
    overlap_end:   Optional[date] = None,
) -> Tuple[float, int]:
    """Pearson r of daily returns in the overlapping date range."""
    ret_a = daily_returns(series_a)
    ret_b = daily_returns(series_b)
    common = sorted(ret_a.keys() & ret_b.keys())
    if overlap_start:
        common = [d for d in common if d >= overlap_start]
    if overlap_end:
        common = [d for d in common if d <= overlap_end]
    if len(common) < MIN_OVERLAP_DAYS:
        return 0.0, len(common)
    xs = [ret_a[d] for d in common]
    ys = [ret_b[d] for d in common]
    return pearson(xs, ys), len(common)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(mongo_url: str, db_name: str) -> None:
    today = date.today()
    cutoff = today - timedelta(days=WOUND_UP_DAYS)

    client_db = AsyncIOMotorClient(mongo_url)
    db = client_db[db_name]

    print("Querying fund_nav_meta for wound-up funds ...", file=sys.stderr)
    cursor = db.fund_nav_meta.find(
        {"latest_nav_date": {"$lt": cutoff.isoformat()}},
        {"scheme_code": 1, "scheme_name": 1, "fund_house": 1, "latest_nav_date": 1, "earliest_nav_date": 1},
    )
    wound_up = [doc async for doc in cursor]
    print(f"  {len(wound_up)} wound-up candidates", file=sys.stderr)

    if not wound_up:
        print("\nNo wound-up funds found in fund_nav_meta.")
        print("Make sure fund_nav_meta has 'latest_nav_date' and 'earliest_nav_date' fields.")
        print("These are written by nav_meta_indexer.py — run POST /api/home/reindex first.")
        return

    # Group active funds (last_nav_date recent) by fund house
    print("Loading active funds by fund house ...", file=sys.stderr)
    active_cursor = db.fund_nav_meta.find(
        {"latest_nav_date": {"$gte": (today - timedelta(days=60)).isoformat()}},
        {"scheme_code": 1, "scheme_name": 1, "fund_house": 1, "earliest_nav_date": 1},
    )
    active_by_house: Dict[str, List[Dict]] = defaultdict(list)
    async for doc in active_cursor:
        house = (doc.get("fund_house") or "").strip()
        if house:
            active_by_house[house].append(doc)
    print(f"  {sum(len(v) for v in active_by_house.values())} active funds across {len(active_by_house)} houses", file=sys.stderr)

    # For each wound-up fund, find candidates from the same house
    sem = asyncio.Semaphore(6)
    http = httpx.AsyncClient(timeout=30.0)

    candidates = []

    async def check_pair(wu: Dict, cand: Dict) -> Optional[Dict]:
        wu_code   = wu["scheme_code"]
        cand_code = cand["scheme_code"]
        wu_name   = wu.get("scheme_name", "")
        cand_name = cand.get("scheme_name", "")

        sim = jaccard(wu_name, cand_name)
        if sim < MIN_KEYWORD_JACCARD:
            return None

        wu_last    = date.fromisoformat(wu["latest_nav_date"])    if wu.get("latest_nav_date")    else None
        cand_first = date.fromisoformat(cand["earliest_nav_date"]) if cand.get("earliest_nav_date") else None

        if not wu_last or not cand_first:
            return None

        # Successor's inception must be within ±TRANSITION_WINDOW days of wound-up's last NAV
        delta = abs((cand_first - wu_last).days)
        if delta > TRANSITION_WINDOW:
            return None

        # Fetch both NAV histories and compute correlation in transition window
        async with sem:
            wu_nav   = await fetch_nav(http, wu_code)
            cand_nav = await fetch_nav(http, cand_code)

        wu_series   = nav_to_series(wu_nav)
        cand_series = nav_to_series(cand_nav)

        # Look for overlap in the transition period (±90 days around wu_last)
        window_start = wu_last - timedelta(days=90)
        window_end   = wu_last + timedelta(days=90)

        corr, overlap = return_correlation(wu_series, cand_series, window_start, window_end)

        # History length bonus — more history = more value from stitching
        wu_years = 0.0
        if wu_nav:
            wu_years = (wu_nav[-1]["date"] - wu_nav[0]["date"]).days / 365.25

        # Confidence: weighted blend of correlation, similarity, and overlap days
        if overlap >= MIN_OVERLAP_DAYS:
            confidence = 0.60 * corr + 0.25 * sim + 0.15 * min(1.0, overlap / 60)
        elif corr == 0.0:
            # No overlap — use similarity + timing only
            timing_score = max(0.0, 1.0 - delta / TRANSITION_WINDOW)
            confidence   = 0.50 * sim + 0.50 * timing_score
        else:
            confidence = 0.0

        return {
            "wu_code":      wu_code,
            "wu_name":      wu_name,
            "wu_last":      wu_last.isoformat(),
            "wu_years":     round(wu_years, 1),
            "cand_code":    cand_code,
            "cand_name":    cand_name,
            "cand_first":   cand_first.isoformat(),
            "transition_days": delta,
            "overlap_days": overlap,
            "correlation":  round(corr, 4),
            "name_sim":     round(sim, 3),
            "confidence":   round(confidence, 4),
            "fund_house":   wu.get("fund_house", ""),
        }

    print(f"\nChecking {len(wound_up)} wound-up funds for merger partners ...", file=sys.stderr)

    tasks = []
    for wu in wound_up:
        house = (wu.get("fund_house") or "").strip()
        actives = active_by_house.get(house, [])
        for cand in actives:
            if cand["scheme_code"] != wu["scheme_code"]:
                tasks.append(check_pair(wu, cand))

    print(f"  {len(tasks)} pairs to evaluate", file=sys.stderr)

    results = await asyncio.gather(*tasks)
    await http.aclose()
    client_db.close()

    raw = [r for r in results if r and r["confidence"] >= 0.30]
    raw.sort(key=lambda x: -x["confidence"])

    # De-duplicate: keep highest-confidence match per wound-up fund
    seen_wu: Set[int] = set()
    deduped = []
    for r in raw:
        if r["wu_code"] not in seen_wu:
            seen_wu.add(r["wu_code"])
            deduped.append(r)

    print(f"\n{'='*80}")
    print(f"MERGER CANDIDATES  ({len(deduped)} unique wound-up funds matched)")
    print(f"{'='*80}")
    print()
    print("CONFIDENCE TIERS:")
    print("  ≥ 0.80  → Very high confidence — safe to add without further verification")
    print("  0.60-0.79 → High — verify with AMFI circular or SEBI order")
    print("  0.40-0.59 → Medium — manual review required")
    print("  < 0.40   → Low — do not add without strong external confirmation")
    print()

    if not deduped:
        print("No confident merger pairs found.")
        print("This could mean:")
        print("  • fund_nav_meta is missing last_nav_date/first_nav_date/fund_house fields")
        print("  • All wound-up funds are ETFs/index funds that were simply discontinued")
        print("  • Mergers happened between fund houses (cross-house mergers are rare)")
    else:
        for i, c in enumerate(deduped, 1):
            tier = (
                "VERY HIGH" if c["confidence"] >= 0.80 else
                "HIGH"      if c["confidence"] >= 0.60 else
                "MEDIUM"    if c["confidence"] >= 0.40 else
                "LOW"
            )
            print(f"[{i}]  Confidence: {c['confidence']:.3f}  [{tier}]  |  {c['fund_house']}")
            print(f"  WOUND-UP  {c['wu_code']:>7}  {c['wu_name']}")
            print(f"            Last NAV: {c['wu_last']}  ({c['wu_years']}Y of history)")
            print(f"  SUCCESSOR {c['cand_code']:>7}  {c['cand_name']}")
            print(f"            First NAV: {c['cand_first']}  (gap: {c['transition_days']} days)")
            print(f"  Correlation: {c['correlation']:.4f}  (over {c['overlap_days']} overlapping days)")
            print(f"  Name similarity: {c['name_sim']:.3f}")
            print()
            print(f"  # Add to amfi_client.py MERGER_MAP after verification:")
            print(f"  {c['wu_code']}: {c['cand_code']},  # {c['wu_name'][:60]} → {c['cand_name'][:40]}")
            print()

    print()
    print("─" * 80)
    print("MERGER_MAP snippet (HIGH+ confidence only) — paste into amfi_client.py:")
    print()
    high_conf = [c for c in deduped if c["confidence"] >= 0.60]
    if high_conf:
        print("MERGER_MAP: Dict[int, int] = {")
        for c in high_conf:
            print(f"    {c['wu_code']}: {c['cand_code']},  "
                  f"# {c['wu_name'][:55]} → {c['cand_name'][:35]}")
        print("}")
    else:
        print("(No HIGH+ confidence pairs found)")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Find fund merger pairs for MERGER_MAP")
    parser.add_argument("--mongo", default="mongodb://localhost:27017", help="MongoDB URL")
    parser.add_argument("--db",    default="mf_analyst",              help="Database name")
    args = parser.parse_args()
    asyncio.run(main(args.mongo, args.db))
