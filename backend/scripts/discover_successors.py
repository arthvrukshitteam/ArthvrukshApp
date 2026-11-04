"""Universe-wide successor discovery driven by ISIN continuity, not name similarity.

Why not name similarity
───────────────────────
`predecessor_discovery.run_discovery` generates candidates from
`_name_sim(name_a, name_b) >= 0.20` within a fund house. That approach has two
failure modes, both observed on this dataset:

  • FALSE POSITIVES — it finds plan/option twins. All 14 candidates from the
    earlier name-stem run were Growth/IDCW or Regular/Direct pairs of ONE
    scheme. Zero were mergers.
  • FALSE NEGATIVES, the worse half — it structurally cannot find a real
    merger, because a merger usually crosses fund houses and shares no words:
        "Morgan Stanley A.C.E. Fund"  ->  "HDFC Small Cap Fund"
        "Principal Multi Cap Growth"  ->  "Sundaram Multi Cap Fund"
    Name similarity for those pairs is ~0.

The signal that actually works: ISIN continuity
───────────────────────────────────────────────
An ISIN identifies a security, not a name. When an AMC is acquired the scheme
keeps its ISIN and gets a new AMFI scheme code, so the SAME ISIN appears under
two codes — one wound up, one live. That is direct identity evidence and it is
independent of naming.

Measured on the lineage table: 23,117 distinct ISINs, of which 451 appear under
more than one scheme code, and 342 of those are a dead-code/live-code pair —
the merger shape.

Corroboration AMFI hands us for free
────────────────────────────────────
Successor scheme names frequently carry "(Formerly Known as <old name>)". That
is AMFI stating the lineage in its own record, so when it is present and names
the predecessor, it is an independent second source agreeing with the ISIN.

Signals scored per candidate (weights in lineage_store.EVIDENCE_WEIGHTS)
───────────────────────────────────────────────────────────────────────
  isin_continuity     0.35  shared ISIN across the two codes (entry condition)
  nav_correlation     0.25  see nav_evidence() — correlation when the series
                            overlap, SEAM CONTINUITY when they do not
  official_source     0.20  AMFI's own "(Formerly Known as …)" naming the
                            predecessor
  amc_continuity      0.10  same canonical fund house
  category_continuity 0.05  same SEBI scheme category
  lifecycle           0.05  handover shape (predecessor ends, successor runs on)

Not scored, because no available feed carries them: benchmark continuity,
riskometer category, AUM. See lineage_store.UNAVAILABLE_SIGNALS.

Nothing is auto-promoted: the output is a ranked review list.

Usage:
    python backend/scripts/discover_successors.py \
        --lineage lineage_out/lineage.json \
        --out lineage_out/successor_candidates.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import amfi_client       # noqa: E402
import fund_lineage      # noqa: E402
import lineage_store as ls  # noqa: E402

NAV_CONCURRENCY = 8

_FORMERLY = re.compile(r"formerly\s+(?:known\s+as\s+)?(.+?)(?:\)|$)", re.IGNORECASE)


def formerly_known_as(name: str) -> Optional[str]:
    m = _FORMERLY.search(name or "")
    return m.group(1).strip() if m else None


# A merger hands the unit-holder's money across at the seam: the predecessor's
# final NAV and the successor's first NAV are close (identical unless a
# conversion ratio applies). Distinct funds have no reason to line up.
SEAM_MAX_DELTA_PCT = 0.10     # 10%
SEAM_MAX_GAP_DAYS = 45


def nav_evidence(pred: List[Dict[str, Any]], succ: List[Dict[str, Any]]) -> Dict[str, Any]:
    """NAV-based evidence appropriate to the pair's shape.

    `fund_lineage.verify_stitch` tests RETURN CORRELATION during an overlap.
    ISIN-continuity pairs have no overlap by construction — the predecessor
    stops and the successor starts — so correlation is undefined and
    verify_stitch's no-overlap branch rejects every non-curated pair. Applying
    it here scored 0/343, which measured my gate choice, not the candidates.

    For a zero-overlap handover the meaningful test is SEAM CONTINUITY: a small
    date gap AND the predecessor's last NAV ≈ the successor's first NAV. Both
    curated pairs show exactly this — Morgan Stanley 20.697 -> HDFC 21.121 and
    JPMorgan 19.832 -> Edelweiss 20.270, each a 4-day gap and ~2% step.

    When the pair DOES overlap, correlation is the stronger test and is used.
    """
    out: Dict[str, Any] = {
        "correlation": None, "overlap_days": 0, "gap_days": None,
        "concurrent_fraction": None, "successor_extension_days": None,
        "seam_pred_last_nav": None, "seam_succ_first_nav": None,
        "seam_delta_pct": None, "nav_test": "none", "nav_reason": "",
        "nav_ok": False, "handover_shape": False,
    }
    if not pred or not succ:
        out["nav_reason"] = "missing NAV series"
        return out

    p_first, p_last = pred[0]["date"], pred[-1]["date"]
    s_first, s_last = succ[0]["date"], succ[-1]["date"]
    out["gap_days"] = (s_first - p_last).days
    concurrent = max(0, (min(p_last, s_last) - max(p_first, s_first)).days)
    life = max(1, (p_last - p_first).days)
    out["concurrent_fraction"] = round(concurrent / life, 4)
    out["successor_extension_days"] = (s_last - p_last).days
    out["overlap_days"] = concurrent
    out["handover_shape"] = (
        out["concurrent_fraction"] <= fund_lineage.MAX_CONCURRENT_FRACTION
        and out["successor_extension_days"] >= fund_lineage.MIN_SUCCESSOR_EXTENSION_DAYS
    )

    if concurrent >= fund_lineage.MIN_OVERLAP_DAYS:
        accepted, sig = fund_lineage.verify_stitch(pred, succ, curated=False)
        out.update({"correlation": sig.get("correlation"), "nav_test": "correlation",
                    "nav_ok": bool(accepted), "nav_reason": sig.get("reason", "")})
        return out

    # Zero / negligible overlap -> seam continuity
    p_nav, s_nav = pred[-1]["nav"], succ[0]["nav"]
    delta = abs(s_nav - p_nav) / p_nav if p_nav else None
    out.update({
        "seam_pred_last_nav": round(p_nav, 4),
        "seam_succ_first_nav": round(s_nav, 4),
        "seam_delta_pct": round(delta, 4) if delta is not None else None,
        "nav_test": "seam_continuity",
    })
    gap_ok = out["gap_days"] is not None and 0 <= out["gap_days"] <= SEAM_MAX_GAP_DAYS
    seam_ok = delta is not None and delta <= SEAM_MAX_DELTA_PCT
    out["nav_ok"] = bool(gap_ok and seam_ok)
    out["nav_reason"] = (
        f"gap {out['gap_days']}d {'<=' if gap_ok else '>'} {SEAM_MAX_GAP_DAYS}d, "
        f"NAV step {delta:.2%} {'<=' if seam_ok else '>'} {SEAM_MAX_DELTA_PCT:.0%}"
        if delta is not None else "no comparable seam NAV"
    )
    return out


def isin_candidates(lineage: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Dead-code -> live-code pairs that share at least one ISIN."""
    by_isin: Dict[str, Set[int]] = defaultdict(set)
    for code, rec in lineage.items():
        for iv in rec["isin_history"]:
            if iv["value"]:
                by_isin[iv["value"]].add(int(code))
        if rec["current_isin"]:
            by_isin[rec["current_isin"]].add(int(code))

    seen: Set[Tuple[int, int]] = set()
    out: List[Dict[str, Any]] = []
    for isin, codes in by_isin.items():
        if len(codes) < 2:
            continue
        dead = [c for c in codes if not lineage[str(c)]["active"]]
        live = [c for c in codes if lineage[str(c)]["active"]]
        for p in dead:
            for s in live:
                if (p, s) in seen:
                    continue
                seen.add((p, s))
                prec, srec = lineage[str(p)], lineage[str(s)]
                out.append({
                    "predecessor_code": p, "predecessor_name": prec["current_name"],
                    "successor_code": s, "successor_name": srec["current_name"],
                    "shared_isin": isin,
                    "predecessor_amc": prec["current_amc"],
                    "successor_amc": srec["current_amc"],
                    "predecessor_last_seen": prec["observed_to"],
                    "successor_last_seen": srec["observed_to"],
                    "predecessor_category": prec.get("scheme_type", ""),
                    "successor_category": srec.get("scheme_type", ""),
                })
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lineage", default="lineage_out/lineage.json")
    ap.add_argument("--out", default="lineage_out/successor_candidates.csv")
    ap.add_argument("--limit", type=int, default=0, help="0 = all candidates")
    ap.add_argument("--refresh-nav", action="store_true", help="ignore the NAV disk cache")
    args = ap.parse_args()

    lineage = json.load(open(args.lineage, encoding="utf-8"))
    cands = isin_candidates(lineage)
    if args.limit:
        cands = cands[: args.limit]
    print(f"ISIN-continuity candidates: {len(cands)}\n", flush=True)

    # Disk cache: a full re-fetch of ~684 complete NAV histories takes tens of
    # minutes, and re-scoring against changed gates must not require it.
    cache_path = os.path.join(os.path.dirname(args.out) or ".", "_nav_cache.json")
    nav_cache: Dict[int, List[Dict[str, Any]]] = {}
    if os.path.exists(cache_path) and not args.refresh_nav:
        raw = json.load(open(cache_path, encoding="utf-8"))
        from datetime import date as _date
        nav_cache = {
            int(k): [{"date": _date.fromisoformat(r["d"]), "nav": r["n"]} for r in v]
            for k, v in raw.items()
        }
        print(f"NAV disk cache: {len(nav_cache)} codes loaded", flush=True)
    sem = asyncio.Semaphore(NAV_CONCURRENCY)

    async def nav(code: int) -> List[Dict[str, Any]]:
        if code in nav_cache:
            return nav_cache[code]
        async with sem:
            try:
                payload = await amfi_client.fetch_fund_nav(code)
                nav_cache[code] = amfi_client.parse_nav_history(payload)
            except Exception:
                nav_cache[code] = []
        return nav_cache[code]

    codes = {c["predecessor_code"] for c in cands} | {c["successor_code"] for c in cands}
    missing = [c for c in codes if c not in nav_cache]
    if missing:
        print(f"fetching NAV for {len(missing)} codes ...", flush=True)
        await asyncio.gather(*[nav(c) for c in missing])
        json.dump(
            {str(k): [{"d": r["date"].isoformat(), "n": r["nav"]} for r in v]
             for k, v in nav_cache.items()},
            open(cache_path, "w"),
        )
        print(f"NAV fetch complete, cached -> {cache_path}\n", flush=True)
    else:
        print("all NAV series served from cache\n", flush=True)

    rows: List[Dict[str, Any]] = []
    for c in cands:
        p, s = c["predecessor_code"], c["successor_code"]
        pred, succ = nav_cache.get(p, []), nav_cache.get(s, [])
        sig = nav_evidence(pred, succ)

        former = formerly_known_as(c["successor_name"])
        name_corroborated = bool(
            former and ls.name_stem(former)
            and ls.name_stem(former) in ls.name_stem(c["predecessor_name"])
        )

        # AMC continuity: same house, or two names that canonicalise together
        # (Principal -> Sundaram is an acquisition, so the raw strings differ
        # while the economic entity continues).
        p_house = fund_lineage.canonical_house(c["predecessor_amc"] or c["predecessor_name"])
        s_house = fund_lineage.canonical_house(c["successor_amc"] or c["successor_name"])
        amc_continuity = bool(p_house and p_house == s_house)

        category_continuity = bool(
            c.get("predecessor_category") and
            c["predecessor_category"] == c.get("successor_category"))

        signals = {
            "isin_continuity": True,                 # entry condition
            "nav_correlation": sig["nav_ok"],
            "official_source": name_corroborated,
            "amc_continuity": amc_continuity,
            "category_continuity": category_continuity,
            "lifecycle": sig["handover_shape"],
        }
        confidence = ls.evidence_score(signals)

        rows.append({
            **c,
            **{k: sig[k] for k in ("correlation", "overlap_days", "gap_days",
                                   "concurrent_fraction", "successor_extension_days",
                                   "seam_pred_last_nav", "seam_succ_first_nav",
                                   "seam_delta_pct", "nav_test", "nav_reason")},
            "formerly_known_as": former or "",
            **{f"signal_{k}": v for k, v in signals.items()},
            "signals_passed": sum(1 for v in signals.values() if v),
            "confidence": confidence,
            "recommendation": ("PROMOTE" if confidence >= 0.75 else
                               "REVIEW" if confidence >= 0.50 else "REJECT"),
            "discovery_method": "AUTO_DISCOVERED",
            "source": "ISIN_CONTINUITY",
        })

    rows.sort(key=lambda r: (-(r["confidence"] or 0), r["predecessor_code"]))
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    rec = Counter(r["recommendation"] for r in rows)
    print("=" * 72)
    print(f"candidates evaluated : {len(rows)}")
    for k in ("PROMOTE", "REVIEW", "REJECT"):
        print(f"  {k:<8}: {rec.get(k, 0)}")
    print(f"\nsignal pass rates (weighted by lineage_store.EVIDENCE_WEIGHTS):")
    for g, w in ls.EVIDENCE_WEIGHTS.items():
        n = sum(1 for r in rows if r.get(f"signal_{g}"))
        print(f"  {g:<22} (w={w:.2f}): {n}/{len(rows)}")
    print(f"  nav test used         : {dict(Counter(r['nav_test'] for r in rows))}")
    print(f"\ntop candidates:")
    for r in rows[:8]:
        print(f"  [{r['confidence']}] {r['predecessor_code']} -> {r['successor_code']}  "
              f"{r['predecessor_name'][:38]!r} -> {r['successor_name'][:38]!r}")
    print(f"\nwritten: {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
