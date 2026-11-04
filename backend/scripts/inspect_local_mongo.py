"""Diagnostic: what is actually in the local Mongo?

`list_long_history_funds.py` found `test_database.fund_nav_meta` empty. Before
paying for a full ~15k-scheme reindex, check whether the index already exists
under a different database name (the code defaults disagree: server.py/.env say
`test_database`, export_mongo_to_excel.py defaults to `mf_analyst`).

Prints every database, every collection, its document count, and — for any
collection that looks like the NAV meta index or the fund analysis cache — a
sample document's keys plus the history_years distribution.

Usage:
    cd backend && python scripts/inspect_local_mongo.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv  # noqa: E402
from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

load_dotenv(BACKEND / ".env")

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")

SKIP_DBS = {"admin", "config", "local"}
INTERESTING = ("nav_meta", "fund_analysis", "fund", "lumpsum", "fqs", "predecessor", "merger")

HIST_BUCKETS = [(20, "20+"), (17, "17-20"), (14, "14-17"), (12, "12-14"),
                (10, "10-12"), (5, "5-10"), (0, "0-5")]


async def main() -> int:
    client = AsyncIOMotorClient(MONGO_URL, serverSelectionTimeoutMS=5000)
    try:
        await client.admin.command("ping")
    except Exception as exc:
        print(f"ERROR: cannot reach Mongo at {MONGO_URL} — {exc}", file=sys.stderr)
        return 2

    print(f"MONGO_URL   {MONGO_URL.rsplit('@', 1)[-1]}")
    print(f"DB_NAME     {DB_NAME}   (from .env)\n")

    db_names = [n for n in await client.list_database_names() if n not in SKIP_DBS]
    if not db_names:
        print("No user databases exist. This Mongo has never been populated.")
        return 0

    for name in db_names:
        db = client[name]
        colls = await db.list_collection_names()
        marker = "  <-- DB_NAME" if name == DB_NAME else ""
        print(f"── {name}{marker}")
        if not colls:
            print("     (no collections)")
        for coll in sorted(colls):
            n = await db[coll].count_documents({})
            print(f"     {n:>9,}  {coll}")
        print()

    # Deep-dive any non-empty collection that looks relevant, in any database.
    print("=" * 72)
    print("DETAIL — non-empty collections matching:", ", ".join(INTERESTING))
    print("=" * 72)
    found_any = False
    for name in db_names:
        db = client[name]
        for coll in sorted(await db.list_collection_names()):
            if not any(tok in coll.lower() for tok in INTERESTING):
                continue
            n = await db[coll].count_documents({})
            if n == 0:
                continue
            found_any = True
            print(f"\n{name}.{coll}  ({n:,} docs)")
            sample = await db[coll].find_one({})
            print("  keys:", sorted(sample.keys()) if sample else "—")

            if sample and "history_years" in sample:
                print("  history_years distribution:")
                prev_hi = None
                for lo, label in HIST_BUCKETS:
                    q = {"history_years": {"$gte": lo}}
                    if prev_hi is not None:
                        q["history_years"]["$lt"] = prev_hi
                    print(f"    {await db[coll].count_documents(q):>7,}  {label} years")
                    prev_hi = lo
                oldest = await db[coll].find_one(
                    {"earliest_nav_date": {"$ne": None}}, sort=[("earliest_nav_date", 1)]
                )
                if oldest:
                    print(f"  oldest first-NAV: {oldest.get('earliest_nav_date')} "
                          f"— {oldest.get('scheme_name')}")
                stitched = await db[coll].count_documents(
                    {"predecessor_used": {"$exists": True, "$ne": []}}
                )
                print(f"  predecessor-stitched docs: {stitched:,}")

    if not found_any:
        print("\nNothing relevant is populated anywhere. A reindex is required:")
        print("  uvicorn server:app --reload      # in backend/, then:")
        print("  curl -X POST http://localhost:8000/api/home/reindex")

    # Refresh logs tell us whether a crawl was ever started and how it ended.
    print("\n" + "=" * 72)
    print("REFRESH LOGS")
    print("=" * 72)
    for name in db_names:
        db = client[name]
        for coll in sorted(await db.list_collection_names()):
            if "refresh_log" not in coll and "refresh" not in coll:
                continue
            async for doc in db[coll].find({}):
                print(f"\n{name}.{coll}  _id={doc.get('_id')}")
                for k, v in doc.items():
                    if k == "_id":
                        continue
                    print(f"    {k}: {str(v)[:120]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
