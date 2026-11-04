"""Shared "which historical variant to show" selection logic.

Both server.py's Home-screen universe endpoints (home_universe /
lumpsum_universe) and proposal_routes.py's Proposal Builder need to answer
the exact same question for a fund_nav_meta document: given the Since-2010
and Separate Management Periods toggle state, which of a fund's precomputed
variants (plain since-inception / forced-2010 / continuity-truncated)
should be read?

This was originally defined only in server.py. Proposal Builder building a
proposal from funds selected while a toggle was on used to silently go back
to each fund's plain since-inception numbers instead — the Home list and
the resulting proposal could show two different FQS/XIRR for the same fund
with no indication why. Pulling this out into its own module (importable
from both server.py and proposal_routes.py, which never imports server.py
directly — see proposal_routes.py's own docstring on why) is what makes it
possible for proposal building to match Home exactly instead of
re-deriving its own (potentially drifting) copy of this logic.

Pure functions — no DB, no I/O. Operate on an already-fetched fund_nav_meta
document (or an equivalent dict carrying the same field names).
"""

from __future__ import annotations

from typing import Any, Dict, List


def continuity_applies(fund_doc: Dict[str, Any], since_2010: bool) -> bool:
    """Whether the "Separate Management Periods" numbers should be used for
    this fund — requires the fund to actually have a verified continuity
    break, and, when Since-2010 is ALSO on, for that break to already be no
    earlier than 2010-01-01. This is a deliberate simplification for the
    rare "both toggles on, break predates 2010" combination: rather than
    precomputing a 4th (continuity + 2010) variant, a fund whose break is
    older than 2010 just falls back to the plain Since-2010 numbers in that
    combination, so the explicit Since-2010 request is never silently
    dropped. Continuity breaks are almost always recent (mergers/AMC
    consolidations), so this fallback is rarely exercised in practice."""
    forced = fund_doc.get("fqs_by_horizon_continuity") or fund_doc.get("horizons_continuity")
    if not forced:
        return False
    reset_date = fund_doc.get("continuity_reset_date")
    if since_2010 and reset_date and reset_date < "2010-01-01":
        return False
    return True


def select_horizon_map(
    fund_doc: Dict[str, Any], since_2010: bool, separate_management: bool = False
) -> Dict[str, Any]:
    """SIP: which per-horizon map (fqs_by_horizon shape) to read for this
    fund. Precedence: Separate Management Periods (continuity) > Since-2010 >
    normal since-inception — each level falls back to the next when this
    fund doesn't have the more specific numbers (too young for 2010, no
    verified continuity break, etc). Not having a variant is never a reason
    to hide the fund, only to fall back to what it does have."""
    if separate_management and continuity_applies(fund_doc, since_2010):
        return fund_doc.get("fqs_by_horizon_continuity") or {}
    if since_2010:
        forced = fund_doc.get("fqs_by_horizon_2010")
        if forced:
            return forced
    return fund_doc.get("fqs_by_horizon") or {}


def select_horizons_list(
    doc: Dict[str, Any], since_2010: bool, separate_management: bool = False
) -> List[Dict[str, Any]]:
    """Lumpsum equivalent of select_horizon_map — doc.horizons is a list,
    not a dict keyed by horizon. Same precedence."""
    if separate_management and continuity_applies(doc, since_2010):
        return doc.get("horizons_continuity") or []
    if since_2010:
        forced = doc.get("horizons_2010")
        if forced:
            return forced
    return doc.get("horizons") or []
