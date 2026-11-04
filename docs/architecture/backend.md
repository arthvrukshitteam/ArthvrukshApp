# Backend architecture

> Verified against `origin/version2` @ `8a0089a1` (2026-09-15). Everything here was read from code; items that could not be verified are marked **UNKNOWN / REQUIRES VERIFICATION**.

## 1. Stack

| Concern | Actual implementation |
|---|---|
| Language / runtime | Python 3.11 (`backend/.python-version` = 3.11.9; `render.yaml` sets `PYTHON_VERSION=3.11`) |
| Framework | FastAPI 0.110.1 on Uvicorn 0.25.0 (single process, single worker) |
| DB driver | Motor 3.3.1 (async) / PyMongo 4.5.0 → MongoDB (Atlas M0 per `DEPLOY.md`) |
| Numerics | stdlib `statistics`/`math` + `pyxirr` 0.10.8 (headline XIRR only) |
| HTTP client | `httpx` 0.28.1 |
| Exports | `openpyxl` 3.1.5 (xlsx), `reportlab` 4.2.5 (pdf), `xlrd` 2.0.1 (legacy .xls read) |
| Validation | Pydantic 2.6.4 request models + FastAPI `Query(ge=, le=)` |
| Auth | **None** for users. Optional shared secret `REFRESH_TOKEN` on a subset of operator endpoints. See [authentication.md](authentication.md). |
| Queue / workers | **None.** Long jobs are `asyncio.create_task` inside the web process. |
| WebSockets | None |
| Caching | In-memory (AMFI fund list, 12 h; total-indexed count, 5 min) + Mongo cache collections (24 h) |
| Logging | stdlib `logging.basicConfig`, module loggers. No structured/JSON logging, no request IDs. |
| Rate limiting | None |

Entry point: `backend/server.py` → `app = FastAPI(...)`, `uvicorn server:app --host 0.0.0.0 --port $PORT` (`render.yaml`).

## 2. Process model (important)

One Uvicorn process hosts **everything**: HTTP API, three nightly schedulers, long-running crawl/batch jobs, merger discovery, lineage build, bulk-Excel jobs. Consequences:

* A deploy/restart/free-tier spin-down **kills any running job** with no resume except `nav_meta_indexer` (skip-already-indexed) and `lineage_store.persist` (staging collection swap).
* Targets a **512 MB** instance (comments in `batch_support.py`, `holdings_ingest.py`). Much of the code is memory-defensive (keyset pagination, projections, `asyncio.to_thread`, per-page release).
* `lifespan` (server.py:143) at boot: load successor cache → `holdings_store.ensure_indexes` → `fund_status.ensure_indexes` → `fund_status.sync_superseded` → start 3 scheduler tasks (22:00, 22:30, 23:00 UTC). Every step is wrapped so a failure never blocks boot.

## 3. Module map (backend/)

| Module | Role | Notes |
|---|---|---|
| `server.py` (2.8k lines) | App, `/api` router, universe endpoints, search, analyze, refresh/admin endpoints, schedulers, merger discovery, mounts 4 feature routers | God-file; see [technical-debt](../audits/technical-debt.md) |
| `fqs_engine.py` | SIP rolling-window scoring (FQS). **Lines 1–~490 are a commented-out previous version**; live code starts at `from pyxirr import xirr` (~line 490) | 🔴 core |
| `lumpsum_engine.py` | Lumpsum twin of the above (WM = end NAV / start NAV) | 🔴 core; no unit tests |
| `category_map.py` | `classify_fund`, `CATEGORY_TABLE` (min-WM benchmarks), `max_plausible_wm`, lumpsum tables, `get_display_category`. **Lines 1–~160 are a commented-out previous version** | 🔴 core |
| `amfi_client.py` | mfapi.in client, IDCW filter, NAV splice sanitiser, successor/predecessor resolution, `load_analysis_nav` (the ONE place NAV is prepared) | 🔴 core |
| `fund_lineage.py` | `CURATED_SUCCESSORS`, `CURATED_AMC_CHANGES`, house aliases, `verify_stitch` correlation/seam gate | data + logic |
| `lineage_store.py` / `lineage_build.py` / `amfi_history.py` | Lineage DB (names/ISINs/AMC history) derived from AMFI live/historical files + Internet Archive captures; powers search-by-old-name and AMC-change breaks | admin-triggered |
| `fund_status.py` | Data-layer "is this fund still offered" rule (`superseded_by` / stale > 180 d), index creation, `sync_superseded` | 🟠 |
| `nav_meta_indexer.py` | Crawls whole universe into `fund_nav_meta` (resumable) | admin-triggered |
| `fqs_refresh.py` / `lumpsum_refresh.py` | Nightly full-universe scoring; write `fqs_by_horizon[_2010|_continuity]` / `fund_lumpsum_analysis` | serialised by `batch_support` |
| `batch_support.py` | Process-wide asyncio lock + keyset pagination for batch jobs | |
| `continuity_floor.py` / `continuity_select.py` | Which break date / which variant ("Since 2010", "Separate Management") to read | pure, tested |
| `additional_rec.py` | "Additional Recommendations" rule (≥5Y history, ≥10 windows, XIRR ≥ 15 %) | pure, tested |
| `xirr_utils.py` | WM↔XIRR (SIP `RATE(n*12,-1,0,WM*n*12)*12`; lumpsum `WM^(1/n)-1`) | canonical copy; **duplicated** in `server.py` aliases and `frontend/src/utils/xirr.ts` |
| `proposal_engine.py` / `proposal_routes.py` / `proposal_pdf.py` / `proposal_xlsx.py` | Portfolio proposal build + SWP + exports | routes inject analysis fns from server.py |
| `rolling_analysis_routes.py` (+ `_pdf`, `_multi_pdf`) | Period-bucketed stability analysis (single fund and multi-fund ranking) | no tests |
| `bulk_analysis.py` / `bulk_analysis_routes.py` | Excel upload → FQS merge → branded xlsx | background job in-process |
| `overlap_engine.py` / `overlap_routes.py` / `holdings_parser.py` / `holdings_ingest.py` / `holdings_store.py` | Portfolio overlap from AMC monthly disclosures | ingest endpoints unauthenticated |
| `indian_format.py` | Indian digit grouping for PDF/XLSX (mirror of `frontend/src/utils/format.ts`) | |
| `predecessor_discovery.py` | Older 3-tier discovery job writing `predecessor_map` | **not imported anywhere** (only a report mentions it) |
| `curated_funds.py` | Old seed list | **dead** (only referenced by ARCHITECTURE.md) |
| `export_mongo_to_excel.py` | Local CLI export | standalone |
| `scripts/*.py` | One-off operator CLIs (lineage build, merger/successor discovery & promotion, universe list) | need real `MONGO_URL`; contents not individually audited |

Also at repo root: `backtest/` (offline walk-forward backtest toolkit, own README-less package), `backtest_data/` (**302 MB** of cached NAV/results committed), `compute_sip.py` (scratch script with a hard-coded local path — dead), `fqs_all_funds.xlsx` ×2 (exports).

## 4. Core pipeline (the product)

```
mfapi.in /mf/{code}  ──►  amfi_client.parse_nav_history (sort, drop nav<=0, splice-sanitise ×4 jumps within 31 d)
                          amfi_client.load_analysis_nav  (forward + backward verified stitching, depth ≤ 4)
        │
        ▼
category_map.classify_fund(name, amfi_category)  → one of 11 buckets (Debt, Gold/Silver, Hybrid, Index Fund, Large/Flexi/ELSS/Multi/Mid/Small Cap, Misc Fund)
        │
        ▼
fqs_engine.analyze_fund
   extract_month_end_navs → compute_full_history_xirr (pyxirr)
   for H in 1..50: compute_rolling_windows → score_horizon → HorizonResult
   headline = first of horizon 15,10,5,3 present (else longest)
   cycle_robustness (only if user_years or headline ≥ 10), stability_check = median10/median5
```

### Business rules (vs implementation details)

**BUSINESS RULES** (change only with product-owner sign-off — they alter every score):

* ₹10,000/month SIP; monthly rolling windows; WM = end value ÷ (₹10,000 × H × 12) (`fqs_engine.py compute_rolling_windows`).
* Pass/fail per window: WM ≥ category min-WM (`CATEGORY_TABLE`, from the Excel "FINAL MODEL"); lumpsum min-WM = `(1+xirr_req)^H`.
* FQS weights (live code, `score_horizon`):
  * Equity: Success 0.30, AdjRisk 0.20, BlendedWM 0.20, Consistency 0.15, Downside 0.10, ES 0.05.
  * Non-equity: Success 0.25, AdjRisk 0.35, BlendedWM 0.15, Consistency 0.10, Downside 0.10, ES 0.05.
  * Lumpsum copies the same two vectors (`lumpsum_engine.EQUITY_WEIGHTS/NON_EQUITY_WEIGHTS`).
  * ⚠ `memory/PRD.md` documents a *different* equity vector (35/25/20/10/10, no consistency). PRD is stale; code is authoritative (see `MODEL_AUDIT.md`).
* Equity/non-equity split: `CATEGORY_TABLE[...]["type"]` — **Index Fund and Hybrid are non-equity** in SIP; lumpsum equity set = Large, Flexi, Multi, Mid, Small, ELSS, Misc.
* Blended WM = 0.6·median + 0.4·P10. Tiers: ≥90 Elite, ≥80 Strong, ≥70 Good, ≥60 Average, else Weak.
* Cycle Robustness: needs effective years ≥ 10; 15Y variant `0.30·S3+0.30·S5+0.25·S10+0.15·S15`, 10Y variant `0.35·S3+0.35·S5+0.30·S10`.
* Direct plans and IDCW/Dividend/Payout/Reinvestment variants are excluded everywhere (growth-investor basis). "Dividend Yield" is a category, not excluded.
* Universe eligibility: `history_years ≥ tenure + 2` (1Y tenure: ≥1). Dead/merged funds hidden (`fund_status`). Debt always sorts last.
* Additional Recommendations: history 5Y … < required; judged at longest window (≥10 rolling windows); XIRR ≥ 15 %.
* Remarks (bulk review): FQS < 75 "Bad — Should Discontinue"; 75–<85 "Not Bad, but better options available"; ≥85 "Good — Should Continue"; None → "Calculation unavailable".
* Proposal: ≤10 funds, allocations sum to 100 % ±1 %, single mode, single horizon (1–50), SWP optional.
* Overlap headline = Σ min(wA, wB) on equity-rebased weights, same disclosure month for both funds.

**IMPLEMENTATION DETAILS** (safe to refactor with tests): keyset pagination, 24 h TTLs, semaphores (4/6/8), batch sizes (100/200), keep-alive self-pings, projection trimming, `_LEGACY_ALIAS_KEYS`.

### Known model quirks (documented, not bugs to silently "fix")

* `total_invested` is hard-coded `10000 × H × 12` but the instalment count in a window can be 59/60/61 → ±~1.7 % WM artefact (`MODEL_AUDIT.md` #6; confirmed in `compute_rolling_windows`: `invested` fixed, `units` summed over `sip_idx`).
* Windows with WM above `max_plausible_wm` are **dropped**, not clipped (NAV corruption guard) — a whole horizon can vanish.
* Sectoral/Thematic/Focused/Value funds are benchmarked as Flexi Cap; Arbitrage as Debt.

## 5. Schedulers and jobs

| Job | Trigger | Concurrency | Writes | Guard |
|---|---|---|---|---|
| Recently-viewed refresh (`_daily_refresh`) | in-process 22:00 UTC; `POST /api/refresh/run` (synchronous!) | Semaphore(4) | `fund_analysis`, `fund_nav_meta` mirror, `refresh_log` | `_daily_refresh_running` flag (status only — does **not** prevent double-run) |
| SIP FQS batch | in-process 22:30 UTC; `POST /api/fqs/refresh` | Semaphore(6), pages of 100 | `fund_nav_meta.*`, `fqs_refresh_log` | `batch_support.exclusive` + "already_running" |
| Lumpsum batch | in-process 23:00 UTC; `POST /api/lumpsum/refresh` | Semaphore(4) | `fund_lumpsum_analysis`, `lumpsum_refresh_log` | same |
| NAV-meta crawl | `POST /api/home/reindex` (manual only) | Semaphore(8) | `fund_nav_meta`, `nav_meta_refresh_log` | skip already-indexed |
| Merger discovery | `POST /api/admin/merger-discovery` | Semaphore(4) | `merger_discovery_log` (`_id:"latest"`) | task flag |
| Lineage build | `POST /api/admin/build-lineage` | n/a | `fund_lineage`, `fund_identifier_idx`, `fund_successor_map`, … | task flag |
| Bulk Excel job | `POST /api/bulk-analysis/{id}/process` | Semaphore(6) | `bulk_analysis_jobs` | `_running_tasks` dict |
| GitHub Actions cron | 22:00 UTC → the three POSTs; keepalive every 8 min | external | – | see [deployment](../deployment/deployment.md) |

External cron and in-process schedulers can both fire; the batch lock serialises FQS vs Lumpsum but the third (recently-viewed) job is not covered by it.

## 6. Error handling conventions

* `HTTPException` with 400 (bad input / Direct plan), 401 (bad token), 404, 409, 413, 422, 502 (upstream/compute failure, **any** unexpected exception in `/analyze` becomes 502), 503 (universe index not built).
* Background tasks log and swallow; schedulers sleep 600 s after an unexpected error.
* No global exception handler, no error-tracking service (no Sentry etc.).

## 7. Things that look implemented but are not / are weaker than they look

* `analyze` endpoint's `years` param exists but the shipped frontend never sends it (`api.analyze` has no `years`).
* `stability_check` is projected by `/export/all-funds` but `fqs_refresh` never writes it to `fund_nav_meta` (only `_save_analysis` payload contains it, and the mirror `$set` there omits it too) → always absent in export. **Verified by reading both writers.**
* `amfi_client.fetch_nav_stitched`, `curated_funds.py`, `predecessor_discovery.py`, `compute_sip.py` have no callers.
* `fund_nav_meta` span fields (`history_years`, `latest_nav_date`, `own_latest_nav_date`) are only written by the crawler, which **skips codes already indexed** → they never refresh after first index (see [technical-debt](../audits/technical-debt.md) #1). DB contents themselves are **UNKNOWN**.
