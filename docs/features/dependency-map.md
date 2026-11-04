# Feature dependency map

Purpose: before changing a module, see what else will move. ⚠ = shared dependency used by several features (highest blast radius).

## 1. Layered chain per feature

```
Home (F2)            home.tsx → api.universe → GET /home|lumpsum/universe → server.home_universe/lumpsum_universe
                       → fund_status ⚠ · continuity_select ⚠ · additional_rec ⚠ · category_map.get_display_category
                       → fund_nav_meta ⚠ / fund_lumpsum_analysis  ← fqs_refresh / lumpsum_refresh ← fqs_engine ⚠ / lumpsum_engine ⚠ ← amfi_client.load_analysis_nav ⚠ ← mfapi.in

Fund detail (F4)     fund/[code].tsx → GET /funds/{c}/analyze[/lumpsum] → _build_analysis ⚠ → load_analysis_nav ⚠ → classify_fund ⚠ → analyze_fund ⚠
                       → fund_analysis ⚠ (cache) ; mirrors into fund_nav_meta ⚠ ; fund_recent
                     AMC history → GET /funds/{c}/lineage → fund_lineage

Search (F3)          index.tsx / FundSearchModal ⚠ → GET /funds/search → lineage_store.resolve → fund_identifier_idx → fund_lineage
                       + amfi_client.fetch_fund_list ⚠ (mfapi.in) + fund_status ⚠ + bulk_analysis.match_key ⚠

Proposal (F6)        build.tsx / [id].tsx → POST /proposal/build → proposal_routes → _build_analysis/_cached_analysis ⚠ (injected) or fund_nav_meta variants (continuity_select ⚠)
                       → proposal_engine → additional_rec ⚠ · xirr_utils ⚠ · category_map ⚠ → portfolio_proposals → proposal_pdf / proposal_xlsx → indian_format

Rolling (F7)         rolling.tsx → POST /rolling-analysis/* → load_analysis_nav ⚠ → fqs_engine.compute_rolling_windows/score_horizon ⚠ + lumpsum_engine ⚠ → rolling_*_runs → *_pdf

Bulk review (F8)     portfolio-review.tsx → /bulk-analysis/* → bulk_analysis.match_fund ⚠ → fund_nav_meta.fqs_by_horizon ⚠ → _cached_analysis ⚠ → _build_analysis ⚠ → bulk_analysis_jobs

Overlap (F9)         overlap.tsx → /overlap/* → overlap_engine ; ingest → holdings_parser → bulk_analysis.match_fund ⚠ → fund_holdings

Nightly (F12)        GH Actions / schedulers → server.* refresh endpoints → fqs_refresh / lumpsum_refresh → batch_support → load_analysis_nav ⚠ → engines ⚠ → fund_nav_meta ⚠
                     continuity: continuity_floor ⚠ ← fund_lineage (AMC_CHANGE) · fund_continuity_events · predecessor stitch

Lineage/Status (F11) admin endpoints → lineage_build → amfi_history + archive.org → lineage_store → fund_lineage* ; merger discovery → merger_discovery_log
                     → refresh_successor_cache → amfi_client._DISCOVERED_* ⚠ → fund_status.sync_superseded ⚠ → fund_nav_meta.superseded_by ⚠
```

## 2. Shared dependencies (change = many features affected)

| Shared thing | Used by | If you change it… |
|---|---|---|
| `amfi_client.load_analysis_nav` | SIP analyze, lumpsum analyze, nightly FQS, nightly lumpsum, NAV crawl, rolling analysis, proposals (via builders), bulk (via builders) | every score and `history_years` changes; nightly jobs must be re-run |
| `fqs_engine.score_horizon` / `compute_rolling_windows` | analyze, nightly, rolling analysis, proposals, bulk, backtest | all FQS values; `test_model_audit_spec` clean-room spec will fail on purpose |
| `lumpsum_engine.*` | lumpsum analyze/universe/nightly, rolling (lumpsum mode), proposals (lumpsum) | lumpsum scores; no unit tests protect it |
| `category_map.CATEGORY_TABLE` / `classify_fund` / `max_plausible_wm` | all engines, bulk (Benchmark XIRR), proposal engine, rolling | benchmarks → every Success %/FQS; re-run batches |
| `fund_nav_meta` document shape | universe, proposal variants, bulk, export, rolling pool, merger discovery, search status | 6+ readers; `fqs_by_horizon` entry keys are a de-facto API |
| `_build_analysis` / `_cached_analysis` (server.py) | analyze endpoint, daily refresh, proposal, bulk | injected into routers — signature must stay compatible |
| `fund_status.active_query/merge_query` | both universe endpoints (core + additional recs) | which funds are offered |
| `continuity_select` + `continuity_floor` | universes, proposals, nightly jobs | Home vs Proposal numbers can diverge if edited in one place |
| `additional_rec.select_*` | universe endpoints, proposal engine | Additional Rec list vs proposal display must agree |
| `xirr_utils` ↔ `server.py` aliases ↔ `frontend/src/utils/xirr.ts` | proposal, universe bonus XIRR, Home benchmark bar | three copies — change all three |
| `indian_format.py` ↔ `frontend/src/utils/format.ts` | PDF/XLSX vs app | numbers shown differently to clients |
| `_universe_metrics` + `_LEGACY_ALIAS_KEYS` | Home (current + legacy APKs) | old APKs go blank |
| `bulk_analysis.match_fund`/`match_key`/`_variant_rank` | bulk review, holdings ingest, search dedupe | fund identity resolution everywhere |
| `batch_support.exclusive` | FQS + lumpsum batches | job overlap → OOM on 512 MB |
| Mongo `fund_analysis` cache | detail, proposals, bulk | stale/default-only semantics |
| mfapi.in availability / schema | everything that computes | single point of failure, no fallback |
| `EXPO_PUBLIC_BACKEND_URL` | entire client | baked at build time |

## 3. Cross-layer coupling traps

* Home passes `since2010`/`mgmt`/`contDate` to Fund Detail via **URL params**, and to Proposals via **ProposalDraftProvider**; Fund Detail reconstructs the equivalent via live `startDate` + `includePredecessor` calls. Three code paths must stay numerically equivalent to the nightly `fqs_by_horizon_2010/_continuity` computation.
* `GET /funds/{c}/analyze` default path both *serves* and *seeds* `fund_nav_meta`; so Fund-Detail views silently overwrite nightly values for that fund.
* Frontend types in `src/api.ts` are hand-maintained mirrors of Python dict shapes — no codegen; renames break silently at runtime (this already caused one production incident, see `_LEGACY_ALIAS_KEYS` comment).
