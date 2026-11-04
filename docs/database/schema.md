# Database schema (MongoDB)

> Reconstructed from every `db.<collection>` access on `origin/version2` @ `8a0089a1`. MongoDB is schemaless: **there are no schema validators, no migrations, no TTL indexes, no foreign keys, no transactions, no soft deletes.** Shapes below are what the code writes. Live production contents/indexes beyond those created in code are **UNKNOWN / REQUIRES VERIFICATION** (production credentials are not available to the dev environment; `backend/.env` was deliberately not read).

Connection: `MONGO_URL` (default `mongodb://localhost:27017` with a logged warning) and `DB_NAME` (default `mf_analyst`); one global `AsyncIOMotorClient` in `server.py`, passed explicitly to other modules.

## 1. Collections overview

| Collection | `_id` | Written by | Read by | Lifecycle |
|---|---|---|---|---|
| `fund_nav_meta` | ObjectId (natural key `scheme_code`) | `nav_meta_indexer` (crawl), `fqs_refresh` (nightly), `_save_analysis` (on view), `fund_status.sync_superseded` | universe endpoints, search status, proposal variants, bulk analysis, export, rolling-rank pool, merger discovery | one doc per scheme (~37k); never deleted |
| `fund_analysis` | ObjectId | `_save_analysis` (default run only) | `/analyze` cache, `/funds/recent`, proposal & bulk (cache tier) | 24 h logical TTL (checked in code, **no Mongo TTL index**); grows with every viewed fund |
| `fund_lumpsum_analysis` | ObjectId | `_build_lumpsum_analysis`, `lumpsum_refresh` | `/analyze/lumpsum`, `/lumpsum/universe`, proposal | one doc per scheme; the nightly job stores **empty `windows`**, on-demand run stores full windows |
| `fund_recent` | ObjectId | `_touch_recent` on every analyze | `/funds/recent`, `_daily_refresh` | **never pruned** |
| `refresh_log` | `"daily"` | `_daily_refresh_inner` | `/refresh/status` | singleton |
| `fqs_refresh_log` | `"fqs_refresh"` | `fqs_refresh` | status/universe | singleton |
| `lumpsum_refresh_log` | `"lumpsum_refresh"` | `lumpsum_refresh` | status/universe | singleton |
| `nav_meta_refresh_log` | `"nav_meta"` | `nav_meta_indexer` | `/home/universe` (503 gate), status | singleton |
| `fund_continuity_events` | ObjectId | `POST /continuity/events` | nightly jobs | manual curation; starts empty |
| `portfolio_proposals` | `proposal_id` (uuid4 hex[:16]) | `/proposal/build` (upsert) | `/proposal/*` | **contains client PII**; never deleted |
| `rolling_analysis_runs` | `analysis_id` (uuid hex[:16]) | `/rolling-analysis/run` | GET + PDF | never deleted |
| `rolling_multi_runs` | `multi_id` | `/rank/run`, `/rank/{id}/add-fund` | GET + PDF | never deleted |
| `bulk_analysis_jobs` | uuid4 | upload / process | status / download | **stores raw upload + output workbook bytes**; never deleted |
| `fund_holdings` | `"{scheme_code}:{YYYY-MM}"` | `holdings_store.save_holdings` | overlap routes | one doc per scheme-month |
| `fund_lineage` | ObjectId; unique `scheme_code` | `lineage_store.persist` (staged swap) | search, continuity floor, lineage endpoint | rebuilt by `POST /admin/build-lineage` |
| `fund_identifier_idx` | ObjectId | same | search `lineage_store.resolve` | rebuilt |
| `fund_lineage_review` | ObjectId; unique `scheme_code` | same | – (review queue) | rebuilt |
| `fund_lineage_meta` | `"current"` + content-hash ids | same | – | version ledger (kept) |
| `fund_successor_map` | – | operator scripts only: `scripts/promote_successors.py`, `scripts/verify_and_promote.py` (via `lineage_store._bulk_load`) | `refresh_successor_cache` (`status:"promoted"`) | operator-managed; the running server never writes it |
| `predecessor_map` | – (unique `successor_code` if created by `predecessor_discovery`) | only `predecessor_discovery.py`, which **nothing imports** | `refresh_successor_cache` (`status:"auto"`) | likely legacy/empty — **production contents UNKNOWN** |
| `merger_discovery_log` | `"latest"` | `_run_merger_discovery` | `refresh_successor_cache` (HIGH/VERY HIGH), status | singleton |

## 2. Key documents

### `fund_nav_meta` (the hub)
```
scheme_code:int  scheme_name  fund_house  scheme_type  scheme_category  category(internal bucket)
earliest_nav_date, latest_nav_date (ISO, STITCHED span)   history_years:float (=(latest-earliest)/365.25)
own_earliest_nav_date, own_latest_nav_date, own_nav_points (this code only)
nav_points  merged_with  predecessor_used:[int]  history_note  indexed_at          ← nav_meta_indexer
superseded_by:int, is_active:false                                                ← fund_status.sync_superseded
headline_fqs, headline_tier, headline_horizon, cycle_robustness, cycle_robustness_variant,
last_fqs_refresh                                                                  ← fqs_refresh / _save_analysis
fqs_by_horizon:        { "<H>": {fqs, tier, blended_wm, median_wm, p10_wm, success_pct, n_windows, cv, alpha_trend, stability_score, stress_score} }
fqs_by_horizon_2010:   same shape, start forced to 2010-01-01 (null if first NAV > 2010-01-01)
fqs_by_horizon_continuity + continuity_reset_date + continuity_reason             ← only funds with a break
```
Indexes created in code: `scheme_code`; `(history_years, own_latest_nav_date)`; `latest_nav_date` (`fund_status.ensure_indexes`, run at every boot). Hot filters not covered: `superseded_by`, `merged_with`. The old ARCHITECTURE.md statement "no createIndex anywhere" is **no longer true**.

### `fund_analysis`
Full `analyze_fund` output + wrapper fields (`scheme_*`, `fund_house`, `wound_up*`, `merged_with`, `predecessor_*`, `history_note`) + `scheme_code`, `computed_at` (ISO string). Each horizon carries its **full windows array** (up to hundreds of rows × up to 50 horizons) — large docs, well under 16 MB for realistic funds but unbounded in principle. Key: `{scheme_code, start_date_applied:null}` (no index declared).

### `fund_lumpsum_analysis`
`scheme_code, scheme_name, category, is_equity, monthly_points, available_horizons, horizons[], horizons_2010[], horizons_continuity[], continuity_reset_date/reason, headline_*, cycle_robustness(+variant), min_xirr_req, cap, computed_at`. No index declared on `scheme_code` (the universe query uses `$in` on it).

### `portfolio_proposals`
`proposal_id, client_name, client_phone, client_pan, advisor_name, proposal_date, mode, horizon, since_2010, separate_management, plan{mode, investment_amount, horizon, funds[], aggregate}, swp|null, warnings[], created_at` — **PAN, phone and name stored in plaintext**.

### `bulk_analysis_jobs`
`status (pending|processing|completed|failed), filename, total, processed, matched, not_found, errors, preview_funds[], not_found_funds[], created_at, finished_at, workbook_bytes` (removed on completion), `output_bytes`.

### `fund_holdings`
`scheme_code, scheme_name, period, amc, source_url, equity_count, equity_weight, holdings[{name, isin, weight_pct, quantity, value}], fetched_at`; indexes `(scheme_code, period desc)`, `(period desc)`.

### `fund_lineage` (+ `fund_identifier_idx`)
`scheme_code, current_name, current_isin, current_amc, scheme_type, name_history[], isin_history[], amc_history[], corporate_actions[{type, effective_date, from, to, successor_code, source, confidence}], observed_from/to, active, successor_code, predecessor_codes[], review[], coverage_start, lineage_version, generator_version, generated_at`. Action types: NAME_CHANGE, NAME_COSMETIC, NAME_TERMINOLOGY, AMC_CHANGE, ISIN_CHANGE, RECATEGORISATION, MERGER, WOUND_UP. Sources: AMFI_SNAPSHOT, CURATED, CORRELATION.

## 3. Relationships

```
fund_nav_meta.scheme_code ──1:0..1── fund_analysis.scheme_code (start_date_applied=null)
                         ──1:0..1── fund_lumpsum_analysis.scheme_code
                         ──1:0..1── fund_lineage.scheme_code ── fund_identifier_idx.scheme_code (1:N)
                         ──1:0..N── fund_holdings.scheme_code (per period)
                         ──1:0..1── fund_continuity_events.scheme_code
fund_recent.scheme_code → fund_analysis (enrichment join done in Python)
portfolio_proposals.plan.funds[].scheme_code → fund_nav_meta/fund_analysis (not enforced)
fund_successor_map / predecessor_map / merger_discovery_log → in-memory amfi_client maps (loaded at boot + after discovery/build)
```
All relations are by `scheme_code` convention only; no referential integrity.

## 4. Integrity & write-safety notes

* `_save_analysis` performs two un-transacted `update_one`s (`fund_analysis` upsert, `fund_nav_meta` set); a crash between them leaves the mirror stale (harmless — rewritten nightly).
* `fund_nav_meta` has **four independent writers** (crawler → span fields; `sync_superseded` → `superseded_by`/`is_active`; nightly FQS and the on-view mirror in `_save_analysis` → the same score fields `fqs_by_horizon`, `headline_*`, `cycle_robustness*`, `last_fqs_refresh`). For the score fields last writer wins: a Fund-Detail view overwrites the nightly map with the on-view result (same engine, so equal unless the code changed between). Note the on-view mirror does not write `fqs_by_horizon_2010/_continuity`, so those stay nightly-only.
* `fund_nav_meta.update_one` in `_save_analysis` has no `upsert` — viewing a fund that is not indexed updates nothing there.
* Lineage persist uses a staging collection swap (`__staging`), so readers never see a half-built lineage.
* Nothing ever TTL-expires: `fund_recent`, `fund_analysis`, `portfolio_proposals`, `rolling_*`, `bulk_analysis_jobs` all grow monotonically. On Atlas M0 (512 MB) this is a capacity risk (`bulk_analysis_jobs` is the fastest grower: up to 10 MB in + output per upload).
