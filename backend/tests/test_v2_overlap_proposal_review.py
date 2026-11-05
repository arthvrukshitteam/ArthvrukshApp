"""version2 additions: overlap unique holdings / candidates, per-fund
proposal basis, the Portfolio Review Nifty 50 benchmark, and fund browse."""
import asyncio
import os
import sys

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import bulk_analysis as ba  # noqa: E402
import overlap_routes  # noqa: E402
import proposal_routes  # noqa: E402
import server  # noqa: E402
from _fake_db import FakeDB  # noqa: E402
from overlap_engine import compute_overlap  # noqa: E402

ISIN = {
    "hdfc": "INE040A01034", "icici": "INE090A01021", "infy": "INE009A01021",
    "rel": "INE002A01018", "tcs": "INE467B01029", "itc": "INE154A01025",
}


def _h(key, w):
    return {"name": key.upper() + " Ltd", "isin": ISIN[key], "weight_pct": w}


# ── overlap engine additions ────────────────────────────────────────────────

def test_overlap_lists_unique_holdings_and_common_share_per_fund():
    a = [_h("hdfc", 9.0), _h("icici", 8.0), _h("infy", 5.0), _h("rel", 3.0)]
    b = [_h("hdfc", 6.0), _h("icici", 2.0), _h("tcs", 7.0), _h("itc", 4.0)]
    r = compute_overlap(a, b)
    assert r["common_count"] == 2
    assert [u["name"] for u in r["unique_a"]] == ["INFY Ltd", "REL Ltd"]      # largest first
    assert [u["name"] for u in r["unique_b"]] == ["TCS Ltd", "ITC Ltd"]
    assert r["only_in_a"] == len(r["unique_a"]) and r["only_in_b"] == len(r["unique_b"])
    # Share of each fund in the common stocks (raw and equity-rebased).
    assert r["common_weight_a"] == pytest.approx(17.0)
    assert r["common_weight_b"] == pytest.approx(8.0)
    assert r["common_weight_a_equity"] == pytest.approx(17 / 25 * 100, abs=0.01)
    assert r["common_weight_b_equity"] == pytest.approx(8 / 19 * 100, abs=0.01)
    # The headline is untouched by the additions: Σ min(rebased weights).
    expected = min(9 / 25, 6 / 19) * 100 + min(8 / 25, 2 / 19) * 100
    assert r["overlap_pct"] == pytest.approx(expected, abs=0.01)


def test_overlap_identical_books_have_no_unique_holdings():
    a = [_h("hdfc", 50.0), _h("tcs", 50.0)]
    r = compute_overlap(a, list(a))
    assert r["unique_a"] == [] and r["unique_b"] == []
    assert r["overlap_pct"] == pytest.approx(100.0)
    assert r["common_weight_a_equity"] == pytest.approx(100.0)


# ── overlap candidates + compare enrichment ─────────────────────────────────

@pytest.fixture
def overlap_client():
    db = FakeDB(
        fund_holdings=[
            {"_id": "1:2026-09", "scheme_code": 1, "scheme_name": "HDFC Flexi Cap Fund - Growth",
             "period": "2026-09", "amc": "HDFC Mutual Fund", "equity_count": 2, "equity_weight": 95.0,
             "holdings": [_h("hdfc", 10.0), _h("icici", 8.0)]},
            {"_id": "2:2026-09", "scheme_code": 2, "scheme_name": "Kotak Flexicap Fund - Growth",
             "period": "2026-09", "amc": "Kotak Mutual Fund", "equity_count": 2, "equity_weight": 90.0,
             "holdings": [_h("hdfc", 7.0), _h("tcs", 6.0)]},
        ],
        fund_nav_meta=[
            {"scheme_code": 1, "scheme_name": "HDFC Flexi Cap Fund - Growth", "fund_house": "HDFC Mutual Fund",
             "category": "Equity", "headline_fqs": 84.0, "headline_tier": "Strong", "headline_horizon": 15},
            {"scheme_code": 2, "scheme_name": "Kotak Flexicap Fund - Growth", "fund_house": "Kotak Mutual Fund",
             "category": "Equity", "headline_fqs": 81.0, "headline_tier": "Strong", "headline_horizon": 10},
            {"scheme_code": 3, "scheme_name": "SBI Bluechip Fund - Regular Growth", "fund_house": "SBI Mutual Fund",
             "headline_fqs": 79.0},
            {"scheme_code": 4, "scheme_name": "SBI Bluechip Fund - Direct Plan - Growth", "headline_fqs": 80.0},
            {"scheme_code": 5, "scheme_name": "SBI Bluechip Fund - IDCW", "headline_fqs": 79.0},
            {"scheme_code": 6, "scheme_name": "Old Merged Fund - Growth", "merged_with": 3, "headline_fqs": 99.0},
        ],
    )
    app = FastAPI()
    app.include_router(overlap_routes.build_router(db))
    return TestClient(app)


def test_candidates_list_app_funds_with_disclosure_first(overlap_client):
    r = overlap_client.get("/api/overlap/candidates").json()
    codes = [x["scheme_code"] for x in r["results"]]
    assert codes[:2] == [1, 2]            # disclosure on file → first, best FQS first
    assert 3 in codes                      # app fund without a disclosure is still offered
    assert 4 not in codes and 5 not in codes  # Direct / IDCW never offered
    assert 6 not in codes                  # merged-away fund is not an option
    assert r["with_holdings"] == 2
    by = {x["scheme_code"]: x for x in r["results"]}
    assert by[1]["has_holdings"] and by[1]["period"] == "2026-09"
    assert by[3]["has_holdings"] is False and by[3]["amc"] == "SBI Mutual Fund"


def test_candidates_search_matches_every_token(overlap_client):
    r = overlap_client.get("/api/overlap/candidates", params={"q": "sbi blue"}).json()
    assert [x["scheme_code"] for x in r["results"]] == [3]
    r = overlap_client.get("/api/overlap/candidates", params={"q": "kotak"}).json()
    assert [x["scheme_code"] for x in r["results"]] == [2]


def test_candidates_search_is_not_a_regex(overlap_client):
    r = overlap_client.get("/api/overlap/candidates", params={"q": "(.*"})
    assert r.status_code == 200 and r.json()["results"] == []


def test_compare_adds_decision_facts(overlap_client):
    r = overlap_client.get("/api/overlap/compare", params={"fund_a": 1, "fund_b": 2}).json()
    assert r["period"] == "2026-09"
    assert r["common_count"] == 1
    assert r["fund_a"]["headline_fqs"] == 84.0 and r["fund_b"]["amc"] == "Kotak Mutual Fund"
    assert r["fund_a"]["top10_weight"] == pytest.approx(18.0)
    assert [u["name"] for u in r["unique_b"]] == ["TCS Ltd"]


# ── proposal: per-fund basis ─────────────────────────────────────────────────

def _analysis(name, fqs):
    return {"scheme_name": name, "category": "Equity", "horizons": [
        {"horizon": 10, "fqs": fqs, "median_wm": 2.4, "p10_wm": 1.9, "success_pct": 0.9,
         "blended_wm": 2.2, "n_windows": 30, "cv": 0.2},
    ]}


@pytest.fixture
def proposal_env():
    calls = {"build": [], "cached": []}

    async def build_sip(code, start_date=None, user_years=None, include_predecessor=True):
        calls["build"].append((code, start_date.isoformat() if start_date else None, include_predecessor))
        return _analysis(f"Fund {code}", 70.0 if start_date else 60.0)

    async def cached_sip(code):
        calls["cached"].append(code)
        return _analysis(f"Fund {code}", 60.0)

    async def unused(*a, **k):
        raise AssertionError("lumpsum path not expected")

    db = FakeDB(portfolio_proposals=[], fund_nav_meta=[
        {"scheme_code": 7, "scheme_name": "Fund 7", "category": "Equity",
         "fqs_by_horizon_continuity": {"10": {"fqs": 99.0, "median_wm": 3.0, "p10_wm": 2.5, "success_pct": 1.0}},
         "continuity_reset_date": "2018-01-01"},
    ])
    app = FastAPI()
    app.include_router(proposal_routes.build_router(db, build_sip, cached_sip, unused, unused))
    return TestClient(app), calls, db


def _req(funds, **extra):
    return {"mode": "sip", "horizon": 10, "investment_amount": 10000, "funds": funds, **extra}


def test_proposal_scores_a_fund_from_its_start_date(proposal_env):
    client, calls, _ = proposal_env
    r = client.post("/api/proposal/build", json=_req([
        {"scheme_code": 5, "allocation_pct": 0.5, "start_date": "2020-01-01", "explicit_basis": True},
        {"scheme_code": 6, "allocation_pct": 0.5},
    ]))
    assert r.status_code == 200, r.text
    doc = r.json()
    assert (5, "2020-01-01", True) in calls["build"]      # live, from the chosen date
    assert 6 in calls["cached"]                           # untouched fund: cached since-inception
    fqs = {f["scheme_code"]: f["fqs"] for f in doc["plan"]["funds"]}
    assert fqs[5] == 70.0 and fqs[6] == 60.0
    assert doc["fund_bases"] == {"5": {"start_date": "2020-01-01", "include_predecessor": True, "explicit_basis": True}}
    assert any("from 2020-01-01" in w for w in doc["warnings"])


def test_explicit_full_history_beats_the_home_management_toggle(proposal_env):
    client, calls, _ = proposal_env
    doc = client.post("/api/proposal/build", json=_req(
        [{"scheme_code": 7, "allocation_pct": 1.0, "explicit_basis": True}],
        separate_management=True,
    )).json()
    # Without explicit_basis the toggle would have served the continuity
    # variant (FQS 99); the advisor explicitly chose "all years".
    assert doc["plan"]["funds"][0]["fqs"] == 60.0


def test_home_toggle_still_applies_without_a_per_fund_basis(proposal_env):
    client, _, _ = proposal_env
    doc = client.post("/api/proposal/build", json=_req(
        [{"scheme_code": 7, "allocation_pct": 1.0}], separate_management=True,
    )).json()
    assert doc["plan"]["funds"][0]["fqs"] == 99.0
    assert doc["fund_bases"] == {}


def test_proposal_rejects_a_bad_start_date(proposal_env):
    client, _, _ = proposal_env
    for bad in ("2020-13-01", "yesterday", "2999-01-01"):
        r = client.post("/api/proposal/build", json=_req([{"scheme_code": 5, "allocation_pct": 1.0, "start_date": bad}]))
        assert r.status_code == 422, bad


# ── Portfolio Review: Nifty 50 benchmark ────────────────────────────────────

BY_CODE = {
    120716: "UTI Nifty 50 Index Fund - Regular Plan - Growth Option",
    120717: "UTI Nifty 50 Index Fund - Direct Plan - Growth Option",
    101: "HDFC Index Fund-NIFTY 50 Plan - Regular Plan",
    102: "ICICI Prudential Nifty Next 50 Index Fund - Growth",
    103: "Motilal Oswal Nifty 500 Index Fund - Regular Plan Growth",
    104: "DSP Nifty 50 Equal Weight Index Fund - Regular Plan - Growth",
    105: "Nippon India ETF Nifty 50 BeES",
    106: "SBI Nifty 50 Index Fund - Regular Plan - IDCW",
    107: "Parag Parikh Flexi Cap Fund - Regular Plan - Growth",
}


def test_nifty50_candidates_name_filter():
    assert sorted(ba.nifty50_candidates(BY_CODE)) == [101, 120716]


def test_nifty50_proxy_is_the_longest_live_history():
    db = FakeDB(fund_nav_meta=[
        {"scheme_code": 120716, "history_years": 19.4, "own_latest_nav_date": "2099-01-01"},
        {"scheme_code": 101, "history_years": 23.1, "own_latest_nav_date": "2000-01-01"},  # stale
    ])
    assert asyncio.run(ba.resolve_nifty50_proxy(db, BY_CODE)) == 120716


def test_nifty50_benchmark_row_and_summary(monkeypatch):
    db = FakeDB(fund_nav_meta=[{"scheme_code": 120716, "history_years": 19.4}])

    async def fake_result(_db, code, *_a):
        return {"picked": ({"fqs": 72.5, "success_pct": 0.95, "median_wm": 2.1, "p10_wm": 1.7, "n_windows": 100, "cv": 0.1}, 10),
                "scheme_name": BY_CODE[code], "category": "Index Fund"}

    monkeypatch.setattr(ba, "get_fund_result", fake_result)
    values, summary = asyncio.run(ba.nifty50_benchmark(db, BY_CODE, None, None))
    assert values["Fund Name"] == ba.NIFTY50_LABEL
    assert "UTI Nifty 50 Index Fund" in values["Remarks"]
    assert summary["status"] == "ok" and summary["fqs"] == 72.5 and summary["horizon"] == 10
    assert summary["xirr_med"] is not None


def test_nifty50_benchmark_unavailable_without_a_proxy():
    values, summary = asyncio.run(ba.nifty50_benchmark(FakeDB(fund_nav_meta=[]), {1: "Some Fund"}, None, None))
    assert values is None and summary["status"] == "unavailable"


# ── fund browse ──────────────────────────────────────────────────────────────

def test_browse_lists_active_regular_funds_best_fqs_first(monkeypatch):
    db = FakeDB(fund_nav_meta=[
        {"scheme_code": 1, "scheme_name": "Alpha Fund - Growth", "headline_fqs": 70.0},
        {"scheme_code": 2, "scheme_name": "Beta Fund - Growth", "headline_fqs": 90.0},
        {"scheme_code": 3, "scheme_name": "Beta Fund - Direct Plan - Growth", "headline_fqs": 91.0},
        {"scheme_code": 4, "scheme_name": "Gamma Fund - IDCW", "headline_fqs": 95.0},
        {"scheme_code": 5, "scheme_name": "Delta Fund - Growth", "superseded_by": 1, "headline_fqs": 99.0},
        {"scheme_code": 6, "scheme_name": "Epsilon Fund - Growth"},
    ])
    monkeypatch.setattr(server, "db", db)
    r = asyncio.run(server.browse_funds(offset=0, limit=40))
    assert [x["scheme_code"] for x in r["results"]] == [2, 1, 6]
    assert all(x["is_active"] for x in r["results"])
    assert r["next_offset"] is None
