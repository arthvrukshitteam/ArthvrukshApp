# System context — ArthVruksh MF Analyst

*Snapshot of `origin/version2` @ `8a0089a1` (2026-09-15), researched read-only. For the compact working brief used by AI sessions see [`../CLAUDE_CONTEXT.md`](../CLAUDE_CONTEXT.md). Doc index at the bottom.*

## 1. What the system is

A mobile-first (Expo/React Native, also web) **mutual-fund analysis tool for Indian advisors (MFDs)**. It re-implements an in-house Excel model: for any AMFI regular-plan growth scheme it computes **rolling SIP (and lumpsum) wealth-multiple distributions over every horizon 1–50 y**, scores them into a **Fund Quality Score (FQS 0–100, tiers Elite/Strong/Good/Average/Weak)**, ranks the whole fund universe for the advisor's chosen tenure, and turns selected funds into client **proposals** (PDF/XLSX/email) with SWP projections. Add-ons: rolling-period stability analysis, bulk portfolio review from an uploaded Excel, and weight-based portfolio-overlap between two funds.

Data source: free community API `api.mfapi.in` (NAV history) + AMFI/Internet-Archive files (identity/lineage) + AMC monthly disclosures (holdings). Persistence: MongoDB. Hosting: Render free web service + Atlas M0 + GitHub Actions cron.

## 2. Architecture in one picture

```
Expo app (Home · Funds · Rolling · Review · Overlap  +  drawer: Methodology, Data Refresh, Mode)
    │ fetch JSON (no auth)
    ▼
FastAPI monolith (backend/server.py + 4 routers) ── in-process schedulers/jobs (22:00/22:30/23:00 UTC)
    │ Motor                       │ httpx
    ▼                             ▼
MongoDB (fund_nav_meta hub, caches, proposals, lineage…)     mfapi.in · amfiindia.com · web.archive.org · AMC sites
```
Details: [backend](architecture/backend.md) · [frontend](architecture/frontend.md) · [data flow](architecture/data-flow.md) · [DB](database/schema.md) · [API](api/api-reference.md).

## 3. What the application can do today (verified capabilities)

1. **Core:** fund search incl. historical names/ISINs; per-fund SIP & lumpsum analysis (1–50 y horizons, FQS, success %, median/P10/blended WM, CV, alpha trend, stability, stress); ranked universe per tenure with Since-2010 and Separate-Management-Periods views; Additional Recommendations (shorter-history funds with XIRR ≥ 15 %).
2. **Supporting:** merger/AMC-change stitching with correlation/ISIN verification; dead/merged fund hiding; IDCW/Direct exclusion; NAV unit-change sanitising; old-vs-new-management toggle; AMC history timeline; light/dark theme; simple/analyst density; glossary hints.
3. **Admin/operator:** NAV-universe crawl, batch refreshes, merger discovery, lineage build, fund-status sync/summary, continuity-event curation, bulk export of scores, in-app Data Refresh screen.
4. **Advisor features:** proposal builder (≤10 funds, SIP or lumpsum, SWP) with PDF/XLSX/email; Portfolio Review (Excel in → branded Excel out with FQS & remarks); Rolling Analysis (single & multi-fund consistency ranking, PDF); Portfolio Overlap.
5. **Automation:** nightly refreshes (GitHub cron + in-process), keep-alive pings, self-pings during long jobs.
6. **Integrations:** mfapi.in, AMFI, Internet Archive, AMC disclosure URLs, device mail app/share sheet.
7. **Reporting/analytics:** all of the above computations; PDF/XLSX exports; **no** product analytics.
8. **Communication:** email via device mail composer only; no server-side email/SMS/WhatsApp, no push notifications.
9. **File handling:** Excel upload/download, disclosure-workbook ingest, PDF/XLSX generation (all in memory, Mongo-persisted for bulk jobs).
10. **Authentication/security:** none for users; optional shared operator token. See [authentication](architecture/authentication.md), [security audit](audits/security-audit.md).

## 4. Gap analysis (summary — details in the audits)

* **Missing:** auth/RBAC/multi-tenancy, audit log, retention, CI, frontend tests, observability, automatic universe refresh, OTA updates, overlap-ingest UI, accessibility, tablet/desktop layouts.
* **Partial:** Portfolio Overlap (needs operator-ingested holdings); nightly pipeline reliability on free tier; lineage coverage (AMFI history starts ~2006; archive.org dependent).
* **Debt:** god-file `server.py`, giant screens, commented-out legacy code, triplicated math, dead modules, repo bloat — [technical-debt](audits/technical-debt.md).
* **Security (top 3):** public client PII, unauthenticated SSRF-capable ingest, operator endpoints open when token unset — [security-audit](audits/security-audit.md).
* **Performance:** payload size (≈0.7 MB per long fund; whole universe per Home load), redundant NAV downloads, hours-long in-process batches on 512 MB — [performance-audit](audits/performance-audit.md).
* **Quality:** scorecard + testing gaps — [code-quality-and-testing](audits/code-quality-and-testing.md). Roadmap — [enhancement-roadmap](audits/enhancement-roadmap.md).

## 5. Facts that surprise newcomers

* Two product versions exist: `main` ≈ MVP (7 endpoints, 3 screens, Expo 54); **`version2`** ≈ the real product (53 endpoints, 5 tabs + drawer, Expo 57). Docs describe version2.
* "FQS" ranking on Home is **by Blended WM**, not FQS (FQS sorts only inside a sub-category) — `MODEL_AUDIT.md` finding #2.
* Index Fund and Hybrid are treated as **non-equity** in SIP scoring.
* Fund Detail views silently refresh the nightly numbers for that fund.
* Installed APKs can't be updated remotely → API changes must stay backward compatible.
* `memory/PRD.md` and the previous `ARCHITECTURE.md` were stale; code is authoritative.

## 6. Unresolved / UNKNOWN (needs a human with prod access)
Which branch Render serves · whether `REFRESH_TOKEN` is set in prod · contents of `backend/.env`/`frontend/.env` · production Mongo indexes/size/`fund_nav_meta` freshness · overlap coverage (`/api/overlap/coverage`) · whether `predecessor_map` is populated · frontend type-check/lint status · PDF disclaimer/compliance review · mfapi.in rate-limit behaviour at nightly volume.

## 7. Document index
`architecture/`: backend, frontend, data-flow, authentication · `features/`: feature-inventory, dependency-map · `api/api-reference.md` · `database/schema.md` · `development/`: local-setup, environment, coding-guidelines · `deployment/deployment.md` · `audits/`: technical-debt, security-audit, performance-audit, code-quality-and-testing, enhancement-roadmap. Pre-existing deep-dives (kept): `MODEL_AUDIT.md` (formula spec), `LUMPSUM_ANALYSIS_REPORT.md`, `LINEAGE_VALIDATION_REPORT.md`, `BACKTEST_REPORT_5_FUNDS.md`, `memory/PROPOSAL_FEATURE_SPEC.md`.
