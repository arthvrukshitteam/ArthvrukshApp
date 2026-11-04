// Excel-style analyst Fund Detail screen.
//
// Layout (top → bottom):
//   1. Fund header + scheme meta + headline FQS / XIRR / aggregate scores
//   2. Start-date filter (NAV history clip)
//   3. Horizon picker (1Y..20Y chips, disabled where data is missing)
//   4. DISTRIBUTION_METRICS row for selected horizon
//   5. Stats block + Sub-scores block
//   6. Rolling-windows table — every SIP window with start/end/units/value/WM/good

import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  View,
  Text,
  StyleSheet,
  ScrollView,
  Pressable,
  ActivityIndicator,
  RefreshControl,
  Modal,
  TextInput,
  KeyboardAvoidingView,
  Platform,
  Keyboard,
  TouchableWithoutFeedback,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { useLocalSearchParams, Stack, useRouter } from "expo-router";
import {
  Calendar, RefreshCw, X, CheckCircle2, XCircle, Filter,
  AlertTriangle, ChevronDown,
} from "lucide-react-native";
import { api, type FundAnalysis, type HorizonStats, type RollingWindow } from "@/src/api";
import { radii, space, type ThemeColors, type ThemeType } from "@/src/theme";
import { useTheme } from "@/src/ThemeProvider";
import { useViewMode } from "@/src/ViewModeProvider";
import { ViewModeToggle } from "@/src/components/ViewModeToggle";
import { FqsGauge } from "@/src/components/FqsGauge";
import {
  PlainEnglishSummary,
  SmartInsights,
  WmHistogram,
} from "@/src/components/HorizonInsights";
import { CollapsibleSection } from "@/src/components/CollapsibleSection";
import { InfoHint } from "@/src/components/InfoHint";
import { BrandLogo } from "@/src/components/BrandLogo";

const WINDOWS_PAGE_SIZE = 30;

export default function FundDetailScreen() {
  const { code } = useLocalSearchParams<{ code: string }>();
  const router = useRouter();
  const { colors, type, tierColor, scoreColor } = useTheme();
  const styles = useMemo(() => makeStyles(colors, type), [colors, type]);

  const schemeCode = Number(code);
  const [data, setData] = useState<FundAnalysis | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [headerRefreshing, setHeaderRefreshing] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  const [activeHorizon, setActiveHorizon] = useState<number | null>(null);
  const [startDate, setStartDate] = useState<string | null>(null);
  const [datePickerOpen, setDatePickerOpen] = useState(false);
  const [includePredecessors, setIncludePredecessors] = useState(false);

  const bannerTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const showBanner = useCallback((msg: string) => {
    setBanner(msg);
    if (bannerTimer.current) clearTimeout(bannerTimer.current);
    bannerTimer.current = setTimeout(() => setBanner(null), 6000);
  }, []);

  const load = useCallback(
    async (opts?: { refresh?: boolean; silent?: boolean }) => {
      try {
        if (!opts?.silent) setError(null);
        const d = await api.analyze(schemeCode, {
          startDate: startDate ?? undefined,
          refresh: opts?.refresh,
          includePredecessors: includePredecessors || undefined,
        });
        setData(d);
        setActiveHorizon((cur) =>
          cur && d.available_horizons.includes(cur)
            ? cur
            : d.headline_horizon ?? d.available_horizons[d.available_horizons.length - 1] ?? null,
        );
      } catch (e: any) {
        const msg = String(e?.message ?? e);
        if (opts?.silent) {
          showBanner(`Refresh failed — showing cached data. ${shortErr(msg)}`);
        } else {
          setError(msg);
        }
      }
    },
    [schemeCode, startDate, includePredecessors, showBanner],
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

  if (error) {
    return (
      <SafeAreaView style={styles.safe} edges={["bottom"]}>
        <Stack.Screen options={{ title: "" }} />
        <View style={styles.errorWrap}>
          <View style={styles.errorIcon}>
            <AlertTriangle size={28} color={colors.tierWeak} />
          </View>
          <Text style={[type.h3, { color: colors.textPri, textAlign: "center", marginTop: space.md }]}>
            Couldn&apos;t load this fund
          </Text>
          <Text style={[type.bodySm, { color: colors.textSec, textAlign: "center", marginTop: 6 }]} numberOfLines={3}>
            {shortErr(error)}
          </Text>
          <Pressable
            onPress={() => load()}
            style={({ pressed }) => [styles.retryBtn, pressed && { opacity: 0.85 }]}
          >
            <RefreshCw size={14} color={colors.textPri} />
            <Text style={[type.body, { color: colors.textPri, fontWeight: "700", marginLeft: 8 }]}>RETRY</Text>
          </Pressable>
          <Pressable onPress={() => router.back()} style={{ marginTop: space.md }}>
            <Text style={[type.bodySm, { color: colors.textTer }]}>← Go back</Text>
          </Pressable>
        </View>
      </SafeAreaView>
    );
  }

  if (!data) return <SkeletonScreen />;

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
              {data.monthly_points} monthly NAVs · {data.first_month?.slice(0, 7) ?? "—"} → {data.last_month?.slice(0, 7) ?? "—"}
            </Text>
          </View>
          {data.scheme_category && (
            <Text style={[type.bodySm, { color: colors.textTer, marginTop: 4 }]}>{data.scheme_category}</Text>
          )}
        </View>

        <View style={styles.headlineRow}>
          <HeadCell label="Headline FQS" labelKey="fqs" value={data.headline_fqs?.toFixed(0) ?? "—"} sub={data.headline_tier ?? "—"} color={tCol} />
          <HeadCell label="XIRR Inception" value={`${xirrPct}%`} sub={data.first_month?.slice(0, 7) ?? "—"} color={colors.tierElite} />
          <HeadCell label="Cycle Robust." labelKey="cycle_robustness" value={data.cycle_robustness?.toFixed(1) ?? "—"} sub="weighted Succ%" color={colors.azure} />
          <HeadCell label="Stability 10/5" labelKey="stability_check" value={data.stability_check?.toFixed(2) ?? "—"} sub="median ratio" color={colors.tierStrong} />
        </View>

        <ViewModeToggle />

        <Pressable
          onPress={() => setDatePickerOpen(true)}
          style={({ pressed }) => [styles.filterRow, pressed && { opacity: 0.85 }]}
        >
          <Filter size={14} color={colors.azure} />
          <Text style={[type.bodySm, { color: colors.textPri, marginLeft: 8, flex: 1 }]}>
            Start date filter: <Text style={{ fontWeight: "700" }}>{startDate ?? "since inception"}</Text>
          </Text>
          {startDate && (
            <Pressable onPress={() => setStartDate(null)} hitSlop={8}>
              <X size={14} color={colors.textTer} />
            </Pressable>
          )}
          <Calendar size={14} color={colors.textSec} style={{ marginLeft: 8 }} />
        </Pressable>

        {data.predecessor_available && !data.predecessors_included && (
          <Pressable
            onPress={() => setIncludePredecessors(true)}
            style={({ pressed }) => [
              styles.filterRow,
              { borderColor: colors.amber, marginTop: space.sm },
              pressed && { opacity: 0.85 },
            ]}
          >
            <Calendar size={14} color={colors.amber} />
            <Text style={[type.bodySm, { color: colors.textPri, marginLeft: 8, flex: 1 }]}>
              Older data available —{" "}
              <Text style={{ fontWeight: "700", color: colors.amber }}>tap to extend history</Text>
            </Text>
          </Pressable>
        )}
        {data.predecessors_included && (
          <Pressable
            onPress={() => setIncludePredecessors(false)}
            style={({ pressed }) => [
              styles.filterRow,
              { borderColor: colors.tierElite, marginTop: space.sm },
              pressed && { opacity: 0.85 },
            ]}
          >
            <Calendar size={14} color={colors.tierElite} />
            <Text style={[type.bodySm, { color: colors.textPri, marginLeft: 8, flex: 1 }]}>
              Full history loaded{" "}
              <Text style={{ fontWeight: "700", color: colors.tierElite }}>
                from {data.first_month?.slice(0, 7)}
              </Text>
            </Text>
            <Pressable onPress={() => setIncludePredecessors(false)} hitSlop={8}>
              <X size={14} color={colors.textTer} />
            </Pressable>
          </Pressable>
        )}

        <Text style={[type.label, { color: colors.textTer, paddingHorizontal: space.md, marginTop: space.md }]}>
          ROLLING HORIZON (1Y – 20Y)
        </Text>
        <ScrollView
          horizontal
          showsHorizontalScrollIndicator={false}
          contentContainerStyle={styles.horizonRow}
        >
          {Array.from({ length: 20 }, (_, i) => i + 1).map((h) => {
            const enabled = data.available_horizons.includes(h);
            const active = activeHorizon === h;
            const hr = data.horizons.find((x) => x.horizon === h);
            const col = hr ? scoreColor(hr.fqs) : colors.textDis;
            return (
              <Pressable
                key={h}
                disabled={!enabled}
                onPress={() => setActiveHorizon(h)}
                style={[
                  styles.horizonChip,
                  active && styles.horizonChipActive,
                  !enabled && { opacity: 0.25 },
                ]}
              >
                <Text style={[styles.horizonChipY, active && { color: colors.textPri }]}>{h}Y</Text>
                {hr && <Text style={[styles.horizonChipFqs, { color: col }]}>{hr.fqs.toFixed(0)}</Text>}
              </Pressable>
            );
          })}
        </ScrollView>

        {horizon && <HorizonView h={horizon} />}
      </ScrollView>

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

// ─── HorizonView ────────────────────────────────────────────────────────────

function HorizonView({ h }: { h: HorizonStats }) {
  const { mode } = useViewMode();
  const showAnalyst = mode === "analyst";
  return (
    <View>
      {/* Always-visible customer-facing widgets */}
      <FqsGauge score={h.fqs} tier={fqsTier(h.fqs)} horizon={h.horizon} />
      <PlainEnglishSummary h={h} />
      <SmartInsights h={h} />
      <WmHistogram h={h} />

      {/* Analyst-only deep dive */}
      {showAnalyst && (
        <>
          <CollapsibleSection
            title="EXPECTED RETURNS"
            subtitle="Where the typical SIP outcome lands"
            initiallyOpen
          >
            <DataRow label="Median WM" labelKey="median_wm" value={`${h.median_wm.toFixed(4)}x`} highlight />
            <DataRow label="Mean WM" labelKey="mean_wm" value={`${h.mean_wm.toFixed(4)}x`} />
            <DataRow label="P25 WM" labelKey="p25" value={`${h.p25_wm.toFixed(4)}x`} />
            <DataRow label="P75 WM" labelKey="p75" value={`${h.p75_wm.toFixed(4)}x`} />
            <DataRow label="Max WM" value={`${h.max_wm.toFixed(4)}x`} good />
            <DataRow label="P90 WM" labelKey="p90" value={`${h.p90_wm.toFixed(4)}x`} />
          </CollapsibleSection>

          <CollapsibleSection
            title="WORST CASE"
            subtitle="Bottom of the distribution"
          >
            <DataRow label="Min WM" value={`${h.min_wm.toFixed(4)}x`} bad={h.min_wm < h.min_wm_required} />
            <DataRow label="P10 WM" labelKey="p10" value={`${h.p10_wm.toFixed(4)}x`} bad={h.p10_wm < h.min_wm_required} />
            <DataRow label="ES10 (avg of bottom 10%)" labelKey="es10" value={`${h.es10.toFixed(4)}x`} />
            <DataRow label="Failure %" labelKey="failure_pct" value={`${(h.failure_pct * 100).toFixed(2)}%`} bad={h.failure_pct > 0} />
          </CollapsibleSection>

          <CollapsibleSection
            title="CONSISTENCY"
            subtitle="How tightly outcomes cluster"
          >
            <DataRow label="Std Dev" labelKey="std_wm" value={h.std_wm.toFixed(4)} />
            <DataRow label="CV (σ / mean)" labelKey="cv" value={`${(h.cv * 100).toFixed(2)}%`} />
            <DataRow label="Success %" labelKey="success_pct" value={`${(h.success_pct * 100).toFixed(2)}%`} good={h.success_pct === 1} highlight />
            <DataRow label="N Windows" labelKey="n_windows" value={String(h.n_windows)} />
            <DataRow label="Min WM Required" labelKey="min_wm_required" value={`${h.min_wm_required.toFixed(4)}x`} />
          </CollapsibleSection>

          <CollapsibleSection
            title="SCORE BREAKDOWN"
            subtitle="How the FQS is built (0–100 sub-scores)"
          >
            <DataRow label="Failure Score" labelKey="fail_score" value={h.fail_score.toFixed(1)} />
            <DataRow label="Downside Severity" labelKey="downside_severity" value={h.downside_severity.toFixed(1)} />
            <DataRow label="CV Score" labelKey="cv_score" value={h.cv_score.toFixed(1)} />
            <DataRow label="Risk Score" labelKey="risk_score" value={h.risk_score.toFixed(1)} />
            <DataRow label="Adj Risk Score" labelKey="adj_risk_score" value={h.adj_risk_score.toFixed(1)} />
            <DataRow label="Blended WM" labelKey="blended_wm" value={`${h.blended_wm.toFixed(4)}x`} />
            <DataRow label="Blended WM Score" labelKey="blended_wm_score" value={`${h.blended_wm_score.toFixed(1)} / cap ${h.cap}`} />
            <DataRow label="Downside Score" labelKey="downside_score" value={`${h.downside_score.toFixed(1)} / cap ${h.cap}`} />
            <DataRow label="Consistency Score" labelKey="consistency_score" value={h.consistency_score.toFixed(1)} />
            <DataRow label="ES Score" labelKey="es_score" value={h.es_score.toFixed(1)} />
          </CollapsibleSection>

          <CollapsibleSection
            title={`ROLLING ${h.horizon}Y SIP WINDOWS`}
            subtitle={`All ${h.n_windows} windows — every individual SIP simulation`}
            dense
          >
            <WindowsTable rows={h.windows} min={h.min_wm_required} />
          </CollapsibleSection>
        </>
      )}
    </View>
  );
}

// ─── WindowsTable (paginated + memoised) ───────────────────────────────────

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

// ─── small UI primitives ───────────────────────────────────────────────────

function HeadCell({
  label, labelKey, value, sub, color,
}: { label: string; labelKey?: string; value: string; sub: string; color: string }) {
  const { colors, type } = useTheme();
  const styles = useMemo(() => makeStyles(colors, type), [colors, type]);
  return (
    <View style={styles.headCell}>
      <View style={{ flexDirection: "row", alignItems: "center", justifyContent: "center" }}>
        <Text style={[type.label, { color: colors.textTer, fontSize: 8, letterSpacing: 0.8 }]} numberOfLines={2}>
          {label}
        </Text>
        {labelKey && <InfoHint term={labelKey} title={label} size={10} />}
      </View>
      <Text style={[styles.headCellVal, { color }]} numberOfLines={1}>{value}</Text>
      <Text style={[type.bodySm, { color: colors.textTer, fontSize: 10 }]} numberOfLines={1}>{sub}</Text>
    </View>
  );
}

function DataRow({
  label, labelKey, value, highlight, good, bad,
}: {
  label: string;
  labelKey?: string;        // glossary key — if set, renders an ⓘ tooltip
  value: string;
  highlight?: boolean;
  good?: boolean;
  bad?: boolean;
}) {
  const { colors, type } = useTheme();
  const styles = useMemo(() => makeStyles(colors, type), [colors, type]);
  const col = bad ? colors.tierWeak : good ? colors.tierElite : highlight ? colors.azure : colors.textPri;
  return (
    <View style={styles.dataRow}>
      <View style={{ flexDirection: "row", alignItems: "center", flex: 1 }}>
        <Text style={[type.body, { color: colors.textSec }]}>{label}</Text>
        {labelKey && <InfoHint term={labelKey} title={label} />}
      </View>
      <Text style={[styles.dataVal, { color: col, fontWeight: highlight || good || bad ? "700" : "600" }]}>
        {value}
      </Text>
    </View>
  );
}

function StartDateModal({
  open, onClose, onApply, initial, firstMonth, lastMonth,
}: {
  open: boolean; onClose: () => void; onApply: (d: string | null) => void;
  initial: string | null; firstMonth: string | null; lastMonth: string | null;
}) {
  const { colors, type } = useTheme();
  const modalStyles = useMemo(() => makeModalStyles(colors, type), [colors, type]);
  const [text, setText] = useState(initial ?? "");
  useEffect(() => { setText(initial ?? ""); }, [initial, open]);

  const presets: { label: string; value: string | null }[] = [
    { label: "Since inception", value: null },
    { label: "Last 10 years", value: backYears(10) },
    { label: "Last 5 years", value: backYears(5) },
    { label: "Last 3 years", value: backYears(3) },
    { label: "Since 2020", value: "2020-01-01" },
    { label: "Since 2015", value: "2015-01-01" },
  ];

  return (
    <Modal visible={open} animationType="slide" transparent onRequestClose={onClose}>
      <KeyboardAvoidingView
        behavior={Platform.OS === "ios" ? "padding" : "height"}
        style={{ flex: 1, justifyContent: "flex-end" }}
        keyboardVerticalOffset={Platform.OS === "ios" ? 0 : 0}
      >
        <TouchableWithoutFeedback onPress={onClose}>
          <View style={modalStyles.backdropFill} />
        </TouchableWithoutFeedback>
        <View style={modalStyles.sheet}>
          <TouchableWithoutFeedback onPress={Keyboard.dismiss}>
            <View>
              <View style={modalStyles.handle} />
              <Text style={[type.h3, { marginBottom: 4 }]}>Filter NAV history</Text>
              <Text style={[type.bodySm, { marginBottom: space.md }]}>
                Recompute all horizons starting from a given date.
              </Text>
              <Text style={[type.label, { color: colors.textTer, marginBottom: 6 }]}>PRESETS</Text>
              <View style={modalStyles.presets}>
                {presets.map((p) => (
                  <Pressable
                    key={p.label}
                    onPress={() => onApply(p.value)}
                    style={({ pressed }) => [modalStyles.preset, pressed && { opacity: 0.85 }]}
                  >
                    <Text style={modalStyles.presetText}>{p.label}</Text>
                  </Pressable>
                ))}
              </View>
              <Text style={[type.label, { color: colors.textTer, marginTop: space.md, marginBottom: 6 }]}>
                CUSTOM (YYYY-MM-DD)
              </Text>
              <TextInput
                style={modalStyles.input}
                placeholder={firstMonth ? `e.g. ${firstMonth.slice(0, 10)}` : "YYYY-MM-DD"}
                placeholderTextColor={colors.textTer}
                value={text}
                onChangeText={setText}
                autoCorrect={false}
                autoCapitalize="none"
                returnKeyType="done"
                onSubmitEditing={() => onApply(text.trim() || null)}
              />
              <Text style={[type.bodySm, { color: colors.textTer, marginTop: 4 }]}>
                NAV available {firstMonth ?? "?"} → {lastMonth ?? "?"}
              </Text>
              <View style={{ flexDirection: "row", gap: space.sm, marginTop: space.lg }}>
                <Pressable style={[modalStyles.btn, modalStyles.btnGhost]} onPress={onClose}>
                  <Text style={[type.body, { color: colors.textSec, fontWeight: "700" }]}>CANCEL</Text>
                </Pressable>
                <Pressable
                  style={[modalStyles.btn, modalStyles.btnPrimary]}
                  onPress={() => onApply(text.trim() || null)}
                >
                  <Text style={[type.body, { color: colors.bg, fontWeight: "700" }]}>APPLY</Text>
                </Pressable>
              </View>
            </View>
          </TouchableWithoutFeedback>
        </View>
      </KeyboardAvoidingView>
    </Modal>
  );
}

// ─── skeleton placeholder ──────────────────────────────────────────────────

function SkeletonScreen() {
  const { colors, type } = useTheme();
  const styles = useMemo(() => makeStyles(colors, type), [colors, type]);
  const bar = { backgroundColor: colors.bgElev, borderRadius: 4 } as const;
  return (
    <SafeAreaView style={styles.safe} edges={["bottom"]}>
      <Stack.Screen options={{ title: "Analysing…" }} />
      <View style={{ paddingHorizontal: space.md, paddingTop: space.md }}>
        <View style={[bar, { width: 90, height: 10 }]} />
        <View style={[bar, { width: "85%", height: 22, marginTop: 10 }]} />
        <View style={[bar, { width: "60%", height: 14, marginTop: 12 }]} />
      </View>
      <View style={styles.headlineRow}>
        {[0, 1, 2, 3].map((i) => (
          <View key={i} style={[styles.headCell, { gap: 6 }]}>
            <View style={[bar, { width: 60, height: 8 }]} />
            <View style={[bar, { width: 44, height: 20, marginTop: 4 }]} />
            <View style={[bar, { width: 36, height: 8 }]} />
          </View>
        ))}
      </View>
      <View style={[styles.filterRow, { backgroundColor: colors.bgCard }]}>
        <View style={[bar, { width: "70%", height: 12 }]} />
      </View>
      <View style={{ flexDirection: "row", gap: space.sm, paddingHorizontal: space.md, marginTop: space.md }}>
        {[0, 1, 2, 3, 4, 5].map((i) => (
          <View key={i} style={[bar, { width: 56, height: 44, borderRadius: radii.sm }]} />
        ))}
      </View>
      <View style={[styles.section, { gap: 10 }]}>
        {[0, 1, 2, 3, 4, 5].map((i) => (
          <View key={i} style={[bar, { width: "100%", height: 14, marginTop: 6 }]} />
        ))}
      </View>
      <View style={{ flex: 1, alignItems: "center", justifyContent: "flex-end", paddingBottom: space.xl }}>
        <ActivityIndicator color={colors.textSec} />
        <Text style={[type.bodySm, { color: colors.textTer, marginTop: 8 }]}>
          Crunching every rolling SIP window 1Y–20Y…
        </Text>
      </View>
    </SafeAreaView>
  );
}

// ─── helpers ───────────────────────────────────────────────────────────────

function backYears(n: number): string {
  const d = new Date(); d.setFullYear(d.getFullYear() - n);
  return d.toISOString().slice(0, 10);
}

function fmtINR(v: number): string {
  if (v >= 1e7) return `${(v / 1e7).toFixed(2)}Cr`;
  if (v >= 1e5) return `${(v / 1e5).toFixed(2)}L`;
  return `₹${Math.round(v).toLocaleString("en-IN")}`;
}

function shortErr(msg: string): string {
  const stripped = msg.replace(/^HTTP \d+:\s*/i, "").trim();
  return stripped.length > 140 ? stripped.slice(0, 140) + "…" : stripped;
}

function fqsTier(score: number): string {
  if (score >= 90) return "Elite";
  if (score >= 80) return "Strong";
  if (score >= 70) return "Good";
  if (score >= 60) return "Average";
  return "Weak";
}

// ─── style factories ──────────────────────────────────────────────────────

function makeStyles(colors: ThemeColors, type: ThemeType) {
  return StyleSheet.create({
    safe: { flex: 1, backgroundColor: colors.bg },
    header: { paddingHorizontal: space.md, paddingTop: space.sm },
    fundName: { ...type.h2, marginTop: 4, fontSize: 20, lineHeight: 26 },
    chipsRow: { flexDirection: "row", alignItems: "center", gap: space.sm, marginTop: 8, flexWrap: "wrap" },
    chip: { paddingHorizontal: 10, paddingVertical: 4, borderRadius: radii.pill, borderWidth: 1 },
    chipText: { fontSize: 11, fontWeight: "700", letterSpacing: 1, textTransform: "uppercase" },
    metaText: { ...type.bodySm, color: colors.textTer },
    headlineRow: {
      flexDirection: "row", marginHorizontal: space.md, marginTop: space.md,
      backgroundColor: colors.bgCard, borderRadius: radii.md,
      borderWidth: 1, borderColor: colors.border, paddingVertical: space.md,
    },
    headCell: { flex: 1, alignItems: "center", gap: 2, paddingHorizontal: 4 },
    headCellVal: { fontSize: 20, fontWeight: "800", fontVariant: ["tabular-nums"], letterSpacing: -0.5 },
    filterRow: {
      flexDirection: "row", alignItems: "center",
      marginHorizontal: space.md, marginTop: space.md,
      paddingHorizontal: space.md, paddingVertical: 12,
      backgroundColor: colors.bgCard, borderRadius: radii.sm,
      borderWidth: 1, borderColor: colors.border,
    },
    horizonRow: { paddingHorizontal: space.md, paddingVertical: space.sm, gap: space.sm },
    horizonChip: {
      minWidth: 56, paddingHorizontal: 12, paddingVertical: 8,
      backgroundColor: colors.bgCard, borderRadius: radii.sm,
      borderWidth: 1, borderColor: colors.border, alignItems: "center",
    },
    horizonChipActive: { borderColor: colors.azure, backgroundColor: colors.azureSoft },
    horizonChipY: { ...type.body, fontWeight: "700", color: colors.textSec },
    horizonChipFqs: { fontSize: 11, fontWeight: "700", fontVariant: ["tabular-nums"], marginTop: 2 },
    section: { marginHorizontal: space.md, marginTop: space.md },
    sectionBody: {
      padding: space.lg, backgroundColor: colors.bgCard, borderRadius: radii.md,
      borderWidth: 1, borderColor: colors.border,
    },
    dataRow: {
      flexDirection: "row", alignItems: "center", justifyContent: "space-between",
      paddingVertical: 8, borderBottomWidth: StyleSheet.hairlineWidth, borderColor: colors.border,
    },
    dataVal: { fontSize: 14, fontWeight: "600", color: colors.textPri, fontVariant: ["tabular-nums"], textAlign: "right" },
    fqsBox: {
      marginTop: space.md, paddingHorizontal: space.md, paddingVertical: space.lg,
      borderRadius: radii.md, borderWidth: 1,
      alignItems: "center", overflow: "hidden",
    },
    fqsGlow: {
      position: "absolute", top: -40, left: -40, right: -40, height: 120,
      borderRadius: 80, opacity: 0.6,
    },
    fqsBig: { fontSize: 52, fontWeight: "800", fontVariant: ["tabular-nums"], letterSpacing: -2, marginTop: 4 },
    banner: {
      flexDirection: "row", alignItems: "center",
      marginHorizontal: space.md, marginTop: space.sm,
      paddingHorizontal: space.md, paddingVertical: 10,
      backgroundColor: "rgba(217,119,6,0.12)",
      borderRadius: radii.sm,
      borderWidth: 1, borderColor: "rgba(217,119,6,0.35)",
    },
    errorWrap: {
      flex: 1, alignItems: "center", justifyContent: "center",
      paddingHorizontal: space.xl,
    },
    errorIcon: {
      width: 64, height: 64, borderRadius: 32,
      backgroundColor: "rgba(220,38,38,0.12)",
      borderWidth: 1, borderColor: "rgba(220,38,38,0.35)",
      alignItems: "center", justifyContent: "center",
    },
    retryBtn: {
      flexDirection: "row", alignItems: "center",
      marginTop: space.lg, paddingHorizontal: space.lg, paddingVertical: 12,
      backgroundColor: colors.bgElev, borderRadius: radii.md,
      borderWidth: 1, borderColor: colors.borderStrong,
    },
  });
}

function makeTableStyles(colors: ThemeColors) {
  return StyleSheet.create({
    headerRow: { backgroundColor: colors.bgElev, borderTopLeftRadius: radii.md, borderTopRightRadius: radii.md },
    header: { fontWeight: "800", color: colors.textSec, fontSize: 10, letterSpacing: 1, textTransform: "uppercase" },
    row: {
      flexDirection: "row", paddingVertical: 8, paddingHorizontal: 8,
      borderBottomWidth: StyleSheet.hairlineWidth, borderColor: colors.border,
      alignItems: "center",
    },
    rowAlt: { backgroundColor: colors.rowAlt },
    cellNum: { width: 32, color: colors.textTer, fontVariant: ["tabular-nums"], fontSize: 11 },
    cellDate: { width: 92, color: colors.textPri, fontVariant: ["tabular-nums"], fontSize: 12 },
    cell: { width: 80, color: colors.textPri, fontVariant: ["tabular-nums"], fontSize: 12, textAlign: "right", paddingHorizontal: 4 },
    cellWide: { width: 96, color: colors.textPri, fontVariant: ["tabular-nums"], fontSize: 12, textAlign: "right", paddingHorizontal: 4 },
    cellFlag: { width: 28, alignItems: "center" },
    expandBtn: {
      flexDirection: "row", alignItems: "center", justifyContent: "center",
      paddingVertical: 12, paddingHorizontal: space.md,
      borderTopWidth: StyleSheet.hairlineWidth, borderColor: colors.border,
    },
  });
}

function makeModalStyles(colors: ThemeColors, type: ThemeType) {
  return StyleSheet.create({
    // Old absolute-positioned backdrop kept for reference; replaced by backdropFill
    // when wrapped in a KeyboardAvoidingView flex layout.
    backdropFill: { flex: 1, backgroundColor: colors.backdrop },
    sheet: {
      backgroundColor: colors.bgCard, padding: space.lg,
      borderTopLeftRadius: 20, borderTopRightRadius: 20,
      borderWidth: 1, borderColor: colors.border,
    },
    handle: {
      alignSelf: "center", width: 40, height: 4, borderRadius: 2,
      backgroundColor: colors.border, marginBottom: space.md,
    },
    presets: { flexDirection: "row", flexWrap: "wrap", gap: space.sm },
    preset: {
      paddingHorizontal: 12, paddingVertical: 8, backgroundColor: colors.bg,
      borderRadius: radii.pill, borderWidth: 1, borderColor: colors.border,
    },
    presetText: { ...type.bodySm, color: colors.textPri, fontWeight: "600" },
    input: {
      paddingHorizontal: space.md, paddingVertical: 12,
      backgroundColor: colors.bg, borderRadius: radii.sm,
      borderWidth: 1, borderColor: colors.border,
      color: colors.textPri, fontSize: 16, fontVariant: ["tabular-nums"],
    },
    btn: { flex: 1, paddingVertical: 14, borderRadius: radii.md, alignItems: "center" },
    btnGhost: { backgroundColor: colors.bg, borderWidth: 1, borderColor: colors.border },
    btnPrimary: { backgroundColor: colors.textPri },
  });
}
