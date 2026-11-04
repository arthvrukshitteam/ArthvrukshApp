"""Backend regression tests for the rebuilt MF Analyst API.

Covers: /health, /funds/search, /funds/{code}/analyze (default + start_date + bad input + 404),
/funds/{code}/meta, /funds/recent, /refresh/status, /refresh/run.
Also asserts that removed endpoints (/funds/top, /goal/*) return 404.
"""
import os
import pytest
import requests

BASE_URL = os.environ["EXPO_PUBLIC_BACKEND_URL"].rstrip("/") if os.environ.get("EXPO_PUBLIC_BACKEND_URL") else "https://fund-scorer.preview.emergentagent.com"
NIPPON = 118778


@pytest.fixture(scope="module")
def s():
    sess = requests.Session()
    sess.headers.update({"Content-Type": "application/json"})
    return sess


# ── health ──
def test_health(s):
    r = s.get(f"{BASE_URL}/api/health", timeout=15)
    assert r.status_code == 200
    assert r.json().get("status") == "ok"


# ── search ──
def test_search_nippon_small(s):
    r = s.get(f"{BASE_URL}/api/funds/search", params={"q": "nippon small"}, timeout=30)
    assert r.status_code == 200
    body = r.json()
    codes = [it["scheme_code"] for it in body["results"]]
    assert NIPPON in codes, f"118778 not in results: {codes[:10]}"
    # multi-token AND check
    for it in body["results"]:
        n = it["scheme_name"].lower()
        assert "nippon" in n and "small" in n


# ── analyze default ──
@pytest.fixture(scope="module")
def analysis(s):
    r = s.get(f"{BASE_URL}/api/funds/{NIPPON}/analyze", timeout=180)
    assert r.status_code == 200, r.text
    return r.json()


def test_analyze_basic_shape(analysis):
    a = analysis
    assert "Nippon India Small Cap" in a["scheme_name"]
    assert a["category"] == "Small Cap"
    assert a["monthly_points"] >= 150
    assert 0.15 <= (a["headline_xirr"] or 0) <= 0.35
    assert 70 <= (a["headline_fqs"] or 0) <= 100
    assert a["cycle_robustness"] is not None
    assert a["stability_check"] is not None
    horizons = a["available_horizons"]
    assert horizons[0] == 1
    assert max(horizons) >= 12  # ~13 expected
    assert a["start_date_applied"] is None


def test_analyze_window_counts(analysis):
    m = analysis["monthly_points"]
    by_h = {h["horizon"]: h for h in analysis["horizons"]}
    # Excel-faithful rolling-window count: skip monthly[0] as start and require
    # one extra month-end after the last SIP. When NAVs are dense (one per
    # calendar month, no gaps) this evaluates to exactly m - N - 1. For
    # Nippon's 186 month-ends this gives 173 (1Y) and 65 (10Y).
    assert by_h[1]["n_windows"] in {m - 12 - 1, m - 12, m - 12 - 2}
    assert by_h[10]["n_windows"] in {m - 120 - 1, m - 120, m - 120 - 2}
    # 1Y > 10Y
    assert by_h[1]["n_windows"] > by_h[10]["n_windows"]
    w = by_h[1]["windows"][0]
    for k in ["start_date", "end_date", "total_units", "end_nav", "end_value",
              "total_invested", "wealth_multiple", "good"]:
        assert k in w


# ── analyze start_date ──
def test_analyze_with_start_date(s):
    r = s.get(f"{BASE_URL}/api/funds/{NIPPON}/analyze",
              params={"start_date": "2018-01-01"}, timeout=180)
    assert r.status_code == 200
    a = r.json()
    assert a["start_date_applied"] == "2018-01-01"
    assert 90 <= a["monthly_points"] <= 110
    assert max(a["available_horizons"]) <= 8


def test_analyze_bad_date(s):
    r = s.get(f"{BASE_URL}/api/funds/{NIPPON}/analyze",
              params={"start_date": "bad-format"}, timeout=30)
    assert r.status_code == 400


def test_analyze_unknown_scheme(s):
    r = s.get(f"{BASE_URL}/api/funds/99999999/analyze", timeout=60)
    assert r.status_code == 404


# ── meta ──
def test_meta(s):
    r = s.get(f"{BASE_URL}/api/funds/{NIPPON}/meta", timeout=30)
    assert r.status_code == 200
    d = r.json()
    assert "scheme_name" in d and d["scheme_name"]
    assert d["earliest_nav_date"] and d["latest_nav_date"]
    assert d["nav_points"] > 1000


# ── recent ──
def test_recent_contains_nippon(s, analysis):
    r = s.get(f"{BASE_URL}/api/funds/recent", timeout=30)
    assert r.status_code == 200
    rows = r.json()["results"]
    codes = [row["scheme_code"] for row in rows]
    assert NIPPON in codes
    row = next(x for x in rows if x["scheme_code"] == NIPPON)
    assert "headline_fqs" in row
    assert "last_seen" in row


# ── refresh ──
def test_refresh_status_initial(s):
    r = s.get(f"{BASE_URL}/api/refresh/status", timeout=15)
    assert r.status_code == 200
    # Either a doc with last_run or default placeholder
    assert "last_run" in r.json() or r.json() == {"last_run": None, "schemes_refreshed": 0, "duration_seconds": 0}


def test_refresh_run(s):
    r = s.post(f"{BASE_URL}/api/refresh/run", timeout=300)
    assert r.status_code == 200
    d = r.json()
    assert "schemes_refreshed" in d
    assert "duration_seconds" in d
    # status now has last_run set
    r2 = s.get(f"{BASE_URL}/api/refresh/status", timeout=15)
    assert r2.status_code == 200
    assert r2.json().get("last_run")


# ── removed endpoints ──
@pytest.mark.parametrize("path,method", [
    ("/api/funds/top", "GET"),
    ("/api/goal/calculate", "POST"),
    ("/api/goal/check", "POST"),
])
def test_removed_endpoints(s, path, method):
    r = s.request(method, f"{BASE_URL}{path}", json={}, timeout=15)
    assert r.status_code == 404, f"{method} {path} returned {r.status_code}"
