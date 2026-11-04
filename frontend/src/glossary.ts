// Plain-English explanations for technical terms surfaced in the fund detail
// screen. Used by <InfoHint term="..." /> to show a one-line tooltip on tap.

export const GLOSSARY: Record<string, string> = {
  fqs:
    "Fund Quality Score (0–100): a weighted composite of success rate, risk-adjusted return, " +
    "median wealth multiple, downside, and tail-loss across every rolling SIP window.",
  wm:
    "Wealth Multiple (WM): final value ÷ total invested. WM 2× means a ₹1 SIP became ₹2.",
  min_wm_required:
    "The minimum WM a fund of this category should produce at this horizon. " +
    "Derived from a category-specific target XIRR (e.g. 17% for Small Cap).",
  n_windows:
    "Total number of overlapping SIP windows of this length in the fund's history. " +
    "More windows = a more reliable statistic.",
  median_wm:
    "Half of all SIP windows ended at this WM or higher. The 'typical' outcome.",
  mean_wm:
    "Arithmetic average WM across all windows. A few extreme outcomes can skew this; " +
    "median is usually a more honest 'typical' number.",
  std_wm:
    "Standard deviation of WM across windows — how spread out the outcomes are.",
  cv:
    "Coefficient of Variation (σ / mean): relative volatility. Lower = more consistent outcomes.",
  p10:
    "P10 — only 10% of SIP windows ended below this WM. A practical 'downside' number.",
  p25: "P25 — the bottom quartile boundary of WM outcomes.",
  p75: "P75 — the top quartile boundary of WM outcomes.",
  p90: "P90 — only 10% of windows did BETTER than this. A practical 'upside'.",
  es10:
    "Expected Shortfall (ES10): the average WM of the worst 10% of windows. " +
    "Your realistic downside if things go badly.",
  failure_pct:
    "Percentage of SIP windows that ended BELOW the category benchmark (Min WM Required).",
  success_pct:
    "Percentage of SIP windows that MET or EXCEEDED the category benchmark.",
  fail_score: "Failure % expressed on a 0–100 scale (raw failure × 100).",
  downside_severity:
    "How far the bottom-quartile (P25) outcome falls short of the benchmark. " +
    "Higher = bigger downside if a SIP underperforms.",
  cv_score: "CV converted to a 0–100 scale (min(100, CV × 100)).",
  risk_score:
    "Composite risk: 40% Failure + 25% Downside Severity + 35% CV Score.",
  adj_risk_score: "100 minus Risk Score — higher is better.",
  blended_wm:
    "0.6 × Median WM + 0.4 × P10 WM. A 'realistic' WM weighted toward the typical outcome but " +
    "pulled down by the downside tail.",
  blended_wm_score:
    "Blended WM ÷ Min WM Required, scaled to 100. Capped at the category's score cap.",
  downside_score:
    "P10 WM ÷ Min WM Required, scaled to 100. Capped at the category's score cap.",
  consistency_score: "Fraction of windows strictly above the benchmark, ×100. Equity weights = 0.",
  es_score: "ES10 ÷ Min WM Required, scaled to 0–100. Captures tail outcome quality.",
  cycle_robustness:
    "Weighted success% across 3Y / 5Y / 10Y / 15Y horizons. Measures how reliably a fund delivers " +
    "across market cycles.",
  stability_check:
    "Median WM (10Y) ÷ Median WM (5Y). Healthy compounding sits in 1.6–1.9. >2.2 may indicate " +
    "cycle-dependent outcomes.",
};

export function glossaryEntry(key: string): string | undefined {
  return GLOSSARY[key.toLowerCase()];
}
