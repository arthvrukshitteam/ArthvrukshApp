"""Additional Recommendations — shared selection rule.

Single source of truth for the "judge this fund at its longest statistically
robust window" rule, used in two places that must never drift apart:

  1. `server.py`  — /home/universe and /lumpsum/universe, to decide WHICH funds
     appear as Additional Recommendations (adds an XIRR >= 15% gate).
  2. `proposal_engine.py` — when such a fund is put into a portfolio whose
     tenure is LONGER than the fund's own history, to decide WHICH window its
     displayed parameters come from.

Keeping the rule here means a fund shown as "judged at 7Y" on the Additional
Recommendations card is guaranteed to be shown at 7Y in the proposal too.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# History from 5Y up to just below Core; judged at the fund's LONGEST window
# (5Y..max_horizon) that has >= 10 rolling windows so its stats are robust;
# XIRR >= 15%.
ADDITIONAL_REC_MIN_XIRR = 0.15
ADDITIONAL_REC_MIN_HISTORY_YEARS = 5
ADDITIONAL_REC_MIN_WINDOWS = 10


def select_robust_horizon(
    horizon_stats: List[Tuple[Any, Dict[str, Any]]],
    max_horizon: int,
) -> Optional[Tuple[int, Dict[str, Any]]]:
    """Longest horizon in [MIN_HISTORY_YEARS..max_horizon] that has a valid
    blended_wm AND at least MIN_WINDOWS rolling windows.

    A window with <10 samples has no real P10/CV distribution, so it is never
    used. Returns (horizon, stats) or None.
    """
    best_h: Optional[int] = None
    best_stats: Optional[Dict[str, Any]] = None
    for h, stats in horizon_stats:
        try:
            h = int(h)
        except (TypeError, ValueError):
            continue
        if not (ADDITIONAL_REC_MIN_HISTORY_YEARS <= h <= max_horizon):
            continue
        if not stats or stats.get("blended_wm") is None:
            continue
        if (stats.get("n_windows") or 0) < ADDITIONAL_REC_MIN_WINDOWS:
            continue
        if best_h is None or h > best_h:
            best_h, best_stats = h, stats
    if best_h is None:
        return None
    return best_h, best_stats  # type: ignore[return-value]


def select_additional_rec(
    horizon_stats: List[Tuple[Any, Dict[str, Any]]],
    max_horizon: int,
    to_xirr,
):
    """`select_robust_horizon` + the XIRR >= 15% qualification gate.

    max_horizon is the Core cutoff (tenure + buffer), so a candidate with more
    history than the tenure is evaluated at its max available window, not the
    tenure. Every returned metric comes from that single window.
    Returns (xirr, horizon, stats) or None.
    """
    pick = select_robust_horizon(horizon_stats, max_horizon)
    if pick is None:
        return None
    best_h, best_stats = pick
    x = to_xirr(best_stats["blended_wm"], best_h)
    if x is None or x < ADDITIONAL_REC_MIN_XIRR:
        return None
    return (x, best_h, best_stats)
