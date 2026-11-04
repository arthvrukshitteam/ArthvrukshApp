"""AMFI / mfapi.in client with simple in-memory caching.

Two distinct phenomena handled here:
  1. SEBI 2018 recategorisation — renamed funds only, same scheme code.
     mfapi.in already serves full continuous NAV history. No stitching needed.

  2. True mergers — Fund A was legally wound up and unitholders moved to Fund B
     with a DIFFERENT scheme code.  Fund A's scheme code has NAV ending at the
     merger date; Fund B's code has NAV from the merger date onward.
     These need explicit stitching via MERGER_MAP below (~50–150 cases industry-wide).

WOUND_UP_THRESHOLD_DAYS: if a fund's last NAV is older than this many days before
today, it is considered wound up / merged / inactive.
"""

import httpx
import re
from typing import List, Dict, Any, Optional, Tuple, Set
from datetime import datetime, timezone, timedelta, date
import logging

import fund_lineage

logger = logging.getLogger(__name__)


# ── IDCW / dividend-option exclusion ─────────────────────────────────────────
# IDCW (Income Distribution cum Capital Withdrawal, formerly "Dividend")
# plan variants distribute payouts, so their NAV understates true returns and
# every WM/XIRR/FQS computed from it is wrong for a growth investor. They are
# excluded from the app everywhere: universe listings, additional
# recommendations, and search.
#
# "Dividend Yield" is a legitimate EQUITY CATEGORY (the fund invests in
# dividend-paying stocks) — those names are stripped of the category phrase
# before token matching so they are NOT excluded.
_DIVIDEND_YIELD_RE = re.compile(r"dividend[\s-]*yield", re.IGNORECASE)
_IDCW_TOKENS_RE    = re.compile(
    r"(idcw|income distribution|dividend|payout|reinvestment|re-investment)",
    re.IGNORECASE,
)


def is_idcw_scheme(name: Optional[str]) -> bool:
    """True when the scheme name denotes an IDCW/Dividend payout-option plan."""
    if not name:
        return False
    return bool(_IDCW_TOKENS_RE.search(_DIVIDEND_YIELD_RE.sub("", name)))

MFAPI_BASE = "https://api.mfapi.in/mf"
WOUND_UP_THRESHOLD_DAYS = 180  # 6 months

# ── Merger map ────────────────────────────────────────────────────────────────
# Format: { wound_up_scheme_code: successor_scheme_code }
#
# ONLY for TRUE mergers where Fund A was wound up and assets moved to Fund B
# with a different scheme code.  DO NOT add 2018 renaming cases here — those
# already have continuous history under the same code.
#
# Add entries after running scripts/merger_discovery.py and manually verifying
# each pair.  Each entry must be checked:
#   ✓ Fund A's last_nav_date ≈ Fund B's first_nav_date
#   ✓ Fund A and Fund B are from the same fund house
#   ✓ Return correlation ≥ 0.93 in the transition period (if overlap exists)
#   ✓ AMFI merger notice or SEBI circular confirms the merger
#
# Example (commented out — replace with verified real pairs):
#   103006: 151133,  # Old scheme → new scheme after merger
#
# Curated, human-verified pairs live in fund_lineage.CURATED_SUCCESSORS (single
# source of truth, correlation-verified at runtime). MERGER_MAP is kept as a
# name for backward compatibility and seeded from the curated table.
MERGER_MAP: Dict[int, int] = dict(fund_lineage.CURATED_SUCCESSORS)

# Successor pairs discovered automatically (cross-house merger/predecessor
# discovery), loaded from MongoDB at startup and after each discovery run.
# Kept separate from the curated map so it can be refreshed without a redeploy.
# Every entry is still re-verified by NAV correlation before it is trusted
# (see resolve_and_stitch → fund_lineage.verify_stitch).
_DISCOVERED_SUCCESSORS: Dict[int, int] = {}

# Reverse index of _DISCOVERED_SUCCESSORS (successor_code → [predecessor_codes]),
# rebuilt whenever set_discovered_successors() is called. This is what lets
# load_analysis_nav walk BACKWARD from an active/current scheme code to find an
# older code whose history should be prepended (see resolve_predecessors).
_DISCOVERED_PREDECESSORS: Dict[int, List[int]] = {}

# Pairs whose identity is backed by ISIN CONTINUITY (the same security appearing
# under two scheme codes). These have no NAV overlap by construction — the old
# code stops and the new one starts — so correlation is undefined for them and
# verify_stitch would reject every one. Flagging them lets verify_stitch apply
# its seam-continuity test instead. See lineage_store / discover_successors.
_ISIN_VERIFIED_PAIRS: Set[Tuple[int, int]] = set()


def set_isin_verified_pairs(pairs) -> None:
    """Replace the ISIN-verified (predecessor, successor) set."""
    global _ISIN_VERIFIED_PAIRS
    _ISIN_VERIFIED_PAIRS = {(int(p), int(s)) for p, s in pairs}
    logger.info("ISIN-verified successor pairs: %d", len(_ISIN_VERIFIED_PAIRS))


def is_isin_verified(pred: int, succ: int) -> bool:
    return (int(pred), int(succ)) in _ISIN_VERIFIED_PAIRS


def set_discovered_successors(mapping: Dict[int, int]) -> None:
    """Replace the runtime-discovered successor cache (wound_up_code → successor_code)."""
    global _DISCOVERED_SUCCESSORS, _DISCOVERED_PREDECESSORS
    _DISCOVERED_SUCCESSORS = {int(k): int(v) for k, v in mapping.items() if k and v}
    reverse: Dict[int, List[int]] = {}
    for pred, succ in _DISCOVERED_SUCCESSORS.items():
        reverse.setdefault(succ, []).append(pred)
    _DISCOVERED_PREDECESSORS = reverse
    logger.info(
        "Discovered-successor cache set: %d entries (%d reverse successor targets)",
        len(_DISCOVERED_SUCCESSORS), len(_DISCOVERED_PREDECESSORS),
    )


def resolve_successor(scheme_code: int) -> Tuple[Optional[int], bool]:
    """Return (successor_code, is_curated) for a wound-up scheme, or (None, False).

    Curated pairs take precedence over auto-discovered ones.
    """
    code = int(scheme_code)
    if code in MERGER_MAP:
        return MERGER_MAP[code], True
    if code in _DISCOVERED_SUCCESSORS:
        return _DISCOVERED_SUCCESSORS[code], False
    return None, False


def resolve_predecessors(scheme_code: int) -> List[Tuple[int, bool]]:
    """Return [(predecessor_code, is_curated), ...] for schemes that name
    scheme_code as their successor — the reverse of resolve_successor.

    Needed because the app (and users) query the CURRENT/active scheme code,
    not the old wound-up one. An active code can itself be the successor of an
    older code (AMC acquisition, true merger, code change on rename) — without
    this reverse lookup, that earlier history is silently unreachable and the
    active code's own first NAV gets mistaken for the fund's inception.
    Curated pairs are listed first (they are allowed to stitch even with no
    NAV overlap; discovered pairs are not — see fund_lineage.verify_stitch).
    """
    code = int(scheme_code)
    curated_preds = [k for k, v in MERGER_MAP.items() if v == code]
    discovered_preds = [p for p in _DISCOVERED_PREDECESSORS.get(code, []) if p not in curated_preds]
    return [(p, True) for p in curated_preds] + [(p, False) for p in discovered_preds]

# ── Fund list cache ───────────────────────────────────────────────────────────

_FUND_LIST_CACHE: Dict[str, Any] = {"data": None, "fetched_at": None}
_FUND_LIST_TTL = timedelta(hours=12)


async def fetch_fund_list() -> List[Dict[str, Any]]:
    """Return the master AMFI fund list (regular plans only). List of {schemeCode, schemeName}."""
    now = datetime.now(timezone.utc)
    cached = _FUND_LIST_CACHE
    if cached["data"] is not None and cached["fetched_at"] and (now - cached["fetched_at"]) < _FUND_LIST_TTL:
        return cached["data"]

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(MFAPI_BASE)
        r.raise_for_status()
        data = r.json()

    # Only keep Regular funds (remove Direct plans) and drop IDCW/dividend
    # payout-option variants (see is_idcw_scheme).
    data = [
        fund
        for fund in data
        if "direct" not in (fund.get("schemeName") or "").lower()
        and not is_idcw_scheme(fund.get("schemeName"))
    ]

    _FUND_LIST_CACHE["data"] = data
    _FUND_LIST_CACHE["fetched_at"] = now
    return data


# ── NAV fetching ──────────────────────────────────────────────────────────────

async def fetch_fund_nav(scheme_code: int) -> Dict[str, Any]:
    """Fetch full NAV history for a single scheme. Returns the mfapi.in payload."""
    url = f"{MFAPI_BASE}/{scheme_code}"
    async with httpx.AsyncClient(timeout=45.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.json()


# ── NAV series sanitisation ──────────────────────────────────────────────────
# Real funds never move ±4x between consecutive NAV prints a few days apart;
# larger jumps are unit changes (e.g. the ₹10 → ₹1000 face-value change many
# debt plans made in 2012-13) or AMFI data errors. Left uncorrected, a ×100
# unit change reads as a 100× "return" and produces absurd Wealth Multiples
# (e.g. a short-term debt fund showing 47×). We rebase the earlier segment by
# the observed ratio so the series is continuous in return space.
SPLICE_RATIO_THRESHOLD = 4.0
SPLICE_MAX_GAP_DAYS    = 31   # jumps across longer data holes may be real growth


def sanitize_nav_history(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove unit-change discontinuities from an ascending [{date, nav}] series.

    When nav[i]/nav[i-1] ≥ 4 or ≤ 1/4 within a ≤31-day gap, every NAV before
    the jump is multiplied by that ratio (a face-value change is a unit
    conversion — the true return on that day is ~0).
    """
    if len(history) < 2:
        return history
    for i in range(1, len(history)):
        prev, cur = history[i - 1], history[i]
        if prev["nav"] <= 0 or cur["nav"] <= 0:
            continue
        gap_days = (cur["date"] - prev["date"]).days
        if gap_days > SPLICE_MAX_GAP_DAYS:
            continue
        ratio = cur["nav"] / prev["nav"]
        if ratio >= SPLICE_RATIO_THRESHOLD or ratio <= 1.0 / SPLICE_RATIO_THRESHOLD:
            logger.info(
                "NAV splice adjusted: %.4f → %.4f (×%.2f) on %s — rebasing %d earlier points",
                prev["nav"], cur["nav"], ratio, cur["date"], i,
            )
            for j in range(i):
                history[j]["nav"] *= ratio
    return history


def parse_nav_history(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Parse mfapi.in payload['data'] into [{date, nav}] sorted ascending,
    with unit-change discontinuities splice-adjusted (see sanitize_nav_history)."""
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
    return sanitize_nav_history(out)


def meta_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    m = payload.get("meta", {}) or {}
    return {
        "scheme_code": m.get("scheme_code"),
        "scheme_name": m.get("scheme_name", ""),
        "fund_house":  m.get("fund_house", ""),
        "scheme_type": m.get("scheme_type", ""),
        "scheme_category": m.get("scheme_category", ""),
    }


# ── Wound-up detection ────────────────────────────────────────────────────────

def wound_up_info(nav_history: List[Dict[str, Any]]) -> Optional[str]:
    """Return the wound-up date (ISO string) if the fund is inactive, else None.

    A fund is considered wound up / merged / inactive when its last NAV date is
    older than WOUND_UP_THRESHOLD_DAYS before today.
    """
    if not nav_history:
        return None
    last_date: date = nav_history[-1]["date"]
    threshold = (datetime.now(timezone.utc).date() - timedelta(days=WOUND_UP_THRESHOLD_DAYS))
    if last_date < threshold:
        return last_date.isoformat()
    return None


# ── Unified resolve + verify + stitch (single source of truth) ───────────────

MAX_STITCH_DEPTH = 4   # guard against pathological successor cycles


async def load_analysis_nav(scheme_code: int, include_predecessor: bool = True) -> Dict[str, Any]:
    """Fetch a fund's NAV and stitch verified predecessor/successor history.

    `include_predecessor` (default True — unchanged historical behaviour):
    when False, the BACKWARD pass below still WALKS the predecessor chain (to
    report what's available) but does not merge it into `nav_history` — the
    fund is analysed using only NAVs recorded under `scheme_code` itself
    (and any codes it wound up INTO — the FORWARD pass is unaffected, since
    that's "same fund, code changed for an unrelated reason", not "old vs new
    management"). This is the toggle a user picks on the fund-detail screen
    to compare "new management only" against "full stitched history".

    This is the ONE place NAV is prepared for analysis — both the SIP and the
    lumpsum builders call it, so stitching behaviour can never drift apart.

    Two independent directions, both gated the same way:

      FORWARD (queried code is wound up → extend to an active successor):
      1. Fetch the fund's own NAV; detect wound-up (last NAV > 180d old).
      2. If wound up, resolve a successor (curated first, then discovered).
      3. Fetch the successor and VERIFY via NAV return-correlation
         (fund_lineage.verify_stitch). Only stitch if it passes — a wrong map
         entry cannot corrupt returns unless the NAVs genuinely track together.
      4. If the successor is itself wound up, repeat (chained mergers), up to
         MAX_STITCH_DEPTH, guarding against cycles.

      BACKWARD (queried code is itself the successor of an older code):
      The common real-world shape — the app always queries the CURRENT/active
      scheme code, which was never wound up, so the forward pass above never
      fires for it. Without a backward pass, an AMC acquisition / true merger
      / code change silently truncates history at the transition date and the
      active code's own first NAV gets mistaken for the fund's inception
      (e.g. Edelweiss Mid Cap Fund, code 140225, would show "inception" 2016
      when the fund actually launched in 2007 as JPMorgan India Mid and Small
      Cap Fund, code 107301 — a different AMFI code entirely).
      5. Resolve predecessor candidate(s) of the earliest known code
         (curated first, then discovered) and VERIFY the same way. Only
         accept and prepend if verify_stitch passes.
      6. Repeat backward from the newly-prepended code, up to MAX_STITCH_DEPTH.

    Returns dict with:
      meta, original_nav, nav_history, wound_up_date, wound_up (bool),
      successor_used (int|None), predecessor_used (list[int]),
      stitch_chain (list[int]), stitch_signals (list), history_note (str|None).
    """
    payload      = await fetch_fund_nav(scheme_code)
    meta         = meta_from_payload(payload)
    original_nav = parse_nav_history(payload)

    nav_history: List[Dict[str, Any]] = list(original_nav)
    stitch_chain: List[int] = []
    predecessor_chain: List[int] = []
    stitch_signals: List[Dict[str, Any]] = []
    visited: set = {int(scheme_code)}

    # ── Forward: queried code wound up → extend to active successor(s) ──────
    current_code = int(scheme_code)
    current_nav  = original_nav
    depth = 0
    while depth < MAX_STITCH_DEPTH:
        depth += 1
        # Only stitch onto a segment that is itself wound up.
        if wound_up_info(current_nav) is None:
            break
        succ_code, is_curated = resolve_successor(current_code)
        if not succ_code or succ_code in visited:
            break
        try:
            succ_payload = await fetch_fund_nav(succ_code)
            succ_nav     = parse_nav_history(succ_payload)
        except Exception as exc:
            logger.warning("Successor %s fetch failed for %s: %s", succ_code, current_code, exc)
            break
        if not succ_nav:
            break

        accepted, signals = fund_lineage.verify_stitch(
            current_nav, succ_nav, curated=is_curated,
            isin_verified=is_isin_verified(current_code, succ_code),
        )
        signals.update({"direction": "forward", "predecessor": current_code, "successor": succ_code})
        stitch_signals.append(signals)
        if not accepted:
            logger.warning(
                "Stitch REJECTED %s → %s (%s)", current_code, succ_code, signals.get("reason")
            )
            break

        nav_history = fund_lineage.stitch(nav_history, succ_nav)
        stitch_chain.append(succ_code)
        visited.add(succ_code)
        logger.info(
            "Stitch OK %s → %s (%s) → %d pts total",
            current_code, succ_code, signals.get("reason"), len(nav_history),
        )
        current_code, current_nav = succ_code, succ_nav

    # ── Backward: queried code (or the oldest code above) is itself a
    # successor of an older code → prepend that older code's history. ──────
    earliest_code = int(scheme_code)
    earliest_nav  = original_nav
    # Every verified predecessor found on the walk, oldest-last (append order),
    # regardless of `include_predecessor` — this is what the fund-detail
    # screen shows to explain WHY the toggle exists / what it would add.
    predecessor_available: List[Dict[str, Any]] = []
    depth = 0
    while depth < MAX_STITCH_DEPTH:
        depth += 1
        accepted_this_round = False
        for pred_code, is_curated in resolve_predecessors(earliest_code):
            if pred_code in visited:
                continue
            try:
                pred_payload = await fetch_fund_nav(pred_code)
                pred_nav     = parse_nav_history(pred_payload)
            except Exception as exc:
                logger.warning("Predecessor %s fetch failed for %s: %s", pred_code, earliest_code, exc)
                continue
            if not pred_nav:
                continue

            accepted, signals = fund_lineage.verify_stitch(
                pred_nav, earliest_nav, curated=is_curated,
                isin_verified=is_isin_verified(pred_code, earliest_code),
            )
            signals.update({"direction": "backward", "predecessor": pred_code, "successor": earliest_code})
            stitch_signals.append(signals)
            if not accepted:
                logger.info(
                    "Backward stitch REJECTED %s → %s (%s)",
                    pred_code, earliest_code, signals.get("reason"),
                )
                continue

            predecessor_available.append({
                "scheme_code": pred_code,
                "first_date": pred_nav[0]["date"].isoformat(),
                "last_date": pred_nav[-1]["date"].isoformat(),
                "points": len(pred_nav),
            })

            if include_predecessor:
                nav_history = fund_lineage.stitch(pred_nav, nav_history)
                predecessor_chain.insert(0, pred_code)
                logger.info(
                    "Backward stitch OK %s → %s (%s) → %d pts total",
                    pred_code, earliest_code, signals.get("reason"), len(nav_history),
                )
            visited.add(pred_code)
            earliest_code, earliest_nav = pred_code, pred_nav
            accepted_this_round = True
            break  # one verified predecessor per level; fan-in merges are rare and reviewed manually
        if not accepted_this_round:
            break

    successor_used = stitch_chain[-1] if stitch_chain else None
    # Wound-up label reflects the FINAL, stitched series: if we successfully
    # extended to an active successor, the fund is no longer "wound up".
    final_wound_up_date = wound_up_info(nav_history)
    original_wound_up_date = wound_up_info(original_nav)

    first_available = nav_history[0]["date"] if nav_history else None
    history_note = fund_lineage.history_note_for(scheme_code, first_available)

    return {
        "meta":              meta,
        "original_nav":      original_nav,
        "nav_history":       nav_history,
        "wound_up":          final_wound_up_date is not None,
        "wound_up_date":     original_wound_up_date,   # date the ORIGINAL code stopped
        "successor_used":    successor_used,           # forward: active code we extended into
        "predecessor_used":  predecessor_chain,         # backward: older code(s) actually merged in, oldest first
        "predecessor_available": predecessor_available, # backward: every verified predecessor found, merged or not
        "stitch_chain":      stitch_chain,
        "stitch_signals":    stitch_signals,
        "history_note":      history_note,
    }
