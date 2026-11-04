"""/api/home/universe — dead-fund filtering and the streaming reduction.

Two things are pinned here, both regressions that would be invisible in a
unit test of either piece alone:

  1. Discontinued and merged funds do not appear in the universe an advisor
     picks from, while live funds do — including the awkward case of a
     merged predecessor whose STITCHED history looks perfectly fresh.
  2. The per-fund response still carries every field the Home screen reads,
     and no longer carries the whole fqs_by_horizon map — the endpoint now
     reduces each document to the selected horizon as it streams, and this
     is what proves the selected numbers still come out right.

The endpoint is called directly with a fake Mongo (no server, no network),
matching this repo's existing convention.
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")

import fund_status  # noqa: E402
import server  # noqa: E402

TODAY = datetime.now(timezone.utc).date()
FRESH = (TODAY - timedelta(days=2)).isoformat()
STALE = (TODAY - timedelta(days=fund_status.STALE_AFTER_DAYS + 60)).isoformat()

_MISSING = object()


def _match(query, doc):
    for key, cond in query.items():
        if key == "$and":
            if not all(_match(c, doc) for c in cond):
                return False
            continue
        if key == "$or":
            if not any(_match(c, doc) for c in cond):
                return False
            continue
        if key == "$nor":
            if any(_match(c, doc) for c in cond):
                return False
            continue
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
                elif op == "$ne":
                    if value is not _MISSING and value == operand:
                        return False
                else:
                    raise AssertionError(f"unhandled operator {op}")
        elif value != cond:
            return False
    return True


def _project(doc, projection):
    if not projection:
        return dict(doc)
    include = [k for k, v in projection.items() if v and k != "_id"]
    if not include:
        return {k: v for k, v in doc.items() if k != "_id"}
    return {k: doc[k] for k in include if k in doc}


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def batch_size(self, _n):
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def sort(self, *_a, **_kw):
        return self

    def __aiter__(self):
        async def gen():
            for d in self._docs:
                yield d
        return gen()


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def find(self, query=None, projection=None):
        return _Cursor([_project(d, projection) for d in self.docs if _match(query or {}, d)])

    async def find_one(self, query=None, projection=None):
        for d in self.docs:
            if _match(query or {}, d):
                return _project(d, projection)
        return None

    async def count_documents(self, query=None):
        return sum(1 for d in self.docs if _match(query or {}, d))


class _DB:
    def __init__(self, funds):
        self.fund_nav_meta = _Collection(funds)
        self.nav_meta_refresh_log = _Collection([
            {"_id": "nav_meta", "finished_at": "2026-08-31T00:00:00Z"},
        ])
        self.fqs_refresh_log = _Collection([
            {"_id": "fqs_refresh", "finished_at": "2026-08-31T02:00:00Z"},
        ])


def _fund(code, name, *, own_latest, latest=None, merged_with=None,
          superseded_by=None, fqs=80.0):
    doc = {
        "scheme_code": code,
        "scheme_name": name,
        "category": "Flexi Cap",
        "scheme_category": "Equity Scheme - Flexi Cap Fund",
        "history_years": 20.0,
        "headline_fqs": fqs,
        "headline_tier": "Good",
        "headline_horizon": 10,
        "last_fqs_refresh": "2026-08-31T02:00:00Z",
        "own_latest_nav_date": own_latest,
        "latest_nav_date": latest or own_latest,
        "fqs_by_horizon": {
            str(h): {
                "fqs": fqs, "tier": "Good", "blended_wm": 2.0 + h / 100,
                "median_wm": 2.2, "p10_wm": 1.8, "success_pct": 0.9,
                "n_windows": 70 + h, "cv": 0.16,
            }
            for h in range(1, 21)
        },
    }
    if merged_with is not None:
        doc["merged_with"] = merged_with
    if superseded_by is not None:
        doc["superseded_by"] = superseded_by
    return doc


@pytest.fixture
def universe(monkeypatch):
    funds = [
        _fund(1, "Live Flexi Cap Fund - Growth", own_latest=FRESH, fqs=88.0),
        # Wound up, never merged into anything: its own feed just stopped.
        _fund(2, "Discontinued Fund - Growth", own_latest=STALE, fqs=90.0),
        # The AMC-change shape: this code stopped years ago, but because its
        # history was stitched forward into the survivor, latest_nav_date is
        # today's. It must not be listed next to the fund it merged into.
        _fund(3, "Pre-Merger Fund - Growth", own_latest=STALE, latest=FRESH,
              merged_with=4, fqs=91.0),
        _fund(4, "Post-Merger Fund - Growth", own_latest=FRESH, fqs=86.0),
        # Marked by the generic supersession sync rather than by the indexer.
        _fund(5, "Transferred Fund - Growth", own_latest=STALE, superseded_by=4,
              fqs=92.0),
    ]
    monkeypatch.setattr(server, "db", _DB(funds))
    return funds


def _home(years, *, since_2010=False, separate_management=False):
    """Call the endpoint function directly with EXPLICIT toggle values.

    The route's own defaults are `Query(False, ...)` objects, which FastAPI
    substitutes at request time but which are plain truthy objects when the
    function is called directly — so omitting them here would silently turn
    both toggles ON and make every assertion below meaningless.
    """
    return asyncio.run(server.home_universe(
        years=years, since_2010=since_2010, separate_management=separate_management,
    ))


def _codes(result):
    return sorted(f["scheme_code"] for f in result["eligible_funds"])


def test_dead_and_merged_funds_are_not_offered(universe):
    result = _home(10)
    assert _codes(result) == [1, 4]


def test_a_live_fund_is_never_hidden(universe):
    result = _home(10)
    names = [f["scheme_name"] for f in result["eligible_funds"]]
    assert "Live Flexi Cap Fund - Growth" in names
    assert "Post-Merger Fund - Growth" in names


def test_selected_horizon_metrics_are_still_correct(universe):
    """The endpoint now reduces each document to one horizon as it streams;
    this is what proves the reduction reads the SAME horizon the request
    asked for."""
    result = _home(7)
    fund = next(f for f in result["eligible_funds"] if f["scheme_code"] == 1)
    assert fund["horizon"] == 7
    assert fund["n_windows"] == 77            # 70 + horizon, per the fixture
    assert fund["blended_wm"] == pytest.approx(2.07)
    assert fund["fqs"] == 88.0
    assert fund["fqs_exact"] is True
    # Legacy aliases kept for old APK builds must still be emitted.
    assert fund["selected_fqs"] == 88.0
    assert fund["selected_n_windows"] == 77


def test_every_field_the_home_screen_reads_is_still_present(universe):
    result = _home(10)
    fund = next(f for f in result["eligible_funds"] if f["scheme_code"] == 1)
    for key in (
        "scheme_code", "scheme_name", "category", "scheme_category",
        "history_years", "headline_fqs", "headline_tier", "headline_horizon",
        "last_fqs_refresh", "broad_category", "amfi_sub",
        "fqs", "fqs_tier", "blended_wm", "success_pct", "n_windows", "cv",
        "p10_wm", "horizon", "fqs_exact",
        "continuity_reset_date", "continuity_reason",
    ):
        assert key in fund, key


def test_per_horizon_maps_are_not_shipped_to_the_client(universe):
    """~50 horizons x 11 fields per fund that no client ever reads. Dropping
    them is most of the response-size and peak-RSS saving on this endpoint."""
    result = _home(10)
    for fund in result["eligible_funds"]:
        assert "fqs_by_horizon" not in fund
        assert "fqs_by_horizon_2010" not in fund
        assert "fqs_by_horizon_continuity" not in fund


def test_total_indexed_still_counts_every_document_including_dead_ones(universe):
    """Hiding dead funds from the LIST must not quietly rewrite the "funds
    indexed" figure — historical data is still there, and is still counted."""
    server._total_indexed_cache.update({"value": None, "at": None})
    result = _home(10)
    assert result["total_funds_indexed"] == 5
    assert result["eligible_fund_count"] == 2


def test_lumpsum_universe_applies_the_same_rule(universe):
    server._total_indexed_cache.update({"value": None, "at": None})
    server.db.fund_lumpsum_analysis = _Collection([])
    server.db.lumpsum_refresh_log = _Collection([
        {"_id": "lumpsum_refresh", "finished_at": "2026-08-31T03:00:00Z"},
    ])
    result = asyncio.run(server.lumpsum_universe(
        tenure=10, since_2010=False, separate_management=False))
    assert _codes(result) == [1, 4]


# ─── Conditional projection: the toggles must still select their own numbers ──
#
# The endpoint now fetches the Since-2010 and Separate-Management per-horizon
# maps ONLY when their toggle is on. That is safe exactly because nothing reads
# them otherwise — which is what these pin. A regression here would show as the
# toggle silently doing nothing.

def test_universe_projection_only_fetches_what_the_toggles_need():
    plain = server._universe_projection(False, False)
    assert "fqs_by_horizon" in plain
    assert "fqs_by_horizon_2010" not in plain
    assert "fqs_by_horizon_continuity" not in plain

    with_2010 = server._universe_projection(True, False)
    assert "fqs_by_horizon_2010" in with_2010
    assert "fqs_by_horizon_continuity" not in with_2010

    with_both = server._universe_projection(True, True)
    assert "fqs_by_horizon_2010" in with_both
    assert "fqs_by_horizon_continuity" in with_both
    assert "continuity_reset_date" in with_both


@pytest.fixture
def variant_universe(monkeypatch):
    """One live fund carrying all three variants with distinguishable FQS."""
    fund = _fund(1, "Variant Fund - Growth", own_latest=FRESH, fqs=70.0)
    fund["fqs_by_horizon_2010"] = {
        "10": {"fqs": 80.0, "tier": "Good", "blended_wm": 2.5, "median_wm": 2.5,
               "p10_wm": 2.0, "success_pct": 0.95, "n_windows": 60, "cv": 0.14},
    }
    fund["fqs_by_horizon_continuity"] = {
        "10": {"fqs": 90.0, "tier": "Good", "blended_wm": 3.0, "median_wm": 3.0,
               "p10_wm": 2.4, "success_pct": 0.98, "n_windows": 30, "cv": 0.12},
    }
    fund["continuity_reset_date"] = "2018-04-01"
    fund["continuity_reason"] = "Merger / successor scheme change"
    monkeypatch.setattr(server, "db", _DB([fund]))
    return fund


def _only(result):
    return result["eligible_funds"][0]


def test_plain_view_uses_since_inception_numbers(variant_universe):
    assert _only(_home(10))["fqs"] == 70.0


def test_since_2010_toggle_still_selects_the_2010_numbers(variant_universe):
    fund = _only(_home(10, since_2010=True))
    assert fund["fqs"] == 80.0


def test_separate_management_toggle_still_selects_the_continuity_numbers(variant_universe):
    fund = _only(_home(10, separate_management=True))
    assert fund["fqs"] == 90.0
    # The break date/reason are surfaced only when they actually drive the
    # numbers — the "since Apr 2018" tag on the fund card.
    assert fund["continuity_reset_date"] == "2018-04-01"
    assert fund["continuity_reason"] == "Merger / successor scheme change"


def test_break_date_is_not_surfaced_when_the_toggle_is_off(variant_universe):
    fund = _only(_home(10))
    assert fund["continuity_reset_date"] is None
    assert fund["continuity_reason"] is None


def test_continuity_takes_precedence_over_since_2010(variant_universe):
    fund = _only(_home(10, since_2010=True, separate_management=True))
    assert fund["fqs"] == 90.0


def test_fund_without_a_variant_falls_back_rather_than_being_hidden(monkeypatch):
    plain = _fund(1, "Plain Fund - Growth", own_latest=FRESH, fqs=70.0)
    monkeypatch.setattr(server, "db", _DB([plain]))
    fund = _only(_home(10, since_2010=True))
    assert fund["fqs"] == 70.0


# ─── /api/funds/search — label by default, hide only when picking ────────────

@pytest.fixture
def search_universe(monkeypatch):
    async def fake_fetch_fund_list():
        return [
            {"schemeCode": 1, "schemeName": "Acme Live Fund - Growth"},
            {"schemeCode": 2, "schemeName": "Acme Discontinued Fund - Growth"},
            {"schemeCode": 3, "schemeName": "Acme Pre-Merger Fund - Growth"},
            {"schemeCode": 9, "schemeName": "Acme Brand New Fund - Growth"},
        ]
    monkeypatch.setattr(server.amfi_client, "fetch_fund_list", fake_fetch_fund_list)

    async def fake_resolve(db, query, limit=25):
        return []
    monkeypatch.setattr(server.lineage_store, "resolve", fake_resolve)

    monkeypatch.setattr(server, "db", _DB([
        _fund(1, "Acme Live Fund - Growth", own_latest=FRESH),
        _fund(2, "Acme Discontinued Fund - Growth", own_latest=STALE),
        _fund(3, "Acme Pre-Merger Fund - Growth", own_latest=STALE,
              latest=FRESH, merged_with=4),
        # scheme 9 is deliberately NOT in the index — a brand-new scheme.
    ]))


def _search(q, active_only=False):
    return asyncio.run(server.search_funds(q=q, limit=25, active_only=active_only))["results"]


def test_search_labels_dead_funds_but_still_finds_them(search_universe):
    """Looking up what happened to an old fund is legitimate, so search does
    not hide it — it says why it is no longer available."""
    by_code = {r["scheme_code"]: r for r in _search("acme")}
    assert set(by_code) == {1, 2, 3, 9}
    assert by_code[1]["is_active"] is True
    assert by_code[2]["is_active"] is False
    assert "no NAV since" in by_code[2]["inactive_reason"]
    assert by_code[3]["is_active"] is False
    assert by_code[3]["current_scheme_code"] == 4


def test_search_active_only_drops_them(search_universe):
    """The Add Fund picker passes active_only — a discontinued scheme must
    never be offered as something to invest in."""
    assert sorted(r["scheme_code"] for r in _search("acme", active_only=True)) == [1, 9]


def test_search_keeps_a_scheme_that_is_not_indexed_yet(search_universe):
    """No index entry is no evidence of death — a brand-new scheme must still
    be pickable."""
    row = next(r for r in _search("acme", active_only=True) if r["scheme_code"] == 9)
    assert row["is_active"] is True


def test_search_still_works_when_the_status_index_is_unreachable(search_universe, monkeypatch):
    """The status lookup is an enhancement, never a dependency."""
    class _Broken:
        def find(self, *a, **kw):
            raise RuntimeError("mongo down")
        async def count_documents(self, *a, **kw):
            return 0
        async def find_one(self, *a, **kw):
            return None

    monkeypatch.setattr(server.db, "fund_nav_meta", _Broken())
    assert sorted(r["scheme_code"] for r in _search("acme")) == [1, 2, 3, 9]
