/**
 * Shared UI for the Fund Details screen (SIP and Lumpsum modes).
 *
 * The two modes analyse a fund with different engines and different result
 * shapes (HorizonStats vs LumpsumHorizonStats), so the calculation-specific
 * views (HorizonView/LumpsumHorizonView, WindowsTable/LumpsumWindowsTable)
 * stay in app/fund/[code].tsx. Everything here is genuinely mode-agnostic
 * chrome that was previously copy-pasted between SipFundDetailScreen and
 * LumpsumFundDetailScreen — error/loading states, the wound-up/merger
 * banners, the start-date filter row, the 1Y–50Y horizon chip strip, the
 * date-filter modal, and the small DataRow/HeadCell primitives both
 * calculation views render into.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  View, Text, StyleSheet, ScrollView, Pressable, Modal, TextInput,
  KeyboardAvoidingView, Platform, Keyboard, TouchableWithoutFeedback, ActivityIndicator,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Stack } from "expo-router";
import { Calendar, RefreshCw, X, CheckCircle2, AlertTriangle, Filter, Info, History, PlusCircle, Briefcase, GitBranch } from "lucide-react-native";
import { radii, space, type ThemeColors, type ThemeType } from "@/src/theme";
import { useTheme } from "@/src/ThemeProvider";
import { InfoHint } from "@/src/components/InfoHint";
import { api, type AmcPeriods } from "@/src/api";
import { CollapsibleSection } from "@/src/components/CollapsibleSection";

// ─── helpers ──────────────────────────────────────────────────────────────

export function backYears(n: number): string {
  const d = new Date(); d.setFullYear(d.getFullYear() - n);
  return d.toISOString().slice(0, 10);
}

/** Route params Home (and other screens) pass to Fund Detail. */
export type DetailRouteParams = {
  code: string;
  horizon?: string;
  since2010?: string;
  mgmt?: string;
  contDate?: string;
  /** A Home card's chosen AMC period start (YYYY-MM-DD): score from that
   *  date across every management since, predecessor history included. */
  amcStart?: string;
};

/** Translate Home's card state into this screen's (startDate,
 *  includePredecessor) and the note explaining why they're pre-set.
 *
 *  - amcStart: the AMC period the advisor picked on the card.
 *  - mgmt + contDate: the latest verified break; own-code history only,
 *    matching fqs_refresh's continuity pass.
 *  - since2010: start forced to 2010-01-01, matching fqs_by_horizon_2010. */
export function initialBasis(p: Partial<DetailRouteParams>): {
  startDate: string | null; includePredecessor: boolean; note: string | null;
} {
  const suffix = "matching your Home screen selection.";
  if (p.amcStart) {
    return {
      startDate: p.amcStart,
      includePredecessor: true,
      note: `AMC period — scored from ${p.amcStart} onward, ${suffix}`,
    };
  }
  if (p.mgmt === "1" && p.contDate) {
    return {
      startDate: p.contDate,
      includePredecessor: false,
      note: `Separate management periods — showing history from ${p.contDate} onward, ${suffix}`,
    };
  }
  if (p.since2010 === "1") {
    return {
      startDate: "2010-01-01",
      includePredecessor: true,
      note: `Since 2010 — showing history from 2010-01-01 onward, ${suffix}`,
    };
  }
  return { startDate: null, includePredecessor: true, note: null };
}

/** Sequence guard for "latest request wins". `next()` tags a request,
 *  `isLatest(n)` says whether a newer one has started since. */
export function useLatestRequest() {
  const seq = useRef(0);
  const next = useCallback(() => ++seq.current, []);
  const isLatest = useCallback((n: number) => n === seq.current, []);
  return useMemo(() => ({ next, isLatest }), [next, isLatest]);
}

/** Inline notice while a filter change is being recomputed, so the numbers
 *  below are never silently stale. */
export function RecomputingNotice({ visible, startDate }: { visible: boolean; startDate: string | null }) {
  const { colors, type } = useTheme();
  if (!visible) return null;
  return (
    <View
      style={{
        flexDirection: "row", alignItems: "center", gap: 8,
        marginHorizontal: space.md, marginTop: space.sm,
        paddingHorizontal: space.md, paddingVertical: 10,
        borderRadius: radii.md, backgroundColor: colors.azureSoft,
      }}
      accessibilityLiveRegion="polite"
    >
      <ActivityIndicator size="small" color={colors.azure} />
      <Text style={[type.bodySm, { color: colors.azure, fontWeight: "600", flex: 1 }]}>
        Recalculating {startDate ? `from ${startDate}` : "for full history"}… numbers below update when ready.
      </Text>
    </View>
  );
}

export function shortErr(msg: string): string {
  const stripped = msg.replace(/^HTTP \d+:\s*/i, "").trim();
  return stripped.length > 140 ? stripped.slice(0, 140) + "…" : stripped;
}

// ─── style factories ──────────────────────────────────────────────────────

export function makeStyles(colors: ThemeColors, type: ThemeType) {
  return StyleSheet.create({
    safe: { flex: 1, backgroundColor: colors.bg },
    header: { paddingHorizontal: space.md, paddingTop: space.sm },
    fundName: { ...type.h2, marginTop: 4, fontSize: 20, lineHeight: 26 },
    chipsRow: { flexDirection: "row", alignItems: "center", gap: space.sm, marginTop: 8, flexWrap: "wrap" },
    chip: { paddingHorizontal: 10, paddingVertical: 4, borderRadius: radii.pill, borderWidth: 1 },
    chipText: { fontSize: 11, fontWeight: "700", letterSpacing: 1, textTransform: "uppercase" },
    metaText: { ...type.bodySm, color: colors.textTer },

    // Shown when navigated from the home screen — reminds user which horizon was pre-selected
    contextPill: {
      flexDirection: "row",
      alignItems: "center",
      marginHorizontal: space.md,
      marginTop: space.sm,
      paddingHorizontal: space.md,
      paddingVertical: 8,
      backgroundColor: colors.azureSoft,
      borderRadius: radii.sm,
      borderWidth: 1,
      borderColor: colors.azure + "44",
    },

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
    // Dashed border to signal "this is where you came from" without stealing focus
    horizonChipContext: {
      borderColor: colors.azure + "66",
      borderStyle: "dashed",
    },
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
    toggleTrack: {
      width: 40, height: 22, borderRadius: 11, borderWidth: 1,
      justifyContent: "center", padding: 2, marginLeft: 8,
    },
    toggleThumb: {
      width: 16, height: 16, borderRadius: 8,
    },
  });
}

export function makeTableStyles(colors: ThemeColors) {
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

export function makeModalStyles(colors: ThemeColors, type: ThemeType) {
  return StyleSheet.create({
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

// ─── shared primitives (used by both HorizonView and LumpsumHorizonView) ───

export function HeadCell({
  label, labelKey, value, sub, color,
}: { label: string; labelKey?: string; value: string; sub: string; color: string }) {
  const { colors, type } = useTheme();
  const styles = useMemo(() => makeStyles(colors, type), [colors, type]);
  return (
    <View style={styles.headCell}>
      <View style={{ flexDirection: "row", alignItems: "center", gap: 3 }}>
        <Text style={[type.label, { color: colors.textTer, fontSize: 9.5 }]} numberOfLines={1}>{label}</Text>
        {labelKey && <InfoHint term={labelKey} title={label} />}
      </View>
      <Text style={[styles.headCellVal, { color }]} numberOfLines={1} adjustsFontSizeToFit>{value}</Text>
      <Text style={[type.bodySm, { color: colors.textTer, fontSize: 10 }]} numberOfLines={1}>{sub}</Text>
    </View>
  );
}

export function DataRow({
  label, labelKey, value, highlight, good, bad,
}: {
  label: string;
  labelKey?: string;
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

// ─── error / loading states ─────────────────────────────────────────────────

export function FundDetailErrorView({
  error, onRetry, onBack,
}: { error: string; onRetry: () => void; onBack: () => void }) {
  const { colors, type } = useTheme();
  const styles = useMemo(() => makeStyles(colors, type), [colors, type]);
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
          onPress={onRetry}
          style={({ pressed }) => [styles.retryBtn, pressed && { opacity: 0.85 }]}
        >
          <RefreshCw size={14} color={colors.textPri} />
          <Text style={[type.body, { color: colors.textPri, fontWeight: "700", marginLeft: 8 }]}>RETRY</Text>
        </Pressable>
        <Pressable onPress={onBack} style={{ marginTop: space.md }}>
          <Text style={[type.bodySm, { color: colors.textTer }]}>← Go back</Text>
        </Pressable>
      </View>
    </SafeAreaView>
  );
}

/** bottomText differentiates the SIP ("rolling SIP window") vs Lumpsum
 * ("lumpsum rolling window") copy while sharing the same rich skeleton —
 * Lumpsum previously fell back to a bare spinner instead of this. */
export function FundDetailSkeleton({ bottomText }: { bottomText: string }) {
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
        <Text style={[type.bodySm, { color: colors.textTer, marginTop: 8 }]}>{bottomText}</Text>
      </View>
    </SafeAreaView>
  );
}

// ─── wound-up / merger / history-note banners ───────────────────────────────

export function FundDetailBanners({
  historyNote, woundUp, woundUpDate, mergedWith, predecessorUsed,
}: {
  historyNote?: string | null;
  woundUp: boolean;
  woundUpDate: string | null;
  mergedWith: number | null;
  predecessorUsed?: number[] | null;
}) {
  const { colors, type } = useTheme();
  const styles = useMemo(() => makeStyles(colors, type), [colors, type]);
  return (
    <>
      {historyNote && (
        <View style={[styles.filterRow, { borderColor: colors.azure, backgroundColor: `${colors.azure}14`, marginBottom: space.sm }]}>
          <Info size={14} color={colors.azure} />
          <Text style={[type.bodySm, { color: colors.textPri, marginLeft: 8, flex: 1 }]}>
            {historyNote}
          </Text>
        </View>
      )}
      {woundUp && (
        <View style={[styles.filterRow, { borderColor: colors.amber, backgroundColor: `${colors.amber}18`, marginBottom: space.sm }]}>
          <AlertTriangle size={14} color={colors.amber} />
          <Text style={[type.bodySm, { color: colors.amber, marginLeft: 8, flex: 1, fontWeight: "600" }]}>
            Fund wound up on {woundUpDate?.slice(0, 7)}.{" "}
            <Text style={{ fontWeight: "400", color: colors.textPri }}>
              This scheme was merged/wound up. Search for its active successor to see current data.
            </Text>
          </Text>
        </View>
      )}
      {(mergedWith || (predecessorUsed && predecessorUsed.length > 0)) && (
        <View style={[styles.filterRow, { borderColor: colors.tierElite, backgroundColor: `${colors.tierElite}18`, marginBottom: space.sm }]}>
          <CheckCircle2 size={14} color={colors.tierElite} />
          <Text style={[type.bodySm, { color: colors.textPri, marginLeft: 8, flex: 1 }]}>
            Continuous history loaded —{" "}
            <Text style={{ fontWeight: "700", color: colors.tierElite }}>
              merged/renamed scheme stitched into one verified series
            </Text>
          </Text>
        </View>
      )}
    </>
  );
}

// ─── start-date filter row + modal ──────────────────────────────────────────

export function StartDateFilterRow({
  startDate, onOpen, onClear, busy = false, label = "Start date filter:",
}: {
  startDate: string | null;
  onOpen: () => void;
  onClear: () => void;
  busy?: boolean;
  label?: string;
}) {
  const { colors, type } = useTheme();
  const styles = useMemo(() => makeStyles(colors, type), [colors, type]);
  // The clear button is a SIBLING of the row's press target, not nested in
  // it: nested, a tap on the X also bubbled to the row on web and reopened
  // the date modal pre-filled with the date just cleared.
  return (
    <View style={styles.filterRow}>
      <Pressable
        onPress={onOpen}
        accessibilityRole="button"
        accessibilityLabel="Change start date filter"
        style={({ pressed }) => [{ flex: 1, flexDirection: "row", alignItems: "center" }, pressed && { opacity: 0.85 }]}
      >
        <Filter size={14} color={colors.azure} />
        <Text style={[type.bodySm, { color: colors.textPri, marginLeft: 8, flex: 1 }]}>
          {label} <Text style={{ fontWeight: "700" }}>{startDate ?? "since inception"}</Text>
        </Text>
        {busy
          ? <ActivityIndicator size="small" color={colors.azure} style={{ marginLeft: 8 }} />
          : <Calendar size={14} color={colors.textSec} style={{ marginLeft: 8 }} />}
      </Pressable>
      {startDate && (
        <Pressable
          onPress={onClear}
          hitSlop={10}
          accessibilityRole="button"
          accessibilityLabel="Remove start date filter"
          style={{ marginLeft: 10, padding: 2 }}
        >
          <X size={15} color={colors.textSec} />
        </Pressable>
      )}
    </View>
  );
}

// ─── predecessor / successor NAV history toggle ─────────────────────────────

/** Only render this row when the fund actually HAS a verified predecessor
 *  (data.predecessor_available?.length) — otherwise there's nothing to
 *  toggle and the row would be a dead control. */
export function PredecessorToggleRow({
  includePredecessor, onChange, predecessorAvailable,
}: {
  includePredecessor: boolean;
  onChange: (v: boolean) => void;
  predecessorAvailable: { scheme_code: number; first_date: string; last_date: string; points: number }[];
}) {
  const { colors, type } = useTheme();
  const styles = useMemo(() => makeStyles(colors, type), [colors, type]);
  if (!predecessorAvailable.length) return null;

  const oldest = predecessorAvailable[predecessorAvailable.length - 1];

  return (
    <Pressable
      onPress={() => onChange(!includePredecessor)}
      style={({ pressed }) => [styles.filterRow, pressed && { opacity: 0.85 }]}
    >
      <History size={14} color={colors.azure} />
      <Text style={[type.bodySm, { color: colors.textPri, marginLeft: 8, flex: 1 }]}>
        {includePredecessor
          ? <>Full history <Text style={{ fontWeight: "700" }}>(since {oldest.first_date.slice(0, 7)}, includes predecessor management)</Text></>
          : <>New management only <Text style={{ fontWeight: "700" }}>(predecessor history excluded)</Text></>}
      </Text>
      <View
        style={[
          styles.toggleTrack,
          { backgroundColor: includePredecessor ? colors.azure : colors.bgElev, borderColor: colors.border },
        ]}
      >
        <View
          style={[
            styles.toggleThumb,
            { backgroundColor: colors.bgCard, alignSelf: includePredecessor ? "flex-end" : "flex-start" },
          ]}
        />
      </View>
    </Pressable>
  );
}

// ─── AMC history timeline ────────────────────────────────────────────────────
//
// Backed by GET /funds/{code}/lineage (backend/lineage_store.py), populated
// by scripts/build_lineage.py (POST /api/admin/build-lineage). Self-fetches
// rather than taking the history as a prop: it is a separate, cheap DB read
// kept out of the hot analyze() path, not part of FundAnalysis/
// LumpsumFundAnalysis.
//
// Requirement covered: "view AMC history from 2000 to current year, identify
// if/when it changed, select the year from which data should be considered."
// Tapping a change date does NOT introduce a new selection mechanism — it
// calls the SAME onSelectYear -> setStartDate the existing StartDateModal
// already uses, so "select this year" and "add to portfolio from here" reuse
// the app's one existing start-date/portfolio flow end to end.

/** "2016-09-20" → "2016–2026": the period an AMC break opens, up to today. */
export function amcPeriodLabel(isoDate: string): string {
  return `${isoDate.slice(0, 4)}–${new Date().getFullYear()}`;
}

export function AmcHistorySection({
  schemeCode, activeStart, onSelectPeriod,
}: {
  schemeCode: number;
  /** The start date currently applied on screen — highlights the matching period. */
  activeStart: string | null;
  /** Re-runs the screen's own start-date filter from the period's start
   *  (null = full history), so a period choice and the date picker are the
   *  same mechanism end to end. */
  onSelectPeriod?: (isoDate: string | null) => void;
}) {
  const { colors, type } = useTheme();
  const [state, setState] = useState<{
    loading: boolean; data: AmcPeriods | null; error: string | null;
  }>({ loading: true, data: null, error: null });

  useEffect(() => {
    let cancelled = false;
    setState({ loading: true, data: null, error: null });
    api.amcPeriods(schemeCode)
      .then((d) => { if (!cancelled) setState({ loading: false, data: d, error: null }); })
      .catch((e: any) => {
        if (!cancelled) setState({ loading: false, data: null, error: shortErr(String(e?.message || e)) });
      });
    return () => { cancelled = true; };
  }, [schemeCode]);

  const breaks = state.data?.breaks ?? [];

  return (
    <CollapsibleSection
      // Remounted once data arrives so a fund WITH management changes opens
      // expanded — the period choice is the point of this section.
      key={state.data ? "loaded" : "pending"}
      title="AMC History & Periods"
      subtitle={
        state.data
          ? breaks.length
            ? `${breaks.length} management change${breaks.length === 1 ? "" : "s"} on record — pick a starting period`
            : "No AMC change on record"
          : state.loading ? "Loading…" : undefined
      }
      initiallyOpen={breaks.length > 0}
    >
      {state.loading && (
        <View style={{ paddingVertical: 16, alignItems: "center" }}>
          <ActivityIndicator color={colors.azure} />
        </View>
      )}
      {state.error && (
        <Text style={[type.bodySm, { color: colors.amber, padding: space.sm }]}>{state.error}</Text>
      )}
      {state.data && (
        <View style={{ padding: space.sm, gap: 8 }}>
          {state.data.current_amc ? (
            <Text style={[type.bodySm, { color: colors.textPri }]}>
              Currently managed by <Text style={{ fontWeight: "700" }}>{state.data.current_amc}</Text>
            </Text>
          ) : null}
          {breaks.length === 0 ? (
            <Text style={[type.bodySm, { color: colors.textSec }]}>
              No AMC change or merger is recorded for this scheme, so its whole history
              belongs to the current fund house.
            </Text>
          ) : (
            <>
              <Text style={[type.bodySm, { color: colors.textSec }]}>
                Each period scores the fund from that change onward, so performance from
                before the change does not count. Pick the one you want to judge it on.
              </Text>
              {breaks.map((b) => (
                <PeriodOption
                  key={b.date}
                  label={amcPeriodLabel(b.date)}
                  sub={`From ${b.date}${b.from && b.to ? ` · ${b.from} → ${b.to}` : ` · ${b.reason}`}`}
                  active={activeStart === b.date}
                  onPress={onSelectPeriod ? () => onSelectPeriod(b.date) : undefined}
                />
              ))}
              <PeriodOption
                label="Full history"
                sub={state.data.first_nav_date ? `Since ${state.data.first_nav_date}, every management` : "Every management since inception"}
                active={activeStart === null}
                onPress={onSelectPeriod ? () => onSelectPeriod(null) : undefined}
              />
            </>
          )}
          {state.data.caveat ? (
            <Text style={[type.bodySm, { color: colors.textTer, fontStyle: "italic" }]}>
              {state.data.caveat}
            </Text>
          ) : null}
        </View>
      )}
    </CollapsibleSection>
  );
}

function PeriodOption({
  label, sub, active, onPress,
}: { label: string; sub: string; active: boolean; onPress?: () => void }) {
  const { colors, type } = useTheme();
  return (
    <Pressable
      onPress={onPress}
      disabled={!onPress}
      accessibilityRole="radio"
      accessibilityState={{ selected: active }}
      style={({ pressed }) => ({
        flexDirection: "row", alignItems: "center", gap: 10,
        paddingHorizontal: space.md, paddingVertical: 10,
        borderRadius: radii.md, borderWidth: 1.5,
        borderColor: active ? colors.azure : colors.border,
        backgroundColor: active ? colors.azureSoft : colors.bgCard,
        opacity: pressed ? 0.85 : 1,
      })}
    >
      <GitBranch size={14} color={active ? colors.azure : colors.textSec} />
      <View style={{ flex: 1 }}>
        <Text style={{ fontSize: 14, fontWeight: "800", color: active ? colors.azure : colors.textPri }}>{label}</Text>
        <Text style={[type.bodySm, { color: colors.textTer, marginTop: 2 }]} numberOfLines={2}>{sub}</Text>
      </View>
      {active && <CheckCircle2 size={16} color={colors.azure} />}
    </Pressable>
  );
}

export function StartDateModal({
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

// ─── 1Y–50Y rolling-horizon chip strip ──────────────────────────────────────

const ALL_HORIZONS = Array.from({ length: 50 }, (_, i) => i + 1);

export function HorizonChipRow({
  availableHorizons, activeHorizon, onSelect, getFqs, contextHorizon,
}: {
  availableHorizons: number[];
  activeHorizon: number | null;
  onSelect: (h: number) => void;
  /** FQS at horizon h, for the chip's colour + sub-label, or undefined if not computed. */
  getFqs: (h: number) => number | undefined;
  /** SIP-only: highlights the horizon the user arrived with from Home. */
  contextHorizon?: number | null;
}) {
  const { colors, type, scoreColor } = useTheme();
  const styles = useMemo(() => makeStyles(colors, type), [colors, type]);
  return (
    <>
      <Text style={[type.label, { color: colors.textTer, paddingHorizontal: space.md, marginTop: space.md }]}>
        ROLLING HORIZON (1Y – 50Y)
      </Text>
      <ScrollView
        horizontal
        showsHorizontalScrollIndicator={false}
        contentContainerStyle={styles.horizonRow}
      >
        {ALL_HORIZONS.map((h) => {
          const enabled = availableHorizons.includes(h);
          const active = activeHorizon === h;
          const isContext = contextHorizon != null && h === contextHorizon && !active;
          const fqs = getFqs(h);
          const col = fqs != null ? scoreColor(fqs) : colors.textDis;
          return (
            <Pressable
              key={h}
              disabled={!enabled}
              onPress={() => onSelect(h)}
              style={[
                styles.horizonChip,
                active && styles.horizonChipActive,
                isContext && styles.horizonChipContext,
                !enabled && { opacity: 0.25 },
              ]}
            >
              <Text style={[
                styles.horizonChipY,
                active && { color: colors.textPri },
                isContext && { color: colors.azure },
              ]}>
                {h}Y
              </Text>
              {fqs != null && (
                <Text style={[styles.horizonChipFqs, { color: col }]}>
                  {fqs.toFixed(0)}
                </Text>
              )}
            </Pressable>
          );
        })}
      </ScrollView>
    </>
  );
}

// ─── Add-to-portfolio (Home's selection flow, reachable from Fund Detail) ──
//
// Home's Select mode lets an advisor build a proposal by toggling funds from
// the ranked list; these two share that exact same ProposalDraftProvider
// selection state so a fund can be added while browsing its own detail page
// too — no need to back out to Home, enter Select mode, and find it again.

export function AddToPortfolioButton({
  selected, onToggle, startDate = null,
}: { selected: boolean; onToggle: () => void; startDate?: string | null }) {
  const { colors } = useTheme();
  return (
    <Pressable
      onPress={onToggle}
      style={({ pressed }) => [
        addS.btn,
        {
          backgroundColor: selected ? `${colors.tierElite}1f` : colors.azure,
          borderWidth: selected ? 1.5 : 0,
          borderColor: colors.tierElite,
          opacity: pressed ? 0.85 : 1,
        },
      ]}
    >
      {selected
        ? <CheckCircle2 size={16} color={colors.tierElite} />
        : <PlusCircle size={16} color="#fff" />}
      <Text style={[addS.btnText, { color: selected ? colors.tierElite : "#fff" }]}>
        {selected ? "Added to Portfolio — tap to remove" : "Add to Portfolio"}
        {startDate ? ` · from ${startDate}` : ""}
      </Text>
    </Pressable>
  );
}

/** Fixed footer, only rendered once at least one fund is selected — mirrors
 *  Home's selection-mode header (Cancel / Build Proposal) so the advisor can
 *  jump straight to the builder without returning to Home at all. */
export function PortfolioSelectionBar({
  count, onBuild,
}: { count: number; onBuild: () => void }) {
  const { colors } = useTheme();
  if (count <= 0) return null;
  return (
    <View style={[barS.wrap, { backgroundColor: colors.bgCard, borderTopColor: colors.border }]}>
      <Text style={[barS.count, { color: colors.textPri }]}>
        {count} fund{count === 1 ? "" : "s"} in portfolio
      </Text>
      <Pressable
        onPress={onBuild}
        style={({ pressed }) => [barS.btn, { backgroundColor: colors.azure, opacity: pressed ? 0.85 : 1 }]}
      >
        <Briefcase size={13} color="#fff" />
        <Text style={barS.btnText2}>Build Proposal</Text>
      </Pressable>
    </View>
  );
}

const addS = StyleSheet.create({
  btn: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 7,
    marginHorizontal: space.md,
    marginTop: space.md,
    borderRadius: radii.md,
    paddingVertical: 12,
  },
  btnText: { fontSize: 13.5, fontWeight: "700" },
});

const barS = StyleSheet.create({
  wrap: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: space.md,
    paddingVertical: 10,
    borderTopWidth: StyleSheet.hairlineWidth,
  },
  count: { fontSize: 13, fontWeight: "700" },
  btn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingHorizontal: 14,
    paddingVertical: 9,
    borderRadius: radii.pill,
  },
  btnText2: { fontSize: 12.5, fontWeight: "700", color: "#fff" },
});
