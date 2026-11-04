"""batch_support.py — shared runtime for the nightly full-universe jobs.

`fqs_refresh.refresh_all_fqs` and `lumpsum_refresh.refresh_all_lumpsum` each
walk every fund in `fund_nav_meta`. They run INSIDE the web process — Render's
free plan has no worker or cron service to move them to — so on a 512MB
instance they have to be careful in two specific ways that this module
provides.

    ── 1. They must never run at the same time ──
The per-job guards in server.py stop a job overlapping ITSELF ("already_running")
but nothing stopped FQS overlapping LUMPSUM, and in practice they always did:
the GitHub Actions cron POSTs /api/refresh/run, /api/fqs/refresh and
/api/lumpsum/refresh within seconds of each other at 22:00 UTC, and the
in-process schedulers fire at 22:00 / 22:30 / 23:00 UTC. Since a full pass
takes hours, two — sometimes three — full-universe jobs were resident
simultaneously, each with its own working set. `exclusive()` serialises them:
the second job waits and then runs, instead of running alongside the first.

Waiting rather than skipping is deliberate. Both jobs genuinely need to run
each night; skipping the loser would mean the lumpsum numbers silently never
refresh — the same class of bug the GitHub cron was added to fix. Pile-up
across nights is already prevented upstream by the per-job "already_running"
guards, so at most one instance of each job can ever be queued here.

    ── 2. They must not hold the whole universe in memory ──
Both jobs used to do `docs = [doc async for doc in cursor]` — the entire
collection materialised before the first fund was processed (~31MB at 37k
funds, and double that when the two jobs overlapped). `iter_batches` replaces
that with keyset pagination: one page of `batch_size` docs is resident at a
time, and the previous page is dropped before the next is fetched.

Keyset (`scheme_code > last_seen`) rather than a long-lived cursor or
`.skip()`: these runs last hours, and an idle MongoDB cursor is closed by the
server after ~10 minutes, which would kill the job partway through with
"cursor not found". A fresh, indexed range query per page has no such
lifetime, and unlike `.skip()` it does not get slower as the offset grows.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional

logger = logging.getLogger("batch_support")

# Module-level so every caller in the process shares one lock. Created lazily:
# constructing an asyncio primitive at import time binds it to whatever loop
# happens to be current then, which is not necessarily the loop uvicorn ends up
# running (and raises on 3.10+ if there is no running loop at import).
_lock: Optional[asyncio.Lock] = None
_running_job: Optional[str] = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def current_job() -> Optional[str]:
    """Name of the full-universe job holding the lock, or None. Surfaced on the
    status endpoints so an operator can see WHY a job hasn't started yet."""
    return _running_job


@asynccontextmanager
async def exclusive(job_name: str) -> AsyncIterator[None]:
    """Hold the process-wide batch lock for the duration of the block.

    Acquire BEFORE allocating the job's working set — the whole point is that
    the waiting job costs nothing but a coroutine frame while it waits.
    """
    global _running_job

    lock = _get_lock()
    if lock.locked():
        logger.info(
            "%s waiting — %s is already running (batch jobs are serialised)",
            job_name, _running_job or "another job",
        )
    await lock.acquire()
    _running_job = job_name
    logger.info("%s acquired the batch lock", job_name)
    try:
        yield
    finally:
        _running_job = None
        lock.release()
        logger.info("%s released the batch lock", job_name)


async def iter_batches(
    collection,
    query: Dict[str, Any],
    projection: Dict[str, Any],
    batch_size: int,
    key: str = "scheme_code",
) -> AsyncIterator[List[Dict[str, Any]]]:
    """Yield pages of documents, keyset-paginated on `key`.

    Only one page is resident at a time. `key` must be indexed and unique
    enough to order by — `scheme_code` is both.

    Documents whose `key` is missing are skipped rather than ending the walk:
    a malformed row must not silently truncate the universe at that point.
    """
    last: Any = None
    while True:
        q = dict(query)
        if last is not None:
            q[key] = {**(q.get(key) or {}), "$gt": last}

        # Projection must include the key even if the caller didn't ask for it,
        # or pagination cannot advance.
        proj = {**projection, key: 1}

        page: List[Dict[str, Any]] = [
            doc async for doc in collection.find(q, proj).sort(key, 1).limit(batch_size)
        ]
        if not page:
            return

        last = page[-1].get(key)
        if last is None:
            logger.warning("iter_batches: page ended with a doc missing %r — stopping", key)
            return

        yield [d for d in page if d.get(key) is not None]
