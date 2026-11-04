# CLAUDE_CONTEXT.md — read this first

> Primary context for AI/dev sessions. Verified by reading code on **`origin/version2` @ `8a0089a1`** (2026-09-15); written 2026-10-07. If code and this file disagree, **code wins — then fix this file**. Items tagged **UNKNOWN** could not be verified from the repo. Deeper detail: [`docs/SYSTEM_CONTEXT.md`](docs/SYSTEM_CONTEXT.md) and the files it indexes.

**Branch warning:** `main` = early MVP (7 endpoints, 3 screens, Expo 54). `version2` = the real product described here (53 endpoints, 5 tabs + drawer, Expo 57). Confirm which branch you are on (`git branch`) before relying on anything below. Which branch Render deploys is **UNKNOWN**.

---

## 1. Project overview
Mobile-first (iOS/Android/web) mutual-fund analysis tool for Indian MFDs. Replicates an Excel model: rolling SIP/lumpsum windows over horizons 1–50 y → distribution stats → **FQS** (0–100; Elite ≥90, Strong ≥80, Good ≥70, Average ≥60, else Weak). Ranks the fund universe per tenure; builds client proposals (PDF/XLSX/SWP); rolling stability analysis; bulk Excel portfolio review; portfolio overlap.

## 2. Business purpose
Replace spreadsheet-based fund selection/proposal work; consistent, explainable scoring of thousands of schemes; client-ready documents branded "ArthVruksh MFDGuru LLP" (ARN default 365554 — regulatory data).

## 3. Technology stack
Backend: Python 3.11, FastAPI 0.110.1, Uvicorn 0.25, Motor 3.3.1/PyMongo 4.5, httpx, pyxirr, openpyxl, reportlab, xlrd, pydantic 2.6. Frontend: React Native 0.86, Expo ^57, expo-router ~57 (typed routes), React 19.2, TypeScript strict, lucide icons. Data: api.mfapi.in, AMFI NAVAll/history, web.archive.org, AMC disclosure workbooks. Infra: Render free web service, MongoDB Atlas M0, GitHub Actions, EAS Build.

## 4. Repository structure
`backend/` (server.py + engines + routers + tests/ + scripts/) · `frontend/` (app/ routes, src/) · `docs/` · `backtest/` + `backtest_data/` (offline, 302 MB) · `memory/` (PRD stale; PROPOSAL_FEATURE_SPEC accurate) · `.github/workflows/` · `render.yaml`, `DEPLOY.md`, `MODEL_AUDIT.md` (formula spec), report `.md/.pdf`, `fqs_all_funds.xlsx`, `compute_sip.py` (dead).

## 5. Frontend architecture
Expo Router tree: `app/_layout.tsx` (Stack + 5 providers + `ModeGuard`) → `(drawer)/_layout.tsx` (Drawer: Dashboard, Methodology, Data Refresh, Analysis Mode) → `(tabs)/_layout.tsx` (custom 5-tab bar: home, index=Funds, rolling, portfolio-review, overlap) ; stack screens: `fund/[code]`, `mode-picker`, `tenure-picker`, `data-refresh`, `proposal/build`, `proposal/[id]`. State = Context (Theme, ViewMode, Mode, Tenure, ProposalDraft) + local `useState`; no query library. All HTTP in `src/api.ts` (`fetch`, no auth/timeout/retry). Persistence: AsyncStorage; SecureStore for optional operator token. See `docs/architecture/frontend.md`.

## 6. Backend architecture
One FastAPI process: `server.py` (router `/api`, universe, search, analyze, refresh/admin, schedulers, merger discovery) + 4 routers (`proposal_routes`, `rolling_analysis_routes`, `bulk_analysis_routes`, `overlap_routes`) built via `build_router(db, injected callables)`. Pure engines: `fqs_engine`, `lumpsum_engine`, `overlap_engine`, `proposal_engine`, `continuity_*`, `additional_rec`, `xirr_utils`. Jobs run **in-process** (`asyncio.create_task`), sized for **512 MB**. See `docs/architecture/backend.md`.

## 7. Database architecture
MongoDB, no schema/migrations/TTL/transactions. Hub: `fund_nav_meta` (one doc per scheme: span fields from the crawler, `fqs_by_horizon[_2010|_continuity]` from nightly job, `superseded_by` from status sync). Caches: `fund_analysis`, `fund_lumpsum_analysis`, `fund_recent`. Features: `portfolio_proposals` (**PII**), `rolling_analysis_runs`, `rolling_multi_runs`, `bulk_analysis_jobs` (stores file bytes), `fund_holdings`. Identity: `fund_lineage`, `fund_identifier_idx`, `fund_successor_map`, `predecessor_map`, `merger_discovery_log`, `fund_continuity_events`. Logs: `refresh_log`, `fqs_refresh_log`, `lumpsum_refresh_log`, `nav_meta_refresh_log`. See `docs/database/schema.md`.

## 8. Authentication
**None.** No users/sessions. Optional shared secret env `REFRESH_TOKEN` passed as `?token=` on some operator endpoints; **if unset those endpoints are open** (probably the case in prod — UNKNOWN). See `docs/architecture/authentication.md`.

## 9. Authorization / RBAC
**None.** No roles, no ownership checks; IDs in URLs are the only "protection" (proposals, rolling runs, bulk jobs). Ungated even with token: overlap ingest, admin status GETs, continuity GET, exports.

## 10. Feature inventory (status)
Onboarding ✔ · Home universe SIP+Lumpsum ✔ · Funds search/recents ✔ · Fund detail ✔ · Methodology ✔(static) · Proposals ✔ · Rolling ✔(untested) · Portfolio Review ✔ · **Overlap ◐ (needs operator-ingested holdings)** · **Holding Review ✔ (drawer; `docs/features/portfolio-holding-review.md`)** · Data Refresh ✔ · Lineage/status ✔(admin) · Nightly pipeline ◐(reliability) · Export-all ◐(backend-only, `stability_check` empty) · Backtest toolkit (offline). Dead: `curated_funds.py`, `predecessor_discovery.py`, `fetch_nav_stitched`, `compute_sip.py`. `docs/features/feature-inventory.md`.

## 11. API architecture
53 routes under `/api`, JSON, no versioning/envelope, errors `{"detail"}`; CORS `*`. Groups: health/search/funds, universe (`/home/universe`, `/lumpsum/universe`), refresh/admin, `/proposal`, `/rolling-analysis`, `/bulk-analysis`, `/overlap`. Full table: `docs/api/api-reference.md`. Regenerate the list: `python -c "import server;[print(r.path) for r in server.app.routes]"` from `backend/`.

## 12. Important data flows
* Home: `api.universe` → `/home|lumpsum/universe` → `fund_nav_meta` (precomputed) → ranked list.
* Detail: `/funds/{c}/analyze` → cache or `_build_analysis` → `load_analysis_nav` → `classify_fund` → `analyze_fund` → save + mirror.
* Proposal: Home selection (+ toggle context) → `/proposal/build` → variant doc or analysis → `proposal_engine` → Mongo → PDF/XLSX.
* Nightly: GH cron + schedulers → `fqs_refresh`/`lumpsum_refresh` → `fund_nav_meta`. Full diagrams: `docs/architecture/data-flow.md`.

## 13. External integrations
`api.mfapi.in` (single point of failure, no SLA, rate limits unhandled) · `amfiindia.com` NAVAll/history · `web.archive.org` · AMC disclosure sites (operator URLs) · device mail/share. No email/SMS/WhatsApp/payments/analytics/error tracking.

## 14. Environment configuration
Backend: `MONGO_URL`, `DB_NAME`, `REFRESH_TOKEN`, `RENDER_EXTERNAL_URL`, `ARTHVRUKSH_COMPANY_NAME`, `ARTHVRUKSH_ARN`. Frontend: `EXPO_PUBLIC_BACKEND_URL` (baked into bundle; no trailing `/api`). CI secrets: `BACKEND_URL`, `REFRESH_TOKEN`. **`backend/.env` and `frontend/.env` are tracked by git — do not read-and-echo them, do not commit changes to them.** `docs/development/environment.md`.

## 15. Important business rules (change = every score changes)
* ₹10 000/month SIP, monthly rolling windows; WM = end value ÷ (₹10 000×H×12); lumpsum WM = end NAV ÷ start NAV; min-WM benchmarks per category (`CATEGORY_TABLE`; lumpsum `(1+xirr)^H`).
* FQS weights — equity: Success .30 / AdjRisk .20 / BlendedWM .20 / Consistency .15 / Downside .10 / ES .05; non-equity: .25 / .35 / .15 / .10 / .10 / .05. **Index Fund & Hybrid are non-equity.** Blended WM = .6·median + .4·P10.
* Headline horizon = first present of 15, 10, 5, 3. Cycle Robustness only when tenure (`user_years`, else headline horizon) ≥ 10 (two variants).
* Direct plans and IDCW/Dividend/Payout variants excluded everywhere ("Dividend Yield" is a category, kept).
* Universe: `history_years ≥ tenure+2` (1Y: ≥1), active only, Debt sorts last, variants deduped, Home ranks by **Blended WM** (FQS only within a sub-category).
* Windows above `max_plausible_wm` are dropped (NAV-corruption guard).
* Stitching only if verified (`verify_stitch`): successor outlives predecessor ≥180 d, concurrency ≤0.75, corr ≥.90/.80 or curated/ISIN seam rules.
* Remarks: FQS <75 "Bad — Should Discontinue", <85 "Not Bad…", else "Good — Should Continue".
* Proposal: ≤10 funds, Σ=100%±1%, single mode+horizon, SWP optional. Overlap = Σmin(w) on equity-rebased weights, same month for both funds.
Authoritative formula spec: `MODEL_AUDIT.md` + `backend/tests/test_model_audit_spec.py`.

## 16. Known technical debt (top)
~~#1 NAV-span fields never refresh~~ (fixed) · `server.py` 2.8k lines · token check copied 10× · WM↔XIRR in 3 places · (commented-out legacy blocks removed) · screens of 800–2 000 lines · dead modules/unused deps · repo bloat (302 MB data) · stale docs (PRD, DEPLOY). Register: `docs/audits/technical-debt.md`.

## 17. Known bugs / quirks
(fixed: orphaned bulk jobs now recovered) · `stability_check` never persisted (#3) · `years` param unused by client (#4) · Fund-Detail view overwrites nightly numbers (#5) · `/refresh/run` is synchronous & unguarded against overlap (#6) · `total_invested` fixed vs 59/60/61 instalments (±1.7 %) · duplicate funds allowed in proposal API (#11) · `main`'s workflow calls endpoints absent on `main`.

## 18. Security considerations
Public client PAN/phone/name; SSRF in `/overlap/ingest`; operator endpoints open if token unset; token in query string; no rate limits; zip-bomb exposure; tracked `.env`. `docs/audits/security-audit.md`.

## 19. Performance considerations
Engine is cheap (~0.1 s for a 25-y fund) but payloads are big (~0.7 MB/fund; whole universe per Home call); redundant NAV downloads (3–5×/fund/night); hours-long in-process batches on 512 MB; missing indexes on cache collections; rolling-rank fans out ≤80 live fetches. `docs/audits/performance-audit.md`.

## 20. Testing status
Backend: 484 offline pytest tests pass (~5 s): `cd backend && python -m pytest tests -q --ignore=tests/test_mf_api.py` (Python 3.11; `uv venv --python 3.11`). `test_mf_api.py` needs a live server. **No tests** for lumpsum engine, nightly jobs, indexer, rolling routes, PDF/XLSX, most endpoints; **zero frontend tests**; **no CI**. `tsc`/`expo lint` status UNKNOWN. `docs/audits/code-quality-and-testing.md`.

## 21. Deployment architecture
Render Blueprint (`render.yaml`, `rootDir: backend`, free plan, autoDeploy) + Atlas M0 + GH Actions (`keepalive` */8 min; `daily-refresh` 22:00 UTC → 3 POSTs) + EAS for mobile (installed APKs cannot be updated remotely). `docs/deployment/deployment.md`.

## 22. Coding conventions
Backend: flat modules, snake_case, pure engines/thin routers, `build_router(db, callables)` injection, `HTTPException` code vocabulary, Semaphore-bounded asyncio, keyset paging/projections, long "why" comments with evidence. Frontend: `@/` alias, `makeStyles(colors,type)` + tokens from `theme.ts`, all HTTP via `src/api.ts`, **never import `@react-navigation/*` directly** (use `expo-router`), loading/empty/error states on every data screen. `docs/development/coding-guidelines.md`.

## 23. Important files
| File | Purpose |
|---|---|
| `backend/server.py` | app, universe/search/analyze/admin routes, schedulers |
| `backend/fqs_engine.py` | SIP scoring (🔴; live code after the commented block) |
| `backend/lumpsum_engine.py` | lumpsum scoring (🔴, untested) |
| `backend/category_map.py` | benchmarks, classifier, sanity caps (🔴) |
| `backend/amfi_client.py` | NAV fetch + `load_analysis_nav` stitching (🔴) |
| `backend/fund_lineage.py` / `lineage_store.py` | curated pairs, `verify_stitch`, lineage DB |
| `backend/fund_status.py` | active/dead fund rule |
| `backend/fqs_refresh.py` / `lumpsum_refresh.py` / `batch_support.py` | nightly jobs |
| `backend/nav_meta_indexer.py` | universe crawl (manual) |
| `backend/proposal_*.py` | proposal engine/routes/PDF/XLSX |
| `backend/bulk_analysis*.py` | Excel review |
| `backend/overlap_*.py`, `holdings_*.py` | overlap |
| `frontend/src/api.ts` | every API call + types |
| `frontend/app/(drawer)/(tabs)/home.tsx` | Home (2 081 lines) |
| `frontend/app/fund/[code].tsx`, `src/components/FundDetailShared.tsx` | Fund detail |
| `MODEL_AUDIT.md` | formula spec |

## 24. Do NOT change casually
1. **Scoring maths & benchmarks** (`score_horizon`, weights, `CATEGORY_TABLE`, `max_plausible_wm`, month-end/window logic) — alters every score; requires re-running both nightly jobs and updating `test_model_audit_spec`.
2. **`amfi_client.load_analysis_nav` / `verify_stitch` thresholds / `CURATED_*`** — wrong stitching silently corrupts history.
3. **`_LEGACY_ALIAS_KEYS` (`selected_*`) and any response shape consumed by installed APKs.**
4. **`fund_nav_meta` field names/shapes** (6+ readers).
5. **`REFRESH_TOKEN` semantics** — changing gating breaks cron/old clients; add auth deliberately (see roadmap P0-1).
6. **Legal name / ARN / `RECOMMENDED_BY`** and PDF/XLSX wording (regulated output).
7. **Memory-defensive patterns** (keyset paging, projections, `batch_support.exclusive`) — removing them OOMs the 512 MB instance.
8. Don't "clean up" the Direct/IDCW exclusions, `HISTORY_BUFFER_YEARS` (+2), Debt-last sort, Index/Hybrid non-equity typing without product sign-off.
9. Don't commit `.env` changes; don't print their contents.

## 25. Areas safe for extension
New advisor features following the router pattern (`new_engine.py` pure + `new_routes.py` `build_router(db)` + a screen + `api.ts` entries) · new categories of tests (the untested modules) · CI workflow · docs · new indexes (idempotent, at boot) · retention/TTL indexes · UI components/tokens · new overlap data sources via `AMC_SOURCES` (after fixing SSRF) · performance work that preserves response shapes (additive params).

## 26. Current development priorities
P0: auth + SSRF/operator-gate fix; self-refreshing `fund_nav_meta`; settle `main` vs `version2` deploy; stop tracking `.env`. P1: CI gate; durable jobs; retention; tests for lumpsum/jobs/endpoints; split `server.py`. (Details + files affected: `docs/audits/enhancement-roadmap.md`.)

---

# RULES FOR FUTURE AI DEVELOPMENT

1. **Understand before modifying.** Read the relevant section above, then the code path end to end (frontend → `api.ts` → route → engine → collection). Never rely on filenames, comments or the PRD alone — several comments are stale.
2. **Never rewrite working functionality without a clear, stated reason**; prefer the smallest change that fits the existing pattern.
3. **Reuse before creating.** Check for an existing engine/helper/component (single sources of truth: `load_analysis_nav`, `xirr_utils`, `continuity_select`, `additional_rec`, `fund_status`, `match_fund`, `indian_format`, `FundSearchModal`, `FundDetailShared`). Do not create duplicate APIs, components or math.
4. **Follow existing naming and structure** (see §22).
5. **Check both frontend and backend** before implementing; list every affected layer first (screen, `api.ts` type, route, engine, collection, exports/PDF, nightly job, tests, docs).
6. **Check DB implications before touching models/fields**: who writes it, who reads it (see dependency map), is there a nightly recompute needed, do old documents still work?
7. **Maintain backward compatibility** with installed APKs and existing documents unless explicitly told otherwise; additive API changes only.
8. **Never remove functionality without confirmation**, including "dead" code (verify with grep across `backend/`, `frontend/`, scripts, docs first).
9. **Never change authentication/authorization or token semantics casually**; any gating change must be staged (soft mode) because old clients can't update.
10. **Never expose, print or commit secrets**; don't read `.env` values; never hard-code credentials; never move the operator token into the bundle.
11. **Validate all user input** (Pydantic models, `Query` bounds, file size/type, URL allow-lists); never add an endpoint that fetches caller-supplied URLs without an allow-list.
12. **Protect PII** (client name/phone/PAN): don't log it, don't put it in URLs/filenames, don't add new places that return it.
13. **Respect the 512 MB budget**: stream/page, project fields, bound concurrency, use threads for CPU work, never materialise the universe.
14. **Maintain responsive behaviour**: consider mobile, tablet and desktop/web; use theme tokens; keep loading/empty/error states.
15. **Do not change scoring outputs unintentionally**: run `test_model_audit_spec.py`; if a change is intended, update the spec, re-run nightly jobs, and document it.
16. **Update documentation when architecture or behaviour changes** — the relevant `docs/` file, this file for major changes, and fix any stale comment you touch.
17. **After implementing, verify the complete data flow** end to end (UI → API → DB → UI), not only the unit you edited.
18. **Run available checks:** backend `pytest` (command in §20); frontend `npx tsc --noEmit` and `npm run lint` (not currently verified clean). Add tests for new logic — especially in the untested modules.
19. **Be honest about verification:** say what you ran and what you could not; mark unknowns; never claim production behaviour you haven't observed.
20. **Ask before destructive or outward-facing actions** (deleting data, running crawls/batches against a real DB, hitting production endpoints, pushing/deploying).
