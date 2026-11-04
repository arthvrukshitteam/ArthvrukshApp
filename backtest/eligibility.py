"""Who is allowed into the model at a given cutoff — and why anyone was refused.

Nothing is dropped silently (MODEL_AUDIT.md §25 requirement). Every fund/cutoff
pair that fails produces an `Exclusion` row with a machine-readable reason, so
the exclusion report can be audited afterwards.

The eligibility rule is the app's own: `history_years >= rolling_period + 2`
(server.py:475,485-486), where the buffer is read out of server.py at import
time rather than hardcoded. See MODEL_AUDIT.md §4.

Important: history is measured from the fund's OWN data as it stood at the
cutoff, never from `fund_nav_meta.history_years`, which is today's value and is
never refreshed (MODEL_AUDIT.md §4.2).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Sequence

from . import config as C
from . import point_in_time as pit


class Reason(str, Enum):
    OK = "eligible"
    NO_DATA = "missing_NAV"
    NOT_YET_LAUNCHED = "not_yet_launched"
    INSUFFICIENT_HISTORY = "insufficient_history"
    INSUFFICIENT_MONTHLY_POINTS = "insufficient_monthly_observations"
    NO_ROLLING_WINDOWS = "insufficient_rolling_observations"
    STALE_AT_CUTOFF = "no_recent_nav_at_cutoff"
    EXCLUDED_CATEGORY = "excluded_category"


#: A fund whose most recent NAV is this far behind the cutoff was not a live,
#: investable option on that date. Mirrors the model's own wound-up test
#: (WOUND_UP_THRESHOLD_DAYS = 180, amfi_client.py:51). Such funds stay in the
#: dataset — they are simply not offerable at cutoffs after they stopped
#: reporting. This is what prevents both survivorship bias and its opposite.
STALE_NAV_DAYS = 180


@dataclass(frozen=True)
class Verdict:
    eligible: bool
    reason: Reason
    available_history_years: float
    required_history_years: int
    monthly_points: int
    n_rolling_windows: int
    first_nav: str | None
    last_nav_at_cutoff: str | None


def assess(
    raw: Sequence[tuple[date, float]],
    cutoff: date,
    internal_category: str,
    horizon: int = C.ROLLING_PERIOD_YEARS,
    *,
    b_eoms: Sequence[date] | None = None,
    min_history_years: int | None = None,
) -> Verdict:
    """Decide whether this fund may participate at this cutoff.

    `b_eoms` is the nominal-redemption-month list from
    `point_in_time.build_all_windows`. Passing it lets the window count come
    from the O(1) slice instead of a recomputation (MODEL_AUDIT.md §7.2).
    """
    required = min_history_years if min_history_years is not None else C.MIN_HISTORY_YEARS

    def _v(ok: bool, reason: Reason, **kw) -> Verdict:
        base = dict(
            available_history_years=0.0,
            required_history_years=required,
            monthly_points=0,
            n_rolling_windows=0,
            first_nav=None,
            last_nav_at_cutoff=None,
        )
        base.update(kw)
        return Verdict(eligible=ok, reason=reason, **base)

    if internal_category in C.EXCLUDED_INTERNAL_CATEGORIES:
        return _v(False, Reason.EXCLUDED_CATEGORY)
    if not raw:
        return _v(False, Reason.NO_DATA)
    if raw[0][0] > cutoff:
        return _v(False, Reason.NOT_YET_LAUNCHED, first_nav=raw[0][0].isoformat())

    monthly = pit.month_ends_at(raw, cutoff)
    if not monthly:
        return _v(False, Reason.NO_DATA, first_nav=raw[0][0].isoformat())

    first = monthly[0][0]
    last = monthly[-1][0]
    history_years = (last - first).days / 365.25      # nav_meta_indexer.py:139-140
    common = dict(
        available_history_years=round(history_years, 2),
        monthly_points=len(monthly),
        first_nav=first.isoformat(),
        last_nav_at_cutoff=last.isoformat(),
    )

    # A fund that stopped reporting well before the cutoff was not investable
    # on that date, even though its history is long.
    if (cutoff - last).days > STALE_NAV_DAYS:
        return _v(False, Reason.STALE_AT_CUTOFF, **common)

    if history_years < required:
        return _v(False, Reason.INSUFFICIENT_HISTORY, **common)

    # The engine's own hard floor: fewer than H*12+1 month-end points and
    # `compute_rolling_windows` returns [] (fqs_engine.py:587-588).
    if len(monthly) < horizon * 12 + 1:
        return _v(False, Reason.INSUFFICIENT_MONTHLY_POINTS, **common)

    if b_eoms is not None:
        lme = pit.eomonth(last)
        n_windows = pit.admissible_count(b_eoms, lme)
    else:
        n_windows = len(pit.windows_at(raw, internal_category, horizon, cutoff)[0])

    if n_windows < 1:
        return _v(False, Reason.NO_ROLLING_WINDOWS, n_rolling_windows=0, **common)

    return _v(True, Reason.OK, n_rolling_windows=n_windows, **common)
