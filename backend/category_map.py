"""Category → Minimum Wealth Multiple (WM) benchmark table.

Drawn directly from the FINAL MODEL sheet of the MF Analysis spec.
The 'cap' column normalises raw scores (Blended WM, Downside) to a 0-100 band.

All min_wm values verified directly against FINAL MODEL!D43:G50.
"""

from typing import Optional

# (min_xirr, min_wm_3y, min_wm_5y, min_wm_10y, min_wm_15y, cap)
CATEGORY_TABLE = {
    # Non-equity categories — use non-equity FQS weight branch
    "Debt":        {"xirr": 0.07, "wm": {3: 1.1083333333333334, 5: 1.1933333333333334, 10: 1.4425,              15: 1.761111111111111},  "cap": 110, "type": "non_equity"},
    "Gold/Silver": {"xirr": 0.08, "wm": {3: 1.125,              5: 1.2249999999999999, 10: 1.5241666666666667,  15: 1.9222222222222223}, "cap": 120, "type": "non_equity"},
    "Hybrid":      {"xirr": 0.11, "wm": {3: 1.178,              5: 1.325,              10: 1.808,               15: 2.526},              "cap": 120, "type": "non_equity"},

    # FIX: Index Fund moved to non_equity — not listed as equity in FINAL MODEL
    "Index Fund":  {"xirr": 0.10, "wm": {3: 1.160,              5: 1.290,              10: 1.710,               15: 2.300},              "cap": 120, "type": "non_equity"},

    # Equity categories — use equity FQS weight branch
    "Large Cap":   {"xirr": 0.12, "wm": {3: 1.1972222222222222, 5: 1.3616666666666666, 10: 1.9166666666666667,  15: 2.7755555555555556}, "cap": 130, "type": "equity"},
    "Flexi Cap":   {"xirr": 0.13, "wm": {3: 1.2138888888888888, 5: 1.3983333333333334, 10: 2.033333333333333,   15: 3.053888888888889},  "cap": 140, "type": "equity"},
    "ELSS":        {"xirr": 0.13, "wm": {3: 1.2138888888888888, 5: 1.3983333333333334, 10: 2.033333333333333,   15: 3.053888888888889},  "cap": 140, "type": "equity"},
    "Multi Cap":   {"xirr": 0.14, "wm": {3: 1.2333333333333334, 5: 1.4366666666666665, 10: 2.1591666666666667,  15: 3.3661111111111114}, "cap": 140, "type": "equity"},
    # FIX: Mid Cap 15Y was 4.541 (Small Cap value) — corrected to 3.7138888888888886
    "Mid Cap":     {"xirr": 0.15, "wm": {3: 1.2527777777777778, 5: 1.4766666666666666, 10: 2.2933333333333334,  15: 3.7138888888888886}, "cap": 145, "type": "equity"},
    "Small Cap":   {"xirr": 0.17, "wm": {3: 1.2916666666666667, 5: 1.5599999999999998, 10: 2.5933333333333333,  15: 4.54111111111111},   "cap": 150, "type": "equity"},
    # Catch-all for funds that don't match any AMFI category keyword.
    # Uses Flexi Cap benchmarks — a conservative mid-point for unknown equity-like funds.
    "Misc Fund":   {"xirr": 0.13, "wm": {3: 1.2138888888888888, 5: 1.3983333333333334, 10: 2.033333333333333,   15: 3.053888888888889},  "cap": 140, "type": "equity"},
}

DEFAULT_CATEGORY = "Misc Fund"


def classify_fund(scheme_name: str, scheme_category: str = "") -> str:
    """Map an AMFI scheme to one of our internal benchmark buckets.

    Two-pass classifier:
      Pass 1 — AMFI canonical `scheme_category` (e.g. "Equity Scheme - Small
               Cap Fund", "Hybrid Scheme - Arbitrage Fund", "Debt Scheme -
               Gilt Fund"). This field is structured and authoritative when
               mfapi.in returns it.
      Pass 2 — keyword match in `name + category` for funds where the AMFI
               category is missing or non-standard (e.g. older schemes).

    Falls back to DEFAULT_CATEGORY (Misc Fund) only when neither pass hits.
    """
    cat = (scheme_category or "").lower().strip()
    name = (scheme_name or "").lower()

    # ── Pass 1: AMFI canonical scheme_category ──────────────────────────────
    if cat:
        # Equity sub-categories
        if "small cap" in cat:
            return "Small Cap"
        if ("mid cap" in cat or "midcap" in cat) and ("large" not in cat):
            return "Mid Cap"
        if "large & mid" in cat or "large and mid" in cat:
            return "Large Cap"
        if "large cap" in cat:
            return "Large Cap"
        if "multi cap" in cat or "multicap" in cat:
            return "Multi Cap"
        if "flexi cap" in cat or "flexicap" in cat:
            return "Flexi Cap"
        if "elss" in cat or "tax sav" in cat:
            return "ELSS"
        # Focused / Value / Contra / Dividend Yield → behave like multi-segment equity
        if "focused" in cat:
            return "Flexi Cap"
        if "value fund" in cat or "contra" in cat:
            return "Flexi Cap"
        if "dividend yield" in cat:
            return "Large Cap"
        if "sectoral" in cat or "thematic" in cat:
            return "Flexi Cap"

        # Hybrid sub-categories — arbitrage is debt-like, others are hybrid
        if "arbitrage" in cat:
            return "Debt"  # arbitrage funds return ~6–7%, behave like debt
        if ("hybrid" in cat or "balanced advantage" in cat
                or "dynamic asset" in cat or "multi asset" in cat
                or "equity savings" in cat):
            return "Hybrid"

        # Solution-oriented (Retirement / Children) — typically equity-heavy hybrids
        if "retirement" in cat or "children" in cat:
            return "Hybrid"

        # Other Scheme — Index / ETF / FoF / Gold/Silver
        if "gold" in cat or "silver" in cat:
            return "Gold/Silver"
        if "index fund" in cat or "etf" in cat:
            return "Index Fund"
        if "fof" in cat or "fund of funds" in cat or "fund of fund" in cat:
            if "overseas" in cat or "international" in cat or "foreign" in cat:
                return "Index Fund"  # international FoFs ~ index-like volatility
            # Domestic FoF — what it actually invests in is in the name:
            if "gold" in name or "silver" in name:
                return "Gold/Silver"
            if "nifty" in name or "sensex" in name or "index" in name:
                return "Index Fund"
            return "Flexi Cap"

        # Debt — catches every "Debt Scheme - *" variant plus loose hints
        if ("debt" in cat or "liquid" in cat or "overnight" in cat
                or "gilt" in cat or "bond" in cat
                or "duration" in cat or "credit risk" in cat
                or "money market" in cat or "floater" in cat
                or "banking and psu" in cat or "banking & psu" in cat
                or "corporate bond" in cat or "dynamic bond" in cat):
            return "Debt"

    # ── Pass 2: name + category keyword match (legacy fallback) ─────────────
    s = f"{cat} {name}"
    if "small cap" in s:
        return "Small Cap"
    if "mid cap" in s or "midcap" in s:
        return "Mid Cap"
    if "multi cap" in s or "multicap" in s:
        return "Multi Cap"
    if "flexi" in s:
        return "Flexi Cap"
    if "elss" in s or "tax saver" in s or "tax sav" in s:
        return "ELSS"
    if ("large cap" in s or "bluechip" in s or "blue chip" in s
            or "large & mid" in s):
        return "Large Cap"
    if "arbitrage" in s:
        return "Debt"
    if "equity savings" in s:
        return "Hybrid"
    if "index" in s or "nifty" in s or "sensex" in s or "etf" in s:
        return "Index Fund"
    if "gold" in s or "silver" in s:
        return "Gold/Silver"
    if ("hybrid" in s or "balanced" in s or "advantage" in s
            or "asset alloc" in s or "retirement" in s or "children" in s):
        return "Hybrid"
    if ("debt" in s or "liquid" in s or "gilt" in s or "bond" in s
            or "income" in s or "duration" in s or "credit" in s
            or "money market" in s or "overnight" in s
            or "banking and psu" in s or "banking & psu" in s
            or "floater" in s
            # Debt-plan names that carry none of the generic tokens above —
            # e.g. "Invesco India Short Term Fund" (whose AMFI meta is
            # corrupted upstream to scheme_category="Direct") was falling to
            # DEFAULT_CATEGORY and dodging the Debt WM sanity ceiling.
            or "short term" in s or "short-term" in s or "ultra short" in s
            or "treasury" in s or "money manager" in s or "floating rate" in s):
        return "Debt"

    return DEFAULT_CATEGORY


def min_wm_for(category: str, horizon: int) -> Optional[float]:
    cat = CATEGORY_TABLE.get(category)
    if not cat:
        return None
    return cat["wm"].get(horizon)


def cap_for(category: str) -> int:
    cat = CATEGORY_TABLE.get(category) or CATEGORY_TABLE[DEFAULT_CATEGORY]
    return cat["cap"]


def is_equity(category: str) -> bool:
    cat = CATEGORY_TABLE.get(category) or CATEGORY_TABLE[DEFAULT_CATEGORY]
    return cat["type"] == "equity"


# ---------------------------------------------------------------------------
# WM sanity ceiling — catches NAV data corruption (a NAV value stored 10-100x
# off due to a decimal-point/rebase/face-value glitch in the upstream feed),
# which otherwise silently inflates a fund's rolling-window Wealth Multiple
# far beyond anything realistically achievable for that asset class.
#
# A flat "WM > 50x is impossible" cutoff (the original safeguard, still used
# for every other category) only catches the most extreme corruption. It's
# far too loose for Debt: Debt/Liquid/Overnight/Money-Market/Credit-Risk
# funds realistically return single-digit % p.a., so a fund showing 30-60x
# WM (implying 50%+ p.a. sustained for a decade — see min_wm_for("Debt", 10)
# above, which pegs a *good* 10Y debt fund at just 1.44x) is just as
# impossible as 200x, but sails straight under the flat 50x net. This gives
# Debt its own, much tighter, horizon-scaled ceiling instead.
#
# BUG FIXED (2026-08-04): the flat 50x cap was written when horizons topped
# out at 20 years, where it's a safe outer bound — even 20% p.a. sustained
# for 20 years is only ~38x. Once 50-year horizons shipped, ordinary
# unremarkable performance legitimately blows past 50x on compounding alone
# (e.g. a lumpsum growing at just 10% p.a. for 50 years reaches ~117x — nothing
# to do with data corruption). Because this cap drops WHOLE windows rather
# than flagging them, a horizon whose every window exceeds it returns ZERO
# windows, i.e. the entire long-horizon result silently vanishes for a
# perfectly healthy fund. Give the generic branch the same horizon-scaled
# treatment as Debt already has, anchored on a ceiling annual rate no real
# fund sustains for decades (22%, well above every CATEGORY_TABLE hurdle,
# the highest of which is Small Cap's 17%). `max(...)` with the original
# flat 50 means this is a no-op through ~19 years — 1.22^20 ≈ 50.4 is the
# first year the scaled term overtakes 50 — so every horizon the cap was
# already tuned for (≤20Y) is byte-for-byte unchanged; only the newly
# unlocked 20-50Y range gets a ceiling that scales with the horizon instead
# of silently zeroing out.
# ---------------------------------------------------------------------------
GENERIC_WM_SANITY_CAP = 50.0
MAX_XIRR_FOR_DEBT = 0.20      # no real Indian debt fund sustains 20% p.a.
MAX_XIRR_FOR_GENERIC = 0.22   # no real equity/hybrid/gold fund sustains 22% p.a. for decades


def max_plausible_wm(category: str, horizon_years: int) -> float:
    """Upper bound a fund's Wealth Multiple can plausibly reach at this
    horizon before it's almost certainly a NAV data artifact rather than
    real performance, used to drop poisoned rolling windows before they
    reach the blended-WM/FQS calculation.

    Debt gets a horizon-scaled ceiling derived the same way min_wm_for()
    derives its floor (compounding a plausible annual rate over the
    horizon). Every other category keeps the flat 50x sanity cap for short/
    medium horizons, but also scales upward for long horizons so genuine
    decades-long compounding isn't mistaken for corrupted NAV data.
    """
    if category == "Debt" and horizon_years and horizon_years > 0:
        return (1.0 + MAX_XIRR_FOR_DEBT) ** horizon_years
    if horizon_years and horizon_years > 0:
        return max(GENERIC_WM_SANITY_CAP, (1.0 + MAX_XIRR_FOR_GENERIC) ** horizon_years)
    return GENERIC_WM_SANITY_CAP


# ---------------------------------------------------------------------------
# Lumpsum benchmark constants
# ---------------------------------------------------------------------------
# Source: FINAL MODEL sheet, rows 43-50 (AXIS SMALLCAP_MASTER_NAV_LUMPSUM.xlsx)
#
# min_wm(category, horizon) = (1 + xirr_req) ^ horizon_years
# This is exact — the Excel formulas are literally: =1*(1+C44)^D$43
#
# Only 7 categories are defined in the lumpsum FINAL MODEL.
# ELSS → Flexi Cap, Hybrid → Flexi Cap, Index Fund → Large Cap (conservative).

LUMPSUM_XIRR_REQ: dict[str, float] = {
    "Large Cap":     0.12,
    "Flexi Cap":     0.13,
    "Multi Cap":     0.14,
    "Mid Cap":       0.15,
    "Small Cap":     0.17,
    "Debt":          0.07,
    "Gold/Silver":   0.08,
    # Mapped categories
    "ELSS":          0.13,   # treated as Flexi Cap for lumpsum
    "Hybrid":        0.13,   # treated as Flexi Cap for lumpsum (conservative)
    "Index Fund":    0.12,   # treated as Large Cap for lumpsum
    "Gold and Silver": 0.08, # alternate spelling
    "Misc Fund":       0.13, # treated as Flexi Cap for unknown funds
}

LUMPSUM_CAP: dict[str, int] = {
    "Large Cap":     130,
    "Flexi Cap":     140,
    "Multi Cap":     140,
    "Mid Cap":       145,
    "Small Cap":     150,
    "Debt":          110,
    "Gold/Silver":   120,
    # Mapped categories
    "ELSS":          140,
    "Hybrid":        140,
    "Index Fund":    130,
    "Gold and Silver": 120,
    "Misc Fund":       140,
}

# Equity flag for lumpsum FQS weight branch
# Non-equity categories use non-equity weights (more weight on risk vs return)
LUMPSUM_EQUITY_CATEGORIES = {
    "Large Cap", "Flexi Cap", "Multi Cap", "Mid Cap", "Small Cap", "ELSS", "Misc Fund"
}


def get_lumpsum_min_wm(category: str, horizon_years: int) -> float:
    """Return the minimum Wealth Multiple required for category over horizon_years.

    Formula: (1 + xirr_req) ^ horizon_years
    Matches FINAL MODEL!D44:G50 exactly.
    """
    xirr = LUMPSUM_XIRR_REQ.get(category, LUMPSUM_XIRR_REQ[DEFAULT_CATEGORY])
    return (1.0 + xirr) ** horizon_years


def get_lumpsum_cap(category: str) -> int:
    """Return WM/Downside score cap for the category (from FINAL MODEL!B44:B50)."""
    return LUMPSUM_CAP.get(category, LUMPSUM_CAP[DEFAULT_CATEGORY])


def is_equity_category(category: str) -> bool:
    """True if the category uses the equity FQS weight branch for lumpsum."""
    return category in LUMPSUM_EQUITY_CATEGORIES


# ---------------------------------------------------------------------------
# Two-level UI category display
# ---------------------------------------------------------------------------

def get_display_category(scheme_category: str) -> tuple[str, str]:
    """Parse AMFI scheme_category → (broad_display, sub_display) for the UI filter.

    AMFI format: "Equity Scheme - Small Cap Fund"
    Returns:     ("Equity", "Small Cap Fund")
    """
    sc = (scheme_category or "").strip()
    sc_lower = sc.lower()

    # ── Broad ───────────────────────────────────────────────────────────────
    if sc_lower.startswith("equity scheme"):
        broad = "Equity"
    elif sc_lower.startswith("debt scheme"):
        broad = "Debt"
    elif sc_lower.startswith("hybrid scheme"):
        broad = "Hybrid"
    elif "solution oriented" in sc_lower:
        broad = "Solution Oriented"
    elif sc_lower.startswith("other scheme"):
        broad = "Other"
    else:
        broad = "Other"

    # ── Sub ─────────────────────────────────────────────────────────────────
    if " - " not in sc:
        return broad, broad   # bare type with no sub

    raw_sub = sc.split(" - ", 1)[1].strip()
    sub_lower = raw_sub.lower()

    if broad == "Equity":
        if "multi cap" in sub_lower:
            return broad, "Multi Cap Fund"
        if "large & mid" in sub_lower or "large and mid" in sub_lower:
            return broad, "Large & Mid Cap Fund"
        if "large cap" in sub_lower:
            return broad, "Large Cap Fund"
        if "mid cap" in sub_lower:
            return broad, "Mid Cap Fund"
        if "small cap" in sub_lower:
            return broad, "Small Cap Fund"
        if "dividend yield" in sub_lower:
            return broad, "Dividend Yield Fund"
        if "value" in sub_lower or "contra" in sub_lower:
            return broad, "Value / Contra Fund"
        if "focused" in sub_lower:
            return broad, "Focused Fund"
        if "sectoral" in sub_lower or "thematic" in sub_lower:
            return broad, "Sectoral / Thematic Fund"
        if "elss" in sub_lower or "tax sav" in sub_lower:
            return broad, "ELSS"
        if "flexi cap" in sub_lower:
            return broad, "Flexi Cap Fund"

    elif broad == "Debt":
        if "overnight" in sub_lower:
            return broad, "Overnight Fund"
        if "liquid" in sub_lower:
            return broad, "Liquid Fund"
        if "ultra short" in sub_lower:
            return broad, "Ultra Short Duration Fund"
        if "low duration" in sub_lower:
            return broad, "Low Duration Fund"
        if "money market" in sub_lower:
            return broad, "Money Market Fund"
        if "short duration" in sub_lower:
            return broad, "Short Duration Fund"
        if "medium to long" in sub_lower:
            return broad, "Medium to Long Duration Fund"
        if "medium duration" in sub_lower:
            return broad, "Medium Duration Fund"
        if "long duration" in sub_lower:
            return broad, "Long Duration Fund"
        if "dynamic bond" in sub_lower:
            return broad, "Dynamic Bond Fund"
        if "corporate bond" in sub_lower:
            return broad, "Corporate Bond Fund"
        if "credit risk" in sub_lower:
            return broad, "Credit Risk Fund"
        if "banking and psu" in sub_lower or "banking & psu" in sub_lower:
            return broad, "Banking and PSU Fund"
        if "10 year" in sub_lower or "constant duration" in sub_lower:
            return broad, "Gilt Fund with 10-year Constant Duration"
        if "gilt" in sub_lower:
            return broad, "Gilt Fund"
        if "floater" in sub_lower:
            return broad, "Floater Fund"

    elif broad == "Hybrid":
        if "conservative" in sub_lower:
            return broad, "Conservative Hybrid Fund"
        if "balanced hybrid" in sub_lower:
            return broad, "Balanced Hybrid Fund"
        if "aggressive" in sub_lower:
            return broad, "Aggressive Hybrid Fund"
        if "dynamic asset" in sub_lower or "balanced advantage" in sub_lower:
            return broad, "Dynamic Asset Allocation / Balanced Advantage"
        if "multi asset" in sub_lower:
            return broad, "Multi Asset Allocation Fund"
        if "arbitrage" in sub_lower:
            return broad, "Arbitrage Fund"
        if "equity savings" in sub_lower:
            return broad, "Equity Savings Fund"

    elif broad == "Solution Oriented":
        if "retirement" in sub_lower:
            return broad, "Retirement Fund"
        if "children" in sub_lower or "child" in sub_lower:
            return broad, "Children's Fund"

    elif broad == "Other":
        if "etf" in sub_lower or "exchange traded" in sub_lower:
            return broad, "ETFs"
        if "fof" in sub_lower or "fund of fund" in sub_lower:
            return broad, "Fund of Funds (FoF)"
        if "index" in sub_lower:
            return broad, "Index Funds"

    return broad, raw_sub  # fallback to raw AMFI string