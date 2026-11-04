# Deployment architecture

> From `render.yaml`, `DEPLOY.md`, `.github/workflows/*`, `frontend/eas.json`, `.emergent/emergent.yml`. **Which branch Render actually deploys, and whether the live `REFRESH_TOKEN` is set, are UNKNOWN / REQUIRES VERIFICATION** (check Render dashboard → service → Settings → Branch).

## Topology

```
Expo app (iOS/Android APK/AAB via EAS, or web)  ──HTTPS──►  Render web service "mf-analyst-api" (FastAPI, 1 process, free plan)
                                                                 │  Motor
                                                                 ▼
                                                      MongoDB Atlas M0 (ap-south-1, per DEPLOY.md)
Render service  ──►  api.mfapi.in (NAV)  ·  amfiindia.com (NAVAll / history)  ·  web.archive.org (lineage)  ·  AMC disclosure sites (overlap ingest, operator-supplied)
GitHub Actions  ──►  Render: keepalive (every 8 min) + nightly refresh (22:00 UTC)
```

| Component | Implementation |
|---|---|
| Hosting | Render Blueprint (`render.yaml`): `type: web`, `runtime: python`, `plan: free`, `rootDir: backend`, `buildCommand: pip install -r requirements.txt`, `startCommand: uvicorn server:app --host 0.0.0.0 --port $PORT`, `healthCheckPath: /api/health`, `autoDeploy: true` |
| Database | MongoDB Atlas free tier (512 MB), Mumbai; network access `0.0.0.0/0` per DEPLOY.md |
| Storage / CDN / DNS | None. No object storage (uploads and generated files live in Mongo or are streamed), no CDN, no custom domain configured in repo |
| Docker | None |
| CI/CD | **No build/test/lint CI.** Only two scheduled workflows. Backend deploy = push to the tracked branch (Render autoDeploy). Frontend = manual `eas build` |
| Mobile distribution | EAS Build `preview` (internal APK, shared manually "with friends" per DEPLOY.md); `production` profile builds an app bundle with `autoIncrement`; `submit.production` is empty. Installed APKs **cannot be updated remotely** (no EAS Update/`expo-updates` in dependencies) — hence the legacy-contract rules |
| Emergent | `.emergent/emergent.yml` pins a base image (`expo_mongo_base_image_cloud_arm`); the project was generated on the Emergent platform and `test_result.md` / `test_reports/` are its artefacts (stale) |

## Scheduled jobs (GitHub Actions)

`keepalive.yml` — cron `*/8 * * * *`: `curl $BACKEND_URL/api/health`, fails the run unless 200 (90 s max).

`daily-refresh.yml` — cron `0 22 * * *` (03:30 IST) + manual: three steps, each `test "$code" = "200"`:
1. `POST /api/refresh/run` (synchronous, `--max-time 1200`)
2. `POST /api/fqs/refresh` (returns immediately; work continues in-process)
3. `POST /api/lumpsum/refresh` (same)

Token is appended only if the `REFRESH_TOKEN` secret exists. History (per commit message of `4a9e0e9d`, not independently re-checked): scheduled runs failed in 6–8 s for at least the 8 days before 2026-09-15 because the earlier workflow required a `REFRESH_TOKEN` secret that was never configured (fixed in `f74ae9bf` on version2 / `4a9e0e9d` on main). The in-process schedulers (22:00/22:30/23:00 UTC) are a second, unreliable trigger on free-tier Render because restarts kill them.

⚠ **Branch skew:** `main`'s `daily-refresh.yml` (updated in `4a9e0e9d`) calls `/api/fqs/refresh` and `/api/lumpsum/refresh`, which **do not exist in `main`'s `server.py`** (7 endpoints only) — steps 2–3 would 404 and fail the workflow if Render is serving `main`. On `version2` they exist.

## Operational runbook (derived)

| Task | How |
|---|---|
| First deploy | Atlas cluster → Render blueprint with `MONGO_URL` → `GET /api/health` → `POST /api/home/reindex` → `POST /api/fqs/refresh` → `POST /api/lumpsum/refresh` → (`POST /api/admin/build-lineage`) |
| Is it warm? | `curl $URL/api/health` (<1 s when warm) |
| Last refresh | `GET /api/refresh/full-status`, `/api/fqs/refresh/status`, `/api/lumpsum/refresh/status`, `/api/home/reindex/status`, `/api/export/status` |
| Why is a fund hidden? | `GET /api/admin/fund-status` |
| Pick up a new merger | `POST /api/admin/merger-discovery` (then poll status) or add to `fund_lineage.CURATED_SUCCESSORS` + redeploy |
| New APK | edit `frontend/.env`, `eas build -p android --profile preview` (DEPLOY.md §5) |

## Known deployment hazards
* 750 free hours/month vs 24×7 keep-alive (DEPLOY.md warns of the day-31 cap).
* GitHub disables scheduled workflows after 60 days of repo inactivity.
* Free instance = 512 MB: batch jobs, bulk Excel jobs, lineage crawl, ranking runs and normal traffic share it.
* `DEPLOY.md` (identical on both branches) still describes only the original single `/refresh/run` workflow, instructs you to set `REFRESH_TOKEN` in both places (the workflow now treats it as optional), and has "Save to GitHub from Emergent" steps; the three-step nightly job is documented only in the workflow comments. `README.md`/`DEPLOY.md` diverge from reality in places — this file and the workflows are authoritative.
