"""WM <-> XIRR conversion tests.

Ground truth = the Priya PF sheet formulas:

    SIP:     =RATE(years*12, -1, 0, WM*years*12) * 12
    Lumpsum: =RATE(years, , -1, WM, 0)  ==  WM^(1/years) - 1

Excel RATE(nper, pmt, pv, fv, type=0) solves:
    pv*(1+r)^n + pmt*((1+r)^n - 1)/r + fv = 0

We verify our Newton-Raphson / closed-form implementations against an
independent high-precision bisection solve of the same RATE equation, plus
round-trips through the inverse functions.
"""
import math

import pytest

from xirr_utils import (
    lumpsum_wm_to_xirr,
    lumpsum_xirr_to_wm,
    sip_wm_to_xirr,
    sip_xirr_to_wm,
    wm_to_xirr,
)


def excel_rate(nper: float, pmt: float, pv: float, fv: float) -> float:
    """Independent bisection solve of Excel's RATE equation (type=0)."""
    def f(r):
        if abs(r) < 1e-12:
            return pv + pmt * nper + fv
        return pv * (1 + r) ** nper + pmt * ((1 + r) ** nper - 1) / r + fv

    lo, hi = -0.99, 10.0
    flo = f(lo)
    for _ in range(300):
        mid = (lo + hi) / 2
        fm = f(mid)
        if flo * fm <= 0:
            hi = mid
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2


# ── SIP: RATE(years*12, -1, 0, WM*years*12) * 12 ─────────────────────────────

@pytest.mark.parametrize("wm,years", [
    (1.5, 3), (2.0, 5), (2.09, 7), (3.50, 13), (2.04, 7),
    (1.2, 1), (4.0, 10), (6.0, 15), (8.0, 20), (1.01, 5),
])
def test_sip_matches_excel_rate(wm, years):
    expected = excel_rate(years * 12, -1, 0, wm * years * 12) * 12
    got = sip_wm_to_xirr(wm, years)
    assert got is not None
    assert got == pytest.approx(expected, abs=1e-8)


def test_sip_screenshot_value():
    # Home card: 13Y SIP, blended WM 3.50x displays 16.6% p.a.
    x = sip_wm_to_xirr(3.50, 13)
    assert round(x * 100, 1) == 16.6


# ── Lumpsum: RATE(years, , -1, WM, 0) == WM^(1/years) - 1 ────────────────────

@pytest.mark.parametrize("wm,years", [
    (1.5, 3), (2.0, 5), (3.1058, 10), (4.7231, 10), (10.283, 10), (2.0, 20),
])
def test_lumpsum_matches_excel_rate(wm, years):
    expected = excel_rate(years, 0, -1, wm)
    got = lumpsum_wm_to_xirr(wm, years)
    assert got == pytest.approx(expected, abs=1e-8)
    assert got == pytest.approx(math.pow(wm, 1 / years) - 1, abs=1e-12)


def test_lumpsum_screenshot_value():
    # Lumpsum Analysis: P10 WM 4.7231x @10Y displays 16.8% p.a.
    x = lumpsum_wm_to_xirr(4.7231, 10)
    assert round(x * 100, 1) == 16.8


# ── Inverses round-trip ──────────────────────────────────────────────────────

@pytest.mark.parametrize("xirr,years", [(0.08, 5), (0.12, 10), (0.15, 7), (0.25, 13)])
def test_sip_round_trip(xirr, years):
    wm = sip_xirr_to_wm(xirr, years)
    assert sip_wm_to_xirr(wm, years) == pytest.approx(xirr, abs=1e-9)


@pytest.mark.parametrize("xirr,years", [(0.08, 5), (0.12, 10), (0.15, 7), (0.25, 13)])
def test_lumpsum_round_trip(xirr, years):
    wm = lumpsum_xirr_to_wm(xirr, years)
    assert lumpsum_wm_to_xirr(wm, years) == pytest.approx(xirr, abs=1e-12)


# ── Guards & dispatch ────────────────────────────────────────────────────────

def test_invalid_inputs_return_none():
    assert sip_wm_to_xirr(None, 5) is None
    assert sip_wm_to_xirr(0, 5) is None
    assert sip_wm_to_xirr(-1, 5) is None
    assert sip_wm_to_xirr(2.0, 0) is None
    assert lumpsum_wm_to_xirr(None, 5) is None
    assert lumpsum_wm_to_xirr(0, 5) is None
    assert lumpsum_wm_to_xirr(2.0, 0) is None


def test_dispatch():
    assert wm_to_xirr("sip", 2.0, 5) == sip_wm_to_xirr(2.0, 5)
    assert wm_to_xirr("lumpsum", 2.0, 5) == lumpsum_wm_to_xirr(2.0, 5)


def test_server_aliases_are_canonical():
    """server.py must use the xirr_utils implementations (no drifted copies)."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "server.py").read_text()
    assert "from xirr_utils import" in src
    assert "def _sip_wm_to_xirr" not in src
    assert "def _lumpsum_wm_to_xirr" not in src
