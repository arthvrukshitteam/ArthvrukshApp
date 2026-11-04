# ArthVruksh — Architecture Reference

> **Revised 2026-10-07** against `origin/version2` @ `8a0089a1`. This file is now a short orientation page; the detailed, verified documentation lives under [`docs/`](docs/SYSTEM_CONTEXT.md). The previous version (2026-06-30) was partly stale — see "Corrections" below. Working brief for AI sessions: [`CLAUDE_CONTEXT.md`](CLAUDE_CONTEXT.md).

## Summary

ArthVruksh ("Money Tree") is a mobile-first mutual-fund analyst tool that replicates an in-house Excel MF Analysis Model: any AMFI-listed regular-plan fund is analysed on a phone via rolling-window SIP/lumpsum simulations (1–50 y) and scored (FQS). Two-tier monolith:

| Layer | Technology |
|---|---|
| App | React Native 0.86 + Expo SDK 57 + Expo Router 57, TypeScript (iOS / Android / web) |
| API | FastAPI 0.110 (Python 3.11) on Uvicorn, single process |
| Database | MongoDB (Atlas) via Motor |
| Hosting / schedulers | Render free web service; GitHub Actions cron (keep-alive + nightly refresh); in-process asyncio schedulers |

No queue, no WebSockets, no user authentication (optional shared operator token only).

## Where to look

| Topic | Document |
|---|---|
| System overview, capabilities, gaps | [docs/SYSTEM_CONTEXT.md](docs/SYSTEM_CONTEXT.md) |
| Backend modules, process model, business rules | [docs/architecture/backend.md](docs/architecture/backend.md) |
| Frontend routes, state, components, UI system | [docs/architecture/frontend.md](docs/architecture/frontend.md) |
| End-to-end flows | [docs/architecture/data-flow.md](docs/architecture/data-flow.md) |
| Auth / authorization (there is none) | [docs/architecture/authentication.md](docs/architecture/authentication.md) |
| All 53 endpoints | [docs/api/api-reference.md](docs/api/api-reference.md) |
| Collections & documents | [docs/database/schema.md](docs/database/schema.md) |
| Features, dependency map | [docs/features/](docs/features/feature-inventory.md) |
| Setup, env, conventions, deployment | [docs/development/](docs/development/local-setup.md), [docs/deployment/deployment.md](docs/deployment/deployment.md) |
| Debt, security, performance, quality, roadmap | [docs/audits/](docs/audits/technical-debt.md) |
| FQS formula specification (code-verified) | [MODEL_AUDIT.md](MODEL_AUDIT.md) |

## Repository map (version2)

```
backend/            FastAPI app + engines (server.py, fqs_engine.py, lumpsum_engine.py, category_map.py, amfi_client.py, fund_lineage.py, …) + tests/ (484) + scripts/
frontend/           Expo app: app/ (routes), src/ (api, providers, components, utils)
backtest/           offline walk-forward backtest toolkit (not part of the running app)
backtest_data/      302 MB of cached NAV/results (repo bloat)
docs/               this documentation set
memory/             PRD.md (historical MVP spec — stale), PROPOSAL_FEATURE_SPEC.md (accurate)
.github/workflows/  keepalive.yml, daily-refresh.yml
render.yaml · DEPLOY.md · design_guidelines.json · *.md reports · fqs_all_funds.xlsx
```

## Core request flow (fund analysis)

```
Fund Detail → api.analyze → GET /api/funds/{code}/analyze
  default run? → fund_analysis cache (24 h) → return
  else _build_analysis → amfi_client.load_analysis_nav (mfapi.in + verified predecessor/successor stitching)
        → category_map.classify_fund → fqs_engine.analyze_fund (month-end NAV → rolling SIP windows → stats → FQS)
        → _save_analysis (fund_analysis + mirror into fund_nav_meta) → _touch_recent → JSON
```
Home reads precomputed `fund_nav_meta.fqs_by_horizon[…]` (nightly) — not live computation. See [data-flow.md](docs/architecture/data-flow.md).

## Which funds are offered (`backend/fund_status.py`)

`fund_nav_meta` indexes the whole regular-plan universe, wound-up schemes included (mfapi.in serves frozen NAV series forever). `fund_status` is the single rule for "still a selectable option": hidden when **superseded** (`superseded_by` / `merged_with` set from the successor map) or **stale** (own last NAV > `WOUND_UP_THRESHOLD_DAYS` = 180 d old). Nothing is deleted; search *labels* dead funds (`is_active:false`, `inactive_reason`) and `active_only=true` hides them for pickers. `sync_superseded(db)` runs at boot and after every discovery/lineage run. ⚠ The stale rule reads `own_latest_nav_date`, which only the (manual, resumable) crawler writes — see debt #1.

## Corrections vs the 2026-06-30 version

| Previous claim | Reality (verified) |
|---|---|
| Tabs: Home / Funds / Methodology under `app/(tabs)`; `fund/[code].tsx` 1 711 lines with old code commented out | `app/(drawer)/(tabs)`: Home, Funds, Rolling, Review, Overlap (5 tabs); Methodology moved to the drawer; plus mode/tenure pickers, data-refresh, proposal screens. `fund/[code].tsx` is 921 lines. |
| 16 endpoints in the API table | 53 application routes (proposal, rolling-analysis, bulk-analysis, overlap, lumpsum, lineage, admin, continuity…). |
| "No `createIndex()` calls anywhere" | Indexes are created at boot: `fund_nav_meta` (3), `fund_holdings` (2); lineage collections on persist. Others still missing. |
| `PREDECESSOR_MAP` in `amfi_client.py` with one entry | Replaced by `MERGER_MAP` seeded from `fund_lineage.CURATED_SUCCESSORS` (3 curated pairs) + DB-discovered pairs + ISIN-verified pairs, all gated by `verify_stitch`. |
| Max horizon 20 y (comments) | 50 y (`MAX_HORIZON_YEARS = 50`). |
| Equity FQS weights discrepancy "unresolved" | Live equity weights: Success .30, AdjRisk .20, BlendedWM .20, Consistency .15, Downside .10, ES .05 (`score_horizon`); `memory/PRD.md` is the stale one. |
| `.venv_test/` checked in; `export_mongo_to_excel.py` duplicated at root | Neither is tracked on version2. |
| Auth section: token-gated `/refresh/run`, `/fqs/refresh`, `/home/reindex` | Also `/lumpsum/refresh`, `/continuity/events` (write), `/admin/*` (some); several admin GETs and all of `/overlap/ingest*` are ungated. |
| Concurrency risk "unlikely" | Full-universe jobs are now serialised by `batch_support.exclusive`; the recently-viewed refresh is not. |

Still accurate from the earlier version and retained: the two-data-plane model (viewed-fund plane `fund_analysis`/`fund_recent` vs universe plane `fund_nav_meta`), the "scoring formula is the source of truth" rule, and the "live code starts after the commented-out block" warning for `fqs_engine.py` and `category_map.py`.
