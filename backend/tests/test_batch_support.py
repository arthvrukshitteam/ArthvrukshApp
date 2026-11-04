"""batch_support.py — tests for the nightly-batch lock and cursor pagination.

These guard the two behaviours the 512MB instance depends on:

  · the two full-universe jobs never hold their working sets at the same time;
  · walking the universe never materialises more than one page.
"""
import asyncio
import os
import sys

import pytest

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import batch_support  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_lock():
    """Each test gets its own lock — the module keeps one per process."""
    batch_support._lock = None
    batch_support._running_job = None
    yield
    batch_support._lock = None
    batch_support._running_job = None


# ── Mutual exclusion ─────────────────────────────────────────────────────────

def test_two_jobs_never_overlap():
    """The bug this exists for: FQS starts at 22:00/22:30 UTC and runs for
    hours, lumpsum starts at 22:00/23:00, and nothing stopped both being
    resident at once — each with its own full working set."""
    timeline = []

    async def job(name, hold):
        async with batch_support.exclusive(name):
            timeline.append(f"{name}:start")
            await asyncio.sleep(hold)
            timeline.append(f"{name}:end")

    async def main():
        await asyncio.gather(job("fqs", 0.05), job("lumpsum", 0.05))

    asyncio.run(main())
    # Whoever wins, the loser's start must come after the winner's end.
    assert timeline[1].endswith(":end"), timeline
    assert timeline == [timeline[0], timeline[0].replace(":start", ":end"),
                        timeline[2], timeline[2].replace(":start", ":end")]


def test_second_job_runs_rather_than_being_skipped():
    """Waiting, not skipping: both jobs genuinely need to run each night.
    Skipping the loser is how the lumpsum numbers silently go stale."""
    ran = []

    async def job(name):
        async with batch_support.exclusive(name):
            ran.append(name)
            await asyncio.sleep(0.01)

    async def main():
        await asyncio.wait_for(
            asyncio.gather(job("fqs"), job("lumpsum")), timeout=5)

    asyncio.run(main())
    assert sorted(ran) == ["fqs", "lumpsum"]


def test_lock_is_released_when_a_job_raises():
    """A crashed batch must not wedge every future batch."""
    async def main():
        with pytest.raises(RuntimeError):
            async with batch_support.exclusive("fqs"):
                raise RuntimeError("boom")
        # Still acquirable.
        async with batch_support.exclusive("lumpsum"):
            assert batch_support.current_job() == "lumpsum"
    asyncio.run(main())
    assert batch_support.current_job() is None


def test_current_job_reports_the_holder():
    async def main():
        assert batch_support.current_job() is None
        async with batch_support.exclusive("fqs_refresh"):
            assert batch_support.current_job() == "fqs_refresh"
        assert batch_support.current_job() is None
    asyncio.run(main())


def test_lock_is_created_lazily_not_at_import():
    """Building an asyncio primitive at import binds it to the wrong loop (and
    raises on 3.10+ when no loop is running)."""
    batch_support._lock = None
    assert batch_support._lock is None          # importing did not create one
    asyncio.run(_take_and_release())
    assert batch_support._lock is not None


async def _take_and_release():
    async with batch_support.exclusive("x"):
        pass


# ── Keyset pagination ────────────────────────────────────────────────────────

class FakeCursor:
    def __init__(self, docs):
        self._docs = docs
        self._sort_key = None
        self._limit = None

    def sort(self, key, direction):
        self._sort_key = key
        self._docs = sorted(self._docs, key=lambda d: d[key], reverse=direction < 0)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def __aiter__(self):
        async def gen():
            for d in self._docs[: self._limit]:
                yield d
        return gen()


class FakeCollection:
    """Applies just enough of the Mongo query surface iter_batches uses."""

    def __init__(self, docs):
        self.docs = docs
        self.queries = []

    def find(self, query, projection=None):
        self.queries.append(query)
        out = []
        for d in self.docs:
            ok = True
            for k, cond in query.items():
                v = d.get(k)
                if isinstance(cond, dict):
                    if "$gt" in cond and not (v is not None and v > cond["$gt"]):
                        ok = False
                    if "$gte" in cond and not (v is not None and v >= cond["$gte"]):
                        ok = False
                elif v != cond:
                    ok = False
            if ok:
                out.append(dict(d))
        return FakeCursor(out)


def _drain(collection, query, projection, size):
    async def main():
        pages = []
        async for page in batch_support.iter_batches(collection, query, projection, size):
            pages.append(page)
        return pages
    return asyncio.run(main())


def test_every_document_is_yielded_exactly_once():
    docs = [{"scheme_code": c, "history_years": 5} for c in range(1, 251)]
    pages = _drain(FakeCollection(docs), {"history_years": {"$gte": 1}}, {}, 100)
    codes = [d["scheme_code"] for p in pages for d in p]
    assert codes == list(range(1, 251))
    assert len(codes) == len(set(codes))


def test_no_page_exceeds_the_batch_size():
    docs = [{"scheme_code": c, "history_years": 5} for c in range(1, 251)]
    pages = _drain(FakeCollection(docs), {"history_years": {"$gte": 1}}, {}, 100)
    assert [len(p) for p in pages] == [100, 100, 50]


def test_the_caller_filter_is_preserved_on_every_page():
    docs = [{"scheme_code": c, "history_years": (5 if c % 2 else 0)} for c in range(1, 101)]
    coll = FakeCollection(docs)
    pages = _drain(coll, {"history_years": {"$gte": 1}}, {}, 10)
    codes = [d["scheme_code"] for p in pages for d in p]
    assert all(c % 2 for c in codes)
    assert all("history_years" in q for q in coll.queries)


def test_pagination_advances_with_a_gt_cursor_not_skip():
    """`.skip()` degrades as the offset grows and a long-lived cursor is closed
    by the server after ~10 idle minutes — this walk lasts hours."""
    docs = [{"scheme_code": c, "history_years": 5} for c in range(1, 31)]
    coll = FakeCollection(docs)
    _drain(coll, {"history_years": {"$gte": 1}}, {}, 10)
    later = [q for q in coll.queries if "scheme_code" in q]
    assert later, "no keyset query was issued"
    assert all("$gt" in q["scheme_code"] for q in later)


def test_empty_universe_yields_nothing():
    assert _drain(FakeCollection([]), {}, {}, 100) == []


def test_exact_multiple_of_batch_size_terminates():
    docs = [{"scheme_code": c, "history_years": 5} for c in range(1, 21)]
    pages = _drain(FakeCollection(docs), {"history_years": {"$gte": 1}}, {}, 10)
    assert [len(p) for p in pages] == [10, 10]


def test_key_is_projected_even_when_the_caller_omits_it():
    """Without the key in the projection, pagination cannot advance and the
    walk would loop forever on page one."""
    docs = [{"scheme_code": c, "scheme_name": f"F{c}", "history_years": 5}
            for c in range(1, 6)]
    pages = _drain(FakeCollection(docs), {"history_years": {"$gte": 1}},
                   {"scheme_name": 1}, 2)
    assert [d["scheme_code"] for p in pages for d in p] == [1, 2, 3, 4, 5]
