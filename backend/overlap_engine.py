"""overlap_engine.py — pure, DB-free portfolio-overlap math.

Answers one question: *how much of two funds' equity portfolios is the same
security?*

The headline number is **weight-based**, not a count of common names. Counting
names is the naive version and it lies in both directions: two funds sharing
thirty 0.2% micro-positions look "60% similar" while actually diverging, and
two funds whose top five holdings are an identical 40% of each book look
"5 stocks similar" while actually moving together. The measure used here is
the standard minimum-weight overlap:

    overlap % = Σ over common securities of min(weight_A, weight_B)

Read it as: "this share of a rupee invested in A is invested in exactly the
same securities as a rupee invested in B." A stock at 8% in A and 7% in B
contributes 7 points; a stock at 0.5% in both contributes 0.5 — which is the
weighting behaviour the feature is meant to have. It is symmetric
(overlap(A,B) == overlap(B,A)), bounded 0–100, and equals 100 only for
identical books.

    ── Basis: why weights are renormalised to the equity sleeve ──
Monthly disclosures quote "% to Net Assets", which includes debt, REITs, cash
and margin. Comparing those raw would punish a fund merely for holding cash:
a fund 95% in equity and one 70% in equity (30% cash) can hold an *identical*
equity book yet score only ~70% raw overlap. Since the feature's question is
about the equity portfolio, weights are rebased so each fund's equity sleeve
sums to 100 before the min() is taken. The raw as-disclosed weights are
carried through untouched for display, and `overlap_pct_raw` reports the
unrebased number for anyone who wants it.

    ── Matching: ISIN first, name only as a fallback ──
ISIN is the only reliable identity for a security — AMCs spell the same
company a dozen ways ("HDFC Bank Ltd.", "HDFC Bank Limited", "Hdfc Bank").
Names are normalised and used only when one side has no ISIN, which keeps a
sloppily-disclosed file usable without letting name collisions invent
overlap that isn't there.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Corporate suffixes stripped before comparing names. Only ever applied on the
# name-fallback path (see module docstring) — never to ISIN-matched rows.
_NAME_NOISE = re.compile(
    r"\b(limited|ltd|private|pvt|company|co|corporation|corp|"
    r"industries|india|the)\b",
    re.IGNORECASE,
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# A disclosed ISIN is 12 chars: 2-letter country + 9 alphanumerics + check digit.
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


def normalize_isin(raw: Any) -> Optional[str]:
    """Upper-cased ISIN, or None when the value isn't a well-formed ISIN.

    Returning None (rather than the raw string) matters: it routes the row to
    the name-fallback path instead of letting a junk value like "-" or "NA"
    become a match key that two unrelated rows could share.
    """
    s = re.sub(r"\s+", "", str(raw or "")).upper()
    return s if _ISIN_RE.match(s) else None


def normalize_name(raw: Any) -> str:
    """Aggressively-normalised security name for fallback matching."""
    s = str(raw or "").lower()
    s = _NAME_NOISE.sub(" ", s)
    return _NON_ALNUM.sub("", s)


def holding_key(holding: Dict[str, Any]) -> Optional[str]:
    """Identity used to decide whether two rows are the same security.

    ISIN-keyed rows are namespaced under "isin:" and name-keyed under "name:"
    so the two can never collide with each other. A row with neither a usable
    ISIN nor a name returns None and is dropped by the caller.
    """
    isin = normalize_isin(holding.get("isin"))
    if isin:
        return f"isin:{isin}"
    name = normalize_name(holding.get("name"))
    return f"name:{name}" if name else None


def _weight(holding: Dict[str, Any]) -> float:
    """Disclosed weight as a float, treating anything unparseable as 0.

    A holding whose weight cannot be read still counts as a *holding* (it is
    a real position in the book) but contributes nothing to the weighted
    overlap — silently guessing a weight would corrupt the headline number.
    """
    try:
        w = float(holding.get("weight_pct"))
    except (TypeError, ValueError):
        return 0.0
    # Negative weights appear for derivative/short legs in some disclosures.
    # They aren't part of a long equity book and would otherwise subtract
    # from the sleeve total, inflating every other holding on rebasing.
    return w if w > 0 else 0.0


def dedupe_holdings(holdings: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse rows that are the same security into one, summing weights.

    Real disclosures split a single name across lines — a stock held both
    directly and via its ADR, or listed once per exchange/series. Left
    unmerged, each fragment would be compared against the other fund
    separately and min() would pick the smaller fragment, understating the
    overlap.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    for h in holdings:
        key = holding_key(h)
        if key is None:
            continue
        w = _weight(h)
        if key in merged:
            merged[key]["weight_pct"] += w
        else:
            merged[key] = {
                "key":        key,
                "isin":       normalize_isin(h.get("isin")),
                "name":       str(h.get("name") or "").strip(),
                "weight_pct": w,
            }
    return list(merged.values())


def _rebase(holdings: List[Dict[str, Any]]) -> Tuple[Dict[str, float], float]:
    """Map key → equity-sleeve weight (summing to 100), plus the sleeve total.

    The sleeve total is the share of net assets the fund actually has in
    equity; it is returned so the API can show it and so a fund with no
    equity at all can be reported honestly instead of dividing by zero.
    """
    total = sum(h["weight_pct"] for h in holdings)
    if total <= 0:
        return {h["key"]: 0.0 for h in holdings}, 0.0
    return {h["key"]: h["weight_pct"] * 100.0 / total for h in holdings}, total


def compute_overlap(
    holdings_a: Iterable[Dict[str, Any]],
    holdings_b: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compare two equity books.

    Each input row is ``{"isin": str|None, "name": str, "weight_pct": float}``
    where ``weight_pct`` is "% to Net Assets" as disclosed. Callers must pass
    equity rows only — this function has no opinion on what is or isn't
    equity; `holdings_parser` decides that at ingestion.

    Returns the full comparison payload the API and UI need. `common` is
    sorted by each stock's actual contribution to the headline number
    (descending), so the rows that explain the score sit at the top of the
    list the user reads first.
    """
    a = dedupe_holdings(holdings_a)
    b = dedupe_holdings(holdings_b)

    wa, sleeve_a = _rebase(a)
    wb, sleeve_b = _rebase(b)

    by_key_a = {h["key"]: h for h in a}
    by_key_b = {h["key"]: h for h in b}
    common_keys = set(by_key_a) & set(by_key_b)

    common: List[Dict[str, Any]] = []
    overlap_pct = 0.0
    overlap_pct_raw = 0.0
    for key in common_keys:
        ha, hb = by_key_a[key], by_key_b[key]
        contribution = min(wa[key], wb[key])
        overlap_pct += contribution
        overlap_pct_raw += min(ha["weight_pct"], hb["weight_pct"])
        common.append({
            # Prefer whichever side spelled the name more fully; they are the
            # same security by construction, but disclosures abbreviate
            # inconsistently and the longer string is the more useful label.
            "name":            max((ha["name"], hb["name"]), key=len),
            "isin":            ha["isin"] or hb["isin"],
            "weight_a":        round(ha["weight_pct"], 4),
            "weight_b":        round(hb["weight_pct"], 4),
            "weight_a_equity": round(wa[key], 4),
            "weight_b_equity": round(wb[key], 4),
            "contribution":    round(contribution, 4),
            "matched_on":      "isin" if key.startswith("isin:") else "name",
        })

    common.sort(key=lambda r: r["contribution"], reverse=True)

    def _unique(by_key: Dict[str, Dict[str, Any]], rebased: Dict[str, float]) -> List[Dict[str, Any]]:
        rows = [
            {
                "name":          h["name"],
                "isin":          h["isin"],
                "weight":        round(h["weight_pct"], 4),
                "weight_equity": round(rebased[key], 4),
            }
            for key, h in by_key.items() if key not in common_keys
        ]
        rows.sort(key=lambda r: r["weight"], reverse=True)
        return rows

    # How much of EACH fund sits in the shared stocks. Unlike the symmetric
    # headline these differ by fund: a concentrated fund can be 70% inside
    # the overlap while a diversified one is only 35% inside it.
    common_weight_a = sum(by_key_a[k]["weight_pct"] for k in common_keys)
    common_weight_b = sum(by_key_b[k]["weight_pct"] for k in common_keys)
    common_weight_a_eq = sum(wa[k] for k in common_keys)
    common_weight_b_eq = sum(wb[k] for k in common_keys)

    # Jaccard on names, reported alongside the weighted figure purely as
    # context — it is NOT the headline metric (see module docstring).
    union = len(set(by_key_a) | set(by_key_b))

    return {
        "fund_a_holdings":   len(a),
        "fund_b_holdings":   len(b),
        "common_count":      len(common),
        "overlap_pct":       round(min(overlap_pct, 100.0), 2),
        "overlap_pct_raw":   round(min(overlap_pct_raw, 100.0), 2),
        "count_overlap_pct": round(len(common) * 100.0 / union, 2) if union else 0.0,
        "equity_sleeve_a":   round(sleeve_a, 2),
        "equity_sleeve_b":   round(sleeve_b, 2),
        "only_in_a":         len(by_key_a) - len(common_keys),
        "only_in_b":         len(by_key_b) - len(common_keys),
        "common":            common,
        "unique_a":          _unique(by_key_a, wa),
        "unique_b":          _unique(by_key_b, wb),
        "common_weight_a":         round(common_weight_a, 2),
        "common_weight_b":         round(common_weight_b, 2),
        "common_weight_a_equity":  round(min(common_weight_a_eq, 100.0), 2),
        "common_weight_b_equity":  round(min(common_weight_b_eq, 100.0), 2),
    }
