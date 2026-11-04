# Environment & configuration

> Names verified by grepping `os.environ`/`process.env`/workflow files on `origin/version2` @ `8a0089a1`. **Values were intentionally not read** (`.env` files are tracked in git; a tool-permission check blocked reading them and that was respected). Production values: **UNKNOWN**.

## Backend (process environment or `backend/.env` via `python-dotenv`)

| Variable | Read in | Default | Purpose / notes |
|---|---|---|---|
| `MONGO_URL` | `server.py`, `export_mongo_to_excel.py`, scripts | `mongodb://localhost:27017` (warning logged) | Mongo connection string (Atlas SRV in prod). **Secret.** |
| `DB_NAME` | same | `mf_analyst` | Database name |
| `REFRESH_TOKEN` | `server.py` (10 handlers) | unset ⇒ gated endpoints open | Shared operator secret, sent as `?token=` |
| `RENDER_EXTERNAL_URL` | `server.py`, `fqs_refresh`, `lumpsum_refresh`, `nav_meta_indexer` | unset ⇒ keep-alive self-ping disabled | Auto-set by Render; used so long jobs ping `/api/health` every 4–5 min |
| `ARTHVRUKSH_COMPANY_NAME` | `proposal_pdf.py`, `proposal_xlsx.py` | `ArthVruksh MFDGuru LLP` | Legal name printed on exports |
| `ARTHVRUKSH_ARN` | same | `365554` | AMFI ARN printed on exports (regulatory identifier — treat as compliance data) |
| `PORT` | Render start command | – | Uvicorn bind |
| `PYTHON_VERSION` | `render.yaml` | `3.11` | Render runtime pin |

No other `os.environ` reads exist in the backend (grep-verified). `load_dotenv(backend/.env)` runs at import of `server.py`.

## Frontend (Expo)

| Variable | Read in | Purpose |
|---|---|---|
| `EXPO_PUBLIC_BACKEND_URL` | `src/api.ts` | API origin. Inlined into the JS bundle at build time (public by design). Trailing slashes and a trailing `/api` are stripped. Missing ⇒ `console.warn` and every call throws "not configured". |
| `METRO_CACHE_ROOT` | `metro.config.js` | Optional Metro cache location |

`app.json` carries `expo.extra.eas.projectId`, bundle ids (`com.arthvruksh.mfanalyst`), and `owner: beingtharur`. No runtime feature flags exist anywhere.

## CI / hosting configuration

| Where | Name | Purpose |
|---|---|---|
| GitHub Actions secrets | `BACKEND_URL` (required by both workflows) | Render service origin |
| GitHub Actions secrets | `REFRESH_TOKEN` (optional since `f74ae9bf`) | must equal the server's `REFRESH_TOKEN` if the server has one |
| Render env | `MONGO_URL` (`sync:false` in `render.yaml`), `DB_NAME`, `PYTHON_VERSION`, optionally `REFRESH_TOKEN`, `ARTHVRUKSH_*` | |

## Secrets strategy (as implemented)

* Backend secrets: environment variables on Render; **no secret manager**.
* Operator token on the client: `expo-secure-store` on native, plain AsyncStorage on web.
* ⚠ `backend/.env` (61 bytes) and `frontend/.env` are **tracked in git** (root `.gitignore` ignores `.env`, but they were committed). Whether they hold real secrets is UNKNOWN; at 61 bytes `backend/.env` is probably only `MONGO_URL`/`DB_NAME` for a local DB. Verify, and if anything real was ever committed, rotate it (history keeps it).

## Test environment
`tests/test_mf_api.py` honours `EXPO_PUBLIC_BACKEND_URL` (falls back to a hard-coded Emergent preview URL). All other tests need no environment.
