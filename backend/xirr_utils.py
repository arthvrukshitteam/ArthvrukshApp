"""Wealth Multiple <-> XIRR conversions.

Canonical Python source for the two formulas extracted from
"ArthVruksh MF Proposal_Priya Ravi.xlsx":

    SIP sheet:     =RATE(years*12, -1, 0, WM*years*12) * 12
    Lumpsum sheet: =RATE(years, , -1, WM, 0)  ==  WM^(1/years) - 1

These are already ported once in `frontend/src/utils/xirr.ts` (for the
Lumpsum Home screen) and once inline in `server.py` as `_sip_wm_to_xirr` /
`_lumpsum_wm_to_xirr` (for the universe endpoints). `proposal_engine.py`
needs the *same* math plus the inverse (XIRR -> WM) for SWP corpus
auto-derivation, so it lives here once rather than adding a third
independent copy inside proposal_engine.py itself.
"""

from __future__ import annotations

import math
from typing import Optional


def lumpsum_wm_to_xirr(wm: Optional[float], horizon_years: float) -> Optional[float]:
    """Lumpsum: exact closed-form. RATE(n,,-1,WM) = WM^(1/n) - 1."""
    if not wm or wm <= 0 or horizon_years <= 0:
        return None
    return math.pow(wm, 1.0 / horizon_years) - 1.0


def sip_wm_to_xirr(wm: Optional[float], horizon_years: float) -> Optional[float]:
    """SIP: Newton-Raphson solve of Excel RATE(n*12,-1,0,WM*n*12)*12.

    Solves ((1+r)^n - 1) / r = WM*n for monthly r (n = years*12), returns
    the annual nominal rate r*12.
    """
    if not wm or wm <= 0 or horizon_years <= 0:
        return None
    n = horizon_years * 12
    fv = wm * n
    r = 0.01
    for _ in range(200):
        powered = (1.0 + r) ** n
        f = (powered - 1.0) / r - fv
        df = (n * (1.0 + r) ** (n - 1) * r - (powered - 1.0)) / (r * r)
        if abs(df) < 1e-15:
            break
        delta = f / df
        r -= delta
        if r <= -1:
            r = 1e-6
        if abs(delta) < 1e-12:
            break
    annual = r * 12
    if not math.isfinite(annual) or annual < -0.5 or annual > 5:
        return None
    return annual


def lumpsum_xirr_to_wm(xirr: float, horizon_years: float) -> float:
    """Inverse of lumpsum_wm_to_xirr. WM = (1 + xirr)^years."""
    return (1.0 + xirr) ** horizon_years


def sip_xirr_to_wm(xirr: float, horizon_years: float) -> float:
    """Inverse of sip_wm_to_xirr. WM = ((1+m)^N - 1) / (N*m), m = xirr/12."""
    m = xirr / 12.0
    n = horizon_years * 12
    if abs(m) < 1e-9:
        return 1.0
    return ((1.0 + m) ** n - 1.0) / (n * m)


def wm_to_xirr(mode: str, wm: Optional[float], horizon_years: float) -> Optional[float]:
    """Dispatch helper — mode is 'sip' or 'lumpsum'."""
    return sip_wm_to_xirr(wm, horizon_years) if mode == "sip" else lumpsum_wm_to_xirr(wm, horizon_years)


def xirr_to_wm(mode: str, xirr: Optional[float], horizon_years: float) -> Optional[float]:
    """Inverse dispatch helper — mode is 'sip' or 'lumpsum'.

    Used to EXTRAPOLATE a wealth multiple to a horizon longer than the one the
    XIRR was measured over (Additional Recommendation funds in a longer-tenure
    portfolio — see proposal_engine.py).
    """
    if xirr is None or horizon_years <= 0:
        return None
    return sip_xirr_to_wm(xirr, horizon_years) if mode == "sip" else lumpsum_xirr_to_wm(xirr, horizon_years)
