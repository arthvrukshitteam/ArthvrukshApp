"""CLI wrapper for building the fund lineage database and emitting it as a
reviewable artifact.

The actual derivation logic lives in backend/lineage_build.py (importable,
so server.py can also trigger a real build in-process — see
POST /api/admin/build-lineage). This script is now just the file-writing +
argument-parsing shell around it; behaviour is unchanged from before that
split.

Usage
─────
    python backend/scripts/build_lineage.py --out ./lineage_out
    python backend/scripts/build_lineage.py --out ./lineage_out --quick   # 6 captures
    python backend/scripts/build_lineage.py --out ./lineage_out --persist # + write Mongo

Outputs (all in --out):
    lineage_actions.csv   one row per corporate action  ← the review unit
    lineage_funds.csv     one row per scheme code       ← identity spine
    lineage.json          full records, machine-readable
    merger_candidates.csv proposed successor pairs, UNVERIFIED
    summary.json          the deliverable counts
    sources.json          exactly which URLs/captures produced this run

Provenance, stated per record
─────────────────────────────
Every action row carries `source`, `confidence_score` and `discovery_method`.
Nothing in this pipeline is anonymous: a reviewer can take any row and trace it
back to a specific AMFI file or archive capture listed in sources.json.

On the Internet Archive
───────────────────────
The archived files are AMFI's OWN NAVAll.txt, byte-for-byte, retrieved from a
capture instead of from amfiindia.com today. The archive is a delivery
mechanism for a primary AMFI artifact, not a third-party dataset — no
Value Research / Morningstar / Moneycontrol content is used anywhere here.
It is nonetheless a weaker chain of custody than fetching from AMFI directly,
so archive-derived rows are tagged AMFI_SNAPSHOT_ARCHIVED and every capture
timestamp is recorded in sources.json for independent re-fetch.

The live and historical AMFI endpoints are queried directly and tagged
AMFI_SNAPSHOT_LIVE / AMFI_SNAPSHOT_HISTORICAL.

The Internet Archive itself is occasionally unreachable (it has had real,
sometimes extended outages). When that happens this run still completes —
existence/wound-up data and the LIVE file don't depend on it — but archived
captures: 0 means no NAME_CHANGE/AMC_CHANGE actions were derived this run
beyond fund_lineage.CURATED_AMC_CHANGES (applied regardless, see
lineage_build.py step 5b). Re-run later, or with a longer wait between
retries, once archive.org is reachable again for full coverage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lineage_build import build_lineage  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="./lineage_out")
    p.add_argument("--quick", action="store_true", help="sample ~6 captures for a fast run")
    p.add_argument("--persist", action="store_true", help="also write to MongoDB")
    p.add_argument("--hist-years", type=int, default=20,
                   help="how far back to pull AMFI existence data (default 20)")
    p.add_argument("--mongo", default=os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
    p.add_argument("--db", default=os.environ.get("DB_NAME", "mf_analyst"))
    args = p.parse_args()

    async def run():
        db = None
        client = None
        if args.persist:
            from motor.motor_asyncio import AsyncIOMotorClient
            client = AsyncIOMotorClient(args.mongo)
            db = client[args.db]
        try:
            summary = await build_lineage(
                db, out_dir=args.out, quick=args.quick,
                persist=args.persist, hist_years=args.hist_years,
            )
        finally:
            if client:
                client.close()
        return summary

    summary = asyncio.run(run())
    print()
    print(json.dumps(summary, indent=2))
    print()
    print(f"artifacts written to: {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
