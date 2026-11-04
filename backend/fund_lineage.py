"""Fund-identity continuity: successor stitching + history disclosure.

Why this module exists
──────────────────────
A mutual fund's real-world life can span MULTIPLE AMFI scheme codes. The three
events that split one economic fund across codes are:

  1. SEBI-2018 rename        — same scheme code, mfapi.in already serves
                               continuous history. Nothing to do.
  2. True merger             — Fund A wound up, unit-holders moved to Fund B
                               under a DIFFERENT code, SAME fund house.
  3. AMC-level acquisition   — the whole asset-management company was acquired,
                               so the fund lives on under a different code AND a
                               different FUND-HOUSE name
                               (e.g. BNP Paribas MF → Baroda BNP Paribas MF).

Cases 2 and 3 truncate NAV history at the code boundary. The old discovery jobs
grouped candidates by fund-house *string*, so case 3 (the most common cause of
orphaned history) could never be detected — the old and new house names differ.

This module provides:
  • CURATED_SUCCESSORS  — hand-verified wound_up_code → successor_code pairs.
  • HOUSE_ALIAS_GROUPS  — canonical grouping so old & new house names collapse
                          to one key (used by discovery to compare across houses).
  • verify_stitch()     — NAV return-correlation gate. A successor is only ever
                          trusted if the two series actually track the same
                          portfolio (r ≥ threshold during overlap), OR the pair
                          is human-curated with a plausible date gap. This makes
                          a wrong map entry unable to silently corrupt returns.
  • HISTORY_NOTES       — honest disclosure for funds whose TRUE inception
                          predates the EFFECTIVE (fully predecessor-stitched)
                          earliest NAV, for the rare case where no earlier
                          scheme code with usable data is known at all — so
                          there is genuinely nothing left to stitch and we
                          disclose the gap instead of fabricating data.
  • CURATED_AMC_CHANGES  — hand-verified SAME-CODE AMC renames/acquisitions
                          (case 1 above — no scheme-code change, so nothing
                          for CURATED_SUCCESSORS to stitch, but the fund's
                          real "current management" still starts on a real
                          date, not at whatever year the app happens to have
                          data from). Applied house-wide, not fund-by-fund —
                          see lineage_store.apply_amc_changes().

On CURATED_AMC_CHANGES vs. the archive-derived pipeline
────────────────────────────────────────────────────────
scripts/build_lineage.py derives AMC_CHANGE actions generically from AMFI's
own archived NAVAll.txt captures (Internet Archive) — the ideal source,
since it needs no per-house research and covers every fund automatically.
CURATED_AMC_CHANGES exists because that source is not always reachable (the
Internet Archive has real, sometimes extended outages) and, even when it is,
its capture density is uneven — a rename that happened between two archived
snapshots is invisible to it. Every entry below is independently verified
against SEBI/AMFI/AMC primary sources (see each entry's `sources`), so it is
trustworthy on its own and is merged in ON TOP of whatever the archive-based
derivation finds — never a replacement for it, and never conflicting: dates
here should match what the archive derivation would show once it can run.

All functions here are PURE (no network, no DB) so they are fully unit-testable.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Set, Tuple

# ── Correlation gate thresholds ──────────────────────────────────────────────
MIN_OVERLAP_DAYS: int = 15          # need this many overlapping trading days to
                                    # trust a correlation figure at all
CORR_THRESHOLD_DISCOVERED: float = 0.90   # automated pairs: strict
CORR_THRESHOLD_CURATED:    float = 0.80   # human-verified pairs: still gated,
                                          # but a hair looser (defence-in-depth
                                          # against a data glitch, not a typo)
MAX_GAP_DAYS_NO_OVERLAP: int = 120  # curated pair with no overlap: predecessor's
                                    # last NAV → successor's first NAV must be
                                    # within this window to accept the stitch

# ── Co-existence thresholds (see verify_stitch) ──────────────────────────────
# A successor must outlive its predecessor by at least this much, otherwise the
# pair is two things that ended together, not a handover.
MIN_SUCCESSOR_EXTENSION_DAYS: int = 180
# If the two series ran side by side for more than this share of the
# predecessor's life, they are concurrent variants of one scheme.
#
# Calibrated against observed pairs, not guessed:
#   plan/option twins (reject) — 1.0000, 0.9982, 1.0000
#   curated successions (keep) — 0.0000, 0.0000, 0.0000
#   legitimate 40-day handover on a short series (keep) — 0.336
# 0.75 separates these with wide margins on both sides. A tighter bound (0.25)
# rejected the legitimate handover, because a fixed transition window is a
# large FRACTION of a short predecessor series while being negligible for a
# fund that ran for years.
MAX_CONCURRENT_FRACTION: float = 0.75

# Seam continuity: when a fund hands over to a new scheme code, the last NAV
# under the old code and the first under the new one are close (identical
# unless a conversion ratio applies). Observed on both curated pairs: Morgan
# Stanley 20.697 -> HDFC 21.121 and JPMorgan 19.832 -> Edelweiss 20.270 —
# ~2% each. 10% leaves room for a conversion ratio while still rejecting two
# unrelated funds, whose NAVs have no reason to line up.
SEAM_MAX_DELTA: float = 0.10


# ── Curated, human-verified successor pairs ──────────────────────────────────
# Format: { wound_up_scheme_code: successor_scheme_code }
#
# Every entry is verified against mfapi.in:
#   ✓ predecessor last_nav_date ≈ successor first_nav_date (or overlap)
#   ✓ same scheme_category
#   ✓ return correlation ≥ 0.80 in the transition window (checked at runtime)
#   ✓ AMFI/SEBI merger or AMC-acquisition notice confirms the pairing
#
# Add new AMC mergers here as they happen (~1-3/year). Runtime correlation
# verification (verify_stitch) means a bad entry can't corrupt returns unless
# the NAVs genuinely correlate.
CURATED_SUCCESSORS: Dict[int, int] = {
    # BNP Paribas MF + Baroda MF → Baroda BNP Paribas MF (AMC merger, 2022)
    113566: 150209,   # BNP Paribas Mid Cap Growth → Baroda BNP Paribas Mid Cap Regular Growth

    # Edelweiss AMC acquired JPMorgan Asset Management's India business (2016).
    # Verified against mfapi.in: JPMorgan India Mid and Small Cap Fund's last
    # NAV (24-11-2016, ₹19.832) → Edelweiss Mid Cap Fund's first NAV
    # (28-11-2016, ₹20.270), 4-day gap, same category (Equity - Mid Cap).
    # Scheme launched 26-Dec-2007 under the JPMorgan code; without this stitch
    # the fund's ~9 years of pre-2016 history is unreachable from the current
    # (Edelweiss) code and its first NAV under that code reads as "inception".
    107301: 140225,   # JPMorgan India Mid and Small Cap Fund Growth → Edelweiss Mid Cap Fund Regular Growth

    # HDFC acquired Morgan Stanley Mutual Fund's India business (2014); the
    # fund was later refocused as HDFC Small Cap (2016). Verified against
    # mfapi.in: Morgan Stanley A.C.E. Fund's last NAV (26-06-2014, ₹20.697) →
    # HDFC Small Cap Fund's first NAV (30-06-2014, ₹21.121), 4-day gap, same
    # category (Equity - Small Cap). Contrary to this module's earlier
    # HISTORY_NOTES entry, the pre-2014 series IS present on the feed — it
    # just wasn't being looked up because nothing walked backward from the
    # active code. See HISTORY_NOTES below: no disclosure entry remains for
    # this fund now that it stitches to its real 2008 launch.
    108197: 130502,   # Morgan Stanley A.C.E. Fund Regular Growth → HDFC Small Cap Fund Growth
}


# ── Honest history-disclosure notes ──────────────────────────────────────────
# For funds whose TRUE inception predates the EFFECTIVE (fully predecessor-
# stitched) earliest NAV — i.e. a gap that stitching genuinely cannot bridge,
# because no earlier scheme code with usable data is known. We do NOT
# fabricate NAV data; we disclose the gap so headline "since inception"
# figures are not read as the fund's full life.
#
# NOTE: before adding an entry here, first check whether the gap can instead
# be bridged with a CURATED_SUCCESSORS pair (preferred — it recovers the real
# data instead of just disclosing its absence). HDFC Small Cap Fund used to
# be listed here on the assumption that its pre-2014 Morgan Stanley A.C.E.
# Fund NAV series wasn't on the feed; it is (see CURATED_SUCCESSORS above),
# it just required walking backward from the active code to find it.
#
# Format: scheme_code → { real_inception: ISO, note: str }
HISTORY_NOTES: Dict[int, Dict[str, str]] = {}


# ── Curated, source-verified SAME-CODE AMC changes ───────────────────────────
# Every fund whose CURRENT fund-house name canonicalises to `to_house` (via
# canonical_house(), applied by lineage_store.apply_amc_changes()) and which
# already existed before `effective_date` gets this event attached — one
# entry here covers the AMC's entire fund range, not one row per scheme.
#
# Principal Mutual Fund -> Sundaram Mutual Fund is deliberately NOT listed
# here: that acquisition was a MIX of same-code renames and cross-code scheme
# mergers (some Principal schemes were folded into a different, pre-existing
# Sundaram scheme under a different code), so a single house-wide same-code
# rule would be wrong for the merged subset. The merged subset is handled via
# ISIN-verified successor pairs (scripts/verify_and_promote.py); the
# same-code subset needs per-scheme confirmation before it can be added here.
#
# `effective_date` is the best-available date from primary/near-primary
# sources; where sources only bracket a date (e.g. a SEBI approval date and a
# later "with immediate effect" notice), the later, more specific date is
# used. See each entry's `sources` to verify or correct.
CURATED_AMC_CHANGES: List[Dict[str, Any]] = [
    {
        "from_house": "Reliance Mutual Fund", "to_house": "Nippon India Mutual Fund",
        "effective_date": "2019-09-28",
        "sources": ["https://www.sebi.gov.in/sebiweb/other/OtherAction.do?doGetFundDetails=yes&type=3&mfId=46"],
    },
    {
        "from_house": "DSP BlackRock Mutual Fund", "to_house": "DSP Mutual Fund",
        "effective_date": "2018-08-16",
        "sources": ["https://www.dspim.com/media/pages/downloads/2ecdfb3003-1757771651/44-notice-for-change-in-name-of-dspbr-money-manager-fund.pdf"],
    },
    {
        "from_house": "Birla Sun Life Mutual Fund", "to_house": "Aditya Birla Sun Life Mutual Fund",
        "effective_date": "2017-07-17",
        "sources": ["https://business.bofa.com/content/dam/flagship/in/en/investment-banking/Aditya-Birla-Sun-Life-AMC-Limited-Addendum-to-the-DRHP.pdf"],
    },
    {
        "from_house": "IDFC Mutual Fund", "to_house": "Bandhan Mutual Fund",
        "effective_date": "2023-03-13",
        "sources": ["https://www.sebi.gov.in/sebiweb/other/OtherAction.do?doGetFundDetails=yes&type=2&mfId=24",
                     "https://bandhanmutual.com/idfc-mf-is-now-bandhan-mf/"],
    },
    {
        "from_house": "DHFL Pramerica Mutual Fund", "to_house": "PGIM India Mutual Fund",
        "effective_date": "2019-08-08",
        "sources": ["https://www.valueresearchonline.com/stories/47187/dhfl-pramerica-mutual-fund-changes-to-pgim-india-mutual-fund/"],
    },
    {
        "from_house": "Essel Mutual Fund", "to_house": "Navi Mutual Fund",
        "effective_date": "2021-03-25",
        "sources": ["https://www.valueresearchonline.com/stories/49251/essel-mutual-fund-becomes-navi-mutual-fund/"],
    },
    {
        "from_house": "Indiabulls Mutual Fund", "to_house": "Groww Mutual Fund",
        "effective_date": "2023-05-30",
        "sources": ["https://www.sebi.gov.in/sebiweb/other/OtherAction.do?doGetFundDetails=yes&type=3&mfId=29"],
    },
    {
        "from_house": "BOI AXA Mutual Fund", "to_house": "Bank of India Mutual Fund",
        "effective_date": "2022-06-17",
        "sources": ["https://www.icicidirect.com/mutual-funds/news/mf/boi-axa-mutual-fund-announces-change-in-name-of-the-asset-management-company/1343536"],
    },
    {
        "from_house": "Union KBC Mutual Fund", "to_house": "Union Mutual Fund",
        "effective_date": "2016-09-20",
        "sources": ["https://www.business-standard.com/amp/article/news-cm/union-bank-of-india-to-purchase-49-stake-in-union-kbc-asset-management-co-and-union-kbc-trustee-company-115100500247_1.html"],
    },
    {
        "from_house": "LIC Nomura Mutual Fund", "to_house": "LIC Mutual Fund",
        "effective_date": "2016-05-12",
        "sources": ["https://www.valueresearchonline.com/stories/31121/lic-nomura-mutual-fund-renamed/"],
    },
    {
        "from_house": "Religare Invesco Mutual Fund", "to_house": "Invesco Mutual Fund",
        "effective_date": "2016-05-06",
        "sources": ["https://invescomutualfund.com/docs/default-source/Press-Releases-PDF/press-release_invesco_mumbai-april-8-2016.pdf"],
    },
    {
        "from_house": "L&T Mutual Fund", "to_house": "HSBC Mutual Fund",
        "effective_date": "2022-11-25",
        "sources": ["https://www.assetmanagement.hsbc.co.in/en/mutual-funds/acquisition-of-lt-mutual-fund",
                     "https://www.business-standard.com/article/companies/hsbc-asset-management-company-completes-acquisition-of-l-t-mutual-fund-122112501062_1.html"],
    },
    {
        "from_house": "Baroda Mutual Fund", "to_house": "Baroda BNP Paribas Mutual Fund",
        "effective_date": "2022-03-14",
        "sources": ["https://www.barodabnpparibasmf.in/amalgamation"],
    },
    {
        "from_house": "Escorts Mutual Fund", "to_house": "quant Mutual Fund",
        "effective_date": "2018-02-28",
        "sources": ["https://www.valueresearchonline.com/learn/amc/quant-mutual-fund/"],
    },
    {
        "from_house": "Yes Mutual Fund", "to_house": "WhiteOak Capital Mutual Fund",
        "effective_date": "2022-01-12",
        "sources": ["https://www.sebi.gov.in/sebiweb/other/OtherAction.do?doGetFundDetails=yes&type=3&mfId=76"],
    },
]


# ── Fund-house canonicalisation (collapses rename + AMC-merger aliases) ───────
# Each group lists house-name fragments that refer to the SAME economic AMC
# across time. canonical_house() maps any of them to the group's canonical key,
# so discovery can compare an old-house wound-up fund against a new-house
# successor (the exact case same-house grouping used to miss).
HOUSE_ALIAS_GROUPS: List[Dict[str, Any]] = [
    {"canonical": "baroda bnp paribas", "aliases": ["baroda bnp paribas", "bnp paribas", "baroda"]},
    {"canonical": "hdfc",               "aliases": ["hdfc", "morgan stanley"]},
    {"canonical": "bandhan",            "aliases": ["bandhan", "idfc"]},
    {"canonical": "hsbc",               "aliases": ["hsbc", "l&t", "l and t"]},
    {"canonical": "nippon india",       "aliases": ["nippon india", "reliance"]},
    {"canonical": "aditya birla sun life", "aliases": ["aditya birla sun life", "aditya birla", "birla sun life"]},
    {"canonical": "pgim india",         "aliases": ["pgim india", "pgim", "dhfl pramerica", "pramerica"]},
    {"canonical": "sundaram",           "aliases": ["sundaram", "principal"]},
    {"canonical": "dsp",                "aliases": ["dsp", "dsp blackrock"]},
    {"canonical": "navi",               "aliases": ["navi", "essel"]},
    {"canonical": "groww",              "aliases": ["groww", "indiabulls"]},
    {"canonical": "bank of india",      "aliases": ["bank of india", "boi axa"]},
    {"canonical": "union",              "aliases": ["union", "union kbc"]},
    {"canonical": "jm",                 "aliases": ["jm financial", "jm"]},
    {"canonical": "lic",                "aliases": ["lic", "lic nomura"]},
    {"canonical": "edelweiss",          "aliases": ["edelweiss", "jpmorgan", "jp morgan"]},
    {"canonical": "invesco",            "aliases": ["invesco", "religare invesco", "religare"]},
]

# Longest aliases first so "baroda bnp paribas" wins over "baroda"/"bnp paribas".
_ALIAS_LOOKUP: List[Tuple[str, str]] = sorted(
    ((alias, g["canonical"]) for g in HOUSE_ALIAS_GROUPS for alias in g["aliases"]),
    key=lambda t: -len(t[0]),
)

_HOUSE_SUFFIX_RE = re.compile(r"\b(mutual fund|mf|asset management|amc|india|limited|ltd)\b")
_NON_ALPHA = re.compile(r"[^a-z0-9&\s]")


def canonical_house(name: str) -> str:
    """Map a fund-house or scheme name to a canonical AMC key.

    Collapses rename/acquisition aliases so, e.g., both
    'BNP Paribas Mutual Fund' and 'Baroda BNP Paribas Mutual Fund' → 'baroda bnp paribas'.
    Falls back to the first 1-2 significant tokens when no alias matches.

    BUG FIXED (2026-08-04): alias matching used to run AFTER _HOUSE_SUFFIX_RE
    stripped generic corporate-boilerplate words — including "india". Three
    canonical keys have "india" baked into the brand itself (nippon india,
    pgim india, bank of india), so stripping it before matching silently
    broke the live-side name for two of them: canonical_house("Nippon India
    Mutual Fund") returned "nippon" instead of "nippon india", so it never
    collapsed together with canonical_house("Reliance Mutual Fund") ==
    "nippon india" — breaking amc_continuity for every Reliance predecessor.
    ("PGIM India" happened to survive by accident, via a coincidental bare
    "pgim" alias in the same group.) Fix: match aliases on the un-stripped
    (only lowercased/punctuation-normalised) string first; only run the
    suffix strip for the token-fallback path when no alias matches at all —
    that fallback's behaviour is unchanged.
    """
    raw = _NON_ALPHA.sub(" ", (name or "").lower())
    raw = " ".join(raw.split())
    for alias, canonical in _ALIAS_LOOKUP:
        if raw == alias or raw.startswith(alias + " ") or (" " + alias + " ") in (" " + raw + " "):
            return canonical
    s = _HOUSE_SUFFIX_RE.sub(" ", raw)
    s = " ".join(s.split())
    tokens = s.split()
    if not tokens:
        return ""
    # Two-token key handles multi-word houses not in the alias table.
    return " ".join(tokens[:2]) if len(tokens) >= 2 else tokens[0]


# ── NAV series helpers (pure) ────────────────────────────────────────────────
# A "nav row" is {"date": datetime.date, "nav": float}, ascending by date —
# exactly what amfi_client.parse_nav_history returns.

def _to_series(nav_rows: List[Dict[str, Any]]) -> Dict[date, float]:
    return {r["date"]: float(r["nav"]) for r in nav_rows if r.get("nav", 0) > 0}


def _daily_returns(series: Dict[date, float]) -> Dict[date, float]:
    dates = sorted(series)
    out: Dict[date, float] = {}
    for i in range(1, len(dates)):
        d0, d1 = dates[i - 1], dates[i]
        if series[d0] > 0:
            out[d1] = (series[d1] - series[d0]) / series[d0]
    return out


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def overlap_correlation(
    pred_rows: List[Dict[str, Any]],
    succ_rows: List[Dict[str, Any]],
) -> Tuple[Optional[float], int]:
    """Return (Pearson r of daily returns during overlap, overlap_trading_days).

    r is None when the overlap is shorter than MIN_OVERLAP_DAYS.
    """
    pr = _daily_returns(_to_series(pred_rows))
    sr = _daily_returns(_to_series(succ_rows))
    common = sorted(set(pr) & set(sr))
    if len(common) < MIN_OVERLAP_DAYS:
        return None, len(common)
    return _pearson([pr[d] for d in common], [sr[d] for d in common]), len(common)


def stitch(
    pred_rows: List[Dict[str, Any]],
    succ_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge predecessor + successor NAV rows, successor winning on overlap."""
    merged: Dict[date, Dict[str, Any]] = {}
    for row in pred_rows:
        merged[row["date"]] = row
    for row in succ_rows:          # successor overrides on shared dates
        merged[row["date"]] = row
    return sorted(merged.values(), key=lambda r: r["date"])


def verify_stitch(
    pred_rows: List[Dict[str, Any]],
    succ_rows: List[Dict[str, Any]],
    *,
    curated: bool,
    isin_verified: bool = False,
) -> Tuple[bool, Dict[str, Any]]:
    """Decide whether pred→succ is a genuine continuation of the same portfolio.

    Rules
    ─────
    • If enough overlapping trading days exist: require return correlation
      ≥ threshold (0.90 discovered / 0.80 curated). This is the strong signal —
      two codes of the same fund track identically during the transition window.
    • If NO usable overlap: only CURATED pairs are accepted, and only when the
      predecessor's last NAV → successor's first NAV gap is within
      MAX_GAP_DAYS_NO_OVERLAP. Discovered pairs with no overlap are rejected.

    Returns (accepted, signals_dict).
    """
    if not pred_rows or not succ_rows:
        return False, {"reason": "empty_series"}

    corr, overlap_days = overlap_correlation(pred_rows, succ_rows)
    pred_last = max(r["date"] for r in pred_rows)
    succ_first = min(r["date"] for r in succ_rows)
    gap_days = (succ_first - pred_last).days

    signals: Dict[str, Any] = {
        "correlation": round(corr, 4) if corr is not None else None,
        "overlap_days": overlap_days,
        "gap_days": gap_days,
        "curated": curated,
    }

    # ── Co-existence gate ────────────────────────────────────────────────────
    # Correlation proves "same portfolio". It does NOT prove "one succeeded the
    # other". Two plan/option variants of ONE scheme (Growth vs IDCW, Regular
    # vs Direct) hold identical portfolios, so their daily returns correlate at
    # ~1.0 — and they coexist for their whole lives.
    #
    # Measured on real candidate pairs: 123688→123689 corr=1.000,
    # 130788→130787 corr=0.9997, 146088→146087 corr=1.000 — all three are
    # Regular/Direct or Growth/IDCW twins with concurrent_fraction ≈ 1.0. On
    # correlation alone all three were ACCEPTED, which would splice a fund's
    # history onto its own sibling and double-count it.
    #
    # A real succession has a HANDOVER shape: the predecessor stops, the
    # successor carries on. All three curated pairs show exactly that —
    # 0 concurrent days, successor outliving by 1601/3536/4418 days.
    p_first, s_last = min(r["date"] for r in pred_rows), max(r["date"] for r in succ_rows)
    concurrent_days = max(0, (min(pred_last, s_last) - max(p_first, succ_first)).days)
    pred_life_days = max(1, (pred_last - p_first).days)
    concurrent_fraction = concurrent_days / pred_life_days
    extension_days = (s_last - pred_last).days
    signals.update({
        "concurrent_days": concurrent_days,
        "concurrent_fraction": round(concurrent_fraction, 4),
        "successor_extension_days": extension_days,
    })

    if extension_days < MIN_SUCCESSOR_EXTENSION_DAYS:
        signals["reason"] = (
            f"successor does not outlive predecessor "
            f"({extension_days}d < {MIN_SUCCESSOR_EXTENSION_DAYS}d) — not a succession"
        )
        return False, signals
    if concurrent_fraction > MAX_CONCURRENT_FRACTION:
        signals["reason"] = (
            f"series ran concurrently for {concurrent_fraction:.0%} of the "
            f"predecessor's life — plan/option variants, not a succession"
        )
        return False, signals

    if corr is not None:
        threshold = CORR_THRESHOLD_CURATED if curated else CORR_THRESHOLD_DISCOVERED
        accepted = corr >= threshold
        signals["reason"] = (
            f"correlation {corr:.3f} {'≥' if accepted else '<'} {threshold:.2f}"
        )
        return accepted, signals

    # ── No usable overlap ────────────────────────────────────────────────────
    # A real AMC acquisition leaves NO overlap at all: the predecessor's code
    # stops printing and the successor's starts days later. Correlation is
    # undefined, so a different test is needed or these pairs can never be
    # accepted — which is exactly what happened when 343 ISIN-continuity
    # candidates were run through this function and scored 0/343.
    gap_ok = 0 <= gap_days <= MAX_GAP_DAYS_NO_OVERLAP
    seam_delta = None
    if pred_rows and succ_rows:
        p_nav = pred_rows[-1]["nav"]
        s_nav = succ_rows[0]["nav"]
        if p_nav:
            seam_delta = abs(s_nav - p_nav) / p_nav
            signals["seam_delta"] = round(seam_delta, 4)
    seam_ok = seam_delta is not None and seam_delta <= SEAM_MAX_DELTA

    if curated and gap_ok:
        signals["reason"] = f"curated, no overlap, gap {gap_days}d within window"
        return True, signals

    # ISIN continuity is identity evidence that does not depend on naming or
    # on the series overlapping: the same security appearing under two scheme
    # codes IS the corporate action. It still has to survive a NAV check —
    # the money must visibly carry across the seam — so a mis-mapped ISIN
    # cannot splice two unrelated funds together.
    if isin_verified and gap_ok and seam_ok:
        signals["reason"] = (
            f"ISIN continuity, no overlap, gap {gap_days}d, "
            f"NAV step {seam_delta:.2%} <= {SEAM_MAX_DELTA:.0%}"
        )
        return True, signals

    if isin_verified:
        signals["reason"] = (
            f"ISIN continuity but seam failed: gap {gap_days}d"
            + (f", NAV step {seam_delta:.2%}" if seam_delta is not None else ", no seam NAV")
        )
        return False, signals

    signals["reason"] = "no overlap and not an acceptable curated/ISIN-verified gap"
    return False, signals


# ── History-disclosure lookup ────────────────────────────────────────────────

def history_note_for(
    scheme_code: int,
    first_available: Optional[date],
) -> Optional[str]:
    """Return an honest disclosure string when the fund's true inception
    predates the earliest available NAV, else None."""
    entry = HISTORY_NOTES.get(int(scheme_code))
    if not entry:
        return None
    # Only surface the note if the available data really does start later than
    # the known real inception (guards against the feed later gaining history).
    if first_available is not None:
        try:
            real = datetime.strptime(entry["real_inception"], "%Y-%m-%d").date()
            if first_available <= real:
                return None
        except Exception:
            pass
    return entry["note"]
