"""Unit tests for lineage derivation, indexing and resolution.

All pure — no DB, no network. Fixtures model the real corporate actions the
lineage table has to survive: the Reliance→Nippon and IDFC→Bandhan house
renames, a SEBI-2018 recategorisation, an ISIN change, a merger, and a
wound-up scheme with no successor.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amfi_history import SchemeObservation, is_valid_isin  # noqa: E402
import lineage_store as ls  # noqa: E402


def obs(code, name, isin="", amc="", stype=""):
    return SchemeObservation(
        scheme_code=code, scheme_name=name, isin_growth=isin,
        isin_reinvest="", nav_date=None, amc_name=amc, scheme_type=stype,
    )


# ── Name normalisation ───────────────────────────────────────────────────────

def test_normalise_name_collapses_punctuation_and_ampersand():
    a = ls.normalise_name("Aditya Birla Sun Life Banking & PSU Debt Fund  - DIRECT - IDCW")
    b = ls.normalise_name("Aditya Birla Sun Life Banking and PSU Debt Fund-Direct-IDCW")
    assert a == b


def test_normalise_keeps_plan_words_so_growth_and_idcw_stay_distinct():
    assert ls.normalise_name("HDFC Top 100 Growth") != ls.normalise_name("HDFC Top 100 IDCW")


def test_name_stem_drops_plan_words():
    assert ls.name_stem("HDFC Top 100 Fund - Regular Plan - Growth Option") == \
           ls.name_stem("HDFC Top 100 - Direct - IDCW")


# ── ISIN validation ──────────────────────────────────────────────────────────

def test_isin_validation_rejects_the_real_amfi_typos():
    assert is_valid_isin("INF209KA12Z1")
    # Both observed in AMFI's own Apr-2016 report.
    assert not is_valid_isin("NF336L01FZ2")    # dropped leading I
    assert not is_valid_isin("IINF277K01Y4")   # doubled I
    assert not is_valid_isin("")


def test_malformed_isin_goes_to_review_and_is_not_indexed():
    recs = ls.derive_lineage({"2016-04-01": [obs(1, "Some Fund", "NF336L01FZ2")]})
    assert recs[1].isin_history == []
    assert any(r["reason"] == "MALFORMED_ISIN" for r in recs[1].review)
    keys = {r["key"] for r in ls.build_identifier_index(recs) if r["kind"] == "isin"}
    assert "NF336L01FZ2" not in keys


# ── Name changes ─────────────────────────────────────────────────────────────

def test_house_rename_reliance_to_nippon_is_one_name_change():
    recs = ls.derive_lineage({
        "2018-01-01": [obs(101, "Reliance Small Cap Fund - Growth", "INF204K01XX1")],
        "2018-04-01": [obs(101, "Reliance Small Cap Fund - Growth", "INF204K01XX1")],
        "2019-10-01": [obs(101, "Nippon India Small Cap Fund - Growth", "INF204K01XX1")],
        "2020-01-01": [obs(101, "Nippon India Small Cap Fund - Growth", "INF204K01XX1")],
    })
    rec = recs[101]
    assert [i.value for i in rec.name_history] == [
        "Reliance Small Cap Fund - Growth", "Nippon India Small Cap Fund - Growth"]
    changes = [a for a in rec.corporate_actions if a["type"] == ls.Action.NAME_CHANGE]
    assert len(changes) == 1
    assert changes[0]["effective_date"] == "2019-10-01"
    assert changes[0]["source"] == ls.Source.AMFI_SNAPSHOT
    assert changes[0]["confidence"] == 1.0
    # The first interval must remember when the OLD name was first seen.
    assert rec.name_history[0].first_seen == "2018-01-01"
    assert rec.name_history[0].last_seen == "2018-04-01"


def test_unchanged_name_across_many_snapshots_yields_no_action():
    snaps = {f"20{y:02d}-01-01": [obs(7, "Steady Fund - Growth", "INF001A01AA1")]
             for y in range(10, 26)}
    rec = ls.derive_lineage(snaps)[7]
    assert len(rec.name_history) == 1
    assert rec.corporate_actions == []
    assert rec.name_history[0].first_seen == "2010-01-01"
    assert rec.name_history[0].last_seen == "2025-01-01"


def test_backfilled_source_cannot_create_phantom_renames():
    """Regression: AMFI's historical NAV report back-fills TODAY's name onto
    historical rows. Interleaving it with true point-in-time archive captures
    produced Nippon(2014) -> Reliance(2016) -> Nippon(2018) for a fund renamed
    exactly once. Existence-only rows must never touch name history."""
    archived = {   # point-in-time: real names
        "2016-10-25": [obs(118778, "Reliance Small Cap Fund - Growth")],
        "2019-11-17": [obs(118778, "Nippon India Small Cap Fund - Growth")],
    }
    backfilled = {  # AMFI historical report: today's name at every date
        "2014-01-01": [obs(118778, "Nippon India Small Cap Fund - Growth")],
        "2017-01-01": [obs(118778, "Nippon India Small Cap Fund - Growth")],
        "2018-01-01": [obs(118778, "Nippon India Small Cap Fund - Growth")],
    }
    rec = ls.derive_lineage(archived, existence_only=backfilled)[118778]

    names = [i.value for i in rec.name_history]
    assert names == ["Reliance Small Cap Fund - Growth",
                     "Nippon India Small Cap Fund - Growth"], names
    assert len(names) == len(set(names)), "history revisits a name it already left"
    assert len([a for a in rec.corporate_actions
                if a["type"] == ls.Action.NAME_CHANGE]) == 1
    # Existence data still extends the observed window back before the archive.
    assert rec.observed_from == "2014-01-01"


def test_existence_only_source_alone_yields_no_name_actions():
    rec = ls.derive_lineage({}, existence_only={
        "2010-01-01": [obs(9, "Some Fund")],
        "2020-01-01": [obs(9, "Some Fund Renamed")],
    })[9]
    assert rec.name_history == []
    assert rec.corporate_actions == []
    assert rec.observed_from == "2010-01-01" and rec.observed_to == "2020-01-01"


def test_sebi_window_category_change_is_recategorisation_not_plain_rename():
    recs = ls.derive_lineage({
        "2017-07-01": [obs(202, "ICICI Prudential Focused Bluechip Equity Fund")],
        "2018-04-01": [obs(202, "ICICI Prudential Bluechip Fund - Large Cap")],
    })
    kinds = [a["type"] for a in recs[202].corporate_actions]
    assert ls.Action.RECATEGORISATION in kinds
    assert ls.Action.NAME_CHANGE not in kinds


def test_rename_outside_sebi_window_is_a_plain_name_change():
    recs = ls.derive_lineage({
        "2021-01-01": [obs(203, "X Focused Fund")],
        "2022-01-01": [obs(203, "X Large Cap Fund")],
    })
    kinds = [a["type"] for a in recs[203].corporate_actions]
    assert kinds == [ls.Action.NAME_CHANGE]


# ── ISIN + AMC changes ───────────────────────────────────────────────────────

def test_isin_change_recorded():
    recs = ls.derive_lineage({
        "2015-01-01": [obs(303, "Some Fund", "INF111A01AA1")],
        "2016-01-01": [obs(303, "Some Fund", "INF111A01BB2")],
    })
    acts = [a for a in recs[303].corporate_actions if a["type"] == ls.Action.ISIN_CHANGE]
    assert len(acts) == 1
    assert acts[0]["from"] == "INF111A01AA1" and acts[0]["to"] == "INF111A01BB2"
    assert [i.value for i in recs[303].isin_history] == ["INF111A01AA1", "INF111A01BB2"]


def test_amc_change_from_live_file_idfc_to_bandhan():
    snaps = {"2022-01-01": [obs(404, "IDFC Large Cap Fund", "INF194K01Y15", amc="IDFC Mutual Fund")]}
    live = [obs(404, "Bandhan Large Cap Fund", "INF194K01Y15",
                amc="Bandhan Mutual Fund", stype="Open Ended Schemes(Equity Scheme - Large Cap Fund)")]
    rec = ls.derive_lineage(snaps, live=live)[404]
    assert rec.active is True
    assert rec.current_amc == "Bandhan Mutual Fund"
    assert [a["type"] for a in rec.corporate_actions if a["type"] == ls.Action.AMC_CHANGE]
    assert rec.scheme_type.startswith("Open Ended")


# ── Wound-up + mergers ───────────────────────────────────────────────────────

def test_code_absent_from_live_is_marked_wound_up():
    snaps = {"2014-01-01": [obs(505, "Morgan Stanley A.C.E. Fund")],
             "2014-04-01": [obs(505, "Morgan Stanley A.C.E. Fund")]}
    live = [obs(606, "HDFC Small Cap Fund", "INF179K01XX1", amc="HDFC Mutual Fund")]
    recs = ls.derive_lineage(snaps, live=live)
    assert recs[505].active is False
    assert any(a["type"] == ls.Action.WOUND_UP for a in recs[505].corporate_actions)
    assert recs[606].active is True


def test_apply_successors_links_both_directions():
    recs = ls.derive_lineage({
        "2014-01-01": [obs(505, "Morgan Stanley A.C.E. Fund")],
        "2015-01-01": [obs(606, "HDFC Small Cap Fund", "INF179K01XX1")],
    })
    n = ls.apply_successors(recs, [(505, 606)], ls.Source.CURATED)
    assert n == 1
    assert recs[505].successor_code == 606
    assert 505 in recs[606].predecessor_codes
    merger = [a for a in recs[505].corporate_actions if a["type"] == ls.Action.MERGER][0]
    assert merger["successor_code"] == 606
    assert merger["source"] == ls.Source.CURATED
    assert merger["confidence"] == ls.SOURCE_CONFIDENCE[ls.Source.CURATED]


def test_successor_self_link_is_ignored():
    recs = ls.derive_lineage({"2020-01-01": [obs(1, "A Fund")]})
    assert ls.apply_successors(recs, [(1, 1)], ls.Source.CURATED) == 0


# ── Resolution: the actual user-facing requirement ───────────────────────────

def _built():
    recs = ls.derive_lineage(
        {
            "2018-01-01": [obs(101, "Reliance Small Cap Fund - Growth", "INF204K01AA1"),
                           obs(505, "Morgan Stanley A.C.E. Fund", "INF109K01ZZ9")],
            "2020-01-01": [obs(101, "Nippon India Small Cap Fund - Growth", "INF204K01BB2")],
        },
        live=[obs(101, "Nippon India Small Cap Fund - Growth", "INF204K01BB2",
                  amc="Nippon India Mutual Fund")],
    )
    ls.apply_successors(recs, [(505, 101)], ls.Source.CORRELATION)
    return recs, ls.build_identifier_index(recs)


def test_search_by_old_name_resolves_to_current_fund():
    _, idx = _built()
    assert ls.resolve_local(idx, "Reliance Small Cap Fund - Growth") == [101]


def test_search_by_new_name_resolves():
    _, idx = _built()
    assert ls.resolve_local(idx, "Nippon India Small Cap Fund - Growth") == [101]


def test_search_by_old_isin_resolves():
    _, idx = _built()
    assert ls.resolve_local(idx, "INF204K01AA1") == [101]


def test_search_by_current_isin_resolves():
    _, idx = _built()
    assert ls.resolve_local(idx, "inf204k01bb2") == [101]


def test_search_by_predecessor_code_resolves_to_survivor():
    _, idx = _built()
    assert 101 in ls.resolve_local(idx, "505")


def test_search_by_current_code_resolves():
    _, idx = _built()
    assert ls.resolve_local(idx, "101") == [101]


def test_search_ignores_plan_option_noise():
    _, idx = _built()
    assert 101 in ls.resolve_local(idx, "Reliance Small Cap")


def test_unknown_query_returns_empty():
    _, idx = _built()
    assert ls.resolve_local(idx, "Totally Nonexistent Scheme") == []
    assert ls.resolve_local(idx, "") == []


# ── Summary counters (the deliverable numbers) ───────────────────────────────

def test_summarise_counts_actions_by_type():
    recs, _ = _built()
    s = ls.summarise(recs)
    assert s["funds_audited"] == 2
    assert s["active_funds"] == 1
    assert s["inactive_funds"] == 1
    assert s["by_action_type"][ls.Action.NAME_CHANGE] == 1
    assert s["by_action_type"][ls.Action.ISIN_CHANGE] == 1
    assert s["by_action_type"][ls.Action.MERGER] == 1
    assert s["lineage_mappings"] == 1
    assert s["coverage_start"] == "2006-01-01"


# ── Name-change classification (noise vs real corporate action) ──────────────

def test_case_only_change_is_cosmetic():
    assert ls.classify_name_change(
        "Nippon India Fixed Horizon Fund XLI - Series 12",
        "NIPPON INDIA FIXED HORIZON FUND - XLI - SERIES 12",
        "2022-03-16") == ls.Action.NAME_COSMETIC


def test_punctuation_only_change_is_cosmetic():
    assert ls.classify_name_change(
        "Sundaram Money Market Fund - Direct Plan",
        "Sundaram Money Market Fund Direct Plan",
        "2022-03-16") == ls.Action.NAME_COSMETIC


def test_dividend_to_idcw_is_terminology_not_a_rename():
    assert ls.classify_name_change(
        "L&T Equity Savings Fund - Regular Plan - Quarterly Dividend",
        "L&T Equity Savings Fund - Regular Plan - Quarterly IDCW",
        "2021-11-01") == ls.Action.NAME_TERMINOLOGY


def test_genuine_scheme_repurposing_is_a_real_name_change():
    assert ls.classify_name_change(
        "DHFL Pramerica Inflation Indexed Bond Fund - Direct Plan - Growth",
        "DHFL Pramerica Strategic Debt Fund - Direct Plan - Growth",
        "2019-11-17") == ls.Action.NAME_CHANGE


def test_house_rename_is_a_real_name_change():
    assert ls.classify_name_change(
        "Reliance Small Cap Fund - Growth",
        "Nippon India Small Cap Fund - Growth",
        "2019-11-17") == ls.Action.NAME_CHANGE


def test_classification_flows_through_derivation():
    recs = ls.derive_lineage({
        "2021-01-01": [obs(50, "X Fund - Quarterly Dividend")],
        "2022-01-01": [obs(50, "X FUND - QUARTERLY IDCW")],
    })
    kinds = [a["type"] for a in recs[50].corporate_actions]
    assert kinds == [ls.Action.NAME_TERMINOLOGY]


# ── Explainability ───────────────────────────────────────────────────────────

def _persisted(recs, code):
    """Shape a derived record the way it is stored, for explain_record()."""
    return recs[code].to_doc()


def test_explain_same_code_rename_is_certain_not_scored():
    """A rename AMFI recorded against one code is an observation, not an
    inference — it must not be scored down by the merger evidence model."""
    recs = ls.derive_lineage(
        {"2017-10-25": [obs(101, "Reliance Small Cap Fund - Growth", "INF204K01AA1")],
         "2019-11-17": [obs(101, "Nippon India Small Cap Fund - Growth", "INF204K01AA1")]},
        live=[obs(101, "Nippon India Small Cap Fund - Growth", "INF204K01AA1",
                  amc="Nippon India Mutual Fund")])
    e = ls.explain_record("Reliance Small Cap Fund - Growth", _persisted(recs, 101))
    assert e["resolution_type"] == "same_code_identity"
    assert e["confidence"] == 1.0
    assert any("previous name" in r for r in e["reasons"])
    assert e["resolved_to"]["scheme_name"].startswith("Nippon India")


def test_explain_cross_code_succession_uses_weighted_evidence():
    recs = ls.derive_lineage(
        {"2014-01-01": [obs(505, "Morgan Stanley A.C.E. Fund", "INF109K01ZZ9")],
         "2016-01-01": [obs(606, "HDFC Small Cap Fund", "INF179K01XX1")]},
        live=[obs(606, "HDFC Small Cap Fund", "INF179K01XX1", amc="HDFC Mutual Fund")])
    ls.apply_successors(recs, [(505, 606)], ls.Source.CURATED)
    e = ls.explain_record("505", _persisted(recs, 606))
    assert e["resolution_type"] == "cross_code_succession"
    assert 0 < (e["confidence"] or 0) <= 1.0
    assert e["confidence_basis"].startswith("weighted evidence")


def test_evidence_weights_sum_to_one():
    assert abs(sum(ls.EVIDENCE_WEIGHTS.values()) - 1.0) < 1e-9


def test_evidence_score_is_additive_over_independent_signals():
    assert ls.evidence_score({"isin_continuity": True}) == 0.35
    assert ls.evidence_score({"isin_continuity": True, "nav_correlation": True}) == 0.60
    assert ls.evidence_score({k: True for k in ls.EVIDENCE_WEIGHTS}) == 1.0
    assert ls.evidence_score({}) == 0.0


def test_explain_reports_isin_match_and_sources():
    recs = ls.derive_lineage(
        {"2015-01-01": [obs(303, "Some Fund", "INF111A01AA1")],
         "2016-01-01": [obs(303, "Some Fund", "INF111A01BB2")]},
        live=[obs(303, "Some Fund", "INF111A01BB2", amc="Some AMC")])
    e = ls.explain_record("INF111A01AA1", _persisted(recs, 303))
    assert any("previous ISIN" in r for r in e["reasons"])
    assert e["signals"].get("isin_continuity") is True
    assert e["identity_history"]["isins"] == ["INF111A01AA1", "INF111A01BB2"]


def test_explain_always_states_coverage_and_unavailable_signals():
    recs = ls.derive_lineage({"2020-01-01": [obs(1, "A Fund", "INF001A01AA1")]})
    e = ls.explain_record("A Fund", _persisted(recs, 1))
    assert "2016" in e["coverage"]["caveat"]
    assert set(e["unavailable_signals"]) == {
        "benchmark_continuity", "risk_category_continuity", "aum_continuity"}
