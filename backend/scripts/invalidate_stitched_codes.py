"""Invalidate only the fund_nav_meta rows whose history can now grow.

Why this exists — the cold-start chicken-and-egg
────────────────────────────────────────────────
`build_nav_meta_index` computes `history_years` from
`amfi_client.load_analysis_nav`, which stitches predecessors from two sources:

  1. fund_lineage.CURATED_SUCCESSORS   — always available (4 hand-verified pairs)
  2. amfi_client._DISCOVERED_SUCCESSORS — populated by server.refresh_successor_cache(),
     which reads db.merger_discovery_log for HIGH / VERY HIGH candidates

But `_run_merger_discovery` finds wound-up funds by QUERYING fund_nav_meta. So on
a cold database the order is forced:

    pass 1 reindex  (curated stitching only)
        -> merger discovery  (needs pass 1's index to find wound-up funds)
            -> pass 2 reindex  (now sees discovered predecessors too)

`build_nav_meta_index` is resumable — it SKIPS any scheme_code already present.
That's what makes pass 2 cheap, but it also means pass 2 would skip the very
codes that just gained a predecessor. This script deletes exactly those rows, so
the next reindex re-crawls only them instead of all ~8k schemes.

Usage:
    cd backend
    python scripts/invalidate_stitched_codes.py            # dry run, shows the list
    python scripts/invalidate_stitched_codes.py --apply    # actually delete
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv  # noqa: E402
from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

from fund_lineage import CURATED_SUCCESSORS  # noqa: E402

load_dotenv(BACKEND / ".env")

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")
NAV_META_COLLECTION = "fund_nav_meta"

ACCEPTED_TIERS = ("HIGH", "VERY HIGH")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="perform the deletion (default is a dry run)")
    args = ap.parse_args()

    client = AsyncIOMotorClient(MONGO_URL, serverSelectionTimeoutMS=5000)
    db = client[DB_NAME]
    try:
        await db.command("ping")
    except Exception as exc:
        print(f"ERROR: cannot reach Mongo at {MONGO_URL} — {exc}", file=sys.stderr)
        return 2

    # ── Successor codes that gain history from a stitched predecessor ─────────
    targets: dict[int, str] = {}

    for pred, succ in CURATED_SUCCESSORS.items():
        targets[int(succ)] = f"curated predecessor {pred}"

    log = await db.merger_discovery_log.find_one({"_id": "latest"}, {"_id": 0})
    if not log:
        print("WARNING: merger_discovery_log has no 'latest' document.")
        print("         Merger discovery has not run yet — only the 4 curated")
        print("         pairs will be invalidated. Run discovery first for the")
        print("         auto-discovered name-change/AMC-acquisition cases:")
        print("           curl -X POST http://localhost:8000/api/admin/merger-discovery")
    else:
        cands = log.get("candidates") or []
        accepted = [c for c in cands if c.get("tier") in ACCEPTED_TIERS]
        print(f"merger_discovery_log: status={log.get('status')} "
              f"candidates={len(cands)} accepted({'/'.join(ACCEPTED_TIERS)})={len(accepted)}")
        for c in accepted:
            wu, su = c.get("wu_code"), c.get("cand_code")
            if wu and su:
                targets[int(su)] = f"discovered predecessor {wu} (tier {c.get('tier')})"

    if not targets:
        print("\nNothing to invalidate.")
        return 0

    print(f"\n{len(targets)} successor code(s) gain stitched history:\n")
    present = 0
    for code, why in sorted(targets.items()):
        doc = await db[NAV_META_COLLECTION].find_one(
            {"scheme_code": code},
            {"_id": 0, "scheme_name": 1, "history_years": 1, "earliest_nav_date": 1,
             "predecessor_used": 1},
        )
        if doc:
            present += 1
            already = doc.get("predecessor_used") or []
            flag = "already stitched" if already else "NOT YET STITCHED"
            print(f"  {code}  {doc.get('history_years'):>6}y  "
                  f"from {doc.get('earliest_nav_date')}  [{flag}]")
            print(f"        {doc.get('scheme_name')}")
            print(f"        {why}")
        else:
            print(f"  {code}  (not in index yet — reindex will pick it up)")
            print(f"        {why}")

    print(f"\n{present} of {len(targets)} are currently in {NAV_META_COLLECTION}.")

    if not args.apply:
        print("\nDRY RUN — nothing deleted. Re-run with --apply to invalidate,")
        print("then POST /api/home/reindex to re-crawl just these codes.")
        return 0

    res = await db[NAV_META_COLLECTION].delete_many(
        {"scheme_code": {"$in": sorted(targets)}}
    )
    print(f"\nDeleted {res.deleted_count} row(s) from {NAV_META_COLLECTION}.")
    print("Now re-run the reindex — it will re-crawl only these codes:")
    print("  curl -X POST http://localhost:8000/api/home/reindex")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
