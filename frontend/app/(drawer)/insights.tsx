// Methodology — visual reference for the FQS model.
// Redesigned with gradient hero, weight bars, connected pipeline, tier showcase.

import { useMemo } from "react";
import { View, Text, StyleSheet, ScrollView, Pressable } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { LinearGradient } from "expo-linear-gradient";
import {
  Monitor, Sun, Moon,
  Target, Shield, TrendingUp, BarChart2,
  Award, Zap, Activity, Layers, CheckCircle,
  Database, Calculator, ChevronRight,
} from "lucide-react-native";
import { radii, space, type ThemeColors, type ThemeType } from "@/src/theme";
import { useTheme, type ThemeMode } from "@/src/ThemeProvider";
import { useMode } from "@/src/ModeProvider";

// ── Data ─────────────────────────────────────────────────────────────────────

const TIERS = [
  { range: "90–100", label: "Elite",   color: "#10B981", desc: "Exceptional consistency" },
  { range: "80–89",  label: "Strong",  color: "#3B82F6", desc: "Above-average reliability" },
  { range: "70–79",  label: "Good",    color: "#F59E0B", desc: "Solid, minor lapses" },
  { range: "60–69",  label: "Average", color: "#F97316", desc: "Passes the bar, barely" },
  { range: "0–59",   label: "Weak",    color: "#EF4444", desc: "Frequent underperformance" },
];

const EQUITY_W = [
  { label: "Success Rate",      pct: 30, icon: CheckCircle, color: "#10B981" },
  { label: "Adj Risk Score",    pct: 20, icon: Shield,      color: "#3B82F6" },
  { label: "Blended WM",        pct: 20, icon: TrendingUp,  color: "#F59E0B" },
  { label: "Consistency (CV)",  pct: 15, icon: Target,      color: "#8B5CF6" },
  { label: "Downside (P10)",    pct: 10, icon: Activity,    color: "#F97316" },
  { label: "Expected Shortfall",pct:  5, icon: Zap,         color: "#EF4444" },
];

const NONEQ_W = [
  { label: "Adj Risk Score",    pct: 35, icon: Shield,      color: "#3B82F6" },
  { label: "Success Rate",      pct: 25, icon: CheckCircle, color: "#10B981" },
  { label: "Blended WM",        pct: 15, icon: TrendingUp,  color: "#F59E0B" },
  { label: "Consistency",       pct: 10, icon: Target,      color: "#8B5CF6" },
  { label: "Downside (P10)",    pct: 10, icon: Activity,    color: "#F97316" },
  { label: "Expected Shortfall",pct:  5, icon: Zap,         color: "#EF4444" },
];

const SIP_PIPELINE = [
  { icon: Database,    color: "#3B82F6", title: "Daily NAV (AMFI)",         desc: "Full history via api.mfapi.in — merger stitching applied for wound-up funds." },
  { icon: Layers,      color: "#8B5CF6", title: "Month-end snapshot",       desc: "Last NAV in each calendar month — real SIP touchpoint." },
  { icon: Calculator,  color: "#10B981", title: "Headline XIRR",            desc: "Full-history SIP simulation via Newton-Raphson." },
  { icon: BarChart2,   color: "#F59E0B", title: "Rolling SIP windows",      desc: "Every possible window (1Y–50Y), monthly step. WM per window." },
  { icon: Activity,    color: "#F97316", title: "Distribution stats",       desc: "Min, P10, P25, Median, P75, P90, Max, σ, CV per horizon." },
  { icon: Award,       color: "#EF4444", title: "Sub-scores → FQS",         desc: "Weighted composite using equity or non-equity weights." },
];

const LUMPSUM_PIPELINE = [
  { icon: Database,    color: "#3B82F6", title: "Daily NAV (AMFI)",         desc: "Full history via api.mfapi.in — merger stitching applied for wound-up funds." },
  { icon: Layers,      color: "#8B5CF6", title: "Month-end snapshot",       desc: "Last NAV each month as invest/redeem touchpoint." },
  { icon: TrendingUp,  color: "#10B981", title: "Rolling lumpsum windows",  desc: "1Y–50Y rolling periods, monthly step. WM = ending NAV ÷ starting NAV." },
  { icon: BarChart2,   color: "#F59E0B", title: "Distribution stats",       desc: "Min, Max, Mean, Median, P10/P25/P75/P90, σ, CV per horizon." },
  { icon: Shield,      color: "#F97316", title: "Cycle Robustness",         desc: "10Y: 0.35×S3 + 0.35×S5 + 0.30×S10. 15Y: 0.30×S3 + 0.30×S5 + 0.25×S10 + 0.15×S15." },
  { icon: Award,       color: "#EF4444", title: "Sub-scores → FQS",         desc: "Same equity/non-equity weight structure as SIP." },
];

const BENCH: [string, string, string, string, string, string][] = [
  ["Category",      "XIRR",  "3Y",   "5Y",   "10Y",  "15Y"],
  ["Debt",          "7%",  "1.108","1.193","1.443","1.761"],
  ["Gold/Silver",   "8%",  "1.125","1.225","1.524","1.922"],
  ["Index Fund",   "10%",  "1.160","1.290","1.710","2.300"],
  ["Hybrid",       "11%",  "1.178","1.325","1.808","2.526"],
  ["Large Cap",    "12%",  "1.197","1.362","1.917","2.776"],
  ["Flexi/ELSS",   "13%",  "1.214","1.398","2.033","3.054"],
  ["Multi Cap",    "14%",  "1.233","1.437","2.159","3.366"],
  ["Mid Cap",      "15%",  "1.253","1.477","2.293","3.714"],
  ["Small Cap",    "17%",  "1.292","1.560","2.593","4.541"],
];

// ── Screen ────────────────────────────────────────────────────────────────────

export default function InsightsScreen() {
  const { colors, type, mode: themeMode, setMode: setThemeMode, scheme } = useTheme();
  const { mode: investMode } = useMode();
  const isLumpsum = investMode === "lumpsum";

  const pipeline  = isLumpsum ? LUMPSUM_PIPELINE : SIP_PIPELINE;
  const eqWeights = EQUITY_W;
  const neWeights = NONEQ_W;

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }} edges={["top"]}>
      <ScrollView contentContainerStyle={{ paddingBottom: 80 }} showsVerticalScrollIndicator={false}>

        {/* ── Hero header ── */}
        <LinearGradient
          colors={scheme === "dark"
            ? ["#0F1729", "#0A0F1E", colors.bg]
            : ["#1E3A8A", "#2563EB", "#60A5FA20"]}
          start={{ x: 0, y: 0 }}
          end={{ x: 1, y: 1 }}
          style={heroStyles.wrap}
        >
          <View style={heroStyles.tagRow}>
            <View style={heroStyles.tag}>
              <Text style={heroStyles.tagText}>
                {isLumpsum ? "LUMPSUM MODEL" : "SIP MODEL"}
              </Text>
            </View>
          </View>

          <Text style={heroStyles.scoreLabel}>FQS</Text>
          <Text style={heroStyles.scoreSub}>Fund Quality Score</Text>
          <Text style={heroStyles.desc}>
            A 0–100 composite measuring how reliably a fund creates wealth
            across every rolling {isLumpsum ? "lumpsum window" : "SIP window"} in its history.
            Built on daily AMFI NAVs — no marketing fluff.
          </Text>

          {/* Mini score ring — decorative */}
          <View style={heroStyles.ringWrap}>
            {TIERS.map((t, i) => (
              <View
                key={t.label}
                style={[
                  heroStyles.ringDot,
                  { backgroundColor: t.color, opacity: 1 - i * 0.15 },
                ]}
              />
            ))}
          </View>
        </LinearGradient>

        {/* ── Tier showcase ── */}
        <SectionHeader label="RATING TIERS" colors={colors} type={type} />
        <View style={{ paddingHorizontal: space.md, gap: 6, marginBottom: space.lg }}>
          {TIERS.map((t) => (
            <View
              key={t.label}
              style={[tierStyles.row, { backgroundColor: t.color + "14", borderColor: t.color + "40" }]}
            >
              <View style={[tierStyles.badge, { backgroundColor: t.color }]}>
                <Text style={tierStyles.badgeRange}>{t.range}</Text>
              </View>
              <View style={{ flex: 1, marginLeft: space.md }}>
                <Text style={[tierStyles.label, { color: t.color }]}>{t.label}</Text>
                <Text style={[tierStyles.desc, { color: colors.textSec }]}>{t.desc}</Text>
              </View>
            </View>
          ))}
        </View>

        {/* ── Pipeline ── */}
        <SectionHeader label="HOW IT'S COMPUTED" colors={colors} type={type} />
        <View style={{ paddingHorizontal: space.md, marginBottom: space.lg }}>
          {pipeline.map((step, i) => (
            <View key={step.title} style={pipeStyles.step}>
              {/* Connecting line */}
              {i < pipeline.length - 1 && (
                <View style={[pipeStyles.line, { backgroundColor: step.color + "40" }]} />
              )}
              <View style={[pipeStyles.iconWrap, { backgroundColor: step.color + "20", borderColor: step.color + "60" }]}>
                <step.icon size={18} color={step.color} />
              </View>
              <View style={{ flex: 1, marginLeft: space.md }}>
                <Text style={[pipeStyles.title, { color: colors.textPri }]}>{step.title}</Text>
                <Text style={[pipeStyles.desc, { color: colors.textSec }]}>{step.desc}</Text>
              </View>
              <View style={[pipeStyles.stepNum, { backgroundColor: colors.bgCard, borderColor: colors.border }]}>
                <Text style={[pipeStyles.stepNumText, { color: colors.textTer }]}>{i + 1}</Text>
              </View>
            </View>
          ))}
        </View>

        {/* ── Score weights ── */}
        <SectionHeader label="EQUITY FUND WEIGHTS" colors={colors} type={type} />
        <WeightChart weights={eqWeights} colors={colors} type={type} />

        <SectionHeader label="NON-EQUITY FUND WEIGHTS" colors={colors} type={type} />
        <WeightChart weights={neWeights} colors={colors} type={type} />

        {/* ── Wealth Multiple ── */}
        <SectionHeader label="THE CORE UNIT" colors={colors} type={type} />
        <View style={[cardStyles.card, { backgroundColor: colors.bgCard, borderColor: colors.border, marginHorizontal: space.md, marginBottom: space.lg }]}>
          <View style={{ flexDirection: "row", alignItems: "center", gap: space.sm, marginBottom: space.sm }}>
            <View style={{ width: 36, height: 36, borderRadius: 10, backgroundColor: "#10B98120", alignItems: "center", justifyContent: "center" }}>
              <TrendingUp size={18} color="#10B981" />
            </View>
            <Text style={[type.body, { color: colors.textPri, fontWeight: "700" }]}>Wealth Multiple (WM)</Text>
          </View>
          <Text style={[type.body, { color: colors.textSec, lineHeight: 22 }]}>
            <Text style={{ color: "#10B981", fontWeight: "700" }}>WM = Final Value ÷ Total Invested</Text>
            {"\n"}₹1 invested → ₹2 means WM = 2.0×. Horizon-agnostic and directly comparable across all fund types.
          </Text>
        </View>

        {/* ── Min WM benchmark ── */}
        <SectionHeader label="MIN WM BENCHMARK (CATEGORY × HORIZON)" colors={colors} type={type} />
        <View style={{ marginHorizontal: space.md, marginBottom: space.lg, borderRadius: radii.md, overflow: "hidden", borderWidth: 1, borderColor: colors.border }}>
          {BENCH.map((row, i) => (
            <View
              key={i}
              style={[
                benchStyles.row,
                i === 0 && { backgroundColor: colors.azure + "18" },
                i > 0 && i % 2 === 0 && { backgroundColor: colors.bgCard },
              ]}
            >
              {row.map((cell, j) => (
                <Text
                  key={j}
                  style={[
                    benchStyles.cell,
                    { color: i === 0 ? colors.azure : colors.textPri },
                    i === 0 && { fontWeight: "700", fontSize: 10, letterSpacing: 0.5 },
                    j === 0 && { flex: 1.5, textAlign: "left", color: i === 0 ? colors.azure : colors.textSec },
                  ]}
                >
                  {cell}
                </Text>
              ))}
            </View>
          ))}
        </View>

        {/* ── Appearance ── */}
        <SectionHeader label="APPEARANCE" colors={colors} type={type} />
        <View style={[cardStyles.card, { backgroundColor: colors.bgCard, borderColor: colors.border, marginHorizontal: space.md, marginBottom: space.lg }]}>
          <Text style={[type.bodySm, { color: colors.textSec, marginBottom: space.sm }]}>
            Currently rendering:{" "}
            <Text style={{ fontWeight: "800", color: colors.textPri }}>
              {scheme === "light" ? "Light" : "Dark"}
            </Text>
            {themeMode === "system" && " · following system"}
          </Text>
          <View style={{ flexDirection: "row", gap: space.sm }}>
            {(["system", "light", "dark"] as ThemeMode[]).map((m) => {
              const Icon = m === "system" ? Monitor : m === "light" ? Sun : Moon;
              const labels = { system: "System", light: "Light", dark: "Dark" };
              const active = themeMode === m;
              return (
                <Pressable
                  key={m}
                  onPress={() => setThemeMode(m)}
                  style={({ pressed }) => [
                    themeStyles.btn,
                    {
                      borderColor: active ? colors.azure : colors.border,
                      backgroundColor: active ? colors.azure + "18" : "transparent",
                      opacity: pressed ? 0.8 : 1,
                    },
                  ]}
                >
                  <Icon size={15} color={active ? colors.azure : colors.textSec} />
                  <Text style={[type.bodySm, { color: active ? colors.azure : colors.textSec, fontWeight: "700" }]}>
                    {labels[m]}
                  </Text>
                </Pressable>
              );
            })}
          </View>
        </View>

      </ScrollView>
    </SafeAreaView>
  );
}

// ── Sub-components ────────────────────────────────────────────────────────────

function SectionHeader({ label, colors, type }: { label: string; colors: ThemeColors; type: ThemeType }) {
  return (
    <View style={{ paddingHorizontal: space.md, paddingBottom: space.sm, paddingTop: space.lg, flexDirection: "row", alignItems: "center", gap: space.sm }}>
      <View style={{ flex: 1, height: 1, backgroundColor: colors.border }} />
      <Text style={[type.label, { color: colors.textTer }]}>{label}</Text>
      <View style={{ flex: 1, height: 1, backgroundColor: colors.border }} />
    </View>
  );
}

function WeightChart({
  weights, colors, type,
}: {
  weights: typeof EQUITY_W;
  colors: ThemeColors;
  type: ThemeType;
}) {
  const maxPct = Math.max(...weights.map((w) => w.pct));
  return (
    <View style={[cardStyles.card, { backgroundColor: colors.bgCard, borderColor: colors.border, marginHorizontal: space.md, marginBottom: space.lg, gap: 10 }]}>
      {weights.map((w) => (
        <View key={w.label} style={{ gap: 4 }}>
          <View style={{ flexDirection: "row", alignItems: "center", justifyContent: "space-between" }}>
            <View style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
              <w.icon size={13} color={w.color} />
              <Text style={[type.bodySm, { color: colors.textPri, fontWeight: "600" }]}>{w.label}</Text>
            </View>
            <Text style={[type.bodySm, { color: w.color, fontWeight: "700", fontVariant: ["tabular-nums"] }]}>
              {w.pct}%
            </Text>
          </View>
          <View style={{ height: 6, backgroundColor: colors.bgElev, borderRadius: 3, overflow: "hidden" }}>
            <View
              style={{
                height: 6,
                width: `${(w.pct / maxPct) * 100}%`,
                backgroundColor: w.color,
                borderRadius: 3,
              }}
            />
          </View>
        </View>
      ))}
    </View>
  );
}

// ── Styles ────────────────────────────────────────────────────────────────────

const heroStyles = StyleSheet.create({
  wrap: {
    paddingHorizontal: space.md,
    paddingTop: space.xl,
    paddingBottom: space.xl + 8,
  },
  tagRow: { flexDirection: "row", marginBottom: space.md },
  tag: {
    paddingHorizontal: 10,
    paddingVertical: 4,
    borderRadius: radii.pill,
    backgroundColor: "rgba(255,255,255,0.12)",
    borderWidth: 1,
    borderColor: "rgba(255,255,255,0.2)",
  },
  tagText: { fontSize: 10, fontWeight: "700", color: "#fff", letterSpacing: 1.5 },
  scoreLabel: {
    fontSize: 72,
    fontWeight: "800",
    color: "#fff",
    letterSpacing: -3,
    lineHeight: 72,
  },
  scoreSub: {
    fontSize: 18,
    fontWeight: "600",
    color: "rgba(255,255,255,0.7)",
    marginTop: 4,
    marginBottom: space.md,
  },
  desc: {
    fontSize: 14,
    color: "rgba(255,255,255,0.65)",
    lineHeight: 21,
    maxWidth: 320,
  },
  ringWrap: {
    flexDirection: "row",
    gap: 6,
    marginTop: space.lg,
  },
  ringDot: {
    width: 8,
    height: 8,
    borderRadius: 4,
  },
});

const tierStyles = StyleSheet.create({
  row: {
    flexDirection: "row",
    alignItems: "center",
    padding: space.sm + 2,
    borderRadius: radii.md,
    borderWidth: 1,
  },
  badge: {
    paddingHorizontal: 10,
    paddingVertical: 6,
    borderRadius: radii.sm,
    minWidth: 64,
    alignItems: "center",
  },
  badgeRange: { fontSize: 12, fontWeight: "800", color: "#fff", fontVariant: ["tabular-nums"] },
  label: { fontSize: 14, fontWeight: "700" },
  desc:  { fontSize: 12, marginTop: 1 },
});

const pipeStyles = StyleSheet.create({
  step: {
    flexDirection: "row",
    alignItems: "flex-start",
    marginBottom: space.md,
    position: "relative",
  },
  line: {
    position: "absolute",
    left: 19,
    top: 40,
    width: 2,
    height: space.md + 8,
    zIndex: 0,
  },
  iconWrap: {
    width: 40,
    height: 40,
    borderRadius: radii.md,
    borderWidth: 1,
    alignItems: "center",
    justifyContent: "center",
    zIndex: 1,
  },
  title:   { fontSize: 14, fontWeight: "700", lineHeight: 20 },
  desc:    { fontSize: 12, lineHeight: 17, marginTop: 2 },
  stepNum: {
    width: 22,
    height: 22,
    borderRadius: 11,
    borderWidth: 1,
    alignItems: "center",
    justifyContent: "center",
    marginLeft: space.sm,
    marginTop: 9,
  },
  stepNumText: { fontSize: 10, fontWeight: "700" },
});

const cardStyles = StyleSheet.create({
  card: { padding: space.md, borderRadius: radii.md, borderWidth: 1 },
});

const benchStyles = StyleSheet.create({
  row: {
    flexDirection: "row",
    paddingVertical: 9,
    paddingHorizontal: space.sm,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: "rgba(128,128,128,0.12)",
  },
  cell: {
    flex: 1,
    fontSize: 12,
    fontVariant: ["tabular-nums"],
    textAlign: "right",
    paddingHorizontal: 2,
  },
});

const themeStyles = StyleSheet.create({
  btn: {
    flex: 1,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 6,
    paddingVertical: 10,
    borderRadius: radii.sm,
    borderWidth: 1,
  },
});
