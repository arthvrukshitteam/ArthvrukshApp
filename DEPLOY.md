# MF Analyst — Render Deployment Guide (with the 3 free-tier fixes)

This guide solves the 3 pain points of Render's free tier:

| Problem | Fix |
|---|---|
| 30-second cold start | **GitHub Actions keepalive** pings `/api/health` every 8 min → backend never sleeps |
| Daily NAV refresh skipped when asleep | **GitHub Actions cron** hits `/api/refresh/run` at 03:30 IST daily |
| MongoDB Atlas setup overhead | One-time 5-minute walkthrough (below) — free forever |

Total cost: **₹0**, total setup time: **~45 minutes**.

---

## STEP 1 — MongoDB Atlas (5 min, one-time)

1. https://www.mongodb.com/cloud/atlas → sign up free.
2. **Build a Database → M0 (Free)** → region **AWS Mumbai `ap-south-1`**.
3. **Database Access → Add user**: username `mfa_admin`, password (auto-generate, **save it**).
4. **Network Access → Add IP → Allow from anywhere** (`0.0.0.0/0`).
5. **Database → Connect → Drivers → Python** → copy the SRV string. Looks like:
   ```
   mongodb+srv://mfa_admin:<PASSWORD>@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority
   ```
   Replace `<PASSWORD>` with the actual password.

---

## STEP 2 — Push repo to GitHub

- In Emergent: click **Save to GitHub** (top-right). Pick a repo name like `mf-analyst`.

---

## STEP 3 — Render (10 min)

1. https://render.com → sign in with GitHub.
2. **New + → Blueprint** → pick your `mf-analyst` repo.
   Render auto-detects the `render.yaml` already in the repo.
3. When prompted, set environment variables:
   - **MONGO_URL** = the Atlas SRV string from Step 1
   - **REFRESH_TOKEN** = generate a random string, e.g. `openssl rand -hex 24` on your laptop, or use a UUID generator — **save it, you'll need it for Step 4**
4. Click **Apply**. First build ~3-5 min.
5. You'll get a URL like `https://mf-analyst-api.onrender.com`.
6. Verify: open `<URL>/api/health` → must return `{"status":"ok","db":"ok",...}`.
   - If `db: down`, your MONGO_URL is wrong or Atlas IP is not `0.0.0.0/0`.

---

## STEP 4 — GitHub Secrets + Actions (5 min) — THIS IS WHAT FIXES THE 3 ISSUES

The repo already contains two ready-to-run GitHub Actions workflows:
- `.github/workflows/keepalive.yml` — pings every 8 min
- `.github/workflows/daily-refresh.yml` — fires at 22:00 UTC = 03:30 IST daily

1. Go to your repo on GitHub → **Settings → Secrets and variables → Actions → New repository secret**.
2. Add **BACKEND_URL** = `https://mf-analyst-api.onrender.com` (no trailing slash).
3. Add **REFRESH_TOKEN** = the same token you put in Render env vars.
4. Go to the **Actions** tab → enable workflows if asked.
5. Open the **keepalive** workflow → **Run workflow** once manually to confirm green.
6. Open the **daily-nav-refresh** workflow → **Run workflow** once manually to confirm green.

✅ Done. From now on, the backend never sleeps and the NAV refresh runs every night at 03:30 IST.

---

## STEP 5 — Build the APK (15 min)

1. Clone your repo locally.
2. Edit `frontend/.env`:
   ```
   EXPO_PUBLIC_BACKEND_URL=https://mf-analyst-api.onrender.com
   ```
3. Edit `frontend/app.json` — set `expo.android.package` (e.g. `com.arthvruksh.mfanalyst`),
   `expo.name`, `expo.version`.
4. From `frontend/`:
   ```bash
   npm install -g eas-cli
   eas login
   eas build:configure
   eas build -p android --profile preview
   ```
5. ~15 min later, EAS gives a direct `.apk` download link. Share with friends.

---

## Monitoring

| Check | Where |
|---|---|
| Is the backend warm right now? | `curl <URL>/api/health` → should respond in <1 s |
| When did NAV refresh last run? | `curl <URL>/api/refresh/status` |
| Are keepalive pings working? | GitHub repo → **Actions** tab → keepalive workflow runs |
| Tail backend logs | Render dashboard → service → **Logs** tab |

---

## Troubleshooting

- **`/api/health` shows `db: down`** → MONGO_URL wrong, or Atlas Network Access not `0.0.0.0/0`.
- **`/api/refresh/run` returns 401** → `REFRESH_TOKEN` query param missing or doesn't match the env var on Render.
- **APK shows "Network request failed"** → `EXPO_PUBLIC_BACKEND_URL` doesn't match the Render URL or includes `/api`.
- **Keepalive workflow doesn't trigger automatically** → GitHub disables scheduled workflows if the repo is inactive for 60 days. A single manual run / commit re-enables them.
- **Render free tier "Service Unavailable"** → free tier has 750 hours/month. With keepalive running 24/7 you'll hit the cap at day 31 of any 30-day month. If that ever happens, upgrade to **Starter ($7/mo)** or reduce keepalive cron to `*/15 * * * *`.

---

## Want to skip GitHub Actions?

Alternative free pinger: https://cron-job.org → add 2 jobs:
1. GET `<URL>/api/health` every 8 minutes
2. POST `<URL>/api/refresh/run?token=<REFRESH_TOKEN>` at `0 22 * * *` UTC

Either way works. GitHub Actions is bundled with the repo and self-documenting; cron-job.org has a web UI but is one more account to manage.
