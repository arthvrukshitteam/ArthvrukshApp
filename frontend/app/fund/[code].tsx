import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { View, Text, ScrollView, Pressable, RefreshControl, ActivityIndicator } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { useLocalSearchParams, Stack, useRouter } from "expo-router";
import { RefreshCw, X, CheckCircle2, XCircle, AlertTriangle, ChevronDown } from "lucide-react-native";
import { api, type FundAnalysis, type HorizonStats, type RollingWindow, type LumpsumFundAnalysis, type LumpsumHorizonStats, type LumpsumWindow } from "@/src/api";
import { sipWmToXirr, lumpsumWmToXirr, fmtXirr } from "@/src/utils/xirr";
import { useTheme } from "@/src/ThemeProvider";
import { useViewMode } from "@/src/ViewModeProvider";
import { useMode } from "@/src/ModeProvider";
import { useProposalDraft } from "@/src/ProposalDraftProvider";
import { ViewModeToggle } from "@/src/components/ViewModeToggle";
import { FqsGauge } from "@/src/components/FqsGauge";
import {
  PlainEnglishSummary,
  SmartInsights,
  WmHistogram,
} from "@/src/components/HorizonInsights";
import { CollapsibleSection } from "@/src/components/CollapsibleSection";
import { BrandLogo } from "@/src/components/BrandLogo";
import { formatRupeesCompact } from "@/src/utils/format";
import {
  makeStyles, makeTableStyles, HeadCell, DataRow, StartDateModal,
  FundDetailErrorView, FundDetailSkeleton, FundDetailBanners,
  StartDateFilterRow, HorizonChipRow, PredecessorToggleRow, shortErr,
  AddToPortfolioButton, PortfolioSelectionBar, AmcHistorySection,
  RecomputingNotice, initialBasis, useLatestRequest, type DetailRouteParams,
} from "@/src/components/FundDetailShared";

const WINDOWS_PAGE_SIZE = 30;

export default function FundDetailScreen() {
  const { mode } = useMode();
  if (mode === "lumpsum") return <LumpsumFundDetailScreen />;
  return <SipFundDetailScreen />;
}

function SipFundDetailScreen() {
  // `horizon` is set when navigating from the Home Universe screen.
  // It is absent when navigating from the Search / Recent screen.
  // `since2010`/`mgmt`/`contDate` mirror whatever the Home screen's 2010 and
  // Management toggles were showing on the card that was tapped — without these,
  // this screen used to always reset to full-history/full-predecessor data,
  // silently disagreeing with the card the user just came from.
  const params = useLocalSearchParams<DetailRouteParams>();
  const { code, horizon: horizonParam, since2010: since2010Param, mgmt: mgmtParam, contDate: contDateParam } = params;
  const router = useRouter();
  const { colors, type, tierColor } = useTheme();
  const styles = useMemo(() => makeStyles(colors, type), [colors, type]);
  const { selectedFunds, toggleFund, updateFund, isSelected, setToggleContext } = useProposalDraft();
  const basis = useMemo(() => initialBasis(params), []); // eslint-disable-line react-hooks/exhaustive-deps
  const latest = useLatestRequest();

  const schemeCode = Number(code);

  // Parse the context horizon from the URL param.  null means "not set" (came
  // from search), which triggers the headline_horizon fallback on data load.
  const contextHorizon = useMemo<number | null>(() => {
    if (!horizonParam) return null;
    const n = parseInt(horizonParam, 10);
    return Number.isFinite(n) ? n : null;
  }, [horizonParam]);

  const [data, setData] = useState<FundAnalysis | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [headerRefreshing, setHeaderRefreshing] = useState(false);
  const [recomputing, setRecomputing] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  const [activeHorizon, setActiveHorizon] = useState<number | null>(null);
  // null = full history. Seeded from whatever Home was showing on the tapped
  // card (see initialBasis) so the first render already matches it.
  const [startDate, setStartDate] = useState<string | null>(basis.startDate);
  const [datePickerOpen, setDatePickerOpen] = useState(false);
  // False = this code's own NAVs only ("new management only").
  const [includePredecessor, setIncludePredecessor] = useState(basis.includePredecessor);
  // The Home-context note is only true while the basis it describes is still
  // the one on screen; once the advisor changes or clears the filter it
  // would be describing data that is no longer shown.
  const homeToggleNote =
    basis.note && startDate === basis.startDate && includePredecessor === basis.includePredecessor
      ? basis.note
      : null;

  // A fund already in the draft portfolio follows the filter on screen, so
  // "Build Proposal" scores it on exactly what the advisor is looking at.
  useEffect(() => {
    updateFund(schemeCode, { start_date: startDate, include_predecessor: includePredecessor, explicit_basis: true });
  }, [schemeCode, startDate, includePredecessor, updateFund]);

  const bannerTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const showBanner = useCallback((msg: string) => {
    setBanner(msg);
    if (bannerTimer.current) clearTimeout(bannerTimer.current);
    bannerTimer.current = setTimeout(() => setBanner(null), 6000);
  }, []);

  // The backend only serves its nightly-batch cache when include_predecessor
  // is true AND no explicit refresh was requested — flipping the toggle from
  // false back to true would otherwise silently return that same cached
  // snapshot instead of recomputing, so a mid-session toggle change never
  // looked "live". Track the last value we actually loaded with; whenever it
  // differs from the current one, force refresh:true so the toggle always
  // triggers a fresh computation in both directions.
  const prevIncludePredecessorRef = useRef(includePredecessor);

  // Derive the horizon to select when fresh data arrives.
  //
  // Decision tree:
  //   1. If the user has already manually picked a horizon this session, keep
  //      it as long as the new data still includes it.
  //   2. If we have a contextHorizon from the URL and the fund supports it,
  //      use that (home screen flow).
  //   3. Fall back to the fund's own headline_horizon (search/recent flow).
  //   4. Last resort: last item of available_horizons.
  const pickHorizon = useCallback(
    (d: FundAnalysis, currentActive: number | null): number | null => {
      // 1. Keep user's manual choice if still valid
      if (currentActive != null && d.available_horizons.includes(currentActive)) {
        return currentActive;
      }
      // 2. Context horizon from home screen
      if (contextHorizon != null && d.available_horizons.includes(contextHorizon)) {
        return contextHorizon;
      }
      // 3. Fund's own best horizon
      if (d.headline_horizon != null && d.available_horizons.includes(d.headline_horizon)) {
        return d.headline_horizon;
      }
      // 4. Last available
      return d.available_horizons[d.available_horizons.length - 1] ?? null;
    },
    [contextHorizon],
  );

  const load = useCallback(
    async (opts?: { refresh?: boolean; silent?: boolean }) => {
      const predecessorToggled = prevIncludePredecessorRef.current !== includePredecessor;
      prevIncludePredecessorRef.current = includePredecessor;
      // Each filter change fires a new request. A filtered run is computed
      // live (slow) while "since inception" is served from cache (fast), so
      // without this guard clearing a filter quickly let the slower filtered
      // response land last and put the old filter's numbers back on screen.
      const req = latest.next();
      setRecomputing(true);
      try {
        if (!opts?.silent) setError(null);
        const d = await api.analyze(schemeCode, {
          startDate: startDate ?? undefined,
          refresh: opts?.refresh || predecessorToggled,
          includePredecessor,
        });
        if (!latest.isLatest(req)) return;
        setData(d);
        setActiveHorizon((cur) => pickHorizon(d, cur));
      } catch (e: any) {
        if (!latest.isLatest(req)) return;
        const msg = String(e?.message ?? e);
        if (opts?.silent) {
          showBanner(`Refresh failed — showing cached data. ${shortErr(msg)}`);
        } else {
          setError(msg);
        }
      } finally {
        if (latest.isLatest(req)) setRecomputing(false);
      }
    },
    [schemeCode, startDate, includePredecessor, showBanner, pickHorizon, latest],
  );

  useEffect(() => { load(); }, [load]);
  useEffect(() => () => { if (bannerTimer.current) clearTimeout(bannerTimer.current); }, []);

  const onRefresh = useCallback(async () => {
    setRefreshing(true);
    await load({ refresh: true, silent: !!data });
    setRefreshing(false);
  }, [load, data]);

  const onHeaderRefresh = useCallback(async () => {
    if (headerRefreshing) return;
    setHeaderRefreshing(true);
    await load({ refresh: true, silent: !!data });
    setHeaderRefreshing(false);
  }, [load, data, headerRefreshing]);

  const horizon: HorizonStats | undefined = useMemo(() => {
    if (!data || activeHorizon == null) return undefined;
    return data.horizons.find((h) => h.horizon === activeHorizon);
  }, [data, activeHorizon]);

  // When the user came from the home screen, show a subtle context pill so
  // they know which horizon was pre-selected for them.
  const showContextPill =
    contextHorizon != null &&
    data != null &&
    data.available_horizons.includes(contextHorizon);

  if (error) {
    return <FundDetailErrorView error={error} onRetry={() => load()} onBack={() => router.back()} />;
  }

  if (!data) return <FundDetailSkeleton bottomText="Crunching every rolling SIP window 1Y–50Y…" />;

  const tCol = tierColor(data.headline_tier);
  const xirrPct = data.headline_xirr != null ? (data.headline_xirr * 100).toFixed(2) : "—";

  return (
    <SafeAreaView style={styles.safe} edges={["bottom"]}>
      <Stack.Screen
        options={{
          title: "Fund Analysis",
          headerStyle: { backgroundColor: colors.bg },
          headerTitleStyle: { color: colors.textPri, fontWeight: "700" },
          headerTintColor: colors.textPri,
          headerRight: () => (
            <Pressable
              onPress={onHeaderRefresh}
              disabled={headerRefreshing}
              hitSlop={8}
              style={{ marginRight: 8, opacity: headerRefreshing ? 0.6 : 1 }}
            >
              {headerRefreshing
                ? <ActivityIndicator size="small" color={colors.textSec} />
                : <RefreshCw size={18} color={colors.textSec} />}
            </Pressable>
          ),
        }}
      />

      {banner && (
        <View style={styles.banner}>
          <AlertTriangle size={14} color={colors.amber} />
          <Text style={[type.bodySm, { color: colors.textPri, flex: 1, marginLeft: 8 }]} numberOfLines={2}>
            {banner}
          </Text>
          <Pressable onPress={() => setBanner(null)} hitSlop={8}>
            <X size={14} color={colors.textSec} />
          </Pressable>
        </View>
      )}

      <ScrollView
        style={{ flex: 1 }}
        contentContainerStyle={{ paddingBottom: 40 }}
        refreshControl={<RefreshControl tintColor={colors.textSec} refreshing={refreshing} onRefresh={onRefresh} />}
      >
        <View style={styles.header}>
          <BrandLogo />
          <Text style={[type.label, { color: colors.textTer }]}>
            {data.fund_house || data.scheme_type || "Mutual Fund"}
          </Text>
          <Text style={styles.fundName}>{data.scheme_name}</Text>
          <View style={styles.chipsRow}>
            <View style={[styles.chip, { borderColor: tCol, backgroundColor: `${tCol}1f` }]}>
              <Text style={[styles.chipText, { color: tCol }]}>{data.category}</Text>
            </View>
            <Text style={styles.metaText}>
              {data.monthly_points} monthly NAVs · NAV history {data.first_month?.slice(0, 7) ?? "—"} → {data.last_month?.slice(0, 7) ?? "—"}
            </Text>
          </View>
          {data.scheme_category && (
            <Text style={[type.bodySm, { color: colors.textTer, marginTop: 4 }]}>{data.scheme_category}</Text>
          )}
        </View>

        <FundDetailBanners
          historyNote={data.history_note}
          woundUp={data.wound_up}
          woundUpDate={data.wound_up_date}
          mergedWith={data.merged_with}
          predecessorUsed={data.predecessor_used}
        />

        {/* Context pill — only shown when pre-selected from the home screen */}
        {showContextPill && (
          <View style={styles.contextPill}>
            <Text style={[type.bodySm, { color: colors.azure, fontWeight: "600" }]}>
              Viewing {contextHorizon}Y rolling period · from your universe search
            </Text>
          </View>
        )}

        {/* Explains why startDate/includePredecessor below are pre-set —
            never let the Home → Fund Detail basis change happen silently. */}
        {homeToggleNote && (
          <View style={styles.contextPill}>
            <Text style={[type.bodySm, { color: colors.azure, fontWeight: "600" }]}>
              {homeToggleNote}
            </Text>
          </View>
        )}

        <View style={styles.headlineRow}>
          <HeadCell label="Headline FQS" labelKey="fqs" value={data.headline_fqs?.toFixed(0) ?? "—"} sub={data.headline_tier ?? "—"} color={tCol} />
          <HeadCell label="XIRR Full History" value={`${xirrPct}%`} sub={`since ${data.first_month?.slice(0, 7) ?? "—"}`} color={colors.tierElite} />
          <HeadCell label="Cycle Robust." labelKey="cycle_robustness" value={data.cycle_robustness?.toFixed(1) ?? "—"} sub="weighted Succ%" color={colors.azure} />
        </View>

        <AddToPortfolioButton
          selected={isSelected(schemeCode)}
          startDate={startDate}
          onToggle={() => toggleFund({
            scheme_code: schemeCode, scheme_name: data.scheme_name,
            start_date: startDate, include_predecessor: includePredecessor, explicit_basis: true,
          })}
        />

        <ViewModeToggle />

        <StartDateFilterRow
          startDate={startDate}
          busy={recomputing}
          onOpen={() => setDatePickerOpen(true)}
          onClear={() => setStartDate(null)}
        />

        <PredecessorToggleRow
          includePredecessor={includePredecessor}
          onChange={setIncludePredecessor}
          predecessorAvailable={data.predecessor_available ?? []}
        />

        <AmcHistorySection
          schemeCode={schemeCode}
          activeStart={startDate}
          onSelectPeriod={(d) => { setStartDate(d); setIncludePredecessor(true); }}
        />

        <HorizonChipRow
          availableHorizons={data.available_horizons}
          activeHorizon={activeHorizon}
          onSelect={setActiveHorizon}
          getFqs={(h) => data.horizons.find((x) => x.horizon === h)?.fqs}
          contextHorizon={contextHorizon}
        />

        <RecomputingNotice visible={recomputing} startDate={startDate} />

        {horizon && <HorizonView h={horizon} />}
      </ScrollView>

      <PortfolioSelectionBar
        count={selectedFunds.length}
        onBuild={() => {
          setToggleContext(since2010Param === "1", mgmtParam === "1" && !!contDateParam);
          router.push("/proposal/build" as any);
        }}
      />

      <StartDateModal
        open={datePickerOpen}
        onClose={() => setDatePickerOpen(false)}
        initial={startDate}
        firstMonth={data.first_month}
        lastMonth={data.last_month}
        onApply={(d) => { setStartDate(d); setDatePickerOpen(false); }}
      />
    </SafeAreaView>
  );
}

// ─── LumpsumFundDetailScreen ────────────────────────────────────────────────

function LumpsumFundDetailScreen() {
  // since2010/mgmt/contDate mirror Home's toggle state for this fund — see
  // the SIP screen above for the full rationale (same fix, same reasoning).
  const params = useLocalSearchParams<DetailRouteParams>();
  const { code, since2010: since2010Param, mgmt: mgmtParam, contDate: contDateParam } = params;
  const router = useRouter();
  const { colors, type, tierColor } = useTheme();
  const styles = useMemo(() => makeStyles(colors, type), [colors, type]);
  const { selectedFunds, toggleFund, updateFund, isSelected, setToggleContext } = useProposalDraft();
  const basis = useMemo(() => initialBasis(params), []); // eslint-disable-line react-hooks/exhaustive-deps
  const latest = useLatestRequest();

  const schemeCode = Number(code);

  const [data, setData] = useState<LumpsumFundAnalysis | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [headerRefreshing, setHeaderRefreshing] = useState(false);
  const [recomputing, setRecomputing] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  const [activeHorizon, setActiveHorizon] = useState<number | null>(null);
  const [startDate, setStartDate] = useState<string | null>(basis.startDate);
  const [datePickerOpen, setDatePickerOpen] = useState(false);
  const [includePredecessor, setIncludePredecessor] = useState(basis.includePredecessor);
  const homeToggleNote =
    basis.note && startDate === basis.startDate && includePredecessor === basis.includePredecessor
      ? basis.note
      : null;

  useEffect(() => {
    updateFund(schemeCode, { start_date: startDate, include_predecessor: includePredecessor, explicit_basis: true });
  }, [schemeCode, startDate, includePredecessor, updateFund]);

  const bannerTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const showBanner = useCallback((msg: string) => {
    setBanner(msg);
    if (bannerTimer.current) clearTimeout(bannerTimer.current);
    bannerTimer.current = setTimeout(() => setBanner(null), 6000);
  }, []);

  // Same fix as the SIP screen: the backend only serves its nightly-batch
  // cache when include_predecessor is true and no explicit refresh was
  // requested, so flipping the toggle back to true mid-session would
  // otherwise silently return that stale cached snapshot instead of
  // recomputing. Force refresh:true whenever the toggle actually changes.
  const prevIncludePredecessorRef = useRef(includePredecessor);

  const load = useCallback(
    async (opts?: { refresh?: boolean; silent?: boolean }) => {
      const predecessorToggled = prevIncludePredecessorRef.current !== includePredecessor;
      prevIncludePredecessorRef.current = includePredecessor;
      const req = latest.next(); // see the SIP screen: drop out-of-order responses
      setRecomputing(true);
      try {
        if (!opts?.silent) setError(null);
        const d = await api.analyzeLumpsum(schemeCode, {
          startDate: startDate ?? undefined,
          refresh: opts?.refresh || predecessorToggled,
          includePredecessor,
        });
        if (!latest.isLatest(req)) return;
        setData(d);
        setActiveHorizon((cur) => {
          if (cur != null && d.available_horizons.includes(cur)) return cur;
          if (d.headline_horizon && d.available_horizons.includes(d.headline_horizon)) return d.headline_horizon;
          return d.available_horizons[d.available_horizons.length - 1] ?? null;
        });
      } catch (e: any) {
        if (!latest.isLatest(req)) return;
        const msg = String(e?.message ?? e);
        if (opts?.silent) showBanner(`Refresh failed. ${shortErr(msg)}`);
        else setError(msg);
      } finally {
        if (latest.isLatest(req)) setRecomputing(false);
      }
    },
    [schemeCode, startDate, includePredecessor, showBanner, latest],
  );

  useEffect(() => { load(); }, [load]);
  useEffect(() => () => { if (bannerTimer.current) clearTimeout(bannerTimer.current); }, []);

  const onRefresh = useCallback(async () => {
    setRefreshing(true);
    await load({ refresh: true, silent: !!data });
    setRefreshing(false);
  }, [load, data]);

  const onHeaderRefresh = useCallback(async () => {
    if (headerRefreshing) return;
    setHeaderRefreshing(true);
    await load({ refresh: true, silent: !!data });
    setHeaderRefreshing(false);
  }, [load, data, headerRefreshing]);

  const lHorizon: LumpsumHorizonStats | undefined = useMemo(() => {
    if (!data || activeHorizon == null) return undefined;
    return data.horizons.find((h) => h.horizon === activeHorizon);
  }, [data, activeHorizon]);

  if (error) {
    return <FundDetailErrorView error={error} onRetry={() => load()} onBack={() => router.back()} />;
  }

  if (!data) return <FundDetailSkeleton bottomText="Computing every rolling lumpsum window 1Y–50Y…" />;

  const tCol = tierColor(data.headline_fqs_rating);
  // Headline XIRR — mirrors the SIP screen's "XIRR Full History" cell, which
  // reads data.headline_xirr (a field that only exists on FundAnalysis).
  // Lumpsum analyses have no equivalent full-history XIRR field, so this
  // derives the same kind of number from the fund's own headline horizon:
  // its median Wealth Multiple converted to an annualised XIRR via the
  // lumpsum WM->XIRR formula (see src/utils/xirr.ts).
  const headlineHorizonStats = data.horizons.find((x) => x.horizon === data.headline_horizon);
  const headlineXirr = headlineHorizonStats
    ? lumpsumWmToXirr(headlineHorizonStats.median_wm, data.headline_horizon)
    : null;
  const headlineXirrPct = fmtXirr(headlineXirr);

  return (
    <SafeAreaView style={styles.safe} edges={["bottom"]}>
      <Stack.Screen
        options={{
          title: "Lumpsum Analysis",
          headerStyle: { backgroundColor: colors.bg },
          headerTitleStyle: { color: colors.textPri, fontWeight: "700" },
          headerTintColor: colors.textPri,
          headerRight: () => (
            <Pressable
              onPress={onHeaderRefresh}
              disabled={headerRefreshing}
              hitSlop={8}
              style={{ marginRight: 8, opacity: headerRefreshing ? 0.6 : 1 }}
            >
              {headerRefreshing
                ? <ActivityIndicator size="small" color={colors.textSec} />
                : <RefreshCw size={18} color={colors.textSec} />}
            </Pressable>
          ),
        }}
      />

      {banner && (
        <View style={styles.banner}>
          <AlertTriangle size={14} color={colors.amber} />
          <Text style={[type.bodySm, { color: colors.textPri, flex: 1, marginLeft: 8 }]} numberOfLines={2}>{banner}</Text>
          <Pressable onPress={() => setBanner(null)} hitSlop={8}><X size={14} color={colors.textSec} /></Pressable>
        </View>
      )}

      <ScrollView
        style={{ flex: 1 }}
        contentContainerStyle={{ paddingBottom: 40 }}
        refreshControl={<RefreshControl tintColor={colors.textSec} refreshing={refreshing} onRefresh={onRefresh} />}
      >
        {/* Fund header */}
        <View style={styles.header}>
          <BrandLogo />
          <Text style={[type.label, { color: colors.textTer }]}>Lumpsum · {data.is_equity ? "Equity" : "Non-Equity"}</Text>
          <Text style={styles.fundName}>{data.scheme_name}</Text>
          <View style={styles.chipsRow}>
            <View style={[styles.chip, { borderColor: tCol, backgroundColor: `${tCol}1f` }]}>
              <Text style={[styles.chipText, { color: tCol }]}>{data.category}</Text>
            </View>
            <Text style={styles.metaText}>{data.monthly_points} monthly NAVs</Text>
          </View>
        </View>

        <FundDetailBanners
          historyNote={data.history_note}
          woundUp={data.wound_up}
          woundUpDate={data.wound_up_date}
          mergedWith={data.merged_with}
          predecessorUsed={data.predecessor_used}
        />

        {/* Explains why startDate/includePredecessor below are pre-set —
            never let the Home → Fund Detail basis change happen silently. */}
        {homeToggleNote && (
          <View style={styles.contextPill}>
            <Text style={[type.bodySm, { color: colors.azure, fontWeight: "600" }]}>
              {homeToggleNote}
            </Text>
          </View>
        )}

        {/* Headline metrics — same 3-cell layout as the SIP screen (Headline
            FQS / Headline XIRR / Cycle Robustness), fed by lumpsum figures. */}
        <View style={styles.headlineRow}>
          <HeadCell label="Headline FQS" labelKey="fqs" value={data.headline_fqs?.toFixed(0) ?? "—"} sub={data.headline_fqs_rating ?? "—"} color={tCol} />
          <HeadCell label="Headline XIRR" value={headlineXirrPct} sub={`at ${data.headline_horizon}Y median`} color={colors.tierElite} />
          <HeadCell label="Cycle Robust." labelKey="cycle_robustness" value={data.cycle_robustness != null ? data.cycle_robustness.toFixed(1) : "—"} sub={data.cycle_variant ?? "weighted Succ%"} color={colors.azure} />
        </View>

        <AddToPortfolioButton
          selected={isSelected(schemeCode)}
          startDate={startDate}
          onToggle={() => toggleFund({
            scheme_code: schemeCode, scheme_name: data.scheme_name,
            start_date: startDate, include_predecessor: includePredecessor, explicit_basis: true,
          })}
        />

        <ViewModeToggle />

        <StartDateFilterRow
          startDate={startDate}
          busy={recomputing}
          onOpen={() => setDatePickerOpen(true)}
          onClear={() => setStartDate(null)}
          label="Start date:"
        />

        <PredecessorToggleRow
          includePredecessor={includePredecessor}
          onChange={setIncludePredecessor}
          predecessorAvailable={data.predecessor_available ?? []}
        />

        <AmcHistorySection
          schemeCode={schemeCode}
          activeStart={startDate}
          onSelectPeriod={(d) => { setStartDate(d); setIncludePredecessor(true); }}
        />

        <HorizonChipRow
          availableHorizons={data.available_horizons}
          activeHorizon={activeHorizon}
          onSelect={setActiveHorizon}
          getFqs={(h) => data.horizons.find((x) => x.horizon === h)?.fqs}
        />

        <RecomputingNotice visible={recomputing} startDate={startDate} />

        {lHorizon && <LumpsumHorizonView h={lHorizon} />}
      </ScrollView>

      <PortfolioSelectionBar
        count={selectedFunds.length}
        onBuild={() => {
          setToggleContext(since2010Param === "1", mgmtParam === "1" && !!contDateParam);
          router.push("/proposal/build" as any);
        }}
      />

      <StartDateModal
        open={datePickerOpen}
        onClose={() => setDatePickerOpen(false)}
        initial={startDate}
        firstMonth={null}
        lastMonth={null}
        onApply={(d) => { setStartDate(d); setDatePickerOpen(false); }}
      />
    </SafeAreaView>
  );
}

// ─── LumpsumHorizonView ──────────────────────────────────────────────────────

function LumpsumHorizonView({ h }: { h: LumpsumHorizonStats }) {
  const { mode } = useViewMode();
  const showAnalyst = mode === "analyst";

  return (
    <View>
      <FqsGauge score={h.fqs} tier={h.fqs_rating} horizon={h.horizon} />

      {/* Same plain-language widgets the SIP screen uses (PlainEnglishSummary /
          SmartInsights / WmHistogram) — identical layout, fed by this fund's
          lumpsum rolling-window stats instead of SIP rolling-window stats. */}
      <PlainEnglishSummary h={h} mode="lumpsum" />
      <SmartInsights h={h} mode="lumpsum" />
      <WmHistogram h={h} />

      {showAnalyst && (
        <>
          <CollapsibleSection
            title="EXPECTED RETURNS"
            subtitle="Where the typical lumpsum outcome lands"
            initiallyOpen
          >
            <DataRow label="Median WM" labelKey="median_wm" value={`${h.median_wm.toFixed(4)}x  (${fmtXirr(lumpsumWmToXirr(h.median_wm, h.horizon))} p.a.)`} highlight />
            <DataRow label="Mean WM" labelKey="mean_wm" value={`${h.mean_wm.toFixed(4)}x`} />
            <DataRow label="P25 WM" labelKey="p25" value={`${h.p25_wm.toFixed(4)}x`} />
            <DataRow label="P75 WM" labelKey="p75" value={`${h.p75_wm.toFixed(4)}x`} />
            <DataRow label="Max WM" value={`${h.max_wm.toFixed(4)}x`} good />
            <DataRow label="P90 WM" labelKey="p90" value={`${h.p90_wm.toFixed(4)}x`} />
          </CollapsibleSection>

          <CollapsibleSection title="WORST CASE" subtitle="Bottom of the distribution">
            <DataRow label="Min WM" value={`${h.min_wm.toFixed(4)}x`} bad={h.min_wm < h.min_wm_required} />
            <DataRow label="P10 WM" labelKey="p10" value={`${h.p10_wm.toFixed(4)}x  (${fmtXirr(lumpsumWmToXirr(h.p10_wm, h.horizon))} p.a.)`} bad={h.p10_wm < h.min_wm_required} />
            <DataRow label="ES10 (avg of bottom 10%)" labelKey="es10" value={`${h.es10.toFixed(4)}x`} />
            <DataRow label="Failure %" labelKey="failure_pct" value={`${(h.failure_pct * 100).toFixed(2)}%`} bad={h.failure_pct > 0} />
          </CollapsibleSection>

          <CollapsibleSection title="CONSISTENCY" subtitle="How tightly outcomes cluster">
            <DataRow label="Std Dev" value={h.std_dev.toFixed(4)} />
            <DataRow label="CV (σ / mean)" labelKey="cv" value={`${(h.cv * 100).toFixed(2)}%`} />
            <DataRow label="Success %" labelKey="success_pct" value={`${(h.success_pct * 100).toFixed(2)}%`} good={h.success_pct === 1} highlight />
            <DataRow label="N Windows" labelKey="n_windows" value={String(h.n_windows)} />
            <DataRow label="Min WM Required" labelKey="min_wm_required" value={`${h.min_wm_required.toFixed(4)}x`} />
            <DataRow
              label="Alpha Trend"
              labelKey="alpha_trend"
              value={h.alpha_trend != null ? `${h.alpha_trend.toFixed(3)}×` : "—"}
              good={h.alpha_trend != null && h.alpha_trend > 1.05}
              bad={h.alpha_trend != null && h.alpha_trend < 0.95}
            />
            <DataRow
              label="Stress Score"
              labelKey="stress_score"
              value={h.stress_score != null ? h.stress_score.toFixed(1) : "—"}
              good={h.stress_score != null && h.stress_score >= 70}
              bad={h.stress_score != null && h.stress_score < 40}
            />
          </CollapsibleSection>

          <CollapsibleSection title="SCORE BREAKDOWN" subtitle="How the FQS is built (0–100 sub-scores)">
            <DataRow label="Failure Score" labelKey="fail_score" value={h.failure_score.toFixed(1)} />
            <DataRow label="Downside Sev. Score" labelKey="downside_severity" value={h.downside_sev_score.toFixed(1)} />
            <DataRow label="CV Score" labelKey="cv_score" value={h.cv_score.toFixed(1)} />
            <DataRow label="Risk Score" labelKey="risk_score" value={h.risk_score.toFixed(1)} />
            <DataRow label="Adj Risk Score" labelKey="adj_risk_score" value={h.adj_risk_score.toFixed(1)} />
            <DataRow label="Blended WM" labelKey="blended_wm" value={`${h.blended_wm.toFixed(4)}x  (${fmtXirr(lumpsumWmToXirr(h.blended_wm, h.horizon))} p.a.)`} />
            <DataRow label="Blended WM Score" labelKey="blended_wm_score" value={`${h.blended_wm_score.toFixed(1)} / cap ${h.cap}`} />
            <DataRow label="Downside Score" labelKey="downside_score" value={`${h.downside_score.toFixed(1)} / cap ${h.cap}`} />
            <DataRow label="Consistency Score" labelKey="consistency_score" value={h.consistency_score.toFixed(1)} />
            <DataRow label="ES Score" labelKey="es_score" value={h.es_score.toFixed(1)} />
          </CollapsibleSection>

          <CollapsibleSection
            title={`ROLLING ${h.horizon}Y LUMPSUM WINDOWS`}
            subtitle={h.windows?.length ? `All ${h.n_windows} windows — every individual lumpsum simulation` : "Load this fund directly to see window detail"}
            dense
          >
            <LumpsumWindowsTable rows={h.windows ?? []} min={h.min_wm_required} />
          </CollapsibleSection>
        </>
      )}
    </View>
  );
}

// ─── LumpsumWindowsTable ──────────────────────────────────────────────────────

const LumpsumWindowsTable = memo(function LumpsumWindowsTable({
  rows, min,
}: { rows: LumpsumWindow[]; min: number }) {
  const { colors, type } = useTheme();
  const tableStyles = useMemo(() => makeTableStyles(colors), [colors]);
  const [expanded, setExpanded] = useState(false);
  useEffect(() => { setExpanded(false); }, [rows]);

  const total = rows.length;
  const visible = useMemo(() => {
    if (expanded || total <= WINDOWS_PAGE_SIZE) return rows;
    return rows.slice(-WINDOWS_PAGE_SIZE).reverse();
  }, [rows, expanded, total]);
  const isCollapsed = !expanded && total > WINDOWS_PAGE_SIZE;

  return (
    <View>
      <ScrollView horizontal showsHorizontalScrollIndicator>
        <View>
          <View style={[tableStyles.row, tableStyles.headerRow]}>
            <Text style={[tableStyles.cellNum, tableStyles.header]}>#</Text>
            <Text style={[tableStyles.cellDate, tableStyles.header]}>Invest</Text>
            <Text style={[tableStyles.cellDate, tableStyles.header]}>Redeem</Text>
            <Text style={[tableStyles.cell, tableStyles.header]}>Start NAV</Text>
            <Text style={[tableStyles.cell, tableStyles.header]}>End NAV</Text>
            <Text style={[tableStyles.cellWide, tableStyles.header]}>Final Value</Text>
            <Text style={[tableStyles.cell, tableStyles.header]}>WM</Text>
            <Text style={[tableStyles.cellFlag, tableStyles.header]}>✓</Text>
          </View>
          {visible.map((w, i) => {
            const ok = w.wealth_multiple >= min;
            const displayIdx = isCollapsed ? total - i : i + 1;
            return (
              <View key={`${w.start_date}-${displayIdx}`} style={[tableStyles.row, i % 2 === 1 && tableStyles.rowAlt]}>
                <Text style={tableStyles.cellNum}>{displayIdx}</Text>
                <Text style={tableStyles.cellDate}>{w.start_date}</Text>
                <Text style={tableStyles.cellDate}>{w.end_date}</Text>
                <Text style={tableStyles.cell}>{w.starting_nav.toFixed(2)}</Text>
                <Text style={tableStyles.cell}>{w.ending_nav.toFixed(2)}</Text>
                <Text style={tableStyles.cellWide}>{fmtINR(w.final_value)}</Text>
                <Text style={[tableStyles.cell, { color: ok ? colors.tierElite : colors.tierWeak, fontWeight: "700" }]}>
                  {w.wealth_multiple.toFixed(3)}x
                </Text>
                <View style={tableStyles.cellFlag}>
                  {ok ? <CheckCircle2 size={14} color={colors.tierElite} /> : <XCircle size={14} color={colors.tierWeak} />}
                </View>
              </View>
            );
          })}
        </View>
      </ScrollView>
      {total > WINDOWS_PAGE_SIZE && (
        <Pressable
          onPress={() => setExpanded((v) => !v)}
          style={({ pressed }) => [tableStyles.expandBtn, pressed && { opacity: 0.85 }]}
        >
          <Text style={[type.bodySm, { color: colors.azure, fontWeight: "700" }]}>
            {expanded ? "SHOW RECENT 30 ONLY" : `SHOW ALL ${total} WINDOWS`}
          </Text>
          <ChevronDown size={14} color={colors.azure} style={{ marginLeft: 6, transform: [{ rotate: expanded ? "180deg" : "0deg" }] }} />
        </Pressable>
      )}
    </View>
  );
});

// ─── HorizonView ────────────────────────────────────────────────────────────

function HorizonView({ h }: { h: HorizonStats }) {
  const { mode } = useViewMode();
  const showAnalyst = mode === "analyst";
  return (
    <View>
      <FqsGauge score={h.fqs} tier={fqsTier(h.fqs)} horizon={h.horizon} />
      <PlainEnglishSummary h={h} />
      <SmartInsights h={h} />
      <WmHistogram h={h} />

      {showAnalyst && (
        <>
          <CollapsibleSection
            title="EXPECTED RETURNS"
            subtitle="Where the typical SIP outcome lands"
            initiallyOpen
          >
            <DataRow label="Median WM" labelKey="median_wm" value={`${h.median_wm.toFixed(4)}x  (${fmtXirr(sipWmToXirr(h.median_wm, h.horizon))} p.a.)`} highlight />
            <DataRow label="Mean WM" labelKey="mean_wm" value={`${h.mean_wm.toFixed(4)}x`} />
            <DataRow label="P25 WM" labelKey="p25" value={`${h.p25_wm.toFixed(4)}x`} />
            <DataRow label="P75 WM" labelKey="p75" value={`${h.p75_wm.toFixed(4)}x`} />
            <DataRow label="Max WM" value={`${h.max_wm.toFixed(4)}x`} good />
            <DataRow label="P90 WM" labelKey="p90" value={`${h.p90_wm.toFixed(4)}x`} />
          </CollapsibleSection>

          <CollapsibleSection title="WORST CASE" subtitle="Bottom of the distribution">
            <DataRow label="Min WM" value={`${h.min_wm.toFixed(4)}x`} bad={h.min_wm < h.min_wm_required} />
            <DataRow label="P10 WM" labelKey="p10" value={`${h.p10_wm.toFixed(4)}x  (${fmtXirr(sipWmToXirr(h.p10_wm, h.horizon))} p.a.)`} bad={h.p10_wm < h.min_wm_required} />
            <DataRow label="ES10 (avg of bottom 10%)" labelKey="es10" value={`${h.es10.toFixed(4)}x`} />
            <DataRow label="Failure %" labelKey="failure_pct" value={`${(h.failure_pct * 100).toFixed(2)}%`} bad={h.failure_pct > 0} />
          </CollapsibleSection>

          <CollapsibleSection title="CONSISTENCY" subtitle="How tightly outcomes cluster">
            <DataRow label="Std Dev" labelKey="std_wm" value={h.std_wm.toFixed(4)} />
            <DataRow label="CV (σ / mean)" labelKey="cv" value={`${(h.cv * 100).toFixed(2)}%`} />
            <DataRow label="Success %" labelKey="success_pct" value={`${(h.success_pct * 100).toFixed(2)}%`} good={h.success_pct === 1} highlight />
            <DataRow label="N Windows" labelKey="n_windows" value={String(h.n_windows)} />
            <DataRow label="Min WM Required" labelKey="min_wm_required" value={`${h.min_wm_required.toFixed(4)}x`} />
            <DataRow
              label="Alpha Trend"
              labelKey="alpha_trend"
              value={h.alpha_trend != null ? `${h.alpha_trend.toFixed(3)}×` : "—"}
              good={h.alpha_trend != null && h.alpha_trend > 1.05}
              bad={h.alpha_trend != null && h.alpha_trend < 0.95}
            />
            <DataRow
              label="Stress Score"
              labelKey="stress_score"
              value={h.stress_score != null ? h.stress_score.toFixed(1) : "—"}
              good={h.stress_score != null && h.stress_score >= 70}
              bad={h.stress_score != null && h.stress_score < 40}
            />
          </CollapsibleSection>

          <CollapsibleSection title="SCORE BREAKDOWN" subtitle="How the FQS is built (0–100 sub-scores)">
            <DataRow label="Failure Score" labelKey="fail_score" value={h.fail_score.toFixed(1)} />
            <DataRow label="Downside Severity" labelKey="downside_severity" value={h.downside_severity.toFixed(1)} />
            <DataRow label="CV Score" labelKey="cv_score" value={h.cv_score.toFixed(1)} />
            <DataRow label="Risk Score" labelKey="risk_score" value={h.risk_score.toFixed(1)} />
            <DataRow label="Adj Risk Score" labelKey="adj_risk_score" value={h.adj_risk_score.toFixed(1)} />
            <DataRow label="Blended WM" labelKey="blended_wm" value={`${h.blended_wm.toFixed(4)}x  (${fmtXirr(sipWmToXirr(h.blended_wm, h.horizon))} p.a.)`} />
            <DataRow label="Blended WM Score" labelKey="blended_wm_score" value={`${h.blended_wm_score.toFixed(1)} / cap ${h.cap}`} />
            <DataRow label="Downside Score" labelKey="downside_score" value={`${h.downside_score.toFixed(1)} / cap ${h.cap}`} />
            <DataRow label="Consistency Score" labelKey="consistency_score" value={h.consistency_score.toFixed(1)} />
            <DataRow label="ES Score" labelKey="es_score" value={h.es_score.toFixed(1)} />
          </CollapsibleSection>

          <CollapsibleSection
            title={`ROLLING ${h.horizon}Y SIP WINDOWS`}
            subtitle={h.windows?.length ? `All ${h.n_windows} windows — every individual SIP simulation` : "Load this fund directly to see window detail"}
            dense
          >
            <WindowsTable rows={h.windows ?? []} min={h.min_wm_required} />
          </CollapsibleSection>
        </>
      )}
    </View>
  );
}

// ─── WindowsTable ──────────────────────────────────────────────────────────

const WindowsTable = memo(function WindowsTable({ rows, min }: { rows: RollingWindow[]; min: number }) {
  const { colors, type } = useTheme();
  const tableStyles = useMemo(() => makeTableStyles(colors), [colors]);
  const [expanded, setExpanded] = useState(false);
  useEffect(() => { setExpanded(false); }, [rows]);

  const total = rows.length;
  const visible = useMemo(() => {
    if (expanded || total <= WINDOWS_PAGE_SIZE) return rows;
    return rows.slice(-WINDOWS_PAGE_SIZE).reverse();
  }, [rows, expanded, total]);
  const isCollapsed = !expanded && total > WINDOWS_PAGE_SIZE;

  return (
    <View>
      <ScrollView horizontal showsHorizontalScrollIndicator>
        <View>
          <View style={[tableStyles.row, tableStyles.headerRow]}>
            <Text style={[tableStyles.cellNum, tableStyles.header]}>#</Text>
            <Text style={[tableStyles.cellDate, tableStyles.header]}>Start</Text>
            <Text style={[tableStyles.cellDate, tableStyles.header]}>End</Text>
            <Text style={[tableStyles.cell, tableStyles.header]}>Units</Text>
            <Text style={[tableStyles.cell, tableStyles.header]}>End NAV</Text>
            <Text style={[tableStyles.cellWide, tableStyles.header]}>End Value</Text>
            <Text style={[tableStyles.cellWide, tableStyles.header]}>Invested</Text>
            <Text style={[tableStyles.cell, tableStyles.header]}>WM</Text>
            <Text style={[tableStyles.cellFlag, tableStyles.header]}>Bar</Text>
          </View>
          {visible.map((w, i) => {
            const ok = w.wealth_multiple >= min;
            const displayIdx = isCollapsed ? total - i : i + 1;
            return (
              <View key={`${w.start_date}-${displayIdx}`} style={[tableStyles.row, i % 2 === 1 && tableStyles.rowAlt]}>
                <Text style={tableStyles.cellNum}>{displayIdx}</Text>
                <Text style={tableStyles.cellDate}>{w.start_date}</Text>
                <Text style={tableStyles.cellDate}>{w.end_date}</Text>
                <Text style={tableStyles.cell}>{w.total_units.toFixed(1)}</Text>
                <Text style={tableStyles.cell}>{w.end_nav.toFixed(2)}</Text>
                <Text style={tableStyles.cellWide}>{fmtINR(w.end_value)}</Text>
                <Text style={[tableStyles.cellWide, { color: colors.textTer }]}>{fmtINR(w.total_invested)}</Text>
                <Text style={[tableStyles.cell, { color: ok ? colors.tierElite : colors.tierWeak, fontWeight: "700" }]}>
                  {w.wealth_multiple.toFixed(3)}x
                </Text>
                <View style={tableStyles.cellFlag}>
                  {ok ? <CheckCircle2 size={14} color={colors.tierElite} /> : <XCircle size={14} color={colors.tierWeak} />}
                </View>
              </View>
            );
          })}
        </View>
      </ScrollView>
      {total > WINDOWS_PAGE_SIZE && (
        <Pressable
          onPress={() => setExpanded((v) => !v)}
          style={({ pressed }) => [tableStyles.expandBtn, pressed && { opacity: 0.85 }]}
        >
          <Text style={[type.bodySm, { color: colors.azure, fontWeight: "700" }]}>
            {expanded ? "SHOW RECENT 30 ONLY" : `SHOW ALL ${total} WINDOWS`}
          </Text>
          <ChevronDown
            size={14}
            color={colors.azure}
            style={{ marginLeft: 6, transform: [{ rotate: expanded ? "180deg" : "0deg" }] }}
          />
        </Pressable>
      )}
    </View>
  );
});

// ─── helpers ──────────────────────────────────────────────────────────────

// Delegates to the shared Indian formatter. The old local version dropped the
// ₹ symbol on the Cr/L branches and used Western-style "L" only.
const fmtINR = (v: number): string => formatRupeesCompact(v, { short: true });

function fqsTier(score: number): string {
  if (score >= 90) return "Elite";
  if (score >= 80) return "Strong";
  if (score >= 70) return "Good";
  if (score >= 60) return "Average";
  return "Weak";
}