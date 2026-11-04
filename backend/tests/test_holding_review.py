"""Portfolio Holding Review: parser round-trip, engine rules, API workflow, PDF."""
import asyncio
import base64
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import bulk_analysis as ba
import holding_review_engine as hre
import holding_review_pdf
import holding_review_routes as hrr
import proposal_engine as pe
import proposal_pdf
import review_pdf_parser as rpp


# ── helpers ──────────────────────────────────────────────────────────────────
def _analysis(fqs, med=2.5, p10=2.0, cv=0.06, horizon=10, cat="Large Cap"):
    return {"category": cat, "last_month": "2026-09-30", "horizons": [
        {"horizon": horizon, "fqs": fqs, "blended_wm": med, "success_pct": 1.0, "median_wm": med, "p10_wm": p10,
         "cv": cv, "n_windows": 50}]}


def _proposal_doc(funds, horizon=10, pan="ABCDE1234F", name="Test Client", pid="pid123"):
    rows = [pe.build_fund_row(c, n, "Large Cap", 1 / len(funds), a, horizon, "sip") for c, n, a in funds]
    proj = pe.build_mode_projection(rows, 5000, horizon, "sip")
    return {"proposal_id": pid, "client_name": name, "client_phone": None, "client_pan": pan,
            "proposal_date": "2026-08-06", "mode": "sip", "horizon": horizon,
            "plan": pe.mode_projection_to_dict(proj), "swp": None}


FUNDS = [(101, "Alpha Large Cap Fund - Regular Plan - Growth", _analysis(92.7, 2.76, 2.44)),
         (102, "Beta Flexi Cap Fund - Regular Plan - Growth", _analysis(71.9, 2.27, 1.65, 0.177))]


# ── parser ───────────────────────────────────────────────────────────────────
def test_parser_round_trip_on_generated_proposal():
    pdf = proposal_pdf.build_pdf(_proposal_doc(FUNDS))
    out = rpp.parse_proposal_pdf(pdf)
    assert out["client"]["name"] == "Test Client" and out["client"]["pan"] == "ABCDE1234F"
    assert out["proposal_date"] == "2026-08-06" and out["plan"]["mode"] == "sip"
    assert out["plan"]["goal_years"] == 10 and out["plan"]["amount"] == 5000
    assert len(out["funds"]) == 2
    by = {f["fund_name"]: f for f in out["funds"]}
    a = by["Alpha Large Cap Fund - Regular Plan - Growth"]
    assert a["previous"]["fqs"] == 92.7 and a["previous"]["wm_med"] == 2.76 and a["previous"]["wm_p10"] == 2.44
    assert a["previous"]["cv"] == pytest.approx(0.06, abs=1e-3) and a["rolling_year"] == 10


def test_embedded_reference_is_readable_and_old_pdfs_have_none():
    pdf = proposal_pdf.build_pdf(_proposal_doc(FUNDS, pid="abc999"))
    assert rpp.read_reference(pdf) == {"kind": "proposal", "id": "abc999", "version": 1}
    assert rpp.read_reference(b"%PDF-1.4 not really") is None


def test_unavailable_fund_stays_blank_not_zero():
    funds = FUNDS + [(103, "Gamma Fund", {"category": "Large Cap", "horizons": []})]
    out = rpp.parse_proposal_pdf(proposal_pdf.build_pdf(_proposal_doc(funds)))
    g = next(f for f in out["funds"] if f["fund_name"] == "Gamma Fund")
    assert all(v is None for v in g["previous"].values())


def test_many_funds_span_pages_and_single_fund():
    many = [(200 + i, f"Fund Number {i} - Regular Plan - Growth", _analysis(80 + i % 10)) for i in range(22)]
    assert len(rpp.parse_proposal_pdf(proposal_pdf.build_pdf(_proposal_doc(many)))["funds"]) == 22
    assert len(rpp.parse_proposal_pdf(proposal_pdf.build_pdf(_proposal_doc(FUNDS[:1])))["funds"]) == 1


def test_short_history_footnote_rolling_year():
    short = (104, "Delta Fund - Regular Plan - Growth", _analysis(88.0, horizon=6))
    out = rpp.parse_proposal_pdf(proposal_pdf.build_pdf(_proposal_doc([FUNDS[0], short])))
    d = next(f for f in out["funds"] if f["fund_name"].startswith("Delta"))
    assert d["rolling_year"] == 6 and d["extrapolated"]


def test_bad_inputs():
    with pytest.raises(rpp.PdfParseError):
        rpp.parse_proposal_pdf(b"not a pdf")
    from reportlab.pdfgen import canvas
    import io
    buf = io.BytesIO(); c = canvas.Canvas(buf); c.showPage(); c.save()      # no text layer
    with pytest.raises(rpp.PdfParseError, match="no readable text"):
        rpp.parse_proposal_pdf(buf.getvalue())
    buf = io.BytesIO(); c = canvas.Canvas(buf)
    c.drawString(50, 800, "A document that is long enough to count as text but is not a proposal at all."); c.save()
    with pytest.raises(rpp.PdfParseError, match="No fund table"):
        rpp.parse_proposal_pdf(buf.getvalue())


def test_parse_number():
    assert rpp.parse_number("19.3%", percent=True) == pytest.approx(0.193)
    assert rpp.parse_number("—") is None and rpp.parse_number("") is None
    assert rpp.parse_number("Rs. 2,40,000") == 240000 and rpp.parse_number("2.92x") == 2.92


# ── engine ───────────────────────────────────────────────────────────────────
def test_changes_percent_vs_percentage_points():
    c = hre.compute_changes({"fqs": 92.7, "irr_med": 0.16, "cv": None}, {"fqs": 92.2, "irr_med": 0.156, "cv": 0.074})
    assert c["fqs"]["abs"] == -0.5 and c["fqs"]["pct"] == pytest.approx(-0.5394, abs=1e-3)
    assert c["irr_med"] == {"abs": pytest.approx(-0.4), "pct": None, "unit": "pp"}
    assert c["cv"] is None                      # blank previous is never treated as 0


def test_pct_change_guards():
    assert hre.pct_change(0, 5) is None and hre.pct_change(None, 5) is None
    assert hre.pct_change(24636, 22422) == -8.99
    m = hre.summarize_market({"aum": {"previous": 483976, "current": 489680}})
    assert m["aum"]["change_pct"] == 1.18 and m["nifty"]["change_pct"] is None


def test_validation():
    base = {"client": {"name": "X"}, "previous_review_date": "2026-08-06", "current_review_date": "2026-10-01",
            "funds": [{"investment_type": "SIP", "scheme_code": 1, "rolling_year": 10, "fund_name_pdf": "F"}]}
    assert hre.validate_review(base, today=date(2026, 10, 7))["errors"] == []
    bad = {**base, "current_review_date": "2026-07-01"}
    assert any("before the previous" in e for e in hre.validate_review(bad, today=date(2026, 10, 7))["errors"])
    unm = {**base, "funds": [{"investment_type": "SIP", "scheme_code": None, "rolling_year": 10, "fund_name_pdf": "XYZ"}]}
    assert any("Unable to map fund: XYZ" in e for e in hre.validate_review(unm, today=date(2026, 10, 7))["errors"])
    fut = {**base, "current_review_date": "2030-01-01"}
    assert any("future" in e for e in hre.validate_review(fut, today=date(2026, 10, 7))["errors"])


def test_observations_use_existing_thresholds():
    funds = [
        {"scheme_name": "Low", "scheme_code": 1, "investment_type": "SIP", "current_status": "ok",
         "previous": {"fqs": 80.0}, "current": {"fqs": ba.FQS_BAD_BELOW - 1, "cv": 0.177}},
        {"scheme_name": "Mid", "scheme_code": 2, "investment_type": "SIP", "current_status": "ok",
         "previous": {}, "current": {"fqs": ba.FQS_BAD_BELOW + 1}},
        {"scheme_name": "Great", "scheme_code": 3, "investment_type": "SIP", "current_status": "ok",
         "previous": {"fqs": 92.7}, "current": {"fqs": 92.2}},
        {"scheme_name": "Ext", "scheme_code": 4, "investment_type": "External", "current_status": "ok",
         "previous": {}, "current": {"fqs": 95.0}},
        {"scheme_name": "Gone", "scheme_code": 5, "investment_type": "SIP", "current_status": "error", "current": None},
    ]
    obs = hre.generate_observations(funds)
    text = " | ".join(o["text"] for o in obs)
    assert "review/redeem Low" in text and "17.7%" in text
    assert "Mid has a current FQS" in text and "decreased from 92.7 to 92.2" in text
    assert "NOT applicable" in text and "Ext" in text and "Gone could not be obtained" in text
    assert all(o["basis"] for o in obs)
    manual = [{"id": "m", "text": "mine", "source": "manual"}]
    assert hre.merge_observations(manual + obs, obs)[-1]["id"] == "m"
    assert sum(1 for o in hre.merge_observations(manual + obs, obs) if o["source"] == "auto") == len(obs)


# ── API workflow (fake Mongo, injected analysis fns) ─────────────────────────
class _Cursor:
    def __init__(self, docs): self.docs = list(docs)
    def sort(self, k, d=1): self.docs.sort(key=lambda x: x.get(k) or "", reverse=d < 0); return self
    def limit(self, n): self.docs = self.docs[:n]; return self
    def __aiter__(self): self._it = iter(self.docs); return self
    async def __anext__(self):
        try: return next(self._it)
        except StopIteration: raise StopAsyncIteration


def _match(d, q):
    for k, v in q.items():
        cur = d
        for part in k.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur != v:
            return False
    return True


class _Coll:
    def __init__(self): self.docs = {}
    async def find_one(self, q, proj=None):
        return next((dict(d) for d in self.docs.values() if _match(d, q)), None)
    async def replace_one(self, q, doc, upsert=False): self.docs[doc["_id"]] = dict(doc)
    async def update_one(self, q, upd, upsert=False):
        _id = q["_id"]
        if _id not in self.docs and upsert: self.docs[_id] = {"_id": _id, **upd.get("$setOnInsert", {})}
    async def count_documents(self, q): return sum(1 for d in self.docs.values() if _match(d, q))
    def find(self, q=None, proj=None): return _Cursor(dict(d) for d in self.docs.values() if _match(d, q or {}))


class _DB:
    def __init__(self): self.c = {}
    def __getitem__(self, n): return self.c.setdefault(n, _Coll())
    def __getattr__(self, n):
        if n.startswith("_"): raise AttributeError(n)
        return self[n]


@pytest.fixture
def client(monkeypatch):
    names = {101: "Alpha Large Cap Fund - Regular Plan - Growth", 102: "Beta Flexi Cap Fund - Regular Plan - Growth",
             103: "Beta Mid Cap Fund - Regular Plan - Growth"}

    async def fake_index():
        by_norm, by_stem = {}, {}
        import lineage_store
        for c, n in names.items():
            by_norm.setdefault(lineage_store.normalise_name(n), []).append(c)
            by_stem.setdefault(ba.match_key(n), []).append(c)
        return dict(names), by_norm, by_stem

    monkeypatch.setattr(ba, "build_master_index", fake_index)
    current = {101: _analysis(92.2, 2.61, 2.38, 0.074), 102: _analysis(71.9, 2.27, 1.65, 0.177),
               103: _analysis(88.0, 2.9, 2.2, 0.09)}

    async def cached(code): return current.get(code)
    async def build(code, **kw): raise AssertionError("cache miss")
    db = _DB()
    app = FastAPI()
    app.include_router(hrr.build_router(db, build, cached, build, cached))
    c = TestClient(app)
    c.db = db
    return c


def _upload(client, funds=FUNDS, **kw):
    pdf = proposal_pdf.build_pdf(_proposal_doc(funds, **kw))
    return client.post("/api/portfolio-review/upload",
                       json={"filename": "p.pdf", "content_base64": base64.b64encode(pdf).decode()})


def test_full_workflow(client):
    r = _upload(client)
    assert r.status_code == 200, r.text
    rev = r.json()
    rid = rev["review_id"]
    assert rev["client"]["name"] == "Test Client" and rev["previous_review_date"] == "2026-08-06"
    assert [f["scheme_code"] for f in rev["funds"]] == [101, 102] or {f["scheme_code"] for f in rev["funds"]} == {101, 102}
    assert all(f["match"]["confidence"] == "high" for f in rev["funds"])

    cur = client.post(f"/api/portfolio-review/{rid}/fetch-current").json()
    f101 = next(f for f in cur["funds"] if f["scheme_code"] == 101)
    assert f101["current"]["fqs"] == 92.2 and f101["changes"]["fqs"]["abs"] == -0.5
    assert cur["data_as_of"] == "2026-09-30"

    obs = client.post(f"/api/portfolio-review/{rid}/observations/generate").json()["observations"]
    assert any("review/redeem Beta Flexi" in o["text"] for o in obs)

    put = client.put(f"/api/portfolio-review/{rid}", json={
        "market": {"nifty": {"previous": 24636, "current": 22422}, "aum": {"previous": 483976, "current": 489680}},
        "current_review_date": "2026-10-01", "confirm_all": True,
        "observations": [{"id": obs[0]["id"], "text": "edited by advisor"}, {"text": "added by advisor"}]})
    assert put.status_code == 200
    body = put.json()
    assert body["market_summary"]["nifty"]["change_pct"] == -8.99
    assert body["observations"][0]["source"] == "manual" and body["observations"][1]["source"] == "manual"

    pdf = client.get(f"/api/portfolio-review/{rid}/export.pdf")
    assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-"
    assert rpp.read_reference(pdf.content) == {"kind": "review", "id": rid, "version": 1}

    assert client.post(f"/api/portfolio-review/{rid}/finalize").status_code == 200
    assert client.put(f"/api/portfolio-review/{rid}", json={"quarters_completed": 3}).status_code == 409
    rev2 = client.post(f"/api/portfolio-review/{rid}/revise").json()
    assert rev2["status"] == "draft" and rev2["parent_review_id"] == rid and rev2["version"] == 2
    assert client.get("/api/portfolio-review").json()["count"] == 2


def test_review_report_becomes_next_baseline(client):
    rid = _upload(client).json()["review_id"]
    client.post(f"/api/portfolio-review/{rid}/fetch-current")
    client.put(f"/api/portfolio-review/{rid}", json={"current_review_date": "2026-10-01",
               "market": {"nifty": {"previous": 24000, "current": 22422}}})
    client.post(f"/api/portfolio-review/{rid}/finalize")
    pdf = client.get(f"/api/portfolio-review/{rid}/export.pdf").content
    nxt = client.post("/api/portfolio-review/upload",
                      json={"filename": "r.pdf", "content_base64": base64.b64encode(pdf).decode()}).json()
    assert nxt["previous_review_id"] == rid and nxt["previous_review_date"] == "2026-10-01"
    f = next(x for x in nxt["funds"] if x["scheme_code"] == 101)
    assert f["previous"]["fqs"] == 92.2                      # last review's CURRENT is now the baseline
    assert nxt["market"]["nifty"]["previous"] == 22422 and nxt["quarters_completed"] == 2


def test_unmapped_fund_needs_advisor_choice(client):
    funds = [(101, "Alpha Large Cap Fund - Regular Plan - Growth", _analysis(90)),
             (999, "Beta Mid-Cap Fnd", _analysis(80))]
    rev = _upload(client, funds).json()
    bad = next(f for f in rev["funds"] if f["fund_name_pdf"].startswith("Beta Mid"))
    assert bad["scheme_code"] is None and bad["status"] == "needs_review" and bad["match"]["confidence"] == "low"
    assert any("Unable to map fund" in e for e in rev["validation"]["errors"])
    assert client.post(f"/api/portfolio-review/{rev['review_id']}/finalize").status_code == 422
    fixed = client.put(f"/api/portfolio-review/{rev['review_id']}",
                       json={"funds": [{"fid": bad["fid"], "scheme_code": 103}]}).json()
    nf = next(f for f in fixed["funds"] if f["fid"] == bad["fid"])
    assert nf["scheme_code"] == 103 and nf["status"] == "verified"


def test_cob_external_fund_keeps_blank_previous(client):
    rid = _upload(client).json()["review_id"]
    cur = client.get(f"/api/portfolio-review/{rid}").json()
    funds = [{"fid": f["fid"]} for f in cur["funds"]] + [{"scheme_code": 103, "rolling_year": 10, "investment_type": "COB"}]
    r = client.put(f"/api/portfolio-review/{rid}", json={"funds": funds}).json()
    client.post(f"/api/portfolio-review/{rid}/fetch-current")
    r = client.get(f"/api/portfolio-review/{rid}").json()
    cob = next(f for f in r["funds"] if f["investment_type"] == "COB")
    assert all(v is None for v in cob["previous"].values()) and cob["current"]["fqs"] == 88.0
    assert cob["changes"]["fqs"] is None
    assert r["type_of_investment"] == "SIP+COB"
    pdf = client.get(f"/api/portfolio-review/{rid}/export.pdf")
    assert pdf.status_code == 200


def test_upload_errors(client):
    post = lambda b: client.post("/api/portfolio-review/upload", json={"filename": "x.pdf", "content_base64": b})
    assert post("!!!").status_code == 400
    assert post(base64.b64encode(b"hello").decode()).status_code == 400
    assert post(base64.b64encode(b"%PDF-1.4 garbage").decode()).status_code == 422
    assert client.get("/api/portfolio-review/nope").status_code == 404


def test_report_pdf_handles_20_plus_funds():
    funds = [{"scheme_name": f"Fund {i} - Regular Plan - Growth", "rolling_year": 10, "investment_type": "SIP",
              "previous": {"fqs": 90.0, "wm_p10": 2.0, "wm_med": 2.5, "irr_p10": 0.15, "irr_med": 0.18, "cv": 0.06},
              "current": {"fqs": 70.0 + i, "wm_p10": 1.9, "wm_med": 2.4, "irr_p10": 0.14, "irr_med": 0.17, "cv": 0.07}}
             for i in range(25)]
    doc = {"review_id": "r1", "client": {"name": "A & B <Client>", "pan": "ABCDE1234F"}, "plan": {"goal_years": 10},
           "previous_review_date": "2026-08-06", "current_review_date": "2026-10-01", "funds": funds,
           "market": {"nifty": {"previous": 24636, "current": 22422}}, "observations": [{"text": "x < y", "included": True}]}
    pdf = holding_review_pdf.build_report_pdf(doc)
    import io, pdfplumber
    with pdfplumber.open(io.BytesIO(pdf)) as p:
        assert len(p.pages) >= 2
        text = "\n".join(pg.extract_text() or "" for pg in p.pages)
    assert "PORTFOLIO HOLDING REVIEW REPORT" in text and "ArthVruksh Observations" in text
    assert "Fund 24" in text and "Past performance does not guarantee" in text and "-8.99%" in text
