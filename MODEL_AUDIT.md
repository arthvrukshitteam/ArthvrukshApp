# MODEL_AUDIT.md — AV FQS (Monthly SIP) Reverse-Engineering

**Status:** Deliverable A. Awaiting approval before backtest implementation.
**Scope:** Monthly SIP mode. Lumpsum documented only where it clarifies SIP.
**Method:** Extracted from source. Every formula below is verified by a clean-room
re-implementation (`backend/tests/test_model_audit_spec.py`) that reproduces
`fqs_engine.analyze_fund()` exactly on 5 funds × 3 horizons.
**Commit audited:** working tree as of 2026-07-30.

> **Rule applied throughout:** nothing here is inferred from a parameter's name.
> Where the code contradicts the conventional financial meaning of a term, the
> code is documented and the divergence is flagged.

---

## 0. Executive summary — the seven things that matter

| # | Finding | Consequence for the backtest |
|---|---|---|
| 1 | **WM does mean Wealth Multiple**, but it is `end_value / total_invested` for a *rolling SIP window*, not a point-to-point return. Verified `fqs_engine.py:611`. | The forward/actual outcome must be computed the same way. |
| 2 | **The universe is ranked by Blended WM, not FQS** (`server.py:545-557`, `home.tsx:1515`). FQS only sorts when the user drills into one AMFI sub-category (`home.tsx:1528-1531`). | Blended WM is the primary prediction; FQS is secondary. |
| 3 | **There is no "recommended fund" selector anywhere in the codebase.** | `recommended_flag` must be defined as *top-of-ranked-list*, labelled a backtest construct. |
| 4 | **CV is NOT computed against a benchmark or a return series.** It is `pstdev(WMs) / mean(WMs)` across rolling windows (`fqs_engine.py:740-741`). | It measures dispersion of overlapping historical outcomes, not volatility. |
| 5 | **Success % is NOT "percent positive".** It is the fraction of rolling windows whose WM ≥ a *category-specific hardcoded hurdle* (`fqs_engine.py:743-745`). | The hurdle is a fixed table, not a live benchmark. Category → hurdle is a look-ahead vector. |
| 6 | **`total_invested` is hardcoded to `10000 × H × 12`** but the actual instalment count varies (measured: 59, 60, 61 for H=5). `fqs_engine.py:590` vs `:608`. | ±1.7% artefact in every WM. Documented, not fixed. |
| 7 | **Rolling windows are strictly nested** — windows on a truncated NAV series are bitwise-identical to a prefix of windows on the full series (verified). | Point-in-time truncation is exact, and enables a ~90× compute reduction. |

---

## 1. The actual pipeline

```
User enters tenure (years, 1–20)
        │  GET /api/home/universe?years=N          server.py:470-472
        ▼
Eligibility filter: fund_nav_meta.history_years ≥ N + 2   server.py:475,485-486
        │  + drop IDCW/dividend schemes                    server.py:503
        │  (Direct plans already excluded upstream)        amfi_client.py:156
        ▼
Read PRE-COMPUTED metrics from Mongo fund_nav_meta.fqs_by_horizon[str(N)]
        │  — nightly batch, NOT computed per request       server.py:513-535
        ▼
Collapse plan/option duplicates                           server.py:540-543
        ▼
SORT: Debt last, then blended_wm DESC, then name ASC      server.py:548-557
        ▼
Frontend re-sort:
   "All"          → CORE_SUBS + Hybrid, by blended_wm DESC  home.tsx:1518-1522
   broad category → by blended_wm DESC                      home.tsx:1524-1526
   sub-category   → by selected_fqs DESC                    home.tsx:1528-1531
        ▼
Advisor manually picks funds + allocations → POST /api/proposal/build
        ▼
Expected wealth = Σ alloc × amount × median_wm × H × 12    proposal_engine.py:274-279
```

**The metrics themselves** are produced by the nightly job
`fqs_refresh.run_fqs_refresh()` → `fqs_engine.analyze_fund()`, for every fund with
`history_years ≥ 1`, at 22:30 UTC daily (`server.py:1436`).

### 1.1 Backtest adapter point

`analyze_fund()` accepts `start_date` (a *lower* bound) and has **no cutoff / end-date
parameter** (`fqs_engine.py:878-885`). The only adapter the backtest needs is to
truncate `nav_history` to `date <= cutoff` before the call. Nothing else in the
scoring path reads the clock.

---

## 2. Input data

| Property | Value | Location |
|---|---|---|
| Source | `https://api.mfapi.in/mf/{scheme_code}` (AMFI mirror) | `amfi_client.py:50,167-173` |
| Frequency | Daily (business days), as published | — |
| Wire date format | `DD-MM-YYYY`, descending | `amfi_client.py:214-215` |
| Internal row | `{"date": datetime.date, "nav": float}`, ascending | `amfi_client.py:225-228` |
| NAV ≤ 0 | Row dropped | `amfi_client.py:223-224` |
| Unparseable row | Silently dropped (`except Exception: continue`) | `amfi_client.py:226-227` |
| Duplicate dates | **Not** de-duplicated within a scheme | `amfi_client.py:225-228` |
| Gaps | **No detection, no fill, no interpolation, no calendar alignment** | — |
| NAV caching | None per-fund; every analysis is a live HTTP fetch | — |

### 2.1 Split / rebase sanitisation — a look-ahead leak

`sanitize_nav_history()` (`amfi_client.py:187-211`):

```
for each consecutive pair (prev, cur) with (cur.date - prev.date).days <= 31:
    ratio = cur.nav / prev.nav
    if ratio >= 4.0 or ratio <= 0.25:
        for j in range(i):  history[j].nav *= ratio      # rescales ALL earlier points
```

Constants: `SPLICE_RATIO_THRESHOLD = 4.0`, `SPLICE_MAX_GAP_DAYS = 31`
(`amfi_client.py:183-184`).

- Effect: the jump-day return becomes exactly 0; rescaling is cumulative across
  multiple detected splices.
- **Look-ahead:** a split detected in year *T+3* retroactively rewrites the NAVs of
  year *T*. **The backtest must call this on the truncated series at each cutoff**,
  not once on the full series.
- Jumps across gaps > 31 days are left untouched.

### 2.2 Month-end extraction

`extract_month_end_navs()` (`fqs_engine.py:519-536`) — for each calendar month, keep
the row whose next row falls in a different `(year, month)`; i.e. **the last NAV
present in that calendar month**, whatever date that is.

Consequence: the final month of any series is a *partial* month whose "month-end"
is simply the last available NAV. **Backtest cutoffs must therefore land on true
calendar month-ends**, or the truncated series' last point differs from the full
series' corresponding point and the nesting property (§7.2) breaks.

---

## 3. Fund lineage — predecessor / successor

`load_analysis_nav()` (`amfi_client.py:313-465`). Depth-limited to
`MAX_STITCH_DEPTH = 4` per direction (`amfi_client.py:310`).

**Forward pass** (extend a wound-up fund into its successor) — runs *only* if the
current segment is itself wound up, where wound up means
`last_nav_date < today_utc − 180 days` (`WOUND_UP_THRESHOLD_DAYS = 180`,
`amfi_client.py:51,245-257`).

**Backward pass** (prepend an older code's history) — runs **unconditionally**,
regardless of wound-up status. Only one verified predecessor per level; fan-in
merges are not supported (`amfi_client.py:441`).

**Stitch operation** (`fund_lineage.py:217-227`) — a date-keyed dict merge,
successor wins on shared dates. **No level rescaling at the join.** Two schemes with
dissimilar NAV levels inject a spurious one-step return directly into the series.

**Verification gate** `verify_stitch()` (`fund_lineage.py:230-277`):

| Constant | Value |
|---|---|
| `MIN_OVERLAP_DAYS` | 15 |
| `CORR_THRESHOLD_DISCOVERED` | 0.90 |
| `CORR_THRESHOLD_CURATED` | 0.80 |
| `MAX_GAP_DAYS_NO_OVERLAP` | 120 (curated only) |

Correlation = Pearson *r* of daily simple returns over the date intersection.
Discovered pairs with no ≥15-day overlap are always rejected.

**Curated table** `CURATED_SUCCESSORS` (`fund_lineage.py:71-94`) — exactly 3 entries:

```
113566 → 150209   BNP Paribas Mid Cap        → Baroda BNP Paribas Mid Cap
107301 → 140225   JPMorgan India Mid & Small → Edelweiss Mid Cap
108197 → 130502   Morgan Stanley A.C.E.      → HDFC Small Cap
```

`HISTORY_NOTES` is **empty** (`fund_lineage.py:112`) — so `history_note` is always
`None` today.

Automated discovery (`predecessor_discovery.run_discovery`) exists with 3 confidence
tiers, **but has no caller anywhere in the repo**. The runtime cache is fed instead
from `predecessor_map` (`status == "auto"`) plus `merger_discovery_log` entries at
tier HIGH / VERY HIGH (`server.py:72-106`).

### 3.1 Backtest implication

Prepending a predecessor is legitimate at any cutoff — that history genuinely
existed. **Appending a successor is pure look-ahead**: at cutoff 2019-06 you cannot
know a fund merges in 2021. The backtest must run **backward-only stitching**.

---

## 4. Eligibility

| Rule | Value | Location |
|---|---|---|
| Rolling tenure accepted | 1 ≤ years ≤ 20 | `server.py:135-136,472` |
| **History requirement** | `history_years ≥ years + HISTORY_BUFFER_YEARS`, buffer = **2** | `server.py:137,475,485-486` |
| `history_years` definition | `(latest_nav_date − earliest_nav_date).days / 365.25`, 2 dp, on the **stitched** series | `nav_meta_indexer.py:139-140,156` |
| Direct plans | Excluded — `"direct" not in schemeName.lower()` | `amfi_client.py:156` |
| IDCW / dividend variants | Excluded — regex below | `amfi_client.py:37-48`, applied `server.py:503` |
| Wound-up / closed funds | **Not excluded** | — |
| Plan/option duplicates | Collapsed to one representative | `server.py:411-465` |
| Frontend "All" tab | Further restricted to `CORE_SUBS ∪ Hybrid` | `home.tsx:166-174,1521` |

IDCW rule: strip every `dividend[\s-]*yield` occurrence, then exclude if any of
`idcw | income distribution | dividend | payout | reinvestment | re-investment`
survives.

`CORE_SUBS` = {Multi Cap, Large Cap, Large & Mid Cap, Mid Cap, Small Cap, ELSS,
Flexi Cap} Fund.

### 4.1 Why "rolling period + 2" — the actual reason

Nothing in the code justifies the buffer; it is a bare constant. But its
*mechanical* effect is derivable: a fund with exactly `H + 2` years of history
yields approximately **`2 × 12 = 24` rolling windows** at horizon `H` (§5.1). At
that boundary:

- **P10** is the ~2.4th order statistic of 24 points.
- **ES10** averages the 2 worst windows.
- **Blended WM puts 40% weight on P10.**

So the buffer is the minimum that makes the tail statistics computable at all — not
that makes them reliable. Expect materially noisier rankings for just-eligible
funds; the backtest should stratify by `n_windows` to quantify this.

### 4.2 `history_years` is unusable for the backtest

`nav_meta_indexer` skips codes already present in `fund_nav_meta`
(`nav_meta_indexer.py:98-99`), so **existing documents are never refreshed** —
`history_years` and `latest_nav_date` go stale until the collection is cleared. The
backtest must recompute available history from the truncated NAV series at each
cutoff.

---

## 5. Rolling SIP windows — the core construction

`compute_rolling_windows()` (`fqs_engine.py:577-634`).

**Constants:** `SIP_AMOUNT = 10_000` ₹/month (`fqs_engine.py:513`);
horizons `1..20` (`HORIZONS_FULL`, `fqs_engine.py:514`).

**Guard:** requires `len(monthly) ≥ H×12 + 1`, else returns `[]`
(`fqs_engine.py:587-588`).

### 5.1 Window generation

Let `M` = the month-end NAV series, `H` = horizon years, `N = 12H`.

```
A₁ = EOMONTH(M[0].date, 0) + 1 day
Aₖ = EOMONTH(Aₖ₋₁, 1)                    for k ≥ 2
Bₖ = EDATE(Aₖ, 12H)

terminate when EOMONTH(Bₖ, 0) > EOMONTH(M[-1].date, 0)
```

`EOMONTH` / `EDATE` are Excel-faithful (`fqs_engine.py:493-506`).

For window *k*:

```
SIP dates    = { d ∈ M.dates : Aₖ ≤ d < Bₖ }          # half-open interval
end index    = last i with M[i].date ≤ Bₖ             # Excel XLOOKUP mode -1
units        = Σ over SIP dates of  10000 / NAV(d)
end_value    = units × NAV(end index)
total_invested = 10000 × N                            # ← HARDCODED
WM           = end_value / total_invested
good         = WM ≥ min_wm_required                   # non-strict ≥
```

### 5.2 ⚠️ Defect: instalment count vs. hardcoded denominator

`invested = SIP_AMOUNT * N` (`fqs_engine.py:590`) is fixed at `N = 12H`, but `units`
sums over `sip_idx`, whose length is whatever the half-open date interval captures
(`fqs_engine.py:600,608`).

**Measured on HDFC Small Cap (130502), H = 5:** instalment counts of **59, 60 and
61** occur across windows against a constant denominator of 60.

Effect: WM is distorted by roughly **±1.7%** from calendar alignment alone. This
propagates into Median WM, P10 WM, CV, Success %, Blended WM and FQS.

Per the no-optimisation rule this is **documented, not fixed**. It is recorded in
the data-quality report and the backtest reproduces it exactly.

### 5.3 WM sanity ceiling

A window is **skipped entirely** if `WM > max_plausible_wm(category, H)`
(`fqs_engine.py:619-621`, `category_map.py:355-377`):

```
Debt:   (1 + 0.20) ^ H                      # MAX_XIRR_FOR_DEBT = 0.20
other:  max(50.0, (1 + 0.22) ^ H)           # GENERIC_WM_SANITY_CAP, MAX_XIRR_FOR_GENERIC = 0.22
```

Intent is to drop NAV-corruption artefacts. Note it removes the window from the
distribution rather than flagging it, so `n_windows` silently shrinks — and if
every window at a horizon exceeds the cap, that horizon returns zero windows
entirely.

**Correction (2026-08-04).** The generic branch was a flat `50.0` regardless
of horizon until this date. That was safe while horizons topped out at 20Y
(even 20% p.a. sustained for 20Y is only ~38x), but once 50-year horizons
shipped (§ nothing in this document originally accounted for this — see
Appendix A), ordinary performance legitimately exceeds 50x on compounding
alone (10% p.a. for 50Y ≈ 117x), and every window at long horizons was being
silently discarded as if it were data corruption — zeroing out the entire
horizon for perfectly healthy funds. Fixed by scaling the generic branch the
same way Debt's already was, anchored on 22% p.a. (above every
`CATEGORY_TABLE` hurdle). `max(50.0, ...)` makes this a no-op through ~19
years (`1.22^20 ≈ 50.4` is the first year the scaled term overtakes 50), so
the previously-audited 1–20Y range is byte-for-byte unchanged.

---

## 6. The five parameters — exact definitions

All computed in `score_horizon()` (`fqs_engine.py:717-853`) over
`wms = [w._wm_raw for w in windows]`, `n = len(wms)`.

`_wm_raw` is the **full-precision** WM. The `wealth_multiple` field is
`round(wm, 4)` and is display-only — the statistics do **not** use it
(`fqs_engine.py:574,629,631`).

### 6.1 WM (Wealth Multiple)

| Field | Value |
|---|---|
| **Definition from code** | Terminal value of a rolling monthly-SIP window ÷ nominal amount invested |
| **Formula** | `WM = (Σ_{d∈SIP dates} 10000/NAV(d)) × NAV(end) / (10000 × 12H)` |
| **Inputs** | Month-end NAVs only |
| **Data frequency** | Monthly (derived from daily) |
| **Period** | One rolling window of exactly `H` years |
| **Normalisation** | None at this stage |
| **Edge cases** | Windows with WM > sanity cap are dropped (§5.3); denominator defect (§5.2) |
| **Location** | `fqs_engine.py:608-611` |

The name matches the conventional meaning. **But it is a SIP wealth multiple, not a
point-to-point NAV ratio** — the lumpsum engine's WM (`ending_nav / starting_nav`,
`lumpsum_engine.py:273`) is a different quantity with the same name. Do not
interchange them.

### 6.2 Median WM

| Field | Value |
|---|---|
| **Definition** | 50th percentile of the WM distribution across **all rolling windows at that horizon** |
| **Formula** | `PERCENTILE.INC(wms, 0.50)` |
| **Percentile method** | `k = (n−1)·p`; linear interpolation `s[f]·(c−k) + s[c]·(k−f)` |
| **Population** | Overlapping windows — **not** independent observations |
| **Location** | `fqs_engine.py:735`, `_percentile` at `:639-650` |

### 6.3 P10 WM

| Field | Value |
|---|---|
| **Definition** | 10th percentile of the same WM distribution |
| **Formula** | `PERCENTILE.INC(wms, 0.10)` — identical machinery, `p = 0.10` |
| **Interpretation in code** | Downside case: "10% of historical windows did worse" |
| **Reliability caveat** | At the eligibility boundary (~24 windows) this is the ~2.4th order statistic (§4.1) |
| **Location** | `fqs_engine.py:736` |

### 6.4 CV

| Field | Value |
|---|---|
| **Definition from code** | Coefficient of variation **of the WM distribution** |
| **Formula** | `CV = pstdev(wms) / mean(wms)` — population σ (`statistics.pstdev`, Excel `STDEV.P`), not sample |
| **What it is NOT** | Not return volatility, not tracking error, not benchmark-relative. It is dispersion of *overlapping historical SIP outcomes*. |
| **Guards** | `std = 0.0` when `n < 2`; `_safe_div` returns `0.0` if `mean == 0` or NaN |
| **Location** | `fqs_engine.py:740-741`, `_safe_div` at `:653-656` |

### 6.5 Success %

| Field | Value |
|---|---|
| **Definition from code** | Fraction of rolling windows meeting a **fixed category hurdle** |
| **Formula** | `failures = #{w : w < min_required}`; `success_pct = 1 − failures/n` |
| **Comparison** | **Non-strict** — a window exactly at the hurdle counts as success |
| **What it is NOT** | Not "percent of positive periods". A window with WM = 1.4 (i.e. +40%) is a *failure* for Small Cap at 5Y (hurdle 1.56). |
| **Location** | `fqs_engine.py:743-745` |

Contrast with **Consistency Score**, which uses the same hurdle but **strict `>`**
(`fqs_engine.py:762`). Both exist and both feed FQS with different weights.

### 6.6 `min_wm_required` — the benchmark

This is the *only* benchmark in the model. It is a **hardcoded table**, not a live
index. `CATEGORY_TABLE` (`category_map.py:173-193`):

| Category | XIRR | 3Y | 5Y | 10Y | 15Y | cap | type |
|---|---|---|---|---|---|---|---|
| Debt | 0.07 | 1.1083 | 1.1933 | 1.4425 | 1.7611 | 110 | non_equity |
| Gold/Silver | 0.08 | 1.1250 | 1.2250 | 1.5242 | 1.9222 | 120 | non_equity |
| Index Fund | 0.10 | 1.1600 | 1.2900 | 1.7100 | 2.3000 | 120 | non_equity |
| Hybrid | 0.11 | 1.1780 | 1.3250 | 1.8080 | 2.5260 | 120 | non_equity |
| Large Cap | 0.12 | 1.1972 | 1.3617 | 1.9167 | 2.7756 | 130 | equity |
| Flexi Cap | 0.13 | 1.2139 | 1.3983 | 2.0333 | 3.0539 | 140 | equity |
| ELSS | 0.13 | 1.2139 | 1.3983 | 2.0333 | 3.0539 | 140 | equity |
| Multi Cap | 0.14 | 1.2333 | 1.4367 | 2.1592 | 3.3661 | 140 | equity |
| Mid Cap | 0.15 | 1.2528 | 1.4767 | 2.2933 | 3.7139 | 145 | equity |
| Small Cap | 0.17 | 1.2917 | 1.5600 | 2.5933 | 4.5411 | 150 | equity |
| **Misc Fund** (default) | 0.13 | 1.2139 | 1.3983 | 2.0333 | 3.0539 | 140 | equity |

**Anchors exist only at 3/5/10/15Y.** For any other horizon,
`_extrapolate_min_wm()` (`fqs_engine.py:856-865`) uses the closed-form SIP future
value:

```
m = category_xirr / 12
N = 12H
min_wm(H) = ((1+m)^N − 1) / (N·m)
```

Note `Index Fund` and `Hybrid` are typed **non_equity** for SIP — they use the
non-equity FQS weight vector (§6.7). `Misc Fund` is the fallback and is typed
equity.

### 6.7 FQS

Six sub-scores, then a weighted sum. All at `fqs_engine.py:747-786`.

```
fail_score        = failure_pct × 100
downside_severity = MIN(100, 100 × min_required / P25)      if P25 > 0 else 100
cv_score          = MIN(100, CV × 100)
risk_score        = 0.40·fail_score + 0.25·downside_severity + 0.35·cv_score
adj_risk          = MAX(0, 100 − risk_score)

blended_wm        = 0.6 × Median WM + 0.4 × P10 WM
blended_wm_score  = MIN(cap, (blended_wm / min_required) × 100)
downside_score    = MIN(cap, (P10 / min_required) × 100)
consistency_score = #{w : w > min_required} / n × 100         ← STRICT >
es10              = mean{w : w < P10}, else min(wms) if empty ← STRICT <
es_score          = MIN(100, 100 × es10 / min_required)
```

**Weights** (`fqs_engine.py:773-776`):

| Component | Equity | Non-equity |
|---|---|---|
| Success % | **0.30** | 0.25 |
| Adjusted Risk | 0.20 | **0.35** |
| Blended WM Score | 0.20 | 0.15 |
| Consistency | 0.15 | 0.10 |
| Downside Score | 0.10 | 0.10 |
| ES Score | 0.05 | 0.05 |
| *sum* | 1.00 | 1.00 |

```
FQS = w_succ · (success_pct × 100)
    + w_arisk · adj_risk
    + w_bwm   · (blended_wm_score × 100 / cap)      ← cap-normalised to 0–100
    + w_dn    · (downside_score   × 100 / cap)      ← cap-normalised to 0–100
    + w_cons  · consistency_score
    + w_es    · es_score

FQS = clamp(FQS, 0, 100), rounded to 1 dp
```

The `cap` division re-normalises the two cap-limited scores back onto 0–100. `cap`
is category-specific (110–150), so **a Small Cap fund's Blended WM Score is divided
by 150 while a Debt fund's is divided by 110** — the same WM-to-hurdle ratio scores
differently by category.

**Tiers** (`fqs_engine.py:870-875`): ≥90 Elite, ≥80 Strong, ≥70 Good, ≥60 Average,
else Weak.

### 6.8 Additional per-horizon metrics

| Metric | Formula | Location |
|---|---|---|
| `alpha_trend` | `mean(wms[split:]) / mean(wms[:split])`, `split = max(1, int(0.70n))`; `None` if `n < 3`. Chronological order. | `fqs_engine.py:791-797` |
| `stability_score` | `clamp((1 − σ/1.5) × 100, 0, 100)` — absolute σ, no hurdle | `fqs_engine.py:803` |
| `stress_score` | `0.4·MIN(downside_score×0.85,100) + 0.4·MIN(es_score×0.8,100) + 0.2·C` where `C = MIN(100, MAX(20, ((0.6+(min_wm_actual−0.6)×0.75)−0.6)/0.9×80+20))`, `min_wm_actual` = observed minimum WM | `fqs_engine.py:813-823` |

None of these feed FQS. They are reported only.

---

## 7. Cross-horizon aggregates

**Headline FQS** — first available of horizons `[15, 10, 5, 3]`, else the longest
computed (`fqs_engine.py:914-924`). Note this is a *preference order*, so a fund
with 15Y history is scored at 15Y even if the user asked for 5Y.

**Cycle Robustness** (`fqs_engine.py:932-955`) — weighted Success % across horizons,
variant chosen by `user_years`:

```
user_years < 10  (or None) → None
10 ≤ user_years < 15       → {3: 0.35, 5: 0.35, 10: 0.30}
user_years ≥ 15 and 15 available → {3: 0.30, 5: 0.30, 10: 0.25, 15: 0.15}
CR = Σ wₕ · success_pctₕ × 100 / Σ wₕ   (over available horizons only)
```

**At the 5Y backtest horizon, Cycle Robustness is `None` and therefore not
testable.**

**Stability Check** = `Median WM(10Y) / Median WM(5Y)`, `None` if either missing
(`fqs_engine.py:958-960`).

**Headline XIRR** (`fqs_engine.py:541-559`) — a single full-history SIP XIRR via
`pyxirr`, requires ≥13 monthly points. Not per-horizon, not used in FQS, not a
forward prediction.

### 7.1 Ranking — authoritative statement

Backend (`server.py:548-557`):

```python
def _sort_key(f):
    wm  = f.get("selected_blended_wm")
    cat = f.get("category", "")
    return (2 if cat == "Debt" else (0 if wm is not None else 1),
            -(wm if wm is not None else 0),
            f.get("scheme_name", ""))
```

Three groups: (0) has Blended WM, (1) missing data, (2) Debt — always last
regardless of score. Within a group, Blended WM descending, then name.

Frontend override (`home.tsx:1512-1532`): Blended WM for "All" and broad-category
views; **FQS descending** for a specific AMFI sub-category.

**`category == "Debt"` sinking to the bottom is the only category rule in the entire
system.** There is no risk profile, no goal input, no allocation optimiser, no
category constraint anywhere (confirmed by repo-wide grep).

### 7.2 Nesting property — corrected

> **Correction (2026-07-30).** An earlier revision of this section stated the
> filter as `end_date ≤ cutoff`. **That is wrong.** It passed a spot-check at
> 2024-12-31 by luck and fails at boundary cutoffs. The corrected rule is below;
> `backtest/point_in_time.py` implements the corrected rule and
> `verify_nesting()` checks it at *every* cutoff rather than a sample.

Windows computed on `M` truncated at cutoff `T` are a **prefix** of the
full-history windows, with identical WMs. `A₁` is anchored to the *first*
month-end, which truncation does not move, so window *k* is the same window at
every cutoff that admits it.

**Admission rule — the subtle part.** `compute_rolling_windows` terminates on
`EOMONTH(Bₖ) > EOMONTH(last month-end)`, where `Bₖ` is the window's *nominal*
redemption date. But the end NAV is read from the last month-end **at or before**
`Bₖ` (`fqs_engine.py:603-607`), which is frequently the *previous* month. So a
window can satisfy `end_date ≤ T` and still be inadmissible at `T`, because the
model insists on seeing the nominal redemption month before counting the window
at all.

*Concrete case (HDFC Small Cap, 5Y):* window 1 runs `A₁ = 2019-06-01`,
`B₁ = 2024-06-01`, `end_date = 2024-05-31`. At cutoff **2024-05-31** the naive
filter admits it; the model produces **zero** windows, because
`EOMONTH(2024-06-01) = 2024-06-30 > 2024-05-31`. The naive filter would hand the
model one window of genuine look-ahead.

Correct rule:

```
admissible at T  ⟺  EOMONTH(Bₖ) ≤ EOMONTH(last month-end NAV ≤ T)
```

Note the right-hand side is the **fund's own** last month-end at or before `T`, not
`EOMONTH(T)` — a fund that stopped reporting in 2023 must not gain windows just
because the cutoff advanced.

**A second trap:** `Bₖ` cannot be matched back to a window by `start_date`. A gap
in the NAV series makes consecutive windows share a first SIP date — observed **6
times in 25 windows** on the fixture data — which silently mis-assigns every
subsequent `Bₖ`. `build_all_windows()` therefore replays the loop step-for-step
and asserts its output against the engine's.

**Verified:** exact agreement across all 73 cutoffs for HDFC Small Cap
(130502), Mirae Large Cap (107578) and UTI Nifty 50 (100822), and for a synthetic
100× unit-change series that forces `sanitize_nav_history` to rebase a prefix.
The splice case holds because sanitisation scales an entire prefix by a constant,
and a rolling SIP wealth multiple is invariant to uniform scaling of every NAV
inside the window.

**Consequence:** windows are computed **once per fund** over full history and
sliced per cutoff — exact, not approximate, eliminating the
cutoffs × funds × windows blow-up.

---

## 8. Expected wealth

Only produced by `proposal_engine.build_mode_projection()`
(`proposal_engine.py:249-303`), and only after an advisor supplies funds and
allocations (1–10 funds, weights summing to 1.00 ± 0.01,
`proposal_routes.py:74-84`).

```
capital_invested   = amount × H × 12                       (SIP)
expected_wealth    = Σ_f allocₓ × amount × WM_f × H × 12
blended_wm_median  = expected_wealth_median / capital_invested
blended_xirr       = sip_wm_to_xirr(blended_wm, H)
```

Two point estimates are produced: one using `median_wm`, one using `p10_wm`.

> **Note the inconsistency:** the ranking screen sorts on `blended_wm` (0.6·median +
> 0.4·P10), but the proposal projects wealth from `median_wm` and `p10_wm`
> separately. The number that ranks a fund is not the number that projects its
> wealth.

**Horizon fallback** (`proposal_engine.py:181-198`): if no row exists at the
requested horizon, use `select_robust_horizon()` — the **longest** horizon in
`[5, H]` with `n_windows ≥ 10` (`additional_rec.py:28-55`). The shorter window's
XIRR is then held constant and compounded out to `H`
(`proposal_engine.py:214-226`).

**WM ↔ XIRR** (`xirr_utils.py`):

```
sip_wm_to_xirr:  solve ((1+r)^n − 1)/r = WM·n  for monthly r, n = 12H; return 12r
                 Newton–Raphson, 200 iters; returns None outside [−0.5, 5.0]
sip_xirr_to_wm:  m = xirr/12;  WM = ((1+m)^n − 1)/(n·m)
lumpsum:         xirr = WM^(1/H) − 1
```

The SIP form is a **nominal** annual rate (monthly × 12), not effective annualised.

---

## 9. Additional Recommendations (secondary list)

`additional_rec.py` + `server.py:562-614`. Funds with history **≥ 5 years but
< tenure + 2** (i.e. below Core eligibility):

```
ADDITIONAL_REC_MIN_HISTORY_YEARS = 5
ADDITIONAL_REC_MIN_WINDOWS       = 10
ADDITIONAL_REC_MIN_XIRR          = 0.15
```

Selection: longest horizon `h ∈ [5, required_history]` with `blended_wm` present and
`n_windows ≥ 10`; qualify if `sip_wm_to_xirr(blended_wm, h) ≥ 0.15`. Sorted by
`best_horizon` DESC, then `blended_xirr` DESC (`server.py:614`).

Note it converts **`blended_wm`**, not `median_wm`.

---

## 10. Missing-data and edge-case behaviour

| Condition | Behaviour | Location |
|---|---|---|
| NAV ≤ 0 | Row dropped at parse | `amfi_client.py:223-224` |
| Unparseable row | Dropped silently | `amfi_client.py:226-227` |
| Duplicate dates (same scheme) | Kept — can produce a 0-day gap | `amfi_client.py:225-228` |
| Duplicate dates (across stitch) | Successor wins | `fund_lineage.py:222-227` |
| Gap in NAV series | **No detection or fill.** Inflates `history_years` (a pure span); a missing month simply reduces the instalment count (§5.2) | — |
| `len(monthly) < H×12 + 1` | Horizon skipped entirely, no error | `fqs_engine.py:587-588` |
| `len(monthly) < 13` | `headline_xirr = None` | `fqs_engine.py:542-543` |
| No windows at any horizon | `headline_fqs = None`, `headline_tier = "Weak"` | `fqs_engine.py:969-970` |
| `n == 1` | `std = 0` → `CV = 0` → `cv_score = 0` | `fqs_engine.py:740` |
| `n < 3` | `alpha_trend = None` | `fqs_engine.py:796-797` |
| `mean == 0` or NaN | `_safe_div` → `0.0` | `fqs_engine.py:653-656` |
| `P25 ≤ 0` | `downside_severity = 100` (worst) | `fqs_engine.py:751` |
| No windows below P10 | `es10 = min(wms)` | `fqs_engine.py:766-767` |
| Category unmatched | Falls back to `Misc Fund` (= Flexi Cap numbers, equity) | `category_map.py:195,316` |
| mfapi transient 5xx during stitch | Caught; yields a **shorter, unstitched series with no error surfaced** | `amfi_client.py:374-378,414-418` |

---

## 11. Look-ahead inventory for the backtest

| # | Leak | Severity | Treatment |
|---|---|---|---|
| L1 | `sanitize_nav_history` rescales all earlier NAVs from a later-detected split | **High** | Re-run on the truncated series at each cutoff |
| L2 | Forward (successor) stitching uses today's merger map | **High** | Backward-only stitching in the backtest |
| L3 | `classify_fund` uses today's AMFI `scheme_category`; post-2018 SEBI recategorisation. Category → `min_wm_required`, `cap`, equity/non-equity weights | **High** | *Approved:* use today's category, then quantify via a sensitivity re-run on plausibly-recategorised funds |
| L4 | `CATEGORY_TABLE` hurdles were calibrated on full history (FINAL MODEL sheet) | **High**, unfixable | Disclose. Cannot refit without violating the no-optimisation rule |
| L5 | `fund_nav_meta.history_years` is today's span, never refreshed | Medium | Recompute per cutoff from truncated NAVs |
| L6 | `fqs_by_horizon` in Mongo is today's batch output | Medium | Never read; always recompute via `analyze_fund()` |
| L7 | Universe drawn from today's `GET /mf` list — dead funds absent | **High**, partly unfixable | Seed from `predecessor_map` + wound-up codes; **measure and report the residual gap** |
| L8 | `is_idcw_scheme` / Direct-plan filters applied on today's names | Low | Names rarely change materially; disclose |

---

## 12. Statistical constraints (must appear in BACKTEST_REPORT.md)

Not a code finding, but binding on what the backtest can conclude.

- **Overlapping forward windows.** Monthly cutoffs with a 5Y forward horizon overlap
  ~98%. Over a *D*-year data span, independent non-overlapping 5Y periods ≈ `D/5`.
- **Cross-sectional questions are answerable** — "at a given cutoff, did
  higher-ranked funds beat lower-ranked funds?" Hundreds of funds per cutoff. Method:
  Spearman ρ per cutoff, aggregated Fama–MacBeth with Newey–West SEs (lag ≈ 60
  months). Everything demeaned by cutoff.
- **Level questions are not answerable** — "does FQS predict absolute return?" With
  a handful of independent periods, any figure describes the sample market regime,
  not the model.
- **Baselines are mandatory.** Accuracy figures are uninterpretable without: random
  selection, a single trailing 5Y SIP WM (the naive momentum baseline — critical,
  since the model is largely a smoothed version of it), trailing CAGR, and
  equal-weight category average.
- **Collinearity.** Blended WM and FQS are both functions of the same WM
  distribution. Report their rank correlation before treating their results as
  independent evidence.
- **Ignore `backtest_data/oos_summary.json`.** Its ρ = −0.73 is a single-fund
  *time-series* correlation, largely an artefact of FQS being high after a market
  run-up. Not evidence about cross-sectional selection.

---

## 13. Verification

`backend/tests/test_model_audit_spec.py` re-implements every formula in §5–§6 from
this document alone — no imports from `fqs_engine` for the math — and compares
against the live engine.

**Result: all checks pass** on HDFC Small Cap (130502), Mirae Large Cap (107578),
Axis Midcap (114564), UTI Nifty 50 (100822), SBI Small Cap (125494) at horizons
1Y / 3Y / 5Y. Tolerances: exact for `n_windows`, 1e-4 for Median WM / P10 WM / CV /
Success % / Blended WM / `min_wm_required`, 0.05 for FQS.

### 13.1 Worked example — HDFC Small Cap, 5Y horizon

Data 2019-05 → 2026-06 (86 month-end points), category `Small Cap` (equity,
cap 150), hurdle `min_wm_required = 1.5600`.

| Quantity | Value |
|---|---|
| `n_windows` | 25 |
| Median WM | 1.7695 |
| P10 WM | 1.3839 |
| CV | 0.1881 |
| Success % | 0.7600 |
| Blended WM = 0.6(1.7695) + 0.4(1.3839) | **1.6152** |
| **FQS** | **70.0** → tier *Good* |

Why 70.0: Success 76.0 × 0.30 = 22.80; Adjusted Risk × 0.20; Blended WM Score
`min(150, 1.6152/1.56 × 100) = 103.5` → `103.5 × 100/150 = 69.0` × 0.20 = 13.80;
Downside Score `min(150, 1.3839/1.56 × 100) = 88.7` → `59.1` × 0.10; Consistency ×
0.15; ES × 0.05.

---

## 14. Resolved decisions

Agreed 2026-07-30. These are binding on the backtest implementation.

| # | Decision | Rationale |
|---|---|---|
| D1 | **Forward outcome uses the identical window machinery** (§5.1), including the `total_invested` defect (§5.2) | Prediction and actual sit on the same scale; a corrected actual would inject a systematic ~1.7% gap into every comparison |
| D2 | **Debt funds excluded from the backtest universe** | They are force-ranked last regardless of score (§7.1), so they are never realistically selectable. Consequence: the backtest cannot say whether the Debt demotion rule was correct — noted as an out-of-scope limitation |
| D3 | **Universe = `CORE_SUBS ∪ Hybrid`** — Large Cap, Large & Mid Cap, Mid Cap, Small Cap, Multi Cap, Flexi Cap, ELSS, Hybrid (`home.tsx:166-174,1521`) | This is the list a real user chooses from, so top-N accuracy is meaningful |
| D4 | **Primary rank metric = Blended WM; FQS secondary** (§7.1) | Blended WM orders the user-facing list; FQS orders only the sub-category drill-down |
| D5 | **Cutoffs start at the earliest feasible date per fund** (inception + `H` + 2), not a fixed 2019 | 2019 yields ~1.5 independent 5Y periods; earliest-feasible roughly triples it (§12) |
| D6 | **Category taken from today's AMFI label**, with a sensitivity re-run quantifying the bias | No historical category data exists (L3). Faithful to the live model |
| D7 | **Backward-only lineage stitching** | Forward stitching is look-ahead (§3.1) |

### 14.1 Universe size

The `backend/fqs_all_funds.xlsx` production export contains **15,422 rows** — this
is the origin of the "17,000 funds" figure. It includes Direct plans, IDCW
variants, and every plan/option permutation. After the §4 filters (Direct, IDCW,
`H + 2` history, plan/option dedup) plus D2 and D3, the tradeable universe is
expected to be an order of magnitude smaller. **It will be measured, not assumed**,
and reported in `data_quality_report`.

### 14.2 Data-quality observation from the production export

The export shows Kotak Liquid (a Debt fund) with Blended WM 15.81 and Median WM
25.61 at the 4Y rolling year — i.e. a 25× wealth multiple on a liquid fund. The
Debt sanity ceiling should be `1.20⁴ = 2.07` (§5.3). Either the export predates the
ceiling, or classification did not route these rows to `Debt`. Excluded from the
backtest under D2, but it indicates the sanity cap is not uniformly effective and
belongs in the data-quality report.

---

## 15. Known blocker — NAV data acquisition

`api.mfapi.in` is **not reachable from the analysis sandbox** (proxy returns
`403 Forbidden`), and `MONGO_URL` points at `localhost:27017` on the developer
machine. Neither the live NAV API nor the populated Mongo instance can be read from
where the backtest is being built.

Local NAV data available today: `backtest_data/universe_nav.json` — **11 funds,
2019-04 → 2026-07 only.** Sufficient to build and validate the engine, insufficient
for any real result (it cannot even satisfy the 7-year history rule before 2026).

**Resolution:** `backtest/download_navs.py` is a standalone, resumable downloader to
be run **on the developer machine**, writing to `backtest_data/nav_store/`. The rest
of the backtest engine is built and tested against the 11-fund fixture in the
meantime and requires no change once the full store exists.

---

## Appendix A — Constant reference

| Constant | Value | Location |
|---|---|---|
| `SIP_AMOUNT` | 10,000 ₹/month | `fqs_engine.py:513` |
| `HORIZONS_FULL` | 1..20 | `fqs_engine.py:514` |
| `MIN_ROLLING_YEARS` / `MAX_ROLLING_YEARS` | 1 / 20 | `server.py:135-136` |
| `HISTORY_BUFFER_YEARS` | 2 | `server.py:137` |
| `ANALYSIS_TTL` | 24 h | `server.py:134` |
| `GENERIC_WM_SANITY_CAP` | 50.0 | `category_map.py:351` |
| `MAX_XIRR_FOR_DEBT` | 0.20 | `category_map.py:352` |
| `MAX_XIRR_FOR_GENERIC` | 0.22 | `category_map.py:353` |
| `SPLICE_RATIO_THRESHOLD` / `SPLICE_MAX_GAP_DAYS` | 4.0 / 31 | `amfi_client.py:183-184` |
| `WOUND_UP_THRESHOLD_DAYS` | 180 | `amfi_client.py:51` |
| `MAX_STITCH_DEPTH` | 4 | `amfi_client.py:310` |
| `MIN_OVERLAP_DAYS` | 15 | `fund_lineage.py:48` |
| `CORR_THRESHOLD_DISCOVERED` / `_CURATED` | 0.90 / 0.80 | `fund_lineage.py:49-50` |
| `MAX_GAP_DAYS_NO_OVERLAP` | 120 | `fund_lineage.py:51` |
| `ADDITIONAL_REC_MIN_XIRR` | 0.15 | `additional_rec.py:23` |
| `ADDITIONAL_REC_MIN_HISTORY_YEARS` | 5 | `additional_rec.py:24` |
| `ADDITIONAL_REC_MIN_WINDOWS` | 10 | `additional_rec.py:25` |
| `MAX_FUNDS` / `ALLOCATION_TOLERANCE` | 10 / 0.01 | `proposal_engine.py:83-84` |
| FQS tiers | 90 / 80 / 70 / 60 | `fqs_engine.py:870-875` |

## Appendix B — Backtest configuration derived from this audit

```
INVESTMENT_MODE      = "sip"
SIP_AMOUNT           = 10_000                    # fqs_engine.py:513
ROLLING_PERIOD       = 5                         # years, this test
HISTORY_BUFFER       = 2                         # server.py:137
MIN_HISTORY          = ROLLING_PERIOD + HISTORY_BUFFER   # = 7, derived not hardcoded
VALIDATION_HORIZON   = 5                         # years
CUTOFF_FREQUENCY     = "monthly", on true calendar month-ends   # §2.2
CUTOFF_START         = per-fund earliest feasible (inception + MIN_HISTORY)
CUTOFF_END           = latest complete month-end in the dataset
SUPPORTED_HORIZONS   = 1..20                     # fqs_engine.py:514
PRIMARY_RANK_METRIC  = "blended_wm"              # D4, server.py:548-557
SECONDARY_RANK_METRIC= "fqs"                     # D4, home.tsx:1528-1531
STITCH_DIRECTION     = "backward_only"           # D7, §3.1
CATEGORY_SOURCE      = "current_amfi"            # D6, L3 + sensitivity re-run
UNIVERSE             = CORE_SUBS | {"Hybrid"}    # D3, home.tsx:166-174
EXCLUDE_CATEGORIES   = {"Debt"}                  # D2
ACTUAL_OUTCOME_MATHS = "model_identical"         # D1, defect reproduced
```
