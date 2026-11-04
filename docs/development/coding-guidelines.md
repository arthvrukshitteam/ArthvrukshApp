# Coding guidelines (as practised in this repo)

Derived from reading the code; follow these so new work is indistinguishable from the existing style. Where the repo is inconsistent it is called out.

## Backend (Python)

* **Layout:** flat modules in `backend/`; one responsibility per module; engines are pure (no DB), routers are thin, DB/HTTP at the edges. New feature = new engine module + `*_routes.py` exposing `build_router(db, …injected callables…)`, mounted at the bottom of `server.py`. Routers receive `_build_analysis`/`_cached_analysis` as callables (no circular import of `server`).
* **Naming:** `snake_case`; private helpers `_leading_underscore`; constants `UPPER_SNAKE`; collection names as module constants (`COLLECTION`, `NAV_META_COLLECTION`).
* **Async:** Motor with `await`; CPU-heavy scoring in `asyncio.to_thread` in batch code (⚠ the interactive `_build_analysis` path still computes inline — don't copy that). Bound concurrency with `asyncio.Semaphore`; page big reads with `batch_support.iter_batches`; always project fields.
* **Memory (512 MB target):** no `[doc async for doc in cursor]` over the universe; drop big maps as soon as the needed slice is read; sequential file ingest.
* **Errors:** raise `HTTPException` with the existing codes (400 input, 404 missing, 409 state, 422 semantically unprocessable, 502 upstream, 503 not-ready). Background tasks log and continue.
* **Validation:** Pydantic models for bodies, `Query(ge/le)` for params; dates via `_parse_date`.
* **Comments:** this codebase writes long "why" comments with measured evidence and dated `BUG FIXED (…)` notes. Match that for non-obvious decisions; keep numbers in comments true (several are now stale — see technical-debt).
* **Single sources of truth** — extend, never fork: NAV prep `amfi_client.load_analysis_nav`; WM↔XIRR `xirr_utils`; variant selection `continuity_select`; Additional Rec rule `additional_rec`; dead-fund rule `fund_status`; Indian formatting `indian_format`/`format.ts`; fund-name matching `bulk_analysis.match_fund/match_key`.
* **Admin endpoints:** copy the `REFRESH_TOKEN` pattern *only* until a shared dependency exists; prefer introducing the dependency over an 11th inline copy.
* **Tests:** pytest, plain functions, no DB (use fakes/pure functions); name `test_<module>.py`; clean-room spec tests (`test_model_audit_spec.py`) pin the scoring maths.

## Frontend (TypeScript / React Native)

* **Structure:** screens under `app/` (expo-router), reusable pieces in `src/components/`, contexts as `src/*Provider.tsx`, pure helpers in `src/utils/`. All HTTP goes through `src/api.ts`; add the type next to the method.
* **Styling:** `StyleSheet` factories taking `(colors, type)` memoised with `useMemo`, tokens from `src/theme.ts`; do not hard-code colours (existing violations exist — don't add more). Icons: `lucide-react-native`.
* **State:** local `useState` per screen; shared state only via the existing Providers; persist with `storage` (not raw AsyncStorage — `ModeProvider`/`TenureProvider` are the legacy exceptions).
* **Imports:** `@/src/...` alias. **Do not import `@react-navigation/*` directly** (Expo SDK ≥56 hard-errors in Metro; use `expo-router` / `expo-router/drawer`).
* **Loading/empty/error:** every data screen has skeleton, empty-state copy and an error path; errors are shown inline or via `Alert.alert` (no toast lib).
* **Platform splits:** `*.web.ts` files; branch with `Platform.OS` for downloads (`FileSystem.downloadAsync` vs `window.open`) and uploads (multipart on web, base64 JSON on native).
* **Responsive:** currently flex-only, portrait; if you add tablet/desktop layouts introduce a shared breakpoint hook rather than ad-hoc `Dimensions` use.
* **Types are strict** but hand-written; when a backend response shape changes, update `src/api.ts` *and* keep backend aliases for old installed APKs.

## Git / process
* Commit messages in this repo are long, narrative and cite evidence; `Co-Authored-By` trailers are used. History includes auto-generated "auto-commit for <uuid>" commits from the Emergent platform.
* No PR template, no CI test gate (see testing audit).
