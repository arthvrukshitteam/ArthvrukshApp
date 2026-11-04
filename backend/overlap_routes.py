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
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import holdings_ingest
import holdings_store
from holdings_parser import HoldingsParseError
from overlap_engine import compute_overlap

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 40 * 1024 * 1024


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
        return {
            "period": resolved,
            "fund_a": {
                "scheme_code":  doc_a["scheme_code"],
                "scheme_name":  doc_a.get("scheme_name") or "",
                "equity_count": result["fund_a_holdings"],
                "equity_weight": doc_a.get("equity_weight"),
            },
            "fund_b": {
                "scheme_code":  doc_b["scheme_code"],
                "scheme_name":  doc_b.get("scheme_name") or "",
                "equity_count": result["fund_b_holdings"],
                "equity_weight": doc_b.get("equity_weight"),
            },
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
