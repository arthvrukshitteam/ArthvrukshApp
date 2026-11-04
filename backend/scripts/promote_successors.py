"""Promote verified successor candidates into the live lineage.

Reads the ranked candidate list produced by discover_successors.py and writes
the accepted pairs to `fund_successor_map`, which server.py loads at startup
(refresh_successor_cache) and hands to amfi_client as ISIN-verified pairs.

Why a collection instead of CURATED_SUCCESSORS
──────────────────────────────────────────────
Hardcoding pairs in fund_lineage.CURATED_SUCCESSORS means every AMC merger
needs a code change and a redeploy. A DB-backed table means a discovery run
plus a review. The runtime safety property is unchanged: a promoted pair is
still re-verified by fund_lineage.verify_stitch on every use, so a wrong row
cannot corrupt returns unless the NAVs genuinely line up at the seam.

Promotion is DELIBERATELY not automatic. `--min-confidence` defaults to a high
bar and `--dry-run` is the default mode; a human runs it with `--apply` after
reading the candidate CSV.

Usage:
    python backend/scripts/promote_successors.py                    # dry run
    python backend/scripts/promote_successors.py --apply
    python backend/scripts/promote_successors.py --apply --min-confidence 0.80
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lineage_store as ls   # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", default="lineage_out/successor_candidates.csv")
    ap.add_argument("--min-confidence", type=float, default=0.75)
    ap.add_argument("--apply", action="store_true", help="write to MongoDB (default: dry run)")
    ap.add_argument("--mongo", default=os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
    ap.add_argument("--db", default=os.environ.get("DB_NAME", "mf_analyst"))
    args = ap.parse_args()

    with open(args.candidates, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    def conf(r):
        try:
            return float(r.get("confidence") or 0)
        except ValueError:
            return 0.0

    eligible = [r for r in rows if conf(r) >= args.min_confidence]
    # One successor may absorb several predecessors (a fund family merging), but
    # a predecessor with two proposed successors is ambiguous and must not be
    # auto-promoted.
    by_pred: dict = {}
    for r in eligible:
        by_pred.setdefault(int(r["predecessor_code"]), []).append(r)
    ambiguous = {p: v for p, v in by_pred.items() if len(v) > 1}
    clean = [v[0] for p, v in by_pred.items() if len(v) == 1]

    print(f"candidates            : {len(rows)}")
    print(f"confidence >= {args.min_confidence:.2f}   : {len(eligible)}")
    print(f"ambiguous (skipped)   : {len(ambiguous)}")
    print(f"promotable            : {len(clean)}")
    if ambiguous:
        for p, v in list(ambiguous.items())[:5]:
            print(f"   ! {p} -> {[x['successor_code'] for x in v]} (needs manual choice)")

    docs = [{
        "predecessor_code": int(r["predecessor_code"]),
        "successor_code": int(r["successor_code"]),
        "predecessor_name": r.get("predecessor_name", ""),
        "successor_name": r.get("successor_name", ""),
        "shared_isin": r.get("shared_isin", ""),
        "confidence": conf(r),
        "signals": {k[len("signal_"):]: (r[k] == "True")
                    for k in r if k.startswith("signal_")},
        "nav_test": r.get("nav_test", ""),
        "nav_reason": r.get("nav_reason", ""),
        "seam_delta_pct": r.get("seam_delta_pct", ""),
        "gap_days": r.get("gap_days", ""),
        "source": r.get("source", "ISIN_CONTINUITY"),
        "discovery_method": r.get("discovery_method", "AUTO_DISCOVERED"),
        "status": "promoted",
        "promoted_at": datetime.now(timezone.utc).isoformat(),
    } for r in clean]

    print("\ntop promotions:")
    for d in sorted(docs, key=lambda x: -x["confidence"])[:10]:
        print(f"  [{d['confidence']:.2f}] {d['predecessor_code']} -> {d['successor_code']}  "
              f"{d['predecessor_name'][:34]!r} -> {d['successor_name'][:34]!r}")

    if not args.apply:
        print(f"\nDRY RUN — nothing written. Re-run with --apply to promote {len(docs)} pairs.")
        return

    from motor.motor_asyncio import AsyncIOMotorClient
    client = AsyncIOMotorClient(args.mongo)
    db = client[args.db]
    await ls._bulk_load(db, ls.SUCCESSOR_COLLECTION, docs,
                        [([("predecessor_code", 1)], {"unique": True}),
                         ([("successor_code", 1)], {})])
    print(f"\npromoted {len(docs)} pairs -> {args.db}.{ls.SUCCESSOR_COLLECTION}")
    print("Restart the API (or POST /api/home/reindex) so refresh_successor_cache picks them up.")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
