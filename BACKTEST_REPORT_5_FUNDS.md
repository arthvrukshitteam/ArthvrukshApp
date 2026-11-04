# Backtest report — 5 funds

Source: `backtest_data/results_fixture/` (the committed fixture run, generated 2026-07-30).
Nothing here was re-run against the network; these are the parquet/JSON outputs already in the repo,
re-aggregated per fund.

---

## Read this first — what this run can and cannot tell you

| Setting | Value | Comment |
|---|---|---|
| Funds in universe | 11 | fixture, not the real universe |
| NAV history available | **2019-04 → 2026-07, ~7.1 years each** | **no fund here has 14 years** |
| Rolling period tested | **2 years** | `config.py` declares `ROLLING_PERIOD_YEARS = 5` |
| Validation horizon | 2 years | |
| `min_history_years` used | **3** | `config.py` derives **7** (5 + `HISTORY_BUFFER_YEARS` 2) |
| Cutoffs | 51 generated, 25–26 validated | 2022-05-31 → 2024-06-30 |
| Predictions / validated | 544 / 268 (264 still pending) | |
| **Independent periods** | **2.04** | the number that governs everything below |

Two independent periods. `accuracy_summary.json` marks **every** rank-quality metric
`"not interpretable — too few independent periods"`, and that verdict is correct — with
overlapping 2-year windows, 25 cutoffs contain about 2 genuinely independent observations.

So: **nothing below is evidence about whether the model picks good funds.** What it *is* good for
is (a) confirming the machinery runs correctly and (b) exposing a specific failure mode worth
testing properly once you have the 14-year universe. The distinction matters — several numbers
here look damning and should not be read that way yet.

Also note the fixture window (2022-05 → 2024-06) is a *single* regime: a small-cap-led rally
followed by rotation. That one fact explains most of what follows.

---

## The five funds

`pred_rank` / `act_rank` are mean ranks out of ~10.8 funds, 1 = best.
`wm` = wealth multiple. `above_p10` = share of validated cutoffs where the realised WM
landed at or above the model's own p10 (downside) estimate — should be ≈0.90.

| Code | Fund | n | pred_rank | act_rank | Δrank | pred WM | act WM | WM err | above_p10 | mean FQS |
|---|---|---|---|---|---|---|---|---|---|---|
| 100177 | quant Small Cap | 24 | **1.00** | 7.17 | **+6.17** | 1.626 | 1.186 | **−0.440** | **0.25** | 82.6 |
| 130502 | HDFC Small Cap | 25 | 2.24 | 7.48 | +5.24 | 1.427 | 1.155 | −0.272 | **0.28** | 78.9 |
| 140225 | Edelweiss Mid Cap | 24 | 4.12 | **1.79** | −2.33 | 1.363 | 1.260 | −0.102 | 0.79 | 76.3 |
| 105503 | Nippon India Growth | 25 | 6.08 | **1.52** | **−4.56** | 1.288 | 1.269 | −0.019 | 0.80 | 70.0 |
| 104481 | Franklin India Prima | 25 | 10.24 | 5.08 | −5.16 | 1.204 | 1.184 | −0.020 | 0.96 | 61.8 |

### 100177 — quant Small Cap Fund
Ranked **#1 at all 24 validated cutoffs**, without exception. Realised mean rank 7.17 of ~10.8;
worst single cutoff missed by **10 places**. Predicted median WM 1.626 vs realised 1.186 — a
₹105,543 mean shortfall on a ₹10k/month SIP. Realised WM fell below the model's own p10 floor in
**75%** of cutoffs. It was in the actual top decile 3 times out of 24.

### 130502 — HDFC Small Cap Fund
Same shape, less extreme: predicted 2.24, realised 7.48, never once in the actual top decile.
Predicted WM 1.427 vs realised 1.155 (−₹65,246). Below p10 in **72%** of cutoffs. Predicted XIRR
averaged 35.2% against 13.6% realised.

### 140225 — Edelweiss Mid Cap Fund
Underrated. Predicted 4.12, realised **1.79** — best or near-best repeatedly. Landed in the actual
top decile **6 times**; the model flagged it as a top-decile pick **zero** times. WM error only
−0.102, and 79% above p10. Worth noting this is one of the curated-stitch funds (predecessor
`107301`, JPMorgan India Mid and Small Cap), though the fixture NAV starts 2019 so no stitching
is exercised here.

### 105503 — Nippon India Growth Fund
The largest miss in the underrated direction: predicted 6.08, realised **1.52**. In the actual top
decile **16 of 25 cutoffs** — the most consistent real winner in the set — while the model never
placed it top-decile. WM error −0.019, essentially calibrated. Its mean FQS was 70.0, mid-pack.

### 104481 — Franklin India Prima Fund
Ranked **last** (10.24) by the model, realised 5.08 — mid-pack. Best-calibrated of the five on
wealth (WM error −0.020, **96%** above p10) and the lowest mean FQS (61.8). The model's least
favoured fund was also its most honestly forecast.

---

## Three findings

**1. Systematic optimism, not noise.** Across all 268 validated predictions: mean predicted WM
1.3238 vs actual 1.1727, mean error **−0.1511**, MAPE 12.4%. All five funds miss in the same
direction. A consistent sign across every fund is a bias, not sampling error — and it survives the
"2 independent periods" objection, because it is arithmetic on realised outcomes rather than
statistical inference about ranking skill.

**2. p10 is not behaving like a downside case.** `wealth_accuracy` reports
`pct_actual_above_p10 = 0.6455` against an expected ≈0.90. The report's own note says this means
the downside estimate is miscalibrated. It concentrates badly: 25% and 28% for the two small caps
versus 96% for Franklin Prima. The p10 floor fails precisely where a user would lean on it hardest.

**3. The rank signal is inverted in this window — with a large caveat.**

| Metric | Role | mean ρ | t | % cutoffs positive |
|---|---|---|---|---|
| `blended_wm` | **MODEL** (app's rank metric) | 0.016 | 0.12 | 0.44 |
| `fqs` | **MODEL** headline | **−0.215** | **−2.18** | 0.24 |
| `success_pct` | component | **−0.218** | **−2.45** | 0.24 |
| `median_wm` | component | 0.047 | 0.36 | 0.44 |
| `p10_wm` | component | −0.088 | −0.74 | 0.40 |
| `cv` | component (lower better) | −0.054 | −0.41 | 0.44 |
| `trailing_wm` | BASELINE | 0.003 | 0.04 | 0.40 |

Selection hit rates:

| | picked actual winner | top-10% hit rate | mean percentile of picks |
|---|---|---|---|
| Model (`blended_wm`) | 0.12 | 0.12 | 0.439 |
| Model (`fqs`) | 0.12 | 0.12 | 0.490 |
| Baseline (`trailing_wm`) | **0.16** | **0.16** | 0.546 |
| Random | 0.097 | 0.10 | 0.500 |

The model does not beat a single trailing window, and both sit near chance. `blended_wm` is
flat (ρ 0.016); `fqs` and `success_pct` are significantly *negative* on their face.

**The caveat is decisive.** With ~2 independent periods in one regime, an inverse tilt is exactly
what mean reversion after a small-cap run looks like — the model's favourites were the funds that
had just run hardest, and 2022-2024 punished them. That is a plausible *momentum-chasing* failure
mode, not proof the score is backwards. Do not conclude `FQS` is inverted from this run. Conclude:
untested, with a specific hypothesis now worth falsifying.

---

## Mechanics that did check out

- **Eligibility gate fires correctly.** At cutoff 2022-04-30 all 11 funds were excluded
  `insufficient_history` (required 3y, available 2.75–3.00) → `NO_ELIGIBLE_FUNDS`, no predictions
  emitted. `exclusion_report.parquet` records the arithmetic per fund.
- **Data quality is clean.** All 11 funds: 0 duplicate dates, 0 missing months, 0 long gaps,
  0 detected splices, months_present == months_expected.
- **No look-ahead in stitching.** `stitch_direction: backward_only`, per MODEL_AUDIT.md §3.1.
- **Prediction and outcome share a scale.** `actual_outcome_maths: model_identical` — the realised
  figure is computed with the model's own window machinery, defects included, so the comparison is
  apples-to-apples.

## Two config discrepancies to resolve

The fixture run used `rolling_period_years = 2` and `min_history_years = 3`. `backtest/config.py`
declares `ROLLING_PERIOD_YEARS = 5`, which derives `MIN_HISTORY_YEARS = 7` via
`HISTORY_BUFFER_YEARS`. So `results_fixture/` does **not** reflect current config defaults — it was
run with a reduced setting to make the 7-year fixture produce any windows at all. Anyone reading
these parquets as "the 5-year backtest" would be misreading them.

## What would make this interpretable

The binding constraint is independent periods, and only real history buys them. On the 14-year
universe at a 5-year horizon you get ~9 non-overlapping windows spanning 2012-2026 — which covers
2013 taper, 2018 mid/small-cap drawdown, the 2020 crash and the 2021-24 small-cap cycle. That is
several regimes instead of one, and it is the run that can actually test finding 3.

Concretely: build the universe (`scripts/build_long_history_universe.py`), then re-run the
backtest at `ROLLING_PERIOD_YEARS = 5` against those funds.
