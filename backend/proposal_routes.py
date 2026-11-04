"""Portfolio Proposal API — mounted at /api/proposal by server.py.

Builds a single-mode (SIP-only or Lumpsum-only, matching whichever mode the
advisor was in on Home), single-horizon (the advisor's own tenure) portfolio
projection plus an SWP withdrawal table, by blending each selected fund's
EXISTING analysis. This module contains NO fund-analysis logic of its own —
see proposal_engine.py's docstring. It receives the app's existing analysis
builders/cache-lookups as plain callables from server.py (matching this
project's "no DI framework, db/helpers passed explicitly" convention) so it
never re-implements NAV fetch, classification, or scoring, and never needs to
import server.py (which would be circular, since server.py imports this
module's router factory).
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

import proposal_engine as pe
from continuity_select import select_horizon_map, select_horizons_list
from proposal_pdf import build_pdf
from proposal_xlsx import build_workbook

logger = logging.getLogger("mf_api.proposal")

MIN_HORIZON = 1
MAX_HORIZON = 50

AnalysisFn = Callable[..., Awaitable[Dict[str, Any]]]
CacheFn = Callable[[int], Awaitable[Optional[Dict[str, Any]]]]


# ─── Request models ───────────────────────────────────────────────────────────

class ProposalFundInput(BaseModel):
    scheme_code: int
    allocation_pct: float = Field(..., gt=0, le=1, description="0..1, e.g. 0.25 = 25%")
    # Per-fund scoring basis: the start-date filter (Fund Detail) or AMC
    # period (Home card) that was on screen when the advisor added the fund.
    # Without it the proposal re-scored the fund since inception and showed
    # different FQS/XIRR from what the advisor had just been looking at.
    start_date: Optional[str] = Field(None, description="YYYY-MM-DD; score this fund from this date")
    include_predecessor: bool = True
    # True when the basis above was chosen for this fund explicitly (Fund
    # Detail, or a Home card's AMC period) — then it is used as-is, even
    # "since inception", instead of the Home-wide 2010/Management toggles.
    explicit_basis: bool = False

    @field_validator("start_date")
    @classmethod
    def _validate_start_date(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            return None
        try:
            parsed = date.fromisoformat(v)
        except (TypeError, ValueError):
            raise ValueError("start_date must be YYYY-MM-DD")
        if parsed > datetime.now(timezone.utc).date():
            raise ValueError("start_date cannot be in the future")
        return parsed.isoformat()

    @property
    def has_custom_basis(self) -> bool:
        return self.explicit_basis or self.start_date is not None or not self.include_predecessor


class SwpInput(BaseModel):
    corpus: Optional[float] = Field(None, description="Omit to auto-derive from the portfolio projection")
    annual_return: Optional[float] = Field(None, description="Omit to auto-derive from the portfolio projection")
    inflation: float = 0.06
    monthly_withdrawal: float = Field(..., gt=0)
    years: int = Field(10, ge=1, le=40)


class ProposalRequest(BaseModel):
    # When provided, the build upserts THIS id instead of minting a new one —
    # used by the Under Review screen to recalculate in place (same proposal,
    # same URL/exports) after the advisor edits values.
    proposal_id: Optional[str] = None
    client_name: Optional[str] = None
    client_phone: Optional[str] = None
    client_pan: Optional[str] = None
    advisor_name: Optional[str] = None
    proposal_date: Optional[str] = None  # YYYY-MM-DD, defaults to today (UTC)

    mode: Literal["sip", "lumpsum"]
    horizon: int = Field(..., ge=MIN_HORIZON, le=MAX_HORIZON, description="Single horizon — the advisor's own tenure")
    investment_amount: float = Field(..., gt=0, description="Monthly amount if mode=sip, total amount if mode=lumpsum")

    # Echoes whatever the Home screen's toggles were set to when these funds
    # were selected — see _fund_analysis below for why this has to be
    # threaded all the way through instead of being a display-only flag.
    since_2010: bool = False
    separate_management: bool = False

    funds: List[ProposalFundInput]
    # SWP is optional — omit it for a plain accumulation proposal with no
    # withdrawal plan. When None, no SWP section is computed or rendered.
    swp: Optional[SwpInput] = None

    @field_validator("funds")
    @classmethod
    def _validate_funds(cls, v: List[ProposalFundInput]) -> List[ProposalFundInput]:
        if not v:
            raise ValueError("at least one fund is required")
        if len(v) > pe.MAX_FUNDS:
            raise ValueError(f"at most {pe.MAX_FUNDS} funds are supported")
        total = sum(f.allocation_pct for f in v)
        if abs(total - 1.0) > pe.ALLOCATION_TOLERANCE:
            raise ValueError(f"allocations must sum to 100% (got {total * 100:.1f}%)")
        return v


# ─── Since-2010 / continuity variant lookup (pure — no DB) ────────────────────
#
# When the advisor built a fund selection with Since-2010 / Separate
# Management Periods on, the proposal must show the SAME numbers the Home
# screen showed for these funds — not each fund's plain since-inception
# numbers. _build_analysis()/_cached_analysis() only ever compute the plain
# variant (they have no notion of these toggles), so for this case the
# router fetches the fund's fund_nav_meta document directly and hands it to
# this function, which applies the SAME select_horizon_map/select_horizons_list
# precedence home_universe/lumpsum_universe use, then reshapes the result
# into the {scheme_name, category, horizons: [...]} dict shape
# proposal_engine.extract_fund_metrics() expects (identical to what
# _build_analysis()/_build_lumpsum_analysis() return).

VARIANT_PROJECTION = {
    "_id": 0, "scheme_name": 1, "category": 1,
    "fqs_by_horizon": 1, "fqs_by_horizon_2010": 1, "fqs_by_horizon_continuity": 1,
    "horizons": 1, "horizons_2010": 1, "horizons_continuity": 1,
    "continuity_reset_date": 1, "continuity_reason": 1,
}


def variant_analysis_from_doc(
    doc: Optional[Dict[str, Any]], mode: str, since_2010: bool, separate_management: bool,
) -> Optional[Dict[str, Any]]:
    """Build a fund_analysis-shaped dict from an already-fetched
    fund_nav_meta document, honouring the Since-2010/continuity toggle
    precedence. Returns None when there's nothing usable at all — e.g. the
    doc doesn't exist (fund not yet nightly-refreshed) or has no horizons
    in ANY variant — so the caller can fall back to the normal live-compute
    path instead of dropping the fund from the proposal outright."""
    if not doc:
        return None

    if mode == "sip":
        horizon_map = select_horizon_map(doc, since_2010, separate_management)
        if not horizon_map:
            return None
        horizons = [
            {**(entry or {}), "horizon": int(h)}
            for h, entry in horizon_map.items()
        ]
    else:
        horizons = select_horizons_list(doc, since_2010, separate_management)
        if not horizons:
            return None

    return {
        "scheme_name": doc.get("scheme_name"),
        "category": doc.get("category"),
        "horizons": horizons,
    }


# ─── Router factory ────────────────────────────────────────────────────────────

def build_router(
    db,
    build_sip_analysis: AnalysisFn,
    cached_sip_analysis: CacheFn,
    build_lumpsum_analysis: AnalysisFn,
    cached_lumpsum_analysis: CacheFn,
) -> APIRouter:
    router = APIRouter(prefix="/api/proposal", tags=["proposal"])

    async def _variant_fund_analysis(
        scheme_code: int, mode: str, since_2010: bool, separate_management: bool,
    ) -> Optional[Dict[str, Any]]:
        doc = await db.fund_nav_meta.find_one({"scheme_code": scheme_code}, VARIANT_PROJECTION)
        return variant_analysis_from_doc(doc, mode, since_2010, separate_management)

    async def _fund_analysis(
        scheme_code: int, mode: str, horizon: int, since_2010: bool, separate_management: bool,
    ) -> Dict[str, Any]:
        # Cache-first, exactly mirroring /funds/{code}/analyze[/lumpsum] — the
        # per-horizon fqs/success_pct/median_wm/p10_wm fields a cached doc
        # carries don't depend on user_years, so a cache hit is always valid
        # input for the portfolio blend below. This is the plain
        # since-inception path — only used when NEITHER toggle is on, or as
        # a fallback for a fund the variant lookup above couldn't find.
        if mode == "sip":
            cached = await cached_sip_analysis(scheme_code)
            if cached:
                return cached
            return await build_sip_analysis(scheme_code, user_years=horizon)
        cached = await cached_lumpsum_analysis(scheme_code)
        if cached:
            return cached
        return await build_lumpsum_analysis(scheme_code, user_years=horizon)

    async def _custom_basis_analysis(
        scheme_code: int, mode: str, horizon: int, start_date: Optional[str], include_predecessor: bool,
    ) -> Dict[str, Any]:
        """Live computation on the fund's own chosen basis — the same builder
        and arguments /funds/{code}/analyze uses for that filter, so the
        proposal row equals what Fund Detail showed."""
        start_d = date.fromisoformat(start_date) if start_date else None
        builder = build_sip_analysis if mode == "sip" else build_lumpsum_analysis
        return await builder(
            scheme_code, start_date=start_d, user_years=horizon,
            include_predecessor=include_predecessor,
        )

    async def _fund_row(
        scheme_code: int, allocation_pct: float, horizon: int, mode: str,
        since_2010: bool, separate_management: bool,
        start_date: Optional[str] = None, include_predecessor: bool = True,
        explicit_basis: bool = False,
    ) -> pe.FundRow:
        try:
            analysis = None
            if start_date is not None or not include_predecessor:
                # A per-fund basis outranks the Home-wide toggles: it is the
                # more specific choice the advisor made for THIS fund.
                analysis = await _custom_basis_analysis(
                    scheme_code, mode, horizon, start_date, include_predecessor,
                )
            elif explicit_basis:
                # Explicitly "since inception": the plain cached analysis,
                # deliberately skipping the toggle variants below.
                analysis = await _fund_analysis(scheme_code, mode, horizon, since_2010, separate_management)
            if analysis is None and (since_2010 or separate_management):
                analysis = await _variant_fund_analysis(scheme_code, mode, since_2010, separate_management)
            if analysis is None:
                analysis = await _fund_analysis(scheme_code, mode, horizon, since_2010, separate_management)
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("proposal: %s analysis failed for %s", mode, scheme_code)
            raise HTTPException(
                status_code=502, detail=f"{mode} analysis failed for scheme {scheme_code}: {exc}",
            ) from exc
        return pe.build_fund_row(
            scheme_code=scheme_code,
            scheme_name=analysis.get("scheme_name", str(scheme_code)),
            category=analysis.get("category", ""),
            allocation_pct=allocation_pct,
            fund_analysis=analysis,
            horizon=horizon,
            mode=mode,
        )

    @router.post("/build")
    async def build_proposal(req: ProposalRequest) -> Dict[str, Any]:
        horizon = req.horizon
        mode = req.mode

        fund_rows = [
            await _fund_row(
                f.scheme_code, f.allocation_pct, horizon, mode,
                req.since_2010, req.separate_management,
                start_date=f.start_date, include_predecessor=f.include_predecessor,
                explicit_basis=f.explicit_basis,
            )
            for f in req.funds
        ]
        fund_bases = {
            str(f.scheme_code): {
                "start_date": f.start_date,
                "include_predecessor": f.include_predecessor,
                "explicit_basis": f.explicit_basis,
            }
            for f in req.funds if f.has_custom_basis
        }
        projection = pe.build_mode_projection(fund_rows, req.investment_amount, horizon, mode)

        # ── Correctness guard ────────────────────────────────────────────────
        # If NONE of the selected funds have data at this horizon, refuse to
        # build a proposal at all rather than shipping one with Rs. 0 expected
        # wealth and a Systematic Withdrawal Plan that silently assumes a
        # Rs. 0 corpus / 0% return (this happened in production — see
        # proposal_engine.py's module docstring "BUG FIXED" note).
        if pe.all_funds_excluded(projection):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"None of the selected funds have {horizon} years of history in {mode} mode. "
                    f"Choose a shorter tenure or different funds."
                ),
            )

        # SWP is optional. When omitted, the proposal is a plain accumulation
        # plan with no withdrawal projection (doc["swp"] stays None).
        swp_dict: Optional[Dict[str, Any]] = None
        if req.swp is not None:
            defaults = pe.default_swp_inputs(projection)
            corpus = req.swp.corpus if req.swp.corpus is not None else defaults["corpus"]
            annual_return = req.swp.annual_return if req.swp.annual_return is not None else defaults["annual_return"]

            if corpus is None or annual_return is None:
                # This should be rare now that all_funds_excluded() is checked
                # above, but stays as a second line of defense: never silently
                # substitute 0 for a corpus/return that couldn't be derived.
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Couldn't auto-derive an SWP corpus/return from this portfolio at this horizon. "
                        "Please provide swp.corpus and swp.annual_return explicitly."
                    ),
                )

            swp_projection = pe.build_swp_projection(
                corpus=corpus,
                annual_return=annual_return,
                inflation=req.swp.inflation,
                monthly_withdrawal=req.swp.monthly_withdrawal,
                years=req.swp.years,
                corpus_auto_derived=req.swp.corpus is None,
                annual_return_auto_derived=req.swp.annual_return is None,
            )
            swp_dict = pe.swp_projection_to_dict(swp_projection, include_rows=True)

        warnings: List[str] = []
        if req.since_2010 or req.separate_management:
            # Never let a global recalculation like this be silent — same
            # principle as the Home screen's own notice strip and the
            # per-fund continuity tag (see home.tsx SortStrip/ContinuityTag).
            toggle_note = (
                "Every fund's history below 2010-01-01 onward"
                if req.since_2010 and not req.separate_management
                else "Funds with a verified management change scored from that break date onward"
                if req.separate_management and not req.since_2010
                else "Since 2010, with a verified management change scored from that break date onward where applicable"
            )
            warnings.append(f"Built with: {toggle_note} (matches the Home screen's 2010/Management toggle at selection time).")
        if fund_bases:
            names = {r.scheme_code: r.scheme_name for r in fund_rows}
            parts = []
            for code_s, b in fund_bases.items():
                label = f"from {b['start_date']}" if b["start_date"] else "since inception"
                if not b["include_predecessor"]:
                    label += ", new management only"
                parts.append(f"{names.get(int(code_s), code_s)} ({label})")
            warnings.append("Scored on the basis chosen when the fund was added: " + "; ".join(parts) + ".")
        if projection.aggregate.extrapolated_funds:
            # Not an error — these are Additional Recommendation funds shown at
            # their own shorter window with wealth extrapolated to the tenure.
            # Surfaced so the advisor knows which rows carry the asterisk.
            short_rows = ", ".join(
                f"{f.scheme_name} ({f.metrics_horizon}Y)"
                for f in projection.funds if f.extrapolated
            )
            warnings.append(
                f"Shown at their own rolling period (shorter than the {horizon}Y tenure); "
                f"wealth extrapolated to {horizon}Y at that period's XIRR: {short_rows}"
            )
        if projection.aggregate.excluded_funds:
            warnings.append(
                f"Excluded from this proposal (insufficient history at {horizon}Y): "
                f"{', '.join(projection.aggregate.excluded_funds)}"
            )

        proposal_id = req.proposal_id or uuid.uuid4().hex[:16]
        proposal_date = req.proposal_date or datetime.now(timezone.utc).date().isoformat()

        doc: Dict[str, Any] = {
            "_id": proposal_id,
            "proposal_id": proposal_id,
            "client_name": req.client_name,
            "client_phone": req.client_phone,
            "client_pan": req.client_pan,
            "advisor_name": req.advisor_name,
            "proposal_date": proposal_date,
            "mode": mode,
            "horizon": horizon,
            "since_2010": req.since_2010,
            "separate_management": req.separate_management,
            "fund_bases": fund_bases,
            "plan": pe.mode_projection_to_dict(projection),
            "swp": swp_dict,
            "warnings": warnings,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        await db.portfolio_proposals.update_one({"_id": proposal_id}, {"$set": doc}, upsert=True)

        response_doc = {k: v for k, v in doc.items() if k != "_id"}
        return response_doc

    @router.get("/{proposal_id}")
    async def get_proposal(proposal_id: str) -> Dict[str, Any]:
        doc = await db.portfolio_proposals.find_one({"_id": proposal_id}, {"_id": 0})
        if not doc:
            raise HTTPException(status_code=404, detail="Proposal not found")
        return doc

    @router.get("/{proposal_id}/export.xlsx")
    async def export_xlsx(proposal_id: str) -> Response:
        doc = await db.portfolio_proposals.find_one({"_id": proposal_id}, {"_id": 0})
        if not doc:
            raise HTTPException(status_code=404, detail="Proposal not found")
        content = build_workbook(doc)
        return Response(
            content=content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{_safe_filename(doc, "xlsx")}"'},
        )

    @router.get("/{proposal_id}/export.pdf")
    async def export_pdf(proposal_id: str) -> Response:
        doc = await db.portfolio_proposals.find_one({"_id": proposal_id}, {"_id": 0})
        if not doc:
            raise HTTPException(status_code=404, detail="Proposal not found")
        content = build_pdf(doc)
        return Response(
            content=content,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{_safe_filename(doc, "pdf")}"'},
        )

    return router


def _slug(value: Any) -> str:
    """Filename-safe token: alphanumerics, spaces->underscores, nothing else.

    WhatsApp/Gmail mangle or truncate names with punctuation, so anything that
    isn't [A-Za-z0-9 _-] is dropped rather than escaped.
    """
    s = "".join(c if c.isalnum() or c in (" ", "_", "-") else "" for c in str(value or "")).strip()
    while "  " in s:
        s = s.replace("  ", " ")
    return s.replace(" ", "_")


def _safe_filename(doc: Dict[str, Any], ext: str) -> str:
    """ClientName_PAN_TENURE_MODE_AMOUNT.ext — the name the advisor sees when
    the file lands in a WhatsApp chat, so the whole proposal is identifiable
    without opening it. Missing parts are simply omitted (no empty "__" gaps).

    e.g. Priya_Ravi_ABCDE1234F_15Y_SIP_10000.pdf
    """
    plan = doc.get("plan") or {}
    amount = plan.get("investment_amount")
    horizon = doc.get("horizon")

    parts = [
        _slug(doc.get("client_name")),
        _slug(doc.get("client_pan")).upper(),
        f"{int(horizon)}Y" if horizon else "",
        (doc.get("mode") or "").upper(),                      # SIP | LUMPSUM
        f"{int(round(float(amount)))}" if amount else "",
    ]
    stem = "_".join(p for p in parts if p) or "Portfolio_Proposal"
    return f"{stem}.{ext}"
