/**
 * Indian number formatting — the single shared implementation.
 *
 * Replaces four divergent formatters that had drifted apart:
 *   app/fund/[code].tsx    fmtINR()      "1.25Cr" / "45.50L"  (no ₹ on those branches)
 *   app/proposal/[id].tsx  rupees()      "₹1,25,00,000"       (no compaction)
 *   app/proposal/[id].tsx  inline toLocaleString x3
 *   backend/proposal_xlsx  f"₹{v:,.0f}"  "₹1,000,000"         ← Western grouping
 *
 * Indian grouping is 2-2-3 from the right, not 3-3-3:
 *   1,000 · 10,000 · 1,00,000 · 10,00,000 · 1,00,00,000 · 10,00,00,000
 *
 * `Intl.NumberFormat("en-IN")` implements this correctly on Hermes/JSC and in
 * browsers, so it is used directly rather than hand-rolling the grouping.
 */

const IN_GROUP = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
const IN_GROUP_2DP = new Intl.NumberFormat("en-IN", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const CRORE = 1e7;
const LAKH = 1e5;

/** Digits only, Indian grouped: 1234567 -> "12,34,567". */
export function formatIndianNumber(value: number | null | undefined): string {
  if (value == null || !isFinite(value)) return "—";
  return IN_GROUP.format(Math.round(value));
}

/** Full rupee amount, no compaction: 12500000 -> "₹1,25,00,000". */
export function formatRupees(value: number | null | undefined): string {
  if (value == null || !isFinite(value)) return "—";
  const sign = value < 0 ? "-" : "";
  return `${sign}₹${IN_GROUP.format(Math.abs(Math.round(value)))}`;
}

/**
 * Compact rupees with an Indian-scale suffix:
 *   12500000 -> "₹1.25 Cr"
 *   4550000  -> "₹45.5 Lakh"
 *   45000    -> "₹45,000"
 *
 * `short` uses "L" instead of "Lakh" for tight table cells.
 * Trailing zeros are trimmed so we get "₹45.5 Lakh", not "₹45.50 Lakh".
 */
export function formatRupeesCompact(
  value: number | null | undefined,
  opts: { short?: boolean; decimals?: number } = {},
): string {
  if (value == null || !isFinite(value)) return "—";
  const { short = false, decimals = 2 } = opts;
  const sign = value < 0 ? "-" : "";
  const abs = Math.abs(value);

  const trim = (n: number) => String(+n.toFixed(decimals));

  if (abs >= CRORE) return `${sign}₹${trim(abs / CRORE)} ${short ? "Cr" : "Cr"}`;
  if (abs >= LAKH) return `${sign}₹${trim(abs / LAKH)} ${short ? "L" : "Lakh"}`;
  return `${sign}₹${IN_GROUP.format(Math.round(abs))}`;
}

/** Two-decimal Indian grouping, for NAV-like values: 1234.5 -> "1,234.50". */
export function formatIndianDecimal(value: number | null | undefined): string {
  if (value == null || !isFinite(value)) return "—";
  return IN_GROUP_2DP.format(value);
}

/** Percentage from a FRACTION: 0.1425 -> "14.3%". */
export function formatPercent(
  value: number | null | undefined,
  decimals = 1,
): string {
  if (value == null || !isFinite(value)) return "—";
  return `${(value * 100).toFixed(decimals)}%`;
}

/** Wealth multiple: 2.3141 -> "2.31x". */
export function formatMultiple(
  value: number | null | undefined,
  decimals = 2,
): string {
  if (value == null || !isFinite(value)) return "—";
  return `${value.toFixed(decimals)}x`;
}

// A refresh is expected roughly nightly — anything older than this many
// hours is flagged (amber, not just grey) rather than stated as if it were
// current. Catches exactly the failure mode from a redeploy silently
// killing the background refresh mid-run: instead of a curl to notice, the
// stale age is right on screen every time you open the app. Shared between
// Home's SortStrip and the Data Refresh screen.
export const STALE_DATA_HOURS = 36;

/** ISO timestamp -> "Data refreshed Xh/Xd ago", flagged `stale` past
 *  STALE_DATA_HOURS. Returns null when there's no timestamp to show. */
export function formatDataAge(
  iso: string | null | undefined,
  label = "Data refreshed",
): { text: string; stale: boolean } | null {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return null;
  const hours = (Date.now() - then) / 3_600_000;
  const stale = hours > STALE_DATA_HOURS;
  let text: string;
  if (hours < 1) text = `${label} <1h ago`;
  else if (hours < 24) text = `${label} ${Math.round(hours)}h ago`;
  else text = `${label} ${Math.round(hours / 24)}d ago`;
  return { text, stale };
}

/** "1 fund", "2 funds", "0 funds" — count with correct singular/plural. */
export function countLabel(n: number, singular: string, plural = `${singular}s`): string {
  return `${n} ${n === 1 ? singular : plural}`;
}
