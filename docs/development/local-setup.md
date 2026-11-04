# Local setup

> Steps marked ✅ were executed during this research pass (macOS, `uv` 3.11 venv). Steps marked ⚠ were **not** run.

## Prerequisites

* Python **3.11** (`backend/.python-version` = 3.11.9; the system `python3` on the research machine was 3.9, which is **not** supported by the pinned deps/`from __future__` usage — use 3.11).
* Node 18+/npm, and Expo tooling via `npx expo` (⚠ not installed during research).
* MongoDB reachable at `MONGO_URL` (default `mongodb://localhost:27017`). Needed for any endpoint that touches the DB; the app *boots* without it (Motor connects lazily) and `/api/health` then reports `db: "down"`.

## Backend

```bash
cd backend
uv venv --python 3.11 .venv          # ✅ (`.venv/` is git-ignored; a throw-away venv elsewhere also works)
uv pip install --python .venv/bin/python -r requirements.txt     # ✅
```

Configure via **shell environment** rather than editing `backend/.env`: root `.gitignore` lists `.env`, yet **`backend/.env` and `frontend/.env` are tracked by git on both branches**, so overwriting them creates a diff you must not commit (see security audit). `python-dotenv` (`load_dotenv`, default `override=False`) never overrides variables already exported:
```bash
export MONGO_URL=mongodb://localhost:27017 DB_NAME=mf_analyst
# export REFRESH_TOKEN=<optional — if set, operator POSTs need ?token=>
```

Run:
```bash
cd backend && .venv/bin/uvicorn server:app --reload --port 8000     # ⚠ boot verified by importing server.app (53 routes), not by serving traffic
curl localhost:8000/api/health
```

### Seeding data (a fresh DB shows an empty Home)
Home returns **503** until the universe index exists. With the server running:
```bash
curl -X POST "localhost:8000/api/home/reindex"          # crawls the regular, non-IDCW schemes from mfapi.in (a comment in server.py measured 12 210 after filtering); resumable; takes a long time
curl -X POST "localhost:8000/api/fqs/refresh"           # then SIP scores (hours for the full universe)
curl -X POST "localhost:8000/api/lumpsum/refresh"       # then lumpsum scores
curl -X POST "localhost:8000/api/admin/build-lineage?quick=true"   # optional: lineage for search/AMC history (archive.org + AMFI)
```
Add `?token=…` if `REFRESH_TOKEN` is set. For development, analysing a single fund (`GET /api/funds/118778/analyze`) needs no seeding and populates `fund_analysis`/`fund_recent`. Overlap needs holdings ingested (`POST /api/overlap/ingest-upload`).

### Tests
```bash
cd backend
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests -q --ignore=tests/test_mf_api.py     # ✅ 484 passed in ~5 s, no network/DB needed
```
`tests/test_mf_api.py` is a **live-server** regression test: it reads `EXPO_PUBLIC_BACKEND_URL` and otherwise defaults to a hard-coded Emergent preview URL (`fund-scorer.preview.emergentagent.com`) — it also asserts removed endpoints (`/funds/top`, `/goal/*`) 404. Do not run it blind; point it at your own server.

## Frontend

```bash
cd frontend
npm install                                   # ⚠
echo "EXPO_PUBLIC_BACKEND_URL=http://localhost:8000" > .env.local     # git-ignored via `.env*.local`; no trailing slash, no /api; for a phone/emulator use your LAN IP
npx expo start --web        # or: npm run web | android | ios      ⚠
npm run lint                # expo lint (eslint-config-expo)          ⚠
npx tsc --noEmit            # strict TS check — no script defined      ⚠
```
`.claude/launch.json` defines a preview task `frontend-web` on port 8098.

Native builds (EAS): `eas.json` profiles `development` (dev client, apk), `preview` (internal apk), `production` (app-bundle, autoIncrement). `.easignore` at repo root exists because EAS archives from the git root and the repo contains >600 MB of backend/backtest data.

## Operating the whole stack locally
1. Mongo up → 2. backend on :8000 → 3. frontend with `EXPO_PUBLIC_BACKEND_URL` → 4. onboarding (mode, tenure) → Home. If Home says "Fund index not built yet", run the seeding calls above.

## Common problems
| Symptom | Cause |
|---|---|
| Home "Fund index not built yet" (HTTP 503) | `nav_meta_refresh_log/nav_meta` missing — run `/api/home/reindex` |
| APK "Network request failed" | `EXPO_PUBLIC_BACKEND_URL` wrong or includes `/api` (code strips a trailing `/api` anyway) |
| Search finds nothing historic | lineage not built (`/admin/build-lineage`) — search degrades to name scan |
| Fund Detail "AMC History" missing | `/funds/{c}/lineage` 404 until lineage built (by design) |
| Slow first analyze on a free Render | cold start + live mfapi.in fetch |
