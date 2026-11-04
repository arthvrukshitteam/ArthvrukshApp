# ArthVruksh — Lumpsum Model Analysis Report

> Complete investigation of the Lumpsum investment flow, SIP vs Lumpsum comparison, and root cause analysis for missing metrics.

---

## Task 1 — Complete Lumpsum Workflow

### 1.1 End-to-End User Flow

```
User selects "Lumpsum" on mode-picker.tsx
    → ModeProvider.setMode("lumpsum")
    → AsyncStorage["@arthvruksh:mode"] = "lumpsum"
    → router.replace("/tenure-picker")

User enters tenure (e.g. 7) on tenure-picker.tsx
    → TenureProvider.setTenure(7)
    → AsyncStorage["@arthvruksh:tenure"] = 7
    → router.replace("/(tabs)/home")

HomeScreen renders LumpsumHomeScreen
    → api.lumpsumUniverse(7)
    → GET /api/lumpsum/universe?tenure=7

Backend resolves and responds
    → Frontend renders ranked FundCards
```

---

### 1.2 Backend: `/api/lumpsum/universe?tenure=N`

**Location:** `backend/server.py` lines 394–481

#### Step 1 — Resolve Horizon

```python
def _resolve_lumpsum_horizon(tenure: int) -> int:
    eligible = [h for h in [3, 5, 7] if h <= tenure]
    return max(eligible) if eligible else 3
```

| User Tenure | Resolved Horizon |
|-------------|-----------------|
| 1Y or 2Y    | 3Y (forced minimum) |
| 3Y          | 3Y |
| 4Y          | 3Y |
| 5Y          | 5Y |
| 6Y          | 5Y |
| 7Y–15Y      | 7Y |

#### Step 2 — Filter Eligible Funds

```python
required_history = tenure + 2   # HISTORY_BUFFER_YEARS = 2
cursor = db.fund_nav_meta.find(
    {"history_years": {"$gte": required_history}},
    {"scheme_code", "scheme_name", "category", "history_years"}
)
```

For tenure=7: requires ≥9 years of NAV history.

#### Step 3 — Join Lumpsum Analysis

```python
lumpsum_cursor = db.fund_lumpsum_analysis.find(
    {"scheme_code": {"$in": scheme_codes}},
    {"scheme_code", "horizons", "stability_check"}
)

# For each fund, find the matching horizon in its stored analysis:
horizon_stats = next(
    (h for h in doc["horizons"] if str(h["horizon"]) == horizon_key),
    None
)

if horizon_stats and horizon_stats.get("fqs") is not None:
    lumpsum_by_code[code] = {
        "blended_wm":      horizon_stats.get("blended_wm"),
        "fqs":             horizon_stats["fqs"],
        "fqs_tier":        fqs_tier(horizon_stats["fqs"]),
        "stability_check": doc.get("stability_check"),
    }
```

#### Step 4 — Sort

```python
eligible.sort(key=lambda f: (
    2 if f.get("category") == "Debt" else (0 if f["blended_wm"] is not None else 1),
    -(f["blended_wm"] or 0),
))
```

| Sort Bucket | Condition |
|-------------|-----------|
| 0 (top) | Non-Debt fund WITH blended_wm, sorted desc |
| 1 | Fund without blended_wm (no lumpsum analysis stored) |
| 2 (bottom) | Debt category — always last |

#### Step 5 — Response Shape

```json
{
  "tenure": 7,
  "resolved_horizon": 7,
  "required_history_years": 9,
  "total_funds_indexed": 1842,
  "eligible_fund_count": 623,
  "eligible_funds": [
    {
      "scheme_code": 120503,
      "scheme_name": "Nippon India Small Cap Fund - Regular Plan - Growth",
      "category": "Small Cap",
      "history_years": 13.4,
      "blended_wm": 2.84,
      "fqs": 83.7,
      "fqs_tier": "Strong",
      "stability_check": 1.44
    }
  ]
}
```

---

### 1.3 Lumpsum Analysis Engine: `lumpsum_engine.py`

The engine is invoked per-fund via `GET /api/funds/{code}/analyze/lumpsum` and stores results in `fund_lumpsum_analysis` collection.

#### Input

Raw daily NAV from AMFI via `amfi_client.fetch_fund_nav(scheme_code)`.

#### Step 1 — Extract Month-End NAVs

Keep only the last trading day of each calendar month. Minimum 36 months required.

```python
# For each calendar month → keep last NAV date
if len(nav_dates) < 36:
    return None  # insufficient history
```

#### Step 2 — For Each Horizon H ∈ [3, 5, 7, 15]

**Min WM Benchmark:**
```python
min_wm = get_lumpsum_min_wm(category, H)
       = (1 + xirr_req) ^ H
```

Example (Small Cap, 7Y): `(1 + 0.17)^7 = 2.5023`

**Rolling Window Construction:**
```
Window 1: Invest ₹10,000 on 2014-02-01 → Redeem 2021-02-01
Window 2: Invest ₹10,000 on 2014-02-28 → Redeem 2021-02-28
Window 3: Invest ₹10,000 on 2014-03-31 → Redeem 2021-03-31
...
```

WM per window = `ending_nav / starting_nav` (NOT XIRR — it is a direct price ratio)

Window marked "good" if `WM ≥ min_wm`.

#### Step 3 — Per-Horizon Statistics

| Stat | Formula |
|------|---------|
| failure_pct | count(WM < min_wm) / n_windows |
| success_pct | 1 − failure_pct |
| blended_wm | 0.6 × median_WM + 0.4 × P10_WM |
| downside_sev_score | min(100, 100 × min_wm / P25) |
| cv_score | min(100, CV × 100) where CV = std_dev/mean |
| risk_score | 0.40 × failure_score + 0.25 × downside_sev + 0.35 × cv_score |
| adj_risk_score | 100 − risk_score |
| blended_wm_score | min(blended_wm / min_wm × 100, cap) |
| downside_score | min(P10 / min_wm × 100, cap) |
| es10 | avg(WMs where WM < P10) |
| es_score | min(100, 100 × es10 / min_wm) |

#### Step 4 — FQS Formula

**Equity categories:**
```
FQS = 0.35 × Success×100
    + 0.25 × AdjRisk
    + 0.20 × (BlendedWMScore/cap) × 100
    + 0.10 × (DownsideScore/cap) × 100
    + 0.00 × Consistency
    + 0.10 × ESScore
```

**Non-equity categories:**
```
FQS = 0.25 × Success×100
    + 0.35 × AdjRisk
    + 0.15 × (BlendedWMScore/cap) × 100
    + 0.10 × (DownsideScore/cap) × 100
    + 0.10 × Consistency
    + 0.05 × ESScore
```

#### Step 5 — Headline FQS

```python
headline_fqs = (0.35×FQS_3Y + 0.35×FQS_5Y + 0.30×FQS_7Y) / total_available_weight
# Preferred headline horizon: 5Y (if available), else max available
```

#### Step 6 — Cross-Horizon Metrics

```python
# Cycle Robustness (if 3Y, 5Y, 7Y all available)
cycle_robustness = 0.35×success_3Y + 0.35×success_5Y + 0.30×success_7Y

# Stability Check
stability_check = Median_7Y_WM / Median_5Y_WM
# Requires both 5Y and 7Y to be available
# > 1.0 means fund grows better over longer time → positive sign
# < 1.0 means fund deteriorates at longer horizon → warning sign
```

#### FQS Tier Thresholds

| Score | Tier |
|-------|------|
| ≥ 90 | Elite |
| ≥ 80 | Strong |
| ≥ 70 | Good |
| ≥ 60 | Average |
| < 60 | Weak |

---

### 1.4 Category Benchmarks (Lumpsum)

| Category | XIRR Req | Min WM 3Y | Min WM 5Y | Min WM 7Y | Cap | Equity? |
|----------|----------|-----------|-----------|-----------|-----|---------|
| Small Cap | 17% | 1.292 | 1.560 | 2.502 | 150 | Yes |
| Mid Cap | 15% | 1.253 | 1.477 | 2.660 | 145 | Yes |
| Multi Cap | 14% | 1.233 | 1.437 | 2.503 | 140 | Yes |
| Flexi Cap | 13% | 1.214 | 1.398 | 2.353 | 140 | Yes |
| ELSS | 13% | 1.214 | 1.398 | 2.353 | 140 | Yes |
| Large Cap | 12% | 1.197 | 1.362 | 2.211 | 130 | Yes |
| Index Fund | 10% | 1.160 | 1.290 | 1.949 | 130 | No |
| Hybrid | 13% | 1.214 | 1.398 | 2.353 | 140 | No |
| Gold/Silver | 8% | 1.125 | 1.225 | 1.714 | 120 | No |
| Debt | 7% | 1.108 | 1.193 | 1.606 | 110 | No |

---

## Task 2 — SIP vs Lumpsum Model Comparison

### 2.1 Investment Mechanism

| Aspect | SIP | Lumpsum |
|--------|-----|---------|
| Investment | ₹10,000/month for N×12 months | ₹10,000 once on start date |
| Wealth Multiple | `final_value / total_invested` | `ending_nav / starting_nav` |
| Return metric | XIRR (time-weighted cash flows) | Simple price ratio (no cash flow timing) |
| Window advance | Monthly (one month forward per step) | Monthly (one month forward per step) |
| Minimum data | N×12 + 1 monthly points | N×12 monthly points |

**Key observation:** SIP WM includes the timing effect of monthly purchases. If NAVs are volatile, SIP WM can exceed Lumpsum WM because units are bought cheaper during dips. Lumpsum WM only captures end-to-end price appreciation.

---

### 2.2 Horizons Computed

| Model | Horizons | FQS-scored | Display-only |
|-------|----------|-----------|--------------|
| SIP | 1Y–20Y | All | None |
| Lumpsum | 3Y, 5Y, 7Y, 15Y | 3Y, 5Y, 7Y | 15Y |

---

### 2.3 FQS Formula Weights — CRITICAL DIFFERENCE

The FQS formulas look structurally identical but have **different weight vectors**.

**Equity categories:**

| Component | SIP weight | Lumpsum weight | Difference |
|-----------|-----------|----------------|------------|
| Success×100 | **0.30** | **0.35** | Lumpsum values success +5pp |
| AdjRisk | **0.20** | **0.25** | Lumpsum values risk control +5pp |
| BlendedWM/cap×100 | 0.20 | 0.20 | Same |
| Downside/cap×100 | 0.10 | 0.10 | Same |
| Consistency | **0.15** | **0.00** | SIP rewards consistency; Lumpsum ignores it |
| ESScore | **0.05** | **0.10** | Lumpsum more sensitive to tail losses |

**Non-equity categories:**

| Component | SIP weight | Lumpsum weight | Difference |
|-----------|-----------|----------------|------------|
| Success×100 | 0.25 | 0.25 | Same |
| AdjRisk | 0.35 | 0.35 | Same |
| BlendedWM/cap×100 | 0.15 | 0.15 | Same |
| Downside/cap×100 | 0.10 | 0.10 | Same |
| Consistency | **0.10** | **0.10** | Same |
| ESScore | **0.05** | **0.05** | Same |

**Implication:** A fund with the same raw data would receive a higher FQS in Lumpsum if it has a very high success rate, and a lower FQS if its tail losses (ES10) are severe. SIP rewards consistent month-over-month returns more.

---

### 2.4 Stability Metrics — Fundamentally Different

This is the most significant conceptual divergence.

| Metric | SIP | Lumpsum |
|--------|-----|---------|
| Name | `stability_score` | `stability_check` |
| Type | Per-horizon score (0–100) | Cross-horizon ratio (any positive float) |
| Formula | `max(0, min(100, (1 − std_dev/1.5) × 100))` | `Median_7Y_WM / Median_5Y_WM` |
| What it measures | How tight (low-variance) the WM distribution is | Whether the fund's median return improves from 5Y to 7Y |
| Interpretation | Higher = more stable returns | >1 = improving; <1 = deteriorating; ≈1 = flat |
| Scope | Specific to the user's selected horizon | Always 7Y÷5Y — fixed, regardless of selected horizon |
| Null condition | Never null (computed for every horizon) | Null if fund lacks either 5Y or 7Y data |
| UI label | "Stability" | "Stability ×" |

**Implication for the UI:** When a user selects 3Y Lumpsum tenure, the "Stability ×" shown on their fund card is still Median_7Y/Median_5Y — it does NOT reflect the 3Y horizon they selected. The stability check is a global fund characteristic, not horizon-specific.

---

### 2.5 Headline FQS Calculation

| Aspect | SIP | Lumpsum |
|--------|-----|---------|
| Strategy | Pick longest available anchor (15Y > 10Y > 5Y > 3Y) | Weighted average across available 3Y/5Y/7Y |
| Preferred horizon | 15Y (if available) | 5Y |
| Weights | Not weighted — single anchor horizon | 0.35×FQS_3Y + 0.35×FQS_5Y + 0.30×FQS_7Y |
| Headline = | FQS at the longest single horizon | Blend of all three |

**Implication:** SIP headline FQS is purely the fund's FQS at its longest horizon. Lumpsum headline FQS smooths over all three horizons, which means a fund that is great at 3Y but poor at 7Y will have its headline pulled down in Lumpsum.

---

### 2.6 Cycle Robustness

| Aspect | SIP | Lumpsum |
|--------|-----|---------|
| Formula | Depends on `user_years` parameter | Always 3-horizon formula |
| user_years < 10 | CR = null (not computed) | N/A |
| user_years 10–14 | `0.35×S3 + 0.35×S5 + 0.30×S10` | N/A |
| user_years ≥ 15 | `0.30×S3 + 0.30×S5 + 0.25×S10 + 0.15×S15` | N/A |
| Lumpsum formula | N/A | `0.35×S3Y + 0.35×S5Y + 0.30×S7Y` |
| S = | success_pct (decimal, 0–1) | success_pct (decimal, 0–1) |

---

### 2.7 SIP-Exclusive Metrics (No Lumpsum Equivalent)

The SIP `fqs_engine.py` computes three additional per-horizon metrics that the Lumpsum engine does NOT compute:

**1. Alpha Trend**
```python
# avg(latest 30% windows) / avg(earliest 70% windows)
# >1 means the fund is getting better over time
# <1 means deteriorating
alpha_trend = avg(wms[split:]) / avg(wms[:split])  # chronological order
```
This is absent from `lumpsum_engine.py` entirely.

**2. Stability Score (SIP per-horizon)**
```python
# Pure dispersion measure — how tight are the WMs?
stability_score = max(0, min(100, (1 − std_dev / 1.5) × 100))
```
Note: This is different from Lumpsum's `stability_check`. The SIP stability score is a 0-100 value per-horizon. Lumpsum has no equivalent.

**3. Stress Score**
```python
stress_score = (0.4 × min(downside_score×0.85, 100)
              + 0.4 × min(es_score×0.8, 100)
              + 0.2 × tail_component)
```
Absent from lumpsum engine.

---

### 2.8 Data Pipeline Architecture

| Aspect | SIP | Lumpsum |
|--------|-----|---------|
| Primary collection | `fund_nav_meta` | `fund_lumpsum_analysis` |
| Batch job | `fqs_refresh.py` — nightly at 22:30 UTC | **None — no batch job exists** |
| Coverage | ALL funds in `fund_nav_meta` index | ONLY funds individually viewed |
| Trigger | Nightly scheduler | Manual: user views fund detail page |
| TTL | 24 hours (re-runs nightly) | 24 hours (re-runs on next view) |

---

### 2.9 Universe Endpoint Differences

| `/home/universe?years=N` (SIP) | `/lumpsum/universe?tenure=N` (Lumpsum) |
|-------------------------------|----------------------------------------|
| Reads `fund_nav_meta.fqs_by_horizon[N]` | Reads `fund_lumpsum_analysis.horizons[resolved_H]` |
| Returns `selected_blended_wm`, `selected_stability_score` | Returns `blended_wm`, `stability_check` |
| All indexed funds have data (after FQS refresh) | Only viewed funds have data |
| stability is 0–100 score | stability is raw Median_7Y/Median_5Y ratio |

---

## Task 3 — Root Cause: Why Blended WM & Stability Show "—" on Lumpsum Screen

### 3.1 What the UI Actually Renders

In `home.tsx`, the `FundCard` component for Lumpsum:

```tsx
<FundCard
  blendedWm={item.blended_wm}          // from LumpsumEligibleFund
  fqs={item.fqs}
  fqsTier={item.fqs_tier}
  stability={item.stability_check}      // from LumpsumEligibleFund
  stabilityLabel="Stability ×"
/>
```

Inside `FundCard`:
```tsx
<MetricCell
  value={blendedWm !== null ? `${blendedWm.toFixed(2)}×` : "—"}
  label="Blended WM"
/>
<MetricCell
  value={stability !== null ? stability.toFixed(2) : "—"}
  label={stabilityLabel}
/>
```

**The UI code is correct.** It renders the value when non-null and "—" when null. The issue is upstream.

---

### 3.2 Where the Null Values Come From

**Backend: `/lumpsum/universe`**

```python
ldata = lumpsum_by_code.get(fund["scheme_code"])  # ← lookup in lumpsum_analysis collection
if ldata:
    fund.update(ldata)
else:
    fund["blended_wm"]      = None   # ← ALL metrics are null if no analysis stored
    fund["fqs"]             = None
    fund["fqs_tier"]        = None
    fund["stability_check"] = None
```

`lumpsum_by_code` is populated from `fund_lumpsum_analysis` collection. That collection is only written by:
```python
# In server.py, /funds/{code}/analyze/lumpsum endpoint:
await db.fund_lumpsum_analysis.update_one(
    {"scheme_code": scheme_code, "start_date_applied": None},
    {"$set": payload_dict},
    upsert=True,
)
```

**This endpoint is called only when a user navigates to an individual fund's detail page in Lumpsum mode.**

---

### 3.3 Root Cause Confirmed: No Lumpsum Batch Job

**SIP path (works):**
```
fqs_refresh.py (nightly at 22:30 UTC)
    → iterates ALL funds in fund_nav_meta
    → calls analyze_fund() for each
    → writes fqs_by_horizon (including blended_wm, stability_score) to fund_nav_meta
    → /home/universe reads from this — data always available
```

**Lumpsum path (broken for new funds):**
```
No batch job exists
    → fund_lumpsum_analysis starts empty on fresh deployment
    → /lumpsum/universe joins against empty collection
    → blended_wm, fqs, stability_check all return null
    → All fund cards show "—" for every metric
    → Funds sorted to bucket 1 (no WM data), same as having no data
```

---

### 3.4 Secondary Root Cause: Stability Requires Two Horizons

Even when lumpsum analysis IS stored:
```python
# stability_check only computed when BOTH 5Y and 7Y horizons have data:
if 7 in fqs_stats and 5 in fqs_stats:
    stability_check = fqs_stats[7].median_wm / fqs_stats[5].median_wm
# If fund has only 3Y or 5Y of history → stability_check = None always
```

For a user who sets tenure=3Y (resolved horizon=3Y), the fund might have exactly 5Y of history and still show `stability_check=None` because 7Y data doesn't exist.

---

### 3.5 Fix Required: Lumpsum Batch Job

Create `backend/lumpsum_refresh.py` mirroring `fqs_refresh.py`:

```python
async def refresh_all_lumpsum(db, concurrency: int = 4) -> Dict[str, Any]:
    """Nightly batch: compute lumpsum analysis for every fund in fund_nav_meta."""
    # 1. Get all funds in nav_meta index with ≥ 36 months history
    cursor = db.fund_nav_meta.find(
        {"history_years": {"$gte": 3.0}},
        {"scheme_code": 1, "scheme_name": 1}
    )
    funds = [f async for f in cursor]
    
    # 2. For each fund: fetch NAV → analyze → upsert to fund_lumpsum_analysis
    async def one(fund):
        code = fund["scheme_code"]
        try:
            payload = await amfi_client.fetch_fund_nav(code)
            nav_history = amfi_client.parse_nav_history(payload)
            result = await lumpsum_engine.analyze_fund_lumpsum(
                scheme_code=code,
                scheme_name=fund["scheme_name"],
                raw_navs=[{"date": r["date"].isoformat(), "nav": str(r["nav"])}
                          for r in nav_history],
            )
            if result:
                payload_dict = _lumpsum_to_dict(result)
                payload_dict["computed_at"] = datetime.now(timezone.utc).isoformat()
                await db.fund_lumpsum_analysis.update_one(
                    {"scheme_code": code, "start_date_applied": None},
                    {"$set": payload_dict},
                    upsert=True,
                )
        except Exception as exc:
            logger.warning("Lumpsum refresh failed for %s: %s", code, exc)
    
    # 3. Run with concurrency limit
    semaphore = asyncio.Semaphore(concurrency)
    async def bounded(f):
        async with semaphore:
            await one(f)
    
    await asyncio.gather(*[bounded(f) for f in funds])
```

Then schedule it in `server.py` at 23:00 UTC (30 min after FQS refresh completes).

---

## Task 4 — Recommendations

### Priority 1 — Critical Fix: Add Lumpsum Batch Job

**Without this, the Lumpsum home screen is empty data on all new deployments.**

Steps:
1. Create `backend/lumpsum_refresh.py` (template above)
2. Add scheduler in `server.py` lifespan (similar to `_fqs_refresh_scheduler`)
3. Add `POST /api/lumpsum/refresh` manual trigger endpoint (with token auth)
4. Add `GET /api/lumpsum/refresh/status` status endpoint
5. Update the Methodology screen to document this in the pipeline explanation

---

### Priority 2 — UI Fix: Distinguish Stability Metrics

The UI currently renders both SIP and Lumpsum stability under a "Stability" column, but they are completely different metrics.

**SIP:** `selected_stability_score` = 0–100 score (higher = more stable returns)
**Lumpsum:** `stability_check` = Median_7Y/Median_5Y ratio

Proposed fix:
- SIP card: label "Stability" with value shown as `83.5` (out of 100)
- Lumpsum card: label "Stability ×" with value shown as `1.44×` (a multiplier)
- Add a tap-for-info tooltip that explains each

---

### Priority 3 — Add Lumpsum Missing Metrics

The Lumpsum engine is missing three metrics the SIP engine has. These are valuable:

1. **Stability Score (0–100):** Replace or complement `stability_check` with `max(0, min(100, (1−std/1.5)×100))` per-horizon. This is horizon-specific, always computable, and user-intuitive.

2. **Alpha Trend:** Add `avg(latest_30%_WMs) / avg(earliest_70%_WMs)` to flag deteriorating funds. This is especially useful for funds claiming historical performance but declining recently.

3. **Stress Score:** Composite tail-risk score. Easy to add since all components (downside_score, es_score) are already computed in `lumpsum_engine.py`.

---

### Priority 4 — Fix Tenure Validation for Lumpsum

**Current bug:** User can enter 1Y or 2Y as lumpsum tenure. The backend resolves this to 3Y, but the user isn't informed upfront in the UI.

The `tenure-picker.tsx` chips for lumpsum mode currently show `[3, 5, 7]` which is correct. But the free-text input allows 1Y or 2Y with no warning.

**Fix:** In `tenure-picker.tsx`, when `mode === "lumpsum"`, change `MIN_YEARS = 3` and update the hint text.

---

### Priority 5 — Clean Up Dead Code in `fqs_engine.py`

`fqs_engine.py` contains ~453 lines of commented-out code (the old engine version) followed by the actual active engine. This is confusing and makes the file unnecessarily long.

**Fix:** Delete lines 1–453 of `fqs_engine.py` (the commented block). The active code starts at line 455.

---

## Task 5 — Inconsistencies, Bugs & Technical Debt

### Bug 1: Lumpsum Home Screen Empty on Fresh Deployment ⚠️ CRITICAL

**Symptom:** All fund cards show "—" for Blended WM, FQS, and Stability.
**Cause:** `fund_lumpsum_analysis` collection is empty; no batch job to populate it.
**Severity:** This means the primary value proposition of the Lumpsum mode (ranked fund recommendations) does not work until each fund is individually viewed.

---

### Bug 2: Stability Metric Inconsistency Between Modes

**SIP screen:** Shows 0–100 score (e.g., "83.5") labeled "Stability"
**Lumpsum screen:** Shows raw ratio (e.g., "1.44") labeled "Stability ×"

A user switching between modes will see completely different numbers under the same column. There is no UI indication that these are different metrics. The card layout treats them identically.

---

### Bug 3: Lumpsum Stability Check Is Horizon-Agnostic

If a user selects 3Y lumpsum tenure (resolved to 3Y horizon), the stability_check displayed is still Median_7Y/Median_5Y — metrics from horizons entirely outside the user's selected window. This is misleading.

---

### Bug 4: Tenure < 3Y Silently Resolves to 3Y for Lumpsum

If a user enters 1Y or 2Y lumpsum tenure, the backend silently uses 3Y horizon data. The UI shows a banner "Showing 3Y data for your 1Y input" but the user can still proceed to the results screen with the wrong understanding.

**Fix:** Enforce `MIN_YEARS = 3` for lumpsum tenure at the UI layer.

---

### Technical Debt 1: 453 Lines of Dead Code in `fqs_engine.py`

The entire old SIP engine is commented out at the top of the file. It is never executed and creates significant confusion about which code is active. This should be deleted or moved to version control history only.

---

### Technical Debt 2: Lumpsum Consistency Weight is 0.00 for Equity

```python
EQUITY_WEIGHTS = {
    ...
    "consistency": 0.00,  # computed but never contributes to FQS
    ...
}
```

`consistency_score` is computed in `compute_horizon_stats` but then multiplied by 0.00 in the FQS formula for equity categories. This is wasted computation and should either be removed or documented as intentionally excluded.

---

### Technical Debt 3: Two Collections for Essentially the Same Underlying Funds

- `fund_nav_meta` — SIP analysis data for all indexed funds
- `fund_lumpsum_analysis` — Lumpsum analysis data, sparsely populated

Since these share the same `scheme_code` and both come from the same AMFI NAV data, a unified collection (or a lumpsum subdocument inside `fund_nav_meta`) would simplify queries and ensure complete data coverage.

---

## Summary Table

| Aspect | SIP | Lumpsum | Gap |
|--------|-----|---------|-----|
| WM calculation | XIRR-weighted cash flows | Simple nav ratio | Fundamental difference |
| Horizons | 1–20Y | 3Y, 5Y, 7Y | Lumpsum limited |
| FQS equity weights | 0.30/0.20/0.20/0.15/0.10/0.05 | 0.35/0.25/0.20/0.00/0.10/0.10 | Different by design |
| Stability metric | 0–100 per-horizon score | 7Y/5Y median ratio | Incompatible |
| Alpha Trend | Yes | No | Missing in lumpsum |
| Stress Score | Yes | No | Missing in lumpsum |
| Batch processing | Nightly `fqs_refresh.py` | **None** | Critical gap |
| Data availability | All indexed funds | Only manually viewed funds | Critical gap |
| Headline FQS | Longest single horizon | Weighted 3Y/5Y/7Y blend | Different strategy |
| Cycle Robustness | User-years-dependent | Fixed 3/5/7 weights | Different formula |
| Min WM benchmark | CATEGORY_TABLE anchors (interpolated) | `(1+xirr)^H` compound | Different formula |
