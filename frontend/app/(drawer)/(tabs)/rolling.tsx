/**
 * Rolling Analysis tab — how STABLE are a fund's rolling-window numbers
 * across different stretches of history, instead of one blended figure?
 *
 * Pick a fund, an overall date range, and a rolling-window length (3/5/7/10Y
 * or any custom length) — every monthly-sliding window inside the range is
 * grouped into periods (one per calendar year by default, or advisor-defined
 * custom periods), and the same P10/Median/Blended WM/Success %/CV/FQS stats
 * the rest of the app already uses are computed independently per period. An
 * "overall" row (every window in the range, in one group) is shown as the
 * baseline each period is implicitly compared against.
 */

import { useMemo, useState } from "react";
import {
  View, Text, StyleSheet, ScrollView, Pressable, TextInput,
  ActivityIndicator, KeyboardAvoidingView, Platform, Alert,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import * as FileSystem from "expo-file-system/legacy";
import * as Sharing from "expo-sharing";
import {
  Repeat, AlertTriangle, Search, X, Plus, Scissors, FileText, Sparkles,
  Trophy, Star, ChevronDown, ChevronUp,
} from "lucide-react-native";
import {
  api,
  type RollingAnalysisResponse, type RollingPeriodRow,
  type RollingRankResponse, type RollingRankFundRow,
} from "@/src/api";
import { useTheme } from "@/src/ThemeProvider";
import { radii, space, type ThemeColors, type ThemeType } from "@/src/theme";
import { formatPercent, formatMultiple } from "@/src/utils/format";
import { FundSearchModal, type PickedFund } from "@/src/components/FundSearchModal";
import { DrawerMenuButton } from "@/src/components/DrawerMenuButton";

const WINDOW_CHIPS = [1, 2, 3, 5, 7, 10, 15, 20, 25, 30];
const SPLIT_CHIPS = [2, 3, 4, 5];
const POOL_SIZE_CHIPS = [0, 10, 20, 30, 50, 80];

let bucketIdSeq = 0;
interface BucketRow { id: number; start: string; end: string }

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

function isValidIsoDate(s: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return false;
  const d = new Date(s + "T00:00:00Z");
  return !isNaN(d.getTime());
}

function yearsBetween(a: string, b: string): number {
  const ms = new Date(b + "T00:00:00Z").getTime() - new Date(a + "T00:00:00Z").getTime();
  return ms / (365.25 * 24 * 60 * 60 * 1000);
}

function toIso(d: Date): string {
  return d.toISOString().slice(0, 10);
}

/** Split [startIso, endIso] into `n` contiguous, roughly-equal, non-
 *  overlapping date ranges — a quick-fill starting point the advisor can
 *  still hand-edit afterwards (drag the boundaries wherever makes sense). */
function splitEvenly(startIso: string, endIso: string, n: number): BucketRow[] {
  const startMs = new Date(startIso + "T00:00:00Z").getTime();
  const endMs = new Date(endIso + "T00:00:00Z").getTime();
  const chunk = (endMs - startMs) / n;
  const rows: BucketRow[] = [];
  for (let i = 0; i < n; i++) {
    const chunkStart = new Date(startMs + i * chunk);
    const chunkEnd = i === n - 1 ? new Date(endMs) : new Date(startMs + (i + 1) * chunk - 24 * 60 * 60 * 1000);
    rows.push({ id: bucketIdSeq++, start: toIso(chunkStart), end: toIso(chunkEnd) });
  }
  return rows;
}

/** Pure validation shared by the single-fund and multi-fund date/window/
 *  bucket controls — same rules, two independent sets of state. */
function computeRangeValidity(startDate: string, endDate: string, windowYears: number, buckets: BucketRow[]) {
  const dateError =
    !isValidIsoDate(startDate) || !isValidIsoDate(endDate)
      ? "Enter dates as YYYY-MM-DD"
      : endDate <= startDate
      ? "End date must be after start date"
      : null;
  const maxFeasibleWindow = dateError ? 0 : Math.max(1, Math.floor(yearsBetween(startDate, endDate)));
  const windowChips = WINDOW_CHIPS.filter((y) => y <= Math.max(maxFeasibleWindow, WINDOW_CHIPS[0]));
  const windowTooLong = !dateError && windowYears > maxFeasibleWindow;
  const bucketErrors = buckets.map((b) => {
    if (!isValidIsoDate(b.start) || !isValidIsoDate(b.end)) return "Dates must be YYYY-MM-DD";
    if (b.end <= b.start) return "End must be after start";
    return null;
  });
  const hasBucketError = bucketErrors.some((e) => e !== null);
  return { dateError, maxFeasibleWindow, windowChips, windowTooLong, bucketErrors, hasBucketError };
}

export default function RollingAnalysisScreen() {
  const { colors, type } = useTheme();
  const styles = useMemo(() => makeStyles(colors, type), [colors, type]);

  const [screenMode, setScreenMode] = useState<"single" | "multi">("single");

  const [fund, setFund] = useState<PickedFund | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);

  const [startDate, setStartDate] = useState("2010-01-01");
  const [endDate, setEndDate] = useState(todayIso());
  const [mode, setMode] = useState<"sip" | "lumpsum">("sip");
  const [windowYears, setWindowYears] = useState(5);
  const [buckets, setBuckets] = useState<BucketRow[]>([]);

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<RollingAnalysisResponse | null>(null);
  const [exporting, setExporting] = useState(false);

  const { dateError, maxFeasibleWindow, windowChips, windowTooLong, bucketErrors, hasBucketError } =
    computeRangeValidity(startDate, endDate, windowYears, buckets);

  const canRun = !!fund && !dateError && !windowTooLong && !hasBucketError && !loading;

  function addBucket() {
    const last = buckets[buckets.length - 1];
    setBuckets((prev) => [...prev, { id: bucketIdSeq++, start: last?.end ?? startDate, end: endDate }]);
  }
  function removeBucket(id: number) {
    setBuckets((prev) => prev.filter((b) => b.id !== id));
  }
  function updateBucket(id: number, field: "start" | "end", value: string) {
    setBuckets((prev) => prev.map((b) => (b.id === id ? { ...b, [field]: value } : b)));
  }
  function splitInto(n: number) {
    if (dateError) return;
    setBuckets(splitEvenly(startDate, endDate, n));
  }

  // ---- Multi-fund ranking mode: independent state, same controls ----
  const [multiMode, setMultiMode] = useState<"sip" | "lumpsum">("sip");
  const [multiStartDate, setMultiStartDate] = useState("2010-01-01");
  const [multiEndDate, setMultiEndDate] = useState(todayIso());
  const [multiWindowYears, setMultiWindowYears] = useState(5);
  const [multiBuckets, setMultiBuckets] = useState<BucketRow[]>([]);
  const [poolSize, setPoolSize] = useState(30);
  const [handPicked, setHandPicked] = useState<PickedFund[]>([]);
  const [handPickSearchOpen, setHandPickSearchOpen] = useState(false);

  const [multiLoading, setMultiLoading] = useState(false);
  const [multiError, setMultiError] = useState<string | null>(null);
  const [multiResult, setMultiResult] = useState<RollingRankResponse | null>(null);
  const [multiExporting, setMultiExporting] = useState(false);
  const [addFundBusy, setAddFundBusy] = useState(false);
  const [addMoreSearchOpen, setAddMoreSearchOpen] = useState(false);
  const [expandedFunds, setExpandedFunds] = useState<Set<number>>(new Set());

  function toggleFundExpanded(schemeCode: number) {
    setExpandedFunds((prev) => {
      const next = new Set(prev);
      if (next.has(schemeCode)) next.delete(schemeCode);
      else next.add(schemeCode);
      return next;
    });
  }

  const {
    dateError: multiDateError, windowChips: multiWindowChips, windowTooLong: multiWindowTooLong,
    bucketErrors: multiBucketErrors, hasBucketError: multiHasBucketError,
  } = computeRangeValidity(multiStartDate, multiEndDate, multiWindowYears, multiBuckets);

  const canRunMulti =
    (poolSize > 0 || handPicked.length > 0) &&
    !multiDateError && !multiWindowTooLong && !multiHasBucketError && !multiLoading;

  function addMultiBucket() {
    const last = multiBuckets[multiBuckets.length - 1];
    setMultiBuckets((prev) => [...prev, { id: bucketIdSeq++, start: last?.end ?? multiStartDate, end: multiEndDate }]);
  }
  function removeMultiBucket(id: number) {
    setMultiBuckets((prev) => prev.filter((b) => b.id !== id));
  }
  function updateMultiBucket(id: number, field: "start" | "end", value: string) {
    setMultiBuckets((prev) => prev.map((b) => (b.id === id ? { ...b, [field]: value } : b)));
  }
  function splitMultiInto(n: number) {
    if (multiDateError) return;
    setMultiBuckets(splitEvenly(multiStartDate, multiEndDate, n));
  }

  function addHandPicked(picked: PickedFund) {
    setHandPicked((prev) => (prev.some((f) => f.scheme_code === picked.scheme_code) ? prev : [...prev, picked]));
    setHandPickSearchOpen(false);
  }
  function removeHandPicked(schemeCode: number) {
    setHandPicked((prev) => prev.filter((f) => f.scheme_code !== schemeCode));
  }

  async function onRunMulti() {
    if (!canRunMulti) return;
    setMultiLoading(true);
    setMultiError(null);
    try {
      const res = await api.runRollingRank({
        start_date: multiStartDate,
        end_date: multiEndDate,
        window_years: multiWindowYears,
        mode: multiMode,
        buckets: multiBuckets.length > 0 ? multiBuckets.map((b) => ({ start_date: b.start, end_date: b.end })) : undefined,
        pool_size: poolSize,
        scheme_codes: handPicked.length > 0 ? handPicked.map((f) => f.scheme_code) : undefined,
      });
      setMultiResult(res);
    } catch (e: any) {
      const msg: string = e?.message ?? "Ranking failed";
      setMultiError(msg.replace(/^HTTP \d+:\s*/, "").replace(/^\{"detail":"|"\}$/g, ""));
      setMultiResult(null);
    } finally {
      setMultiLoading(false);
    }
  }

  async function onAddFundToRanking(picked: PickedFund) {
    setAddMoreSearchOpen(false);
    if (!multiResult) return;
    setAddFundBusy(true);
    setMultiError(null);
    try {
      await api.addRankedFund(multiResult.multi_id, picked.scheme_code);
      const refreshed = await api.getRollingRank(multiResult.multi_id);
      setMultiResult(refreshed);
    } catch (e: any) {
      const msg: string = e?.message ?? "Couldn't add fund";
      setMultiError(msg.replace(/^HTTP \d+:\s*/, "").replace(/^\{"detail":"|"\}$/g, ""));
    } finally {
      setAddFundBusy(false);
    }
  }

  async function onExportMultiPdf() {
    if (!multiResult) return;
    setMultiExporting(true);
    try {
      const url = api.rollingRankExportUrl(multiResult.multi_id);
      const fname = `RollingConsistencyRanking_${multiResult.mode.toUpperCase()}_${multiResult.window_years}Y.pdf`;
      if (Platform.OS === "web") {
        window.open(url, "_blank");
        return;
      }
      const dest = FileSystem.cacheDirectory + fname;
      const { uri } = await FileSystem.downloadAsync(url, dest);
      if (await Sharing.isAvailableAsync()) await Sharing.shareAsync(uri);
      else Alert.alert("Saved", `File saved to ${uri}`);
    } catch (e: any) {
      Alert.alert("Export failed", e?.message ?? "Unknown error");
    } finally {
      setMultiExporting(false);
    }
  }

  async function onCalculate() {
    if (!canRun || !fund) return;
    setLoading(true);
    setError(null);
    try {
      const res = await api.runRollingAnalysis({
        scheme_code: fund.scheme_code,
        start_date: startDate,
        end_date: endDate,
        window_years: windowYears,
        mode,
        buckets: buckets.length > 0 ? buckets.map((b) => ({ start_date: b.start, end_date: b.end })) : undefined,
      });
      setResult(res);
    } catch (e: any) {
      const msg: string = e?.message ?? "Rolling analysis failed";
      setError(msg.replace(/^HTTP \d+:\s*/, "").replace(/^\{"detail":"|"\}$/g, ""));
      setResult(null);
    } finally {
      setLoading(false);
    }
  }

  async function onExportPdf() {
    if (!result) return;
    setExporting(true);
    try {
      const url = api.rollingAnalysisExportUrl(result.analysis_id);
      const fname = `RollingAnalysis_${result.scheme_name.replace(/[^a-z0-9]+/gi, "_")}_${mode.toUpperCase()}_${windowYears}Y.pdf`;
      if (Platform.OS === "web") {
        window.open(url, "_blank");
        return;
      }
      const dest = FileSystem.cacheDirectory + fname;
      const { uri } = await FileSystem.downloadAsync(url, dest);
      if (await Sharing.isAvailableAsync()) await Sharing.shareAsync(uri);
      else Alert.alert("Saved", `File saved to ${uri}`);
    } catch (e: any) {
      Alert.alert("Export failed", e?.message ?? "Unknown error");
    } finally {
      setExporting(false);
    }
  }

  return (
    <SafeAreaView style={styles.safe} edges={["top"]}>
      <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === "ios" ? "padding" : undefined}>
        <ScrollView contentContainerStyle={{ padding: space.md, paddingBottom: space.xl * 2 }} keyboardShouldPersistTaps="handled">
          <View style={styles.headerRow}>
            <DrawerMenuButton />
            <Repeat size={20} color={colors.azure} />
            <Text style={styles.title}>Rolling Analysis</Text>
          </View>
          <Text style={styles.subtitle}>
            {screenMode === "single"
              ? "Pick one fund and see how P10/Median/Blended WM, Success %, CV and FQS vary period-by-period, instead of one blended number across its whole history."
              : "Run the same period-by-period analysis across many funds at once — an auto-selected pool, funds you pick by hand, or both — ranked by how consistent each one is across periods."}
          </Text>

          <View style={[styles.pillRow, { marginTop: space.md }]}>
            <Pressable
              onPress={() => setScreenMode("single")}
              style={[styles.modePill, { borderColor: screenMode === "single" ? colors.azure : colors.border, backgroundColor: screenMode === "single" ? colors.azure : colors.bgCard }]}
            >
              <Repeat size={14} color={screenMode === "single" ? "#fff" : colors.textSec} />
              <Text style={[styles.pillText, { color: screenMode === "single" ? "#fff" : colors.textSec }]}>Single Fund</Text>
            </Pressable>
            <Pressable
              onPress={() => setScreenMode("multi")}
              style={[styles.modePill, { borderColor: screenMode === "multi" ? colors.azure : colors.border, backgroundColor: screenMode === "multi" ? colors.azure : colors.bgCard }]}
            >
              <Trophy size={14} color={screenMode === "multi" ? "#fff" : colors.textSec} />
              <Text style={[styles.pillText, { color: screenMode === "multi" ? "#fff" : colors.textSec }]}>Multi-Fund Ranking</Text>
            </Pressable>
          </View>

          {screenMode === "single" && (
          <>
          <Text style={styles.sectionTitle}>Fund</Text>
          {fund ? (
            <View style={styles.fundPill}>
              <Text style={styles.fundPillText} numberOfLines={2}>{fund.scheme_name}</Text>
              <Pressable onPress={() => setFund(null)} hitSlop={10}>
                <X size={16} color={colors.textTer} />
              </Pressable>
            </View>
          ) : (
            <Pressable style={styles.searchBtn} onPress={() => setSearchOpen(true)}>
              <Search size={16} color={colors.azure} />
              <Text style={styles.searchBtnText}>Search &amp; select a fund</Text>
            </Pressable>
          )}

          <Text style={styles.sectionTitle}>Mode</Text>
          <View style={styles.pillRow}>
            {(["sip", "lumpsum"] as const).map((m) => (
              <Pressable
                key={m}
                onPress={() => setMode(m)}
                style={[styles.pill, { borderColor: mode === m ? colors.azure : colors.border, backgroundColor: mode === m ? colors.azure : colors.bgCard }]}
              >
                <Text style={[styles.pillText, { color: mode === m ? "#fff" : colors.textSec }]}>{m === "sip" ? "SIP" : "Lumpsum"}</Text>
              </Pressable>
            ))}
          </View>

          <Text style={styles.sectionTitle}>Overall Date Range</Text>
          <View style={styles.row2}>
            <View style={styles.col}>
              <Text style={styles.label}>Start date</Text>
              <TextInput
                style={styles.input}
                value={startDate}
                onChangeText={setStartDate}
                placeholder="YYYY-MM-DD"
                placeholderTextColor={colors.textTer}
                autoCapitalize="none"
                autoCorrect={false}
              />
            </View>
            <View style={styles.col}>
              <Text style={styles.label}>End date</Text>
              <TextInput
                style={styles.input}
                value={endDate}
                onChangeText={setEndDate}
                placeholder="YYYY-MM-DD"
                placeholderTextColor={colors.textTer}
                autoCapitalize="none"
                autoCorrect={false}
              />
            </View>
          </View>
          {dateError && (
            <View style={styles.errorRow}>
              <AlertTriangle size={13} color={colors.ruby} />
              <Text style={[styles.errorText, { color: colors.ruby }]}>{dateError}</Text>
            </View>
          )}

          <Text style={styles.sectionTitle}>Rolling Period</Text>
          <View style={styles.chipsRow}>
            {windowChips.map((y) => (
              <Pressable
                key={y}
                onPress={() => setWindowYears(y)}
                style={[
                  styles.chip,
                  { borderColor: windowYears === y ? colors.azure : colors.border,
                    backgroundColor: windowYears === y ? colors.azure : colors.bgCard },
                ]}
              >
                <Text style={[styles.chipText, { color: windowYears === y ? "#fff" : colors.textSec }]}>{y}Y</Text>
              </Pressable>
            ))}
          </View>
          {windowTooLong && !dateError && (
            <View style={styles.errorRow}>
              <AlertTriangle size={13} color={colors.ruby} />
              <Text style={[styles.errorText, { color: colors.ruby }]}>
                {windowYears}Y period doesn&apos;t fit — your date range only spans ~{maxFeasibleWindow}Y. Pick a shorter period.
              </Text>
            </View>
          )}

          <View style={styles.sectionTitleRow}>
            <Text style={[styles.sectionTitle, { marginTop: 0, marginBottom: 0 }]}>Comparison Periods</Text>
            {buckets.length > 0 && (
              <Pressable onPress={() => setBuckets([])}>
                <Text style={styles.linkText}>Reset to auto (1Y each)</Text>
              </Pressable>
            )}
          </View>
          <Text style={styles.hintText}>
            {buckets.length === 0
              ? "Leave empty to compare year-by-year automatically. Or define your own periods below — fewer, wider periods mean more rolling windows (more data points) feed each row's numbers."
              : "Each period pools every rolling window that starts inside its dates — wider periods get a bigger sample, so P10/CV/Success% are sturdier."}
          </Text>

          {buckets.length > 0 && (
            <View style={{ gap: space.sm, marginTop: space.sm }}>
              {buckets.map((b, i) => (
                <View key={b.id} style={styles.bucketRow}>
                  <View style={styles.bucketRowInputs}>
                    <TextInput
                      style={[styles.input, styles.bucketInput]}
                      value={b.start}
                      onChangeText={(v) => updateBucket(b.id, "start", v)}
                      placeholder="YYYY-MM-DD"
                      placeholderTextColor={colors.textTer}
                      autoCapitalize="none"
                      autoCorrect={false}
                    />
                    <Text style={{ color: colors.textTer }}>→</Text>
                    <TextInput
                      style={[styles.input, styles.bucketInput]}
                      value={b.end}
                      onChangeText={(v) => updateBucket(b.id, "end", v)}
                      placeholder="YYYY-MM-DD"
                      placeholderTextColor={colors.textTer}
                      autoCapitalize="none"
                      autoCorrect={false}
                    />
                  </View>
                  <Pressable onPress={() => removeBucket(b.id)} hitSlop={10}>
                    <X size={16} color={colors.textTer} />
                  </Pressable>
                  {bucketErrors[i] && (
                    <View style={styles.errorRow}>
                      <AlertTriangle size={12} color={colors.ruby} />
                      <Text style={[styles.errorText, { color: colors.ruby, fontSize: 12 }]}>{bucketErrors[i]}</Text>
                    </View>
                  )}
                </View>
              ))}
            </View>
          )}

          <View style={styles.bucketActionsRow}>
            <Pressable style={styles.addPeriodBtn} onPress={addBucket}>
              <Plus size={14} color={colors.azure} />
              <Text style={styles.addPeriodBtnText}>Add period</Text>
            </Pressable>
            {SPLIT_CHIPS.map((n) => (
              <Pressable key={n} style={styles.splitChip} onPress={() => splitInto(n)} disabled={!!dateError}>
                <Scissors size={12} color={colors.textSec} />
                <Text style={styles.splitChipText}>Split ×{n}</Text>
              </Pressable>
            ))}
          </View>

          <Pressable
            style={[styles.runBtn, !canRun && { opacity: 0.6 }]}
            disabled={!canRun}
            onPress={onCalculate}
          >
            {loading ? <ActivityIndicator color="#fff" /> : <Text style={styles.runBtnText}>Calculate Rolling Analysis</Text>}
          </Pressable>

          {error && (
            <View style={styles.errorBox}>
              <AlertTriangle size={14} color={colors.ruby} />
              <Text style={[styles.errorText, { color: colors.ruby, flex: 1, marginLeft: 8 }]}>{error}</Text>
            </View>
          )}

          {result && (
            <>
              <View style={styles.summaryBox}>
                <Text style={styles.summaryText}>
                  <Text style={{ fontWeight: "800" }}>{result.scheme_name}</Text> · {result.category} ·{" "}
                  {result.mode === "sip" ? "SIP" : "Lumpsum"} · {result.window_years}Y rolling period ·{" "}
                  {result.start_date} to {result.end_date} ·{" "}
                  {result.custom_buckets ? "custom periods" : "auto (1 year each)"}
                </Text>
              </View>

              {result.key_points.length > 0 && (
                <>
                  <View style={styles.keyPointsHeaderRow}>
                    <Sparkles size={14} color={colors.azure} />
                    <Text style={[styles.sectionTitle, { marginTop: 0, marginBottom: 0 }]}>Key Points</Text>
                  </View>
                  <View style={styles.keyPointsBox}>
                    {result.key_points.map((kp, i) => (
                      <View key={i} style={styles.keyPointRow}>
                        <Text style={styles.keyPointBullet}>•</Text>
                        <Text style={styles.keyPointText}>{kp}</Text>
                      </View>
                    ))}
                  </View>
                </>
              )}

              {(result.periods.length > 0 || result.overall) && (
                <>
                  <Text style={styles.sectionTitle}>Comparison Table</Text>
                  <ComparisonTable periods={result.periods} overall={result.overall} colors={colors} styles={styles} />
                </>
              )}

              {result.periods.length > 0 && (
                <Pressable
                  style={[styles.exportBtn, exporting && { opacity: 0.6 }]}
                  disabled={exporting}
                  onPress={onExportPdf}
                >
                  {exporting ? <ActivityIndicator color="#fff" /> : (
                    <>
                      <FileText size={16} color="#fff" />
                      <Text style={styles.exportBtnText}>Export PDF for Client</Text>
                    </>
                  )}
                </Pressable>
              )}

              {result.overall && (
                <>
                  <Text style={styles.sectionTitle}>Overall (Full Range Baseline)</Text>
                  <PeriodCard row={result.overall} colors={colors} styles={styles} highlight />
                </>
              )}

              {result.periods.length > 0 && (
                <>
                  <Text style={styles.sectionTitle}>By Period ({result.periods.length})</Text>
                  {result.periods.map((p) => (
                    <PeriodCard key={p.start_date + p.end_date} row={p} colors={colors} styles={styles} />
                  ))}
                </>
              )}

              {result.periods.length === 0 && (
                <Text style={styles.emptyText}>No rolling periods had enough windows to score.</Text>
              )}
            </>
          )}
          </>
          )}

          {screenMode === "multi" && (
          <>
          <Text style={styles.sectionTitle}>Auto-Selected Pool</Text>
          <Text style={styles.hintText}>
            Top funds by NAV history depth (excluding Debt &amp; Gold/Silver), scored automatically. Set to 0 to
            score only funds you pick by hand below.
          </Text>
          <View style={[styles.chipsRow, { marginTop: space.sm }]}>
            {POOL_SIZE_CHIPS.map((n) => (
              <Pressable
                key={n}
                onPress={() => setPoolSize(n)}
                style={[
                  styles.chip,
                  { borderColor: poolSize === n ? colors.azure : colors.border,
                    backgroundColor: poolSize === n ? colors.azure : colors.bgCard },
                ]}
              >
                <Text style={[styles.chipText, { color: poolSize === n ? "#fff" : colors.textSec }]}>
                  {n === 0 ? "Off" : n}
                </Text>
              </Pressable>
            ))}
          </View>

          <Text style={styles.sectionTitle}>Hand-Picked Funds</Text>
          {handPicked.length > 0 && (
            <View style={{ gap: space.sm, marginBottom: space.sm }}>
              {handPicked.map((f) => (
                <View key={f.scheme_code} style={styles.fundPill}>
                  <Text style={styles.fundPillText} numberOfLines={2}>{f.scheme_name}</Text>
                  <Pressable onPress={() => removeHandPicked(f.scheme_code)} hitSlop={10}>
                    <X size={16} color={colors.textTer} />
                  </Pressable>
                </View>
              ))}
            </View>
          )}
          <Pressable style={styles.searchBtn} onPress={() => setHandPickSearchOpen(true)}>
            <Search size={16} color={colors.azure} />
            <Text style={styles.searchBtnText}>Search &amp; add a fund</Text>
          </Pressable>

          <Text style={styles.sectionTitle}>Mode</Text>
          <View style={styles.pillRow}>
            {(["sip", "lumpsum"] as const).map((m) => (
              <Pressable
                key={m}
                onPress={() => setMultiMode(m)}
                style={[styles.pill, { borderColor: multiMode === m ? colors.azure : colors.border, backgroundColor: multiMode === m ? colors.azure : colors.bgCard }]}
              >
                <Text style={[styles.pillText, { color: multiMode === m ? "#fff" : colors.textSec }]}>{m === "sip" ? "SIP" : "Lumpsum"}</Text>
              </Pressable>
            ))}
          </View>

          <Text style={styles.sectionTitle}>Overall Date Range</Text>
          <View style={styles.row2}>
            <View style={styles.col}>
              <Text style={styles.label}>Start date</Text>
              <TextInput
                style={styles.input}
                value={multiStartDate}
                onChangeText={setMultiStartDate}
                placeholder="YYYY-MM-DD"
                placeholderTextColor={colors.textTer}
                autoCapitalize="none"
                autoCorrect={false}
              />
            </View>
            <View style={styles.col}>
              <Text style={styles.label}>End date</Text>
              <TextInput
                style={styles.input}
                value={multiEndDate}
                onChangeText={setMultiEndDate}
                placeholder="YYYY-MM-DD"
                placeholderTextColor={colors.textTer}
                autoCapitalize="none"
                autoCorrect={false}
              />
            </View>
          </View>
          {multiDateError && (
            <View style={styles.errorRow}>
              <AlertTriangle size={13} color={colors.ruby} />
              <Text style={[styles.errorText, { color: colors.ruby }]}>{multiDateError}</Text>
            </View>
          )}

          <Text style={styles.sectionTitle}>Rolling Period</Text>
          <View style={styles.chipsRow}>
            {multiWindowChips.map((y) => (
              <Pressable
                key={y}
                onPress={() => setMultiWindowYears(y)}
                style={[
                  styles.chip,
                  { borderColor: multiWindowYears === y ? colors.azure : colors.border,
                    backgroundColor: multiWindowYears === y ? colors.azure : colors.bgCard },
                ]}
              >
                <Text style={[styles.chipText, { color: multiWindowYears === y ? "#fff" : colors.textSec }]}>{y}Y</Text>
              </Pressable>
            ))}
          </View>
          {multiWindowTooLong && !multiDateError && (
            <View style={styles.errorRow}>
              <AlertTriangle size={13} color={colors.ruby} />
              <Text style={[styles.errorText, { color: colors.ruby }]}>
                {multiWindowYears}Y period doesn&apos;t fit this date range. Pick a shorter period.
              </Text>
            </View>
          )}

          <View style={styles.sectionTitleRow}>
            <Text style={[styles.sectionTitle, { marginTop: 0, marginBottom: 0 }]}>Comparison Periods</Text>
            {multiBuckets.length > 0 && (
              <Pressable onPress={() => setMultiBuckets([])}>
                <Text style={styles.linkText}>Reset to auto (1Y each)</Text>
              </Pressable>
            )}
          </View>
          <Text style={styles.hintText}>
            {multiBuckets.length === 0
              ? "Leave empty to compare year-by-year automatically. Or define your own periods below — fewer, wider periods mean a bigger sample per period."
              : "Each period pools every rolling window that starts inside its dates."}
          </Text>

          {multiBuckets.length > 0 && (
            <View style={{ gap: space.sm, marginTop: space.sm }}>
              {multiBuckets.map((b, i) => (
                <View key={b.id} style={styles.bucketRow}>
                  <View style={styles.bucketRowInputs}>
                    <TextInput
                      style={[styles.input, styles.bucketInput]}
                      value={b.start}
                      onChangeText={(v) => updateMultiBucket(b.id, "start", v)}
                      placeholder="YYYY-MM-DD"
                      placeholderTextColor={colors.textTer}
                      autoCapitalize="none"
                      autoCorrect={false}
                    />
                    <Text style={{ color: colors.textTer }}>→</Text>
                    <TextInput
                      style={[styles.input, styles.bucketInput]}
                      value={b.end}
                      onChangeText={(v) => updateMultiBucket(b.id, "end", v)}
                      placeholder="YYYY-MM-DD"
                      placeholderTextColor={colors.textTer}
                      autoCapitalize="none"
                      autoCorrect={false}
                    />
                  </View>
                  <Pressable onPress={() => removeMultiBucket(b.id)} hitSlop={10}>
                    <X size={16} color={colors.textTer} />
                  </Pressable>
                  {multiBucketErrors[i] && (
                    <View style={styles.errorRow}>
                      <AlertTriangle size={12} color={colors.ruby} />
                      <Text style={[styles.errorText, { color: colors.ruby, fontSize: 12 }]}>{multiBucketErrors[i]}</Text>
                    </View>
                  )}
                </View>
              ))}
            </View>
          )}

          <View style={styles.bucketActionsRow}>
            <Pressable style={styles.addPeriodBtn} onPress={addMultiBucket}>
              <Plus size={14} color={colors.azure} />
              <Text style={styles.addPeriodBtnText}>Add period</Text>
            </Pressable>
            {SPLIT_CHIPS.map((n) => (
              <Pressable key={n} style={styles.splitChip} onPress={() => splitMultiInto(n)} disabled={!!multiDateError}>
                <Scissors size={12} color={colors.textSec} />
                <Text style={styles.splitChipText}>Split ×{n}</Text>
              </Pressable>
            ))}
          </View>

          <Pressable
            style={[styles.runBtn, !canRunMulti && { opacity: 0.6 }]}
            disabled={!canRunMulti}
            onPress={onRunMulti}
          >
            {multiLoading ? <ActivityIndicator color="#fff" /> : <Text style={styles.runBtnText}>Run Ranking</Text>}
          </Pressable>

          {multiError && (
            <View style={styles.errorBox}>
              <AlertTriangle size={14} color={colors.ruby} />
              <Text style={[styles.errorText, { color: colors.ruby, flex: 1, marginLeft: 8 }]}>{multiError}</Text>
            </View>
          )}

          {multiResult && (
            <>
              <View style={styles.summaryBox}>
                <Text style={styles.summaryText}>
                  {multiResult.mode === "sip" ? "SIP" : "Lumpsum"} · {multiResult.window_years}Y rolling period ·{" "}
                  {multiResult.start_date} to {multiResult.end_date} ·{" "}
                  {multiResult.custom_buckets ? "custom periods" : "auto (1 year each)"} ·{" "}
                  {multiResult.scored_count} of {multiResult.candidate_count} funds scored
                </Text>
              </View>

              {multiResult.key_points.length > 0 && (
                <>
                  <View style={styles.keyPointsHeaderRow}>
                    <Sparkles size={14} color={colors.azure} />
                    <Text style={[styles.sectionTitle, { marginTop: 0, marginBottom: 0 }]}>Key Points</Text>
                  </View>
                  <View style={styles.keyPointsBox}>
                    {multiResult.key_points.map((kp, i) => (
                      <View key={i} style={styles.keyPointRow}>
                        <Text style={styles.keyPointBullet}>•</Text>
                        <Text style={styles.keyPointText}>{kp}</Text>
                      </View>
                    ))}
                  </View>
                </>
              )}

              <View style={styles.sectionTitleRow}>
                <Text style={[styles.sectionTitle, { marginTop: space.lg, marginBottom: 0 }]}>
                  Ranked by Consistency ({multiResult.funds.length})
                </Text>
                <Pressable onPress={() => setAddMoreSearchOpen(true)} disabled={addFundBusy}>
                  {addFundBusy ? <ActivityIndicator color={colors.azure} /> : <Text style={styles.linkText}>+ Add another fund</Text>}
                </Pressable>
              </View>

              {multiResult.funds.map((f) => (
                <RankedFundCard
                  key={f.scheme_code}
                  row={f}
                  colors={colors}
                  styles={styles}
                  expanded={expandedFunds.has(f.scheme_code)}
                  onToggle={() => toggleFundExpanded(f.scheme_code)}
                />
              ))}

              {multiResult.funds.length > 0 && (
                <Pressable
                  style={[styles.exportBtn, multiExporting && { opacity: 0.6 }]}
                  disabled={multiExporting}
                  onPress={onExportMultiPdf}
                >
                  {multiExporting ? <ActivityIndicator color="#fff" /> : (
                    <>
                      <FileText size={16} color="#fff" />
                      <Text style={styles.exportBtnText}>Export PDF for Client</Text>
                    </>
                  )}
                </Pressable>
              )}

              {multiResult.funds.length === 0 && (
                <Text style={styles.emptyText}>No funds scored — widen the date range or pick funds with more history.</Text>
              )}
            </>
          )}
          </>
          )}
        </ScrollView>
      </KeyboardAvoidingView>

      <FundSearchModal
        visible={searchOpen}
        onClose={() => setSearchOpen(false)}
        onPick={(picked) => { setFund(picked); setResult(null); setSearchOpen(false); }}
        title="Select Fund"
      />
      <FundSearchModal
        visible={handPickSearchOpen}
        onClose={() => setHandPickSearchOpen(false)}
        onPick={addHandPicked}
        title="Add Fund to Pool"
      />
      <FundSearchModal
        visible={addMoreSearchOpen}
        onClose={() => setAddMoreSearchOpen(false)}
        onPick={onAddFundToRanking}
        title="Add Fund to Ranking"
      />
    </SafeAreaView>
  );
}

const TABLE_COLS: { key: keyof RollingPeriodRow | "label"; label: string; width: number; fmt: (r: RollingPeriodRow) => string }[] = [
  { key: "label", label: "Period", width: 118, fmt: (r) => r.label },
  { key: "n_windows", label: "Windows", width: 64, fmt: (r) => String(r.n_windows) },
  { key: "p10_wm", label: "P10 WM", width: 72, fmt: (r) => formatMultiple(r.p10_wm) },
  { key: "median_wm", label: "Median WM", width: 82, fmt: (r) => formatMultiple(r.median_wm) },
  { key: "blended_wm", label: "Blended WM", width: 86, fmt: (r) => formatMultiple(r.blended_wm) },
  { key: "success_pct", label: "Success %", width: 78, fmt: (r) => formatPercent(r.success_pct) },
  { key: "cv", label: "CV", width: 66, fmt: (r) => formatPercent(r.cv) },
  { key: "min_wm_required", label: "Min WM Req.", width: 88, fmt: (r) => formatMultiple(r.min_wm_required) },
  { key: "fqs", label: "FQS", width: 60, fmt: (r) => (r.fqs != null ? r.fqs.toFixed(0) : "—") },
];

function ComparisonTable({
  periods, overall, colors, styles,
}: { periods: RollingPeriodRow[]; overall: RollingPeriodRow | null; colors: ThemeColors; styles: RollingStyles }) {
  const rows = [...periods, ...(overall ? [{ ...overall, label: "Overall" }] : [])];
  return (
    <ScrollView horizontal showsHorizontalScrollIndicator style={styles.tableWrap}>
      <View>
        <View style={styles.tableRow}>
          {TABLE_COLS.map((c) => (
            <View key={c.key} style={[styles.tableHeaderCell, { width: c.width }]}>
              <Text style={styles.tableHeaderText}>{c.label}</Text>
            </View>
          ))}
        </View>
        {rows.map((r, i) => {
          const isOverall = r.label === "Overall" && i === rows.length - 1 && !!overall;
          return (
            <View key={i} style={[styles.tableRow, isOverall && { backgroundColor: colors.azureSoft }]}>
              {TABLE_COLS.map((c) => (
                <View key={c.key} style={[styles.tableCell, { width: c.width }]}>
                  <Text
                    style={[styles.tableCellText, isOverall && { fontWeight: "800", color: colors.azure }]}
                    numberOfLines={2}
                  >
                    {c.fmt(r)}
                  </Text>
                </View>
              ))}
            </View>
          );
        })}
      </View>
    </ScrollView>
  );
}

function PeriodCard({
  row, colors, styles, highlight,
}: { row: RollingPeriodRow; colors: ThemeColors; styles: RollingStyles; highlight?: boolean }) {
  const beat = row.blended_wm != null && row.min_wm_required != null && row.blended_wm >= row.min_wm_required;
  const empty = row.n_windows === 0;
  return (
    <View style={[styles.card, highlight && { borderColor: colors.azure, borderWidth: 1.5 }]}>
      <View style={styles.cardHeaderRow}>
        <View>
          <Text style={styles.periodLabel}>{row.label}</Text>
          <Text style={styles.periodDates}>{row.start_date} → {row.end_date}</Text>
        </View>
        <View style={{ flex: 1 }} />
        <Text style={styles.periodMeta}>{row.n_windows} windows</Text>
        {!empty && (
          <Text style={[styles.fqsBadge, { color: colors.azure }]}>{row.fqs != null ? row.fqs.toFixed(0) : "—"}</Text>
        )}
      </View>
      {empty ? (
        <Text style={styles.emptyPeriodText}>No rolling windows start inside this period — try a wider range.</Text>
      ) : (
        <View style={styles.metricsGrid}>
          <Metric label="P10 WM" value={formatMultiple(row.p10_wm)} styles={styles} />
          <Metric label="Median WM" value={formatMultiple(row.median_wm)} styles={styles} />
          <Metric label="Blended WM" value={formatMultiple(row.blended_wm)} color={beat ? colors.tierElite : colors.tierWeak} styles={styles} />
          <Metric label="Success %" value={formatPercent(row.success_pct)} styles={styles} />
          <Metric label="CV" value={formatPercent(row.cv)} styles={styles} />
          <Metric label="Min WM Req." value={formatMultiple(row.min_wm_required)} styles={styles} />
        </View>
      )}
    </View>
  );
}

function Metric({
  label, value, color, styles,
}: { label: string; value: string; color?: string; styles: RollingStyles }) {
  return (
    <View style={styles.metricCell}>
      <Text style={styles.metricLabel}>{label}</Text>
      <Text style={[styles.metricValue, color ? { color } : null]}>{value}</Text>
    </View>
  );
}

/** One ranked fund in the multi-fund consistency ranking — rank badge, name,
 *  and its cross-period stability figures alongside its overall snapshot. */
function RankedFundCard({
  row, colors, styles, expanded, onToggle,
}: { row: RollingRankFundRow; colors: ThemeColors; styles: RollingStyles; expanded: boolean; onToggle: () => void }) {
  const unrankable = row.cross_period_cv == null;
  return (
    <View style={styles.rankCard}>
      <Pressable style={styles.rankCardHeader} onPress={onToggle}>
        <View style={styles.rankBadge}>
          <Text style={styles.rankBadgeText}>{unrankable ? "—" : `#${row.rank}`}</Text>
        </View>
        <View style={{ flex: 1 }}>
          <View style={styles.cardHeaderRow}>
            <Text style={styles.periodLabel} numberOfLines={2}>{row.scheme_name}</Text>
            {row.manually_added && <Star size={13} color={colors.azure} fill={colors.azure} />}
          </View>
          <Text style={styles.periodDates}>{row.category} · {row.periods_scored} period{row.periods_scored === 1 ? "" : "s"} scored</Text>
          {unrankable ? (
            <Text style={styles.emptyPeriodText}>Fewer than 2 scored periods — shown but not ranked.</Text>
          ) : (
            <View style={styles.metricsGrid}>
              <Metric label="Cross-Period CV" value={formatPercent(row.cross_period_cv)} styles={styles} />
              <Metric label="WM Spread" value={formatMultiple(row.wm_spread)} styles={styles} />
              <Metric label="Overall Blended WM" value={formatMultiple(row.overall?.blended_wm ?? null)} styles={styles} />
              <Metric label="Overall FQS" value={row.overall?.fqs != null ? row.overall.fqs.toFixed(0) : "—"} styles={styles} />
              <Metric label="Most Consistent In" value={row.wm_min_period ?? "—"} styles={styles} />
              <Metric label="Least Consistent In" value={row.wm_max_period ?? "—"} styles={styles} />
            </View>
          )}
        </View>
        {expanded ? <ChevronUp size={18} color={colors.textTer} /> : <ChevronDown size={18} color={colors.textTer} />}
      </Pressable>

      {expanded && (
        <View style={styles.rankCardExpanded}>
          {!!row.key_points?.length && (
            <>
              <View style={styles.keyPointsHeaderRow}>
                <Sparkles size={13} color={colors.azure} />
                <Text style={[styles.sectionTitle, { marginTop: 0, marginBottom: 0, fontSize: 12 }]}>Key Points</Text>
              </View>
              <View style={[styles.keyPointsBox, { marginTop: space.sm }]}>
                {row.key_points.map((kp, i) => (
                  <View key={i} style={styles.keyPointRow}>
                    <Text style={styles.keyPointBullet}>•</Text>
                    <Text style={styles.keyPointText}>{kp}</Text>
                  </View>
                ))}
              </View>
            </>
          )}

          {(row.periods.length > 0 || row.overall) && (
            <>
              <Text style={[styles.sectionTitle, { fontSize: 12 }]}>Comparison Table</Text>
              <ComparisonTable periods={row.periods} overall={row.overall} colors={colors} styles={styles} />
            </>
          )}

          {row.overall && (
            <>
              <Text style={[styles.sectionTitle, { fontSize: 12 }]}>Overall (Full Range Baseline)</Text>
              <PeriodCard row={row.overall} colors={colors} styles={styles} highlight />
            </>
          )}

          {row.periods.length > 0 && (
            <>
              <Text style={[styles.sectionTitle, { fontSize: 12 }]}>By Period ({row.periods.length})</Text>
              {row.periods.map((p) => (
                <PeriodCard key={p.start_date + p.end_date} row={p} colors={colors} styles={styles} />
              ))}
            </>
          )}
        </View>
      )}
    </View>
  );
}

type RollingStyles = ReturnType<typeof makeStyles>;

function makeStyles(colors: ThemeColors, type: ThemeType) {
  return StyleSheet.create({
    safe: { flex: 1, backgroundColor: colors.bg },
    headerRow: { flexDirection: "row", alignItems: "center", gap: 8, marginTop: space.sm },
    title: { ...type.h2 },
    subtitle: { ...type.bodySm, color: colors.textSec, marginTop: 6, lineHeight: 19 },
    sectionTitle: {
      fontSize: 13, fontWeight: "700", color: colors.textTer,
      textTransform: "uppercase", letterSpacing: 0.6,
      marginTop: space.lg, marginBottom: space.sm,
    },
    sectionTitleRow: {
      flexDirection: "row", alignItems: "center", justifyContent: "space-between",
      marginTop: space.lg,
    },
    linkText: { fontSize: 12, fontWeight: "700", color: colors.azure },
    hintText: { fontSize: 12, color: colors.textTer, lineHeight: 17, marginTop: 4 },
    bucketRow: {
      backgroundColor: colors.bgCard, borderRadius: radii.md, borderWidth: 1, borderColor: colors.border,
      padding: space.sm, flexDirection: "row", alignItems: "center", gap: 10, flexWrap: "wrap",
    },
    bucketRowInputs: { flexDirection: "row", alignItems: "center", gap: 8, flex: 1 },
    bucketInput: { flex: 1, paddingVertical: 9, fontSize: 13 },
    bucketActionsRow: { flexDirection: "row", flexWrap: "wrap", gap: 8, marginTop: space.sm },
    addPeriodBtn: {
      flexDirection: "row", alignItems: "center", gap: 6,
      paddingHorizontal: 14, paddingVertical: 9, borderRadius: radii.pill,
      borderWidth: 1.5, borderColor: colors.azure, borderStyle: "dashed",
    },
    addPeriodBtnText: { fontSize: 12.5, fontWeight: "700", color: colors.azure },
    splitChip: {
      flexDirection: "row", alignItems: "center", gap: 5,
      paddingHorizontal: 12, paddingVertical: 9, borderRadius: radii.pill,
      borderWidth: 1, borderColor: colors.border, backgroundColor: colors.bgCard,
    },
    splitChipText: { fontSize: 12, fontWeight: "600", color: colors.textSec },
    searchBtn: {
      flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 8,
      paddingVertical: 13, borderRadius: radii.md,
      borderWidth: 1.5, borderColor: colors.azure, borderStyle: "dashed",
    },
    searchBtnText: { fontSize: 14, fontWeight: "700", color: colors.azure },
    fundPill: {
      flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 10,
      backgroundColor: colors.bgCard, borderRadius: radii.md, borderWidth: 1, borderColor: colors.border,
      paddingHorizontal: space.md, paddingVertical: 12,
    },
    fundPillText: { flex: 1, fontSize: 14, fontWeight: "700", color: colors.textPri },
    pillRow: { flexDirection: "row", gap: 8 },
    pill: { paddingHorizontal: 18, paddingVertical: 10, borderRadius: radii.pill, borderWidth: 1.5 },
    pillText: { fontSize: 13, fontWeight: "700" },
    modePill: {
      flex: 1, flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 7,
      paddingHorizontal: 14, paddingVertical: 11, borderRadius: radii.pill, borderWidth: 1.5,
    },
    rankCard: {
      marginTop: space.sm, backgroundColor: colors.bgCard, borderRadius: radii.md,
      borderWidth: 1, borderColor: colors.border, overflow: "hidden",
    },
    rankCardHeader: {
      flexDirection: "row", alignItems: "flex-start", gap: space.sm, padding: space.md,
    },
    rankCardExpanded: {
      padding: space.md, paddingTop: 0,
      borderTopWidth: StyleSheet.hairlineWidth, borderColor: colors.border,
    },
    rankBadge: {
      width: 40, height: 40, borderRadius: radii.pill, backgroundColor: colors.azureSoft,
      alignItems: "center", justifyContent: "center",
    },
    rankBadgeText: { fontSize: 13, fontWeight: "800", color: colors.azure },
    row2: { flexDirection: "row", gap: space.sm },
    col: { flex: 1 },
    label: { fontSize: 12, color: colors.textTer, marginBottom: 4, fontWeight: "600" },
    input: {
      backgroundColor: colors.bgCard, borderWidth: 1, borderColor: colors.border,
      borderRadius: radii.md, paddingHorizontal: space.md, paddingVertical: 11,
      color: colors.textPri, fontSize: 15,
    },
    errorRow: { flexDirection: "row", alignItems: "center", gap: 6, marginTop: 8 },
    errorText: { fontSize: 13, fontWeight: "600" },
    errorBox: {
      flexDirection: "row", alignItems: "center",
      marginTop: space.md, padding: space.md,
      backgroundColor: `${colors.ruby}18`, borderRadius: radii.md,
      borderWidth: 1, borderColor: `${colors.ruby}44`,
    },
    chipsRow: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
    chip: { paddingHorizontal: 14, paddingVertical: 9, borderRadius: radii.pill, borderWidth: 1.5 },
    chipText: { fontSize: 13, fontWeight: "700" },
    runBtn: {
      marginTop: space.xl, backgroundColor: colors.azure, borderRadius: radii.md,
      paddingVertical: 16, alignItems: "center",
    },
    runBtnText: { color: "#fff", fontSize: 16, fontWeight: "700" },
    summaryBox: {
      marginTop: space.lg, padding: space.md,
      backgroundColor: colors.azureSoft, borderRadius: radii.md,
      borderWidth: 1, borderColor: colors.azure + "40",
    },
    summaryText: { fontSize: 12.5, color: colors.textPri, lineHeight: 18 },
    card: {
      marginTop: space.sm, backgroundColor: colors.bgCard, borderRadius: radii.md,
      borderWidth: 1, borderColor: colors.border, padding: space.md,
    },
    cardHeaderRow: { flexDirection: "row", alignItems: "center", gap: 10 },
    periodLabel: { fontSize: 14.5, fontWeight: "800", color: colors.textPri },
    periodDates: { fontSize: 11, color: colors.textTer, marginTop: 1 },
    periodMeta: { fontSize: 11.5, color: colors.textTer },
    fqsBadge: { fontSize: 18, fontWeight: "800" },
    emptyPeriodText: { fontSize: 12, color: colors.textTer, marginTop: space.sm, fontStyle: "italic" },
    metricsGrid: {
      flexDirection: "row", flexWrap: "wrap", gap: space.sm,
      marginTop: space.md, paddingTop: space.sm,
      borderTopWidth: StyleSheet.hairlineWidth, borderColor: colors.border,
    },
    metricCell: { width: "31%" },
    metricLabel: { fontSize: 10, color: colors.textTer, fontWeight: "600", textTransform: "uppercase", letterSpacing: 0.4 },
    metricValue: { fontSize: 13.5, fontWeight: "700", color: colors.textPri, marginTop: 2 },
    emptyText: { fontSize: 13, color: colors.textTer, textAlign: "center", marginTop: space.xl },
    keyPointsHeaderRow: { flexDirection: "row", alignItems: "center", gap: 6, marginTop: space.lg },
    keyPointsBox: {
      marginTop: space.sm, backgroundColor: colors.bgCard, borderRadius: radii.md,
      borderWidth: 1, borderColor: colors.border, padding: space.md, gap: 8,
    },
    keyPointRow: { flexDirection: "row", gap: 8 },
    keyPointBullet: { fontSize: 14, color: colors.azure, fontWeight: "800", lineHeight: 19 },
    keyPointText: { flex: 1, fontSize: 13, color: colors.textPri, lineHeight: 19 },
    exportBtn: {
      flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 8,
      marginTop: space.md, backgroundColor: colors.azure, borderRadius: radii.md, paddingVertical: 15,
    },
    exportBtnText: { color: "#fff", fontSize: 15, fontWeight: "700" },
    tableWrap: {
      marginTop: space.sm, borderRadius: radii.md, borderWidth: 1, borderColor: colors.border,
    },
    tableRow: { flexDirection: "row", borderBottomWidth: StyleSheet.hairlineWidth, borderColor: colors.border },
    tableHeaderCell: {
      paddingHorizontal: 8, paddingVertical: 9, backgroundColor: colors.bgElev,
      borderRightWidth: StyleSheet.hairlineWidth, borderColor: colors.border,
    },
    tableHeaderText: { fontSize: 10.5, fontWeight: "700", color: colors.textSec, textTransform: "uppercase", letterSpacing: 0.3 },
    tableCell: {
      paddingHorizontal: 8, paddingVertical: 10,
      borderRightWidth: StyleSheet.hairlineWidth, borderColor: colors.border, justifyContent: "center",
    },
    tableCellText: { fontSize: 12.5, color: colors.textPri, fontWeight: "600" },
  });
}
