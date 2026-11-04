"""Regression tests: NAV-span refresh and orphaned bulk-job recovery."""
import asyncio
from datetime import date

import bulk_analysis_routes
import nav_meta_indexer


def _loaded(first, last, own_first=None, own_last=None):
    rows = lambda a, b: [{"date": a, "nav": 10.0}, {"date": b, "nav": 12.0}]
    return {
        "nav_history": rows(first, last),
        "original_nav": rows(own_first or first, own_last or last),
        "successor_used": None, "predecessor_used": [101], "history_note": None,
    }


def test_span_fields_uses_stitched_and_own_spans():
    f = nav_meta_indexer.span_fields(
        _loaded(date(2010, 1, 1), date(2026, 9, 1), date(2016, 1, 1), date(2026, 9, 1)))
    assert f["history_years"] == round((date(2026, 9, 1) - date(2010, 1, 1)).days / 365.25, 2)
    assert f["earliest_nav_date"] == "2010-01-01"
    assert f["own_earliest_nav_date"] == "2016-01-01"
    assert f["latest_nav_date"] == f["own_latest_nav_date"] == "2026-09-01"
    assert f["predecessor_used"] == [101]


def test_span_fields_empty_history():
    assert nav_meta_indexer.span_fields({"nav_history": []}) == {}


def test_span_fields_grows_with_new_nav():
    old = nav_meta_indexer.span_fields(_loaded(date(2020, 1, 1), date(2024, 1, 1)))
    new = nav_meta_indexer.span_fields(_loaded(date(2020, 1, 1), date(2026, 1, 1)))
    assert new["history_years"] > old["history_years"]
    assert new["own_latest_nav_date"] > old["own_latest_nav_date"]


class _Coll:
    def __init__(self, docs): self.docs = docs
    async def update_many(self, q, upd):
        n = 0
        for d in self.docs:
            if d.get("status") == q["status"]:
                d.update(upd["$set"]); n += 1
        class R: modified_count = n
        return R()


class _DB:
    def __init__(self, docs): self.c = _Coll(docs)
    def __getitem__(self, _): return self.c


def test_orphaned_processing_jobs_are_failed_not_stuck():
    docs = [{"status": "processing"}, {"status": "completed"}, {"status": "pending"}]
    n = asyncio.run(bulk_analysis_routes.recover_orphaned_jobs(_DB(docs)))
    assert n == 1
    assert docs[0]["status"] == "failed" and "restart" in docs[0]["error"]
    assert docs[1]["status"] == "completed" and docs[2]["status"] == "pending"
