"""Dead-fund / AMC-supersession rule tests.

The rule decides what an advisor is OFFERED, so the failure that matters most
is hiding a fund that is actually alive. These tests pin the fail-open
behaviour as hard as they pin the hiding.

Pure-logic: the Mongo predicate is exercised by evaluating it against
in-memory documents with a tiny matcher rather than a real server, matching
this repo's no-network test convention.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import amfi_client  # noqa: E402
import fund_status  # noqa: E402

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
FRESH = (NOW.date() - timedelta(days=3)).isoformat()
STALE = (NOW.date() - timedelta(days=fund_status.STALE_AFTER_DAYS + 30)).isoformat()
JUST_INSIDE = (NOW.date() - timedelta(days=fund_status.STALE_AFTER_DAYS - 1)).isoformat()


# ─── Python-side predicate ───────────────────────────────────────────────────

def test_live_fund_is_active():
    assert fund_status.is_active({"own_latest_nav_date": FRESH}, NOW)


def test_fund_with_no_nav_for_over_the_threshold_is_dead():
    doc = {"own_latest_nav_date": STALE}
    assert not fund_status.is_active(doc, NOW)
    assert "no NAV since" in fund_status.inactive_reason(doc, NOW)


def test_fund_just_inside_the_threshold_is_still_active():
    assert fund_status.is_active({"own_latest_nav_date": JUST_INSIDE}, NOW)


def test_merged_fund_is_hidden_even_though_its_stitched_history_looks_fresh():
    """The exact AMC-change shape: the predecessor code stopped publishing
    years ago, but because its history was stitched forward into the live
    successor, `latest_nav_date` is today's. Reading that field alone would
    keep the pre-merger fund listed next to the fund it merged into."""
    doc = {"own_latest_nav_date": STALE, "latest_nav_date": FRESH, "merged_with": 140225}
    assert not fund_status.is_active(doc, NOW)
    assert fund_status.inactive_reason(doc, NOW) == "Merged into scheme 140225"


def test_superseded_flag_hides_a_fund():
    doc = {"own_latest_nav_date": STALE, "superseded_by": 150209}
    assert not fund_status.is_active(doc, NOW)


def test_document_without_own_date_falls_back_to_stitched_date():
    # Pre-backfill document, never forward-stitched: latest_nav_date IS its
    # own last NAV, so the rule still works before any re-crawl.
    assert not fund_status.is_active({"latest_nav_date": STALE}, NOW)
    assert fund_status.is_active({"latest_nav_date": FRESH}, NOW)


def test_document_with_no_dates_at_all_fails_open():
    """No evidence is not evidence of death — hiding a live fund is the worse
    failure, so an unindexed/incomplete document stays visible."""
    assert fund_status.is_active({"scheme_code": 1}, NOW)
    assert fund_status.inactive_reason({"scheme_code": 1}, NOW) is None


def test_filter_active():
    docs = [
        {"scheme_code": 1, "own_latest_nav_date": FRESH},
        {"scheme_code": 2, "own_latest_nav_date": STALE},
        {"scheme_code": 3, "own_latest_nav_date": FRESH, "superseded_by": 9},
    ]
    assert [d["scheme_code"] for d in fund_status.filter_active(docs, NOW)] == [1]


# ─── Mongo-side predicate — must agree with the Python one ───────────────────

def _matches(query, doc):
    """Minimal evaluator for the operators active_query() actually emits."""
    for key, cond in query.items():
        if key == "$and":
            if not all(_matches(c, doc) for c in cond):
                return False
        elif key == "$or":
            if not any(_matches(c, doc) for c in cond):
                return False
        else:
            value = doc.get(key, _MISSING)
            if isinstance(cond, dict):
                for op, operand in cond.items():
                    if op == "$exists":
                        if (value is not _MISSING) != operand:
                            return False
                    elif op == "$gte":
                        if value is _MISSING or not (value >= operand):
                            return False
                    elif op == "$lt":
                        if value is _MISSING or not (value < operand):
                            return False
                    elif op == "$in":
                        if value is _MISSING or value not in operand:
                            return False
                    elif op == "$nin":
                        if value is not _MISSING and value in operand:
                            return False
                    else:
                        raise AssertionError(f"unhandled operator {op}")
            elif value != cond:
                return False
    return True


class _Missing:
    pass


_MISSING = _Missing()


@pytest.mark.parametrize("doc", [
    {"own_latest_nav_date": FRESH},
    {"own_latest_nav_date": STALE},
    {"own_latest_nav_date": STALE, "latest_nav_date": FRESH, "merged_with": 1},
    {"own_latest_nav_date": FRESH, "superseded_by": 2},
    {"latest_nav_date": STALE},
    {"latest_nav_date": FRESH},
    {"scheme_code": 1},
    {"own_latest_nav_date": FRESH, "merged_with": None, "superseded_by": None},
])
def test_mongo_predicate_agrees_with_python_predicate(doc):
    """The two must never diverge: one hides funds from listings, the other
    labels them in search, and a disagreement shows up as a fund that is
    hidden in one place and offered in another."""
    assert _matches(fund_status.active_query(NOW), doc) == fund_status.is_active(doc, NOW)


def test_merge_query_keeps_the_callers_own_conditions():
    base = {"history_years": {"$gte": 12}}
    merged = fund_status.merge_query(base, NOW)
    assert merged["history_years"] == {"$gte": 12}
    assert _matches(merged, {"history_years": 15, "own_latest_nav_date": FRESH})
    assert not _matches(merged, {"history_years": 5, "own_latest_nav_date": FRESH})
    assert not _matches(merged, {"history_years": 15, "own_latest_nav_date": STALE})


def test_merge_query_does_not_clobber_an_existing_and():
    base = {"$and": [{"a": 1}]}
    merged = fund_status.merge_query(base, NOW)
    assert {"a": 1} in merged["$and"]
    assert len(merged["$and"]) == 4   # the caller's one + active_query's three


# ─── Generic AMC-change mapping ──────────────────────────────────────────────

def test_successor_pairs_reads_every_source(monkeypatch):
    monkeypatch.setattr(amfi_client, "MERGER_MAP", {1: 2})
    monkeypatch.setattr(amfi_client, "_DISCOVERED_SUCCESSORS", {3: 4})
    monkeypatch.setattr(amfi_client, "_ISIN_VERIFIED_PAIRS", {(5, 6)})
    assert fund_status.successor_pairs() == [(1, 2), (3, 4), (5, 6)]


def test_resolve_current_code_follows_a_chain(monkeypatch):
    """A fund transferred twice (AMC sold, then merged) must resolve to the
    ONE surviving code — the generic version of "handle this known case"."""
    monkeypatch.setattr(amfi_client, "MERGER_MAP", {1: 2, 2: 3})
    monkeypatch.setattr(amfi_client, "_DISCOVERED_SUCCESSORS", {})
    assert fund_status.resolve_current_code(1) == 3
    assert fund_status.resolve_current_code(3) == 3


def test_resolve_current_code_survives_a_cycle(monkeypatch):
    monkeypatch.setattr(amfi_client, "MERGER_MAP", {1: 2, 2: 1})
    monkeypatch.setattr(amfi_client, "_DISCOVERED_SUCCESSORS", {})
    assert fund_status.resolve_current_code(1) in (1, 2)


def test_resolve_current_code_leaves_unknown_codes_alone(monkeypatch):
    monkeypatch.setattr(amfi_client, "MERGER_MAP", {})
    monkeypatch.setattr(amfi_client, "_DISCOVERED_SUCCESSORS", {})
    assert fund_status.resolve_current_code(999) == 999
