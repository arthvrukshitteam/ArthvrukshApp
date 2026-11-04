# ArthVruksh — MF Analyst

A mobile-first (iOS / Android / web) **mutual-fund analysis tool for Indian mutual-fund distributors**. It faithfully re-implements an in-house Excel "MF Analysis Model": for any AMFI regular-plan fund it simulates **every rolling SIP (and lumpsum) window across horizons of 1–50 years**, derives wealth-multiple distribution statistics, and rolls them into a **Fund Quality Score (FQS, 0–100)** with tiers *Elite / Strong / Good / Average / Weak*. Advisors use it to rank the whole fund universe for a client's tenure and to produce proposals.

> **Branches:** this README describes **`version2`** (the full product). `main` is the earlier MVP (3 screens, 7 endpoints). Which one Render serves is **unconfirmed** — see [docs/deployment/deployment.md](docs/deployment/deployment.md).

## What problem it solves
Advisors previously analysed funds in a spreadsheet on a laptop and hand-copied results into proposal workbooks. This app does the same analysis from NAV history on a phone, ranks thousands of funds consistently, and generates client-ready PDF/XLSX proposals.

## Features
* **Home** – ranked fund universe for your tenure (SIP or Lumpsum), filters, benchmark-XIRR slider, *Since 2010* and *Separate Management Periods* views, Additional Recommendations.
* **Funds** – search by current/old name, old scheme code or ISIN; recently analysed funds.
* **Fund detail** – FQS gauge, plain-English summary, per-horizon distribution metrics, rolling-windows table, start-date filter, old-vs-new-management toggle, AMC history.
* **Proposal builder** – up to 10 funds, allocations, SWP withdrawal plan, PDF / Excel export, email.
* **Rolling** – how stable are a fund's results across periods; multi-fund consistency ranking (PDF).
* **Review** – upload a client portfolio Excel → get it back with FQS, success %, WM/XIRR, CV and remarks merged in.
* **Overlap** – weight-based equity-portfolio overlap between two funds (from AMC monthly disclosures).
* **Data Refresh / Methodology** (drawer) – manual batch triggers, scoring explainer.

## Tech stack
React Native 0.86 · Expo SDK 57 · Expo Router · TypeScript — FastAPI 0.110 · Python 3.11 · Motor/MongoDB — `api.mfapi.in` (NAV data) — Render (API) · MongoDB Atlas · GitHub Actions (cron) · EAS Build (mobile).

## Architecture at a glance
```
Expo app ──HTTPS/JSON (no auth)──► FastAPI monolith ──► MongoDB (fund_nav_meta hub + caches + proposals + lineage)
                                         │  in-process nightly schedulers + background jobs
                                         └──► mfapi.in · amfiindia.com · web.archive.org · AMC disclosure sites
```
Full detail: [docs/SYSTEM_CONTEXT.md](docs/SYSTEM_CONTEXT.md) · [ARCHITECTURE.md](ARCHITECTURE.md) · AI-session brief: [CLAUDE_CONTEXT.md](CLAUDE_CONTEXT.md).

## Repository structure
```
backend/      FastAPI app, scoring engines, jobs, tests (484), scripts
frontend/     Expo app (app/ routes, src/ api+providers+components+utils)
backtest/ backtest_data/   offline backtest toolkit and cached data (not part of the running app)
docs/         architecture, API, DB, features, development, deployment, audits
memory/       PRD.md (historical MVP spec), PROPOSAL_FEATURE_SPEC.md
.github/workflows/  keepalive.yml, daily-refresh.yml
render.yaml · DEPLOY.md · MODEL_AUDIT.md (formula spec) · *_REPORT*.md
```

## Run locally
```bash
# Backend (Python 3.11 + MongoDB at localhost:27017)
cd backend
uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -r requirements.txt
# NB: backend/.env and frontend/.env are TRACKED by git — don't overwrite/commit them. Prefer shell env
# (python-dotenv does not override variables already set in the environment):
export MONGO_URL=mongodb://localhost:27017 DB_NAME=mf_analyst
.venv/bin/uvicorn server:app --reload --port 8000

# Frontend
cd ../frontend && npm install
echo 'EXPO_PUBLIC_BACKEND_URL=http://localhost:8000' > .env.local     # git-ignored (frontend/.gitignore: .env*.local)
npx expo start --web        # or --android / --ios
```
Seed data (a fresh DB shows an empty Home / HTTP 503 "index not built"): `POST /api/home/reindex`, then `POST /api/fqs/refresh`, `POST /api/lumpsum/refresh` (add `?token=` if `REFRESH_TOKEN` is set). Details: [docs/development/local-setup.md](docs/development/local-setup.md).

## Environment variables
Backend: `MONGO_URL`, `DB_NAME`, `REFRESH_TOKEN` (optional operator token), `RENDER_EXTERNAL_URL` (auto on Render), `ARTHVRUKSH_COMPANY_NAME`, `ARTHVRUKSH_ARN` (export branding). Frontend: `EXPO_PUBLIC_BACKEND_URL`. CI: GitHub secrets `BACKEND_URL`, `REFRESH_TOKEN`. See [docs/development/environment.md](docs/development/environment.md).

## Database
MongoDB; collections are created implicitly; indexes are created at boot for `fund_nav_meta` and `fund_holdings`. Schema notes: [docs/database/schema.md](docs/database/schema.md).

## Testing
```bash
cd backend && .venv/bin/python -m pytest tests -q --ignore=tests/test_mf_api.py    # 484 tests, offline, ~5 s
```
`tests/test_mf_api.py` hits a live server (set `EXPO_PUBLIC_BACKEND_URL`). There are **no frontend tests** and no CI test gate.

## Build & deployment
* API: Render Blueprint (`render.yaml`, `rootDir: backend`, `uvicorn server:app`), auto-deploy on push. GitHub Actions: keep-alive every 8 min; nightly refresh 22:00 UTC (03:30 IST).
* Mobile: `eas build -p android --profile preview` (internal APK) / `production` (AAB). Installed APKs cannot be updated remotely.
* Guide: [DEPLOY.md](DEPLOY.md) (partly stale) and [docs/deployment/deployment.md](docs/deployment/deployment.md).

## Important architectural decisions
* Engines are pure and reproduce the Excel model cell-for-cell; a clean-room test pins the SIP maths (`backend/tests/test_model_audit_spec.py`, `MODEL_AUDIT.md`).
* Everything runs in **one web process** sized for 512 MB (keyset paging, projections, serialised batch jobs).
* NAV is prepared in exactly one place (`amfi_client.load_analysis_nav`) with verified predecessor/successor stitching.
* Dead/merged funds are hidden at the data layer, never deleted (`fund_status.py`).
* Backward compatibility with installed APKs is mandatory (legacy `selected_*` fields).

## Known limitations
No user authentication; client PII (name/phone/PAN) in proposals is publicly retrievable by ID; `/api/overlap/ingest` fetches arbitrary URLs; fund-universe span fields don't refresh automatically; long jobs die on restart; reliance on an unofficial free NAV API; no frontend tests/CI. Full register: [docs/audits/](docs/audits/technical-debt.md).
