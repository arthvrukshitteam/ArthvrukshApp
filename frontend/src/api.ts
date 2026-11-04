// API client — strict types matching the new analyst-grade payload.

// The tenure-picker UI accepts any positive number of years, but fund
// calculations remain capped at 15Y — clamp here, at the network boundary,
// so the backend's calculation logic never has to change.
export const MAX_CALC_YEARS = 50;

// Every proposal is recommended by the AMFI-registered distributor entity.
// Fixed (not advisor-entered) per compliance; mirrors the backend default.
export const RECOMMENDED_BY = "ArthVruksh MFDGuru LLP";

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

// Short-lived response cache + in-flight de-duplication for read-only GETs
// whose data only changes with the nightly batch (the universe lists, fund
// browse pages). Flipping a Home toggle off and back on, or returning to a
// tab, used to re-download the whole ranked universe every time.
const _getCache = new Map<string, { at: number; value: unknown }>();
const _inFlight = new Map<string, Promise<unknown>>();
const GET_CACHE_MAX = 40;

async function getCached<T>(path: string, ttlMs: number): Promise<T> {
  const hit = _getCache.get(path);
  if (hit && Date.now() - hit.at < ttlMs) return hit.value as T;
  const pending = _inFlight.get(path);
  if (pending) return pending as Promise<T>;
  const p = get<T>(path)
    .then((value) => {
      if (_getCache.size >= GET_CACHE_MAX) {
        const oldest = _getCache.keys().next().value;
        if (oldest !== undefined) _getCache.delete(oldest);
      }
      _getCache.set(path, { at: Date.now(), value });
      return value;
    })
    .finally(() => _inFlight.delete(path));
  _inFlight.set(path, p);
  return p;
}

/** Drop cached GETs (pull-to-refresh must always hit the network). */
export function invalidateApiCache(prefix?: string) {
  if (!prefix) { _getCache.clear(); return; }
  for (const k of [..._getCache.keys()]) if (k.startsWith(prefix)) _getCache.delete(k);
}

const UNIVERSE_TTL_MS = 3 * 60 * 1000;

async function post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(buildUrl(path), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
  return res.json();
}

async function put<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(buildUrl(path), {
    method: "PUT",
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
  alpha_trend: number | null;
  stress_score: number | null;
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
  horizons: HorizonStats[];
  available_horizons: number[];
  /** True when this fund is wound up / merged and has NO verified successor in MERGER_MAP. */
  wound_up: boolean;
  /** ISO date string of the last NAV if wound up, else null. */
  wound_up_date: string | null;
  /** Successor scheme code if histories were stitched, else null. */
  merged_with: number | null;
  /** Older scheme code(s) whose history was verified and prepended (e.g. an
   *  AMC acquisition/rename that changed the scheme code), oldest first.
   *  Empty when this scheme's own code already covers its full history. */
  predecessor_used?: number[] | null;
  /** Every verified predecessor found on the backward walk, whether or not it
   *  was actually merged into this analysis (depends on the caller's
   *  include_predecessor choice). Non-empty means the "old vs new management"
   *  toggle is meaningful for this fund. */
  predecessor_available?: { scheme_code: number; first_date: string; last_date: string; points: number }[] | null;
  /** Echoes the include_predecessor query param this analysis was computed
   *  with — lets the UI show which side of the toggle is currently active. */
  include_predecessor_applied?: boolean;
  /** Honest disclosure when the fund's true inception predates the earliest
   *  available NAV (e.g. AMC transfer with no pre-transfer data on the feed). */
  history_note?: string | null;
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

// Per-horizon FQS entry stored by _save_analysis in server.py.
// Keys are stringified horizon numbers e.g. "5", "10".
export interface FqsByHorizonEntry {
  fqs: number;
  tier: string;
}

// ---------------------------------------------------------------------------
// Unified Universe contract — /home/universe (SIP) and /lumpsum/universe
// return the SAME shape. Only the engine behind the numbers differs
// (fqs_engine rolling-SIP windows vs lumpsum_engine single-investment
// windows). See _universe_metrics() in backend/server.py.
// ---------------------------------------------------------------------------

/** The per-fund metric block, identical in both modes. */
export interface UniverseMetrics {
  /** Blended Wealth Multiple at `horizon`. */
  blended_wm: number | null;
  /** FQS at `horizon`. */
  fqs: number | null;
  fqs_tier: string | null;
  /** Success rate (0–1) at `horizon`. */
  success_pct: number | null;
  /** Number of rolling windows at `horizon`. */
  n_windows: number | null;
  /** Coefficient of Variation at `horizon`. */
  cv: number | null;
  /** P10 Wealth Multiple at `horizon`. */
  p10_wm: number | null;
  /** The rolling period these metrics were read at. */
  horizon: number;
  /** False when the fund had no usable data at `horizon` (all metrics null). */
  fqs_exact: boolean;
}

export interface EligibleFund extends UniverseMetrics {
  scheme_code: number;
  scheme_name: string;
  category?: string;
  history_years?: number;

  // Generic headline score (can be 15Y — do not use as selected-year score).
  headline_fqs?: number | null;
  headline_tier?: string | null;
  headline_horizon?: number | null;

  last_fqs_refresh?: string | null;
  fqs_by_horizon?: Record<string, FqsByHorizonEntry> | null;
  /** Two-level UI filter */
  broad_category?: string;
  amfi_sub?: string;

  /** Set only when separate_management=true AND this fund actually has a
   *  verified continuity break driving its numbers (merger/successor change,
   *  or a manually-curated management change) — the date that break's numbers
   *  start from. Null otherwise, even if the fund HAS a break on file but the
   *  toggle is off. Powers the "since Apr 2018" tag on the fund card. */
  continuity_reset_date?: string | null;
  /** Short label for why (e.g. "Merger / successor scheme change"). */
  continuity_reason?: string | null;
  /** Only with separate_management=true: every recorded management break
   *  for this fund (AMC change, merger boundary, curated event), newest
   *  first. Two or more → the card offers a starting-period choice. */
  amc_breaks?: AmcBreak[] | null;
}

/** One management-change boundary — see GET /funds/{code}/amc-periods. */
export interface AmcBreak {
  /** YYYY-MM-DD the new management's period starts. */
  date: string;
  reason: string;
  from?: string | null;
  to?: string | null;
  source: "lineage" | "curated" | "manual" | "merger";
}

export interface AmcPeriods {
  scheme_code: number;
  current_amc: string | null;
  first_nav_date: string | null;
  /** Newest first. */
  breaks: AmcBreak[];
  /** The break the nightly "Separate Management Periods" numbers use. */
  latest_break: string | null;
  caveat: string | null;
}

/**
 * A fund that doesn't have enough history for the selected tenure, but whose
 * Blended WM XIRR at its best available horizon meets or beats the benchmark.
 * Shown above normal funds in the All view with a disclaimer.
 *
 * Its `UniverseMetrics` are read at `best_horizon`, not at the user's tenure —
 * `horizon` echoes `best_horizon`.
 */
export interface BonusFund extends UniverseMetrics {
  scheme_code: number;
  scheme_name: string;
  category?: string;
  broad_category?: string;
  amfi_sub?: string;
  history_years?: number;
  is_bonus: true;
  /** The horizon (years) at which this fund was evaluated */
  best_horizon: number;
  /** Pre-computed XIRR at best_horizon */
  blended_xirr: number | null;
  /** Pre-computed equivalent XIRR of p10_wm at best_horizon */
  p10_xirr: number | null;
  /** See EligibleFund.continuity_reset_date. */
  continuity_reset_date?: string | null;
  continuity_reason?: string | null;
  /** See EligibleFund.amc_breaks. */
  amc_breaks?: AmcBreak[] | null;
}

export interface UniverseResponse {
  mode: "sip" | "lumpsum";
  /** True when this response was computed with every fund's start date
   *  forced to 2010-01-01 (the Home screen's "Since 2010" toggle) — for
   *  funds old enough to have existed by then. A fund that started later
   *  simply keeps its normal since-inception numbers instead (it's still
   *  shown, not excluded) — see backend/server.py home_universe. */
  since_2010?: boolean;
  /** True when this response was computed with every fund that has a
   *  verified continuity break (merger/successor change, or a
   *  manually-curated management change) scored from that break date
   *  forward instead of its full stitched history (the Home screen's
   *  "Separate Management Periods" toggle). Funds with no known break keep
   *  whatever since_2010 resolved to — see backend/server.py
   *  _select_horizon_map / _continuity_applies. */
  separate_management?: boolean;
  /** The tenure the user asked for. */
  tenure: number;
  /** The horizon actually used (the backend may cap it). */
  resolved_horizon: number;
  required_history_years: number;
  total_funds_indexed: number;
  eligible_fund_count: number;
  eligible_funds: EligibleFund[];
  /** Additional Recommendations: funds with 5Y–tenure history, XIRR ≥ 15%. */
  bonus_funds?: BonusFund[];
  /** Minimum XIRR threshold (0.15) used for Additional Recommendations. */
  benchmark_xirr?: number | null;
  index_last_built?: string | null;
  /** Last batch score refresh — FQS refresh in SIP, lumpsum refresh in Lumpsum. */
  scores_last_refreshed?: string | null;
}

export interface FullRefreshStatus {
  nav: {
    last_run?: string | null;
    schemes_refreshed?: number;
    running?: boolean;
  };
  fqs: {
    last_run?: string | null;
    refreshed?: number;
    total?: number;
    running?: boolean;
  };
}

/** Shape shared by GET /fqs/refresh/status and GET /lumpsum/refresh/status —
 *  see server.py's fqs_refresh_status / lumpsum_refresh_status. */
export interface BatchRefreshStatus {
  running: boolean;
  last_run: {
    duration_seconds?: number;
    failed?: number;
    finished_at?: string | null;
    refreshed?: number;
    skipped?: number;
    total_in_index?: number;
    /** SIP-only. */
    refreshed_2010?: number;
    refreshed_continuity?: number;
  } | null;
}

// ---------------------------------------------------------------------------
// Lumpsum types
// ---------------------------------------------------------------------------

export interface LumpsumWindow {
  start_date: string;
  end_date: string;
  starting_nav: number;
  ending_nav: number;
  total_invested: number;
  final_value: number;
  wealth_multiple: number;
  good: boolean;
}

export interface LumpsumHorizonStats {
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
  std_dev: number;
  cv: number;
  failure_pct: number;
  success_pct: number;
  failure_score: number;
  consistency_score: number;
  downside_sev_score: number;
  cv_score: number;
  risk_score: number;
  adj_risk_score: number;
  blended_wm: number;
  blended_wm_score: number;
  downside_score: number;
  es10: number;
  es_score: number;
  fqs: number;
  fqs_rating: string;
  alpha_trend: number | null;
  stress_score: number | null;
  windows: LumpsumWindow[];
}

export interface LumpsumFundAnalysis {
  scheme_code: number;
  scheme_name: string;
  category: string;
  is_equity: boolean;
  monthly_points: number;
  start_date_applied: string | null;
  available_horizons: number[];
  horizons: LumpsumHorizonStats[];
  headline_fqs: number | null;
  headline_horizon: number;
  headline_fqs_rating: string | null;
  cycle_robustness: number | null;
  cycle_variant: string | null;
  min_xirr_req: number;
  cap: number;
  wound_up: boolean;
  wound_up_date: string | null;
  merged_with: number | null;
  predecessor_used?: number[] | null;
  predecessor_available?: { scheme_code: number; first_date: string; last_date: string; points: number }[] | null;
  include_predecessor_applied?: boolean;
  history_note?: string | null;
}

// ---------------------------------------------------------------------------
// Portfolio Proposal types
// ---------------------------------------------------------------------------

export interface ProposalFundRow {
  scheme_code: number;
  scheme_name: string;
  category: string;
  allocation_pct: number;
  fqs: number | null;
  success_pct: number | null;
  median_wm: number | null;
  xirr_median: number | null;
  p10_wm: number | null;
  xirr_p10: number | null;
  available: boolean;
  /** The rolling period ALL the figures above are measured at. Equals the
   *  proposal horizon for a full-history fund; shorter (e.g. 7 in a 10Y
   *  proposal) for an Additional Recommendation fund. */
  metrics_horizon?: number | null;
  /** True when metrics_horizon < the proposal horizon — render this row with
   *  an asterisk and the "shown at NY" note. */
  extrapolated?: boolean;
  /** Wealth multiples AT the proposal horizon (the shorter period's XIRR
   *  compounded out). Same as median_wm/p10_wm when not extrapolated. */
  proj_wm_median?: number | null;
  proj_wm_p10?: number | null;
  /** Coefficient of Variation at metrics_horizon — same figure the fund-detail
   *  screen's CONSISTENCY section shows (key "cv"). */
  cv?: number | null;
  /** Category minimum WM benchmark at metrics_horizon. */
  min_wm_required?: number | null;
  /** min_wm_required converted to an annualised XIRR — "what XIRR would just
   *  meet the benchmark". */
  benchmark_xirr?: number | null;
  /** How many rolling windows median_wm/p10_wm/success_pct were computed
   *  from at metrics_horizon — a data-sufficiency signal alongside them. */
  n_windows?: number | null;
}

export interface ProposalAggregate {
  horizon: number;
  capital_invested: number;
  expected_wealth_median: number;
  expected_wealth_p10: number;
  blended_wm_median: number;
  blended_wm_p10: number;
  blended_xirr_median: number | null;
  blended_xirr_p10: number | null;
  excluded_funds: string[];
  /** scheme_names shown at a shorter rolling period than the horizon.
   *  Optional — proposals built before this feature won't carry it. */
  extrapolated_funds?: string[];
  /** Allocation-weighted average CV across included funds. */
  blended_cv?: number | null;
  /** Allocation-weighted average category-minimum WM across included funds. */
  benchmark_wm?: number | null;
  /** benchmark_wm converted to an annualised XIRR at `horizon`. */
  benchmark_xirr?: number | null;
}

export interface ProposalPlan {
  mode: "sip" | "lumpsum";
  investment_amount: number;
  horizon: number;
  funds: ProposalFundRow[];
  aggregate: ProposalAggregate;
}

export interface SwpMonthRow {
  month: number;
  opening_principal: number;
  withdrawal: number;
  principal_after_withdrawal: number;
  growth: number;
  closing_balance: number;
}

export interface ProposalSwp {
  corpus: number;
  annual_return: number;
  inflation: number;
  monthly_withdrawal: number;
  withdrawal_with_inflation: number;
  years: number;
  ending_balance: number;
  corpus_auto_derived: boolean;
  annual_return_auto_derived: boolean;
  rows?: SwpMonthRow[];
}

export interface ProposalDoc {
  proposal_id: string;
  client_name: string | null;
  client_phone: string | null;
  client_pan: string | null;
  advisor_name: string | null;
  proposal_date: string;
  mode: "sip" | "lumpsum";
  horizon: number;
  /** Echoes the Home screen's toggle state at the moment these funds were
   *  selected — see ProposalRequest. */
  since_2010?: boolean;
  separate_management?: boolean;
  /** Per-fund scoring basis, keyed by scheme code — only funds that were
   *  added with a start-date filter appear. Echoed back on recalculation. */
  fund_bases?: Record<string, { start_date: string | null; include_predecessor: boolean; explicit_basis?: boolean }>;
  plan: ProposalPlan;
  swp: ProposalSwp | null; // optional — null when no withdrawal plan was added
  warnings: string[];
  created_at: string;
}

export interface ProposalFundInput {
  scheme_code: number;
  allocation_pct: number;
  /** Score this fund from this date (YYYY-MM-DD) rather than since inception
   *  — the start-date filter / AMC period that was on screen when it was added. */
  start_date?: string | null;
  /** False = own-code NAVs only ("new management only"). Default true. */
  include_predecessor?: boolean;
  /** Use the basis above as-is, ignoring the Home-wide 2010/Management toggles. */
  explicit_basis?: boolean;
}

export interface ProposalSwpInput {
  corpus?: number | null;
  annual_return?: number | null;
  inflation: number;
  monthly_withdrawal: number;
  years: number;
}

export interface ProposalRequest {
  /** When set, the backend recalculates this same proposal in place (used by
   *  the Under Review screen) instead of creating a new one. */
  proposal_id?: string;
  client_name?: string;
  client_phone?: string;
  client_pan?: string;
  advisor_name?: string;
  proposal_date?: string;
  mode: "sip" | "lumpsum";
  horizon: number;
  /** Home screen's "Since 2010" / "Separate Management Periods" toggle
   *  state at the moment these funds were selected — so the proposal uses
   *  the SAME numbers Home showed, not each fund's plain since-inception
   *  data. See backend/proposal_routes.py's ProposalRequest. */
  since_2010?: boolean;
  separate_management?: boolean;
  investment_amount: number;
  funds: ProposalFundInput[];
  swp?: ProposalSwpInput; // optional — omit for a proposal with no withdrawal plan
}

// Rolling Analysis types — the "Rolling" tab.
//
// One fund, one overall date range, one rolling-window length. Every
// monthly-sliding window inside the range is grouped into "buckets" — by
// default one per calendar year, or advisor-defined custom periods (e.g.
// 2010-2017 / 2018-2026) for a wider, statistically sturdier sample per
// row — and the same P10/Median/Blended WM/Success %/CV/FQS stats are
// computed independently per bucket. A bucket with no windows is still
// returned (n_windows: 0, other fields null) rather than dropped. See
// backend/rolling_analysis_routes.py.
// ---------------------------------------------------------------------------

export interface RollingBucketRequest {
  start_date: string;
  end_date: string;
  label?: string;
}

export interface RollingPeriodRow {
  label: string;         // e.g. "2010–2017", or the advisor's custom label
  start_date: string;
  end_date: string;
  n_windows: number;
  p10_wm: number | null;
  median_wm: number | null;
  blended_wm: number | null;
  success_pct: number | null;
  cv: number | null;
  min_wm_required: number | null;
  fqs: number | null;
}

export interface RollingAnalysisResponse {
  analysis_id: string;
  scheme_code: number;
  scheme_name: string;
  category: string;
  mode: "sip" | "lumpsum";
  window_years: number;
  start_date: string;
  end_date: string;
  custom_buckets: boolean;
  periods: RollingPeriodRow[];
  /** Same stats computed over every window in the range at once — the
   *  baseline each period row is implicitly being compared against. */
  overall: RollingPeriodRow | null;
  /** Plain-English bullets comparing each period to the previous one —
   *  server-generated so the screen and the exported PDF always agree. */
  key_points: string[];
}

export interface RollingAnalysisRequest {
  scheme_code: number;
  start_date: string;
  end_date: string;
  window_years: number;
  mode: "sip" | "lumpsum";
  /** Omit for the default: one bucket per calendar year. */
  buckets?: RollingBucketRequest[];
}

// Multi-fund consistency ranking — the Rolling tab's "Multi-Fund Ranking"
// mode. Same period-bucketed analysis as single-fund Rolling Analysis, run
// across many funds at once (an auto-selected pool, hand-picked scheme
// codes, or both), then ranked by how STABLE each fund's Blended WM is
// across periods (lower cross_period_cv = more consistent) rather than
// inspected one fund at a time. See backend/rolling_analysis_routes.py.

export interface RollingRankFundRow {
  scheme_code: number;
  scheme_name: string;
  category: string;
  periods: RollingPeriodRow[];
  overall: RollingPeriodRow | null;
  periods_scored: number;
  /** stdev / mean of Blended WM across scored periods — null if fewer than
   *  2 periods had data (shown but not rankable). Lower = more consistent. */
  cross_period_cv: number | null;
  wm_spread: number | null;
  wm_min_period: string | null;
  wm_max_period: string | null;
  rank: number;
  manually_added?: boolean;
  /** Same plain-English bullets single-fund Rolling Analysis generates —
   *  computed per-fund server-side so tapping a fund on screen (or its PDF
   *  section) shows an identical breakdown to running it standalone. */
  key_points?: string[];
}

export interface RollingRankResponse {
  multi_id: string;
  start_date: string;
  end_date: string;
  window_years: number;
  mode: "sip" | "lumpsum";
  custom_buckets: boolean;
  n_periods: number;
  candidate_count: number;
  scored_count: number;
  funds: RollingRankFundRow[];
  key_points: string[];
}

export interface RollingRankRequest {
  start_date: string;
  end_date: string;
  window_years: number;
  mode: "sip" | "lumpsum";
  buckets?: RollingBucketRequest[];
  /** Auto-selected candidates (excl. Debt/Gold-Silver), sorted by NAV
   *  history depth. 0 = auto-pool disabled (hand-picked only). */
  pool_size: number;
  /** Hand-picked scheme codes, included on top of (or instead of) the pool. */
  scheme_codes?: number[];
}

/**
 * One row from GET /funds/search.
 *
 * `is_active` is the app's single dead-fund rule (no NAV for 180+ days, or
 * the scheme was merged into another) applied at the data layer — see
 * backend/fund_status.py. Search LABELS rather than hides, so an advisor
 * looking up what happened to an old fund still finds it; pass
 * `activeOnly` when the user is choosing a fund to invest in.
 */
export interface FundSearchResult {
  scheme_code: number;
  scheme_name: string;
  /** Present when the lineage resolver matched a historical identifier. */
  resolved_from?: string | null;
  formerly_known_as?: string | null;
  is_active?: boolean;
  /** e.g. "Merged into scheme 140225" / "Discontinued — no NAV since …". */
  inactive_reason?: string | null;
  /** The surviving scheme code, when this one was merged into another. */
  current_scheme_code?: number | null;
  /** Set when this row stands for several AMFI listings of the SAME scheme
   *  that were collapsed into it (plan/option spellings, or the same name
   *  repeated under many codes — "Motilal Oswal Liquid Fund" is 16). Absent
   *  when nothing was folded in. */
  variant_count?: number;
}

/** One corporate action from a fund's AMC-history timeline — see
 *  backend/lineage_store.py's Action constants. `AMC_CHANGE` is a fund-house
 *  rename/acquisition with no scheme-code change (Reliance -> Nippon India,
 *  IDFC -> Bandhan, etc); `MERGER` is a scheme-code change (the predecessor
 *  code's own entry carries `successor_code`). */
export interface LineageAction {
  type: "NAME_CHANGE" | "NAME_COSMETIC" | "NAME_TERMINOLOGY" | "AMC_CHANGE"
      | "ISIN_CHANGE" | "RECATEGORISATION" | "MERGER" | "WOUND_UP";
  effective_date?: string | null;
  from?: string | null;
  to?: string | null;
  successor_code?: number | null;
  source: "AMFI_SNAPSHOT" | "CURATED" | "CORRELATION";
  confidence?: number | null;
  sources?: string[];
}

/** GET /funds/{code}/lineage — the fund's full identity/AMC-history
 *  explanation. `identity_history.amcs` is the ordered list of AMC/fund-house
 *  values this scheme code has ever been reported under — what the fund
 *  detail screen's "AMC History" section renders. */
export interface LineageExplanation {
  query: string;
  resolution_type: "same_code_identity" | "cross_code_succession";
  resolved_to: {
    scheme_code: number; scheme_name: string; isin?: string | null;
    amc?: string | null; active?: boolean | null;
  };
  formerly_known_as?: string | null;
  reasons: string[];
  confidence?: number | null;
  sources: string[];
  identity_history: {
    names: string[]; isins: string[]; amcs: string[];
    predecessor_codes: number[]; successor_code?: number | null;
  };
  coverage: {
    observed_from?: string | null; observed_to?: string | null;
    name_history_from: string; caveat: string;
  };
  corporate_actions?: LineageAction[];
}


// ---------------------------------------------------------------------------
// Portfolio Holding Review — see backend/holding_review_routes.py
// IRR_* and CV are FRACTIONS (0.193 = 19.3 %); FQS is 0-100; WM is a multiple.
// ---------------------------------------------------------------------------

export type ReviewMetricKey = "fqs" | "wm_p10" | "wm_med" | "irr_p10" | "irr_med" | "cv";
export type ReviewMetrics = Record<ReviewMetricKey, number | null>;
export type InvestmentType = "SIP" | "Lumpsum" | "COB" | "External";
export type ReviewFundStatus = "extracted" | "verified" | "missing" | "needs_review";

export interface ReviewMetricChange {
  abs: number;
  /** Percent change — only for FQS / WM; null for XIRR & CV (percentage points). */
  pct: number | null;
  unit: "pts" | "x" | "pp";
}

export interface ReviewMatchCandidate { scheme_code: number; scheme_name: string; similarity: number }

export interface ReviewFund {
  fid: string;
  fund_name_pdf: string | null;
  scheme_code: number | null;
  scheme_name: string | null;
  category: string | null;
  rolling_year: number | null;
  investment_type: InvestmentType;
  allocation: number | null;
  match: { method: string | null; confidence: "high" | "medium" | "low" | null; candidates: ReviewMatchCandidate[] };
  previous: ReviewMetrics;
  current: (ReviewMetrics & { horizon?: number | null; n_windows?: number | null }) | null;
  current_status: "ok" | "unavailable" | "error" | null;
  current_error: string | null;
  changes: Partial<Record<ReviewMetricKey, ReviewMetricChange | null>>;
  status: ReviewFundStatus;
  issues: string[];
}

export interface ReviewObservation {
  id: string;
  text: string;
  source: "auto" | "manual";
  included?: boolean;
  basis?: Record<string, unknown> | null;
}

export interface MarketPair { previous: number | null; current: number | null; change_pct?: number | null }

export interface HoldingReview {
  review_id: string;
  status: "draft" | "finalized";
  version: number;
  parent_review_id: string | null;
  previous_review_id: string | null;
  client: { name: string | null; pan: string | null; phone: string | null; client_key?: string | null };
  previous_review_date: string | null;
  current_review_date: string | null;
  plan: { mode: "sip" | "lumpsum" | null; goal_years: number | null; amount?: number | null };
  market: { nifty: MarketPair; aum: MarketPair };
  market_summary: { nifty: MarketPair; aum: MarketPair };
  type_of_investment: string;
  quarters_completed: number | null;
  funds: ReviewFund[];
  observations: ReviewObservation[];
  review_flags: string[];
  parse_warnings: string[];
  source: { filename?: string; parse_method?: string };
  confirmed: boolean;
  data_as_of: string | null;
  current_fetched_at: string | null;
  validation: { errors: string[]; warnings: string[] };
  updated_at?: string;
}

export interface HoldingReviewSummary {
  review_id: string;
  status: "draft" | "finalized";
  client_name: string | null;
  pan_masked: string | null;
  previous_review_date: string | null;
  current_review_date: string | null;
  fund_count: number;
  version: number;
  updated_at: string | null;
}

export interface HoldingReviewPatch {
  client?: Partial<HoldingReview["client"]>;
  previous_review_date?: string | null;
  current_review_date?: string | null;
  plan?: { mode?: "sip" | "lumpsum"; goal_years?: number };
  market?: { nifty?: Partial<MarketPair>; aum?: Partial<MarketPair> };
  quarters_completed?: number;
  funds?: {
    fid?: string; scheme_code?: number | null; rolling_year?: number | null;
    investment_type?: InvestmentType; previous?: Partial<ReviewMetrics>;
  }[];
  observations?: { id?: string; text: string; included?: boolean }[];
  confirm_all?: boolean;
}

export const api = {
  /** `activeOnly` drops wound-up / merged schemes from the results — pass it
   *  from any surface where the user is PICKING a fund to invest in. Lookup
   *  surfaces (the Funds tab) leave it off so a discontinued scheme can still
   *  be found and read; those results carry `is_active: false` and an
   *  `inactive_reason` to label instead of hide.
   *
   *  Plan/option variants of one scheme are collapsed server-side by default
   *  (the same funds Home already hides), so this matches what the ranked
   *  list shows. `dedupeVariants: false` returns every raw AMFI listing. */
  search: (q: string, opts?: { activeOnly?: boolean; dedupeVariants?: boolean }) => {
    const p = new URLSearchParams({ q });
    if (opts?.activeOnly) p.set("active_only", "true");
    if (opts?.dedupeVariants === false) p.set("dedupe_variants", "false");
    return get<{ query: string; results: FundSearchResult[] }>(`/funds/search?${p.toString()}`);
  },
  recent: () => get<{ count: number; results: RecentFund[] }>("/funds/recent"),
  /** Full AMC-history timeline for the fund detail screen's "AMC History"
   *  section — see LineageExplanation. 404s (rather than an empty history)
   *  when scripts/build_lineage.py hasn't been run yet for this deployment;
   *  callers should treat that as "not available yet", not as an error to
   *  surface loudly. */
  lineage: (code: number) => get<LineageExplanation>(`/funds/${code}/lineage`),
  /** Every management period this fund has had (AMC changes, merger
   *  boundaries, curated breaks). Works whether or not the lineage build
   *  has been run — see backend continuity_floor.all_breaks. */
  amcPeriods: (code: number) => get<AmcPeriods>(`/funds/${code}/amc-periods`),
  /** First page of the fund list with no query typed — so search screens
   *  show funds the moment they open. Active, regular-plan funds only. */
  browseFunds: (opts?: { offset?: number; limit?: number }) => {
    const p = new URLSearchParams();
    if (opts?.offset) p.set("offset", String(opts.offset));
    if (opts?.limit) p.set("limit", String(opts.limit));
    const qs = p.toString();
    return getCached<{ total: number; offset: number; next_offset: number | null; results: FundSearchResult[] }>(
      `/funds/browse${qs ? "?" + qs : ""}`, UNIVERSE_TTL_MS,
    );
  },
  analyze: (
    code: number,
    opts?: { startDate?: string; refresh?: boolean; includePredecessor?: boolean; summary?: boolean },
  ) => {
    const q = new URLSearchParams();
    if (opts?.startDate) q.set("start_date", opts.startDate);
    if (opts?.refresh) q.set("refresh", "1");
    if (opts?.includePredecessor === false) q.set("include_predecessor", "0");
    // summary=1 drops the per-window rows (most of a ~0.7 MB payload) —
    // for callers that only need per-horizon stats, e.g. a Home card.
    if (opts?.summary) q.set("summary", "1");
    const qs = q.toString();
    return get<FundAnalysis>(`/funds/${code}/analyze${qs ? "?" + qs : ""}`);
  },
  /** Ranked fund universe for either mode. Both endpoints return the same
   *  `UniverseResponse` shape — only the engine behind the numbers differs.
   *  since2010: force every fund's start date to 2010-01-01 instead of its
   *  own inception (the Home screen's "2010" toggle).
   *  separateManagement: score funds with a verified continuity break from
   *  that break date forward instead of their full stitched history (the
   *  Home screen's "Separate Management Periods" toggle). */
  universe: (
    mode: "sip" | "lumpsum",
    tenure: number,
    since2010?: boolean,
    separateManagement?: boolean,
  ) => {
    const years = Math.min(tenure, MAX_CALC_YEARS);
    let suffix = since2010 ? "&since_2010=1" : "";
    if (separateManagement) suffix += "&separate_management=1";
    return getCached<UniverseResponse>(
      mode === "lumpsum"
        ? `/lumpsum/universe?tenure=${years}${suffix}`
        : `/home/universe?years=${years}${suffix}`,
      UNIVERSE_TTL_MS,
    );
  },
  fullRefreshStatus: () =>
    get<FullRefreshStatus>("/refresh/full-status"),

  // ── Manual batch refresh triggers (Data Refresh screen) ────────────────────
  // Same endpoints the nightly GitHub Actions cron hits (see
  // .github/workflows/daily-refresh.yml) — a manual escape hatch for when the
  // advisor wants a fresh run right now instead of waiting for 22:00 UTC, or
  // to confirm a refresh actually started after a deploy. Token-gated
  // whenever REFRESH_TOKEN is set on the backend (it is, in production) —
  // the app asks for it once and stores it in the device Keychain, never in
  // the JS bundle.
  fqsRefreshStatus: () => get<BatchRefreshStatus>("/fqs/refresh/status"),
  triggerFqsRefresh: (token?: string) =>
    post<{ status: "started" | "already_running" }>(
      `/fqs/refresh${token ? `?token=${encodeURIComponent(token)}` : ""}`,
    ),
  lumpsumRefreshStatus: () => get<BatchRefreshStatus>("/lumpsum/refresh/status"),
  triggerLumpsumRefresh: (token?: string) =>
    post<{ status: "started" | "already_running" }>(
      `/lumpsum/refresh${token ? `?token=${encodeURIComponent(token)}` : ""}`,
    ),

  analyzeLumpsum: (
    code: number,
    opts?: { startDate?: string; refresh?: boolean; includePredecessor?: boolean; summary?: boolean },
  ) => {
    const q = new URLSearchParams();
    if (opts?.startDate) q.set("start_date", opts.startDate);
    if (opts?.refresh) q.set("refresh", "1");
    if (opts?.includePredecessor === false) q.set("include_predecessor", "0");
    if (opts?.summary) q.set("summary", "1");
    const qs = q.toString();
    return get<LumpsumFundAnalysis>(`/funds/${code}/analyze/lumpsum${qs ? "?" + qs : ""}`);
  },

  buildProposal: (req: ProposalRequest) =>
    post<ProposalDoc>("/proposal/build", { ...req, horizon: Math.min(req.horizon, MAX_CALC_YEARS) }),
  getProposal: (id: string) =>
    get<ProposalDoc>(`/proposal/${id}`),
  /** Absolute URL — use with expo-file-system's downloadAsync, not fetch(). */
  proposalExportUrl: (id: string, format: "xlsx" | "pdf") =>
    buildUrl(`/proposal/${id}/export.${format}`),

  runRollingAnalysis: (req: RollingAnalysisRequest) =>
    post<RollingAnalysisResponse>("/rolling-analysis/run", req),
  /** Absolute URL — use with expo-file-system's downloadAsync, not fetch(),
   *  same pattern as proposalExportUrl. */
  rollingAnalysisExportUrl: (analysisId: string) =>
    buildUrl(`/rolling-analysis/${analysisId}/export.pdf`),

  runRollingRank: (req: RollingRankRequest) =>
    post<RollingRankResponse>("/rolling-analysis/rank/run", req),
  /** Score one more fund (picked via search) against an existing ranking
   *  run's dates/window/periods and merge it in — same escape hatch pattern
   *  as the single-fund add-fund flows elsewhere. Returns just that fund's row. */
  addRankedFund: (multiId: string, schemeCode: number) =>
    post<RollingRankFundRow>(`/rolling-analysis/rank/${multiId}/add-fund`, { scheme_code: schemeCode }),
  /** Re-fetch the full ranked run — used after addRankedFund, since adding a
   *  fund re-ranks everyone (ranks shift) but only returns the new row. */
  getRollingRank: (multiId: string) =>
    get<RollingRankResponse>(`/rolling-analysis/rank/${multiId}`),
  rollingRankExportUrl: (multiId: string) =>
    buildUrl(`/rolling-analysis/rank/${multiId}/export.pdf`),

  // ── Bulk Fund Analysis (Portfolio Review tab) ──────────────────────────────
  /** Absolute URL for the multipart upload — used on web only, via a real
   *  Blob/File + fetch (see PortfolioReviewScreen.uploadFile()). NOT fetched
   *  via this client's post()/get() helpers (those are JSON-only). */
  bulkAnalysisUploadUrl: () => buildUrl("/bulk-analysis/upload"),
  /** Native (iOS/Android) upload path — plain JSON, no multipart/FormData at
   *  all. RN's native file-upload story proved unreliable across two
   *  different approaches during testing (FormData's {uri,name,type} trick
   *  throws "Unsupported FormDataPart implementation" under the new
   *  architecture/Hermes; expo-file-system's uploadAsync threw an
   *  IOException reading its own app's DocumentPicker cache file in Expo
   *  Go) — base64-over-JSON sidesteps all of that, same code path as every
   *  other request this client makes. See PortfolioReviewScreen.uploadFile(). */
  bulkAnalysisUploadBase64: (filename: string, contentBase64: string) =>
    post<BulkAnalysisUploadResponse>("/bulk-analysis/upload-base64", { filename, content_base64: contentBase64 }),
  bulkAnalysisProcess: (jobId: string, opts?: { includeNifty50?: boolean }) =>
    post<{ status: "started" | "already_running" | "already_completed" }>(
      `/bulk-analysis/${jobId}/process`,
      { include_nifty50: !!opts?.includeNifty50 },
    ),
  bulkAnalysisStatus: (jobId: string) =>
    get<BulkAnalysisJobStatus>(`/bulk-analysis/${jobId}/status`),
  /** Absolute URL — use with expo-file-system's downloadAsync, not fetch(),
   *  same pattern as proposalExportUrl. */
  bulkAnalysisDownloadUrl: (jobId: string) =>
    buildUrl(`/bulk-analysis/${jobId}/download`),

  // ── Portfolio Overlap tab ─────────────────────────────────────────────────
  /** Funds that have a portfolio disclosure on file. Scoped this way (rather
   *  than the full AMFI universe) because picking a fund with no disclosure
   *  can only end in an error at the compare step. */
  overlapFunds: (period?: string) =>
    get<OverlapFundsResponse>(`/overlap/funds${period ? `?period=${encodeURIComponent(period)}` : ""}`),
  /** Omitting `period` lets the backend resolve the newest month BOTH funds
   *  disclose — never each fund's own latest, which would silently compare
   *  two different months. */

  // ── Portfolio Holding Review (drawer → Holding Review) ─────────────────────
  holdingReviewUpload: (filename: string, contentBase64: string) =>
    post<HoldingReview>("/portfolio-review/upload", { filename, content_base64: contentBase64 }),
  holdingReviewList: () =>
    get<{ count: number; results: HoldingReviewSummary[] }>("/portfolio-review"),
  holdingReviewGet: (id: string) => get<HoldingReview>(`/portfolio-review/${id}`),
  holdingReviewSave: (id: string, patch: HoldingReviewPatch) => put<HoldingReview>(`/portfolio-review/${id}`, patch),
  holdingReviewFetchCurrent: (id: string) => post<HoldingReview>(`/portfolio-review/${id}/fetch-current`),
  holdingReviewObservations: (id: string) => post<HoldingReview>(`/portfolio-review/${id}/observations/generate`),
  holdingReviewFinalize: (id: string) => post<HoldingReview>(`/portfolio-review/${id}/finalize`),
  holdingReviewRevise: (id: string) => post<HoldingReview>(`/portfolio-review/${id}/revise`),
  /** Absolute URL — use with FileSystem.downloadAsync (native) / window.open (web). */
  holdingReviewPdfUrl: (id: string) => buildUrl(`/portfolio-review/${id}/export.pdf`),

  /** Pick-list for the overlap selectors: the app's own funds, those with a
   *  portfolio disclosure on file first (`has_holdings`). Empty `q` = the
   *  list shown the moment the picker opens. */
  overlapCandidates: (q?: string) => {
    const p = new URLSearchParams();
    if (q && q.trim()) p.set("q", q.trim());
    const qs = p.toString();
    return get<{ query: string; with_holdings: number; results: OverlapCandidate[] }>(
      `/overlap/candidates${qs ? "?" + qs : ""}`,
    );
  },
  /** Store an AMC monthly portfolio disclosure workbook (base64). Each sheet
   *  is matched to a scheme by name; unmatched sheets are reported, never guessed. */
  overlapIngestUpload: (filename: string, contentBase64: string, period?: string) =>
    post<OverlapIngestResult>("/overlap/ingest-upload", {
      filename, content_base64: contentBase64, ...(period ? { period } : {}),
    }),
  overlapCompare: (fundA: number, fundB: number, period?: string) => {
    const q = new URLSearchParams({ fund_a: String(fundA), fund_b: String(fundB) });
    if (period) q.set("period", period);
    return get<OverlapComparison>(`/overlap/compare?${q.toString()}`);
  },
};

// ---------------------------------------------------------------------------
// Portfolio Overlap types

export interface OverlapFund {
  scheme_code: number;
  scheme_name: string;
  period: string;
  equity_count: number;
  amc?: string;
}

export interface OverlapFundsResponse {
  count: number;
  period: string | null;
  funds: OverlapFund[];
}

export interface OverlapCommonHolding {
  name: string;
  isin: string | null;
  /** Weight as disclosed, i.e. % of the fund's NET ASSETS. */
  weight_a: number;
  weight_b: number;
  /** Weight rebased so each fund's equity sleeve sums to 100 — the basis the
   *  headline `overlap_pct` is computed on. */
  weight_a_equity: number;
  weight_b_equity: number;
  /** min(weight_a_equity, weight_b_equity): this stock's share of the
   *  headline overlap. The list is sorted by it, descending. */
  contribution: number;
  matched_on: "isin" | "name";
}

export interface OverlapSide {
  scheme_code: number;
  scheme_name: string;
  equity_count: number;
  equity_weight: number | null;
  amc?: string | null;
  category?: string | null;
  headline_fqs?: number | null;
  headline_tier?: string | null;
  headline_horizon?: number | null;
  /** % of net assets in the fund's ten largest equity positions. */
  top10_weight?: number | null;
}

export interface OverlapCandidate {
  scheme_code: number;
  scheme_name: string;
  has_holdings: boolean;
  period: string | null;
  equity_count: number | null;
  amc: string | null;
  category?: string | null;
  headline_fqs?: number | null;
}

export interface OverlapUniqueHolding {
  name: string;
  isin: string | null;
  /** % of the fund's net assets. */
  weight: number;
  weight_equity: number;
}

export interface OverlapIngestResult {
  period: string;
  sheets_found: number;
  stored: { scheme_code: number; scheme_name: string; equity_count: number; period: string }[];
  unresolved: { sheet: string | null; scheme_hint: string; equity_count: number }[];
}

export interface OverlapComparison {
  /** YYYY-MM the comparison is based on — the same month for both funds. */
  period: string;
  fund_a: OverlapSide;
  fund_b: OverlapSide;
  fund_a_holdings: number;
  fund_b_holdings: number;
  common_count: number;
  /** THE headline: Σ min(weight_a, weight_b) over common stocks, on the
   *  equity-rebased basis. "This much of a rupee in A is in the same
   *  securities as a rupee in B." */
  overlap_pct: number;
  /** Same sum on raw as-disclosed weights (so a cash-heavy fund scores
   *  lower). Shown as a secondary figure only. */
  overlap_pct_raw: number;
  /** Naive count-based overlap (Jaccard), for contrast — NOT the headline. */
  count_overlap_pct: number;
  equity_sleeve_a: number;
  equity_sleeve_b: number;
  only_in_a: number;
  only_in_b: number;
  common: OverlapCommonHolding[];
  /** Holdings found in only one of the two funds, largest first. */
  unique_a?: OverlapUniqueHolding[];
  unique_b?: OverlapUniqueHolding[];
  /** Share of each fund (% of net assets / % of equity sleeve) held in the common stocks. */
  common_weight_a?: number;
  common_weight_b?: number;
  common_weight_a_equity?: number;
  common_weight_b_equity?: number;
}

// ---------------------------------------------------------------------------
// Bulk Fund Analysis types
// ---------------------------------------------------------------------------

export interface BulkAnalysisUploadResponse {
  job_id: string;
  filename: string;
  total_funds: number;
  /** First ~25 fund names detected, for the pre-processing preview. */
  detected_funds: string[];
}

/** One row this job couldn't match to a fund — enough to go add it manually
 *  (see backend/bulk_analysis.py's not_found_funds). */
export interface BulkAnalysisUnmatchedFund {
  row: number;
  name: string | null;
  isin: string | null;
  code: string | null;
  /** Whichever of name/isin/code was present, or "Row N" if none was. */
  label: string;
}

export interface BulkAnalysisJobStatus {
  job_id: string;
  status: "pending" | "processing" | "completed" | "failed";
  filename: string;
  total: number;
  processed: number;
  matched: number;
  not_found: number;
  errors: number;
  preview_funds: string[];
  created_at: string;
  finished_at: string | null;
  /** Present once any row has failed to match — every unmatched row, so it
   *  can be shown directly on the summary card instead of hunting through
   *  the downloaded Excel's "Fund not found" rows. */
  not_found_funds?: BulkAnalysisUnmatchedFund[];
  /** Present when the job ran with the Nifty 50 benchmark row. */
  benchmark?: BulkBenchmark | null;
}

export interface BulkBenchmark {
  status: "ok" | "unavailable";
  label: string;
  scheme_code?: number;
  /** The Nifty 50 index fund whose NAV stands in for the index. */
  proxy_name?: string;
  horizon?: number | null;
  fqs?: number | null;
  success_pct?: number | null;
  xirr_med?: number | null;
  p10_xirr?: number | null;
  median_wm?: number | null;
  reason?: string;
}