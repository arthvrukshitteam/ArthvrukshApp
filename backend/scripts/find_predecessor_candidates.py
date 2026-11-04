"""Find predecessor candidates for the 2018 SEBI fund recategorisation.

Usage:
    python find_predecessor_candidates.py > candidates.txt

What it does:
1. Fetches all schemes from mfapi.in fund list.
2. For each scheme whose NAV history starts between 2016-01-01 and 2019-12-31
   (likely a post-recat successor), searches for schemes from the SAME fund house
   whose NAV history ends around the same period (likely the merged-away predecessor).
3. Prints ranked candidate pairs for manual review.

After verifying a pair, add it to amfi_client.py:
    PREDECESSOR_MAP[successor_code] = [predecessor_code]
"""

import asyncio
import sys
from collections import defaultdict
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx

MFAPI_BASE = "https://api.mfapi.in/mf"

# NAV history window that suggests a 2018 recat fund
RECAT_INCEPTION_START = date(2016, 1, 1)
RECAT_INCEPTION_END   = date(2019, 12, 31)

# A predecessor's last NAV should be in this window
PREDECESSOR_LAST_NAV_START = date(2016, 1, 1)
PREDECESSOR_LAST_NAV_END   = date(2020, 6, 30)

# Minimum predecessor history (years) to be worth stitching
MIN_PREDECESSOR_HISTORY_YEARS = 3.0


# ── Helpers ──────────────────────────────────────────────────────────────────

def extract_fund_house(scheme_name: str) -> str:
    """Best-effort fund house extraction from scheme name.
    Returns the first 1–2 tokens (e.g. 'Axis', 'Nippon India', 'HDFC').
    """
    name = scheme_name.strip()
    tokens = name.split()
    # Known two-word fund houses
    two_word = {
        "nippon india", "mirae asset", "franklin templeton", "dsp blackrock",
        "uti mutual", "l&t", "l & t", "sundaram mutual", "invesco india",
        "pgim india", "navi mutual", "white oak", "trust mutual", "bank of",
        "tata mutual", "kotak mahindra", "canara robeco",
    }
    prefix2 = " ".join(tokens[:2]).lower()
    if prefix2 in two_word:
        return prefix2.title()
    return tokens[0] if tokens else ""


def name_keywords(scheme_name: str) -> set:
    """Extract meaningful keywords from a scheme name for fuzzy matching."""
    stopwords = {
        "fund", "mutual", "scheme", "direct", "regular", "growth", "dividend",
        "idcw", "plan", "option", "reinvestment", "payout", "bonus", "-", "–",
        "the", "of", "and", "&", "india", "limited", "ltd",
    }
    tokens = scheme_name.lower().replace("-", " ").replace("&", " ").split()
    return {t for t in tokens if t not in stopwords and len(t) > 2}


def keyword_overlap(name_a: str, name_b: str) -> float:
    """Jaccard similarity on meaningful name keywords."""
    a = name_keywords(name_a)
    b = name_keywords(name_b)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ── Fetch helpers ─────────────────────────────────────────────────────────────

async def fetch_fund_list(client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    r = await client.get(MFAPI_BASE, timeout=60.0)
    r.raise_for_status()
    return r.json()


async def fetch_nav_dates(
    client: httpx.AsyncClient, scheme_code: int
) -> Tuple[Optional[date], Optional[date], int]:
    """Return (first_nav_date, last_nav_date, n_points) for a scheme."""
    try:
        r = await client.get(f"{MFAPI_BASE}/{scheme_code}", timeout=30.0)
        r.raise_for_status()
        data = r.json().get("data", []) or []
        dates = []
        for row in data:
            try:
                dates.append(datetime.strptime(row["date"], "%d-%m-%Y").date())
            except Exception:
                pass
        if not dates:
            return None, None, 0
        return min(dates), max(dates), len(dates)
    except Exception:
        return None, None, 0


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    print("Fetching full fund list from mfapi.in …", file=sys.stderr)

    async with httpx.AsyncClient(timeout=60.0) as client:
        all_funds = await fetch_fund_list(client)

    # Drop direct plans — we only care about regular plans
    regular = [
        f for f in all_funds
        if "direct" not in (f.get("schemeName") or "").lower()
    ]
    print(f"  {len(regular)} regular-plan schemes found", file=sys.stderr)

    # Group by fund house
    by_house: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for f in regular:
        house = extract_fund_house(f.get("schemeName", ""))
        by_house[house].append(f)

    # Fetch NAV date ranges for all schemes (batched, concurrency 10)
    print("Fetching NAV date ranges (this takes a few minutes) …", file=sys.stderr)

    semaphore = asyncio.Semaphore(10)
    nav_info: Dict[int, Tuple[Optional[date], Optional[date], int]] = {}

    async def fetch_one(scheme_code: int) -> None:
        async with semaphore:
            async with httpx.AsyncClient(timeout=30.0) as cl:
                nav_info[scheme_code] = await fetch_nav_dates(cl, scheme_code)

    await asyncio.gather(*[
        fetch_one(int(f["schemeCode"]))
        for f in regular
        if f.get("schemeCode")
    ])

    print(f"  NAV ranges fetched for {len(nav_info)} schemes", file=sys.stderr)

    # ── Find candidates ───────────────────────────────────────────────────────

    candidates: List[Dict[str, Any]] = []

    for house, funds in by_house.items():
        if len(funds) < 2:
            continue

        # Partition into successors (inception 2016–2019) and potential predecessors
        successors    = []
        predecessors  = []

        for f in funds:
            code = int(f.get("schemeCode", 0))
            info = nav_info.get(code)
            if not info or info[0] is None:
                continue
            first_nav, last_nav, n_pts = info

            if RECAT_INCEPTION_START <= first_nav <= RECAT_INCEPTION_END:
                successors.append((code, f["schemeName"], first_nav, last_nav, n_pts))

            if (last_nav is not None
                    and PREDECESSOR_LAST_NAV_START <= last_nav <= PREDECESSOR_LAST_NAV_END
                    and first_nav is not None
                    and (last_nav - first_nav).days / 365.25 >= MIN_PREDECESSOR_HISTORY_YEARS):
                predecessors.append((code, f["schemeName"], first_nav, last_nav, n_pts))

        for s_code, s_name, s_first, s_last, s_pts in successors:
            for p_code, p_name, p_first, p_last, p_pts in predecessors:
                if p_code == s_code:
                    continue
                # Predecessor must end BEFORE or around the successor's inception
                if p_last > s_first:
                    # Allow up to 6 months overlap (recat transition period)
                    overlap_days = (p_last - s_first).days
                    if overlap_days > 180:
                        continue

                sim = keyword_overlap(s_name, p_name)
                if sim < 0.15:
                    continue  # too dissimilar

                pred_years = round((p_last - p_first).days / 365.25, 1)

                candidates.append({
                    "successor_code":   s_code,
                    "successor_name":   s_name,
                    "successor_first":  s_first.isoformat(),
                    "predecessor_code": p_code,
                    "predecessor_name": p_name,
                    "predecessor_first": p_first.isoformat(),
                    "predecessor_last":  p_last.isoformat(),
                    "predecessor_years": pred_years,
                    "similarity":        round(sim, 3),
                    "fund_house":        house,
                })

    # Sort by similarity desc
    candidates.sort(key=lambda x: -x["similarity"])

    # ── Output ────────────────────────────────────────────────────────────────

    print(f"\n{'='*80}")
    print(f"PREDECESSOR CANDIDATES  ({len(candidates)} pairs found)")
    print(f"{'='*80}\n")

    if not candidates:
        print("No candidates found. The NAV date ranges may not match the expected window.")
        return

    for i, c in enumerate(candidates, 1):
        print(f"[{i}]  Similarity: {c['similarity']:.3f}  |  Fund House: {c['fund_house']}")
        print(f"  SUCCESSOR   {c['successor_code']:>7}  {c['successor_name']}")
        print(f"              first NAV: {c['successor_first']}")
        print(f"  PREDECESSOR {c['predecessor_code']:>7}  {c['predecessor_name']}")
        print(f"              {c['predecessor_first']} → {c['predecessor_last']}  ({c['predecessor_years']}Y of history)")
        print()
        print(f"  # Add to amfi_client.py PREDECESSOR_MAP if confirmed:")
        print(f"  PREDECESSOR_MAP[{c['successor_code']}] = [{c['predecessor_code']}]")
        print()


if __name__ == "__main__":
    asyncio.run(main())
