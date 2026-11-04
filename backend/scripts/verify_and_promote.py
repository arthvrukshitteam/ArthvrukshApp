"""Apply manual verification decisions to the ISIN-continuity candidates and
promote the resulting verified set into `fund_successor_map`.

This is the human-in-the-loop step discover_successors.py's docstring calls
for ("Nothing is auto-promoted: the output is a ranked review list"). The 343
candidates in successor_candidates.csv collapse to exactly 8 distinct
AMC-level corporate events (grouped by predecessor_amc -> successor_amc);
each event was independently verified against official sources (SEBI
circulars/orders, AMC press releases and merger pages, financial press) via
web research documented in LINEAGE_VALIDATION_REPORT.md. This script encodes
those per-event verification decisions plus scheme-level exclusions found
during review, and writes the resulting rows to `fund_successor_map` with
the effective_date/evidence/sources fields the promotion schema requires.

Usage:
    python backend/scripts/verify_and_promote.py                 # dry run
    python backend/scripts/verify_and_promote.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lineage_store as ls  # noqa: E402

# ── Per-AMC-event verification decisions ─────────────────────────────────────
# Keyed by (predecessor_amc, successor_amc) exactly as they appear in
# successor_candidates.csv. Each entry is the evidence bundle attached to
# EVERY promoted row from that event (an AMC merger is one corporate action;
# it does not need a separate citation per scheme).
VERIFIED_EVENTS: dict[tuple[str, str], dict] = {
    ("L&T Mutual Fund", "HSBC Mutual Fund"): {
        "effective_date": "2022-11-25",
        "evidence": (
            "SEBI approved HSBC Asset Management (India)'s acquisition of L&T "
            "Investment Management on 2022-10-11 (Rs 3,191 crore, $425M deal "
            "announced Dec 2021, CCI approval Mar 2022); transaction completed "
            "2022-11-25 and L&T scheme management transferred to HSBC MF."
        ),
        "sources": [
            "https://www.business-standard.com/article/companies/hsbc-asset-management-company-completes-acquisition-of-l-t-mutual-fund-122112501062_1.html",
            "https://www.business-standard.com/amp/article/markets/sebi-gives-go-ahead-to-hsbc-amc-s-acquisition-of-l-t-investment-managers-122101101176_1.html",
        ],
    },
    ("BNP Paribas Mutual Fund", "Baroda BNP Paribas Mutual Fund"): {
        "effective_date": "2022-03-14",
        "evidence": (
            "Merger of Baroda Asset Management India Ltd with BNP Paribas Asset "
            "Management India Pvt Ltd (and the corresponding trustee-company "
            "merger) came into effect 2022-03-14, per SEBI no-objection letters "
            "dated 2022-01-17, 2022-01-19 and 2022-01-28. Bank of Baroda holds "
            "50.1%, BNPP AM 49.9% of the surviving AMC."
        ),
        "sources": [
            "https://www.barodabnpparibasmf.in/amalgamation",
            "https://www.business-standard.com/article/markets/bank-of-baroda-bnp-paribas-announce-mutual-fund-jv-122031400960_1.html",
        ],
    },
    ("JPMorgan Mutual Fund", "Edelweiss Mutual Fund"): {
        "effective_date": "2016-11-25",
        "evidence": (
            "Edelweiss Asset Management completed acquisition of JPMorgan Asset "
            "Management India's onshore mutual fund business; JPMorgan MF "
            "schemes transferred to and formed part of Edelweiss Mutual Fund "
            "from close of business 2016-11-25 (AUM ~Rs 7,081 Cr)."
        ),
        "sources": [
            "https://cdn1.edelweissfin.com/wp-content/uploads/2020/03/Edelweiss-acquires-JP-Morgan-Mutual-Fund-Business-in-India.pdf",
            "https://www.business-standard.com/article/markets/edelweiss-announces-buy-of-jpmorgan-india-mf-biz-116032200710_1.html",
        ],
    },
    ("Principal Mutual Fund", "Sundaram Mutual Fund"): {
        "effective_date": "2021-12-31",
        "evidence": (
            "Sundaram Asset Management received SEBI approval 2021-11-18 to "
            "acquire Principal Asset Management, Principal Trustee Company and "
            "Principal Retirement Advisors (100% share capital); acquisition "
            "completed 2021-12-31, Principal India schemes merged into or "
            "renamed as Sundaram schemes."
        ),
        "sources": [
            "https://www.sundarammutual.com/Acquisition-of-Principal-Mutual-Fund",
            "https://www.business-standard.com/amp/article/pti-stories/sundaram-asset-mgmt-receives-sebi-nod-for-purchase-of-principal-amc-in-india-121111900861_1.html",
        ],
    },
    ("IDBI Mutual Fund", "LIC Mutual Fund"): {
        "effective_date": "2023-07-29",
        "evidence": (
            "LIC Mutual Fund completed takeover of IDBI Mutual Fund schemes "
            "effective 2023-07-29 (announced complete 2023-07-31), driven by "
            "SEBI Mutual Fund Regulation 7B (one sponsor cannot control two "
            "AMCs, triggered by LIC's 2019 majority stake in IDBI Bank, IDBI "
            "MF's sponsor). 10 of 20 IDBI schemes merged into similar LIC "
            "schemes; the other 10 taken over standalone. Candidate rows with "
            "clean seam continuity (<5% NAV step, 3-4 day gap) promoted despite "
            "the AMC/category/official-source soft signals correctly not firing "
            "(LIC did not tag renamed schemes 'Formerly IDBI ...' in AMFI data, "
            "and IDBI/LIC are genuinely different brands, not a rename)."
        ),
        "sources": [
            "https://www.licmf.com/LICMF-IDBI-Mergerpage/",
            "https://www.business-standard.com/amp/companies/news/lic-mutual-fund-completes-the-takeover-of-idbi-mutual-fund-schemes-123073100956_1.html",
            "https://cleartax.in/s/merger-of-idbi-with-lic-mutual-fund",
        ],
        # Scheme-level exclusions within an otherwise-verified event: these
        # rows showed seam NAV discontinuities (13%-99%) inconsistent with a
        # clean 1:1 succession, most likely the "merged with a similar (not
        # identical) scheme" cases LIC's own announcement describes. Left for
        # manual review rather than promoted on the strength of the AMC-level
        # event alone.
        "exclude_predecessor_codes": {115939, 118457, 127182, 127184, 128332, 128337},
    },
    ("Goldman Sachs Mutual Fund", "Nippon India Mutual Fund"): {
        "effective_date": "2016-11-05",
        "evidence": (
            "Reliance Capital Asset Management completed acquisition of "
            "Goldman Sachs Asset Management (India) from Goldman Sachs Asset "
            "Management L.P. on 2016-11-05; management of all 12 Goldman Sachs "
            "MF schemes (mostly BeES-branded ETFs) transferred to Reliance "
            "(renamed Nippon India in 2019, hence the successor AMC name seen "
            "today). The 4 candidates with clean seam continuity (<=1.5% "
            "NAV step, 4-day gap: Shariah/Infrastructure/CPSE/Nifty Junior "
            "BeES) are promoted; the other 5 (Nifty/Bank/Gold/PSU Bank/Hang "
            "Seng BeES) show 90-99% seam NAV discontinuities inconsistent "
            "with a simple handover — likely a unit-structure rebase during "
            "transfer that this candidate's ISIN match cannot verify on its "
            "own — and are left for manual review, not promoted."
        ),
        "sources": [
            "https://www.marketscreener.com/quote/stock/NIPPON-LIFE-INDIA-ASSET-M-40202042/news/Reliance-Capital-Asset-Management-Limited-completed-the-acquisition-of-Goldman-Sachs-Asset-Managemen-35287196/",
            "https://www.business-standard.com/amp/article/pti-stories/reliance-mf-to-manage-goldman-sachs-mf-s-schemes-from-nov-5-116110201041_1.html",
        ],
        # 115729 (Nifty Junior BeES) initially fetched with no comparable seam
        # NAV (transient fetch gap on the first discovery pass) and rejected;
        # the re-run after the canonical_house fix re-fetched successfully and
        # shows the same clean seam continuity (0.56%) as the other 4 included
        # ETFs, so it belongs in this set on re-inspection.
        "include_only_predecessor_codes": {115750, 115755, 128751, 115729},
    },
    ("JM Financial Mutual Fund", "JM Financial Mutual Fund"): {
        "effective_date": "2018-05-01",
        "evidence": (
            "Same-AMC scheme consolidation/rename (JM Money Manager Fund's "
            "Super Plan / Super Plus Plan into JM Money Market Fund / JM Low "
            "Duration Fund respectively), consistent with the industry-wide "
            "SEBI 'Categorisation and Rationalisation of Mutual Fund Schemes' "
            "circular (Oct 2017) that JM Financial implemented from May 2018 "
            "for its other schemes (JM Balanced->JM Equity Hybrid, JM High "
            "Liquidity->JM Liquid, etc. — same mechanism, confirmed renames). "
            "This specific Money Manager pairing is not independently named in "
            "the sources found, so the evidentiary basis here is the very "
            "strong internal evidence: same AMC throughout (no ownership "
            "change), near-zero seam NAV steps (0.03-0.04%), 1-day gaps, "
            "across all 8 rows."
        ),
        "sources": [
            "https://cleartax.in/s/mutual-fund-scheme-categorisation-rationalisation-sebi",
            "https://prognoadvisor.com/home/blogdetails/10117/Scheme-Name-Changes-of-JM-Mutual-Fund",
        ],
        "confidence_note": "same-AMC scheme consolidation, not an AMC acquisition — lower external-citation strength, flagged in report",
    },
    ("Reliance Mutual Fund", "Nippon India Mutual Fund"): {
        "effective_date": "2019-09-30",
        "evidence": (
            "Reliance Nippon Life Asset Management renamed to Nippon Life "
            "India Asset Management following Nippon Life's 2019 majority "
            "stake purchase; scheme codes were NOT migrated for the AMC-wide "
            "rename (handled by the existing same-code name_history / "
            "current_amc mechanism, verified separately — see "
            "LINEAGE_VALIDATION_REPORT.md resolver examples). Only the "
            "narrower, genuinely code-migrating candidate is handled here: "
            "predecessor 118610 'Reliance Liquid Fund - Cash Plan - Direct "
            "Plan' -> successor 143494 'Nippon India Ultra Short Duration "
            "Fund - Direct', clean seam continuity (0.01% NAV step, 1-day "
            "gap) consistent with the 2018 SEBI Liquid->Ultra-Short-Duration "
            "recategorisation layered on the 2019 AMC rename. Predecessor "
            "118609 maps ambiguously to TWO successor codes (143497 and "
            "143502) in the candidate set and is excluded — needs manual "
            "disambiguation, not auto-promoted."
        ),
        "sources": [
            "https://www.marketscreener.com/quote/stock/NIPPON-LIFE-INDIA-ASSET-M-40202042/",
        ],
        "include_only_predecessor_codes": {118610},
    },
}


def decide(row: dict) -> tuple[bool, dict | None]:
    key = (row["predecessor_amc"], row["successor_amc"])
    event = VERIFIED_EVENTS.get(key)
    if event is None:
        return False, None
    pred_code = int(row["predecessor_code"])
    if "include_only_predecessor_codes" in event:
        if pred_code not in event["include_only_predecessor_codes"]:
            return False, None
    if "exclude_predecessor_codes" in event:
        if pred_code in event["exclude_predecessor_codes"]:
            return False, None
    return True, event


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", default="lineage_out/successor_candidates.csv")
    ap.add_argument("--out", default="lineage_out/verified_promotions.csv")
    ap.add_argument("--apply", action="store_true", help="write to MongoDB (default: dry run)")
    ap.add_argument("--mongo", default=os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
    ap.add_argument("--db", default=os.environ.get("DB_NAME", "mf_analyst"))
    args = ap.parse_args()

    with open(args.candidates, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    print(f"candidates in file      : {len(rows)}")

    by_pred: dict[int, list] = {}
    verified_rows = []
    for r in rows:
        ok, event = decide(r)
        if not ok:
            continue
        r["_effective_date"] = event["effective_date"]
        r["_evidence"] = event["evidence"]
        r["_sources"] = event["sources"]
        verified_rows.append(r)
        by_pred.setdefault(int(r["predecessor_code"]), []).append(r)

    ambiguous = {p: v for p, v in by_pred.items() if len(v) > 1}
    clean = [v[0] for p, v in by_pred.items() if len(v) == 1]

    print(f"verified by event rules : {len(verified_rows)}")
    print(f"ambiguous (skipped)      : {len(ambiguous)}  -> {list(ambiguous.keys())}")
    print(f"promotable               : {len(clean)}")

    fieldnames = list(rows[0].keys()) + ["_effective_date", "_evidence"]
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in clean:
            w.writerow(r)
    print(f"written: {args.out}")

    docs = [{
        "predecessor_code": int(r["predecessor_code"]),
        "successor_code": int(r["successor_code"]),
        "predecessor_name": r.get("predecessor_name", ""),
        "successor_name": r.get("successor_name", ""),
        "shared_isin": r.get("shared_isin", ""),
        "confidence": float(r.get("confidence") or 0),
        "signals": {k[len("signal_"):]: (r[k] == "True")
                    for k in r if k.startswith("signal_")},
        "nav_test": r.get("nav_test", ""),
        "nav_reason": r.get("nav_reason", ""),
        "seam_delta_pct": r.get("seam_delta_pct", ""),
        "gap_days": r.get("gap_days", ""),
        "source": r.get("source", "ISIN_CONTINUITY"),
        "discovery_method": "MANUALLY_VERIFIED",
        "effective_date": r["_effective_date"],
        "evidence": r["_evidence"],
        "sources": r["_sources"],
        "status": "promoted",
        "promoted_at": datetime.now(timezone.utc).isoformat(),
    } for r in clean]

    print("\nby event:")
    from collections import Counter
    cnt = Counter((d["source"], d["predecessor_name"][:0]) for d in docs)  # placeholder
    by_event_count = Counter()
    for r in clean:
        by_event_count[(r["predecessor_amc"], r["successor_amc"])] += 1
    for k, v in sorted(by_event_count.items(), key=lambda kv: -kv[1]):
        print(f"  {k[0]:35s} -> {k[1]:30s}  {v}")

    if not args.apply:
        print(f"\nDRY RUN — nothing written to Mongo. Re-run with --apply to promote {len(docs)} pairs.")
        return

    from motor.motor_asyncio import AsyncIOMotorClient
    client = AsyncIOMotorClient(args.mongo)
    db = client[args.db]
    await ls._bulk_load(db, ls.SUCCESSOR_COLLECTION, docs,
                        [([("predecessor_code", 1)], {"unique": True}),
                         ([("successor_code", 1)], {})])
    print(f"\npromoted {len(docs)} pairs -> {args.db}.{ls.SUCCESSOR_COLLECTION}")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
