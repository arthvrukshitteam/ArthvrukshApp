# Portfolio Proposal Generation — Design Spec

Automates the manual workflow: analyse funds in-app → hand-copy into
`ArthVruksh MF Proposal_*.xlsx` → convert to PDF → share.

## Template analysis (source of truth)

Uploaded workbook has 3 sheets, no cover/branding/charts — a bare calculation
grid the advisor fills by hand:

- **SIP** — per fund: FQS / Success% / Median WM / Median XIRR / P10 WM / P10
  XIRR at 3 hardcoded durations (3/5/7Y), each fund's `% Allocation`. Bottom
  rows blend into portfolio-level Capital Invested, Expected Wealth (Median &
  P10), blended Wealth Multiple, blended XIRR — per duration.
- **Lumpsum** — identical structure, WM/XIRR formulas differ (single
  investment vs monthly SIP annuity).
- **SWP** — independent 120-month withdrawal amortisation table (corpus,
  return %, inflation %, monthly withdrawal → month-by-month balance). Its
  inputs don't reference the SIP/Lumpsum sheets in the template.
- XIRR is always derived from WM via Excel `RATE()`:
  `SIP: RATE(years*12,-1,0,WM*years*12)*12`,
  `Lumpsum: WM^(1/years) - 1`.
  (Already ported to frontend `src/utils/xirr.ts` and `server.py`
  `_sip_wm_to_xirr` / `_lumpsum_wm_to_xirr` — canonicalised again in
  `backend/xirr_utils.py` for the proposal engine to avoid a 3rd copy drifting.)

## Decisions (confirmed with user)

1. Every proposal always generates all 3 sheets (SIP + Lumpsum + SWP) —
   a complete plan regardless of which mode the advisor used for analysis.
2. Durations are advisor-selectable (any 3 distinct horizons, 1–20Y) instead
   of hardcoded 3/5/7 — reuses the app's existing horizon range.
3. SWP corpus/return are **auto-derived but editable**: default corpus =
   the SIP-mode expected median wealth at the longest of the 3 chosen
   horizons; default return = the SIP-mode blended median XIRR at that same
   horizon. Advisor can override either, plus must always supply inflation %
   and monthly withdrawal (no sensible default).
4. Exports get a light cover section (client name, advisor, date, ArthVruksh
   branding) — the template itself has none, but this is now a customer-facing
   document.

## Backend

- `backend/xirr_utils.py` — canonical WM↔XIRR conversions (both directions).
- `backend/proposal_engine.py` — pure blending math, no fund-analysis logic
  duplicated. Reuses whatever `_build_analysis()` (SIP) / lumpsum equivalent
  already computed per fund; only blends by allocation %.
  - `extract_fund_horizon_metrics()` — pulls {fqs, success_pct, median_wm,
    p10_wm} per horizon out of an existing SIP or Lumpsum analysis dict (same
    field names in both → one extractor for both modes).
  - `build_fund_row()` — per-fund per-horizon metrics + WM→XIRR.
  - `build_mode_projection()` — allocation-weighted Expected Wealth
    (median & P10), Capital Invested, blended WM, blended XIRR — generic
    over N funds and any 3 horizons. Verified cell-for-cell against the
    template's own U4:Z10 formulas using the exact Priya Ravi funds/allocations.
  - `build_swp_projection()` — month-by-month amortisation, verified against
    the template's SWP!B8:F127.
  - `default_swp_inputs()` — corpus/return auto-derive rule above.
- `backend/proposal_routes.py` — new router, mounted at `/api/proposal`:
  - `POST /api/proposal/build` — validates funds/allocations/horizons,
    fetches each fund's SIP + Lumpsum analysis (via existing cached/build
    helpers — no duplicate AMFI fetch or scoring logic), computes SIP +
    Lumpsum + SWP projections, persists to `portfolio_proposals` (Mongo),
    returns the computed JSON + `proposal_id` for in-app preview.
  - `GET /api/proposal/{id}` — refetch a saved proposal.
  - `GET /api/proposal/{id}/export.xlsx` — regenerate the workbook from the
    saved doc (deterministic, no recompute) via `xlsx_export.py`.
  - `GET /api/proposal/{id}/export.pdf` — same via `pdf_export.py`.
- `server.py` gets one small safe refactor: the inline body of
  `analyze_lumpsum()` is extracted into `_build_lumpsum_analysis()` so the
  proposal builder can call the *exact* same function instead of
  re-implementing NAV fetch / wound-up / merger-stitch logic a second time.
- Existing single-fund endpoints are untouched.

## Frontend

- `frontend/src/api.ts` — add proposal types + `api.buildProposal()`,
  `api.proposalExportUrl(id, format)`.
- New stack routes (root `Stack`, not the tab bar — same pattern as
  `fund/[code]` and `mode-picker`, so the existing Home/Funds/Methodology tabs
  and single-fund flow are untouched):
  - `app/proposal/build.tsx` — fund multi-select (reuses the existing search
    from `(tabs)/index.tsx`), allocation % per fund (must sum to 100%),
    SIP monthly amount, Lumpsum total amount, 3-horizon picker, SWP inputs
    (corpus/return pre-filled from the draft portfolio, editable; inflation +
    withdrawal required).
  - `app/proposal/[id].tsx` — preview (numbers only) + "Download PDF" /
    "Download Excel" buttons using `expo-file-system` + `expo-sharing`
    (both added to `package.json`, not previously installed).
- Entry point: a "Build Proposal" action from the Funds tab — no changes to
  the tab bar or the single-fund analysis screen.

## Known simplifications (flagged, not silently assumed)

- PDF renders the SWP table as yearly checkpoints (every 12th month) instead
  of all 120 monthly rows, to stay a shareable page count; the Excel export
  has the full monthly schedule, matching the template exactly.
- If a fund lacks history at the chosen horizon, it is NOT dropped. It falls
  back to its longest statistically robust window (>=5Y, >=10 rolling windows —
  the same rule that qualified it as an Additional Recommendation, shared via
  `backend/additional_rec.py`). All its displayed parameters (FQS, Success %,
  Median WM, P10 WM, both XIRRs) are read at that shorter window and ONLY
  there; the row carries an asterisk in the PDF, Excel and app, plus a
  footnote naming the window. For the portfolio's Median and P10 wealth, that
  window's XIRR is held constant and compounded out to the full user horizon
  (both SIP and Lumpsum) — see `proposal_engine.py`'s `proj_wm_median` /
  `proj_wm_p10`. Affected funds are listed in `aggregate.extrapolated_funds`
  and in the `warnings` list returned by `/proposal/build`.
- Only a fund with NO usable rolling window at all is excluded from the blend
  (`available: false`, listed in `aggregate.excluded_funds`) — it does not
  silently understate the portfolio number without surfacing why.
