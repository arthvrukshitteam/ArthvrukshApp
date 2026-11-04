"""Indian number formatting — backend twin of frontend/src/utils/format.ts.

Both must agree: the same portfolio figure appears in the app, the PDF and the
Excel export, and a client comparing them will notice a mismatch immediately.

Indian grouping is 2-2-3 from the right, not 3-3-3:
    1,000 · 10,000 · 1,00,000 · 10,00,000 · 1,00,00,000 · 10,00,00,000

Python's `format(v, ",")` produces WESTERN grouping. `proposal_xlsx.py` used
exactly that and shipped "₹1,000,000" into a client-facing workbook where it
should read "₹10,00,000". `locale` is not used here because it depends on the
host having the en_IN locale installed, which Render's image does not.
"""

from __future__ import annotations

import math
from typing import Optional

CRORE = 10_000_000
LAKH = 100_000


def indian_group(value) -> str:
    """Digits only, Indian grouped: 1234567 -> '12,34,567'."""
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return "—"
    sign = "-" if n < 0 else ""
    s = str(abs(n))
    if len(s) <= 3:
        return sign + s
    # Last three digits, then the rest in pairs.
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return sign + ",".join(parts + [tail])


def rupees(value, ascii_symbol: bool = False) -> str:
    """Full amount: 12500000 -> '₹1,25,00,000'.

    `ascii_symbol` emits 'Rs.' instead of '₹' — reportlab's built-in Helvetica
    (WinAnsi) has no glyph for U+20B9 and renders it as a black box.
    """
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(v):
        return "—"
    sign = "-" if v < 0 else ""
    sym = "Rs. " if ascii_symbol else "₹"
    return f"{sign}{sym}{indian_group(abs(v))}"


def rupees_compact(value, short: bool = False, decimals: int = 2,
                   ascii_symbol: bool = False) -> str:
    """Compact with Indian scale: 12500000 -> '₹1.25 Cr', 4550000 -> '₹45.5 Lakh'.

    Trailing zeros are trimmed so it reads '₹45.5 Lakh', not '₹45.50 Lakh'.
    """
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(v):
        return "—"
    sign = "-" if v < 0 else ""
    a = abs(v)

    def trim(x: float) -> str:
        return f"{x:.{decimals}f}".rstrip("0").rstrip(".")

    sym = "Rs. " if ascii_symbol else "₹"
    if a >= CRORE:
        return f"{sign}{sym}{trim(a / CRORE)} Cr"
    if a >= LAKH:
        return f"{sign}{sym}{trim(a / LAKH)} {'L' if short else 'Lakh'}"
    return f"{sign}{sym}{indian_group(a)}"


def percent(value: Optional[float], decimals: int = 1) -> str:
    """From a FRACTION: 0.1425 -> '14.3%'."""
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    return "—" if not math.isfinite(v) else f"{v * 100:.{decimals}f}%"


def multiple(value: Optional[float], decimals: int = 2) -> str:
    """Wealth multiple: 2.3141 -> '2.31x'."""
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    return "—" if not math.isfinite(v) else f"{v:.{decimals}f}x"


def wm_with_xirr(wm: Optional[float], xirr: Optional[float]) -> str:
    """'2.31x · 14.3% p.a.' — WM is kept, XIRR is shown alongside it.

    Advisors read WM; clients read an annualised rate. Showing both avoids
    each side having to convert in their head, and avoids dropping WM (which
    is what the FQS model is actually built on).
    """
    if wm is None and xirr is None:
        return "—"
    if xirr is None:
        return multiple(wm)
    if wm is None:
        return f"{percent(xirr)} p.a."
    return f"{multiple(wm)} · {percent(xirr)} p.a."
