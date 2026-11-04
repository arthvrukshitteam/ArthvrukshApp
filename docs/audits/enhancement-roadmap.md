# Enhancement readiness & recommendations

**Nothing here is implemented.** Priorities: **P0** critical · **P1** high · **P2** medium · **P3** nice-to-have. Complexity: Low / Medium / High. "Debt #"/"S#" refer to [technical-debt](technical-debt.md) / [security-audit](security-audit.md).

## P0 — Critical

### P0-1 Put authentication and authorization in front of PII and operator actions
* **Problem:** public API exposes client PAN/phone/name (S1), arbitrary-URL fetch (S2), operator actions open when `REFRESH_TOKEN` unset (S3).
* **Current:** no auth; 10 inline token checks; evidence the token is unset in prod.
* **Why:** regulatory/privacy exposure + trivial DoS on a 512 MB box.
* **Recommended:** (1) immediately: set `REFRESH_TOKEN` on Render + GitHub, fail closed when unset, move to header + `hmac.compare_digest`; (2) block ingest URLs not on an allow-list and private IP ranges; (3) introduce real advisor auth (e.g. signed login → JWT/session) and scope proposals/runs/jobs by owner; mask/encrypt PAN.
* **Files:** `backend/server.py` (replace 10 checks with one dependency), `proposal_routes.py`, `overlap_routes.py`, `rolling_analysis_routes.py`, `bulk_analysis_routes.py`, `holdings_ingest.py`; `frontend/src/api.ts`, new login screen + provider, `app/_layout.tsx`.
* **DB:** new `users`/owner fields on `portfolio_proposals`, `rolling_*`, `bulk_analysis_jobs`; migration for existing docs.
* **API:** new auth routes; every existing route gets a dependency; `Authorization` header. **Breaking for installed APKs** → needs a grace period (soft mode first) because APKs cannot be updated remotely.
* **Frontend:** login/logout, token storage (SecureStore), 401 handling in `get/post`.
* **Risk:** High (breaks old clients; touches every router). **Complexity:** High. Phase it: step (1) and (2) are Low.

### P0-2 Make the fund universe self-refreshing
* **Problem:** debt #1 — `nav_meta_indexer` never refreshes spans or adds new schemes automatically; dead-fund and eligibility rules read frozen data.
* **Current:** manual `POST /home/reindex`, skip-already-indexed.
* **Why:** silently wrong eligibility/visibility; new funds never appear.
* **Recommended:** add a nightly incremental pass: update `latest_nav_date`/`history_years`/`own_*` from the NAV fetch that `fqs_refresh` already performs (zero extra requests), and index only genuinely new codes from the AMFI list; then call `fund_status.sync_superseded`.
* **Files:** `fqs_refresh.py` (write span fields), `nav_meta_indexer.py`, `server.py` scheduler + workflow. **DB:** none (fields exist). **API:** none. **FE:** none. **Risk:** Medium (changes eligible universe on first run — snapshot before). **Complexity:** Medium.

### P0-3 Settle the branch/deploy story
* **Problem:** `main` (MVP) and `version2` diverge massively; `main`'s nightly workflow calls endpoints that exist only on version2; which branch Render serves is UNKNOWN.
* **Recommended:** confirm Render's branch; merge/retire; keep one default branch; update `render.yaml`, workflows, DEPLOY.md.
* **Files:** git, `.github/workflows/*`, `render.yaml`, `DEPLOY.md`. **Risk:** Low–Medium. **Complexity:** Low.

### P0-4 Stop tracking env files; audit secrets
* `git rm --cached backend/.env frontend/.env`, add `.env.example`, inspect history, rotate anything real (S7). **Complexity:** Low. **Risk:** Low.

## P1 — High

### P1-1 CI quality gate
* **Problem:** no CI runs tests/types/lint. **Recommended:** workflow running backend `pytest` (3.11), `ruff`, frontend `tsc --noEmit` + `expo lint`, `pip-audit`/`npm audit`; require green before Render auto-deploys. **Files:** new `.github/workflows/ci.yml`. **Complexity:** Low.

### P1-2 Durable background work
* **Problem:** debt #2, #6; every long job lives in the web process and dies on restart. **Current:** `asyncio.create_task` + memory flags. **Recommended:** persist job state (`jobs` collection: queued/running/done, heartbeat, progress); on boot reconcile orphaned jobs; longer term run batch jobs as Render Cron/worker processes. **Files:** `server.py`, `batch_support.py`, `bulk_analysis_routes.py`, `fqs_refresh.py`, `lumpsum_refresh.py`. **DB:** new `jobs` collection. **API:** status endpoints read from DB. **FE:** none (status shapes preserved). **Risk:** Medium. **Complexity:** High.

### P1-3 Retention & storage hygiene
* **Problem:** unbounded growth (S5; schema §4). **Recommended:** TTL indexes (`bulk_analysis_jobs` 7 d, `rolling_*` 30 d), cap/prune `fund_recent`, purge proposals by policy, don't store workbooks in Mongo (GridFS/object storage). **Files:** `server.py` lifespan (index creation), routers. **DB:** indexes only. **Risk:** Low–Medium (data deletion policy needs product decision). **Complexity:** Low–Medium.

### P1-4 Test the untested core
* Lumpsum engine parity suite, nightly-job tests, indexer tests, endpoint contract tests, PDF/XLSX goldens ([testing audit §3](code-quality-and-testing.md)). **Complexity:** Medium.

### P1-5 Decompose `server.py` and centralise operator guard
* **Recommended:** `routers/{funds,universe,refresh,admin}.py`, `services/{analysis,merger_discovery,schedulers}.py`, one `Depends(require_operator)`; keep import-compatible re-exports used by tests (`server._dedupe_*`, `_build_analysis`). **Risk:** Medium (tests import `server`). **Complexity:** Medium–High.

## P2 — Medium

### P2-1 Trim payloads
* Lazy per-horizon windows for `/analyze` (P1 perf), universe response caching/pagination (P2 perf). **API impact:** additive query params; keep old shape default for old APKs. **Frontend:** Fund Detail fetches windows on horizon change. **Complexity:** Medium.

### P2-2 Frontend structure & data layer
* Split `home.tsx`, `rolling.tsx`, `fund/[code].tsx`; merge SIP/Lumpsum detail via a mode adapter; add AbortController + a small query cache; delete unused deps/API members; replace hard-coded colours with tokens. **Risk:** Medium (UI regressions without tests → pair with P1-4/RTL). **Complexity:** High.

### P2-3 Observability
* Structured JSON logs with request IDs, Sentry (both sides), persisted refresh-run history, `/metrics`. **Complexity:** Medium.

### P2-4 Raw NAV cache
* Cache mfapi payloads (Mongo/disk) keyed by `(code, latest_nav_date)` to remove 3–5× redundant downloads per fund per night and soften the single-point-of-failure on mfapi.in. **Files:** `amfi_client.py`. **DB:** new collection (or GridFS). **Complexity:** Medium.

### P2-5 Repo hygiene
* Delete dead code and the commented-out legacy blocks in `fqs_engine.py`/`category_map.py` (git has history), move `backtest_data/` & large xlsx/PDF out of git, drop duplicate `fqs_all_funds.xlsx`, fix stale comments (see debt #30–#33). **Risk:** Low. **Complexity:** Low.

### P2-6 Unify duplicated math
* One WM↔XIRR spec with shared golden vectors for Python and TS; one stats/date-helper module for both engines. **Complexity:** Low–Medium.

## P3 — Nice to have
* **P3-1** Accessibility labels/roles, dynamic type, tablet & web layouts (breakpoint hook).
* **P3-2** Generate `src/api.ts` types from `/openapi.json` (needs response models on endpoints).
* **P3-3** Overlap: in-app disclosure ingest UI, populated `AMC_SOURCES`, monthly scheduled ingest, coverage banner.
* **P3-4** PRD "future" items still open: per-window CSV export, side-by-side compare, watchlist/alerts, saved filtered analyses.
* **P3-5** `expo-updates` (OTA) to end the "old APK in the wild" compatibility burden (`_LEGACY_ALIAS_KEYS`).
* **P3-6** Optional: replace free-tier hosting assumptions (keep-alive pings, 750-hour cap) with a paid always-on instance.
