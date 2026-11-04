"""Edge-case tests for fund-identity continuity (successor stitching + disclosure).

Run: cd backend && python3 -m pytest tests/test_fund_lineage.py -v

These cover the pure logic (no network). The live mfapi.in end-to-end check
(BNP 113566 → Baroda BNP 150209) is exercised by test_load_analysis_nav via a
fake fetch, and should also be smoke-tested against the deployed API where the
mfapi.in feed is reachable.
"""

import asyncio
from datetime import date, timedelta

import pytest

import fund_lineage as fl
import amfi_client


# ── Helpers ──────────────────────────────────────────────────────────────────

def _series(start: date, n: int, base: float, drift: float, step_days: int = 1):
    """Build ascending [{date,nav}] with a deterministic pseudo-random walk."""
    rows = []
    nav = base
    seed = 7
    for i in range(n):
        seed = (1103515245 * seed + 12345) % (2 ** 31)
        shock = (seed / (2 ** 31) - 0.5) * 0.02       # ±1% daily noise
        nav = nav * (1 + drift + shock)
        rows.append({"date": start + timedelta(days=i * step_days), "nav": round(nav, 4)})
    return rows


def _daily_until(start: date, end: date, base: float, drift: float):
    """Daily walk from start to end inclusive (used so a fund actually reaches today)."""
    n = (end - start).days + 1
    return _series(start, max(n, 1), base, drift)


# ── canonical_house: cross-house AMC aliases collapse ────────────────────────

def test_canonical_house_amc_merger_aliases():
    assert fl.canonical_house("BNP Paribas Mutual Fund") == "baroda bnp paribas"
    assert fl.canonical_house("Baroda BNP Paribas Mutual Fund") == "baroda bnp paribas"
    # Old and new names must land in the SAME bucket — the whole point.
    assert fl.canonical_house("BNP Paribas Mid Cap Fund-Growth Option") == \
           fl.canonical_house("BARODA BNP PARIBAS Mid Cap Fund - Regular Plan - Growth Option")

def test_canonical_house_other_acquisitions():
    assert fl.canonical_house("Morgan Stanley A.C.E. Fund") == "hdfc"
    assert fl.canonical_house("HDFC Small Cap Fund") == "hdfc"
    assert fl.canonical_house("IDFC Sterling Value Fund") == fl.canonical_house("Bandhan Sterling Value Fund")
    assert fl.canonical_house("L&T Midcap Fund") == fl.canonical_house("HSBC Midcap Fund")

def test_canonical_house_unrelated_stay_distinct():
    # Two genuinely different houses must NOT collapse together.
    assert fl.canonical_house("Axis Bluechip Fund") != fl.canonical_house("SBI Bluechip Fund")
    assert fl.canonical_house("Quant Small Cap Fund") != fl.canonical_house("HDFC Small Cap Fund")

def test_canonical_house_brand_names_containing_india():
    # BUG FIXED (2026-08-04): _HOUSE_SUFFIX_RE strips "india" as generic
    # corporate boilerplate, but three canonical keys have "india" baked
    # into the brand itself. Stripping it before alias-matching broke the
    # LIVE side of each pair, since the alias table lists the OLD name
    # ("reliance", "boi axa") but the group's canonical key ("nippon india",
    # "bank of india") itself contains "india" and must also self-match.
    assert fl.canonical_house("Nippon India Mutual Fund") == "nippon india"
    assert fl.canonical_house("Reliance Mutual Fund") == "nippon india"
    assert fl.canonical_house("Nippon India Mutual Fund") == fl.canonical_house("Reliance Mutual Fund")

    assert fl.canonical_house("Bank of India Mutual Fund") == "bank of india"
    assert fl.canonical_house("BOI AXA Mutual Fund") == "bank of india"
    assert fl.canonical_house("Bank of India Mutual Fund") == fl.canonical_house("BOI AXA Mutual Fund")

    # PGIM India survived by accident before the fix (coincidental bare
    # "pgim" alias) — pin it down explicitly now that it's fixed for real.
    assert fl.canonical_house("PGIM India Mutual Fund") == "pgim india"
    assert fl.canonical_house("DHFL Pramerica Mutual Fund") == "pgim india"

def test_canonical_house_empty_and_junk():
    assert fl.canonical_house("") == ""
    assert fl.canonical_house(None) == ""


# ── overlap_correlation + verify_stitch ──────────────────────────────────────

def test_verify_stitch_true_continuation_accepts():
    """Same portfolio during overlap → high correlation → accept."""
    pred = _series(date(2021, 12, 1), 120, 50.0, 0.0005)
    # Successor shares the SAME dated NAVs in the overlap window (identical path).
    overlap = {r["date"]: r["nav"] for r in pred[-40:]}
    succ = [{"date": d, "nav": v} for d, v in overlap.items()]
    succ += _series(pred[-1]["date"] + timedelta(days=1), 200, pred[-1]["nav"], 0.0006)
    ok, sig = fl.verify_stitch(pred, succ, curated=True)
    assert ok is True
    assert sig["correlation"] is not None and sig["correlation"] > 0.9

def test_verify_stitch_uncorrelated_rejected_even_if_curated():
    """A wrong pairing (independent random walks) must be REJECTED despite curation.
    This is the guard that a bad map entry can't corrupt returns."""
    pred = _series(date(2021, 1, 1), 300, 40.0, 0.0004)
    # Independent series over the SAME dates but different noise seed base.
    succ = [{"date": r["date"], "nav": round(r["nav"] * 0.3 + (i % 7) * 1.7 + 5, 4)}
            for i, r in enumerate(pred)]
    ok, sig = fl.verify_stitch(pred, succ, curated=True)
    assert ok is False
    assert sig["correlation"] is not None and sig["correlation"] < 0.8

def test_verify_stitch_no_overlap_curated_small_gap_accepts():
    pred = _series(date(2018, 1, 1), 400, 30.0, 0.0003)
    succ = _series(pred[-1]["date"] + timedelta(days=20), 300, pred[-1]["nav"], 0.0004)
    ok, sig = fl.verify_stitch(pred, succ, curated=True)
    assert ok is True
    assert sig["overlap_days"] < fl.MIN_OVERLAP_DAYS
    assert 0 <= sig["gap_days"] <= fl.MAX_GAP_DAYS_NO_OVERLAP

def test_verify_stitch_no_overlap_discovered_rejected():
    """Automated pairs with no overlap are never trusted."""
    pred = _series(date(2018, 1, 1), 400, 30.0, 0.0003)
    succ = _series(pred[-1]["date"] + timedelta(days=20), 300, pred[-1]["nav"], 0.0004)
    ok, _ = fl.verify_stitch(pred, succ, curated=False)
    assert ok is False

def test_verify_stitch_no_overlap_huge_gap_rejected():
    pred = _series(date(2015, 1, 1), 400, 30.0, 0.0003)
    succ = _series(pred[-1]["date"] + timedelta(days=800), 300, pred[-1]["nav"], 0.0004)
    ok, _ = fl.verify_stitch(pred, succ, curated=True)
    assert ok is False

def test_verify_stitch_empty_series_rejected():
    ok, sig = fl.verify_stitch([], [{"date": date(2020, 1, 1), "nav": 10.0}], curated=True)
    assert ok is False and sig["reason"] == "empty_series"


# ── stitch: successor wins on overlap, chronological ─────────────────────────

def test_stitch_successor_wins_on_overlap():
    d = date(2022, 3, 13)
    pred = [{"date": d - timedelta(days=1), "nav": 55.0}, {"date": d, "nav": 55.78}]
    succ = [{"date": d, "nav": 99.99}, {"date": d + timedelta(days=1), "nav": 100.5}]
    merged = fl.stitch(pred, succ)
    assert [r["date"] for r in merged] == sorted(r["date"] for r in merged)
    got = {r["date"]: r["nav"] for r in merged}
    assert got[d] == 99.99                      # successor overrides on shared date
    assert got[d - timedelta(days=1)] == 55.0   # predecessor-only date kept
    assert len(merged) == 3


# ── history_note_for: honest disclosure only when data truly starts later ────
# HISTORY_NOTES is intentionally empty by default (real gaps get bridged via
# CURATED_SUCCESSORS instead, see fund_lineage.py's HDFC Small Cap comment) —
# these tests inject a fake entry to cover the pure disclosure logic itself,
# for the rare case a gap genuinely cannot be stitched.

def test_history_note_present_when_data_starts_after_inception(monkeypatch):
    monkeypatch.setitem(fl.HISTORY_NOTES, 999001, {
        "real_inception": "2008-04-03",
        "note": "Fund inception ~2008; NAV history on the feed only from 2014.",
    })
    note = fl.history_note_for(999001, date(2014, 6, 1))
    assert note and "2008" in note

def test_history_note_suppressed_if_feed_gains_earlier_data(monkeypatch):
    # If the feed ever starts serving pre-inception data, the note disappears.
    monkeypatch.setitem(fl.HISTORY_NOTES, 999001, {
        "real_inception": "2008-04-03",
        "note": "Fund inception ~2008; NAV history on the feed only from 2014.",
    })
    assert fl.history_note_for(999001, date(2008, 1, 1)) is None

def test_history_note_none_for_normal_fund():
    assert fl.history_note_for(120503, date(2013, 1, 1)) is None


# ── resolver precedence ──────────────────────────────────────────────────────

def test_resolve_successor_curated_first():
    amfi_client.set_discovered_successors({113566: 999999})   # conflicting discovered
    code, curated = amfi_client.resolve_successor(113566)
    assert code == 150209 and curated is True                 # curated wins
    amfi_client.set_discovered_successors({})                 # reset

def test_resolve_successor_discovered_used_when_no_curated():
    amfi_client.set_discovered_successors({555001: 555002})
    code, curated = amfi_client.resolve_successor(555001)
    assert code == 555002 and curated is False
    amfi_client.set_discovered_successors({})

def test_resolve_successor_none_when_unknown():
    amfi_client.set_discovered_successors({})
    assert amfi_client.resolve_successor(123456) == (None, False)


# ── load_analysis_nav: end-to-end with a fake feed (no network) ──────────────

def _install_fake_feed(monkeypatch, feed):
    """feed: {scheme_code: (meta_dict, [{'date':'DD-MM-YYYY','nav':'x'}...] )}"""
    async def fake_fetch(code):
        meta, data = feed[int(code)]
        return {"meta": meta, "data": data}
    monkeypatch.setattr(amfi_client, "fetch_fund_nav", fake_fetch)


def _raw(rows):
    return [{"date": r["date"].strftime("%d-%m-%Y"), "nav": str(r["nav"])} for r in rows]


def test_load_analysis_nav_stitches_wound_up_bnp_like(monkeypatch):
    """BNP-like: old code wound up (last NAV ~4y ago), curated successor active,
    correlated overlap → stitched, wound_up flips to False, merged_with set."""
    today = date.today()
    pred_start = today - timedelta(days=365 * 6)
    pred = _series(pred_start, 800, 20.0, 0.0004)
    # cut predecessor off ~4 years ago (well past the 180d wound-up threshold)
    cutoff = today - timedelta(days=365 * 4)
    pred = [r for r in pred if r["date"] <= cutoff]
    overlap = pred[-40:]
    succ = [dict(r) for r in overlap] + _daily_until(
        pred[-1]["date"] + timedelta(days=1), today, pred[-1]["nav"], 0.0005
    )
    # ensure successor is active up to ~today
    feed = {
        113566: ({"scheme_code": 113566, "scheme_name": "BNP Paribas Mid Cap Fund-Growth Option",
                  "scheme_category": "Equity Scheme - Mid Cap Fund", "fund_house": "BNP Paribas Mutual Fund"}, _raw(pred)),
        150209: ({"scheme_code": 150209, "scheme_name": "BARODA BNP PARIBAS Mid Cap Fund - Regular Plan - Growth Option",
                  "scheme_category": "Equity Scheme - Mid Cap Fund", "fund_house": "Baroda BNP Paribas Mutual Fund"}, _raw(succ)),
    }
    _install_fake_feed(monkeypatch, feed)
    amfi_client.set_discovered_successors({})

    res = asyncio.run(amfi_client.load_analysis_nav(113566))
    assert res["successor_used"] == 150209
    assert res["wound_up"] is False                       # extended to active successor
    assert res["wound_up_date"] is not None               # original code did stop
    assert res["nav_history"][-1]["date"] >= today - timedelta(days=10)
    assert len(res["nav_history"]) > len(res["original_nav"])

def test_load_analysis_nav_rejects_bad_successor(monkeypatch):
    """If the mapped successor doesn't correlate, DO NOT stitch — stay wound up."""
    today = date.today()
    pred = _series(today - timedelta(days=365 * 6), 500, 20.0, 0.0004)
    cutoff = today - timedelta(days=365 * 4)
    pred = [r for r in pred if r["date"] <= cutoff]
    # unrelated successor over overlapping dates
    bad = [{"date": r["date"], "nav": round(3 + (i % 5) * 2.3, 4)} for i, r in enumerate(pred)]
    bad += _series(pred[-1]["date"] + timedelta(days=1), 900, 40.0, -0.0002)
    feed = {
        700001: ({"scheme_code": 700001, "scheme_name": "Foo Fund-Growth",
                  "scheme_category": "Equity", "fund_house": "Foo MF"}, _raw(pred)),
        700002: ({"scheme_code": 700002, "scheme_name": "Bar Fund-Growth",
                  "scheme_category": "Equity", "fund_house": "Foo MF"}, _raw(bad)),
    }
    _install_fake_feed(monkeypatch, feed)
    amfi_client.set_discovered_successors({700001: 700002})   # discovered (strict)

    res = asyncio.run(amfi_client.load_analysis_nav(700001))
    assert res["successor_used"] is None
    assert res["wound_up"] is True                            # correctly stays wound up

def test_load_analysis_nav_active_fund_untouched(monkeypatch):
    """A normal active fund: no successor lookup, no wound-up, history unchanged."""
    today = date.today()
    nav = _daily_until(today - timedelta(days=365 * 3), today, 15.0, 0.0004)
    feed = {800001: ({"scheme_code": 800001, "scheme_name": "Active Fund-Growth",
                      "scheme_category": "Equity", "fund_house": "Active MF"}, _raw(nav))}
    _install_fake_feed(monkeypatch, feed)
    amfi_client.set_discovered_successors({})
    res = asyncio.run(amfi_client.load_analysis_nav(800001))
    assert res["wound_up"] is False
    assert res["successor_used"] is None
    assert res["history_note"] is None
    assert len(res["nav_history"]) == len(res["original_nav"])

def test_load_analysis_nav_chained_merger(monkeypatch):
    """A → B → C chained mergers stitch through to the active tail."""
    today = date.today()
    a = _series(today - timedelta(days=365 * 8), 400, 10.0, 0.0004)
    a = [r for r in a if r["date"] <= today - timedelta(days=365 * 6)]
    b_overlap = a[-30:]
    b = [dict(r) for r in b_overlap] + _daily_until(a[-1]["date"] + timedelta(days=1), today - timedelta(days=365 * 3), a[-1]["nav"], 0.0004)
    c_overlap = b[-30:]
    c = [dict(r) for r in c_overlap] + _daily_until(b[-1]["date"] + timedelta(days=1), today, b[-1]["nav"], 0.0004)
    feed = {
        900001: ({"scheme_code": 900001, "scheme_name": "A Fund-Growth", "scheme_category": "Equity", "fund_house": "X MF"}, _raw(a)),
        900002: ({"scheme_code": 900002, "scheme_name": "B Fund-Growth", "scheme_category": "Equity", "fund_house": "X MF"}, _raw(b)),
        900003: ({"scheme_code": 900003, "scheme_name": "C Fund-Growth", "scheme_category": "Equity", "fund_house": "X MF"}, _raw(c)),
    }
    _install_fake_feed(monkeypatch, feed)
    amfi_client.set_discovered_successors({900001: 900002, 900002: 900003})
    res = asyncio.run(amfi_client.load_analysis_nav(900001))
    assert res["stitch_chain"] == [900002, 900003]
    assert res["wound_up"] is False
    assert res["nav_history"][-1]["date"] >= today - timedelta(days=10)
    amfi_client.set_discovered_successors({})

# ── resolve_predecessors: reverse lookup (backward direction) ────────────────

def test_resolve_predecessors_curated_real_pair():
    """The real Edelweiss Mid Cap Fund (140225) curated entry must be
    reverse-resolvable to its JPMorgan predecessor (107301) — this is the
    exact pairing the inception-date bug report was about."""
    preds = amfi_client.resolve_predecessors(140225)
    assert (107301, True) in preds

def test_resolve_predecessors_curated_hdfc_small_cap():
    """HDFC Small Cap Fund (130502) must reverse-resolve to Morgan Stanley
    A.C.E. Fund (108197) — a second, independently-verified real pairing
    (2014 AMC acquisition) proving the fix isn't Edelweiss-specific."""
    preds = amfi_client.resolve_predecessors(130502)
    assert (108197, True) in preds

def test_resolve_predecessors_discovered(monkeypatch):
    amfi_client.set_discovered_successors({601001: 601002})
    preds = amfi_client.resolve_predecessors(601002)
    assert preds == [(601001, False)]
    amfi_client.set_discovered_successors({})

def test_resolve_predecessors_curated_precedence(monkeypatch):
    """A code with BOTH a curated and a conflicting discovered predecessor
    must list the curated one; discovered entries pointing at the same
    successor but a different predecessor code are still included."""
    monkeypatch.setitem(amfi_client.MERGER_MAP, 602001, 602099)
    amfi_client.set_discovered_successors({602002: 602099})
    preds = amfi_client.resolve_predecessors(602099)
    assert (602001, True) in preds
    assert (602002, False) in preds
    amfi_client.set_discovered_successors({})

def test_resolve_predecessors_none_when_unmapped():
    assert amfi_client.resolve_predecessors(999999999) == []


# ── load_analysis_nav: BACKWARD stitching (active code has a predecessor) ────
# This is the shape of the real-world bug: the app queries the CURRENT/active
# scheme code (never itself wound up), so the pre-existing FORWARD-only logic
# never fired. These tests cover the fix.

def test_load_analysis_nav_backward_stitches_edelweiss_like(monkeypatch):
    """Query the ACTIVE (successor) code directly — mirrors an app user
    opening Edelweiss Mid Cap Fund. A curated predecessor with no NAV overlap
    but a small gap must be prepended, extending history far earlier than the
    active code's own first NAV."""
    today = date.today()
    pred_start = today - timedelta(days=365 * 9)
    pred = _series(pred_start, 2000, 10.0, 0.0004)
    cutoff = today - timedelta(days=365 * 5)
    pred = [r for r in pred if r["date"] <= cutoff]   # predecessor wound up ~5y ago
    succ = _daily_until(pred[-1]["date"] + timedelta(days=4), today, pred[-1]["nav"] * 1.02, 0.0005)

    feed = {
        701001: ({"scheme_code": 701001, "scheme_name": "Old Co India Mid Cap Fund-Growth",
                  "scheme_category": "Equity Scheme - Mid Cap Fund", "fund_house": "Old Co MF"}, _raw(pred)),
        701002: ({"scheme_code": 701002, "scheme_name": "New Co Mid Cap Fund - Regular Plan - Growth",
                  "scheme_category": "Equity Scheme - Mid Cap Fund", "fund_house": "New Co MF"}, _raw(succ)),
    }
    _install_fake_feed(monkeypatch, feed)
    monkeypatch.setitem(amfi_client.MERGER_MAP, 701001, 701002)

    res = asyncio.run(amfi_client.load_analysis_nav(701002))   # query the ACTIVE code
    assert res["predecessor_used"] == [701001]
    assert res["successor_used"] is None            # no forward stitch — 701002 was never wound up
    assert res["wound_up"] is False
    assert res["nav_history"][0]["date"] == pred[0]["date"]      # extends back to predecessor's start
    assert res["nav_history"][0]["date"] < res["original_nav"][0]["date"]
    assert len(res["nav_history"]) > len(res["original_nav"])

def test_load_analysis_nav_backward_rejects_uncorrelated_discovered(monkeypatch):
    """A discovered (non-curated) backward pair with no overlap must be
    rejected — only curated pairs may bridge a no-overlap gap."""
    today = date.today()
    pred = _series(today - timedelta(days=365 * 6), 500, 20.0, 0.0004)
    pred = [r for r in pred if r["date"] <= today - timedelta(days=365 * 4)]
    succ = _daily_until(pred[-1]["date"] + timedelta(days=10), today, pred[-1]["nav"], 0.0004)

    feed = {
        702001: ({"scheme_code": 702001, "scheme_name": "Foo Predecessor Fund-Growth",
                  "scheme_category": "Equity", "fund_house": "Foo MF"}, _raw(pred)),
        702002: ({"scheme_code": 702002, "scheme_name": "Bar Active Fund-Growth",
                  "scheme_category": "Equity", "fund_house": "Bar MF"}, _raw(succ)),
    }
    _install_fake_feed(monkeypatch, feed)
    amfi_client.set_discovered_successors({702001: 702002})   # discovered, not curated

    res = asyncio.run(amfi_client.load_analysis_nav(702002))
    assert res["predecessor_used"] == []
    assert res["nav_history"][0]["date"] == res["original_nav"][0]["date"]   # untouched
    amfi_client.set_discovered_successors({})

def test_load_analysis_nav_backward_chained(monkeypatch):
    """A → B → C: querying the newest active code (C) must chase back through
    B to A, prepending both, oldest first."""
    today = date.today()
    a = _series(today - timedelta(days=365 * 12), 1500, 8.0, 0.0004)
    a = [r for r in a if r["date"] <= today - timedelta(days=365 * 8)]
    b = _daily_until(a[-1]["date"] + timedelta(days=3), today - timedelta(days=365 * 4), a[-1]["nav"], 0.0004)
    c = _daily_until(b[-1]["date"] + timedelta(days=3), today, b[-1]["nav"], 0.0004)

    feed = {
        703001: ({"scheme_code": 703001, "scheme_name": "A Fund-Growth", "scheme_category": "Equity", "fund_house": "X MF"}, _raw(a)),
        703002: ({"scheme_code": 703002, "scheme_name": "B Fund-Growth", "scheme_category": "Equity", "fund_house": "X MF"}, _raw(b)),
        703003: ({"scheme_code": 703003, "scheme_name": "C Fund-Growth", "scheme_category": "Equity", "fund_house": "X MF"}, _raw(c)),
    }
    _install_fake_feed(monkeypatch, feed)
    monkeypatch.setitem(amfi_client.MERGER_MAP, 703001, 703002)
    monkeypatch.setitem(amfi_client.MERGER_MAP, 703002, 703003)

    res = asyncio.run(amfi_client.load_analysis_nav(703003))
    assert res["predecessor_used"] == [703001, 703002]     # oldest first
    assert res["nav_history"][0]["date"] == a[0]["date"]
    assert res["wound_up"] is False

def test_load_analysis_nav_backward_cycle_guard(monkeypatch):
    """Pathological cycle A↔B in the backward direction must terminate."""
    today = date.today()
    a = _series(today - timedelta(days=365 * 6), 300, 10.0, 0.0004)
    a = [r for r in a if r["date"] <= today - timedelta(days=365 * 4)]
    b = _daily_until(a[-1]["date"] + timedelta(days=3), today, a[-1]["nav"], 0.0004)

    feed = {
        704001: ({"scheme_code": 704001, "scheme_name": "A", "scheme_category": "Equity", "fund_house": "Y MF"}, _raw(a)),
        704002: ({"scheme_code": 704002, "scheme_name": "B", "scheme_category": "Equity", "fund_house": "Y MF"}, _raw(b)),
    }
    _install_fake_feed(monkeypatch, feed)
    monkeypatch.setitem(amfi_client.MERGER_MAP, 704001, 704002)
    monkeypatch.setitem(amfi_client.MERGER_MAP, 704002, 704001)   # cycle

    res = asyncio.run(amfi_client.load_analysis_nav(704002))   # must return, not loop forever
    assert res["predecessor_used"] == [704001]
    assert 704002 not in res["predecessor_used"]


def test_load_analysis_nav_cycle_guard(monkeypatch):
    """Pathological cycle A→B→A must terminate, not loop forever."""
    today = date.today()
    a = _series(today - timedelta(days=365 * 6), 300, 10.0, 0.0004)
    a = [r for r in a if r["date"] <= today - timedelta(days=365 * 4)]
    b = [dict(r) for r in a[-30:]] + _series(a[-1]["date"] + timedelta(days=1), 300, a[-1]["nav"], 0.0004)
    b = [r for r in b if r["date"] <= today - timedelta(days=365 * 3)]
    feed = {
        910001: ({"scheme_code": 910001, "scheme_name": "A", "scheme_category": "Equity", "fund_house": "Y MF"}, _raw(a)),
        910002: ({"scheme_code": 910002, "scheme_name": "B", "scheme_category": "Equity", "fund_house": "Y MF"}, _raw(b)),
    }
    _install_fake_feed(monkeypatch, feed)
    amfi_client.set_discovered_successors({910001: 910002, 910002: 910001})
    res = asyncio.run(amfi_client.load_analysis_nav(910001))   # must return
    assert 910002 in res["stitch_chain"]
    assert res["stitch_chain"].count(910001) == 0              # no re-visit
    amfi_client.set_discovered_successors({})


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))


# ── Co-existence gate: correlation alone must not accept plan variants ───────

def _coex_series(start: date, end: date, step_days: int = 1, base: float = 100.0):
    """Deterministic NAV series; both variants of a scheme share the shape."""
    out, d, nav, i = [], start, base, 0
    while d <= end:
        nav *= 1.0 + ((i % 7) - 3) * 0.002
        out.append({"date": d, "nav": round(nav, 4)})
        d += timedelta(days=step_days)
        i += 1
    return out


def test_concurrent_plan_variants_are_rejected_despite_perfect_correlation():
    """Regression for a real false positive: 146088 (DIRECT) -> 146087 (REGULAR),
    correlation 1.000, concurrent for 100% of the predecessor's life."""
    a = _coex_series(date(2015, 1, 1), date(2020, 1, 1))
    b = _coex_series(date(2015, 1, 1), date(2020, 1, 1), base=120.0)  # same shape
    accepted, signals = fl.verify_stitch(a, b, curated=False)
    assert accepted is False
    assert signals["correlation"] is not None and signals["correlation"] > 0.99
    assert "not a succession" in signals["reason"] or "variants" in signals["reason"]


def test_successor_that_does_not_outlive_predecessor_is_rejected():
    a = _coex_series(date(2015, 1, 1), date(2020, 1, 1))
    b = _coex_series(date(2015, 1, 1), date(2020, 1, 1), base=90.0)
    accepted, signals = fl.verify_stitch(a, b, curated=False)
    assert accepted is False
    assert signals["successor_extension_days"] == 0


def test_genuine_handover_still_accepted():
    """A real succession: predecessor stops, successor carries on, brief overlap."""
    a = _coex_series(date(2008, 1, 1), date(2014, 6, 30))
    b = _coex_series(date(2014, 4, 1), date(2026, 1, 1), base=21.0)
    accepted, signals = fl.verify_stitch(a, b, curated=False)
    assert signals["concurrent_fraction"] <= 0.75
    assert signals["successor_extension_days"] > 180
    assert accepted is True, signals


def test_curated_no_overlap_handover_still_accepted():
    """All three production CURATED pairs have this shape: zero overlap, small
    gap, successor outliving by years."""
    a = _coex_series(date(2008, 1, 1), date(2014, 6, 26))
    b = _coex_series(date(2014, 6, 30), date(2026, 1, 1), base=21.121)
    accepted, signals = fl.verify_stitch(a, b, curated=True)
    assert accepted is True, signals
    assert signals["concurrent_days"] == 0


# ── Real-world scenario matrix for the co-existence gate ────────────────────
#
# IMPORTANT distinction this matrix encodes:
#
#   Stitching only applies when a fund's history is split across DIFFERENT
#   scheme codes. A rename, an AMC rebrand or a SEBI recategorisation happens
#   on the SAME code — mfapi already serves continuous NAV for it, so there is
#   nothing to stitch and verify_stitch is never called. Those events are
#   recorded by lineage_store as NAME_CHANGE / AMC_CHANGE / RECATEGORISATION.
#
#   Asserting "a rename must stitch" would therefore be asserting the wrong
#   thing. What must hold is: same-code events need no stitch, and cross-code
#   events (true merger, AMC acquisition that reissued the code) must stitch.

def _pair(pred_start, pred_end, succ_start, succ_end, base_a=50.0, base_b=50.0):
    a = _coex_series(pred_start, pred_end, base=base_a)
    b = _coex_series(succ_start, succ_end, base=base_b)
    return a, b


def test_scenario_regular_vs_direct_plan_must_never_stitch():
    """Same scheme, two plans. Identical portfolio, both alive 2013->today.
    Real example: 146088 (DIRECT) / 146087 (REGULAR), correlation 1.000."""
    a, b = _pair(date(2013, 1, 1), date(2026, 1, 1), date(2013, 1, 1), date(2026, 1, 1))
    accepted, sig = fl.verify_stitch(a, b, curated=False)
    assert accepted is False, sig
    assert sig["concurrent_fraction"] > 0.75


def test_scenario_growth_vs_idcw_option_must_never_stitch():
    """Real example: 100043 (Growth) / 100041 (IDCW) of one liquid fund."""
    a, b = _pair(date(2010, 1, 1), date(2026, 1, 1), date(2010, 1, 1), date(2026, 1, 1),
                 base_a=100.0, base_b=15.0)
    accepted, sig = fl.verify_stitch(a, b, curated=False)
    assert accepted is False, sig


def test_scenario_genuine_merger_across_codes_must_stitch():
    """Predecessor wound up, unit-holders moved to a different code.
    Shape of 108197 (Morgan Stanley A.C.E.) -> 130502 (HDFC Small Cap)."""
    a, b = _pair(date(2008, 1, 1), date(2014, 6, 26), date(2014, 6, 30), date(2026, 1, 1),
                 base_a=20.697, base_b=21.121)
    accepted, sig = fl.verify_stitch(a, b, curated=True)
    assert accepted is True, sig
    assert sig["concurrent_days"] == 0
    assert sig["successor_extension_days"] > 180


def test_scenario_amc_acquisition_with_new_code_must_stitch():
    """Whole AMC acquired and the fund reissued under a new code.
    Shape of 107301 (JPMorgan) -> 140225 (Edelweiss), 4-day gap."""
    a, b = _pair(date(2007, 12, 26), date(2016, 11, 24), date(2016, 11, 28), date(2026, 1, 1),
                 base_a=19.832, base_b=20.270)
    accepted, sig = fl.verify_stitch(a, b, curated=True)
    assert accepted is True, sig


def test_scenario_wrong_pair_with_plausible_dates_still_rejected():
    """Handover-shaped dates but uncorrelated returns: a bad map entry must not
    be able to splice two unrelated funds together."""
    a = _coex_series(date(2008, 1, 1), date(2014, 6, 26), base=20.0)
    b = [{"date": r["date"], "nav": round(50 + (i % 11) * 3.1, 4)}
         for i, r in enumerate(_coex_series(date(2014, 3, 1), date(2026, 1, 1), base=50.0))]
    accepted, sig = fl.verify_stitch(a, b, curated=False)
    assert accepted is False, sig


def _same_code_event(kind, old, new, when):
    """Same-code events are lineage records, not stitches."""
    import lineage_store as _ls
    recs = _ls.derive_lineage({when[0]: [_obs(1, old)], when[1]: [_obs(1, new)]})
    return [a["type"] for a in recs[1].corporate_actions]


def _obs(code, name):
    from amfi_history import SchemeObservation
    return SchemeObservation(scheme_code=code, scheme_name=name, isin_growth="",
                             isin_reinvest="", nav_date=None)


def test_scenario_scheme_rename_same_code_needs_no_stitch():
    kinds = _same_code_event(
        "rename", "HDFC Equity Fund - Growth", "HDFC Flexi Cap Fund - Growth",
        ("2021-01-01", "2022-01-01"))
    import lineage_store as _ls
    assert _ls.Action.NAME_CHANGE in kinds


def test_scenario_amc_rebranding_same_code_recorded_not_stitched():
    """Reliance -> Nippon India kept the scheme code; history is already
    continuous, so this is a recorded event, never a stitch."""
    kinds = _same_code_event(
        "rebrand", "Reliance Small Cap Fund - Growth",
        "Nippon India Small Cap Fund - Growth", ("2017-10-25", "2019-11-17"))
    import lineage_store as _ls
    assert _ls.Action.NAME_CHANGE in kinds


def test_scenario_sebi_recategorisation_recorded_as_its_own_type():
    kinds = _same_code_event(
        "recat", "ICICI Prudential Focused Bluechip Equity Fund",
        "ICICI Prudential Bluechip Fund - Large Cap", ("2017-11-01", "2018-06-01"))
    import lineage_store as _ls
    assert _ls.Action.RECATEGORISATION in kinds


def test_scenario_cosmetic_relabel_is_not_a_corporate_action():
    kinds = _same_code_event(
        "cosmetic", "Nippon India Fixed Horizon Fund XLI - Series 12",
        "NIPPON INDIA FIXED HORIZON FUND - XLI - SERIES 12",
        ("2022-01-01", "2023-01-01"))
    import lineage_store as _ls
    assert kinds == [_ls.Action.NAME_COSMETIC]


# ── ISIN-verified no-overlap stitching ───────────────────────────────────────
# An AMC acquisition leaves NO overlap: the old code stops, the new one starts
# days later. Correlation is undefined, so ISIN continuity + seam continuity is
# the admissible evidence. Regression for the run where 343 such candidates all
# scored 0/343 because only the correlation path existed.

def test_isin_verified_no_overlap_pair_is_accepted():
    a = _coex_series(date(2008, 1, 1), date(2014, 6, 26), base=20.697)
    b = _coex_series(date(2014, 6, 30), date(2026, 1, 1), base=21.121)
    accepted, sig = fl.verify_stitch(a, b, curated=False, isin_verified=True)
    assert accepted is True, sig
    assert sig["overlap_days"] == 0
    assert sig["seam_delta"] <= fl.SEAM_MAX_DELTA


def test_same_pair_without_isin_evidence_is_rejected():
    """Identical NAV shape; only the provenance flag differs."""
    a = _coex_series(date(2008, 1, 1), date(2014, 6, 26), base=20.697)
    b = _coex_series(date(2014, 6, 30), date(2026, 1, 1), base=21.121)
    accepted, sig = fl.verify_stitch(a, b, curated=False, isin_verified=False)
    assert accepted is False
    assert "not an acceptable curated/ISIN-verified gap" in sig["reason"]


def test_isin_verified_pair_with_a_nav_cliff_is_still_rejected():
    """ISIN evidence does not bypass the NAV check — a mis-mapped ISIN must not
    be able to splice two unrelated funds together."""
    a = _coex_series(date(2008, 1, 1), date(2014, 6, 26), base=20.0)
    b = _coex_series(date(2014, 6, 30), date(2026, 1, 1), base=140.0)  # 7x jump
    accepted, sig = fl.verify_stitch(a, b, curated=False, isin_verified=True)
    assert accepted is False
    assert "seam failed" in sig["reason"]


def test_isin_verified_pair_with_a_huge_date_gap_is_rejected():
    a = _coex_series(date(2008, 1, 1), date(2012, 1, 1), base=20.0)
    b = _coex_series(date(2018, 1, 1), date(2026, 1, 1), base=20.5)
    accepted, sig = fl.verify_stitch(a, b, curated=False, isin_verified=True)
    assert accepted is False
    assert sig["gap_days"] > fl.MAX_GAP_DAYS_NO_OVERLAP


def test_isin_verified_does_not_weaken_the_coexistence_gate():
    """Plan/option twins share an ISIN family and coexist. Even flagged as
    ISIN-verified they must never stitch."""
    a = _coex_series(date(2013, 1, 1), date(2026, 1, 1), base=100.0)
    b = _coex_series(date(2013, 1, 1), date(2026, 1, 1), base=120.0)
    accepted, sig = fl.verify_stitch(a, b, curated=False, isin_verified=True)
    assert accepted is False, sig
