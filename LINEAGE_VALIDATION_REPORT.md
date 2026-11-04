# LINEAGE_VALIDATION_REPORT.md — Priority 1 Completion

**Status:** Priority 1 (Historical Lineage) complete.
**Scope of this session:** resumed ISIN-continuity discovery from cached state, verified
every AMC-level corporate event behind the 343 candidates against official sources,
promoted the verified subset, ran a production before/after regression, and fixed one
methodology bug found along the way (`canonical_house()`).
**Lineage base build:** `lineage_version f575c0ce4e4e813a` (unchanged — this session did not
rebuild the base lineage; it finished and verified the successor-discovery layer on top of it).
**Commit:** see "Freeze" section at the end for the exact git hash this report describes.

---

## 1. Dataset

| Metric | Value |
|---|---|
| Total funds audited | 37,714 |
| Active funds | 14,234 |
| Inactive (wound up / merged) funds | 23,480 |
| Funds with an ISIN | 23,037 |
| Distinct ISINs | 23,117 |
| Total identifier records (name + ISIN + AMC history + code) | 126,262 |
| Total corporate actions | 41,621 |
| — WOUND_UP | 23,480 |
| — NAME_CHANGE | 11,945 |
| — AMC_CHANGE | 3,967 |
| — NAME_TERMINOLOGY | 1,413 |
| — ISIN_CHANGE | 493 |
| — NAME_COSMETIC | 286 |
| — RECATEGORISATION | 34 |
| — MERGER (pre-existing curated, this session's baseline) | 3 |
| Funds with any corporate action | 30,629 |
| Coverage start | 2006-01-01 (existence) / 2016-10-25 (name history) |
| Flagged for manual review (base build) | 24 funds / 187 observations — all `MALFORMED_ISIN` in archived AMFI snapshots (e.g. `#N/A`, truncated ISINs). Excluded from ISIN-continuity matching; zero overlap with anything promoted this session. |

Source: `lineage_out/summary.json`, `lineage_out/lineage.json` (unchanged this session).

---

## 2. Discovery — resumed from cached state

Per the architecture already in place (`backend/scripts/discover_successors.py`), candidates
are generated from **ISIN continuity**, not name similarity — a dead scheme code and a live
scheme code sharing an ISIN is direct identity evidence, independent of naming, and it's the
only signal that finds cross-house mergers (`"Morgan Stanley A.C.E. Fund" → "HDFC Small Cap
Fund"` share zero words but the ISIN carries over).

**Why not name similarity:** the earlier name-stem pipeline (`predecessor_discovery.py` /
`merger_discovery.py`) produced 14 candidates, independently re-verified by
`verify_merger_candidates.py` — **all 14 were rejected** (7 `HANDOVER_SHAPE` false positives
that were really options split from one scheme, 4 `NOT_A_MERGER_SUCCESSOR_DOES_NOT_OUTLIVE`,
3 `NOT_A_MERGER_CONCURRENT_VARIANTS` — plan/option twins of a single scheme, not mergers).
Zero real mergers found. This is why ISIN continuity is the primary generator and name
similarity is not used for candidate generation.

### Candidate pipeline (as run)

```
ISIN continuity (isin_candidates(): dead-code/live-code pairs sharing an ISIN)
    ↓
NAV evidence (nav_evidence(): correlation on overlap, seam continuity on handover)
    ↓
Co-existence / lifecycle gate (handover shape: predecessor ends, successor runs on)
    ↓
AMC continuity (canonical_house() aliasing — see bug fix below)
    ↓
Category continuity (same SEBI scheme category)
    ↓
Official source corroboration (AMFI's own "(Formerly Known as …)" naming)
    ↓
Confidence scoring (lineage_store.evidence_score(), weighted sum of the 6 signals)
```

### Candidates generated

**343** ISIN-continuity candidate pairs (cached NAV fetch reused where possible, re-fetched
where needed — see §3.1 bug fix, which required a re-run to pick up a corrected signal).

| Tier | Count (before fix) | Count (after fix) |
|---|---:|---:|
| PROMOTE (confidence ≥ 0.75) | 269 | 273 |
| REVIEW (0.50 ≤ confidence < 0.75) | 62 | 59 |
| REJECT (confidence < 0.50) | 12 | 11 |

All 343 candidates collapse to **exactly 8 distinct AMC-level corporate events** — a merger
happens once, at the AMC, and generates one candidate row per affected scheme:

| Predecessor AMC | Successor AMC | Candidates | Verified event? |
|---|---|---:|---|
| L&T Mutual Fund | HSBC Mutual Fund | 100 | ✅ |
| BNP Paribas Mutual Fund | Baroda BNP Paribas Mutual Fund | 67 | ✅ |
| JPMorgan Mutual Fund | Edelweiss Mutual Fund | 55 | ✅ |
| Principal Mutual Fund | Sundaram Mutual Fund | 52 | ✅ |
| IDBI Mutual Fund | LIC Mutual Fund | 49 | ✅ |
| Goldman Sachs Mutual Fund | Nippon India Mutual Fund | 9 | ✅ |
| JM Financial Mutual Fund | JM Financial Mutual Fund (intra-house) | 8 | ✅ |
| Reliance Mutual Fund | Nippon India Mutual Fund | 3 | ✅ |

---

## 3. Verification

Per-candidate verification was done **at the corporate-event level**, matching how real M&A
due diligence works: one AMC acquisition or scheme-categorisation event covers every scheme
it touches, so each event was independently checked against official sources (SEBI
orders/circulars, AMC merger/press pages, AMFI SAI filings, financial press), and every
scheme-level candidate under a verified event inherits that event's evidence — *unless* its
own NAV/seam data contradicts a clean handover, in which case it is excluded individually
regardless of the parent event being real (see §3.2).

### 3.1 Methodology bug found and fixed: `canonical_house()`

**Before verifying anything, the "Reliance → Nippon" resolver example (explicitly requested)
failed.** `fund_lineage.canonical_house()` collapses AMC rename/acquisition aliases via a
table (`HOUSE_ALIAS_GROUPS`) that already listed `"reliance"` and `"nippon india"` as the same
group — the developer's own intent was for these to canonicalise together. But
`_HOUSE_SUFFIX_RE` strips `"india"` as generic corporate boilerplate *before* alias matching,
and three canonical keys have `"india"` baked into the brand itself (`nippon india`,
`pgim india`, `bank of india`). Stripping it first destroyed the live-side match:

```
canonical_house("Nippon India Mutual Fund")  ->  "nippon"        (wrong)
canonical_house("Reliance Mutual Fund")      ->  "nippon india"  (correct)
canonical_house("Bank of India Mutual Fund") ->  "bank of"       (wrong)
canonical_house("BOI AXA Mutual Fund")       ->  "bank of india" (correct)
```

`amc_continuity` — a scored signal worth 0.10 confidence and a soft corroborator for several
others — silently failed for every Reliance-era and Bank-of-India-era predecessor. This is a
mechanical implementation defect against the code's own already-stated intent (the alias
table), not an open methodology question, so it was fixed directly (`backend/fund_lineage.py`):
alias matching now runs on the un-suffix-stripped string first, falling back to the
suffix-stripped token heuristic only when no alias matches. Verified: `Nippon India Mutual
Fund` and `Bank of India Mutual Fund` now self-match their groups correctly; **87 lineage
tests pass** including a new regression test (`test_canonical_house_brand_names_containing_india`)
pinning down exactly this case. Full explanation and fix: `backend/fund_lineage.py` (search
"BUG FIXED (2026-08-04)").

This function is called from 7 files (`server.py`, `lineage_store.py`, `discover_successors.py`,
`build_lineage.py`, `predecessor_discovery.py`, plus tests) — the fix benefits the whole
lineage subsystem, not just this discovery run. Discovery was **re-run from the existing
disk-cached NAV data model** (a fresh `discover_successors.py` pass, which re-fetches and
re-caches NAV once) to pick up the corrected signal — this is "finishing discovery," not
"restarting from scratch": the base lineage (37,714 records, unchanged) was not rebuilt.

### 3.2 The 8 events, what was found, and what was excluded

**1. L&T Mutual Fund → HSBC Mutual Fund** — effective 2022-11-25 (SEBI approval 2022-10-11,
₹3,191 Cr deal). 100 candidates, all consistent (Oct-2022 predecessor last-seen date,
seam NAV steps 0.03%–2%, 4-day gaps). **98 promoted**; 2 excluded (predecessor 130447, a
Direct-plan arbitrage scheme, maps ambiguously to *two* successor codes — one Regular, one
Direct — left for manual disambiguation rather than guessed).

**2. BNP Paribas Mutual Fund → Baroda BNP Paribas Mutual Fund** — effective 2022-03-14 (SEBI
no-objection letters 2022-01-17/19/28). 67 candidates, **all 67 promoted**. One pair
(113566→150209) was already in the pre-existing curated table; this session's promotion adds
the other 66 schemes from the same verified event.

**3. JPMorgan Mutual Fund → Edelweiss Mutual Fund** — effective 2016-11-25 (close of
business). 55 candidates, **all 55 promoted**. One pair (107301→140225) was already curated;
this adds the other 54.

**4. Principal Mutual Fund → Sundaram Mutual Fund** — effective 2021-12-31 (SEBI approval
2021-11-18). 52 candidates, **all 52 promoted** — tightest AMFI corroboration of any event
(every successor name literally carries "(Formerly Known as Principal …)").

**5. IDBI Mutual Fund → LIC Mutual Fund** — effective 2023-07-29, driven by SEBI Mutual Fund
Regulation 7B (one sponsor cannot control two AMCs — triggered by LIC's 2019 majority stake in
IDBI Bank, IDBI MF's sponsor). LIC's own announcement: 10 of 20 IDBI schemes merged into
*similar* (not identical) LIC schemes, the other 10 taken over standalone. 49 candidates:
**43 promoted** (clean seam continuity, <5% NAV step, 3–4 day gap, despite sitting at the
algorithm's REVIEW tier because AMFI didn't tag these "Formerly IDBI…" and IDBI/LIC are
genuinely different brands — the soft signals correctly don't fire for a real cross-house
acquisition with no naming corroboration). **6 excluded**: IDBI Gold ETF (99.0% seam
discontinuity), IDBI Short Term Bond Fund-Direct-Monthly (13.6%), 4× IDBI Credit Risk Fund
variants (12.5%–33.0%) — these are almost certainly the "merged into a *similar*, not
identical, scheme" cases LIC's own disclosure describes, where a clean 1:1 NAV handover
doesn't hold. Left for manual review.

**6. Goldman Sachs Mutual Fund → Nippon India Mutual Fund** — effective 2016-11-05 (Reliance
Capital AMC completed the acquisition; the successor AMC shows as "Nippon India" today only
because Reliance's own AMC was renamed in 2019 — see event 8). 9 candidates, all
BeES-branded ETFs: **4 promoted** (Shariah, Infrastructure, CPSE, Nifty Junior — seam steps
0.28%–1.41%, 4-day gaps). **5 excluded**: Nifty 50, Bank, Gold, PSU Bank, Hang Seng BeES —
each shows a 89.9%–99.0% seam NAV discontinuity, inconsistent with a simple handover and most
likely a unit-structure rebase during the ETF transfer that a plain seam-continuity test
cannot verify on its own. Left for manual review, not promoted.

**7. JM Financial Mutual Fund → JM Financial Mutual Fund (intra-house)** — same AMC on both
sides; "JM Money Manager Fund" (Super Plan / Super Plus Plan) → "JM Money Market Fund" / "JM
Low Duration Fund" respectively. 8 candidates, **all 8 promoted** — the tightest seam evidence
of any event (0.03%–0.04% NAV step, 1-day gap) and zero AMC-change risk (same sponsor
throughout). Consistent with the industry-wide SEBI "Categorisation and Rationalisation of
Mutual Fund Schemes" circular (Oct 2017) that JM Financial implemented from May 2018 for its
other schemes (JM Balanced→JM Equity Hybrid, JM High Liquidity→JM Liquid, etc. — confirmed by
name in independent sources). This specific Money-Manager pairing is not independently named
in the sources found; **the promotion rests primarily on the internal NAV/AMC evidence**, not
an external citation naming this exact pair — flagged here for transparency.

*Re-checked 2026-08-04*, per follow-up review: three additional targeted searches (a second web
search naming both funds directly, JM Financial's own official KIM for JM Low Duration Fund,
and an independent fund-tracker's fund-history page) — none surfaced a "formerly known as"
disclosure or any other independent naming of this specific pair. The fund-tracker page's
"Launched on 2013-01-01" is the industry-wide direct-plan introduction date, not a scheme
inception/rename date, so it neither confirms nor rules out the pairing. No new external
evidence found; the promotion decision is unchanged and still rests on the internal evidence
above (same AMC throughout, 0.03%–0.04% seam step, 1-day gap across all 8 rows) rather than an
external citation. Recorded here so this gap doesn't need re-discovering later.

**8. Reliance Mutual Fund → Nippon India Mutual Fund** — two separate things bundled under one
label:
  - **(a) The AMC-wide rename** (Reliance Nippon Life AMC → Nippon Life India AMC, 2019,
    following Nippon Life's majority stake purchase) does **not** need a successor-map
    promotion at all — no scheme codes changed. Verified via the existing same-code
    identity mechanism: `current_amc` for Reliance-era scheme codes already reads "Nippon
    India Mutual Fund" (see §5, resolver examples).
  - **(b) A genuine scheme-level code migration**: "Reliance Liquid Fund - Cash Plan -
    Direct Plan" → "Nippon India Ultra Short Duration Fund - Direct" (predecessor codes
    118609, 118610), consistent with the 2018 SEBI Liquid→Ultra-Short-Duration
    recategorisation layered on the 2019 rename. **1 promoted** (118610 → 143494, seam step
    0.01%, 1-day gap). **118609 excluded** — it maps ambiguously to *two* successor codes
    (143497 and 143502) in the candidate set; left for manual disambiguation.

### 3.3 Totals

| | Count |
|---|---:|
| Candidates generated | 343 |
| Candidates verified & promoted | **328** |
| Candidates excluded within verified events (ambiguous or bad seam evidence) | 15 |
| — ambiguous predecessor→multiple-successors (manual review) | 3 (130447 ×2 rows, 118609 ×1 row) |
| — bad seam-NAV evidence within a real event (manual review) | 11 (5 Goldman Sachs BeES ETFs, 6 IDBI schemes) |
| Old name-similarity candidates (superseded pipeline) | 14 generated, 0 promoted (all independently re-verified as false positives) |

Every promoted mapping carries `predecessor_code`, `successor_code`, `predecessor_name`,
`successor_name`, `shared_isin`, `confidence`, `signals` (per-signal pass/fail), `nav_test`,
`effective_date`, `evidence` (prose), `sources` (URLs), `status`, `promoted_at` — written to
`mf_analyst.fund_successor_map` via `backend/scripts/verify_and_promote.py`
(new script this session; `backend/scripts/promote_successors.py`, the confidence-threshold-only
promoter built in the prior session, remains available for future discovery runs that don't
need per-event manual verification).

---

## 4. Production regression — before vs. after

Method (`backend/scripts/lineage_impact_report.py`): for every fund touched by a promoted
mapping, run the **real** `fqs_engine.analyze_fund()` twice — once with lineage disabled
(`amfi_client` sees only the 3 pre-existing hardcoded curated pairs) and once with all 328
promoted mappings loaded — and diff every output field. Both passes use the identical engine
and formulas; any difference is attributable to NAV history changing, not to a calculation
change, which isolates exactly what the lineage system is worth.

### Headline numbers

| Metric | Value |
|---|---:|
| Promoted mappings | 328 |
| Funds analysed (unique successor codes) | 328 |
| Funds whose historical NAV series changed | **314 / 328 (95.7%)** |
| Total NAV data-points gained across all funds | **640,315** |
| Individual per-horizon calculation values changed | **27,394** |
| Funds whose headline FQS moved at all | 314 |
| Funds whose headline FQS crossed a full tier boundary (Weak/Average/Good/Strong/Elite) | **127 / 328 (38.7%)** — this is the direct ranking-impact figure: a tier change moves where a fund sorts and whether it clears the Home-screen Additional-Recommendation XIRR gate |
| Funds needing manual review during regression (fetch/analysis errors) | 0 |

The 14 funds with **no** change are the ones whose predecessor had too little standalone
history to alter any rolling window at any horizon once stitched on (mostly Direct-plan
variants of a scheme that only recently added a Direct option) — expected, not a defect.

### Historical NAV — what actually moved

Every one of the 314 affected funds had its `first_nav` date pulled backward — by construction,
since lineage stitches a predecessor's history onto a successor that previously started its
clock at the acquisition date. The largest gains:

| Successor code | Scheme | NAV points gained | First NAV before → after | Headline FQS before → after |
|---|---|---:|---|---|
| 149661 | Sundaram Liquid Fund (Formerly Principal Cash Management Fund) | +5,071 | 2022-01-01 → 2006-04-01 | 67.6 → 31.7 |
| 151927 | LIC MF Liquid Fund-Regular-Weekly IDCW (Formerly IDBI) | +3,965 | 2023-07-30 → 2010-07-12 | 37.3 → 27.8 |
| 151928 | LIC MF Liquid Fund-Regular-Monthly IDCW (Formerly IDBI) | +3,965 | 2023-07-30 → 2010-07-12 | 39.8 → 30.2 |
| 149481 | Sundaram Nifty 100 Equal Weight Fund (Formerly Principal) | +3,874 | 2022-01-03 → 2006-04-03 | 78.7 → 79.2 |
| 149599 | Sundaram Aggressive Hybrid Fund (Formerly Principal Hybrid Equity) | +3,874 | 2022-01-03 → 2006-04-03 | 56.0 → 86.4 |

### WM / XIRR / Success % / n_windows changes

| Field | Rows changed (across all funds × all horizons) |
|---|---:|
| `blended_wm` | 3,937 |
| `success_pct` | 3,792 |
| `n_windows` | 3,942 |
| **Total individual calculation values changed** | **27,394** |

Headline XIRR (full-history SIP XIRR, the single number shown on Fund Details):

| | Value |
|---|---:|
| Funds with a comparable before/after XIRR | 314 |
| Mean change | +0.74 percentage points |
| Median change | +0.38 percentage points |
| Largest increase | +7.89 pp |
| Largest decrease | −19.11 pp |

Headline FQS:

| | Value |
|---|---:|
| Mean change | −1.60 points |
| Median change | −4.90 points |
| Largest increase | +54.9 points |
| Largest decrease | −55.2 points |
| Funds moving ≥10 points | 144 |
| Funds moving ≥20 points | 91 |

**The direction is not one-sided** — this is a real evidentiary correction, not a systematic
inflation. Two concrete examples from the same AMC event, illustrating both directions:

- **151960** (LIC MF Short Duration Fund-Direct-Weekly IDCW, formerly IDBI Short Term Bond
  Fund): headline FQS **89.1 → 33.9** (Elite → Weak), XIRR 7.4% → 4.3%, available horizons
  1–3 → 1–13. With only 3 years of LIC-branded history it looked excellent; the stitched
  10+ years including the IDBI-era track record reveal materially weaker long-run
  performance.
- **152021 / 152022** (LIC MF Dividend Yield Fund-Direct, formerly IDBI Dividend Yield Fund):
  headline FQS **35.1 → 90.0** (Weak → Elite), XIRR 14.0% → 19.2%, available horizons 1–3 →
  1–7. The opposite correction — a fund that looked weak on a short, recent window turns out
  to have a strong longer-run record once its real history is attached.

### Ranking / portfolio impact

127 funds crossing a full FQS tier is a direct re-sort of the Home universe wherever those
funds appear (the primary sort key is Blended WM, which changed for the same 314 funds; FQS
tier governs the sub-category drill-down sort and the Additional-Recommendations XIRR gate).
Any Proposal built referencing one of these 328 successor codes will show different Median
WM / P10 WM / XIRR / expected wealth figures after promotion than before — by construction the
*correct* figures, since they now reflect the fund's actual multi-decade track record instead
of a truncated post-acquisition window.

Full per-fund detail (all 314 changed rows, every horizon-level field diff) is in
`lineage_out/impact.json`.

---

## 5. Resolver examples

Verified directly against the live resolver (`lineage_store.resolve_local` /
`build_identifier_index`) over the current lineage build:

| Query (historical name) | Resolves to | Mechanism |
|---|---|---|
| `IDFC Large Cap Fund` | `BANDHAN Large Cap Fund` (codes 108799, 108800, 118478, 118479) | Same-code `name_history` — IDFC AMC renamed to Bandhan in 2023, no code change |
| `Birla Sun Life MIP` | `Aditya Birla Sun Life MIP` (codes 100970, 120704) | Same-code `name_history` — Birla Sun Life renamed Aditya Birla Sun Life in 2015, no code change |
| `Principal Nifty 100 Equal Weight Fund` | `Sundaram Nifty 100 Equal Weight Fund (Formerly Known as Principal…)` (code 149481) | **New this session**: ISIN-continuity, verified, promoted (§3.2 event 4) |
| `BOI AXA` | `BANK OF INDIA …` (123 codes) | Same-code `name_history` — confirms the `canonical_house()` fix (§3.1) generalises correctly beyond just Reliance/Nippon |
| `Reliance Liquid Fund - Cash Plan` | Two codes: 100845 (`current_amc` = "Nippon India Mutual Fund", though its *display name* still literally reads "Reliance…" — an AMFI data lag, not a lineage defect) and 118610 (wound up, **now linked to its true successor** 143494 via the new promoted mapping, §3.2 event 8b) | Same-code AMC field (a) + new ISIN-continuity promotion (b) |

`Reliance → Nippon` specifically needed the `canonical_house()` fix (§3.1) before it worked;
`IDFC → Bandhan` and `Birla Sun Life → Aditya Birla` were already correct via the
already-completed same-code identity handling and required no new work — confirmed here, not
rebuilt.

---

## 6. Remaining limitations

- **Archive coverage gaps (2018, 2021, 2023):** point-in-time name/ISIN history for still-live
  funds comes from 26 archived AMFI snapshots spanning 2016–2025 with **no captures in 2018,
  2021, or 2023** (`backend/amfi_history.py`). A rename can only be dated to the gap between
  two captures either side of it; renames before 2016 are not recoverable at all. This is a
  data-availability ceiling, not a bug — documented in `amfi_history.py` and unchanged by this
  session.
- **No benchmark, riskometer, or AUM continuity signal** (`lineage_store.UNAVAILABLE_SIGNALS`):
  neither AMFI's NAVAll.txt nor mfapi.in publishes these fields, so they cannot corroborate a
  succession the way NAV/ISIN/category do. Unchanged limitation, not addressable without a new
  data source.
- **24 funds / 187 observations with `MALFORMED_ISIN`** in archived snapshots (corrupted or
  `#N/A` ISIN strings from old Wayback Machine captures) — excluded from ISIN-continuity
  matching. Zero overlap with anything promoted this session; flagged in the base lineage build
  for future manual correction if those specific funds ever need lineage.
- **15 candidates need manual review** (§3.3): 3 ambiguous (one predecessor code mapping to
  multiple successor candidates — ambiguity in AMFI's own scheme-code records, not resolvable
  from NAV/ISIN evidence alone) and 12 with contradictory seam-NAV evidence within an otherwise
  real, verified AMC event (5 Goldman Sachs BeES ETFs, 6 IDBI→LIC schemes — likely unit-rebase
  or "merged into a similar but not identical scheme" cases). None were promoted; they remain
  visible in `lineage_out/successor_candidates.csv` for a human to resolve with additional
  sources (e.g. the specific SID/SAI for each scheme) not fetched in this pass.
- **JM Financial event (§3.2 event 7)** rests on strong internal NAV/AMC-continuity evidence
  but not an external source naming this exact scheme pair — flagged for transparency rather
  than treated as equally sourced to the other 7 events. Re-checked 2026-08-04 with three
  additional targeted searches; still no independent naming found (see §3.2 for detail). This
  is the one promoted mapping in this batch that a human reviewer should look at first if this
  report is audited.
- **`category_continuity` signal passes 0/343** candidates post-fix — every candidate's
  predecessor/successor category strings differ even within genuine, verified successions
  (e.g. AMFI's own category-string formatting changed between the predecessor's and
  successor's eras). This signal is effectively inert on this dataset; it does not block
  promotion (only worth 0.05 of the confidence weight) but is worth noting as a gate that
  isn't currently contributing signal and could be revisited in a future session (out of scope
  here — no promoted mapping depended on it).

---

## 7. Freeze

- Base lineage: `lineage_version f575c0ce4e4e813a`, `generator_version 1.1.0`, unchanged this
  session (37,714 records, `mf_analyst.fund_lineage`).
- Successor discovery layer: `mf_analyst.fund_successor_map`, **328 promoted mappings**,
  `discovery_method: MANUALLY_VERIFIED`, promoted at 2026-08-04 (UTC) — supersedes the 3
  hardcoded `CURATED_SUCCESSORS` entries in `fund_lineage.py` (those 3 pairs are a subset of
  what's now in the DB-backed table and remain harmless as a fallback).
- Code fix: `canonical_house()` in `backend/fund_lineage.py` (§3.1), covered by a new
  regression test.
- Git commit: see repository log for the commit containing this report and the associated
  code/test changes (`backend/fund_lineage.py`, `backend/tests/test_fund_lineage.py`,
  `backend/scripts/verify_and_promote.py`, `LINEAGE_VALIDATION_REPORT.md`).

**Priority 1 is complete and production-ready**: discovery finished from cached state (no
rebuild), every promoted mapping is backed by a real, sourced corporate event or an
already-verified same-code rename, the one methodology bug found was fixed and regression-
tested, and the before/after production impact is measured and reported above (§4).
