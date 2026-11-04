"""AMFI / mfapi.in client with simple in-memory + Mongo caching."""

import httpx
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timezone, timedelta
import logging

logger = logging.getLogger(__name__)

MFAPI_BASE = "https://api.mfapi.in/mf"

#
PREDECESSOR_MAP: Dict[int, List[int]] = {
    
    151133: [103006],
}

_FUND_LIST_CACHE: Dict[str, Any] = {"data": None, "fetched_at": None}
_FUND_LIST_TTL = timedelta(hours=12)


async def fetch_fund_list() -> List[Dict[str, Any]]:
    """Return the master AMFI fund list. List of {schemeCode, schemeName}."""
    now = datetime.now(timezone.utc)
    cached = _FUND_LIST_CACHE
    if cached["data"] is not None and cached["fetched_at"] and (now - cached["fetched_at"]) < _FUND_LIST_TTL:
        return cached["data"]

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(MFAPI_BASE)
        r.raise_for_status()
        data = r.json()
    
    # Only keep Regular funds (remove Direct plans)
    data = [
        fund
        for fund in data
        if "direct" not in (fund.get("schemeName") or "").lower()
    ]

    _FUND_LIST_CACHE["data"] = data
    _FUND_LIST_CACHE["fetched_at"] = now
    return data


async def fetch_fund_nav(scheme_code: int) -> Dict[str, Any]:
    """Fetch full NAV history for a single scheme. Returns the mfapi.in payload."""
    url = f"{MFAPI_BASE}/{scheme_code}"
    async with httpx.AsyncClient(timeout=45.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.json()


def parse_nav_history(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Parse mfapi.in payload['data'] (list of {date, nav}) into sorted ascending."""
    raw = payload.get("data", []) or []
    out = []
    for row in raw:
        try:
            d = datetime.strptime(row["date"], "%d-%m-%Y").date()
            nav = float(row["nav"])
            if nav <= 0:
                continue
            out.append({"date": d, "nav": nav})
        except Exception:
            continue
    out.sort(key=lambda x: x["date"])
    return out


async def fetch_nav_with_predecessors(
    scheme_code: int,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Fetch NAV history, prepending any known predecessor scheme histories.

    Returns (meta, stitched_nav_history). The history is sorted ascending by
    date and deduplicated — the primary scheme wins on any date conflict.
    """
    payload = await fetch_fund_nav(scheme_code)
    meta = meta_from_payload(payload)
    primary_history = parse_nav_history(payload)

    predecessors = PREDECESSOR_MAP.get(scheme_code, [])
    if not predecessors or not primary_history:
        return meta, primary_history

    merged: Dict[Any, Dict[str, Any]] = {}

    for pred_code in predecessors:
        try:
            pred_payload = await fetch_fund_nav(pred_code)
            pred_history = parse_nav_history(pred_payload)
            for row in pred_history:
                merged[row["date"]] = row
            logger.info(
                "Stitched %d NAV points from predecessor %s → %s",
                len(pred_history), pred_code, scheme_code,
            )
        except Exception as exc:
            logger.warning(
                "Predecessor %s fetch failed for scheme %s — skipping: %s",
                pred_code, scheme_code, exc,
            )

    # Primary wins on date conflicts (more authoritative for recent data)
    for row in primary_history:
        merged[row["date"]] = row

    stitched = sorted(merged.values(), key=lambda x: x["date"])
    return meta, stitched


def meta_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    m = payload.get("meta", {}) or {}
    return {
        "scheme_code": m.get("scheme_code"),
        "scheme_name": m.get("scheme_name", ""),
        "fund_house": m.get("fund_house", ""),
        "scheme_type": m.get("scheme_type", ""),
        "scheme_category": m.get("scheme_category", ""),
    }
