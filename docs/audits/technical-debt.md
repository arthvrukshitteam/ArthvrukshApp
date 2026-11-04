# Technical debt & known bugs register

`origin/version2` @ `8a0089a1`. Items are *verified from code* unless tagged UNKNOWN. IDs are referenced from other docs — keep them stable. Severity: 🔴 correctness/ops risk · 🟠 maintainability/risk · 🟡 hygiene.

> **Fix status (branch `fix/span-refresh-bulk-cleanup`):** #1 span fields now refreshed nightly by `fqs_refresh` (shared `nav_meta_indexer.span_fields`) and new schemes indexed incrementally before each run; #2 orphaned jobs marked `failed` at startup and `/process` restartable; #24 ~615 commented lines deleted; #30/#31 dead modules, `fetch_nav_stitched`, unused client API members and 6 unused npm packages removed. **#20 (`server.py` split) is NOT done.**

## A. Correctness & operational bugs

| ID | Sev | Issue | Evidence | Effect |
|---|---|---|---|---|
| #1 | ✅ FIXED | **`fund_nav_meta` NAV-span fields never refresh.** `nav_meta_indexer.build_nav_meta_index` skips every `scheme_code` already present; `history_years`, `earliest/latest_nav_date`, `own_*`, `merged_with` are written **only** there. Nothing in the nightly pipeline calls it (reindex is a manual `POST`). | `nav_meta_indexer.py` L98-L100; grep of writers; `server.py` schedulers | (a) a fund's `history_years` is frozen at first-index time, so eligibility (`history_years ≥ tenure+2`) does not grow; (b) `fund_status` "stale after 180 days" can never trigger for a fund that was live when indexed; (c) brand-new schemes never enter the universe until someone reindexes. Live DB values UNKNOWN — verify with `GET /api/admin/fund-status` and `index_last_built`. |
| #2 | ✅ FIXED | **Bulk-job state is process-local.** `_running_tasks` + job `status:"processing"` in Mongo; a restart orphans the job, and `POST /{id}/process` then returns `already_running` forever (it trusts `status`). Uploaded workbook bytes stay in Mongo. | `bulk_analysis_routes.py` L142-L170 | stuck jobs; storage growth |
| #3 | 🟠 | `stability_check` is never persisted to `fund_nav_meta` (neither nightly nor on-view mirror), yet `/export/all-funds` projects it. | `fqs_refresh.py` $set block; `server._save_analysis` | export column always empty |
| #4 | 🟠 | `/funds/{c}/analyze` accepts `years` (Cycle Robustness variant) but the client never sends it and the cache ignores it: the cached doc carries whichever variant was computed last. | `api.ts analyze`; `_cached_analysis` | CR value may not match the Home tenure; confusing |
| #5 | 🟠 | Two writers `$set` the same `fund_nav_meta` fields (`fqs_by_horizon`, `headline_*`, `cycle_robustness*`, `last_fqs_refresh`) — the nightly FQS job and the on-view mirror in `_save_analysis` (the crawler and `sync_superseded` write disjoint fields) — so a Fund-Detail view overwrites the nightly numbers for that fund (and `last_fqs_refresh`). Equal only while engine code is unchanged. | `server._save_analysis`, `fqs_refresh` | silent divergence after an engine change until next nightly |
| #6 | 🟠 | `_daily_refresh_running` is status-only — nothing prevents the cron-triggered `/refresh/run` and the 22:00 in-process refresh from running concurrently. The third job is outside `batch_support.exclusive`. | `server.py` L2137-L2144 | duplicate work/memory |
| #7 | 🟠 | Cache semantics: fund-detail cache only for default run, 24 h logical TTL checked in code (no TTL index); nightly *recently-viewed* refresh only covers funds viewed in 30 days. A "since 2010" Fund-Detail view bypasses the cache and is computed live every time. | `_cached_analysis`, `analyze` | slow repeat views; inconsistent freshness |
| #8 | 🟠 | Windows with WM above `max_plausible_wm` are **dropped**; a horizon whose every window is dropped silently disappears (already bitten once at 50-year horizons, fixed by scaling the cap). | `category_map.max_plausible_wm`, engines | silent data holes |
| #9 | 🟡 | `total_invested` hard-codes `SIP_AMOUNT×H×12` while units use the actual instalment count (59/60/61) → ±~1.7 % WM artefact (documented in `MODEL_AUDIT.md`). | `fqs_engine.compute_rolling_windows` | accepted model quirk — don't "fix" without product sign-off |
| #10 | 🟡 | `/funds/recent` has an unbounded `limit`; `/funds/search` fallback is O(universe). | `server.py` | minor DoS vector |
| #11 | 🟡 | Proposal request accepts duplicate `scheme_code`s (only count and Σ allocation validated server-side). | `ProposalRequest._validate_funds` | double-counted fund possible via API |

## B. Structure & duplication

| ID | Sev | Issue | Where |
|---|---|---|---|
| #20 | 🟠 | **`server.py` is a 2.8k-line god-module**: routes, schedulers, merger-discovery algorithm (≈250 lines of Pearson/Jaccard helpers), dedupe utilities, universe logic, startup wiring. | `backend/server.py` |
| #21 | 🟠 | **Operator-token check copy-pasted 10×** (and the keep-alive `_ping` coroutine 2× in server + 1× each in 3 job modules; `_log_*_task_result` 3×). | `server.py`, `fqs_refresh`, `lumpsum_refresh`, `nav_meta_indexer` |
| #22 | 🟠 | **Triplicated math/logic:** WM↔XIRR (`xirr_utils.py`, aliases in `server.py`, `frontend/src/utils/xirr.ts`); EOMONTH/EDATE/percentile/stdev helpers in `fqs_engine` and `lumpsum_engine` (and a third NAV-correlation implementation in `server._md_*`, `fund_lineage`, `predecessor_discovery`); Indian formatting in `indian_format.py` and `format.ts`. | see paths |
| #23 | 🟠 | **Giant screens**: `home.tsx` 2 081 lines (≈20 inline components), `rolling.tsx` 1 194, `fund/[code].tsx` 921 (two screens), `proposal/[id].tsx` 846, `FundDetailShared.tsx` 813. SIP vs Lumpsum fund-detail components duplicate each other. | `frontend/` |
| #24 | ✅ FIXED | **Commented-out previous implementations kept as "diff reference":** `fqs_engine.py` lines 1-~490, `category_map.py` lines 1-~160. New contributors must scroll past dead text; `git` already preserves history. Live code starts at `from pyxirr import xirr` / the second docstring. | files |
| #25 | 🟡 | Hard-coded values scattered: horizon cap 50 (6 places), 180-day stale threshold (2), `tenure+2` buffer (server only; backtest has its own), cron times (server ×3, workflow, DEPLOY.md, UI strings), legal name/ARN defaults (backend ×2 + `RECOMMENDED_BY` in client), `CORR_*` thresholds, curated pair tables in code. | – |
| #26 | 🟡 | Inconsistent logger names (`mf_api`, `mf_api.proposal`, `fqs_refresh`, `__name__`), mixed `Optional`/`|` typing, no lint/format config for Python (no `ruff`/`black`/`mypy` config). | backend |

## C. Dead / stale / misleading assets

| ID | Item | Status |
|---|---|---|
| #30 | ✅ removed: `backend/curated_funds.py` (no importers), `backend/predecessor_discovery.py` (no importers; only `LINEAGE_VALIDATION_REPORT.md` mentions it), `amfi_client.fetch_nav_stitched` (no callers), root `compute_sip.py` (hard-coded `/var/folders/...` path), client `api.refreshRun/refreshStatus/meta/overlapCoverage` | dead — verify then delete |
| #31 | ✅ removed: declared but never imported frontend deps: `react-native-gifted-charts`, `expo-video`, `expo-blur`, `expo-haptics`, `react-native-webview`, `expo-symbols` | remove |
| #32 | Stale docs/comments: `memory/PRD.md` (still the 3-screen MVP and old FQS weights), `ARCHITECTURE.md` (2026-06-30 snapshot — rewritten in this pass), `DEPLOY.md` (single-workflow story, mandatory token), `README.md` (was "# Here are your Instructions"), `frontend/README.md` (Expo boilerplate), `TenureProvider.tsx` header ("capped at 15Y", "3/5/7Y"), `api.ts` `MAX_CALC_YEARS` comment ("capped at 15Y" vs value 50), `tests/test_mf_api.py` docstring, `test_result.md` + `test_reports/*` (Emergent artefacts, not maintained) | refresh |
| #33 | Repo bloat: `backtest_data/` 302 MB, `backend/fqs_all_funds.xlsx` 11 MB + root copy 932 KB, `long_history_universe.{csv,json}`, 4 PDFs; `.easignore` exists solely to avoid shipping them to EAS. On **main**, `frontend/.metro-cache/` (≈23.8 k files) is tracked. | move to release assets / LFS / `.gitignore` |
| #34 | `backend/.env`, `frontend/.env` tracked despite `.gitignore` | see security S7 |
| #35 | Two versions of the product live on two branches (`main` MVP vs `version2`); `main`'s workflow calls endpoints that exist only on `version2` | decide the branch strategy; UNKNOWN which one Render serves |
| #36 | Legacy contract shims that **must stay**: `_LEGACY_ALIAS_KEYS` (`selected_*`) for installed APKs; `MERGER_MAP` name kept for back-compat | keep |

## D. Missing functionality an operator would expect
* Scheduled/automatic NAV-index refresh (new schemes, span refresh) — #1.
* Auth, audit log, data retention/purge for proposals/jobs/runs.
* CI that runs `pytest`, `tsc`, `expo lint`; dependency scanning.
* Observability: structured logs, error tracking, job-status persistence across restarts.
* Frontend automated tests; accessibility pass; tablet/desktop layouts.
* Proposal compliance copy review (UNKNOWN).
