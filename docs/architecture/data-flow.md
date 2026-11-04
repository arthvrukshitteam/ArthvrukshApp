# End-to-end data flows

> Verified against `origin/version2` @ `8a0089a1`. Each flow: user action → frontend → API → backend → DB → external → response → UI.

## 0. Onboarding & global context

```
launch → ModeProvider/TenureProvider read AsyncStorage → ModeGuard
  mode null  → /mode-picker (SIP | Lumpsum)  → ModeProvider.setMode → /tenure-picker
  tenure null→ /tenure-picker (any number ≥1) → TenureProvider.setTenure → /home
```
Mode + tenure are device-local and drive every later screen (Home endpoint choice, Fund Detail variant). There is no server-side user state.

## 1. Home universe (the main screen)

```
UniverseScreen (home.tsx) mounts / toggles change
 → api.universe(mode, tenure, since2010, separateManagement)           [tenure clamped ≤50]
 → GET /api/home/universe?years=N[&since_2010=1][&separate_management=1]      (SIP)
   GET /api/lumpsum/universe?tenure=N[…]                                        (Lumpsum)
 → server.home_universe
     gate: nav_meta_refresh_log/nav_meta must exist else 503
     query fund_nav_meta: history_years ≥ N+2 (1Y: ≥1) AND fund_status.active_query, batch_size 200, trimmed projection
     per doc (streaming): drop IDCW; continuity_applies?; select_horizon_map (continuity > 2010 > inception);
                          read fqs_by_horizon[N]; drop heavy maps; get_display_category; _universe_metrics (+legacy selected_* aliases)
     _dedupe_fund_variants (identical displayed metrics → keep Growth option)
     sort: Debt last → has WM → blended_wm desc
     second query: Additional Recommendations (history 5Y..<required; additional_rec.select_additional_rec; XIRR≥15%)
     envelope + index_last_built / scores_last_refreshed
 ← UniverseResponse → setResult → FlatList of FundCard; chips filter client-side (CORE_SUBS whitelist hides Debt/Index/Gold/Sectoral in "All")
```
Lumpsum variant: eligible funds from `fund_nav_meta`, metrics joined from `fund_lumpsum_analysis` by `$in`.

Failure modes: 503 (index not built) → "Fund index not built yet"; any other error → "Couldn't load funds". Empty `since2010` result tells the user the nightly refresh may be catching up.

## 2. Fund detail

```
tap card → router.push(/fund/{code}?horizon=N[&since2010=1][&mgmt=1][&contDate=YYYY-MM-DD])
 → mode==='lumpsum' ? LumpsumFundDetailScreen : SipFundDetailScreen
 → api.analyze(code,{startDate (2010-01-01 or contDate), includePredecessor(false if mgmt), refresh})
 → GET /api/funds/{code}/analyze
     start_date None & include_predecessor & !refresh → fund_analysis cache (24 h) → touch fund_recent → return
     else _build_analysis → amfi_client.load_analysis_nav → classify_fund → analyze_fund (SYNC, on event loop)
          → _save_analysis (only default run) → mirror into fund_nav_meta → _touch_recent
 ← FundAnalysis → pickHorizon (manual choice > URL horizon > headline > last) → gauge, insights, tables
 secondary: AmcHistorySection → GET /funds/{code}/lineage (404 until lineage build ran → hidden)
 "Add to portfolio" → ProposalDraftProvider.toggleFund / setToggleContext
```
Toggling the predecessor switch forces `refresh` so the backend does not serve the cached full-history doc.

## 3. Search / Funds tab

```
type ≥2 chars (280 ms debounce) → GET /funds/search?q=
  lineage_store.resolve (fund_identifier_idx → fund_lineage) → rows(matched_via=lineage)
  then substring scan of the in-memory AMFI list (12 h cache; Direct + IDCW removed)
  _annotate_fund_status (one $in on fund_nav_meta) → _dedupe_search_variants → trim to limit
```
Funds tab also loads `/funds/recent` and `/refresh/full-status` (the freshness banner). Fund picker modals (proposal add-fund, rolling) reuse `api.search`, passing `activeOnly` where the user is choosing an investment.

## 4. Proposal

```
Home: "Select" mode → toggleFund… → "Build Proposal" → setToggleContext(since2010,mgmt); exitSelectionMode → /proposal/build
build.tsx: client fields, amount, SWP (optional) → api.buildProposal → POST /api/proposal/build
  per fund: toggles on → fund_nav_meta variant doc  ; else fund_analysis cache → live build (sip|lumpsum)
  proposal_engine.build_fund_row (+ extrapolation for short-history Additional Rec funds) → build_mode_projection → [SWP]
  guard: all_funds_excluded → 422
  upsert portfolio_proposals (_id = proposal_id)
 ← ProposalDoc → router.replace(/proposal/{id})
[id].tsx: GET /proposal/{id}; edit allocations/amount/SWP → POST /proposal/build with proposal_id (recalc in place);
          export → GET …/export.pdf|xlsx → FileSystem.downloadAsync + Sharing (native) | window.open (web);
          email → MailComposer with PDF attachment
```

## 5. Rolling analysis

```
rolling.tsx: pick fund(s) (FundSearchModal), dates, window, optional custom buckets
 single:  POST /rolling-analysis/run → load_analysis_nav → _compute_fund_periods (fqs_engine.compute_rolling_windows / lumpsum_engine… per bucket start-date group) → save rolling_analysis_runs
 ranking: POST /rolling-analysis/rank/run → pool from fund_nav_meta (excl. Debt/Gold) + hand-picked, 8-way parallel live NAV fetch, rank by cross-period CV → save rolling_multi_runs
          POST …/add-fund → merge + re-rank ; GET …/rank/{id} ; …/export.pdf
```
Every ranking click performs up to 80 live mfapi.in fetches + scoring in the web process.

## 6. Portfolio review (bulk Excel)

```
pick .xlsx/.xls → web: multipart POST /bulk-analysis/upload | native: base64 POST /upload-base64
 → parse_preview in worker thread → job doc (status pending, workbook bytes stored in Mongo) → preview shown
"Process" → POST /{id}/process → asyncio task process_bulk_job:
     read sheet → build_master_index (AMFI list) → per row (6 parallel, batches of 25):
        match_fund (code → ISIN → name → stem)  →  get_fund_result: fund_nav_meta.fqs_by_horizon → fund_analysis cache → live _build_analysis
        pick 10Y else longest shorter horizon → build_app_values + Remarks
     progress doc updated per batch → build_output_workbook (thread) → status completed, output_bytes saved
UI polls GET /{id}/status (chained timeout) → GET /{id}/download → FileSystem + Sharing
```
Risk: process death mid-job leaves `status:"processing"` forever and `/process` then answers `already_running`.

## 7. Overlap

```
overlap.tsx → GET /overlap/funds (funds with holdings) → pick A, B → GET /overlap/compare?fund_a&fund_b
 → holdings_store.latest_common_period → get_holdings ×2 → overlap_engine.compute_overlap → response
Data entry (operators only, no UI): POST /overlap/ingest {urls} | /ingest-upload {base64}
 → holdings_ingest → holdings_parser.parse_workbook (read-only openpyxl) → ba.match_fund → holdings_store.save_holdings
```

## 8. Nightly pipeline

```
GitHub Actions 22:00 UTC (daily-refresh.yml): POST /refresh/run (sync, ≤1200 s) → POST /fqs/refresh → POST /lumpsum/refresh
in-process (if process alive): 22:00 _daily_refresh ; 22:30 refresh_all_fqs ; 23:00 refresh_all_lumpsum
 fqs_refresh per fund: load_analysis_nav → analyze_fund(include_windows=False) in thread
    → since-inception map + (if first NAV ≤ 2010-01-01) 2010 map + (if predecessor|manual event|lineage AMC_CHANGE) continuity map
    → fund_nav_meta.$set(...)
 batch_support.exclusive serialises FQS vs Lumpsum; iter_batches keyset-pages 100 docs
 fund_status/nav_meta crawl are NOT in the nightly pipeline (crawl is manual) → see technical-debt #1
```

## 9. Operator flows

* `POST /home/reindex` → `nav_meta_indexer.build_nav_meta_index` (resumable, 8 parallel, self-ping keep-alive via `RENDER_EXTERNAL_URL`).
* `POST /admin/merger-discovery` → candidates → `merger_discovery_log` → `refresh_successor_cache` + `sync_superseded`.
* `POST /admin/build-lineage` → `lineage_build.build_lineage` (archive.org + AMFI) → staged swap → cache refresh.
* In-app **Data Refresh** screen → `POST /fqs/refresh` / `/lumpsum/refresh` (+ optional token) and status polling every `POLL_MS` while focused.
