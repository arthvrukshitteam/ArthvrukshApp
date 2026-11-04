"""Portfolio Holding Review — pure comparison + observation logic (no DB, no IO).

This module contains NO fund-scoring maths. FQS, WM, XIRR and CV for a fund
always come from the existing engines (`fqs_engine` / `lumpsum_engine` via
`proposal_engine.build_fund_row`), so a review and the proposal it follows can
never disagree about how a fund is scored. What lives here is only:

  * Previous-vs-Current deltas, with the percent / percentage-point distinction
  * the market + AUM summary arithmetic
  * validation of a review document
  * observation generation from the EXISTING FQS action thresholds
    (`bulk_analysis.get_fund_remark` — <75 / 75–<85 / ≥85) — no new rules.

Units: FQS is a 0-100 score; WM is a multiple (x); IRR and CV are FRACTIONS
internally (0.193 = 19.3 %) and shown as percentages.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Dict, List, Optional

import bulk_analysis as _ba

METRICS = ["fqs", "wm_p10", "wm_med", "irr_p10", "irr_med", "cv"]
METRIC_LABEL = {
    "fqs": "FQS", "wm_p10": "WM_P10", "wm_med": "WM_Med",
    "irr_p10": "XIRR_P10", "irr_med": "XIRR_Med", "cv": "CV",
}
# "ratio" metrics: an absolute difference AND a % change are both meaningful.
# "points" metrics are already percentages: only a percentage-POINT difference
# is reported (a "% change of a %" would be misleading).
METRIC_KIND = {"fqs": "ratio", "wm_p10": "ratio", "wm_med": "ratio",
               "irr_p10": "points", "irr_med": "points", "cv": "points"}

INVESTMENT_TYPES = ["SIP", "Lumpsum", "COB", "External"]
# The proposal's own numbers do not exist for funds that were not part of it.
NO_PROPOSAL_DATA = {"COB", "External"}


def metric_change(metric: str, prev: Optional[float], cur: Optional[float]) -> Optional[Dict[str, Any]]:
    """{abs, pct, unit} or None when either side is missing.

    A blank previous value is NEVER treated as zero — no change is reported."""
    if prev is None or cur is None:
        return None
    if METRIC_KIND[metric] == "points":
        return {"abs": round((cur - prev) * 100.0, 4), "pct": None, "unit": "pp"}
    pct = round((cur - prev) / prev * 100.0, 4) if prev > 0 else None
    return {"abs": round(cur - prev, 4), "pct": pct, "unit": "x" if metric.startswith("wm") else "pts"}


def compute_changes(previous: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Optional[Dict[str, Any]]]:
    return {m: metric_change(m, (previous or {}).get(m), (current or {}).get(m)) for m in METRICS}


def pct_change(prev: Optional[float], cur: Optional[float]) -> Optional[float]:
    """Relative change in %, None unless both exist and prev > 0."""
    if prev is None or cur is None or prev <= 0:
        return None
    return round((cur - prev) / prev * 100.0, 2)


def summarize_market(market: Dict[str, Any]) -> Dict[str, Any]:
    """Adds `change_pct` to each {previous, current} pair (advisor-entered)."""
    out: Dict[str, Any] = {}
    for key in ("nifty", "aum"):
        pair = dict((market or {}).get(key) or {})
        pair.setdefault("previous", None)
        pair.setdefault("current", None)
        pair["change_pct"] = pct_change(pair["previous"], pair["current"])
        out[key] = pair
    return out


def investment_type_summary(funds: List[Dict[str, Any]]) -> str:
    present = {f.get("investment_type") for f in funds}
    return "+".join(t for t in INVESTMENT_TYPES if t in present)


def fund_name(f: Dict[str, Any]) -> str:
    return f.get("scheme_name") or f.get("fund_name_pdf") or "this fund"


# ─── validation ───────────────────────────────────────────────────────────────

def _d(v: Any) -> Optional[date]:
    try:
        return date.fromisoformat(v) if v else None
    except (TypeError, ValueError):
        return None


def validate_review(doc: Dict[str, Any], today: Optional[date] = None) -> Dict[str, List[str]]:
    """{errors, warnings}. Errors block generating the report; warnings do not."""
    today = today or date.today()
    errors: List[str] = []
    warnings: List[str] = []

    prev_d, cur_d = _d(doc.get("previous_review_date")), _d(doc.get("current_review_date"))
    if not (doc.get("client") or {}).get("name"):
        errors.append("Client name is required.")
    if prev_d is None:
        errors.append("Previous review date is missing or invalid (use YYYY-MM-DD).")
    if cur_d is None:
        errors.append("Current review date is missing or invalid (use YYYY-MM-DD).")
    if prev_d and cur_d and cur_d < prev_d:
        errors.append("Current review date cannot be before the previous review date.")
    if cur_d and cur_d > today:
        errors.append("Current review date cannot be in the future — current data is only available up to today.")

    funds = doc.get("funds") or []
    if not funds:
        errors.append("The review has no funds.")
    seen: Dict[int, str] = {}
    for f in funds:
        nm = fund_name(f)
        if f.get("investment_type") not in INVESTMENT_TYPES:
            errors.append(f"{nm}: investment type must be one of {', '.join(INVESTMENT_TYPES)}.")
        if f.get("scheme_code") is None:
            errors.append(f"Unable to map fund: {nm}. Choose the matching scheme.")
        elif f["scheme_code"] in seen:
            errors.append(f"{nm} is listed twice (same scheme as {seen[f['scheme_code']]}).")
        else:
            seen[f["scheme_code"]] = nm
        ry = f.get("rolling_year")
        if not isinstance(ry, int) or ry < 1 or ry > 50:
            errors.append(f"{nm}: rolling year must be a whole number from 1 to 50.")
        if f.get("current_status") == "error":
            warnings.append(f"{nm}: current data unavailable — {f.get('current_error') or 'unknown error'}.")
        elif f.get("current_status") == "unavailable":
            warnings.append(f"{nm}: not enough history for current figures at {ry}Y.")
        cur_h = (f.get("current") or {}).get("horizon")
        if cur_h and ry and cur_h != ry:
            warnings.append(f"{nm}: current figures are measured at {cur_h}Y (fund has less history than {ry}Y).")
    if not funds or all(f.get("current_status") is None for f in funds):
        warnings.append("Current data has not been fetched yet.")
    return {"errors": errors, "warnings": warnings}


# ─── observations ─────────────────────────────────────────────────────────────

def _obs(text: str, basis: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": uuid.uuid4().hex[:10], "text": text, "source": "auto", "basis": basis}


def _fmt(v: Optional[float], dp: int = 1) -> str:
    return "–" if v is None else f"{v:.{dp}f}"


def generate_observations(funds: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Auto observations, each carrying the `basis` (rule + the actual numbers)
    it was derived from.

    The only judgement rule is the application's existing one
    (`bulk_analysis.get_fund_remark`): FQS < FQS_BAD_BELOW → review/redeem,
    < FQS_GOOD_FROM → acceptable, better options may exist. Trend sentences
    merely state the measured FQS movement."""
    out: List[Dict[str, Any]] = []

    no_proposal = [fund_name(f) for f in funds if f.get("investment_type") in NO_PROPOSAL_DATA]
    if no_proposal:
        kinds = sorted({f["investment_type"] for f in funds if f.get("investment_type") in NO_PROPOSAL_DATA})
        out.append(_obs(
            f"Since the client has {' and '.join(kinds)} fund(s), Proposal Data as per FQS Model is NOT applicable "
            f"and is kept blank for: {', '.join(no_proposal)}.",
            {"rule": "no_proposal_data", "funds": no_proposal},
        ))

    for f in funds:
        nm = fund_name(f)
        cur = f.get("current") or {}
        prev = f.get("previous") or {}
        fqs, pfqs, cv = cur.get("fqs"), prev.get("fqs"), cur.get("cv")
        if f.get("current_status") in ("error", "unavailable") or fqs is None:
            out.append(_obs(
                f"Current FQS data for {nm} could not be obtained; please review this fund manually.",
                {"rule": "current_unavailable", "scheme_code": f.get("scheme_code")},
            ))
            continue

        remark = _ba.get_fund_remark(fqs)
        cv_txt = f" and volatility (CV) is {_fmt(cv * 100 if cv is not None else None)}%" if cv is not None else ""
        basis = {"rule": "fqs_remark", "scheme_code": f.get("scheme_code"), "fqs": fqs,
                 "previous_fqs": pfqs, "cv": cv, "remark": remark,
                 "bad_below": _ba.FQS_BAD_BELOW, "good_from": _ba.FQS_GOOD_FROM}
        trend = ""
        if pfqs is not None:
            trend = f" Since the previous review its FQS moved from {_fmt(pfqs)} to {_fmt(fqs)}."
        if remark == _ba.REMARK_BAD:
            out.append(_obs(
                f"It is recommended to review/redeem {nm} as its current FQS ({_fmt(fqs)}) is below "
                f"{_ba.FQS_BAD_BELOW}{cv_txt}, and a better fund can be suggested as per the FQS Model.{trend}",
                basis,
            ))
        elif remark == _ba.REMARK_MID:
            out.append(_obs(
                f"{nm} has a current FQS of {_fmt(fqs)} (between {_ba.FQS_BAD_BELOW} and {_ba.FQS_GOOD_FROM}): "
                f"not bad, but better options may be available as per the FQS Model.{trend}",
                basis,
            ))
        elif pfqs is not None and fqs < pfqs:
            out.append(_obs(
                f"The FQS of {nm} has decreased from {_fmt(pfqs)} to {_fmt(fqs)} since the previous review; "
                f"it remains at or above {_ba.FQS_GOOD_FROM} and the fund can be continued.",
                {**basis, "rule": "fqs_decline_still_good"},
            ))
    return out


def merge_observations(existing: List[Dict[str, Any]], generated: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Regenerating replaces auto observations but never touches manual ones."""
    manual = [o for o in (existing or []) if o.get("source") == "manual"]
    return generated + manual
