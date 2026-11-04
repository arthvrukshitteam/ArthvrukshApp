> ⚠️ **HISTORICAL — describes the original 3-screen MVP (`main` branch).** Verified out of date on 2026-10-07 against `version2`:
> * Scope is far larger now (Home universe, Rolling, Portfolio Review, Overlap, Proposals, lumpsum mode, lineage…) — see `docs/features/feature-inventory.md`.
> * **FQS weights below are wrong for the live code.** Live (`fqs_engine.score_horizon`): equity Success .30 / Adj-Risk .20 / Blended-WM .20 / Consistency .15 / Downside .10 / ES .05; non-equity .25 / .35 / .15 / .10 / .10 / .05. (`MODEL_AUDIT.md` is the authoritative spec.)
> * Horizons are 1Y–**50Y** (not 20Y); the nightly refresh covers the whole indexed universe, not just recently viewed funds.
> * `fund_analysis` cache logic is as described; "Methodology tab" is now in the drawer.
> Kept for product-intent context only; do not use it as a technical source.

# MF Analyst — PRD

## Vision
A mobile-first **MFD analyst tool** that faithfully replicates the in-house
Excel MF Analysis Model so any AMFI-listed fund can be analysed end-to-end on
a phone — no laptop, no spreadsheet.

## End Goal
Open the app, search any fund, see the same six-stage pipeline output that the
Excel produces — daily NAV → month-end → rolling SIP windows (1Y–20Y) →
distribution metrics → FQS — for **any** scheme exposed by AMFI.

## Scope (current build)
1. **Funds tab** — search across the 37k+ AMFI scheme universe + recently-analysed
   list with cached FQS pills + refresh-status banner.
2. **Fund Analysis screen** — analyst-grade detail per fund:
   - Header: scheme name, fund-house, category chip, NAV coverage range.
   - Headline strip: Headline FQS / XIRR Inception / Cycle Robustness / Stability 10/5.
   - Start-date filter (bottom-sheet, presets + custom YYYY-MM-DD).
   - Horizon picker — every year from 1Y to 20Y, FQS per chip, disabled where data is short.
   - DISTRIBUTION METRICS section — Min WM Required, N Windows, Min/Max/Median/Mean WM,
     Std Dev, CV, P10/25/75/90, ES10, Failure %, Success %.
   - Sub-scores section — Failure Score, Downside Severity, CV Score, Risk Score,
     Adj Risk, Blended WM + Score, Downside Score, Consistency, ES Score, **FQS**.
   - Rolling windows table — every SIP window with start/end dates, units, end NAV,
     end value, total invested, WM, Bar pass/fail. Horizontally scrollable.
3. **Methodology tab** — pipeline diagram, equity vs non-equity weight tables,
   tier table, full MIN-WM benchmark grid.

## Engine (matches Excel exactly)
- Pipeline: RAW_NAV → MONTHLY_NAV (last NAV per calendar month) →
  SIP_CASHFLOWS (full-history XIRR via `pyxirr`) → ROLLING_SIP_{N}Y (every
  rolling window, monthly step) → DISTRIBUTION_METRICS → FQS.
- Horizons: **1Y through 20Y**, computed only where data is sufficient.
- Start-date filter: applied before month-end extraction, recomputes everything.
- Weights — Equity: Success 35%, Adj Risk 25%, Blended WM 20%, Downside 10%, ES 10%.
- Weights — Non-equity: Success 25%, Adj Risk 35%, Blended WM 15%, Consistency 10%,
  Downside 10%, ES 5%.
- Aggregates: **Cycle Robustness** (weighted Success% across 3/5/10/15) and
  **Stability Check** (Median 10Y / Median 5Y).
- Min-WM benchmark table per (category × horizon) with category-specific cap;
  horizons beyond 15Y derived from each category's XIRR via the SIP-FV closed form.

## Data
- Source: AMFI via `api.mfapi.in/mf/{scheme_code}` (free, no auth).
- Persistence: `fund_analysis` (24-h cache, default no-filter only),
  `fund_recent` (tracks every fund opened), `refresh_log` (singleton).

## Daily NAV Refresh
- FastAPI background task starts on app boot, sleeps until 22:00 UTC = 03:30 IST.
- Refreshes every fund opened in the last 30 days (concurrency 4).
- Manual trigger: `POST /api/refresh/run`. Status: `GET /api/refresh/status`.

## Removed (vs prior MVP)
- Curated top-funds list / podium (`/api/funds/top` → 404).
- Goal Planner tab and `/api/goal/*` endpoints — out of scope for analyst tool.

## Future enhancements
- Per-window data export (CSV / share-sheet).
- Side-by-side fund compare (2–3 schemes).
- Watchlist + push notification when FQS tier slips.
- Save filtered analyses (e.g., "Nippon SC since 2020").
