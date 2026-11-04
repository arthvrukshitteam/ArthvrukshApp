"""What actually happened after the cutoff.

The realised outcome is **one** monthly SIP started the month after the cutoff
and run for the full horizon — not a lump-sum return, and not an average of
overlapping windows (there is only one future).

Decision D1: this is computed with the model's own window construction
(MODEL_AUDIT.md §5.1), *including* the hardcoded-denominator defect (§5.2), so
prediction and outcome are measured on the same ruler. Correcting only the
actual side would inject a systematic ~1.7% gap into every comparison.

Look-ahead safety: this module is the only place allowed to read NAVs after the
cutoff, and it is called strictly after the prediction has been frozen.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Sequence

from . import config as C
from . import point_in_time as pit

import sys
sys.path.insert(0, str(C.BACKEND))
from fqs_engine import _edate, _eomonth  # noqa: E402  — the model's date helpers


@dataclass
class Actual:
    """One fund's realised outcome for one cutoff. MODEL_AUDIT.md §23."""
    cutoff_date: str
    scheme_code: int
    horizon: int
    future_start_date: str
    future_end_date: str
    n_instalments: int
    total_invested: float
    actual_wealth: float
    actual_wm: float
    actual_xirr: float | None
    actual_cagr: float | None
    validation_status: str = "VALIDATED"

    def to_row(self) -> dict:
        return asdict(self)


def realise(
    raw: Sequence[tuple[date, float]],
    cutoff: date,
    scheme_code: int,
    horizon: int = C.VALIDATION_HORIZON_YEARS,
) -> Actual | None:
    """Run the single forward SIP. Returns None if the future is incomplete.

    Window construction mirrors `compute_rolling_windows` exactly for a single
    `k` anchored at the cutoff:

        A = EOMONTH(cutoff) + 1 day        first instalment month begins
        B = EDATE(A, 12H)                  nominal redemption date
        SIP dates  = month-ends in [A, B)  half-open, as the model does
        end NAV    = last month-end <= B
        invested   = SIP_AMOUNT * 12H      hardcoded, per D1

    Admission uses the model's own rule (MODEL_AUDIT.md §7.2): the outcome only
    counts once `EOMONTH(B)` is covered by the fund's data. A fund that wound up
    mid-horizon therefore yields None rather than a truncated, flattering
    number.
    """
    if not raw:
        return None

    monthly = pit.month_ends_at(raw, date.max)
    if not monthly:
        return None

    a = _eomonth(cutoff, 0) + timedelta(days=1)
    b = _edate(a, 12 * horizon)
    if _eomonth(b, 0) > _eomonth(monthly[-1][0], 0):
        return None                                  # future not complete yet

    dates = [d for d, _ in monthly]
    sip_idx = [i for i, d in enumerate(dates) if a <= d < b]
    if not sip_idx:
        return None
    end_candidates = [i for i, d in enumerate(dates) if d <= b]
    if not end_candidates:
        return None

    units = sum(C.SIP_AMOUNT / monthly[i][1] for i in sip_idx)
    end_i = end_candidates[-1]
    end_nav = monthly[end_i][1]
    wealth = units * end_nav
    invested = C.SIP_AMOUNT * horizon * 12           # D1 — defect reproduced
    wm = wealth / invested

    from xirr_utils import sip_wm_to_xirr

    # CAGR on a SIP is not well defined (contributions arrive at different
    # times), so the annualised figure is the model's own SIP XIRR. A
    # point-to-point CAGR is reported separately as a NAV-only reference.
    start_nav = monthly[sip_idx[0]][1]
    years = (dates[end_i] - dates[sip_idx[0]]).days / 365.25
    cagr = (end_nav / start_nav) ** (1 / years) - 1 if years > 0 and start_nav > 0 else None

    return Actual(
        cutoff_date=cutoff.isoformat(),
        scheme_code=scheme_code,
        horizon=horizon,
        future_start_date=dates[sip_idx[0]].isoformat(),
        future_end_date=dates[end_i].isoformat(),
        n_instalments=len(sip_idx),
        total_invested=invested,
        actual_wealth=round(wealth, 2),
        actual_wm=wm,
        actual_xirr=sip_wm_to_xirr(wm, horizon),
        actual_cagr=cagr,
    )


def future_is_available(
    raw: Sequence[tuple[date, float]], cutoff: date, horizon: int
) -> bool:
    """Cheap pre-check used to mark predictions PENDING vs VALIDATED."""
    if not raw:
        return False
    a = _eomonth(cutoff, 0) + timedelta(days=1)
    b = _edate(a, 12 * horizon)
    return _eomonth(b, 0) <= _eomonth(raw[-1][0], 0)
