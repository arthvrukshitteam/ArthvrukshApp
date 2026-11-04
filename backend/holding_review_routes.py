"""Portfolio Holding Review API — mounted at /api/portfolio-review by server.py.

Workflow (one document per review, persisted from the first upload):

  POST /upload              PDF → parse → draft review (extracted values + fund matches)
  GET  /                    recent reviews
  GET  /{id}                one review (+ validation)
  PUT  /{id}                advisor edits (client, dates, funds, market, observations)
  POST /{id}/fetch-current  current FQS/WM/XIRR/CV per fund — EXISTING analysis
  POST /{id}/observations/generate
  POST /{id}/finalize       freeze (immutable)
  POST /{id}/revise         new editable draft copied from a finalized review
  GET  /{id}/export.pdf     the report (works for drafts → preview)

No scoring happens here. Like proposal_routes.py this router receives the app's
existing analysis builders/cache lookups as callables from server.py, so FQS in
a review is, by construction, the same FQS a proposal uses
(`proposal_engine.build_fund_row`). Document parsing is review_pdf_parser,
comparison/observation rules are holding_review_engine, PDF is
holding_review_pdf.

Persistence model (extensible for review history / trends): collection
`portfolio_holding_reviews` — one document per review version, linked by
`client.client_key`, `previous_review_id` (the baseline review, when the
baseline was itself a review report) and `parent_review_id` (revision chain).
The uploaded PDF is kept in `portfolio_review_files` keyed by SHA-256.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import difflib
import hashlib
import logging
import re
import uuid
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

import bulk_analysis as ba
import holding_review_engine as hre
import proposal_engine as pe
import review_pdf_parser as rpp
from holding_review_pdf import build_report_pdf
from proposal_routes import VARIANT_PROJECTION, variant_analysis_from_doc

logger = logging.getLogger("mf_api.holding_review")

COLLECTION = "portfolio_holding_reviews"
FILES_COLLECTION = "portfolio_review_files"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
FETCH_CONCURRENCY = 4

AnalysisFn = Callable[..., Awaitable[Dict[str, Any]]]
CacheFn = Callable[[int], Awaitable[Optional[Dict[str, Any]]]]


# ─── request models ───────────────────────────────────────────────────────────

class UploadIn(BaseModel):
    filename: str = "portfolio.pdf"
    content_base64: str


class ClientIn(BaseModel):
    name: Optional[str] = None
    pan: Optional[str] = None
    phone: Optional[str] = None


class PlanIn(BaseModel):
    mode: Optional[str] = Field(None, pattern="^(sip|lumpsum)$")
    goal_years: Optional[int] = Field(None, ge=1, le=50)


class PairIn(BaseModel):
    previous: Optional[float] = None
    current: Optional[float] = None


class MarketIn(BaseModel):
    nifty: Optional[PairIn] = None
    aum: Optional[PairIn] = None


class PreviousIn(BaseModel):
    fqs: Optional[float] = None
    wm_p10: Optional[float] = None
    wm_med: Optional[float] = None
    irr_p10: Optional[float] = None
    irr_med: Optional[float] = None
    cv: Optional[float] = None


class FundIn(BaseModel):
    fid: Optional[str] = None                 # existing row id; omit for a new row
    scheme_code: Optional[int] = None
    rolling_year: Optional[int] = None
    investment_type: Optional[str] = None
    previous: Optional[PreviousIn] = None


class ObservationIn(BaseModel):
    id: Optional[str] = None
    text: str
    included: bool = True


class ReviewPatch(BaseModel):
    client: Optional[ClientIn] = None
    previous_review_date: Optional[str] = None
    current_review_date: Optional[str] = None
    plan: Optional[PlanIn] = None
    market: Optional[MarketIn] = None
    quarters_completed: Optional[int] = Field(None, ge=0, le=500)
    funds: Optional[List[FundIn]] = None
    observations: Optional[List[ObservationIn]] = None
    confirm_all: bool = False                 # advisor has checked every extracted value


# ─── helpers ──────────────────────────────────────────────────────────────────

_PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")


def client_key(name: Optional[str], pan: Optional[str]) -> Optional[str]:
    """Stable identity for grouping a client's reviews: the PAN when it is a
    well-formed one, else the normalised name. Sensitive — never logged."""
    p = (pan or "").strip().upper()
    if _PAN_RE.match(p):
        return f"pan:{p}"
    n = re.sub(r"\s+", " ", (name or "").strip().lower())
    return f"name:{n}" if n else None


def mask_pan(pan: Optional[str]) -> Optional[str]:
    p = (pan or "").strip()
    return None if not p else (("•" * (len(p) - 4) + p[-4:]) if len(p) > 4 else p)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_fund(**kw: Any) -> Dict[str, Any]:
    f: Dict[str, Any] = {
        "fid": uuid.uuid4().hex[:10], "fund_name_pdf": None, "scheme_code": None, "scheme_name": None,
        "category": None, "rolling_year": None, "investment_type": "SIP", "allocation": None,
        "match": {"method": None, "confidence": None, "candidates": []},
        "previous": {m: None for m in hre.METRICS},
        "current": None, "current_status": None, "current_error": None,
        "changes": {}, "status": "extracted", "issues": [],
    }
    f.update(kw)
    return f


def _prev_from_proposal_row(r: Dict[str, Any]) -> Dict[str, Any]:
    ok = r.get("available", True)
    g = (lambda k: r.get(k)) if ok else (lambda k: None)
    return {"fqs": g("fqs"), "wm_p10": g("p10_wm"), "wm_med": g("median_wm"),
            "irr_p10": g("xirr_p10"), "irr_med": g("xirr_median"), "cv": g("cv")}


def _from_proposal_doc(p: Dict[str, Any]) -> Dict[str, Any]:
    """Same shape review_pdf_parser returns, from the stored proposal (exact)."""
    plan = p.get("plan") or {}
    mode = p.get("mode") or "sip"
    funds = []
    for r in plan.get("funds") or []:
        funds.append({
            "fund_name": r.get("scheme_name"), "scheme_code": r.get("scheme_code"),
            "category": r.get("category"), "allocation": r.get("allocation_pct"),
            "rolling_year": (r.get("metrics_horizon") or p.get("horizon")) if r.get("available", True) else p.get("horizon"),
            "previous": _prev_from_proposal_row(r), "unreadable": [], "missing": [],
        })
    return {
        "client": {"name": p.get("client_name"), "pan": p.get("client_pan"), "phone": p.get("client_phone")},
        "plan": {"mode": mode, "goal_years": p.get("horizon"), "amount": plan.get("investment_amount"), "arn": None},
        "proposal_date": p.get("proposal_date"), "funds": funds, "warnings": [], "needs_review": [],
        "since_2010": bool(p.get("since_2010")), "separate_management": bool(p.get("separate_management")),
    }


def _from_review_doc(r: Dict[str, Any]) -> Dict[str, Any]:
    """A finalized/previous review becomes the next baseline: its CURRENT
    figures are the new PREVIOUS figures."""
    funds = [{
        "fund_name": f.get("scheme_name") or f.get("fund_name_pdf"), "scheme_code": f.get("scheme_code"),
        "category": f.get("category"), "allocation": f.get("allocation"),
        "rolling_year": f.get("rolling_year"), "investment_type": f.get("investment_type"),
        "previous": {m: (f.get("current") or {}).get(m) for m in hre.METRICS}, "unreadable": [], "missing": [],
    } for f in r.get("funds") or []]
    mk = hre.summarize_market(r.get("market") or {})
    return {
        "client": dict(r.get("client") or {}), "plan": dict(r.get("plan") or {}),
        "proposal_date": r.get("current_review_date"), "funds": funds, "warnings": [], "needs_review": [],
        "since_2010": bool(r.get("since_2010")), "separate_management": bool(r.get("separate_management")),
        "market_previous": {"nifty": mk["nifty"]["current"], "aum": mk["aum"]["current"]},
        "quarters_completed": (r.get("quarters_completed") or 0) + 1,
    }


async def _resolve_fund(db, idx, name: str) -> Dict[str, Any]:
    """Safe name → scheme matching. Returns {scheme_code, scheme_name, match}.

    High confidence: exact normalised-name hit. Medium: unique stem/lineage hit
    (deterministic, but the advisor is asked to confirm). Otherwise NO code is
    assigned — close candidates are offered with Low confidence and the advisor
    must pick one."""
    by_code, by_norm, by_stem = idx
    name = (name or "").strip()
    if not name:
        return {"scheme_code": None, "scheme_name": None, "match": {"method": None, "confidence": None, "candidates": []}}
    import lineage_store
    norm = lineage_store.normalise_name(name)
    exact = ba.resolve_variants(by_norm.get(norm) or [], by_code)
    if exact is not None:
        return {"scheme_code": exact, "scheme_name": by_code[exact],
                "match": {"method": "exact_name", "confidence": "high", "candidates": []}}
    code = await ba.match_fund(db, by_code, by_norm, by_stem, name_val=name)
    if code is not None:
        return {"scheme_code": code, "scheme_name": by_code.get(code),
                "match": {"method": "normalised_or_lineage", "confidence": "medium", "candidates": []}}
    stems = list(by_stem.keys())
    close = difflib.get_close_matches(ba.match_key(name), stems, n=4, cutoff=0.6)
    cands = []
    for s in close:
        c = ba.resolve_variants(by_stem[s], by_code) or sorted(by_stem[s])[0]
        cands.append({"scheme_code": c, "scheme_name": by_code[c],
                      "similarity": round(difflib.SequenceMatcher(None, ba.match_key(name), s).ratio(), 3)})
    return {"scheme_code": None, "scheme_name": None,
            "match": {"method": "fuzzy" if cands else "none", "confidence": "low", "candidates": cands}}


def _recompute(doc: Dict[str, Any]) -> None:
    for f in doc.get("funds") or []:
        applicable = f.get("investment_type") not in hre.NO_PROPOSAL_DATA
        f["changes"] = hre.compute_changes(f.get("previous") if applicable else {}, f.get("current") or {})
    doc["market_summary"] = hre.summarize_market(doc.get("market") or {})
    doc["type_of_investment"] = hre.investment_type_summary(doc.get("funds") or [])


def _public(doc: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["validation"] = hre.validate_review(out)
    return out


def _summary_row(d: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "review_id": d["review_id"], "status": d.get("status"),
        "client_name": (d.get("client") or {}).get("name"),
        "pan_masked": mask_pan((d.get("client") or {}).get("pan")),
        "previous_review_date": d.get("previous_review_date"),
        "current_review_date": d.get("current_review_date"),
        "fund_count": len(d.get("funds") or []), "version": d.get("version", 1),
        "updated_at": d.get("updated_at"),
    }


# ─── router ───────────────────────────────────────────────────────────────────

def build_router(
    db,
    build_sip_analysis: AnalysisFn,
    cached_sip_analysis: CacheFn,
    build_lumpsum_analysis: AnalysisFn,
    cached_lumpsum_analysis: CacheFn,
) -> APIRouter:
    router = APIRouter(prefix="/api/portfolio-review", tags=["portfolio-review"])
    coll = db[COLLECTION]

    async def _load(review_id: str) -> Dict[str, Any]:
        doc = await coll.find_one({"_id": review_id})
        if not doc:
            raise HTTPException(404, "Review not found")
        return doc

    def _editable(doc: Dict[str, Any]) -> None:
        if doc.get("status") == "finalized":
            raise HTTPException(409, "This review is finalized and cannot be edited. Use 'Revise' to create an editable copy.")

    async def _save(doc: Dict[str, Any]) -> Dict[str, Any]:
        _recompute(doc)
        doc["updated_at"] = _now()
        await coll.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        return doc

    # ── upload ────────────────────────────────────────────────────────────────
    @router.post("/upload")
    async def upload(body: UploadIn) -> Dict[str, Any]:
        try:
            content = base64.b64decode(body.content_base64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(400, "content_base64 is not valid base64.")
        if not content:
            raise HTTPException(400, "The uploaded file is empty.")
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"File too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)}MB).")
        if not content.lstrip()[:5] == b"%PDF-":
            raise HTTPException(400, "Only PDF files are supported.")

        ref = rpp.read_reference(content)
        parsed: Optional[Dict[str, Any]] = None
        source: Dict[str, Any] = {"parse_method": "pdf_text", "reference": ref}
        previous_review_id: Optional[str] = None
        try:
            if ref and ref["kind"] == "proposal":
                p = await db.portfolio_proposals.find_one({"_id": ref["id"]}, {"_id": 0})
                if p:
                    parsed, source["parse_method"] = _from_proposal_doc(p), "embedded_reference"
                    source["proposal_id"] = ref["id"]
            elif ref and ref["kind"] == "review":
                r = await coll.find_one({"_id": ref["id"]})
                if r:
                    parsed, source["parse_method"] = _from_review_doc(r), "embedded_reference"
                    previous_review_id = ref["id"]
            if parsed is None:
                parsed = await asyncio.to_thread(rpp.parse_proposal_pdf, content)
        except rpp.PdfParseError as exc:
            raise HTTPException(422, str(exc))
        except Exception:
            logger.exception("portfolio-review: unexpected parse failure")
            raise HTTPException(422, "Couldn't read this PDF. Upload the original proposal PDF generated by ArthVruksh.")

        sha = hashlib.sha256(content).hexdigest()
        await db[FILES_COLLECTION].update_one(
            {"_id": sha},
            {"$setOnInsert": {"filename": body.filename[:200], "size": len(content), "data": content, "created_at": _now()}},
            upsert=True,
        )
        source.update({"filename": body.filename[:200], "sha256": sha})

        idx = await ba.build_master_index()
        funds: List[Dict[str, Any]] = []
        for pf in parsed["funds"]:
            f = _new_fund(
                fund_name_pdf=pf.get("fund_name"), category=pf.get("category"), allocation=pf.get("allocation"),
                rolling_year=pf.get("rolling_year"),
                investment_type=pf.get("investment_type") or ("Lumpsum" if (parsed["plan"].get("mode") == "lumpsum") else "SIP"),
                previous={m: (pf.get("previous") or {}).get(m) for m in hre.METRICS},
            )
            if pf.get("scheme_code"):                       # exact, from a stored document
                nm = idx[0].get(int(pf["scheme_code"]))
                f.update(scheme_code=int(pf["scheme_code"]), scheme_name=nm or pf.get("fund_name"),
                         match={"method": "stored_reference", "confidence": "high", "candidates": []})
            else:
                f.update(await _resolve_fund(db, idx, pf.get("fund_name") or ""))
            issues = list(pf.get("unreadable") or [])
            if f["match"]["confidence"] != "high":
                issues.append("fund_match")
            f["issues"] = issues
            prev_vals = list(f["previous"].values())
            if issues:
                f["status"] = "needs_review"
            elif all(v is None for v in prev_vals):
                f["status"] = "missing"
            funds.append(f)

        client = parsed["client"]
        key = client_key(client.get("name"), client.get("pan"))
        quarters = parsed.get("quarters_completed")
        if quarters is None:
            quarters = (await coll.count_documents({"client.client_key": key, "status": "finalized"}) + 1) if key else 1
        review_id = uuid.uuid4().hex[:16]
        market = {"nifty": {"previous": (parsed.get("market_previous") or {}).get("nifty"), "current": None},
                  "aum": {"previous": (parsed.get("market_previous") or {}).get("aum"), "current": None}}
        doc: Dict[str, Any] = {
            "_id": review_id, "review_id": review_id, "status": "draft", "version": 1,
            "parent_review_id": None, "previous_review_id": previous_review_id,
            "client": {**client, "client_key": key},
            "previous_review_date": parsed.get("proposal_date"),
            "current_review_date": date.today().isoformat(),
            "plan": parsed["plan"], "market": market, "quarters_completed": quarters,
            "since_2010": parsed.get("since_2010", False),
            "separate_management": parsed.get("separate_management", False),
            "funds": funds, "observations": [], "review_flags": list(parsed.get("needs_review") or []),
            "source": source, "parse_warnings": parsed.get("warnings") or [],
            "confirmed": False, "data_as_of": None, "current_fetched_at": None,
            "created_at": _now(),
        }
        await _save(doc)
        return _public(doc)

    # ── list / get ────────────────────────────────────────────────────────────
    @router.get("")
    async def list_reviews(limit: int = Query(30, ge=1, le=100)) -> Dict[str, Any]:
        cur = coll.find({}, {"funds": 1, "client": 1, "status": 1, "review_id": 1, "previous_review_date": 1,
                             "current_review_date": 1, "updated_at": 1, "version": 1}).sort("updated_at", -1).limit(limit)
        rows = [_summary_row(d) async for d in cur]
        return {"count": len(rows), "results": rows}

    @router.get("/{review_id}")
    async def get_review(review_id: str) -> Dict[str, Any]:
        return _public(await _load(review_id))

    # ── edit ──────────────────────────────────────────────────────────────────
    @router.put("/{review_id}")
    async def update_review(review_id: str, patch: ReviewPatch) -> Dict[str, Any]:
        doc = await _load(review_id)
        _editable(doc)
        data = patch.model_dump(exclude_unset=True)

        if "client" in data:
            c = dict(doc["client"]); c.update({k: v for k, v in data["client"].items()})
            c["client_key"] = client_key(c.get("name"), c.get("pan"))
            doc["client"] = c
            doc["review_flags"] = [f for f in doc.get("review_flags", []) if not (f == "client.name" and c.get("name"))]
        for k in ("previous_review_date", "current_review_date"):
            if k in data:
                if data[k] and not _valid_date(data[k]):
                    raise HTTPException(422, f"{k} must be YYYY-MM-DD")
                doc[k] = data[k]
                if k == "previous_review_date" and data[k]:
                    doc["review_flags"] = [f for f in doc.get("review_flags", []) if f != "proposal_date"]
        if "plan" in data:
            doc["plan"] = {**doc["plan"], **{k: v for k, v in data["plan"].items() if v is not None}}
            doc["review_flags"] = [f for f in doc.get("review_flags", []) if not f.startswith("plan.")]
        if "market" in data:
            m = doc.get("market") or {}
            for key, pair in (data["market"] or {}).items():
                m[key] = {**(m.get(key) or {}), **(pair or {})}
            doc["market"] = m
        if "quarters_completed" in data:
            doc["quarters_completed"] = data["quarters_completed"]

        if "funds" in data:
            idx = await ba.build_master_index()
            existing = {f["fid"]: f for f in doc["funds"]}
            new_list: List[Dict[str, Any]] = []
            for fin in data["funds"] or []:
                f = existing.get(fin.get("fid") or "")
                is_new = f is None
                if is_new:
                    f = _new_fund(investment_type=fin.get("investment_type") or "COB")
                    f["status"] = "verified"
                changed = False
                if fin.get("investment_type") and fin["investment_type"] != f["investment_type"]:
                    f["investment_type"], changed = fin["investment_type"], True
                if fin.get("rolling_year") is not None and fin["rolling_year"] != f["rolling_year"]:
                    f["rolling_year"], changed = fin["rolling_year"], True
                    f["current"], f["current_status"] = None, None     # stale at the old horizon
                if fin.get("scheme_code") is not None and fin["scheme_code"] != f["scheme_code"]:
                    nm = idx[0].get(fin["scheme_code"])
                    if nm is None:
                        raise HTTPException(422, f"Unknown scheme code {fin['scheme_code']}.")
                    f.update(scheme_code=fin["scheme_code"], scheme_name=nm,
                             match={"method": "advisor_selected", "confidence": "high", "candidates": []},
                             current=None, current_status=None, current_error=None)
                    changed = True
                if fin.get("previous") is not None:
                    newprev = {m: fin["previous"].get(m) for m in hre.METRICS}
                    if newprev != f["previous"]:
                        f["previous"], changed = newprev, True
                if changed or is_new:
                    f["status"], f["issues"] = "verified", []
                new_list.append(f)
            doc["funds"] = new_list

        if "observations" in data:
            old = {o["id"]: o for o in doc.get("observations", [])}
            obs = []
            for o in data["observations"] or []:
                prev = old.get(o.get("id") or "")
                if prev:
                    edited = prev["text"] != o["text"]
                    obs.append({**prev, "text": o["text"], "included": o.get("included", True),
                                "source": "manual" if edited else prev["source"]})
                else:
                    obs.append({"id": uuid.uuid4().hex[:10], "text": o["text"], "source": "manual",
                                "basis": None, "included": o.get("included", True)})
            doc["observations"] = obs

        if patch.confirm_all:
            doc["confirmed"] = True
            for f in doc["funds"]:
                if f.get("scheme_code") is not None:
                    f["status"], f["issues"] = "verified", []
            doc["review_flags"] = []
        return _public(await _save(doc))

    # ── current data (existing analysis — no scoring here) ───────────────────
    async def _analysis_for(code: int, mode: str, horizon: int, s2010: bool, sep: bool) -> Dict[str, Any]:
        if s2010 or sep:
            d = await db.fund_nav_meta.find_one({"scheme_code": code}, VARIANT_PROJECTION)
            v = variant_analysis_from_doc(d, mode, s2010, sep)
            if v is not None:
                return v
        if mode == "sip":
            return (await cached_sip_analysis(code)) or await build_sip_analysis(code, user_years=horizon)
        return (await cached_lumpsum_analysis(code)) or await build_lumpsum_analysis(code, user_years=horizon)

    @router.post("/{review_id}/fetch-current")
    async def fetch_current(review_id: str) -> Dict[str, Any]:
        doc = await _load(review_id)
        _editable(doc)
        mode = (doc.get("plan") or {}).get("mode") or "sip"
        sem = asyncio.Semaphore(FETCH_CONCURRENCY)
        as_of: List[str] = []

        async def one(f: Dict[str, Any]) -> None:
            f["current_error"] = None
            if f.get("scheme_code") is None:
                f["current"], f["current_status"], f["current_error"] = None, "error", "Fund is not mapped to a scheme."
                return
            async with sem:
                try:
                    a = await _analysis_for(f["scheme_code"], mode, f["rolling_year"] or 10,
                                            doc.get("since_2010", False), doc.get("separate_management", False))
                    row = pe.build_fund_row(f["scheme_code"], f.get("scheme_name") or "", a.get("category", ""),
                                            0.0, a, f["rolling_year"] or 10, mode)
                except HTTPException as exc:
                    f["current"], f["current_status"], f["current_error"] = None, "error", str(exc.detail)
                    return
                except Exception as exc:
                    logger.warning("portfolio-review: current data failed for scheme %s: %s", f["scheme_code"], exc)
                    f["current"], f["current_status"], f["current_error"] = None, "error", "Current data could not be calculated."
                    return
            f["category"] = a.get("category") or f.get("category")
            if a.get("last_month"):
                as_of.append(a["last_month"])
            if row.median_wm is None:
                f["current"], f["current_status"] = None, "unavailable"
                return
            f["current"] = {"fqs": row.fqs, "wm_p10": row.p10_wm, "wm_med": row.median_wm,
                            "irr_p10": row.xirr_p10, "irr_med": row.xirr_median, "cv": row.cv,
                            "horizon": row.metrics_horizon, "n_windows": row.n_windows}
            f["current_status"] = "ok"

        await asyncio.gather(*[one(f) for f in doc["funds"]])
        doc["data_as_of"] = max(as_of) if as_of else doc.get("data_as_of")
        doc["current_fetched_at"] = _now()
        return _public(await _save(doc))

    @router.post("/{review_id}/observations/generate")
    async def gen_observations(review_id: str) -> Dict[str, Any]:
        doc = await _load(review_id)
        _editable(doc)
        _recompute(doc)
        doc["observations"] = hre.merge_observations(doc.get("observations") or [], hre.generate_observations(doc["funds"]))
        return _public(await _save(doc))

    # ── lifecycle ─────────────────────────────────────────────────────────────
    @router.post("/{review_id}/finalize")
    async def finalize(review_id: str) -> Dict[str, Any]:
        doc = await _load(review_id)
        _editable(doc)
        v = hre.validate_review(doc)
        problems = list(v["errors"])
        if any(f.get("current_status") is None for f in doc["funds"]):
            problems.append("Fetch current data before finalizing.")
        if problems:
            raise HTTPException(422, " ".join(problems))
        doc["status"], doc["finalized_at"] = "finalized", _now()
        return _public(await _save(doc))

    @router.post("/{review_id}/revise")
    async def revise(review_id: str) -> Dict[str, Any]:
        src = await _load(review_id)
        new_id = uuid.uuid4().hex[:16]
        doc = {**src, "_id": new_id, "review_id": new_id, "status": "draft", "parent_review_id": review_id,
               "version": src.get("version", 1) + 1, "created_at": _now()}
        doc.pop("finalized_at", None)
        return _public(await _save(doc))

    @router.get("/{review_id}/export.pdf")
    async def export_pdf(review_id: str) -> Response:
        doc = await _load(review_id)
        errs = hre.validate_review(doc)["errors"]
        if errs:
            raise HTTPException(422, " ".join(errs))
        doc = {k: v for k, v in doc.items() if k != "_id"}
        _recompute(doc)
        content = await asyncio.to_thread(build_report_pdf, doc)
        c = doc.get("client") or {}
        stem = "_".join(p for p in (
            re.sub(r"[^A-Za-z0-9]+", "_", c.get("name") or "").strip("_"), "Holding_Review",
            (doc.get("current_review_date") or ""),
        ) if p)
        return Response(content=content, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{stem}.pdf"'})

    return router


def _valid_date(s: str) -> bool:
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False
