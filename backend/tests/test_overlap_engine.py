"""overlap_engine.py — tests for the weight-based portfolio overlap math.

Mirrors the fixture style of the other pure-function suites here (no DB, no
network): build two holdings lists, assert on the returned payload.
"""
import os
import sys

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from overlap_engine import (  # noqa: E402
    compute_overlap,
    dedupe_holdings,
    holding_key,
    normalize_isin,
    normalize_name,
)


def h(name, weight, isin=None):
    return {"name": name, "weight_pct": weight, "isin": isin}


# ── Identity / normalisation ─────────────────────────────────────────────────

def test_valid_isin_is_accepted_and_uppercased():
    assert normalize_isin(" ine040a01034 ") == "INE040A01034"


def test_junk_isin_is_rejected_so_it_cannot_become_a_match_key():
    for junk in ("-", "NA", "", None, "INE040A0103", "1234567890AB"):
        assert normalize_isin(junk) is None


def test_name_normalisation_collapses_corporate_suffix_spellings():
    assert normalize_name("HDFC Bank Ltd.") == normalize_name("HDFC Bank Limited")
    assert normalize_name("Reliance Industries Ltd") == normalize_name("Reliance Ltd.")


def test_isin_and_name_keys_are_namespaced_apart():
    assert holding_key(h("X", 1, "INE040A01034")).startswith("isin:")
    assert holding_key(h("X", 1)).startswith("name:")
    assert holding_key(h("", 1)) is None


# ── The headline number ──────────────────────────────────────────────────────

def test_identical_books_are_100_pct():
    a = [h("Alpha", 60, "INE001A01036"), h("Beta", 40, "INE002A01018")]
    r = compute_overlap(a, list(a))
    assert r["overlap_pct"] == 100.0
    assert r["common_count"] == 2


def test_disjoint_books_are_zero():
    a = [h("Alpha", 100, "INE001A01036")]
    b = [h("Beta", 100, "INE002A01018")]
    r = compute_overlap(a, b)
    assert r["overlap_pct"] == 0.0
    assert r["common_count"] == 0
    assert r["only_in_a"] == 1 and r["only_in_b"] == 1


def test_overlap_is_the_sum_of_min_weights():
    # Alpha 60/30 → 30, Beta 40/50 → 40. Gamma is only in B.
    a = [h("Alpha", 60, "INE001A01036"), h("Beta", 40, "INE002A01018")]
    b = [h("Alpha", 30, "INE001A01036"), h("Beta", 50, "INE002A01018"),
         h("Gamma", 20, "INE003A01024")]
    r = compute_overlap(a, b)
    assert r["overlap_pct"] == 70.0
    assert r["common_count"] == 2


def test_overlap_is_symmetric():
    a = [h("Alpha", 70, "INE001A01036"), h("Beta", 30, "INE002A01018")]
    b = [h("Alpha", 25, "INE001A01036"), h("Gamma", 75, "INE003A01024")]
    assert compute_overlap(a, b)["overlap_pct"] == compute_overlap(b, a)["overlap_pct"]


def test_big_shared_position_outweighs_many_tiny_ones():
    """The whole point of weighting: one 40% shared name must dominate ten
    shared 0.5% names, which a count-based measure gets exactly backwards."""
    heavy_a = [h("Big", 40, "INE001A01036"), h("FillerA", 60, "INE0FILLA018")]
    heavy_b = [h("Big", 40, "INE001A01036"), h("FillerB", 60, "INE0FILLB016")]

    tiny_a = [h(f"S{i}", 0.5, f"INE{i:03d}A01036") for i in range(10)] + \
             [h("RestA", 95, "INE0RESTA015")]
    tiny_b = [h(f"S{i}", 0.5, f"INE{i:03d}A01036") for i in range(10)] + \
             [h("RestB", 95, "INE0RESTB013")]

    heavy = compute_overlap(heavy_a, heavy_b)
    tiny  = compute_overlap(tiny_a, tiny_b)

    assert heavy["common_count"] < tiny["common_count"]      # fewer shared names
    assert heavy["overlap_pct"] > tiny["overlap_pct"]        # but far more overlap
    assert heavy["overlap_pct"] == 40.0
    assert tiny["overlap_pct"] == 5.0


# ── Equity-sleeve rebasing ───────────────────────────────────────────────────

def test_cash_heavy_fund_is_not_punished_for_holding_cash():
    """A holds the same equity book as B but B's disclosure only totals 70%
    of net assets (30% cash). Rebased to the equity sleeve they are
    identical; on raw as-disclosed weights they would score only 70."""
    a = [h("Alpha", 60, "INE001A01036"), h("Beta", 40, "INE002A01018")]
    b = [h("Alpha", 42, "INE001A01036"), h("Beta", 28, "INE002A01018")]
    r = compute_overlap(a, b)
    assert r["overlap_pct"] == 100.0
    assert r["overlap_pct_raw"] == 70.0
    assert r["equity_sleeve_a"] == 100.0
    assert r["equity_sleeve_b"] == 70.0


def test_zero_weight_book_does_not_divide_by_zero():
    a = [h("Alpha", 0, "INE001A01036")]
    b = [h("Alpha", 0, "INE001A01036")]
    r = compute_overlap(a, b)
    assert r["overlap_pct"] == 0.0
    assert r["equity_sleeve_a"] == 0.0
    assert r["common_count"] == 1          # still a shared holding, just unweighted


# ── Messy real-world input ───────────────────────────────────────────────────

def test_same_stock_split_across_rows_is_merged_before_comparing():
    a = [h("Alpha", 30, "INE001A01036"), h("Alpha", 30, "INE001A01036")]
    b = [h("Alpha", 60, "INE001A01036")]
    r = compute_overlap(a, b)
    assert r["fund_a_holdings"] == 1
    assert r["overlap_pct"] == 100.0


def test_name_fallback_matches_when_isin_is_missing_on_one_side():
    a = [h("HDFC Bank Ltd.", 100)]
    b = [h("HDFC Bank Limited", 100)]
    r = compute_overlap(a, b)
    assert r["common_count"] == 1
    assert r["common"][0]["matched_on"] == "name"


def test_isin_wins_over_differing_names():
    a = [h("Bajaj Finance Ltd", 50, "INE296A01024"), h("Other", 50, "INE002A01018")]
    b = [h("BAJAJ FINANCE", 50, "INE296A01024"), h("Third", 50, "INE003A01024")]
    r = compute_overlap(a, b)
    assert r["common_count"] == 1
    assert r["common"][0]["matched_on"] == "isin"
    assert r["overlap_pct"] == 50.0


def test_negative_derivative_leg_does_not_inflate_other_weights():
    a = [h("Alpha", 50, "INE001A01036"), h("ShortLeg", -10, "INE0SHORT017")]
    b = [h("Alpha", 50, "INE001A01036")]
    r = compute_overlap(a, b)
    assert r["equity_sleeve_a"] == 50.0        # the -10 is dropped, not subtracted
    assert r["overlap_pct"] == 100.0


def test_unparseable_weight_still_counts_as_a_holding():
    a = [{"name": "Alpha", "weight_pct": "n/a", "isin": "INE001A01036"}]
    b = [h("Alpha", 100, "INE001A01036")]
    r = compute_overlap(a, b)
    assert r["fund_a_holdings"] == 1
    assert r["overlap_pct"] == 0.0


def test_rows_with_neither_isin_nor_name_are_dropped():
    a = [h("", 50), h("Alpha", 50, "INE001A01036")]
    assert len(dedupe_holdings(a)) == 1


# ── Payload shape the UI depends on ──────────────────────────────────────────

def test_common_rows_are_sorted_by_contribution_and_carry_both_weights():
    a = [h("Small", 5, "INE001A01036"), h("Large", 95, "INE002A01018")]
    b = [h("Small", 5, "INE001A01036"), h("Large", 95, "INE002A01018")]
    r = compute_overlap(a, b)
    names = [c["name"] for c in r["common"]]
    assert names == ["Large", "Small"]
    top = r["common"][0]
    assert top["weight_a"] == 95.0 and top["weight_b"] == 95.0
    assert set(top) >= {"name", "isin", "weight_a", "weight_b",
                        "weight_a_equity", "weight_b_equity",
                        "contribution", "matched_on"}


def test_counts_reported_for_both_funds():
    a = [h(f"A{i}", 10, f"INE{i:03d}A01036") for i in range(10)]
    b = [h(f"A{i}", 5, f"INE{i:03d}A01036") for i in range(4)] + \
        [h("Solo", 80, "INE0SOLO0011")]
    r = compute_overlap(a, b)
    assert r["fund_a_holdings"] == 10
    assert r["fund_b_holdings"] == 5
    assert r["common_count"] == 4
