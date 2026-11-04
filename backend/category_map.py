"""Category → Minimum Wealth Multiple (WM) benchmark table.

Drawn directly from the FINAL MODEL sheet of the MF Analysis spec.
The 'cap' column normalises raw scores (Blended WM, Downside) to a 0-100 band.
"""

from typing import Optional

# (min_xirr, min_wm_3y, min_wm_5y, min_wm_10y, min_wm_15y, cap)
CATEGORY_TABLE = {
    "Debt":        {"xirr": 0.07, "wm": {3: 1.108, 5: 1.193, 10: 1.443, 15: 1.761}, "cap": 110, "type": "non_equity"},
    "Gold/Silver": {"xirr": 0.08, "wm": {3: 1.125, 5: 1.225, 10: 1.524, 15: 1.922}, "cap": 120, "type": "non_equity"},
    "Index Fund":  {"xirr": 0.10, "wm": {3: 1.160, 5: 1.290, 10: 1.710, 15: 2.300}, "cap": 120, "type": "equity"},
    "Hybrid":      {"xirr": 0.11, "wm": {3: 1.178, 5: 1.325, 10: 1.808, 15: 2.526}, "cap": 120, "type": "non_equity"},
    "Large Cap":   {"xirr": 0.12, "wm": {3: 1.197, 5: 1.362, 10: 1.917, 15: 2.776}, "cap": 130, "type": "equity"},
    "Flexi Cap":   {"xirr": 0.13, "wm": {3: 1.214, 5: 1.398, 10: 2.033, 15: 3.054}, "cap": 140, "type": "equity"},
    "ELSS":        {"xirr": 0.13, "wm": {3: 1.214, 5: 1.398, 10: 2.033, 15: 3.054}, "cap": 140, "type": "equity"},
    "Multi Cap":   {"xirr": 0.14, "wm": {3: 1.233, 5: 1.437, 10: 2.159, 15: 3.366}, "cap": 140, "type": "equity"},
    "Mid Cap":     {"xirr": 0.15, "wm": {3: 1.253, 5: 1.477, 10: 2.293, 15: 3.714}, "cap": 145, "type": "equity"},
    "Small Cap":   {"xirr": 0.17, "wm": {3: 1.292, 5: 1.560, 10: 2.593, 15: 4.541}, "cap": 150, "type": "equity"},
}

DEFAULT_CATEGORY = "Flexi Cap"


def classify_fund(scheme_name: str, scheme_category: str = "") -> str:
    """Map an AMFI scheme to one of our internal benchmark buckets.

    Two-pass classifier:
      Pass 1 — AMFI canonical `scheme_category` (e.g. "Equity Scheme - Small
               Cap Fund", "Hybrid Scheme - Arbitrage Fund", "Debt Scheme -
               Gilt Fund"). This field is structured and authoritative when
               mfapi.in returns it.
      Pass 2 — keyword match in `name + category` for funds where the AMFI
               category is missing or non-standard (e.g. older schemes).

    Falls back to DEFAULT_CATEGORY (Flexi Cap) only when neither pass hits.
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
            # Sectoral/thematic volatility varies; Flexi Cap is the safest
            # benchmark default (XIRR 13%). Known limitation.
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
            or "floater" in s):
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
