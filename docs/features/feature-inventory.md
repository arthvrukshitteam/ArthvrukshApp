# Feature inventory

> `origin/version2` @ `8a0089a1`. **Roles:** there are no roles — every feature below is available to any caller (see [authentication](../architecture/authentication.md)); "operator" = whoever calls the token-gated/admin endpoints. **Status** values: Fully implemented · Partially implemented · UI only · Backend only · Deprecated · Dead/unused · Unclear.
>
> Production-readiness is judged from code + the 484 passing backend tests; nothing was exercised against a live server or real Mongo in this pass.

## Legend per feature
`FE` frontend location · `API` endpoints · `DB` collections · `EXT` external services · `Rules` business rules/validation · `States` loading/empty/error handling · `Limits/Risks`.

---

### F1. Onboarding (mode + tenure) — Fully implemented, FE-only
* FE: `app/mode-picker.tsx`, `app/tenure-picker.tsx`, `ModeProvider`, `TenureProvider`, `ModeGuard` in `app/_layout.tsx`. API/DB/EXT: none.
* Rules: tenure integer ≥ 1 (UI accepts any; network clamps to 50). Mode persisted in AsyncStorage.
* Limits: changing mode mid-session re-routes to tenure picker; no server-side profile. Stale comments (15Y cap, 3/5/7Y) in `TenureProvider`.
* Ready: yes.

### F2. Home — ranked fund universe (SIP & Lumpsum) — Fully implemented
* FE: `(drawer)/(tabs)/home.tsx` (2 081 lines). API: `GET /home/universe`, `GET /lumpsum/universe`. DB: `fund_nav_meta`, `fund_lumpsum_analysis`, `nav_meta_refresh_log`, `fqs_refresh_log`, `lumpsum_refresh_log`. EXT: none at request time.
* Workflow: choose tenure → list of eligible funds ranked by Blended WM → filter chips (broad → AMFI sub-category), name search, custom benchmark-XIRR slider (client recompute of pass count), two toggles (Since 2010; Separate Management Periods), Additional Recommendations section, select-mode for proposals.
* Rules: eligibility `history_years ≥ tenure+2`; Debt last; variant dedupe; "All" view shows only `CORE_SUBS` + Hybrid.
* States: skeleton, error, empty (toggle-specific message), pull-to-refresh, data-age strip (`formatDataAge`, stale after 36 h).
* Limits/Risks: whole universe in one payload; depends on nightly batch being fresh and on `fund_nav_meta` span fields (technical-debt #1); `home.tsx` is a monolith.
* Ready: yes (core).

### F3. Funds tab — search + recents — Fully implemented
* FE: `(drawer)/(tabs)/index.tsx`, `FundSearchModal`. API: `/funds/search`, `/funds/recent`, `/refresh/full-status`. DB: `fund_identifier_idx`, `fund_lineage`, `fund_nav_meta`, `fund_recent`, `fund_analysis`. EXT: mfapi.in master list.
* Rules: min 2 chars; searches historical names, old codes, ISINs; dead funds labelled not hidden (`active_only` for pickers); variants collapsed (not for code/ISIN queries).
* Risks: unbounded `limit` on `/funds/recent`; fall-back scan is O(37k) per request in Python.
* Ready: yes.

### F4. Fund detail (SIP / Lumpsum) — Fully implemented
* FE: `app/fund/[code].tsx`, `FundDetailShared`, `HorizonInsights`, `FqsGauge`, `CollapsibleSection`. API: `/funds/{c}/analyze`, `/analyze/lumpsum`, `/lineage`. DB: `fund_analysis`, `fund_lumpsum_analysis`, `fund_nav_meta`, `fund_lineage`. EXT: mfapi.in (live fetch on cache miss).
* Workflow: horizon chips (every horizon with data), optional start-date filter, "old vs new management" switch (only when a verified predecessor exists), simple vs analyst density, windows table (30/page), AMC history timeline, add-to-portfolio.
* Rules: Direct plans rejected (400); cache only for default run; wound-up funds stitched forward; honest `history_note` disclosure.
* Limits/Risks: `_build_analysis` computes synchronously on the event loop; SIP and Lumpsum screens duplicate ~40 % of their code.
* Ready: yes.

### F5. Methodology — Fully implemented (static)
* FE: `(drawer)/insights.tsx` (reachable only from the drawer). No API. Content is hand-written copy and **can drift from code** (e.g. it contains a duplicated "Daily NAV (AMFI)" card at two list positions; weights shown must be re-checked against `score_horizon` when changed).

### F6. Portfolio proposal (build, review/edit, SWP, export, email) — Fully implemented
* FE: Home select-mode → `proposal/build.tsx` → `proposal/[id].tsx`. API: `/proposal/*`. DB: `portfolio_proposals` (+ reads `fund_nav_meta`, `fund_analysis`, `fund_lumpsum_analysis`). EXT: mfapi.in on cache miss; device mail app.
* Rules: ≤10 funds, Σ allocation 100 % ±1 %, one mode + one horizon (1–50), median & P10 wealth, blended XIRR, optional SWP (120-month amortisation, constant withdrawal incl. inflation uplift), short-history Additional-Rec funds extrapolated at their own XIRR and asterisked, 422 when nothing has history. Legal name/ARN (`ARTHVRUKSH_COMPANY_NAME`, `ARTHVRUKSH_ARN`, default ARN `365554`) printed on exports.
* Limits/Risks: PII (name/phone/PAN) unauthenticated and plaintext; PDF/XLSX builders have no tests; `FundSearchModal` prevents duplicate funds client-side only (backend has no per-proposal uniqueness check — `ProposalRequest` validator checks count and sum only).
* Ready: functional; compliance/PII posture needs review.

### F7. Rolling analysis — single fund & multi-fund ranking — Fully implemented
* FE: `(drawer)/(tabs)/rolling.tsx`. API: `/rolling-analysis/*`. DB: `rolling_analysis_runs`, `rolling_multi_runs`, `fund_nav_meta` (pool). EXT: mfapi.in (live, ≤80 funds per ranking click).
* Rules: window 1–30 Y; ≤20 custom buckets; default one bucket per calendar year; empty buckets returned with `n_windows:0`; ranking by lowest cross-period CV of Blended WM (needs ≥2 scored periods); Debt/Gold-Silver excluded from auto pool.
* Limits/Risks: **no tests**; heavy live computation per click; results persisted forever.
* Ready: beta-quality.

### F8. Portfolio Review (bulk Excel) — Fully implemented
* FE: `(drawer)/(tabs)/portfolio-review.tsx`. API: `/bulk-analysis/*`. DB: `bulk_analysis_jobs`, `fund_nav_meta`, `fund_analysis`. EXT: mfapi.in (fallback tier).
* Rules: .xlsx/.xls ≤10 MB; header row auto-detected in first 10 rows; keyword column matching so advisor columns are reused not duplicated; Allocation/Fund Name/Category are user-owned (only filled when blank); FQS at 10Y else longest shorter horizon; Remarks thresholds 75/85.
* Limits/Risks: job state lost on restart; uploads stored in Mongo and never purged; Expo-Go-on-Android file-picking limitation noted in code.
* Ready: yes, with the operational caveats.

### F9. Portfolio Overlap — **Partially implemented**
* FE: `(drawer)/(tabs)/overlap.tsx`. API: `/overlap/*`. DB: `fund_holdings`. EXT: AMC/AMFI disclosure workbooks (operator-supplied URLs).
* Works end-to-end **only for funds whose monthly disclosure has been ingested**. There is no in-app ingestion UI, `AMC_SOURCES` is an empty registry, and the ingest endpoints are public (SSRF/abuse surface). Coverage in production: **UNKNOWN** (`GET /api/overlap/coverage`).
* Math is well specified and tested (`test_overlap_engine`, `test_overlap_pipeline`, `test_holdings_parser`).

### F10. Data Refresh screen — Fully implemented
* FE: `app/data-refresh.tsx`. API: `/fqs/refresh[/status]`, `/lumpsum/refresh[/status]`. Optional token field (SecureStore; plain AsyncStorage on web). Polls while focused.

### F11. Fund identity / lineage / merger stitching / dead-fund rule — Fully implemented (backend-heavy, operator-driven)
* Backend: `amfi_client.load_analysis_nav`, `fund_lineage`, `lineage_store`, `lineage_build`, `amfi_history`, `fund_status`, `continuity_*`; admin endpoints; `scripts/*`. DB: `fund_lineage*`, `fund_identifier_idx`, `fund_successor_map`, `predecessor_map`, `merger_discovery_log`, `fund_continuity_events`, `fund_nav_meta`.
* Rules: stitch accepted only if the successor outlives the predecessor by ≥180 d, concurrent fraction ≤0.75, and either correlation ≥0.90 (discovered) / ≥0.80 (curated) or (no overlap) curated with gap ≤120 d / ISIN-verified with seam step ≤10 %; chain depth ≤4. Hidden when `superseded_by`/`merged_with` or own last NAV older than 180 d.
* Risks: `fund_nav_meta` freshness (see technical-debt #1); `CURATED_*` tables are code-resident (redeploy to change).

### F12. Nightly refresh pipeline — Implemented, reliability uncertain
* See [data-flow §8](../architecture/data-flow.md). In-process schedulers + GitHub Actions. Historical failure: cron ran 8+ days failing (workflow demanded an unset secret) — fixed in `f74ae9bf` on version2 / `4a9e0e9d` on main. **Current Render behaviour UNKNOWN.**

### F13. Bulk export of scored funds — Backend only
* `GET /export/all-funds`, `/export/status`; consumer is an offline script (not in repo; `export_mongo_to_excel.py` is a different, local-DB exporter). `stability_check` field is never populated (see backend.md §7).

### F14. Theme & density — Fully implemented (FE)
* `ThemeProvider` (light/dark/system), `ViewModeProvider` (simple/analyst).

### F15. Offline backtest toolkit — Separate, not part of the running app
* `backtest/`, `backtest_data/` (302 MB), PDFs and `BACKTEST_REPORT_5_FUNDS.md`. The committed report itself states the fixture run has ~2 independent periods and "not interpretable" rank metrics. Not wired to the API.

### F16. Dead / deprecated code (do not build on)
`backend/curated_funds.py`, `backend/predecessor_discovery.py` (unimported), `amfi_client.fetch_nav_stitched`, root `compute_sip.py` (hard-coded local path), `api.refreshRun/refreshStatus/meta/overlapCoverage` in the client, `README.md`'s "Here are your Instructions" stub (now replaced), duplicate `fqs_all_funds.xlsx` ×2.

## Feature × layer summary

| Feature | Screen | Primary endpoints | Collections | Engine |
|---|---|---|---|---|
| F2 Home | home.tsx | universe ×2 | fund_nav_meta, fund_lumpsum_analysis | fqs_refresh/lumpsum_refresh outputs |
| F3 Search | index.tsx | search, recent | lineage*, fund_nav_meta, fund_recent | lineage_store |
| F4 Detail | fund/[code].tsx | analyze ×2, lineage | fund_analysis, fund_lumpsum_analysis | fqs_engine / lumpsum_engine |
| F6 Proposal | proposal/* | proposal/* | portfolio_proposals | proposal_engine |
| F7 Rolling | rolling.tsx | rolling-analysis/* | rolling_* | fqs_engine + lumpsum_engine |
| F8 Review | portfolio-review.tsx | bulk-analysis/* | bulk_analysis_jobs | bulk_analysis |
| F9 Overlap | overlap.tsx | overlap/* | fund_holdings | overlap_engine |
