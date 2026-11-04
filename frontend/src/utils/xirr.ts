/**
 * Wealth Multiple → Equivalent XIRR conversion.
 *
 * Formulas extracted from ArthVruksh MF Proposal_Priya Ravi.xlsx:
 *
 *  SIP sheet:     =RATE(years*12, -1, 0, WM*years*12) * 12
 *  Lumpsum sheet: =RATE(years, , -1, WM, 0)  →  WM^(1/years) - 1
 */

/**
 * Lumpsum: exact closed-form.
 * RATE(n, , -1, WM) = WM^(1/n) - 1
 */
export function lumpsumWmToXirr(wm: number, horizonYears: number): number | null {
  if (!wm || wm <= 0 || horizonYears <= 0) return null;
  return Math.pow(wm, 1 / horizonYears) - 1;
}

/**
 * SIP: numerical solve of Excel RATE(n*12, -1, 0, WM*n*12) * 12.
 *
 * Solves the annuity equation:  ((1+r)^n - 1) / r = WM * n
 * where n = horizonYears * 12 (monthly periods), r = monthly rate.
 * Returns annual XIRR = r * 12.
 *
 * Newton-Raphson with 100 iterations; converges in <10 for typical WM values.
 */
export function sipWmToXirr(wm: number, horizonYears: number): number | null {
  if (!wm || wm <= 0 || horizonYears <= 0) return null;
  const n = horizonYears * 12;
  const fv = wm * n; // target future value per unit invested/month

  // f(r)  = ((1+r)^n - 1) / r - fv = 0
  // f'(r) = [n*(1+r)^(n-1)*r - ((1+r)^n - 1)] / r^2
  let r = 0.01; // start at 1% monthly (12% annual)
  for (let i = 0; i < 200; i++) {
    const powered = Math.pow(1 + r, n);
    const f  = (powered - 1) / r - fv;
    const df = (n * Math.pow(1 + r, n - 1) * r - (powered - 1)) / (r * r);
    if (Math.abs(df) < 1e-15) break;
    const delta = f / df;
    r -= delta;
    if (r <= -1) r = 1e-6; // guard against negative
    if (Math.abs(delta) < 1e-12) break;
  }
  const annual = r * 12;
  // Sanity check: reject nonsensical results
  if (!isFinite(annual) || annual < -0.5 || annual > 5) return null;
  return annual;
}

/** Format XIRR as a percentage string, e.g. "14.2%" */
export function fmtXirr(xirr: number | null): string {
  if (xirr == null || !isFinite(xirr)) return "—";
  return `${(xirr * 100).toFixed(1)}%`;
}

/**
 * SIP: annual XIRR → minimum Wealth Multiple.
 * Inverse of sipWmToXirr.
 *
 * WM = ((1+m)^N − 1) / (N · m)   where m = xirr/12, N = horizonYears×12
 */
export function sipXirrToMinWm(xirr: number, horizonYears: number): number {
  const m = xirr / 12;
  const N = horizonYears * 12;
  if (Math.abs(m) < 1e-9) return 1.0;
  return ((1 + m) ** N - 1) / (N * m);
}

/**
 * Lumpsum: annual XIRR → minimum Wealth Multiple.
 * Exact closed-form: WM = (1 + xirr)^years
 */
export function lumpsumXirrToMinWm(xirr: number, horizonYears: number): number {
  return (1 + xirr) ** horizonYears;
}
