# Code-quality scorecard & testing audit

`origin/version2` @ `8a0089a1`. Scores are judgement calls (1 = poor, 10 = excellent) justified by cited evidence; they are for prioritisation, not benchmarking.

## 1. Scorecard

| Area | Score | Why |
|---|---|---|
| Architecture | **6** | Good seams: pure engines (`fqs_engine`, `overlap_engine`, `proposal_engine`, `continuity_*`, `additional_rec`) vs thin routers with injected callables; single sources of truth for NAV prep, XIRR, variant selection. Hurt by the 2.8 k-line `server.py`, an in-process job model on a 512 MB box, and two diverging branches. |
| Maintainability | **5** | Excellent "why" comments with measured evidence, but 490 + 160 lines of commented-out legacy code in the two most critical files, 2 k-line screens, copy-pasted guards (#21), three copies of WM↔XIRR. |
| Naming | **7** | Consistent `snake_case` / `camelCase`; names describe domain (`load_analysis_nav`, `continuity_floor`). A few misleading historical names (`MERGER_MAP` now also holds curated lineage; `fqs_by_horizon` also feeds export). |
| Separation of concerns | **6** | Backend good; frontend screens mix data fetch, view-model logic and presentation in one file (`home.tsx`). |
| Reusability | **5** | Shared helpers exist but SIP/Lumpsum detail screens and engines duplicate large parts. |
| Error handling | **6** | Consistent HTTP status vocabulary, defensive try/except around optional enhancements (lineage, status) so features degrade rather than fail. But blanket 502 for any exception, swallow-and-log in background, no global handler, no error tracking, client shows raw `HTTP 502: …` text in places. |
| Testing | **5** | Backend: 484 offline tests pass in ~5 s, strong on pure logic (lineage, bulk matching, overlap, proposals, continuity). Gaps in lumpsum engine, refresh jobs, indexer, PDFs/XLSX, rolling routes, endpoint-level behaviour; **frontend: zero tests**; **no CI gate**. |
| Type safety | **6** | Frontend `strict: true` (not verified to compile clean); API types hand-mirrored. Backend: partial hints, `Dict[str, Any]` everywhere, no mypy. |
| Documentation | **6** (was ~3) | Inline docs are exceptional; repository-level docs were stale/misleading until this pass (README stub, PRD, ARCHITECTURE, DEPLOY). |
| Logging / observability | **4** | stdlib logs only; no request IDs, metrics, tracing, alerting; job state in memory. |
| Configuration | **5** | Few env vars, sane defaults; but tracked `.env` files, no `.env.example`, duplicated constants, schedule times in 4 places. |
| Security | **3** | See [security-audit](security-audit.md): no auth, public PII, SSRF endpoint. |
| Performance | **6** | Memory-conscious batch design, cheap engine; heavy payloads, redundant NAV fetches, in-process long jobs. |

## 2. Test inventory (backend) — verified by running

`cd backend && pytest tests --ignore=tests/test_mf_api.py` → **484 passed in 4.6 s** (Python 3.11 venv built from `requirements.txt`; no DB, no network).

| Test module | Tests | Covers |
|---|---|---|
| `test_bulk_analysis.py` | 83 | header detection, column planning, allocation parsing, fund matching ladder, remarks, output workbook, job processing |
| `test_fund_lineage.py` | 50 | stitch verification gates, house aliasing, curated tables |
| `test_category_map.py` | 39 | classifier buckets & edge cases |
| `test_lineage_store.py` | 37 | derivation, identifier index, resolve, persistence staging |
| `test_holdings_parser.py` | 32 | disclosure workbook parsing |
| `test_xirr_utils.py` | 29 | WM↔XIRR round trips |
| `test_search_dedupe.py`, `test_fund_status.py`, `test_universe_filtering.py`, `test_idcw_filter.py` | 22 / 22 / 18 / 17 | search collapse, dead-fund rule, universe dedupe/ranking pieces, IDCW exclusion |
| `test_overlap_engine.py`, `test_overlap_pipeline.py` | 19 / 7 | weight-based overlap, ingest→compare |
| `test_model_audit_spec.py` | 15 | **clean-room re-implementation of the SIP FQS maths** matching `analyze_fund` on 5 funds × 3 horizons |
| `test_proposal_*` (3 files), `test_continuity_*`, `test_additional_recs`, `test_batch_support`, `test_nav_sanitize` | 13+12+12, 13+13, 13, 12, 6 | proposal maths/short-history/routes, continuity floor & selection, Additional Rec rule, batch lock + keyset paging, NAV splice |
| `test_mf_api.py` | 12 | **live-server** regression (excluded above); needs `EXPO_PUBLIC_BACKEND_URL` |

**Not covered by any test** (verified by import grep): `lumpsum_engine`, `fqs_refresh`, `lumpsum_refresh`, `nav_meta_indexer`, `rolling_analysis_routes`, `proposal_pdf`, `proposal_xlsx`, `rolling_analysis_pdf`, `holdings_ingest` fetch path, most of `server.py` handlers (4 test modules import `server` for pure helpers), all scheduler code, all frontend code. There is no coverage tool configured. Legacy artefacts `test_result.md` and `test_reports/` are Emergent-platform outputs, not a live test system.

## 3. Missing tests, prioritised

**CRITICAL**
1. `nav_meta_indexer` skip/refresh semantics (would have caught debt #1) and `fund_status` freshness over time.
2. `lumpsum_engine` parity suite: window builder (EOMONTH/EDATE), `compute_horizon_stats`, weights, cycle robustness — mirror `test_model_audit_spec` for lumpsum. It feeds Home (Lumpsum), proposals and rolling and has no tests at all.
3. Endpoint contract tests with FastAPI `TestClient` + an in-memory Mongo fake: `/home/universe` & `/lumpsum/universe` (toggle precedence, 503 gate, legacy `selected_*` aliases), `/funds/{c}/analyze` cache semantics (default vs filtered vs `include_predecessor=false`), operator-token gating on every protected route (and absence on the others — documents current behaviour before auth is added).
4. Nightly job tests with fake NAV + fake DB: `fqs_refresh` writes the three variants correctly, continuity floor wiring, failure counting, batch lock serialisation.

**HIGH**
5. PDF/XLSX smoke + golden tests (Indian grouping, extrapolation asterisks, SWP table) — client-facing documents.
6. Rolling analysis: bucket derivation, empty buckets, ranking order, add-fund re-rank.
7. Bulk-job lifecycle incl. orphaned `processing` state (#2).
8. Frontend contract tests: golden vectors proving `utils/xirr.ts` equals `xirr_utils.py`; `format.ts` equals `indian_format.py`; type-check (`tsc --noEmit`) and `expo lint` in CI.
9. Overlap ingest hardening tests (private-IP/redirect blocking) once S2 is fixed.

**MEDIUM**
10. Home filtering/sorting logic extracted from `home.tsx` into pure functions and unit-tested (CORE_SUBS whitelist, Debt-last, Additional Rec sorting).
11. `amfi_client.load_analysis_nav` with recorded mfapi fixtures (forward/backward stitching, depth limit, rejected stitches).
12. Component smoke tests (React Native Testing Library) for Home/Fund Detail loading/error/empty states.
13. Property tests for `compute_rolling_windows` window counts and WM bounds; regression fixtures for known tricky funds (HDFC Small Cap, Edelweiss Mid Cap, BNP/Baroda).

## 4. CI recommendation (none exists today)
GitHub Actions on PR: backend `pytest` (3.11) + `ruff`; frontend `npm ci`, `tsc --noEmit`, `expo lint`; `pip-audit` / `npm audit`. The scheduled workflows already prove the team uses Actions.
