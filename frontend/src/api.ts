// API client — strict types matching the new analyst-grade payload.

const RAW_BASE = process.env.EXPO_PUBLIC_BACKEND_URL;
const BASE = RAW_BASE ? RAW_BASE.replace(/\/+$/, "").replace(/\/api$/, "") : "";

if (!BASE) {
  console.warn(
    "[api] EXPO_PUBLIC_BACKEND_URL is not set. " +
      "Edit frontend/.env before running expo start or eas build.",
  );
}

function buildUrl(path: string): string {
  if (!BASE) {
    throw new Error(
      "EXPO_PUBLIC_BACKEND_URL is not configured. Edit frontend/.env.",
    );
  }
  return `${BASE}/api${path}`;
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(buildUrl(path));
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
  return res.json();
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(buildUrl(path), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
  return res.json();
}

export interface RollingWindow {
  start_date: string;
  end_date: string;
  total_units: number;
  end_nav: number;
  end_value: number;
  total_invested: number;
  wealth_multiple: number;
  good: boolean;
}

export interface HorizonStats {
  horizon: number;
  n_windows: number;
  min_wm_required: number;
  cap: number;
  min_wm: number;
  max_wm: number;
  mean_wm: number;
  median_wm: number;
  p10_wm: number;
  p25_wm: number;
  p75_wm: number;
  p90_wm: number;
  std_wm: number;
  cv: number;
  failure_pct: number;
  success_pct: number;
  fail_score: number;
  downside_severity: number;
  cv_score: number;
  risk_score: number;
  adj_risk_score: number;
  blended_wm: number;
  blended_wm_score: number;
  downside_score: number;
  consistency_score: number;
  es10: number;
  es_score: number;
  fqs: number;
  windows: RollingWindow[];
}

export interface FundAnalysis {
  scheme_code: number;
  scheme_name: string;
  fund_house?: string;
  scheme_type?: string;
  scheme_category?: string;
  category: string;
  monthly_points: number;
  first_month: string | null;
  last_month: string | null;
  start_date_applied: string | null;
  headline_xirr: number | null;
  headline_fqs: number | null;
  headline_tier: string | null;
  headline_horizon: number | null;
  cycle_robustness: number | null;
  stability_check: number | null;
  horizons: HorizonStats[];
  available_horizons: number[];
  predecessor_available: boolean;
  predecessors_included: boolean;
}

export interface FundMeta {
  scheme_code: number;
  scheme_name: string;
  fund_house: string;
  scheme_type: string;
  scheme_category: string;
  category: string;
  earliest_nav_date: string | null;
  latest_nav_date: string | null;
  nav_points: number;
}

export interface RecentFund {
  scheme_code: number;
  scheme_name: string;
  last_seen: string;
  category?: string;
  headline_fqs?: number | null;
  headline_tier?: string | null;
  headline_horizon?: number | null;
  headline_xirr?: number | null;
  available_horizons?: number[];
}

export const api = {
  search: (q: string) =>
    get<{ query: string; results: { scheme_code: number; scheme_name: string }[] }>(
      `/funds/search?q=${encodeURIComponent(q)}`,
    ),
  recent: () => get<{ count: number; results: RecentFund[] }>("/funds/recent"),
  meta: (code: number) => get<FundMeta>(`/funds/${code}/meta`),
  analyze: (
    code: number,
    opts?: { startDate?: string; refresh?: boolean; includePredecessors?: boolean },
  ) => {
    const q = new URLSearchParams();
    if (opts?.startDate) q.set("start_date", opts.startDate);
    if (opts?.refresh) q.set("refresh", "1");
    if (opts?.includePredecessors) q.set("include_predecessors", "1");
    const qs = q.toString();
    return get<FundAnalysis>(`/funds/${code}/analyze${qs ? "?" + qs : ""}`);
  },
  refreshStatus: () =>
    get<{
      last_run?: string | null;
      schemes_refreshed?: number;
      total_attempted?: number;
      duration_seconds?: number;
    }>("/refresh/status"),
  refreshRun: () => post<{ schemes_refreshed: number; duration_seconds: number }>("/refresh/run"),
};
