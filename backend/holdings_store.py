"""holdings_store.py — Mongo persistence for fund equity holdings.

Collection: ``fund_holdings``, one document per (scheme_code, period)::

    {
      "_id":          "119551:2026-08",
      "scheme_code":  119551,
      "scheme_name":  "Parag Parikh Flexi Cap Fund - Growth",
      "period":       "2026-08",          # YYYY-MM of the disclosure
      "amc":          "PPFAS Mutual Fund",
      "source_url":   "https://…/monthly-portfolio-aug-2026.xlsx",
      "equity_count": 62,
      "equity_weight": 82.4,              # % of net assets in equity
      "holdings": [ {"name","isin","weight_pct","quantity","value"}, … ],
      "fetched_at":  "2026-09-02T…Z",
    }

    ── Why the period is in the _id ──
Disclosures are monthly and the interesting question ("what did these two
funds hold *at the same time*") is only answerable if old months are kept
rather than overwritten. Keying on scheme+period makes ingestion idempotent
— re-running a month replaces exactly that month — and lets the overlap API
insist both funds are compared on the SAME period instead of silently
comparing August against May, which would produce a real-looking number that
means nothing.

Holdings are stored as a single embedded array rather than one document per
stock: a portfolio is ~40-80 rows, always read whole, and never queried by
individual stock. One document is one round trip and stays far inside
Mongo's 16MB limit.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

COLLECTION = "fund_holdings"


def doc_id(scheme_code: int, period: str) -> str:
    return f"{int(scheme_code)}:{period}"


async def ensure_indexes(db) -> None:
    """Indexes the read paths need. Safe to call repeatedly (Mongo no-ops on
    an index that already exists), so this is called at startup rather than
    kept as a migration someone has to remember to run."""
    await db[COLLECTION].create_index([("scheme_code", 1), ("period", -1)])
    await db[COLLECTION].create_index([("period", -1)])


async def save_holdings(
    db,
    scheme_code: int,
    period: str,
    holdings: List[Dict[str, Any]],
    scheme_name: str = "",
    amc: str = "",
    source_url: str = "",
) -> Dict[str, Any]:
    """Upsert one scheme-month. Returns the stored summary."""
    equity_weight = round(sum(h.get("weight_pct") or 0.0 for h in holdings), 2)
    doc = {
        "scheme_code":   int(scheme_code),
        "scheme_name":   scheme_name,
        "period":        period,
        "amc":           amc,
        "source_url":    source_url,
        "equity_count":  len(holdings),
        "equity_weight": equity_weight,
        "holdings":      holdings,
        "fetched_at":    datetime.now(timezone.utc).isoformat(),
    }
    await db[COLLECTION].update_one(
        {"_id": doc_id(scheme_code, period)}, {"$set": doc}, upsert=True
    )
    return {k: v for k, v in doc.items() if k != "holdings"}


async def get_holdings(
    db, scheme_code: int, period: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """One scheme's holdings for `period`, or its most recent month when
    `period` is None."""
    if period:
        return await db[COLLECTION].find_one({"_id": doc_id(scheme_code, period)})
    cursor = db[COLLECTION].find({"scheme_code": int(scheme_code)}).sort("period", -1).limit(1)
    docs = [d async for d in cursor]
    return docs[0] if docs else None


async def latest_common_period(db, code_a: int, code_b: int) -> Optional[str]:
    """The newest period for which BOTH funds have a disclosure.

    Comparing two funds on different months is the single easiest way to
    produce a confidently wrong overlap number, so the API resolves the
    period through this rather than taking each fund's own latest.
    """
    async def periods(code: int) -> set:
        cur = db[COLLECTION].find({"scheme_code": int(code)}, {"_id": 0, "period": 1})
        return {d["period"] async for d in cur}

    shared = await periods(code_a) & await periods(code_b)
    return max(shared) if shared else None


async def available_funds(db, period: Optional[str] = None) -> List[Dict[str, Any]]:
    """Funds that have holdings on file — the pick-list the UI's fund
    pickers are populated from, so a user can only choose funds the overlap
    can actually be computed for."""
    match: Dict[str, Any] = {"period": period} if period else {}
    cursor = db[COLLECTION].find(
        match,
        {"_id": 0, "scheme_code": 1, "scheme_name": 1, "period": 1,
         "equity_count": 1, "amc": 1},
    ).sort("period", -1)

    # Collapse to one row per scheme (its newest period) — the picker lists
    # funds, not scheme-months.
    seen: Dict[int, Dict[str, Any]] = {}
    async for d in cursor:
        seen.setdefault(d["scheme_code"], d)
    return sorted(seen.values(), key=lambda d: d.get("scheme_name") or "")


async def coverage(db) -> Dict[str, Any]:
    """What the store actually holds — surfaced on the admin/ingest screen so
    an operator can see coverage rather than guess at it."""
    total = await db[COLLECTION].count_documents({})
    codes = await db[COLLECTION].distinct("scheme_code")
    periods = sorted(await db[COLLECTION].distinct("period"), reverse=True)
    return {
        "documents":     total,
        "funds":         len(codes),
        "periods":       periods[:24],
        "latest_period": periods[0] if periods else None,
    }
