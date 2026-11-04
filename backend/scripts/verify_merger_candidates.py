"""Verify proposed merger pairs against NAV evidence before promoting any.

Runs each candidate through TWO independent gates:

  1. fund_lineage.verify_stitch — NAV return-correlation during overlap.
  2. A CO-EXISTENCE check this script adds.

Why gate 2 exists
─────────────────
Correlation alone cannot tell a merger from two plan variants of one scheme.
"HDFC Top 100 - Growth" and "HDFC Top 100 - IDCW" hold the identical portfolio,
so their daily returns correlate ~1.0 — they would sail through gate 1 — yet
neither succeeds the other; they coexist for decades. Splicing one onto the
other would silently double-count a fund's history.

A genuine merger has a HANDOVER shape: the predecessor stops publishing, and
the successor carries on. Two variants have a CONCURRENT shape: both publish
across the same years and both are still alive (or both died together).

So a pair is only a merger candidate worth promoting when:
    • returns correlate (same portfolio), AND
    • the predecessor's series ENDS while the successor's CONTINUES —
      concretely, the successor's last NAV is materially later than the
      predecessor's, and their concurrent lifespan is short relative to the
      predecessor's total life.

Usage:
    python backend/scripts/verify_merger_candidates.py \
        --candidates lineage_out/merger_candidates.csv \
        --out lineage_out/merger_verification.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import date
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import amfi_client            # noqa: E402
import fund_lineage           # noqa: E402

# A successor must outlive its predecessor by at least this much for the pair
# to have a handover shape rather than a concurrent one.
MIN_SUCCESSOR_EXTENSION_DAYS = 180

# If the two series ran side by side for longer than this share of the
# predecessor's life, they are concurrent variants, not a succession.
MAX_CONCURRENT_FRACTION = 0.25


async def load_nav(code: int) -> List[Dict[str, Any]]:
    try:
        payload = await amfi_client.fetch_fund_nav(code)
        return amfi_client.parse_nav_history(payload)
    except Exception as exc:
        print(f"   ! NAV fetch failed for {code}: {exc}", flush=True)
        return []


def coexistence(pred: List[Dict[str, Any]], succ: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Shape metrics distinguishing a handover from two concurrent variants."""
    if not pred or not succ:
        return {"verdict": "NO_DATA"}
    p_first, p_last = pred[0]["date"], pred[-1]["date"]
    s_first, s_last = succ[0]["date"], succ[-1]["date"]

    concurrent_start = max(p_first, s_first)
    concurrent_end = min(p_last, s_last)
    concurrent_days = max(0, (concurrent_end - concurrent_start).days)
    pred_life_days = max(1, (p_last - p_first).days)
    extension_days = (s_last - p_last).days
    frac = concurrent_days / pred_life_days

    if extension_days < MIN_SUCCESSOR_EXTENSION_DAYS:
        verdict = "NOT_A_MERGER_SUCCESSOR_DOES_NOT_OUTLIVE"
    elif frac > MAX_CONCURRENT_FRACTION:
        verdict = "NOT_A_MERGER_CONCURRENT_VARIANTS"
    else:
        verdict = "HANDOVER_SHAPE"

    return {
        "pred_first": p_first.isoformat(), "pred_last": p_last.isoformat(),
        "succ_first": s_first.isoformat(), "succ_last": s_last.isoformat(),
        "concurrent_days": concurrent_days,
        "pred_life_days": pred_life_days,
        "concurrent_fraction": round(frac, 4),
        "successor_extension_days": extension_days,
        "verdict": verdict,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", default="lineage_out/merger_candidates.csv")
    ap.add_argument("--out", default="lineage_out/merger_verification.csv")
    args = ap.parse_args()

    with open(args.candidates, newline="", encoding="utf-8") as fh:
        cands = list(csv.DictReader(fh))
    print(f"candidates to verify: {len(cands)}\n", flush=True)

    cache: Dict[int, List[Dict[str, Any]]] = {}

    async def nav(code: int):
        if code not in cache:
            cache[code] = await load_nav(code)
        return cache[code]

    rows: List[Dict[str, Any]] = []
    for c in cands:
        p, s = int(c["predecessor_code"]), int(c["successor_code"])
        pred, succ = await nav(p), await nav(s)
        accepted, signals = fund_lineage.verify_stitch(pred, succ, curated=False)
        shape = coexistence(pred, succ)
        promote = bool(accepted and shape.get("verdict") == "HANDOVER_SHAPE")
        rows.append({
            "predecessor_code": p, "predecessor_name": c["predecessor_name"],
            "successor_code": s, "successor_name": c["successor_name"],
            "correlation": signals.get("correlation"),
            "overlap_days": signals.get("overlap_days"),
            "gap_days": signals.get("gap_days"),
            "correlation_gate": "PASS" if accepted else "FAIL",
            "correlation_reason": signals.get("reason"),
            **shape,
            "promote": promote,
        })
        print(f"{p} -> {s}", flush=True)
        print(f"   corr={signals.get('correlation')} overlap={signals.get('overlap_days')}d "
              f"gate1={'PASS' if accepted else 'FAIL'}", flush=True)
        print(f"   shape: {shape.get('verdict')} "
              f"(concurrent {shape.get('concurrent_days')}d = "
              f"{shape.get('concurrent_fraction')} of pred life; "
              f"successor outlives by {shape.get('successor_extension_days')}d)", flush=True)
        print(f"   PROMOTE: {promote}\n", flush=True)

    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    n_gate1 = sum(1 for r in rows if r["correlation_gate"] == "PASS")
    n_promote = sum(1 for r in rows if r["promote"])
    print("=" * 70)
    print(f"correlation gate alone would ACCEPT : {n_gate1}/{len(rows)}")
    print(f"after co-existence gate, PROMOTED   : {n_promote}/{len(rows)}")
    print(f"false positives correlation missed  : {n_gate1 - n_promote}")
    print(f"\nwritten: {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
