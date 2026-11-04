# Authentication & authorization

> Verified against `origin/version2` @ `8a0089a1`.

## Summary

**There is no user authentication, no accounts, no sessions, no JWT/cookies, no RBAC, no roles.** The system has exactly one trust mechanism: an *optional* shared secret (`REFRESH_TOKEN`) guarding a subset of operator endpoints. Everything else — including endpoints that read and write client PII — is public to anyone who can reach the server URL.

| Question | Answer (evidence) |
|---|---|
| Login / registration flow | None (no such route in `server.py` or routers; no login screen in `frontend/app`) |
| Session / token mechanism | None for users |
| Token storage (client) | Only the optional operator token: `expo-secure-store` (Keychain / EncryptedSharedPreferences) key `TOKEN_KEY` in `app/data-refresh.tsx`; on **web** `index.web.ts` falls back to AsyncStorage/localStorage (secure methods are not secure there — confirm in `utils/storage/index.web.ts`) |
| Refresh mechanism | None |
| Password handling | None — no passwords anywhere |
| Roles | None. Informally: "advisor" (the person using the app) vs "operator" (anyone who holds `REFRESH_TOKEN`). The code never distinguishes them |
| Protected routes (client) | None (`ModeGuard` only enforces onboarding order, not identity) |
| Protected APIs | See matrix below |
| Admin functionality | `/admin/*`, `/home/reindex`, `/fqs/refresh`, `/lumpsum/refresh`, `/refresh/run`, `/continuity/events` (write), plus the in-app "Data Refresh" screen |

## How the token check works

```python
expected = os.environ.get("REFRESH_TOKEN")
if expected and token != expected:        # token = ?token=... query param
    raise HTTPException(401, "Invalid token")
```
(`server.py`, repeated inline in 10 handlers — no shared dependency.)

* If `REFRESH_TOKEN` is **unset** → the endpoint is open. The in-app comment in `app/data-refresh.tsx` says "this deployment currently doesn't set it"; `src/api.ts` says "it is, in production". These conflict; the commit message of `4a9e0e9d` ("only BACKEND_URL was ever configured") supports *unset*. **Production state: UNKNOWN / REQUIRES VERIFICATION** (`gh secret list` + Render env).
* Comparison is non-constant-time `!=`; token travels in the query string (appears in access logs, Render logs, GitHub Actions logs, browser history on web).
* No throttling on failed attempts.

## "Role × feature" matrix (what the code actually enforces)

| Feature / endpoint group | Anonymous | Holder of `REFRESH_TOKEN` (when configured) |
|---|---|---|
| Fund search / analyze / universe / meta / lineage / history-debug / categories | ✅ | ✅ |
| Proposal build, read, export (PDF/XLSX) — **includes client name/phone/PAN** | ✅ | ✅ |
| Rolling analysis (single, ranking, exports) | ✅ | ✅ |
| Bulk analysis upload/process/download | ✅ | ✅ |
| Overlap read (`funds`, `coverage`, `compare`, `sources`) | ✅ | ✅ |
| Overlap **write** (`/ingest`, `/ingest-upload`) | ✅ (**no gate at all**) | ✅ |
| `GET /admin/fund-status`, `GET /admin/build-lineage/status`, `GET /continuity/events`, `/export/*`, all `GET …/status` | ✅ | ✅ |
| `POST /refresh/run`, `/fqs/refresh`, `/lumpsum/refresh`, `/home/reindex` | only if token unset | ✅ |
| `POST/DELETE /continuity/events`, `POST /admin/merger-discovery`, `GET /admin/merger-discovery/status`, `POST /admin/fund-status/sync`, `POST /admin/build-lineage` | only if token unset | ✅ |

## Authorization gaps to treat as design facts

1. **Object ownership does not exist.** Proposals, rolling runs, ranking runs and bulk jobs are addressed purely by a random ID in the URL; anyone holding the ID can read/export them (IDs are 64-bit hex for proposals/rolling, full uuid4 for bulk jobs — unguessable, but unrevoked and unauthenticated; IDs also appear in exported filenames/URLs the advisor shares).
2. **Proposal PII** (`client_pan`, `client_phone`, `client_name`) is returned by `GET /proposal/{id}` and embedded in export filenames without authentication.
3. **Client-side secret handling:** `EXPO_PUBLIC_*` variables are public by construction; the backend URL is the only value shipped. No API keys exist in the bundle.
4. **Multi-tenancy:** none. Every advisor sees the same shared `fund_recent`, caches, and (if they know the ID) everyone's proposals.

## What to do before adding auth (guidance for future work)

* Add a FastAPI dependency (`Depends(require_operator)`) and replace the 10 inline checks — do not add an 11th copy.
* Make a *deliberate* decision per router: proposal/bulk/rolling routers take `db` and injected callables via `build_router(...)`; add the dependency inside those factories.
* The legacy-APK compatibility rule applies to auth too: an installed APK cannot be updated remotely, so any change that makes the API reject unauthenticated calls will break every old build (see [CLAUDE_CONTEXT.md](../../CLAUDE_CONTEXT.md) "Do not change casually").
* Never move the operator token into the JS bundle.
