# API reference

> Derived from `backend/server.py` and the four feature routers on `origin/version2` @ `8a0089a1`. Base path is `/api`. No API versioning, no response envelope; errors are FastAPI's `{"detail": "..."}`. All responses JSON unless noted. CORS: `*`, no credentials.
>
> **Auth legend** — `none`: no check. `token*`: *only if env `REFRESH_TOKEN` is set on the server*, caller must pass `?token=<value>` (plain `!=` comparison); if the env var is unset the endpoint is **open**.
>
> **Completeness check:** importing `server.app` and listing its routes yielded exactly 53 application routes at the documented commit (62 after the Portfolio Holding Review router below) (+ `/docs`, `/redoc`, `/openapi.json`, `/docs/oauth2-redirect`); every one is covered below.
>
> The OpenAPI schema is auto-generated at `/docs` and `/openapi.json` on a running server (FastAPI default; not disabled).

## 1. Health, search, fund data

| Method & path | Auth | Purpose / params | Response (key fields) | Errors | DB / external |
|---|---|---|---|---|---|
| `GET /health` | none | liveness + Mongo ping | `{status:"ok", db:"ok"|"down", ts}` (always 200) | – | `db.command("ping")` |
| `GET /funds/search` | none | `q` (min 2 chars, required), `limit` 1–100 (25), `active_only` (false), `dedupe_variants` (true). Lineage-first resolver (old names, old codes, ISIN) then substring scan of AMFI list. ISIN/numeric queries are never collapsed | `{query, results:[{scheme_code, scheme_name, matched_via?, resolved_from?, formerly_known_as?, lineage_confidence?, is_active?, inactive_reason?, current_scheme_code?, variant_count?}]}` | 422 bad params | mfapi.in list (12 h cache), `fund_lineage`, `fund_identifier_idx`, `fund_nav_meta` |
| `GET /funds/recent` | none | `limit` (20, **unbounded**) | `{count, results:[{scheme_code, scheme_name, last_seen, category?, headline_fqs?, …}]}` | – | `fund_recent`, `fund_analysis` |
| `GET /funds/{scheme_code}/meta` | none | cheap metadata (fetches full NAV payload anyway) | `{scheme_code, scheme_name, fund_house, scheme_type, scheme_category, category, earliest_nav_date, latest_nav_date, nav_points}` | 404 | mfapi.in |
| `GET /funds/{scheme_code}/history-debug` | none | how the NAV series was assembled/stitched | own vs effective dates, `predecessor_chain`, `successor_chain`, `stitch_signals`, `history_note` | 404 | mfapi.in |
| `GET /funds/{scheme_code}/analyze` | none | `refresh` (false), `start_date` YYYY-MM-DD, `years` 1–50 (CR variant; **frontend never sends**), `include_predecessor` (true). Cache (24 h) only for default run | full `FundAnalysis` incl. `horizons[].windows`, `wound_up*`, `merged_with`, `predecessor_used/available`, `history_note` | 400 bad date / **Direct plan**, 404, 502 (any compute/upstream failure) | mfapi.in (+ predecessors/successors), writes `fund_analysis`, mirrors `fund_nav_meta.fqs_by_horizon`, `fund_recent` |
| `GET /funds/{scheme_code}/analyze/lumpsum` | none | `refresh`, `start_date`, `include_predecessor`. Serves cache only if windows present | `LumpsumFundAnalysis` | 400, 404, 422 (<36 months), 502 | writes `fund_lumpsum_analysis`, `fund_recent` |
| `GET /funds/{scheme_code}/lineage` | none | `q` optional. Identity/AMC-history explanation | explain object (`identity_history`, `corporate_actions`, `confidence`, `coverage`) | 404 until lineage build has run | `fund_lineage` |
| `GET /categories` | none | internal bucket names | `{categories, default:"Misc Fund"}` | – | – |

## 2. Universe (Home screen)

| Method & path | Auth | Params | Response | Errors |
|---|---|---|---|---|
| `GET /home/universe` | none | `years` 1–50 (required), `since_2010` (false), `separate_management` (false) | envelope `{mode:"sip", since_2010, separate_management, tenure, resolved_horizon, required_history_years, total_funds_indexed, eligible_fund_count, eligible_funds[], bonus_funds[], benchmark_xirr, index_last_built, scores_last_refreshed}`. Each fund: `scheme_code, scheme_name, category, scheme_category, broad_category, amfi_sub, history_years, headline_*, fqs, fqs_tier, blended_wm, success_pct, n_windows, cv, p10_wm, horizon, fqs_exact, continuity_reset_date, continuity_reason` **plus legacy `selected_*` aliases** | 503 until `nav_meta_refresh_log/nav_meta` exists |
| `GET /lumpsum/universe` | none | `tenure` 1–50, same toggles | same envelope with `mode:"lumpsum"` | – (returns empty envelope if nothing eligible) |

Rules: eligible = `history_years ≥ tenure+2` (1Y → ≥1), active per `fund_status`, non-IDCW, deduped by displayed metrics (`_dedupe_fund_variants`), sorted Debt-last then by blended WM. `bonus_funds` = Additional Recommendations. Variant precedence: continuity > since-2010 > since-inception (`continuity_select`).

## 3. Refresh / operator endpoints

| Method & path | Auth | Behaviour |
|---|---|---|
| `GET /refresh/status` | none | last recently-viewed refresh (`refresh_log/daily`) |
| `POST /refresh/run` | token* | **synchronous** recompute of funds viewed in last 30 d → `{schemes_refreshed, duration_seconds}` (CI caps curl at 1200 s) |
| `GET /refresh/full-status` | none | `{nav:{last_run,schemes_refreshed,running}, fqs:{last_run,refreshed,total,running}}` |
| `POST /fqs/refresh` | token* | start SIP batch → `{status:"started"|"already_running"}` |
| `GET /fqs/refresh/status` | none | `{running, last_run, batch_lock_held_by, waiting_for_lock}` |
| `POST /lumpsum/refresh`, `GET /lumpsum/refresh/status` | token* / none | same for lumpsum |
| `POST /home/reindex` | token* | start NAV-meta crawl (resumable) |
| `GET /home/reindex/status` | none | `{running, last_run}` |
| `GET /continuity/events` | none | manual continuity breaks |
| `POST /continuity/events` | token* | body `{scheme_code, break_date, reason, note?}`; keeps the **later** break date; 409 if existing is later; 400 bad date |
| `DELETE /continuity/events/{scheme_code}` | token* | delete |
| `GET /export/all-funds` | none | `min_history_years` 0.5–20 (1.0) → all scored funds + `fqs_by_horizon` (whole universe in one response) |
| `GET /export/status` | none | readiness counts |
| `POST /admin/merger-discovery` | token* | start background discovery |
| `GET /admin/merger-discovery/status` | token* | running / result (also from `merger_discovery_log`) |
| `POST /admin/fund-status/sync` | token* | re-stamp `superseded_by` universe-wide |
| `GET /admin/fund-status` | **none** | counts + sample of hidden funds (`limit` 1–500) |
| `POST /admin/build-lineage` | token* | `quick` (false), `hist_years` 1–30 (20); long background crawl |
| `GET /admin/build-lineage/status` | **none** | `{running, last_result}` |

## 4. Proposal (`/api/proposal`) — `proposal_routes.py`

| Method & path | Auth | Request | Response / errors |
|---|---|---|---|
| `POST /proposal/build` | none | `{proposal_id?, client_name?, client_phone?, client_pan?, advisor_name?, proposal_date?, mode:"sip"|"lumpsum", horizon 1–50, investment_amount>0, since_2010, separate_management, funds:[{scheme_code, allocation_pct (0,1]}] (1–10, Σ=1±0.01), swp?:{corpus?, annual_return?, inflation 0.06, monthly_withdrawal>0, years 1–40}}` | `ProposalDoc` (`proposal_id`, `plan.funds[]`, `plan.aggregate`, `swp`, `warnings`, `created_at`). 422 when no selected fund has history at the horizon, or SWP defaults cannot be derived; 502 per-fund analysis failure. Upserts `portfolio_proposals` (`_id` = given or `uuid4().hex[:16]`) |
| `GET /proposal/{id}` | none | – | doc (404) |
| `GET /proposal/{id}/export.xlsx` | none | – | xlsx attachment `Client_PAN_15Y_SIP_10000.xlsx` |
| `GET /proposal/{id}/export.pdf` | none | – | pdf attachment |

Per-fund data source: if a toggle is on → `fund_nav_meta` variant doc; else `fund_analysis` cache → live `_build_analysis` / `_build_lumpsum_analysis`.

## 5. Rolling analysis (`/api/rolling-analysis`) — `rolling_analysis_routes.py`

| Method & path | Request | Notes |
|---|---|---|
| `POST /run` | `{scheme_code, start_date, end_date, window_years 1–30 (5), mode, buckets?[≤20 {start_date,end_date,label?}]}` | 400 bad date; 422 end≤start / range < window / not enough history; 502/404 NAV fetch. Saves `rolling_analysis_runs` (id = uuid). Returns `periods[]`, `overall`, `key_points[]`, `analysis_id` |
| `GET /{analysis_id}`, `GET /{analysis_id}/export.pdf` | – | 404 |
| `POST /rank/run` | `{start_date, end_date, window_years, mode, buckets?, pool_size 0–80 (30), scheme_codes?[≤80]}` | auto pool excludes Debt & Gold/Silver, sorted by NAV depth; concurrency 8; ≤80 funds total; ranks by lowest cross-period CV. Saves `rolling_multi_runs` |
| `POST /rank/{multi_id}/add-fund` | `{scheme_code}` | scores one more fund against the run's parameters |
| `GET /rank/{multi_id}`, `GET /rank/{multi_id}/export.pdf` | – | |

## 6. Bulk analysis (`/api/bulk-analysis`) — `bulk_analysis_routes.py`

| Method & path | Request | Response / errors |
|---|---|---|
| `POST /upload` | multipart `file` (.xlsx/.xls, ≤10 MB) | `{job_id, filename, total_funds, detected_funds[]}`; 400 bad type/empty/too large/unreadable. Stores workbook bytes **inside the job document** |
| `POST /upload-base64` | `{filename, content_base64}` | same (native clients) |
| `POST /{job_id}/process` | – | `{status:"started"|"already_running"|"already_completed"}`; 404; 409 nothing to process |
| `GET /{job_id}/status` | – | counters, `status` pending/processing/completed/failed, `not_found_funds[]` |
| `GET /{job_id}/download` | – | xlsx; 409 until completed |

Row→fund match ladder: code → ISIN → normalised name → name stem (`match_fund`); FQS read at 10Y else longest shorter horizon (`FQS (6 YR)` annotation); Remarks via `get_fund_remark`.

## 7. Overlap (`/api/overlap`) — `overlap_routes.py`

| Method & path | Auth | Request | Notes |
|---|---|---|---|
| `GET /funds` | none | `period?` | funds with holdings on file (latest period per scheme) |
| `GET /coverage` | none | – | `{documents, funds, periods, latest_period}` |
| `GET /compare` | none | `fund_a`, `fund_b` (required, must differ), `period?` YYYY-MM | defaults to newest month **both** funds disclose; 400 same fund; 404 missing disclosure / no common month. Returns `overlap_pct` (equity-rebased headline), `overlap_pct_raw`, `count_overlap_pct`, `common[]` sorted by contribution |
| `GET /sources` | none | – | best-effort scrape of AMFI disclosure index + `AMC_SOURCES` (currently `{}`) |
| `POST /ingest` | **none** | `{urls[], period?}` | **server fetches arbitrary URLs** (follows redirects, 40 MB cap) → SSRF surface |
| `POST /ingest-upload` | **none** | `{filename, content_base64, period?, amc?}` | ≤40 MB; 422 parse error |

## 8. Frontend caller map

`src/api.ts` method → endpoint: `search`→`/funds/search`; `recent`→`/funds/recent`; `lineage`→`/funds/{c}/lineage`; `analyze`→`/funds/{c}/analyze`; `analyzeLumpsum`→`/funds/{c}/analyze/lumpsum`; `universe`→`/home/universe`|`/lumpsum/universe`; `fullRefreshStatus`; `fqsRefreshStatus`/`triggerFqsRefresh`; `lumpsumRefreshStatus`/`triggerLumpsumRefresh`; `buildProposal`/`getProposal`/`proposalExportUrl`; `runRollingAnalysis`/`rollingAnalysisExportUrl`; `runRollingRank`/`addRankedFund`/`getRollingRank`/`rollingRankExportUrl`; `bulkAnalysis*`; `overlapFunds`/`overlapCompare`. Never called by the client: `/funds/{c}/meta`, `/history-debug`, `/categories`, `/refresh/run`, `/refresh/status`, `/export/*`, `/continuity/*`, `/home/reindex*`, `/admin/*`, `/overlap/{coverage,sources,ingest,ingest-upload}`.

## 9. Compatibility contracts (do not break)

* `selected_*` aliases on both universe responses (old APKs in the field).
* Response shape of `/funds/{c}/analyze` is consumed by `proposal_engine.extract_fund_metrics`, `bulk_analysis._pick_horizon_from_list`, and the Fund Detail screen.
* `fund_nav_meta.fqs_by_horizon` map shape is consumed by universe, proposal variants, bulk analysis, export.

## 10. Portfolio Holding Review (`/api/portfolio-review`) — `holding_review_routes.py`
Added after the audit; full design in [portfolio-holding-review.md](../features/portfolio-holding-review.md). All unauthenticated; PAN is masked in list output.

| Method & path | Purpose | Errors |
|---|---|---|
| `POST /upload` `{filename, content_base64}` | parse a proposal/review PDF → draft review | 400 bad base64 / not a PDF; 413 >10 MB; 422 scanned, corrupt or non-proposal PDF |
| `GET /` (`limit`) | recent reviews (summary rows) | |
| `GET /{id}` | review + `validation{errors,warnings}` | 404 |
| `PUT /{id}` | advisor edits (client, dates, plan, market, funds, observations, `confirm_all`) | 404; 409 finalized; 422 bad date / unknown scheme |
| `POST /{id}/fetch-current` | current metrics per fund via the existing analysis | 409 finalized |
| `POST /{id}/observations/generate` | auto observations (manual ones preserved) | 409 finalized |
| `POST /{id}/finalize` | freeze | 422 validation / current data missing |
| `POST /{id}/revise` | editable copy of a review | 404 |
| `GET /{id}/export.pdf` | report PDF | 422 validation errors |
