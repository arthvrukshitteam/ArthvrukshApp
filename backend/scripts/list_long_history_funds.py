"""List funds whose EFFECTIVE (predecessor-stitched) NAV history is >= N years.

Why this script exists
──────────────────────
The deployed Render instance's `fund_nav_meta` index was built before the
predecessor/name-change stitching landed, so funds whose pre-transition history
lives under an older scheme code understate `history_years` there. The LOCAL
Mongo index is the trustworthy one — this script reads it directly.

It reads `fund_nav_meta` (built by nav_meta_indexer, which calls
amfi_client.load_analysis_nav and therefore already includes curated +
discovered + correlation-verified predecessor chains), filters on
`history_years`, collapses plan/option variants of the same fund down to one
row, and writes the result next to the repo root so it can be inspected or
diffed later.

Usage
─────
    cd backend
    python scripts/list_long_history_funds.py                 # >= 14 years, all categories
    python scripts/list_long_history_funds.py --years 12
    python scripts/list_long_history_funds.py --no-dedupe
    python scripts/list_long_history_funds.py --category Equity

Outputs (repo root):
    long_history_funds.json   full records + run metadata
    long_history_funds.csv    flat table
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv  # noqa: E402
from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

import amfi_client  # noqa: E402
from category_map import get_display_category  # noqa: E402

load_dotenv(BACKEND / ".env")

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")

NAV_META_COLLECTION = "fund_nav_meta"

# ── Plan/option tokens stripped when deciding "is this the same fund?" ───────
# Deliberately conservative: only plan/option scaffolding is removed, never
# words that distinguish one scheme from another (e.g. "Value", "Focused").
_PLAN_TOKENS = [
    r"\bregular\s+plan\b", r"\bregular\b",
    r"\bdirect\s+plan\b", r"\bdirect\b",
    r"\bgrowth\s+plan\b", r"\bgrowth\s+option\b", r"\bgrowth\b",
    r"\bbonus\s+option\b", r"\bbonus\b",
    r"\binstitutional\s+plan\b", r"\binstitutional\b",
    r"\bretail\s+plan\b", r"\bretail\b",
    r"\bsuper\s+institutional\b",
    r"\bplan\b", r"\boption\b",
    r"\bexisting\s+plan\b",
    r"\bunclaimed\b",
    r"\bsegregated\s+portfolio\b",
]
_PLAN_RE = re.compile("|".join(_PLAN_TOKENS), re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^a-z0-9]+")

_NON_GROWTH_OPTION_TOKENS = ("bonus", "dividend", "idcw", "payout", "reinvestment")


def normalise_fund_name(name: str) -> str:
    """Collapse a scheme name to a plan/option-agnostic identity key."""
    s = (name or "").lower()
    s = _PLAN_RE.sub(" ", s)
    s = _PUNCT_RE.sub(" ", s)
    return " ".join(s.split())


def _variant_rank(doc: dict):
    """Prefer a plain Growth option on the Regular plan, then shortest name.

    Option tier mirrors server.py::_dedupe_fund_variants's `_rank` (plain Growth
    beats Bonus/Dividend-style options). Plan tier is added on top because the
    retail-facing Regular plan is the one the app quotes — a Direct or
    Institutional variant must never win a group just for having a shorter name.
    """
    name = doc.get("scheme_name") or ""
    lname = name.lower()
    has_non_growth = any(tok in lname for tok in _NON_GROWTH_OPTION_TOKENS)
    is_growth = "growth" in lname
    if is_growth and not has_non_growth:
        option_tier = 0
    elif not has_non_growth:
        option_tier = 1
    else:
        option_tier = 2

    if "direct" in lname:
        plan_tier = 2
    elif any(tok in lname for tok in ("institutional", "retail plan", "super")):
        plan_tier = 1
    else:
        plan_tier = 0

    return (option_tier, plan_tier, len(name), name, doc.get("scheme_code") or 0)


def dedupe_variants(docs: list[dict]) -> tuple[list[dict], dict]:
    """One row per economic fund.

    Grouping key = (fund_house, plan/option-agnostic name, earliest_nav_date).
    earliest_nav_date is part of the key on purpose: two schemes from the same
    house with the same normalised name but genuinely different start dates are
    NOT the same fund and must not be silently merged.
    """
    groups: dict[tuple, list[dict]] = {}
    order: list[tuple] = []
    for d in docs:
        key = (
            (d.get("fund_house") or "").strip().lower(),
            normalise_fund_name(d.get("scheme_name") or ""),
            d.get("earliest_nav_date"),
        )
        if key not in groups:
            order.append(key)
        groups.setdefault(key, []).append(d)

    kept, dropped_map = [], {}
    for key in order:
        group = sorted(groups[key], key=_variant_rank)
        winner = group[0]
        if len(group) > 1:
            dropped_map[winner["scheme_code"]] = [
                {"scheme_code": g["scheme_code"], "scheme_name": g["scheme_name"]}
                for g in group[1:]
            ]
        kept.append(winner)
    return kept, dropped_map


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--years", type=float, default=14.0,
                    help="minimum effective history in years (default 14)")
    ap.add_argument("--category", default=None,
                    help="filter on the internal `category` field (e.g. Equity, Debt)")
    ap.add_argument("--no-dedupe", action="store_true",
                    help="list every eligible scheme_code separately")
    ap.add_argument("--out-dir", default=str(REPO_ROOT),
                    help="where to write the json/csv (default: repo root)")
    args = ap.parse_args()

    client = AsyncIOMotorClient(MONGO_URL, serverSelectionTimeoutMS=5000)
    db = client[DB_NAME]

    try:
        await db.command("ping")
    except Exception as exc:
        print(f"ERROR: cannot reach Mongo at {MONGO_URL} — {exc}", file=sys.stderr)
        print("Is mongod running? (brew services start mongodb-community)", file=sys.stderr)
        return 2

    total_indexed = await db[NAV_META_COLLECTION].count_documents({})
    if total_indexed == 0:
        print(
            f"ERROR: {DB_NAME}.{NAV_META_COLLECTION} is empty. Build the index first:\n"
            "  curl -X POST http://localhost:8000/api/home/reindex",
            file=sys.stderr,
        )
        return 3

    index_stats = await db.nav_meta_refresh_log.find_one({"_id": "nav_meta"}, {"_id": 0})

    query: dict = {"history_years": {"$gte": args.years}}
    if args.category:
        query["category"] = args.category

    projection = {
        "_id": 0, "scheme_code": 1, "scheme_name": 1, "fund_house": 1,
        "scheme_category": 1, "category": 1, "history_years": 1,
        "earliest_nav_date": 1, "latest_nav_date": 1, "nav_points": 1,
        "predecessor_used": 1, "merged_with": 1, "history_note": 1,
        "indexed_at": 1,
    }

    docs = [
        d async for d in db[NAV_META_COLLECTION].find(query, projection)
        if not amfi_client.is_idcw_scheme(d.get("scheme_name"))
    ]

    for d in docs:
        broad, sub = get_display_category(d.get("scheme_category", "") or "")
        d["broad_category"] = broad
        d["amfi_sub"] = sub
        d["stitched"] = bool(d.get("predecessor_used"))

    raw_count = len(docs)
    dropped_map: dict = {}
    if not args.no_dedupe:
        docs, dropped_map = dedupe_variants(docs)

    docs.sort(key=lambda d: (-(d.get("history_years") or 0), d.get("scheme_name") or ""))

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": f"{MONGO_URL.rsplit('@', 1)[-1]}/{DB_NAME}.{NAV_META_COLLECTION}",
        "criteria": {
            "min_history_years": args.years,
            "category_filter": args.category,
            "deduped": not args.no_dedupe,
            "note": (
                "history_years is the span of the EFFECTIVE series from "
                "amfi_client.load_analysis_nav, i.e. after curated + discovered "
                "predecessor stitching, so name-change / merger / AMC-acquisition "
                "history is included."
            ),
        },
        "index_health": {
            "total_schemes_indexed": total_indexed,
            "nav_meta_refresh_log": index_stats,
        },
        "counts": {
            "eligible_scheme_codes": raw_count,
            "funds_after_dedupe": len(docs),
            "stitched_funds": sum(1 for d in docs if d.get("stitched")),
            "with_history_note": sum(1 for d in docs if d.get("history_note")),
        },
        "funds": docs,
        "deduped_variants": dropped_map,
    }

    json_path = out_dir / "long_history_funds.json"
    json_path.write_text(json.dumps(payload, indent=2, default=str))

    csv_path = out_dir / "long_history_funds.csv"
    cols = [
        "scheme_code", "scheme_name", "fund_house", "category",
        "broad_category", "amfi_sub", "scheme_category",
        "history_years", "earliest_nav_date", "latest_nav_date",
        "nav_points", "stitched", "predecessor_used", "merged_with",
        "history_note",
    ]
    with csv_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for d in docs:
            row = dict(d)
            row["predecessor_used"] = ",".join(str(p) for p in (d.get("predecessor_used") or []))
            w.writerow(row)

    # ── Console summary ──────────────────────────────────────────────────────
    print(f"Mongo            {MONGO_URL.rsplit('@', 1)[-1]} / {DB_NAME}")
    print(f"Indexed schemes  {total_indexed:,}")
    if index_stats:
        print(f"Index last run   {index_stats.get('finished_at') or index_stats.get('started_at')}")
    print(f"Criteria         history_years >= {args.years}"
          + (f", category == {args.category}" if args.category else ""))
    print(f"Eligible codes   {raw_count:,}")
    print(f"After dedupe     {len(docs):,}")
    print(f"Predecessor-stitched among them: {payload['counts']['stitched_funds']:,}")
    print()

    by_sub: dict[str, int] = {}
    for d in docs:
        by_sub[d.get("amfi_sub") or "—"] = by_sub.get(d.get("amfi_sub") or "—", 0) + 1
    print("By AMFI sub-category:")
    for sub, n in sorted(by_sub.items(), key=lambda kv: -kv[1]):
        print(f"  {n:5d}  {sub}")
    print()

    print(f"{'code':>8}  {'yrs':>6}  {'first NAV':10}  {'st':2}  name")
    for d in docs:
        print(f"{d['scheme_code']:>8}  {d['history_years']:>6.2f}  "
              f"{d.get('earliest_nav_date') or '—':10}  "
              f"{'*' if d.get('stitched') else ' ':2}  {d['scheme_name']}")

    print(f"\nWrote {json_path}")
    print(f"Wrote {csv_path}")
    print("(* = history extended by a stitched predecessor code)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
