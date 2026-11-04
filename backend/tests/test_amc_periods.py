"""AMC periods: every management break per fund, selectable — the pure
helpers in continuity_floor plus GET /funds/{code}/amc-periods, the universe
annotation, and the analyze `summary` trimming."""
import asyncio
import os
import sys

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # noqa: E402

import continuity_floor as cf  # noqa: E402
import fund_lineage  # noqa: E402
import server  # noqa: E402
from _fake_db import FakeDB  # noqa: E402

CURATED = fund_lineage.CURATED_AMC_CHANGES
CANON = fund_lineage.canonical_house


# ── curated fallback ─────────────────────────────────────────────────────────

def test_curated_actions_apply_to_a_fund_that_existed_before_the_change():
    acts = cf.curated_amc_actions("Nippon India Mutual Fund", "2005-03-01", CURATED, CANON)
    assert [a["effective_date"] for a in acts] == ["2019-09-28"]
    assert acts[0]["from"] == "Reliance Mutual Fund"


def test_curated_actions_skip_a_fund_launched_after_the_change():
    assert cf.curated_amc_actions("Nippon India Mutual Fund", "2020-01-15", CURATED, CANON) == []


def test_curated_actions_need_house_and_start_date():
    assert cf.curated_amc_actions(None, "2005-01-01", CURATED, CANON) == []
    assert cf.curated_amc_actions("Nippon India Mutual Fund", None, CURATED, CANON) == []


def test_lineage_or_curated_prefers_a_real_lineage_doc():
    doc = {"corporate_actions": [{"type": "AMC_CHANGE", "effective_date": "2011-01-01"}]}
    assert cf.lineage_or_curated(doc, "Nippon India Mutual Fund", "2005-01-01") is doc


def test_lineage_or_curated_builds_a_stand_in_without_a_build():
    doc = cf.lineage_or_curated(None, "Bandhan Mutual Fund", "2008-06-01")
    assert doc and doc["corporate_actions"][0]["effective_date"] == "2023-03-13"
    # The stand-in feeds the existing floor logic unchanged.
    floor, reason = cf.compute_continuity_floor(
        predecessor_used=[], own_code_history_start=None, lineage_doc=doc,
    )
    assert floor.isoformat() == "2023-03-13"
    assert "Bandhan" in reason


def test_lineage_or_curated_none_for_an_unchanged_house():
    assert cf.lineage_or_curated(None, "Parag Parikh Mutual Fund", "2013-05-01") is None


# ── all_breaks ───────────────────────────────────────────────────────────────

def test_all_breaks_lists_every_change_newest_first():
    lineage = {"corporate_actions": [
        {"type": "AMC_CHANGE", "effective_date": "2010-04-01", "from": "A", "to": "B"},
        {"type": "NAME_CHANGE", "effective_date": "2012-01-01"},
        {"type": "AMC_CHANGE", "effective_date": "2016-09-20", "from": "B", "to": "C"},
        {"type": "AMC_CHANGE", "effective_date": "2018-02-28", "from": "C", "to": "D"},
    ]}
    breaks = cf.all_breaks(lineage_doc=lineage)
    assert [b["date"] for b in breaks] == ["2018-02-28", "2016-09-20", "2010-04-01"]
    assert all(b["source"] == "lineage" for b in breaks)
    assert breaks[0]["to"] == "D"


def test_all_breaks_merges_signals_and_dedupes_dates():
    breaks = cf.all_breaks(
        curated_actions=[{"effective_date": "2019-09-28", "from": "Reliance", "to": "Nippon"}],
        manual_event={"break_date": "2021-07-01", "reason": "Manager change"},
        merger_boundary="2019-09-28",
    )
    assert [b["date"] for b in breaks] == ["2021-07-01", "2019-09-28"]
    # The descriptive (from/to) entry wins over the bare merger one on the same date.
    assert breaks[1]["from"] == "Reliance"


def test_all_breaks_ignores_curated_when_lineage_has_amc_actions():
    lineage = {"corporate_actions": [{"type": "AMC_CHANGE", "effective_date": "2016-01-01"}]}
    breaks = cf.all_breaks(
        lineage_doc=lineage,
        curated_actions=[{"effective_date": "2019-09-28", "from": "X", "to": "Y"}],
    )
    assert [b["date"] for b in breaks] == ["2016-01-01"]


def test_all_breaks_skips_bad_dates():
    assert cf.all_breaks(lineage_doc={"corporate_actions": [
        {"type": "AMC_CHANGE", "effective_date": "live"},
        {"type": "AMC_CHANGE", "effective_date": "not-a-date"},
        {"type": "AMC_CHANGE"},
    ]}) == []


# ── endpoint + universe annotation ──────────────────────────────────────────

@pytest.fixture
def amc_db(monkeypatch):
    db = FakeDB(
        fund_nav_meta=[
            {"scheme_code": 101, "scheme_name": "Nippon India Growth Fund - Growth",
             "fund_house": "Nippon India Mutual Fund", "earliest_nav_date": "1995-10-08",
             "own_earliest_nav_date": "1995-10-08", "predecessor_used": [],
             "continuity_reset_date": "2019-09-28"},
            {"scheme_code": 202, "scheme_name": "Steady Fund - Growth",
             "fund_house": "Parag Parikh Mutual Fund", "earliest_nav_date": "2013-05-28"},
            {"scheme_code": 303, "scheme_name": "Thrice Changed Fund - Growth",
             "fund_house": "quant Mutual Fund", "earliest_nav_date": "2001-01-01",
             "own_earliest_nav_date": "2001-01-01"},
        ],
        fund_lineage=[
            {"scheme_code": 303, "current_amc": "quant Mutual Fund", "corporate_actions": [
                {"type": "AMC_CHANGE", "effective_date": "2010-01-04", "from": "A", "to": "B"},
                {"type": "AMC_CHANGE", "effective_date": "2016-06-01", "from": "B", "to": "Escorts"},
                {"type": "AMC_CHANGE", "effective_date": "2018-02-28", "from": "Escorts", "to": "quant"},
            ]},
        ],
        fund_continuity_events=[],
    )
    monkeypatch.setattr(server, "db", db)
    return db


def test_amc_periods_endpoint_uses_curated_table_without_lineage(amc_db):
    r = asyncio.run(server.fund_amc_periods(101))
    assert [b["date"] for b in r["breaks"]] == ["2019-09-28"]
    assert r["breaks"][0]["source"] == "curated"
    assert r["current_amc"] == "Nippon India Mutual Fund"
    assert r["latest_break"] == "2019-09-28"
    assert r["caveat"]


def test_amc_periods_endpoint_multiple_changes(amc_db):
    r = asyncio.run(server.fund_amc_periods(303))
    assert [b["date"] for b in r["breaks"]] == ["2018-02-28", "2016-06-01", "2010-01-04"]
    assert r["current_amc"] == "quant Mutual Fund"


def test_amc_periods_endpoint_no_change(amc_db):
    r = asyncio.run(server.fund_amc_periods(202))
    assert r["breaks"] == []


def test_amc_periods_endpoint_unknown_fund(amc_db):
    with pytest.raises(server.HTTPException) as e:
        asyncio.run(server.fund_amc_periods(999))
    assert e.value.status_code == 404


def test_attach_amc_breaks_annotates_only_funds_with_breaks(amc_db):
    funds = [{"scheme_code": 101}, {"scheme_code": 202}, {"scheme_code": 303}]
    asyncio.run(server._attach_amc_breaks(funds))
    assert [b["date"] for b in funds[0]["amc_breaks"]] == ["2019-09-28"]
    assert funds[1]["amc_breaks"] is None
    assert len(funds[2]["amc_breaks"]) == 3


def test_attach_amc_breaks_never_breaks_the_universe(monkeypatch):
    class Broken:
        def __getattr__(self, _):
            raise RuntimeError("db down")

        def __getitem__(self, _):
            raise RuntimeError("db down")

    monkeypatch.setattr(server, "db", Broken())
    funds = [{"scheme_code": 1}]
    asyncio.run(server._attach_amc_breaks(funds))
    assert "amc_breaks" not in funds[0]


# ── analyze summary trimming ─────────────────────────────────────────────────

def test_without_windows_copies_and_drops_windows_only():
    payload = {"scheme_code": 1, "horizons": [
        {"horizon": 5, "fqs": 80.0, "windows": [{"wm": 1.2}]},
        {"horizon": 10, "fqs": 75.0, "windows": []},
    ]}
    trimmed = server._without_windows(payload)
    assert [h.get("windows") for h in trimmed["horizons"]] == [None, None]
    assert trimmed["horizons"][0]["fqs"] == 80.0
    # The original (possibly a cached document) is untouched.
    assert payload["horizons"][0]["windows"] == [{"wm": 1.2}]
