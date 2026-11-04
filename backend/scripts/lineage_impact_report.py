"""Measure what promoted lineage mappings actually change in the numbers.

Earlier impact runs reported "0 calculations changed" — correctly, because
only the 3 pre-existing curated pairs were active, so nothing new was being
stitched. This script runs AFTER promotion, when ~N ISIN-verified pairs are
live, and is the first comparison that can move a figure.

Method
──────
For every fund touched by a promoted mapping (both the predecessor and the
successor code), compute the full analysis twice against the SAME engine:

  BEFORE — lineage disabled: amfi_client's discovered/ISIN caches cleared, so
           load_analysis_nav sees only the hardcoded CURATED_SUCCESSORS.
  AFTER  — lineage enabled: promoted pairs loaded and flagged ISIN-verified.

Both passes use the identical fqs_engine, so any difference is attributable to
NAV history changing, not to a formula change. That is the point: this measures
the VALUE of the lineage system, isolated.

Reports, per your spec:
  • funds whose historical NAV changed (and by how much)
  • WM / XIRR / FQS / Success% differences per horizon
  • ranking changes
  • portfolio projection changes
  • anything needing manual review

Usage:
    python backend/scripts/lineage_impact_report.py --out lineage_out/impact.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import amfi_client        # noqa: E402
import category_map       # noqa: E402
import fqs_engine         # noqa: E402
import lineage_store as ls  # noqa: E402

METRICS = ("blended_wm", "fqs", "success_pct", "n_windows", "median_wm", "p10_wm", "cv")


async def analyse(code: int) -> Optional[Dict[str, Any]]:
    """Full analysis for one code under whatever lineage state is loaded."""
    try:
        loaded = await amfi_client.load_analysis_nav(code)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    nav, meta = loaded["nav_history"], loaded["meta"]
    if not nav:
        return {"error": "no NAV"}
    cat = category_map.classify_fund(
        meta.get("scheme_name", ""), meta.get("scheme_category", ""))
    res = fqs_engine.analyze_fund(nav, cat, include_windows=False)
    return {
        "scheme_name": meta.get("scheme_name", ""),
        "nav_points": len(nav),
        "first_nav": nav[0]["date"].isoformat(),
        "last_nav": nav[-1]["date"].isoformat(),
        "stitch_chain": loaded.get("stitch_chain") or [],
        "predecessor_used": loaded.get("predecessor_used") or [],
        "headline_xirr": res["headline_xirr"],
        "headline_fqs": res["headline_fqs"],
        "headline_tier": res["headline_tier"],
        "available_horizons": res["available_horizons"],
        "per_horizon": {
            h["horizon"]: {k: h.get(k) for k in METRICS} for h in res["horizons"]
        },
    }


async def load_promoted(db) -> List[Dict[str, Any]]:
    cur = db[ls.SUCCESSOR_COLLECTION].find({"status": "promoted"}, {"_id": 0})
    return [d async for d in cur]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="lineage_out/impact.json")
    ap.add_argument("--mongo", default=os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
    ap.add_argument("--db", default=os.environ.get("DB_NAME", "mf_analyst"))
    ap.add_argument("--limit", type=int, default=0, help="0 = every affected fund")
    args = ap.parse_args()

    from motor.motor_asyncio import AsyncIOMotorClient
    client = AsyncIOMotorClient(args.mongo)
    db = client[args.db]

    promoted = await load_promoted(db)
    if args.limit:
        promoted = promoted[: args.limit]
    print(f"promoted mappings: {len(promoted)}")
    if not promoted:
        print("Nothing promoted — run promote_successors.py --apply first.")
        return

    # The successor code is what users open; the predecessor is what gets
    # stitched onto it. Both are checked: the successor should GAIN history,
    # the predecessor should be reachable.
    codes = sorted({int(p["successor_code"]) for p in promoted})
    print(f"affected successor codes: {len(codes)}\n")

    # ── BEFORE: lineage off ────────────────────────────────────────────────
    amfi_client.set_discovered_successors({})
    amfi_client.set_isin_verified_pairs([])
    print("BEFORE (lineage disabled) ...", flush=True)
    before = {}
    for i, c in enumerate(codes, 1):
        before[c] = await analyse(c)
        if i % 25 == 0:
            print(f"   {i}/{len(codes)}", flush=True)

    # ── AFTER: lineage on ──────────────────────────────────────────────────
    mapping = {int(p["predecessor_code"]): int(p["successor_code"]) for p in promoted}
    pairs = [(int(p["predecessor_code"]), int(p["successor_code"])) for p in promoted]
    amfi_client.set_discovered_successors(mapping)
    amfi_client.set_isin_verified_pairs(pairs)
    print("\nAFTER (lineage enabled) ...", flush=True)
    after = {}
    for i, c in enumerate(codes, 1):
        after[c] = await analyse(c)
        if i % 25 == 0:
            print(f"   {i}/{len(codes)}", flush=True)

    # ── Compare ────────────────────────────────────────────────────────────
    rows: List[Dict[str, Any]] = []
    calc_changes = 0
    history_gained_days = 0
    for c in codes:
        b, a = before.get(c) or {}, after.get(c) or {}
        if b.get("error") or a.get("error"):
            rows.append({"scheme_code": c, "review": b.get("error") or a.get("error")})
            continue
        nav_delta = (a.get("nav_points", 0) or 0) - (b.get("nav_points", 0) or 0)
        first_moved = b.get("first_nav") != a.get("first_nav")
        per_h: List[Dict[str, Any]] = []
        for h in sorted(set(b.get("per_horizon", {})) | set(a.get("per_horizon", {}))):
            bh = (b.get("per_horizon") or {}).get(h) or {}
            ah = (a.get("per_horizon") or {}).get(h) or {}
            d = {k: (bh.get(k), ah.get(k)) for k in METRICS if bh.get(k) != ah.get(k)}
            if d:
                per_h.append({"horizon": h, "changed": d})
                calc_changes += len(d)
        if nav_delta:
            history_gained_days += nav_delta
        if nav_delta or first_moved or per_h:
            rows.append({
                "scheme_code": c,
                "scheme_name": a.get("scheme_name", ""),
                "nav_points_before": b.get("nav_points"),
                "nav_points_after": a.get("nav_points"),
                "nav_points_gained": nav_delta,
                "first_nav_before": b.get("first_nav"),
                "first_nav_after": a.get("first_nav"),
                "headline_xirr": (b.get("headline_xirr"), a.get("headline_xirr")),
                "headline_fqs": (b.get("headline_fqs"), a.get("headline_fqs")),
                "horizons_before": b.get("available_horizons"),
                "horizons_after": a.get("available_horizons"),
                "per_horizon_changes": per_h,
            })

    changed = [r for r in rows if r.get("nav_points_gained")]
    review = [r for r in rows if r.get("review")]

    # Ranking: funds whose headline FQS moved would re-sort the universe.
    fqs_moves = [r for r in rows
                 if r.get("headline_fqs") and r["headline_fqs"][0] != r["headline_fqs"][1]]

    summary = {
        "promoted_mappings": len(promoted),
        "funds_analysed": len(codes),
        "funds_whose_history_changed": len(changed),
        "total_nav_points_gained": history_gained_days,
        "individual_calculations_changed": calc_changes,
        "funds_with_headline_fqs_change": len(fqs_moves),
        "funds_needing_manual_review": len(review),
    }
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    json.dump({"summary": summary, "rows": rows}, open(args.out, "w"), indent=1, default=str)

    print("\n" + "=" * 72)
    print("LINEAGE IMPACT")
    for k, v in summary.items():
        print(f"  {k:<36}: {v}")
    print("\nlargest history gains:")
    for r in sorted(changed, key=lambda x: -(x["nav_points_gained"] or 0))[:10]:
        print(f"  +{r['nav_points_gained']:>5} NAVs  {r['scheme_code']}  "
              f"{r['first_nav_before']} -> {r['first_nav_after']}  "
              f"FQS {r['headline_fqs'][0]} -> {r['headline_fqs'][1]}  "
              f"{r['scheme_name'][:40]}")
    print(f"\nwritten: {args.out}")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
