"""Bulk Fund Analysis tests — Remarks boundaries, header/column detection,
fund matching, the 10Y-with-fallback horizon picker, row writing, and a full
process_bulk_job dry run. Pure-logic tests: lineage/amfi lookups and the
Mongo job collection are faked/monkeypatched — no network, no real Mongo,
matching this repo's existing async-test convention (asyncio.run, not
pytest-asyncio — see test_fund_lineage.py).
"""

import asyncio
import io
import os
import sys

import pytest
from openpyxl import Workbook, load_workbook

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bulk_analysis as ba  # noqa: E402


# ─── get_fund_remark boundaries (exact cases from the spec) ─────────────────

@pytest.mark.parametrize("fqs,expected", [
    (74.99, "Bad — Should Discontinue"),
    (75.00, "Not Bad, but better options available"),
    (84.99, "Not Bad, but better options available"),
    (85.00, "Good — Should Continue"),
    (100, "Good — Should Continue"),
    (None, "Calculation unavailable"),
])
def test_get_fund_remark_boundaries(fqs, expected):
    assert ba.get_fund_remark(fqs) == expected


# ─── Header / identifier-column detection ────────────────────────────────────

def test_find_identifier_columns_various_headers():
    headers = ["S.No", "Fund Name", "AMC", "ISIN"]
    cols = ba._find_identifier_columns(headers)
    assert cols["name"] == 1
    assert cols["isin"] == 3
    assert cols["code"] is None


def test_find_identifier_columns_missing_name_column():
    headers = ["AMC", "Category", "AUM"]
    cols = ba._find_identifier_columns(headers)
    assert cols == {"name": None, "code": None, "isin": None}


def test_select_worksheet_prefers_active_sheet_with_identifier():
    wb = Workbook()
    summary = wb.active
    summary.title = "Summary"
    summary.append(["Report", None])
    holdings = wb.create_sheet("Holdings")
    holdings.append(["Fund Name", "ISIN"])
    holdings.append(["Fund A", "INF000A01AAA"])
    picked = ba._select_worksheet(wb)
    assert picked.title == "Holdings"


# ─── Fund matching ────────────────────────────────────────────────────────────

def test_match_fund_exact_code():
    by_code = {100001: "Alpha Fund - Growth"}
    result = asyncio.run(ba.match_fund(None, by_code, {}, code_val=100001))
    assert result == 100001


def test_match_fund_exact_name_case_and_whitespace():
    name = "Beta Equity Fund - Growth"
    by_norm_name = {ba.lineage_store.normalise_name(name): [200002]}
    result = asyncio.run(
        ba.match_fund(None, {}, by_norm_name, name_val="  beta   EQUITY fund - growth  ")
    )
    assert result == 200002


def test_match_fund_unmatched_returns_none(monkeypatch):
    async def fake_resolve(db, query, limit=25):
        return []
    monkeypatch.setattr(ba.lineage_store, "resolve", fake_resolve)
    result = asyncio.run(ba.match_fund(None, {}, {}, name_val="Totally Unknown Fund"))
    assert result is None


def test_match_fund_ambiguous_name_returns_none(monkeypatch):
    """Two candidates that are NOT variants of one scheme stay unresolved.
    `by_code` is empty, so resolve_variants can't rank them and must refuse."""
    async def no_lineage(db, query, limit=25):
        return []
    monkeypatch.setattr(ba.lineage_store, "resolve", no_lineage)
    by_norm_name = {ba.lineage_store.normalise_name("Ambiguous Fund"): [1, 2]}
    result = asyncio.run(ba.match_fund(None, {}, by_norm_name, name_val="Ambiguous Fund"))
    assert result is None  # never guess between two candidates (Rule 7)


def test_match_fund_isin(monkeypatch):
    async def fake_resolve(db, query, limit=25):
        return [777] if query == "INF000A01AAA" else []
    monkeypatch.setattr(ba.lineage_store, "resolve", fake_resolve)
    result = asyncio.run(ba.match_fund(None, {}, {}, isin_val="INF000A01AAA"))
    assert result == 777


def test_match_fund_code_falls_back_to_lineage_for_historical_code(monkeypatch):
    async def fake_resolve(db, query, limit=5):
        return [999] if query == "555" else []
    monkeypatch.setattr(ba.lineage_store, "resolve", fake_resolve)
    # 555 isn't in the current master list (by_code) — must resolve via lineage.
    result = asyncio.run(ba.match_fund(None, {123: "Some Fund"}, {}, code_val=555))
    assert result == 999


def test_match_fund_falls_back_to_name_stem_when_lineage_index_empty(monkeypatch):
    # Reproduces the real production bug: AMFI's own name carries plan/option
    # wording ("- GROWTH OPTION - Regular Plan") that an advisor's sheet
    # omits, so normalised-name matching misses even though the fund is
    # live and current. lineage_store.resolve returning [] mirrors a prod DB
    # where fund_identifier_idx was never populated (build_lineage.py
    # --persist is a manual, un-automated script) — this tier must not
    # depend on it.
    async def fake_resolve(db, query, limit=25):
        return []
    monkeypatch.setattr(ba.lineage_store, "resolve", fake_resolve)

    amfi_name = "quant Multi Cap Fund-GROWTH OPTION - Regular Plan"
    by_norm_name = {ba.lineage_store.normalise_name(amfi_name): [100631]}
    by_stem = {ba.lineage_store.name_stem(amfi_name): [100631]}

    result = asyncio.run(
        ba.match_fund(None, {}, by_norm_name, by_stem, name_val="Quant Multi Cap Fund Growth")
    )
    assert result == 100631


def test_match_fund_ambiguous_stem_returns_none(monkeypatch):
    async def fake_resolve(db, query, limit=25):
        return []
    monkeypatch.setattr(ba.lineage_store, "resolve", fake_resolve)

    by_norm_name = {}  # exact-normalised match deliberately misses
    by_stem = {ba.lineage_store.name_stem("Ambiguous Fund Growth"): [1, 2]}

    result = asyncio.run(
        ba.match_fund(None, {}, by_norm_name, by_stem, name_val="Ambiguous Fund - Growth Option")
    )
    assert result is None  # two stem hits — never guess (Rule 7)


# ─── Horizon picker (10Y primary, longest-available fallback) ───────────────

def test_pick_horizon_prefers_10y():
    m = {"5": {"fqs": 50}, "10": {"fqs": 90}}
    picked = ba._pick_horizon_from_map(m)
    assert picked == (m["10"], 10)


def test_pick_horizon_falls_back_to_longest_below_10():
    # Mirrors the advisor's own reference sheet: a fund with only 6Y of
    # history gets scored at 6Y, not left blank.
    m = {"3": {"fqs": 41.0}, "4": {"fqs": 70.7}, "6": {"fqs": 85.5}}
    picked = ba._pick_horizon_from_map(m)
    assert picked[1] == 6
    assert picked[0]["fqs"] == 85.5


def test_pick_horizon_none_when_nothing_usable():
    assert ba._pick_horizon_from_map({}) is None
    assert ba._pick_horizon_from_map({"5": {"fqs": None}}) is None


def test_get_fqs_result_uses_nav_meta_cache_first():
    doc = {"fqs_by_horizon": {"10": {"fqs": 88.0, "median_wm": 2.0, "p10_wm": 1.7, "success_pct": 0.9}}}
    db = _FakeDB(doc)

    async def cached_fn(code):
        raise AssertionError("should not reach the per-fund cache")

    async def build_fn(code, **kw):
        raise AssertionError("should not reach live compute")

    picked = asyncio.run(ba.get_fqs_result(db, 1, build_fn, cached_fn))
    assert picked[1] == 10
    assert picked[0]["fqs"] == 88.0


def test_get_fqs_result_falls_back_to_live_compute():
    db = _FakeDB(None)

    async def cached_fn(code):
        return None

    async def build_fn(code, **kw):
        return {"horizons": [{"horizon": 6, "fqs": 85.5, "median_wm": 2.1, "p10_wm": 1.8, "success_pct": 0.95}]}

    picked = asyncio.run(ba.get_fqs_result(db, 1, build_fn, cached_fn))
    assert picked[1] == 6
    assert picked[0]["fqs"] == 85.5


def test_get_fqs_result_none_when_live_compute_raises():
    db = _FakeDB(None)

    async def cached_fn(code):
        return None

    async def build_fn(code, **kw):
        raise RuntimeError("Scheme not found on AMFI")

    picked = asyncio.run(ba.get_fqs_result(db, 1, build_fn, cached_fn))
    assert picked is None


class _FakeNavMetaColl:
    def __init__(self, doc):
        self.doc = doc

    async def find_one(self, *a, **kw):
        return self.doc


class _FakeDB:
    def __init__(self, fund_nav_meta_doc=None):
        self.fund_nav_meta = _FakeNavMetaColl(fund_nav_meta_doc)


# ─── Row writing ──────────────────────────────────────────────────────────────

class _FakeJobsColl:
    def __init__(self):
        self.doc = {}

    async def update_one(self, _filt, update, **kw):
        for k, v in update.get("$set", {}).items():
            self.doc[k] = v
        for k in update.get("$unset", {}):
            self.doc.pop(k, None)


class _FakeJobDB:
    """Just enough of a Mongo-like `db` for process_bulk_job: attribute
    access for fund_nav_meta, subscript access for the jobs collection."""

    def __init__(self, nav_meta_by_code):
        self.fund_nav_meta = _NavMetaByCode(nav_meta_by_code)
        self._jobs = _FakeJobsColl()

    def __getitem__(self, _name):
        return self._jobs


class _NavMetaByCode:
    def __init__(self, by_code):
        self.by_code = by_code

    async def find_one(self, filt, *a, **kw):
        return self.by_code.get(filt.get("scheme_code"))


# ─── Allocation parsing ──────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (0.25, 0.25),          # already a fraction (how Excel stores 25%)
    (25, 0.25),            # a bare percentage number
    ("25%", 0.25),         # text with a percent sign
    ("25 %", 0.25),
    (" 0.4 ", 0.4),
    ("1,250", 12.5),       # thousands separator, still read as a percentage
    (1, 1.0),              # 100% in a single-fund portfolio
    (100, 1.0),
    (None, None),
    ("", None),
    ("n/a", None),         # never guessed
])
def test_parse_allocation(raw, expected):
    assert ba.parse_allocation(raw) == expected


# ─── Column planning — preserve, don't duplicate, order ──────────────────────
#
# These pin the three rules the download's layout exists to satisfy (see
# ba.APP_COLUMNS): nothing of the advisor's is lost, a column of theirs that
# means the same as one of ours is used rather than duplicated, and our
# columns come first in the specified order.

def _labels(plan):
    return [c["name"] for c in plan["order"]]


def test_plan_columns_puts_app_columns_first_in_the_specified_order():
    plan = ba.plan_columns(["Fund Name", "Client Name", "Investment Amount", "Allocation"])
    assert _labels(plan)[:13] == [
        "Fund Name", "Category", "Benchmark XIRR", "Allocation", "No of Windows",
        "FQS", "Success%", "Median WM", "XIRR (Med)", "P10 WM", "P10 XIRR",
        "CV (Stability)", "Remarks",
    ]
    assert [c["app"] for c in plan["order"][:13]] == ba.APP_COLUMNS


def test_plan_columns_preserves_every_user_column():
    header = ["Fund Name", "Client Name", "Investment Amount", "Allocation", "Folio No"]
    plan = ba.plan_columns(header)
    labels = _labels(plan)
    for original in header:
        assert original in labels, original
    # The ones that aren't ours keep their original relative order, after ours.
    extras = [c["name"] for c in plan["order"] if c["app"] is None]
    assert extras == ["Client Name", "Investment Amount", "Folio No"]


def test_plan_columns_binds_matching_user_columns_instead_of_duplicating():
    """The example from the brief: they have Fund Name and Allocation, so those
    must be USED, not shadowed by a second column of the same meaning."""
    plan = ba.plan_columns(["Fund Name", "Client Name", "Investment Amount", "Allocation"])
    by_app = {c["app"]: c for c in plan["order"] if c["app"]}
    assert by_app["Fund Name"]["source"] == 0
    assert by_app["Allocation"]["source"] == 3
    assert by_app["FQS"]["source"] is None          # they don't have it -> we add it
    assert _labels(plan).count("Fund Name") == 1
    assert _labels(plan).count("Allocation") == 1


def test_plan_columns_recognises_real_world_header_wording():
    """A real advisor sheet, and a file that has already been through this
    tool. Neither uses our canonical wording, and neither may be duplicated."""
    header = ["SCHEME NAME", "FQS BASED ON 10 YR ROLLING", "MEDIAN XIRR",
              "Success %", "Weightage", "REMARKS"]
    plan = ba.plan_columns(header)
    by_app = {c["app"]: c for c in plan["order"] if c["app"]}
    assert by_app["Fund Name"]["source"] == 0
    assert by_app["FQS"]["source"] == 1
    assert by_app["XIRR (Med)"]["source"] == 2
    assert by_app["Success%"]["source"] == 3
    assert by_app["Allocation"]["source"] == 4
    assert by_app["Remarks"]["source"] == 5
    # Nothing was appended a second time.
    assert len(plan["order"]) == len(ba.APP_COLUMNS)


def test_plan_columns_keeps_the_advisors_own_header_text():
    """The brief says not to rename their columns — a bound column keeps their
    wording and is simply filled in."""
    plan = ba.plan_columns(["FQS BASED ON 10 YR ROLLING"])
    by_app = {c["app"]: c for c in plan["order"] if c["app"]}
    assert by_app["FQS"]["name"] == "FQS BASED ON 10 YR ROLLING"
    assert by_app["Median WM"]["name"] == "Median WM"   # one we created


def test_plan_columns_does_not_confuse_similar_headers():
    """Headers that share words must not claim each other's column."""
    plan = ba.plan_columns(["Fund Name", "Median XIRR", "Median WM",
                            "P10 XIRR", "P10 WM", "Benchmark XIRR", "Client Name"])
    by_app = {c["app"]: c for c in plan["order"] if c["app"]}
    assert by_app["XIRR (Med)"]["source"] == 1
    assert by_app["Median WM"]["source"] == 2
    assert by_app["P10 XIRR"]["source"] == 3
    assert by_app["P10 WM"]["source"] == 4
    assert by_app["Benchmark XIRR"]["source"] == 5
    extras = [c["name"] for c in plan["order"] if c["app"] is None]
    assert extras == ["Client Name"]
    # "Client Name" must never be mistaken for the fund name.
    assert by_app["Fund Name"]["source"] == 0


def test_plan_columns_ignores_unlabelled_columns():
    plan = ba.plan_columns(["Fund Name", None, "", "Folio"])
    assert [c["name"] for c in plan["order"] if c["app"] is None] == ["Folio"]


# ─── The preserve / fill / update rule ───────────────────────────────────────

def _cell_for(app_column, theirs, app_value):
    plan_col = {"name": app_column, "app": app_column, "source": 0 if theirs is not _ABSENT else None}
    row = [theirs] if theirs is not _ABSENT else []
    return ba._resolve_cell(plan_col, row, {app_column: app_value})


class _Absent:
    pass


_ABSENT = _Absent()


def test_user_owned_column_keeps_the_advisors_value():
    """Fund Name / Category / Allocation describe the advisor's client, not
    the fund — the app must never restate them."""
    assert _cell_for("Fund Name", "PPFAS Flexi Cap", "Parag Parikh Flexi Cap Fund - Growth") == "PPFAS Flexi Cap"
    assert _cell_for("Category", "Equity - Flexi", "Flexi Cap") == "Equity - Flexi"


def test_user_owned_column_is_filled_only_where_blank():
    assert _cell_for("Fund Name", None, "Parag Parikh Flexi Cap Fund") == "Parag Parikh Flexi Cap Fund"
    assert _cell_for("Category", "", "Flexi Cap") == "Flexi Cap"


def test_allocation_text_is_normalised_so_the_percent_format_renders():
    assert _cell_for("Allocation", "25%", None) == 0.25
    assert _cell_for("Allocation", 25, None) == 0.25
    # Unparseable text is kept verbatim rather than discarded.
    assert _cell_for("Allocation", "see note", None) == "see note"


def test_app_owned_column_is_always_updated():
    """A re-uploaded file must get fresh numbers, not last month's."""
    assert _cell_for("FQS", 61.0, 88.4) == 88.4


def test_user_only_column_is_passed_straight_through():
    col = {"name": "Folio No", "app": None, "source": 0}
    assert ba._resolve_cell(col, ["F-1001"], {}) == "F-1001"


# ─── End to end ──────────────────────────────────────────────────────────────

NAV_META = {
    111: {"scheme_name": "Alpha Equity Fund - Growth", "category": "Flexi Cap",
          "fqs_by_horizon": {"10": {"fqs": 88.0, "median_wm": 2.2, "p10_wm": 1.8,
                                    "success_pct": 0.9, "n_windows": 74, "cv": 0.161}}},
}


def _run(monkeypatch, header, rows, *, job_id="job", nav_meta=None, build=None):
    async def fake_fetch_fund_list():
        return [{"schemeCode": 111, "schemeName": "Alpha Equity Fund - Growth"},
                {"schemeCode": 222, "schemeName": "Beta Equity Fund - Growth"}]
    monkeypatch.setattr(ba.amfi_client, "fetch_fund_list", fake_fetch_fund_list)

    async def fake_resolve(db, query, limit=25):
        return []
    monkeypatch.setattr(ba.lineage_store, "resolve", fake_resolve)

    db = _FakeJobDB(NAV_META if nav_meta is None else nav_meta)

    async def cached_fn(code):
        return None

    async def default_build(code, **kw):
        raise AssertionError(f"unexpected live compute for {code}")

    wb = Workbook()
    ws = wb.active
    ws.title = "Holdings"
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)

    asyncio.run(ba.process_bulk_job(job_id, db, buf.getvalue(), build or default_build, cached_fn))
    doc = db._jobs.doc
    out = load_workbook(io.BytesIO(doc["output_bytes"]))
    return doc, out


def _sheet_header(ws):
    """Header row of the output — row 4, under the branded title block."""
    return [c.value for c in next(ws.iter_rows(min_row=4, max_row=4))]


def _sheet_row(ws, n):
    return [c.value for c in next(ws.iter_rows(min_row=4 + n, max_row=4 + n))]


def test_output_has_one_sheet_carrying_everything(monkeypatch):
    doc, out = _run(
        monkeypatch,
        ["Fund Name", "Client Name", "Investment Amount", "Allocation"],
        [["Alpha Equity Fund - Growth", "R. Kumar", 250000, "40%"]],
        job_id="e2e1",
    )
    assert doc["status"] == "completed"
    assert out.sheetnames == [ba.REVIEW_SHEET_TITLE]

    header = _sheet_header(out.active)
    assert header[:13] == [
        "Fund Name", "Category", "Benchmark XIRR", "Allocation", "No of Windows",
        "FQS", "Success%", "Median WM", "XIRR (Med)", "P10 WM", "P10 XIRR",
        "CV (Stability)", "Remarks",
    ]
    # Their own columns survived, after ours, in their original order.
    assert header[13:] == ["Client Name", "Investment Amount"]

    row = dict(zip(header, _sheet_row(out.active, 1)))
    assert row["Client Name"] == "R. Kumar"          # untouched
    assert row["Investment Amount"] == 250000        # untouched
    assert row["Fund Name"] == "Alpha Equity Fund - Growth"
    assert row["Allocation"] == 0.4                  # their 40%, as a fraction
    assert row["FQS"] == 88.0                        # ours
    assert row["Category"] == "Flexi Cap"            # ours: they had no column
    assert row["Remarks"] == "Good — Should Continue"


def test_output_fills_an_existing_column_rather_than_duplicating_it(monkeypatch):
    _, out = _run(
        monkeypatch,
        ["Scheme Name", "FQS BASED ON 10 YR ROLLING", "Folio"],
        [["Alpha Equity Fund - Growth", None, "F-1"]],
        job_id="e2e2",
    )
    header = _sheet_header(out.active)
    assert header.count("FQS BASED ON 10 YR ROLLING") == 1
    assert "FQS" not in header                       # no second column added
    row = dict(zip(header, _sheet_row(out.active, 1)))
    assert row["FQS BASED ON 10 YR ROLLING"] == 88.0
    assert row["Folio"] == "F-1"


def test_output_never_overwrites_the_advisors_own_fund_name(monkeypatch):
    _, out = _run(
        monkeypatch,
        ["Fund Name"],
        [["Alpha Equity Fund"]],          # their shorthand, not AMFI's wording
        job_id="e2e3",
    )
    row = dict(zip(_sheet_header(out.active), _sheet_row(out.active, 1)))
    assert row["Fund Name"] == "Alpha Equity Fund"


def test_unmatched_row_is_preserved_and_labelled(monkeypatch):
    doc, out = _run(
        monkeypatch,
        ["Fund Name", "Folio"],
        [["Totally Unknown Fund", "F-9"]],
        job_id="e2e4",
    )
    assert doc["not_found"] == 1
    assert doc["not_found_funds"][0]["row"] == 2      # Excel's own row number
    row = dict(zip(_sheet_header(out.active), _sheet_row(out.active, 1)))
    assert row["Fund Name"] == "Totally Unknown Fund"
    assert row["Folio"] == "F-9"                     # their data still there
    assert row["Remarks"] == "Fund not found"
    assert row["FQS"] is None                        # nothing invented


def test_short_history_fund_annotates_its_horizon(monkeypatch):
    async def build(code, **kw):
        return {"scheme_name": "Beta Equity Fund - Growth", "category": "Small Cap",
                "horizons": [{"horizon": 6, "fqs": 70.7, "median_wm": 1.9,
                              "p10_wm": 1.5, "success_pct": 0.7, "n_windows": 20, "cv": 0.2}]}
    _, out = _run(
        monkeypatch,
        ["Fund Name"],
        [["Beta Equity Fund - Growth"]],
        job_id="e2e5", nav_meta={}, build=build,
    )
    row = dict(zip(_sheet_header(out.active), _sheet_row(out.active, 1)))
    assert row["FQS"] == "70.7 (6 YR)"
    assert row["Remarks"] == "Bad — Should Discontinue"


def test_one_calc_error_does_not_stop_the_batch(monkeypatch):
    async def build(code, **kw):
        raise RuntimeError("boom")
    doc, out = _run(
        monkeypatch,
        ["Fund Name"],
        [["Alpha Equity Fund - Growth"]],
        job_id="e2e6", nav_meta={}, build=build,
    )
    assert doc["status"] == "completed"
    assert doc["errors"] == 1
    row = dict(zip(_sheet_header(out.active), _sheet_row(out.active, 1)))
    assert row["Remarks"] == "Calculation unavailable"


def test_get_fund_result_passes_allow_direct_to_build_analysis_fn():
    """Portfolio Review must opt in to Direct-plan scoring on every call —
    this is the one piece of wiring the whole fix depends on. Regression-
    guards against the kwarg silently being dropped in a future edit, which
    would reintroduce the "real holding comes back blank" bug with no
    visible error (build_analysis_fn would just reject it again)."""
    db = _FakeDB(None)
    seen_kwargs = {}

    async def cached_fn(code):
        return None

    async def build_fn(code, **kw):
        seen_kwargs.update(kw)
        return {"horizons": [{"horizon": 10, "fqs": 80.0, "median_wm": 2.0,
                               "p10_wm": 1.7, "success_pct": 0.9}]}

    asyncio.run(ba.get_fqs_result(db, 1, build_fn, cached_fn))
    assert seen_kwargs.get("allow_direct") is True


def test_direct_plan_holding_is_scored_like_any_other_fund(monkeypatch):
    """The actual product requirement: a client's real Direct-plan holding,
    correctly identified (e.g. by ISIN), gets its real FQS — not a blank
    cell — because this screen reviews EXISTING holdings rather than
    recommending new ones. Mirrors _build_analysis honouring allow_direct=True
    for a scheme whose name contains "direct"."""
    async def build(code, **kw):
        assert kw.get("allow_direct") is True
        return {"scheme_name": "Alpha Equity Fund - Direct Plan - Growth", "category": "Flexi Cap",
                "horizons": [{"horizon": 10, "fqs": 91.2, "median_wm": 2.3,
                              "p10_wm": 1.9, "success_pct": 0.95, "n_windows": 80, "cv": 0.12}]}
    doc, out = _run(
        monkeypatch,
        ["Fund Name", "ISIN"],
        [["Alpha Equity Fund - Direct Plan - Growth", "INF000A01111"]],
        job_id="e2e6c", nav_meta={}, build=build,
    )
    assert doc["status"] == "completed"
    assert doc["matched"] == 1
    assert doc["errors"] == 0
    row = dict(zip(_sheet_header(out.active), _sheet_row(out.active, 1)))
    assert row["FQS"] == 91.2
    assert row["Remarks"] == "Good — Should Continue"


def test_direct_plan_row_gets_a_specific_remark_when_still_rejected(monkeypatch):
    """Fallback path: if build_analysis_fn rejects a Direct-plan scheme
    anyway (e.g. a caller wired without allow_direct support, or some other
    guard), the row must not read the same as a genuine data/matching
    failure — see REMARK_DIRECT_PLAN's docstring for why that would mislead
    an advisor who uploaded a real, identifiable holding.
    """
    from fastapi import HTTPException

    async def build(code, **kw):
        raise HTTPException(
            status_code=400,
            detail="Direct plans are not supported. Please use the Regular plan.",
        )
    doc, out = _run(
        monkeypatch,
        ["Fund Name", "ISIN"],
        [["Alpha Equity Fund - Direct Plan - Growth", "INF000A01111"]],
        job_id="e2e6b", nav_meta={}, build=build,
    )
    assert doc["status"] == "completed"
    assert doc["errors"] == 1          # still unscored — counted, not hidden
    assert doc["not_found"] == 0       # the fund WAS identified, just excluded
    row = dict(zip(_sheet_header(out.active), _sheet_row(out.active, 1)))
    assert row["Remarks"] == ba.REMARK_DIRECT_PLAN
    assert row["Remarks"] != "Calculation unavailable"
    assert row["FQS"] is None          # never invented from the wrong scheme


def test_too_new_fund_gets_a_specific_remark_not_a_generic_error(monkeypatch):
    """A fund that computed cleanly (real NAV fetched, no exception) but has
    zero rolling windows — e.g. launched 6 weeks ago, nowhere near the 13
    months a 1Y window needs — is a genuine data limitation, not a matching
    failure. Mirrors a real case: Invesco India Pharma and Healthcare Fund
    Regular Growth, 27 daily / 2 monthly NAV points since launch 2026-09-10.
    """
    async def build(code, **kw):
        return {"scheme_name": "Beta Equity Fund - Growth", "category": "Flexi Cap",
                "horizons": [], "monthly_points": 2, "first_month": "2026-09-30"}
    doc, out = _run(
        monkeypatch,
        ["Fund Name"],
        [["Beta Equity Fund - Growth"]],   # code 222 in _run's fake master list
        job_id="e2e6d", nav_meta={}, build=build,
    )
    assert doc["status"] == "completed"
    assert doc["errors"] == 1
    assert doc["not_found"] == 0
    row = dict(zip(_sheet_header(out.active), _sheet_row(out.active, 1)))
    assert row["Remarks"] == ba.remark_for_too_new(2, "2026-09-30")
    assert row["Remarks"] != "Calculation unavailable"
    assert row["Remarks"] != ba.REMARK_DIRECT_PLAN
    assert "2026-09-30" in row["Remarks"]
    assert row["FQS"] is None


def test_round_trip_reupload_refreshes_without_growing_the_sheet(monkeypatch):
    """Re-uploading our own download must update the numbers in place, not
    append a second set of analysis columns each time."""
    _, first = _run(
        monkeypatch,
        ["Fund Name", "Folio"],
        [["Alpha Equity Fund - Growth", "F-1"]],
        job_id="rt1",
    )
    buf = io.BytesIO()
    first.save(buf)

    async def fake_fetch_fund_list():
        return [{"schemeCode": 111, "schemeName": "Alpha Equity Fund - Growth"}]
    monkeypatch.setattr(ba.amfi_client, "fetch_fund_list", fake_fetch_fund_list)

    async def fake_resolve(db, query, limit=25):
        return []
    monkeypatch.setattr(ba.lineage_store, "resolve", fake_resolve)

    db = _FakeJobDB(NAV_META)

    async def cached_fn(code):
        return None

    async def build(code, **kw):
        raise AssertionError("should not recompute")

    # The download's own title block means row 1 is not the header row, so a
    # straight re-upload is not expected to parse — this asserts the column
    # SET is stable when the same logical sheet goes through twice.
    second_header = ba.plan_columns(_sheet_header(first.active))
    assert len(second_header["order"]) == len(ba.plan_columns(["Fund Name", "Folio"])["order"])


# ─── Branding / formatting ───────────────────────────────────────────────────

def test_output_is_branded_and_client_ready(monkeypatch):
    _, out = _run(
        monkeypatch,
        ["Fund Name", "Folio"],
        [["Alpha Equity Fund - Growth", "F-1"],
         ["Alpha Equity Fund - Growth", "F-2"]],
        job_id="brand1",
    )
    ws = out.active
    assert "ArthVruksh" in str(ws["A1"].value)
    assert ba.ARN_NUMBER in str(ws["A2"].value)          # compliance line present
    assert ws["A1"].fill.fgColor.rgb == ba.BRAND_NAVY
    assert ws["A4"].font.bold and ws["A4"].fill.fgColor.rgb == ba.BRAND_NAVY
    assert ws.freeze_panes == "B5"                        # title block + fund name
    assert ws.auto_filter.ref is not None
    assert ws.sheet_view.showGridLines is False
    assert ws.page_setup.orientation == "landscape"
    # Percentages/multiples render as such, not as raw decimals.
    header = _sheet_header(ws)
    fqs_col = header.index("FQS") + 1
    succ_col = header.index("Success%") + 1
    assert ws.cell(row=5, column=succ_col).number_format == "0.0%"
    assert ws.cell(row=5, column=fqs_col).number_format == "0.0"
    # Every data cell is bordered, and rows alternate.
    assert ws.cell(row=5, column=1).border.left.style == "thin"
    assert ws.cell(row=6, column=1).fill.fgColor.rgb == ba.BAND_FILL


@pytest.mark.parametrize("fqs,expected", [
    (61.0, "bad"), (80.0, "mid"), (92.0, "good"), ("70.7 (6 YR)", "bad"), (None, None),
])
def test_fqs_cells_are_colour_coded_on_the_remark_thresholds(fqs, expected):
    colour = ba._fqs_tier_colour(fqs)
    assert colour == (ba._TIER_COLOUR[expected] if expected else None)


# ─── Header detection — title rows above the table ───────────────────────────
#
# Row 1 is not assumed to be the header. Advisors' own exports open with a
# title block, and so does OUR OWN download — re-uploading last month's
# reviewed file to refresh its numbers has to work.

def test_find_header_row_skips_a_title_block():
    rows = [
        ["ArthVruksh — Portfolio Review"],
        ["ArthVruksh MFDGuru LLP · ARN: 365554"],
        [],
        ["Fund Name", "Allocation", "FQS"],
        ["Alpha Equity Fund - Growth", "40%", None],
    ]
    assert ba.find_header_row(rows) == 3


def test_find_header_row_returns_zero_when_there_is_no_identifier():
    assert ba.find_header_row([["AMC", "Category"], ["HDFC", "Debt"]]) == 0


def test_find_header_row_plain_sheet():
    assert ba.find_header_row([["Fund Name", "ISIN"], ["Alpha", "INF1"]]) == 0


def test_our_own_download_can_be_re_uploaded(monkeypatch):
    """The round trip that matters: process a file, feed the download straight
    back in, and get the same columns with refreshed numbers — not a parse
    error, and not a second set of analysis columns."""
    _, first = _run(
        monkeypatch,
        ["Fund Name", "Folio", "Client Name"],
        [["Alpha Equity Fund - Growth", "F-1", "R. Kumar"]],
        job_id="rt-a",
    )
    buf = io.BytesIO()
    first.save(buf)

    async def fake_fetch_fund_list():
        return [{"schemeCode": 111, "schemeName": "Alpha Equity Fund - Growth"}]
    monkeypatch.setattr(ba.amfi_client, "fetch_fund_list", fake_fetch_fund_list)

    async def fake_resolve(db, query, limit=25):
        return []
    monkeypatch.setattr(ba.lineage_store, "resolve", fake_resolve)

    # The preview step must accept it too — that is the first thing a
    # re-upload hits.
    preview = ba.parse_preview(buf.getvalue())
    assert preview["total"] == 1
    assert preview["preview_funds"] == ["Alpha Equity Fund - Growth"]

    db2 = _FakeJobDB(NAV_META)

    async def cached_fn(code):
        return None

    async def build(code, **kw):
        raise AssertionError("should not recompute")

    asyncio.run(ba.process_bulk_job("rt-b", db2, buf.getvalue(), build, cached_fn))
    doc2 = db2._jobs.doc
    assert doc2["status"] == "completed"
    assert doc2["matched"] == 1
    assert doc2["not_found"] == 0

    second = load_workbook(io.BytesIO(doc2["output_bytes"]))
    h1, h2 = _sheet_header(first.active), _sheet_header(second.active)
    assert h1 == h2                       # stable — no columns accumulated
    row = dict(zip(h2, _sheet_row(second.active, 1)))
    assert row["FQS"] == 88.0
    assert row["Folio"] == "F-1"          # their columns survived the round trip
    assert row["Client Name"] == "R. Kumar"


def test_preview_and_job_agree_on_the_row_count(monkeypatch):
    """A divergence here shows up as a progress bar that stops at 2/3."""
    header = ["Fund Name"]
    rows = [["Alpha Equity Fund - Growth"], [None], ["Beta Equity Fund - Growth"]]
    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)

    assert ba.parse_preview(buf.getvalue())["total"] == 2

    doc, _ = _run(monkeypatch, header, rows, job_id="count1")
    assert doc["total"] if "total" in doc else True
    assert doc["processed"] == 2


# ─── Name matching: the tiers must be independent ────────────────────────────
#
# Regression tests for four funds reported as "Fund not found" while being
# findable in the app (2026-09-01). Two distinct causes, both fixed:
#
#   1. The stem tier sat behind `if not lineage_hits`, so it only ran when the
#      lineage index found NOTHING. Once that index was built, an ambiguous
#      multi-hit result became normal and silently blocked the tier that would
#      have resolved the fund. Measured against a 7,482-scheme universe
#      snapshot: shorthand-name recall was 43.6% with a built index and 99.7%
#      without it — the index made matching WORSE.
#   2. "Fund of Fund" and "FOF" could never meet, because name_stem strips
#      "fund" as plan noise: "Gold and Silver ETF Fund of Fund" -> "...etf of"
#      vs AMFI's "Gold and Silver ETF FOF" -> "...etf fof".

_MASTER = {
    1: "ICICI Prudential Dividend Yield Equity Fund Growth Option",
    2: "Motilal Oswal Large and Midcap Fund - Regular Plan Growth",
    3: "Edelweiss Gold and Silver ETF FOF - Regular Plan - Growth",
    4: "Edelweiss Large Cap Fund - Regular Plan - Growth Option",
    5: "Edelweiss Large Cap Fund -Plan B - Growth option",
    6: "Edelweiss Large Cap Fund -Plan C - Growth option",
    7: "Edelweiss Large & Mid Cap Fund - Regular Plan - Growth Option",
    8: "Motilal Oswal Large Cap Regular Plan Growth",
}


def _master_indexes():
    by_norm, by_stem = {}, {}
    for code, name in _MASTER.items():
        by_norm.setdefault(ba.lineage_store.normalise_name(name), []).append(code)
        key = ba.match_key(name)
        if key:
            by_stem.setdefault(key, []).append(code)
    return _MASTER, by_norm, by_stem


def _match(name, monkeypatch, lineage=()):
    """Run the matcher with a controllable lineage index."""
    async def resolve(db, query, limit=25):
        return list(lineage)
    monkeypatch.setattr(ba.lineage_store, "resolve", resolve)
    by_code, by_norm, by_stem = _master_indexes()
    return asyncio.run(ba.match_fund(None, by_code, by_norm, by_stem, name_val=name))


@pytest.mark.parametrize("query,expected", [
    ("ICICI Prudential Dividend Yield Equity Fund Growth", 1),
    ("Motilal Oswal Large and Midcap Fund Regular Growth", 2),
    ("Edelweiss Gold and Silver ETF Fund of Fund Regular Growth", 3),
    ("Edelweiss Large Cap Fund Growth", 4),
])
def test_reported_funds_now_match(query, expected, monkeypatch):
    """All four were reported not found. An AMBIGUOUS lineage result is the
    condition that used to break them, so that is what this runs under."""
    assert _match(query, monkeypatch, lineage=[97, 98]) == expected


@pytest.mark.parametrize("query,expected", [
    ("ICICI Prudential Dividend Yield Equity Fund Growth", 1),
    ("Edelweiss Large Cap Fund Growth", 4),
])
def test_matching_is_unaffected_by_lineage_state(query, expected, monkeypatch):
    """A better lineage index must never match FEWER funds — the whole shape
    of the original bug."""
    assert _match(query, monkeypatch, lineage=[]) == expected
    assert _match(query, monkeypatch, lineage=[97, 98]) == expected


def test_a_confident_lineage_hit_still_wins(monkeypatch):
    """The lineage tier is more precise than the stem tier and keeps priority
    when it is confident — it is what resolves renamed and historical names."""
    assert _match("Some Old Renamed Fund", monkeypatch, lineage=[8]) == 8


def test_fund_of_fund_and_fof_are_the_same_words():
    assert ba.match_key("Edelweiss Gold and Silver ETF Fund of Fund Regular Growth") \
        == ba.match_key("Edelweiss Gold and Silver ETF FOF - Regular Plan - Growth")
    assert ba.match_key("X Fund of Funds - Growth") == ba.match_key("X FOF Growth")


def test_match_key_does_not_collapse_genuinely_different_schemes():
    assert ba.match_key("Edelweiss Large Cap Fund - Regular Plan - Growth Option") \
        != ba.match_key("Edelweiss Large & Mid Cap Fund - Regular Plan - Growth Option")
    assert ba.match_key("Motilal Oswal Large Cap Regular Plan Growth") \
        != ba.match_key("Motilal Oswal Large and Midcap Fund - Regular Plan Growth")


# ─── Choosing between plan variants of one scheme ────────────────────────────

def test_plain_growth_beats_a_lettered_legacy_plan():
    """"Edelweiss Large Cap Fund Growth" must resolve to the Regular Plan, not
    to the closed Plan B / Plan C share classes."""
    assert ba.resolve_variants([4, 5, 6], _MASTER) == 4


def test_growth_beats_a_payout_variant():
    master = {1: "X Fund - Regular Plan - Growth", 2: "X Fund - Regular Plan - IDCW"}
    assert ba.resolve_variants([1, 2], master) == 1


def test_a_genuine_tie_is_refused():
    """Two equally plausible growth variants is a real ambiguity — Rule 7 says
    report "Fund not found" rather than pick one."""
    master = {1: "X Fund - Growth", 2: "Y Fund - Growth"}
    assert ba.resolve_variants([1, 2], master) is None


def test_duplicate_listings_of_one_code_are_not_an_ambiguity():
    """mfapi's master list repeats entries for the same schemeCode."""
    assert ba.resolve_variants([4, 4, 4], _MASTER) == 4


def test_resolve_variants_on_empty():
    assert ba.resolve_variants([], _MASTER) is None


# ─── P10 XIRR ────────────────────────────────────────────────────────────────

def test_p10_xirr_sits_immediately_after_p10_wm():
    i = ba.APP_COLUMNS.index("P10 WM")
    assert ba.APP_COLUMNS[i + 1] == "P10 XIRR"
    assert ba.APP_COLUMNS[i + 2] == "CV (Stability)"


def test_p10_xirr_is_converted_at_the_horizon_actually_used():
    values = ba.build_app_values(
        fund_result={"picked": ({"fqs": 70.7, "p10_wm": 1.5, "median_wm": 1.9}, 6),
                     "scheme_name": "X", "category": "Small Cap"},
        remark="x",
    )
    assert values["P10 XIRR"] == pytest.approx(ba.sip_wm_to_xirr(1.5, 6))
    assert values["P10 XIRR"] != pytest.approx(ba.sip_wm_to_xirr(1.5, 10))


def test_p10_xirr_binds_to_an_existing_column_rather_than_duplicating():
    """The column this feature used to append was called exactly "P10 XIRR",
    so a previously processed sheet must fill it, not add a second one."""
    plan = ba.plan_columns(["Fund Name", "P10 WM", "P10 XIRR"])
    by_app = {c["app"]: c for c in plan["order"] if c["app"]}
    assert by_app["P10 WM"]["source"] == 1
    assert by_app["P10 XIRR"]["source"] == 2
    assert [c["name"] for c in plan["order"]].count("P10 XIRR") == 1


def test_p10_xirr_appears_in_the_download(monkeypatch):
    _, out = _run(monkeypatch, ["Fund Name"], [["Alpha Equity Fund - Growth"]], job_id="p10x")
    header = _sheet_header(out.active)
    assert header.index("P10 XIRR") == header.index("P10 WM") + 1
    row = dict(zip(header, _sheet_row(out.active, 1)))
    assert row["P10 XIRR"] == pytest.approx(ba.sip_wm_to_xirr(1.8, 10))
    ws = out.active
    assert ws.cell(row=5, column=header.index("P10 XIRR") + 1).number_format == "0.0%"


def test_a_broken_lineage_index_does_not_break_matching(monkeypatch):
    """The lineage tier is reached for every name now, so a missing collection
    or a Mongo hiccup must not turn a fund the stem tier can resolve into
    "Fund not found"."""
    async def exploding(db, query, limit=25):
        raise RuntimeError("lineage collection missing")
    monkeypatch.setattr(ba.lineage_store, "resolve", exploding)
    by_code, by_norm, by_stem = _master_indexes()
    got = asyncio.run(ba.match_fund(None, by_code, by_norm, by_stem,
                                    name_val="Edelweiss Large Cap Fund Growth"))
    assert got == 4
