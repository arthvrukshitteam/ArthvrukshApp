# Portfolio Holding Review Report

Drawer → **Holding Review**. Upload an earlier Portfolio Proposal PDF (or an earlier Holding Review report), verify what was extracted, compare it with current FQS data, edit observations, download the report PDF. Added on branch `fix/span-refresh-bulk-cleanup` (version2).

## 1. Findings about the existing system (what was reused)

| Question | Answer (verified in code) |
|---|---|
| Where is portfolio data stored? | `portfolio_proposals` (one doc per proposal; `_id` = `proposal_id`): client name/phone/PAN, `proposal_date`, `mode`, `horizon`, `since_2010`, `separate_management`, `plan.funds[]` (per-fund fqs, median/p10 WM, XIRRs, cv, `metrics_horizon`). |
| Where is client data stored? | **There is no client entity.** Client fields live inside each proposal. The review links by `client.client_key` = `pan:<PAN>` if well-formed, else `name:<normalised name>`. |
| How is the proposal PDF made? | `proposal_pdf.build_pdf` (reportlab, landscape A4). Fund table columns: Fund, Category, Benchmark XIRR, Allocation, Windows, FQS, Success %, Median WM, XIRR (Med), P10 WM, XIRR (P10), CV. Short-history funds carry `*` + a footnote with their own rolling year. |
| How is FQS calculated? | `fqs_engine.analyze_fund` / `lumpsum_engine`, exposed through `server._build_analysis` / `_build_lumpsum_analysis` (+ caches). Proposals read it via `proposal_engine.build_fund_row`. **The review calls the same functions.** |
| FQS action rules? | `bulk_analysis.get_fund_remark`: <75 Bad/discontinue, 75–<85 Not bad/better options, ≥85 Good. Thresholds were hoisted to constants `FQS_BAD_BELOW/FQS_GOOD_FROM` and are used by the observation engine — no new rules. |
| Fund lookup? | `bulk_analysis.build_master_index` + `match_fund` (code → ISIN → lineage → normalised name → stem). Reused; wrapped with a confidence model. |
| PDF infrastructure / disclaimer? | `proposal_pdf` styles, `_decorate` brand band, `_info_card`, `_grid_style`, `COMPANY_LEGAL_NAME`, `ARN_NUMBER`, canonical `DISCLAIMER_INTRO` + "No Guarantees" line (looked up, not retyped). |
| Auth / file storage? | No auth in the app. Uploaded PDFs are stored in Mongo (`portfolio_review_files`, keyed by SHA-256) — the same approach as bulk-analysis uploads; never served publicly. |
| Not available in the system | **Nifty index level** and **client MF AUM / invested value** (no index feed, no client holdings). They are advisor-entered and labelled so. "Investment type" COB/External cannot be known from a proposal — advisor sets it; funds not in the proposal can be added. |

## 2. Design

```
upload PDF ─► review_pdf_parser ──► draft review (portfolio_holding_reviews)
   (embedded reference → stored proposal/review doc; else text/table parse)
   ─► fund match (high / medium / low-needs-choice) ─► advisor verifies/edits (PUT)
   ─► fetch-current: cached/live analysis → proposal_engine.build_fund_row ─► current metrics
   ─► holding_review_engine: deltas (% vs percentage-points), market summary, validation, observations
   ─► export.pdf (holding_review_pdf)  ─► finalize (immutable) ─► revise (new draft, parent link)
```

* **Parser** (`review_pdf_parser.py`): locates the fund table by header names, tolerates any fund count/pages, blanks stay `None` (never 0), reads rolling years from the footnote, rejects scanned/corrupt/non-proposal PDFs with specific messages.
* **Embedded reference:** `build_pdf` now writes `arthvruksh:proposal:<id>:v1` (and the review PDF `arthvruksh:review:<id>:v1`) into PDF Keywords. If the stored document exists, its exact numbers are used; otherwise (all older PDFs) the text is parsed. A review report as upload ⇒ its *current* figures become the new *previous* baseline (`previous_review_id` chain, quarters +1, market baseline carried).
* **Statuses per fund:** Extracted · Needs review (low/medium match, unreadable cell) · Missing (no figures) · Verified (advisor edited or confirmed).
* **Deltas:** FQS & WM → absolute + % change; XIRR & CV → percentage points only; blank previous ⇒ no change shown.
* **Observations:** from `get_fund_remark` thresholds plus plain statements of measured FQS movement; each carries `basis` (rule + numbers). Regeneration replaces only `source:"auto"`.
* **Validation:** current date ≥ previous date and not in the future; every fund mapped, unique, with rolling year 1–50; unmapped fund → "Unable to map fund: X".
* **Honesty:** current figures use the *latest* NAV data (`data_as_of` shown); a back-dated "current review date" does not re-compute history. The report states that fund figures are model data, not realised client returns.
* **History model:** `portfolio_holding_reviews` (one doc per version; `client.client_key`, `previous_review_id`, `parent_review_id`, `version`, `status` draft|finalized). Finalized ⇒ PUT/fetch/generate return 409 until **Revise**.

## 3. API (`/api/portfolio-review`, router `holding_review_routes.py`)
`POST /upload` · `GET /` · `GET /{id}` · `PUT /{id}` · `POST /{id}/fetch-current` · `POST /{id}/observations/generate` · `POST /{id}/finalize` · `POST /{id}/revise` · `GET /{id}/export.pdf`. Like all other routes it is unauthenticated (see security audit) — PAN is masked in list responses and never logged.

## 4. Files
Backend: `review_pdf_parser.py`, `holding_review_engine.py`, `holding_review_pdf.py`, `holding_review_routes.py` (+ mount at the end of `server.py`; `proposal_pdf.build_pdf` metadata + `doc_label`; `bulk_analysis` threshold constants; `requirements.txt`: pdfplumber, pypdf). Tests: `tests/test_holding_review.py` (17). Frontend: `app/(drawer)/holding-review.tsx`, `src/components/HoldingReviewParts.tsx`, `src/api.ts`, drawer entry in `app/(drawer)/_layout.tsx`.

## 5. Known limits / follow-ups
Nifty/AUM manual (needs an index data source decision) · quarters default = finalized reviews for the client + 1 (editable) · no multi-user/auth · scanned PDFs unsupported (no OCR) · review-report-as-baseline needs the embedded reference (generic review PDFs without it are not parsable) · email/WhatsApp sharing, trend charts and alerts are future work (the persisted history supports them).
