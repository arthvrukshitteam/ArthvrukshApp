"""server.py — tests for /funds/search plan/option variant collapsing.

Fixtures are drawn from the shapes actually present in the live AMFI list
(measured, not invented):

  · "Motilal Oswal Liquid Fund" — 16 distinct scheme codes, byte-identical
    names. The pre-existing `seen` set never caught these because it de-dupes
    repeats of the SAME code.
  · "Edelweiss Liquid Fund" / "… - Growth Option" / "… - Regular Plan -
    Growth" — one scheme, three spellings, three codes.

Both must collapse to one row, and the row kept must be the one the rest of
the app would show.
"""
import os
import sys

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import (  # noqa: E402
    _dedupe_search_variants,
    _search_stem,
    _search_variant_rank,
)


def row(code, name, **extra):
    return {"scheme_code": code, "scheme_name": name, **extra}


# ── Kind A: identical names under different codes ────────────────────────────

def test_identical_names_under_many_codes_collapse_to_one():
    rows = [row(c, "Motilal Oswal Liquid Fund")
            for c in (145834, 145946, 146218, 146220, 146222, 147441)]
    out = _dedupe_search_variants(rows)
    assert len(out) == 1
    assert out[0]["variant_count"] == 6


def test_identical_name_collapse_is_deterministic_across_orderings():
    """Kind A rows tie on every name-based key, so without the scheme_code
    tiebreak the survivor would depend on list order — a search returning a
    different code run to run."""
    codes = [147441, 145834, 146220, 145946]
    a = _dedupe_search_variants([row(c, "Motilal Oswal Liquid Fund") for c in codes])
    b = _dedupe_search_variants([row(c, "Motilal Oswal Liquid Fund") for c in reversed(codes)])
    assert a[0]["scheme_code"] == b[0]["scheme_code"] == min(codes)


# ── Kind B: one scheme, several plan/option spellings ────────────────────────

def test_plan_option_spellings_collapse_and_keep_growth():
    rows = [
        row(1, "Edelweiss Liquid Fund"),
        row(2, "Edelweiss Liquid Fund - Growth Option"),
        row(3, "Edelweiss Liquid Fund - Regular Plan - Growth"),
    ]
    out = _dedupe_search_variants(rows)
    assert len(out) == 1
    assert "Growth" in out[0]["scheme_name"]
    assert out[0]["variant_count"] == 3


def test_payout_variant_loses_to_growth():
    rows = [
        row(1, "Some Equity Fund - Growth Plan - Bonus Option"),
        row(2, "Some Equity Fund - Growth Plan - Growth Option"),
    ]
    out = _dedupe_search_variants(rows)
    assert len(out) == 1
    assert out[0]["scheme_code"] == 2


def test_casing_and_punctuation_differences_still_group():
    rows = [
        row(1, "BNP PARIBAS LIQUID FUND  GROWTH OPTION"),
        row(2, "BNP PARIBAS LIQUID FUND - GROWTH OPTION"),
        row(3, "BNP PARIBAS LIQUID Fund-Regular Plan-Growth Option"),
    ]
    assert len(_dedupe_search_variants(rows)) == 1


# ── What must NOT be merged ──────────────────────────────────────────────────

def test_genuinely_different_schemes_are_kept_apart():
    rows = [
        row(1, "Edelweiss Liquid Fund"),
        row(2, "Edelweiss Large Cap Fund"),
        row(3, "Edelweiss Mid Cap Fund"),
        row(4, "Motilal Oswal Liquid Fund"),
    ]
    out = _dedupe_search_variants(rows)
    assert len(out) == 4


def test_same_amc_different_category_not_merged():
    rows = [row(1, "HDFC Flexi Cap Fund - Growth"),
            row(2, "HDFC Small Cap Fund - Growth")]
    assert len(_dedupe_search_variants(rows)) == 2


def test_single_row_group_gets_no_variant_count():
    """variant_count is the UI's signal that rows were folded in — putting it
    on an un-collapsed row would claim a duplication that didn't happen."""
    out = _dedupe_search_variants([row(1, "Solo Fund - Growth")])
    assert "variant_count" not in out[0]


def test_first_seen_order_is_preserved():
    rows = [row(1, "Zeta Fund - Growth"), row(2, "Alpha Fund - Growth"),
            row(3, "Zeta Fund - Regular Plan - Growth")]
    out = _dedupe_search_variants(rows)
    assert [r["scheme_name"].split()[0] for r in out] == ["Zeta", "Alpha"]


# ── Lineage annotation must survive collapsing ───────────────────────────────

def test_lineage_row_wins_an_otherwise_equal_tie():
    """A lineage hit carries the 'formerly known as' explanation that told the
    user why an old name matched. Dropping it for an identical plain row would
    silently remove that explanation."""
    rows = [
        row(1, "Nippon India Small Cap Fund"),
        row(2, "Nippon India Small Cap Fund", matched_via="lineage",
            formerly_known_as="Reliance Small Cap Fund"),
    ]
    out = _dedupe_search_variants(rows)
    assert len(out) == 1
    assert out[0]["formerly_known_as"] == "Reliance Small Cap Fund"


def test_growth_tier_still_beats_lineage_annotation():
    """Which SCHEME to show is decided before which row explains it best — a
    lineage-annotated payout variant must not displace the Growth option."""
    rows = [
        row(1, "Some Fund - Growth Plan - Bonus Option", matched_via="lineage"),
        row(2, "Some Fund - Growth Plan - Growth Option"),
    ]
    out = _dedupe_search_variants(rows)
    assert out[0]["scheme_code"] == 2


# ── Degenerate input ─────────────────────────────────────────────────────────

def test_rows_whose_names_normalise_to_nothing_are_not_all_merged():
    rows = [row(1, "-"), row(2, "."), row(3, "")]
    assert len(_dedupe_search_variants(rows)) == 3


def test_empty_input():
    assert _dedupe_search_variants([]) == []


def test_rank_is_a_total_order_usable_by_min():
    rows = [row(2, "Fund B - Growth"), row(1, "Fund A - Bonus")]
    assert min(rows, key=_search_variant_rank)["scheme_code"] == 2


# ── Payout-option stripping, and the strategy names it must not touch ────────

def test_bonus_option_collapses_into_growth():
    """306 Bonus listings sit beside their Growth twin in the live master
    list, and Home already hides them — search must agree."""
    rows = [
        row(1, "Nippon India Growth Mid Cap Fund - Regular Plan - Bonus Option"),
        row(2, "Nippon India Growth Mid Cap Fund - Regular Plan - Growth Option"),
    ]
    out = _dedupe_search_variants(rows)
    assert len(out) == 1
    assert out[0]["scheme_code"] == 2


def test_dividend_yield_is_a_strategy_not_a_payout_option():
    """The negative lookahead is load-bearing: stripping "dividend" here would
    dissolve 22 real funds' identities and could merge them with unrelated
    schemes from the same AMC."""
    assert "yield" in _search_stem("Aditya Birla Sun Life Dividend Yield Fund - Growth")
    a = _search_stem("Aditya Birla Sun Life Dividend Yield Fund - Regular Plan - GROWTH")
    b = _search_stem("Aditya Birla Sun Life Flexi Cap Fund - Growth")
    assert a != b


def test_dividend_yield_fund_not_merged_with_sibling_scheme():
    rows = [
        row(1, "Baroda BNP Paribas Dividend Yield Fund - Regular Plan - Growth Option"),
        row(2, "Baroda BNP Paribas Large Cap Fund - Regular Plan - Growth Option"),
    ]
    assert len(_dedupe_search_variants(rows)) == 2


def test_dividend_opportunities_is_also_protected():
    assert "opportunit" in _search_stem("Some AMC Dividend Opportunities Fund - Growth")


# ── Wound-up variants must not swallow a live scheme ─────────────────────────
#
# These cover a regression the first cut of this feature had: dedup ran BEFORE
# _annotate_fund_status, so the representative was chosen blind to is_active.
# When the best-named variant was wound up and a live sibling existed,
# active_only=true then dropped the whole group and a perfectly available fund
# became unselectable in the Add Fund picker.

def test_live_variant_beats_a_dead_one_even_when_worse_named():
    """A user can act on a live Bonus option and cannot act on a dead Growth
    option, so is_active outranks the Growth preference."""
    rows = [
        row(1, "Some Fund - Growth Plan - Growth Option", is_active=False),
        row(2, "Some Fund - Growth Plan - Bonus Option", is_active=True),
    ]
    out = _dedupe_search_variants(rows)
    assert len(out) == 1
    assert out[0]["scheme_code"] == 2


def test_dead_variant_still_wins_when_every_sibling_is_dead():
    """Nothing live to prefer — the group must still yield a row rather than
    vanish, so a lookup for a wound-up fund keeps working."""
    rows = [
        row(1, "Some Fund - Growth Plan - Bonus Option", is_active=False),
        row(2, "Some Fund - Growth Plan - Growth Option", is_active=False),
    ]
    out = _dedupe_search_variants(rows)
    assert len(out) == 1
    assert out[0]["scheme_code"] == 2          # falls back to the Growth rank


def test_unknown_status_is_not_treated_as_dead():
    """_annotate_fund_status leaves is_active absent for a brand-new scheme,
    and returns rows untouched if its lookup fails. Neither may demote a row
    below a genuinely dead one."""
    rows = [
        row(1, "Some Fund - Growth Plan - Growth Option"),               # unknown
        row(2, "Some Fund - Growth Plan - Growth Option", is_active=False),
    ]
    out = _dedupe_search_variants(rows)
    assert out[0]["scheme_code"] == 1


def test_none_status_is_not_treated_as_dead():
    rows = [
        row(1, "Some Fund - Growth Plan - Growth Option", is_active=None),
        row(2, "Some Fund - Growth Plan - Growth Option", is_active=False),
    ]
    assert _dedupe_search_variants(rows)[0]["scheme_code"] == 1
