"""Portfolio Overlap API — mounted at /api/overlap by server.py.

HTTP layer only. The comparison math lives in `overlap_engine` (pure, no DB),
disclosure reading in `holdings_parser`, persistence in `holdings_store` and
fetching in `holdings_ingest` — same "routers are thin, engines are pure"
split the proposal/rolling/bulk-analysis features already use.

    GET  /funds                 -> funds that have holdings on file
    GET  /compare               -> the overlap between two of them
    GET  /coverage              -> what the holdings store contains
    POST /ingest                -> pull disclosure workbooks from URLs
    POST /ingest-upload         -> ingest a disclosure workbook posted as
                                   base64 (same transport rationale as
                                   bulk_analysis_routes' base64 upload route)
    GET  /sources               -> best-effort AMFI disclosure link discovery

The ingest routes are operator tools, not end-user endpoints. They are
mounted on the same public API as everything else in this app — which has no
auth layer at all today — so they are deliberately limited to *pulling* from
a URL and writing to one collection, and they never delete. If auth is added
later these are the routes to put behind it.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import amfi_client
import fund_status
import holdings_ingest
import holdings_store
from holdings_parser import HoldingsParseError
from overlap_engine import compute_overlap

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 40 * 1024 * 1024
_DIRECT_RE = re.compile(r"\bdirect\b", re.IGNORECASE)

# Fields from fund_nav_meta that help an advisor decide WHICH of two
# overlapping funds to keep — read-only, already computed nightly.
_FUND_FACTS_PROJECTION = {
    "_id": 0, "scheme_code": 1, "scheme_name": 1, "fund_house": 1, "category": 1,
    "scheme_category": 1, "headline_fqs": 1, "headline_tier": 1, "headline_horizon": 1,
    "history_years": 1,
}


def _name_query(q: str) -> Dict[str, Any]:
    """Every whitespace token must appear in the name (any order), so
    'hdfc flexi' finds 'HDFC Flexi Cap Fund - Growth'. Tokens are escaped —
    user text is never interpreted as a regex."""
    tokens = [t for t in q.lower().split() if t][:6]
    return {"$and": [{"scheme_name": {"$regex": re.escape(t), "$options": "i"}} for t in tokens]} if tokens else {}


class IngestRequest(BaseModel):
    urls:   List[str] = Field(default_factory=list)
    period: Optional[str] = None


class IngestUploadRequest(BaseModel):
    filename:       str = "disclosure.xlsx"
    content_base64: str
    period:         Optional[str] = None
    amc:            str = ""


def build_router(db) -> APIRouter:
    router = APIRouter(prefix="/api/overlap", tags=["overlap"])

    @router.get("/funds")
    async def funds(period: Optional[str] = None) -> Dict[str, Any]:
        """The pick-list both fund selectors are populated from.

        Deliberately scoped to funds that HAVE holdings rather than the full
        37k AMFI universe: letting a user pick a fund with no disclosure on
        file only buys them an error at the compare step.
        """
        rows = await holdings_store.available_funds(db, period)
        return {"count": len(rows), "period": period, "funds": rows}

    @router.get("/candidates")
    async def candidates(
        q: Optional[str] = Query(None, max_length=80),
        limit: int = Query(60, ge=1, le=200),
    ) -> Dict[str, Any]:
        """Funds the overlap pickers offer: the app's OWN fund universe
        (fund_nav_meta — active regular plans, the same index Home ranks),
        never an arbitrary external name.

        Every row says whether a portfolio disclosure is on file
        (`has_holdings`). Those come first: they are the ones that can be
        compared right now. The rest stay selectable so the advisor can see
        the fund is known and upload its disclosure, rather than the picker
        silently looking empty — which is what made the old picker (fed only
        from fund_holdings) appear broken whenever nothing had been ingested.
        """
        query_text = (q or "").strip()
        holdings_rows = await holdings_store.available_funds(db)
        holdings_by_code = {r["scheme_code"]: r for r in holdings_rows}

        rows: Dict[int, Dict[str, Any]] = {}
        name_filter = _name_query(query_text) if query_text else {}

        # Funds with a disclosure on file, filtered by the same tokens.
        for r in holdings_rows:
            name = r.get("scheme_name") or ""
            if query_text and not all(t in name.lower() for t in query_text.lower().split()):
                continue
            rows[r["scheme_code"]] = {
                "scheme_code": r["scheme_code"], "scheme_name": name,
                "has_holdings": True, "period": r.get("period"),
                "equity_count": r.get("equity_count"), "amc": r.get("amc") or None,
            }

        base = fund_status.merge_query(name_filter or {})
        cursor = (
            db.fund_nav_meta.find(base, _FUND_FACTS_PROJECTION)
            .sort([("headline_fqs", -1), ("scheme_name", 1)])
            .limit(limit * 3)
        )
        async for d in cursor:
            code = d.get("scheme_code")
            name = d.get("scheme_name") or ""
            if code is None or not name or amfi_client.is_idcw_scheme(name) or _DIRECT_RE.search(name):
                continue
            h = holdings_by_code.get(code)
            row = rows.get(code) or {
                "scheme_code": code, "scheme_name": name,
                "has_holdings": bool(h), "period": (h or {}).get("period"),
                "equity_count": (h or {}).get("equity_count"), "amc": None,
            }
            row.update({
                "amc": row.get("amc") or d.get("fund_house") or None,
                "category": d.get("category"),
                "headline_fqs": d.get("headline_fqs"),
            })
            rows[code] = row

        ordered = sorted(
            rows.values(),
            key=lambda r: (0 if r["has_holdings"] else 1,
                           -(r.get("headline_fqs") or 0), r["scheme_name"]),
        )
        with_holdings = sum(1 for r in ordered if r["has_holdings"])
        return {
            "query": query_text,
            "with_holdings": with_holdings,
            "results": ordered[: max(limit, with_holdings if not query_text else limit)],
        }

    @router.get("/coverage")
    async def coverage() -> Dict[str, Any]:
        return await holdings_store.coverage(db)

    @router.get("/compare")
    async def compare(
        fund_a: int = Query(..., description="AMFI scheme code of the first fund"),
        fund_b: int = Query(..., description="AMFI scheme code of the second fund"),
        period: Optional[str] = Query(None, description="YYYY-MM; defaults to the newest month BOTH funds disclose"),
    ) -> Dict[str, Any]:
        if fund_a == fund_b:
            raise HTTPException(400, "Pick two different funds to compare.")

        # Resolving to a period both funds share (rather than each fund's own
        # latest) is what stops the API quietly comparing August against May
        # and returning a confident, meaningless number.
        resolved = period or await holdings_store.latest_common_period(db, fund_a, fund_b)
        if not resolved:
            have_a = await holdings_store.get_holdings(db, fund_a)
            have_b = await holdings_store.get_holdings(db, fund_b)
            missing = [c for c, h in ((fund_a, have_a), (fund_b, have_b)) if not h]
            if missing:
                raise HTTPException(
                    404,
                    f"No portfolio disclosure on file for scheme code(s): "
                    f"{', '.join(str(m) for m in missing)}.",
                )
            raise HTTPException(
                404,
                "These two funds have no disclosure month in common — "
                "ingest the same month for both before comparing.",
            )

        doc_a = await holdings_store.get_holdings(db, fund_a, resolved)
        doc_b = await holdings_store.get_holdings(db, fund_b, resolved)
        if not doc_a or not doc_b:
            raise HTTPException(404, f"No holdings for {resolved} for one of the two funds.")

        result = compute_overlap(doc_a["holdings"], doc_b["holdings"])

        facts: Dict[int, Dict[str, Any]] = {}
        try:
            async for d in db.fund_nav_meta.find(
                {"scheme_code": {"$in": [fund_a, fund_b]}}, _FUND_FACTS_PROJECTION,
            ):
                facts[d["scheme_code"]] = d
        except Exception:
            logger.exception("overlap compare: fund facts lookup failed")

        def _side(doc: Dict[str, Any], equity_count: int, top10: float) -> Dict[str, Any]:
            f = facts.get(doc["scheme_code"], {})
            return {
                "scheme_code":   doc["scheme_code"],
                "scheme_name":   doc.get("scheme_name") or f.get("scheme_name") or "",
                "equity_count":  equity_count,
                "equity_weight": doc.get("equity_weight"),
                "amc":           doc.get("amc") or f.get("fund_house") or None,
                "category":      f.get("category"),
                "headline_fqs":  f.get("headline_fqs"),
                "headline_tier": f.get("headline_tier"),
                "headline_horizon": f.get("headline_horizon"),
                "top10_weight":  round(top10, 2),
            }

        def _top10(holdings: List[Dict[str, Any]]) -> float:
            ws = sorted((float(h.get("weight_pct") or 0) for h in holdings), reverse=True)
            return sum(w for w in ws[:10] if w > 0)

        return {
            "period": resolved,
            "fund_a": _side(doc_a, result["fund_a_holdings"], _top10(doc_a["holdings"])),
            "fund_b": _side(doc_b, result["fund_b_holdings"], _top10(doc_b["holdings"])),
            **result,
        }

    @router.get("/sources")
    async def sources() -> Dict[str, Any]:
        urls = await holdings_ingest.discover_amfi_sources()
        return {
            "index":     holdings_ingest.AMFI_DISCLOSURE_INDEX,
            "count":     len(urls),
            "urls":      urls,
            "registry":  holdings_ingest.AMC_SOURCES,
        }

    @router.post("/ingest")
    async def ingest(req: IngestRequest) -> Dict[str, Any]:
        if not req.urls:
            raise HTTPException(400, "Provide at least one disclosure URL.")
        try:
            period = holdings_ingest.normalize_period(req.period)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return await holdings_ingest.ingest_many(db, req.urls, period)

    @router.post("/ingest-upload")
    async def ingest_upload(req: IngestUploadRequest) -> Dict[str, Any]:
        try:
            period = holdings_ingest.normalize_period(req.period)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        try:
            content = base64.b64decode(req.content_base64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(400, "content_base64 is not valid base64.")
        if not content:
            raise HTTPException(400, "Uploaded file is empty.")
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"File exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)}MB.")
        try:
            return await holdings_ingest.ingest_workbook_bytes(
                db, content, period, amc=req.amc, source_url=f"upload:{req.filename}"
            )
        except HoldingsParseError as exc:
            raise HTTPException(422, str(exc))

    return router
