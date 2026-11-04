# Frontend architecture

> Verified against `origin/version2` @ `8a0089a1`, `frontend/` (≈13.4k lines TS/TSX). No frontend tests exist and `tsc`/`expo lint` were **not run** in this research pass (no `node_modules`) → type-health is UNKNOWN.

## 1. Stack

| Concern | Actual |
|---|---|
| Framework | React Native 0.86 + Expo SDK ^57, React 19.2.3, `newArchEnabled: true` |
| Targets | iOS, Android (EAS builds), **web** (`expo start --web`, metro, `output: "static"`) — one codebase |
| Language | TypeScript (`strict: true`), path alias `@/* → frontend/*` |
| Routing | `expo-router` ~57 (file-based, typed routes on) |
| Package manager | npm (`package-lock.json`) |
| Build | Metro; EAS Build (`eas.json`: development / preview (apk) / production (app-bundle)); metro cache dir `.metro-cache` (ignored on version2, **committed on main** — 23.8k files) |
| State | React Context (5 providers) + per-screen `useState`. **No** Redux/Zustand/React-Query/SWR |
| Networking | `fetch` wrapper in `src/api.ts` (no retry, no timeout, no abort, no auth header) |
| Persistence | `AsyncStorage` (prefs) + `expo-secure-store` (refresh token), via `src/utils/storage` (`.web.ts` variant on web) |
| UI libs actually imported | `lucide-react-native` (icons, 21 files), `react-native-svg`, `expo-linear-gradient`, `expo-document-picker`, `expo-file-system`, `expo-sharing`, `expo-mail-composer`, `expo-router/drawer` (needs `react-native-reanimated` + gesture-handler as peers) |
| Declared in `package.json` but **never imported** under `app/` or `src/` | `react-native-gifted-charts`, `expo-video`, `expo-blur`, `expo-haptics`, `react-native-webview`, `expo-symbols`, (`expo-image`/`expo-web-browser` only as config plugins) — bundle-size / upgrade-surface debt |
| Analytics / feature flags / crash reporting | **None** (grep for analytics/Sentry/feature-flag found nothing) |
| Env | `EXPO_PUBLIC_BACKEND_URL` only (no trailing `/` or `/api`; baked into the bundle) |

## 2. Navigation tree (actual)

```
app/_layout.tsx  (Stack; providers; ModeGuard)
├─ (drawer)/_layout.tsx        Drawer: Dashboard | Methodology | Data Refresh | Analysis Mode
│   ├─ (tabs)/_layout.tsx      Custom bottom tab bar, 5 tabs
│   │    ├─ home              (drawer)/(tabs)/home.tsx            /home
│   │    ├─ index             (drawer)/(tabs)/index.tsx           /  (Funds search + recent)
│   │    ├─ rolling           (drawer)/(tabs)/rolling.tsx         /rolling
│   │    ├─ portfolio-review  (drawer)/(tabs)/portfolio-review.tsx
│   │    └─ overlap           (drawer)/(tabs)/overlap.tsx
│   └─ insights               (drawer)/insights.tsx               /insights  (Methodology)
├─ fund/[code]                app/fund/[code].tsx                 ?horizon&since2010&mgmt&contDate
├─ mode-picker                SIP vs Lumpsum (first launch; gesture disabled)
├─ tenure-picker              years input (any positive number, clamped to 50 at the network edge)
├─ data-refresh               manual batch triggers + status
├─ proposal/build             build proposal from Home selection
└─ proposal/[id]              "Under Review" – edit, recalc, export, email
```

`ModeGuard` (root layout) redirects: no mode → `/mode-picker`; mode but no tenure → `/tenure-picker`. Drawer entries hard-code routes (`/insights`, `/data-refresh`, `/mode-picker`). Drawer "Dashboard" pushes `/home`.

### Route table

| Path | Screen | Auth/role | API calls | Navigates to |
|---|---|---|---|---|
| `/mode-picker` | `app/mode-picker.tsx` | none | – | `/tenure-picker` |
| `/tenure-picker` | `app/tenure-picker.tsx` | none | – | `/home`, `/mode-picker`, `/data-refresh` |
| `/home` | `home.tsx` (`UniverseScreen`) | none | `GET /home/universe` or `/lumpsum/universe` | `/fund/{code}`, `/tenure-picker`, `/proposal/build` |
| `/` (Funds) | `index.tsx` | none | `GET /funds/recent`, `/refresh/full-status`, `/funds/search` | `/fund/{code}` |
| `/rolling` | `rolling.tsx` | none | `POST /rolling-analysis/run`, `/rank/run`, `/rank/{id}/add-fund`, `GET /rank/{id}`, PDF exports | – |
| `/portfolio-review` | `portfolio-review.tsx` | none | `POST /bulk-analysis/upload[-base64]`, `/{id}/process`, `GET /status`, `/download` | – |
| `/overlap` | `overlap.tsx` | none | `GET /overlap/funds`, `/overlap/compare` | – |
| `/insights` | `insights.tsx` | none | none (static copy) | – |
| `/fund/[code]` | `fund/[code].tsx` | none | `GET /funds/{code}/analyze` (SIP) or `/analyze/lumpsum`; `GET /funds/{code}/lineage` (AMC history section) | `/proposal/build` |
| `/data-refresh` | `data-refresh.tsx` | optional token (SecureStore) | `GET /fqs/refresh/status`, `/lumpsum/refresh/status`; `POST /fqs/refresh`, `/lumpsum/refresh` | – |
| `/proposal/build` | `proposal/build.tsx` | none | `POST /proposal/build` | `/proposal/{id}` (replace) |
| `/proposal/[id]` | `proposal/[id].tsx` | none | `GET /proposal/{id}`, `POST /proposal/build` (recalc), `export.pdf|xlsx` | – |

There is no login, no role concept, no protected route in the client.

## 3. State management

| Provider (`src/`) | State | Persistence | Consumers |
|---|---|---|---|
| `ThemeProvider` | `mode` light/dark/system, resolved `colors`, `type`, tier/score colour helpers | AsyncStorage key `theme_mode` | all |
| `ViewModeProvider` | `simple` / `analyst` density | key `view_mode` | fund detail |
| `ModeProvider` | `sip` / `lumpsum` | key `@arthvruksh:investment_mode` | guard, Home, fund detail |
| `TenureProvider` | tenure years (int ≥ 1) | key `@arthvruksh:tenure` | guard, Home |
| `ProposalDraftProvider` | selection mode, selected funds, captured `since2010` / `separateManagement` | **memory only** (lost on reload) | Home, fund detail, proposal/build |

Screen-local state covers all server data (`result`, `data`, `error`, `loading`…). Consequences: every navigation refetches; no cross-screen cache; Home's toggles (`since2010`, `separateManagement`) live in `UniverseScreen` and are re-passed to Fund Detail via **URL params** and to the Proposal builder via the draft provider. Stale-data risks: Fund Detail forces `refresh` when the predecessor toggle flips (otherwise the backend would serve the cached default run).

Duplicated storage layers: `ModeProvider`/`TenureProvider` call `AsyncStorage` directly while Theme/ViewMode use `storage` wrapper. `TenureProvider`'s header comment ("capped at 15Y", "Lumpsum 3/5/7Y") is stale — code supports 1–50.

## 4. API layer (`src/api.ts`)

Single `api` object (unused members: `refreshRun`, `refreshStatus`, `meta`, `overlapCoverage`); `get<T>`/`post<T>` throw `Error("HTTP {status}: {body}")`. Absolute-URL builders (`proposalExportUrl`, `rollingAnalysisExportUrl`, `bulkAnalysisDownloadUrl`…) feed `FileSystem.downloadAsync` (native) or `window.open` (web). Tenure is clamped `Math.min(tenure, 50)` in `api.universe`/`buildProposal` (comment above `MAX_CALC_YEARS` still says 15). The refresh token, if any, is sent as `?token=` in the query string. Full endpoint ↔ caller mapping: [api-reference](../api/api-reference.md).

Contract subtleties:
* `UniverseMetrics` fields (`fqs`, `blended_wm`, …) are the current names; the backend also emits legacy `selected_*` aliases for old APKs — **do not remove** (see `server.py:_LEGACY_ALIAS_KEYS`).
* Types in `api.ts` are hand-written, not generated; `FundAnalysis.horizons[].windows` for SIP and `LumpsumHorizonStats.windows` differ in shape (units/end_nav vs starting/ending_nav).

## 5. Components

Shared (`src/components/`): `BrandLogo`, `CollapsibleSection`, `DrawerMenuButton`, `FqsGauge` (SVG), `FundDetailShared` (813 lines: styles factories, table cells, modals, banners, AMC history section, add-to-portfolio bar), `FundSearchModal`, `HorizonInsights` (`PlainEnglishSummary`, `SmartInsights`, `WmHistogram`), `InfoHint`, `KPICard`, `ViewModeToggle`. Glossary copy in `src/glossary.ts`. Pure helpers: `utils/xirr.ts` (WM↔XIRR — **third copy** of the formula), `utils/format.ts` (Indian digit grouping; mirror of `backend/indian_format.py`).

Large single-file screens: `home.tsx` 2081 lines (contains ~20 inline components), `rolling.tsx` 1194, `fund/[code].tsx` 921 (two screens: `SipFundDetailScreen`, `LumpsumFundDetailScreen`, selected by `useMode()`), `proposal/[id].tsx` 846. Duplicated logic: SIP vs Lumpsum fund-detail screens; `tierColor` exists both in `theme.ts` and a local `TIER_COLORS` map in `home.tsx`.

## 6. Forms, validation, uploads, lists

* Validation is minimal and client-side only where present (tenure ≥ 1, date strings YYYY-MM-DD in `StartDateModal`, allocations summing to 100 % in the proposal editor); the backend re-validates (Pydantic).
* Uploads: Portfolio Review uses real multipart on web, **base64-over-JSON on native** (workaround for RN FormData failures; documented in `bulk_analysis_routes.py`). 10 MB cap enforced server-side only.
* Lists: `FlatList` for Home/Funds; Fund Detail rolling-window table paginates client-side (`WINDOWS_PAGE_SIZE = 30`). Search = debounced call to `/funds/search`; Home search is a client-side name filter over the already-ranked list. No server-side pagination anywhere (Home receives the whole eligible universe).
* Notifications: no toast library. `Alert.alert` (proposal, rolling, review, refresh), inline banners (`FundDetailBanners`, auto-dismiss 6 s), empty/error components.
* Loading states: skeleton cards (Home, Fund Detail), `ActivityIndicator`, pull-to-refresh.

## 7. UI/UX system

* Design tokens: `src/theme.ts` (dark default `#07090E`, light variant; azure `#2563EB` primary; tier colours Elite emerald / Strong blue / Good amber / Average orange / Weak red; `radii` 8/12/16/pill; `space` 4/8/16/24/32; `makeType` scale). `design_guidelines.json` is the original brief (mentions IBM Plex Sans — **no font is loaded except SpaceMono**; the app uses system fonts).
* Hard-coded colours bypass tokens in several places (`home.tsx` `TIER_COLORS`, CV colours, `insights.tsx` icon colours).
* Responsive: **no breakpoints / `useWindowDimensions`** anywhere. Layout is flex-based, portrait-locked (`orientation: portrait`). Tablet is `supportsTablet: true` (iOS) but no tablet-specific layout exists. Web renders the mobile layout full-viewport (`+html.tsx` fixes body scroll).
* Accessibility: effectively absent (9 `accessibility*` props across the app; tab bar has `accessibilityRole`).
* Inconsistency to know about: SIP vs Lumpsum copy; two hard-coded "recommended by" strings (`RECOMMENDED_BY` in `api.ts` vs backend `COMPANY_LEGAL_NAME`); stale comments about 15Y cap.

## 8. Performance notes

* `/home/universe` returns thousands of funds in one payload → one big `FlatList` (virtualised) plus several `useMemo` re-sorts keyed on filters. No pagination, no response caching, no ETag.
* Fund Detail recomputes derived values with `useMemo`; windows table paginated.
* No bundle analysis; web build is a full mobile bundle.
