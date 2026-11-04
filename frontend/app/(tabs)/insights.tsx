// Methodology — concise reference for the analyst. Mirrors the Excel model.
// Also hosts the Settings section (theme switcher).

import { useMemo } from "react";
import { View, Text, StyleSheet, ScrollView, Pressable } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Monitor, Sun, Moon } from "lucide-react-native";
import { radii, space, type ThemeColors, type ThemeType } from "@/src/theme";
import { useTheme, type ThemeMode } from "@/src/ThemeProvider";
import { BrandLogo } from "@/src/components/BrandLogo";

export default function InsightsScreen() {
  const { colors, type, mode, setMode, scheme } = useTheme();
  const styles = useMemo(() => makeStyles(colors, type), [colors, type]);
  const benchStyles = useMemo(() => makeBenchStyles(colors), [colors]);

  const TIERS = [
    { range: "90+", label: "Elite", color: colors.tierElite },
    { range: "80–90", label: "Strong", color: colors.tierStrong },
    { range: "70–80", label: "Good", color: colors.tierGood },
    { range: "60–70", label: "Average", color: colors.tierAvg },
    { range: "<60", label: "Weak", color: colors.tierWeak },
  ];

  const EQUITY_W = [
    ["Success Rate", "35%"],
    ["Adj Risk Score", "25%"],
    ["Blended WM", "20%"],
    ["Downside (P10)", "10%"],
    ["Expected Shortfall", "10%"],
  ];
  const NONEQ_W = [
    ["Success Rate", "25%"],
    ["Adj Risk Score", "35%"],
    ["Blended WM", "15%"],
    ["Consistency", "10%"],
    ["Downside (P10)", "10%"],
    ["Expected Shortfall", "5%"],
  ];

  const BENCH: [string, string, string, string, string, string][] = [
    ["Category", "Min XIRR", "3Y", "5Y", "10Y", "15Y"],
    ["Debt", "7%", "1.108", "1.193", "1.443", "1.761"],
    ["Gold / Silver", "8%", "1.125", "1.225", "1.524", "1.922"],
    ["Index Fund", "10%", "1.160", "1.290", "1.710", "2.300"],
    ["Hybrid", "11%", "1.178", "1.325", "1.808", "2.526"],
    ["Large Cap", "12%", "1.197", "1.362", "1.917", "2.776"],
    ["Flexi / ELSS", "13%", "1.214", "1.398", "2.033", "3.054"],
    ["Multi Cap", "14%", "1.233", "1.437", "2.159", "3.366"],
    ["Mid Cap", "15%", "1.253", "1.477", "2.293", "3.714"],
    ["Small Cap", "17%", "1.292", "1.560", "2.593", "4.541"],
  ];

  return (
    <SafeAreaView style={styles.safe} edges={["top"]}>
      <ScrollView contentContainerStyle={{ paddingBottom: 64 }}>
        <View style={styles.header}>
          <BrandLogo />
          <Text style={[type.label, { color: colors.azure }]}>METHODOLOGY</Text>
          <Text style={styles.title}>FQS Model</Text>
          <Text style={[type.bodySm, { marginTop: 6, lineHeight: 20 }]}>
            FQS is a 0–100 composite measuring how reliably a fund creates wealth
            across every rolling SIP window in its history. Built on daily AMFI
            NAVs — no marketing fluff.
          </Text>
        </View>

        {/* ─── APPEARANCE / THEME SWITCHER ─── */}
        <Section title="APPEARANCE" styles={styles} type={type} colors={colors}>
          <Text style={[type.bodySm, { color: colors.textSec, marginBottom: space.sm }]}>
            Currently rendering: <Text style={{ fontWeight: "800", color: colors.textPri }}>
              {scheme === "light" ? "Light" : "Dark"}
            </Text>{mode === "system" && " · following system"}
          </Text>
          <View style={styles.themeRow}>
            <ThemeOption
              active={mode === "system"}
              label="System"
              icon={<Monitor size={16} color={mode === "system" ? colors.textPri : colors.textSec} />}
              onPress={() => setMode("system")}
              colors={colors}
              type={type}
            />
            <ThemeOption
              active={mode === "light"}
              label="Light"
              icon={<Sun size={16} color={mode === "light" ? colors.textPri : colors.textSec} />}
              onPress={() => setMode("light")}
              colors={colors}
              type={type}
            />
            <ThemeOption
              active={mode === "dark"}
              label="Dark"
              icon={<Moon size={16} color={mode === "dark" ? colors.textPri : colors.textSec} />}
              onPress={() => setMode("dark")}
              colors={colors}
              type={type}
            />
          </View>
        </Section>

        <Section title="THE CORE UNIT — WEALTH MULTIPLE" styles={styles} type={type} colors={colors}>
          <Text style={type.body}>
            <Text style={{ color: colors.tierElite, fontWeight: "700" }}>WM = Final Value ÷ Total Invested</Text>.
            ₹1 invested became ₹2 → WM = 2.0x. Horizon-agnostic.
          </Text>
        </Section>

        <Section title="PIPELINE" styles={styles} type={type} colors={colors}>
          <Step n="1" title="Daily NAV (AMFI)" desc="api.mfapi.in/mf/{scheme_code} — full history." colors={colors} type={type} />
          <Step n="2" title="Month-end snapshot" desc="Last NAV in each calendar month — the real SIP touchpoint." colors={colors} type={type} />
          <Step n="3" title="Headline XIRR" desc="Full-history SIP simulation via Newton-Raphson." colors={colors} type={type} />
          <Step n="4" title="Rolling SIP windows (1Y–20Y)" desc="Every possible window, monthly step. WM per window." colors={colors} type={type} />
          <Step n="5" title="Distribution stats" desc="Min, P10, P25, Median, P75, P90, Max, σ, CV per horizon." colors={colors} type={type} />
          <Step n="6" title="Sub-scores → FQS" desc="Weighted composite (equity vs non-equity weights)." colors={colors} type={type} />
        </Section>

        <Section title="EQUITY WEIGHTS" styles={styles} type={type} colors={colors}>
          {EQUITY_W.map(([k, v]) => <KV key={k} k={k} v={v} colors={colors} type={type} />)}
        </Section>
        <Section title="NON-EQUITY WEIGHTS" styles={styles} type={type} colors={colors}>
          {NONEQ_W.map(([k, v]) => <KV key={k} k={k} v={v} colors={colors} type={type} />)}
        </Section>

        <Section title="RATING TIERS" styles={styles} type={type} colors={colors}>
          {TIERS.map((t) => (
            <View key={t.label} style={styles.tierRow}>
              <View style={[styles.tierPill, { backgroundColor: `${t.color}25`, borderColor: t.color }]}>
                <Text style={[styles.tierVal, { color: t.color }]}>{t.range}</Text>
              </View>
              <Text style={[type.body, { fontWeight: "700", color: t.color, flex: 1 }]}>{t.label}</Text>
            </View>
          ))}
        </Section>

        <Section title="MIN WM BENCHMARK (CATEGORY × HORIZON)" styles={styles} type={type} colors={colors}>
          <View>
            {BENCH.map((row, i) => (
              <View key={i} style={[benchStyles.row, i === 0 && benchStyles.head]}>
                {row.map((c, j) => (
                  <Text key={j} style={[benchStyles.cell, i === 0 && benchStyles.headCell, j === 0 && { flex: 1.6, textAlign: "left" }]}>
                    {c}
                  </Text>
                ))}
              </View>
            ))}
          </View>
        </Section>

        <Section title="START-DATE FILTER" styles={styles} type={type} colors={colors}>
          <Text style={type.body}>
            On the Fund Analysis screen, tap the filter row to recompute every horizon
            from a chosen start date. Useful for testing "what if I'd started after
            COVID" / "post 2018 SEBI re-classification" scenarios.
          </Text>
        </Section>

        <Section title="DAILY NAV REFRESH" styles={styles} type={type} colors={colors}>
          <Text style={type.body}>
            The backend re-fetches NAV nightly at 03:30 IST for every fund analysed
            in the last 30 days. Run on-demand from the funds tab refresh button.
          </Text>
        </Section>
      </ScrollView>
    </SafeAreaView>
  );
}

function ThemeOption({
  active, label, icon, onPress, colors, type,
}: {
  active: boolean;
  label: string;
  icon: React.ReactNode;
  onPress: () => void;
  colors: ThemeColors;
  type: ThemeType;
}) {
  return (
    <Pressable
      onPress={onPress}
      style={({ pressed }) => [
        {
          flex: 1,
          flexDirection: "row",
          alignItems: "center",
          justifyContent: "center",
          gap: 6,
          paddingVertical: 10,
          borderRadius: radii.sm,
          borderWidth: 1,
          borderColor: active ? colors.azure : colors.border,
          backgroundColor: active ? colors.azureSoft : "transparent",
        },
        pressed && { opacity: 0.85 },
      ]}
    >
      {icon}
      <Text style={[type.bodySm, { color: active ? colors.textPri : colors.textSec, fontWeight: "700" }]}>
        {label}
      </Text>
    </Pressable>
  );
}

function Section({
  title, children, styles, type, colors,
}: { title: string; children: React.ReactNode; styles: ReturnType<typeof makeStyles>; type: ThemeType; colors: ThemeColors }) {
  return (
    <View style={styles.section}>
      <Text style={[type.label, { color: colors.textTer, marginBottom: space.sm }]}>{title}</Text>
      <View style={styles.body}>{children}</View>
    </View>
  );
}

function KV({ k, v, colors, type }: { k: string; v: string; colors: ThemeColors; type: ThemeType }) {
  return (
    <View style={{
      flexDirection: "row", alignItems: "center", paddingVertical: 6,
      borderBottomWidth: StyleSheet.hairlineWidth, borderColor: colors.border,
    }}>
      <Text style={[type.body, { color: colors.textSec, flex: 1 }]}>{k}</Text>
      <Text style={[type.body, { color: colors.textPri, fontWeight: "800", fontVariant: ["tabular-nums"] }]}>{v}</Text>
    </View>
  );
}

function Step({
  n, title, desc, colors, type,
}: { n: string; title: string; desc: string; colors: ThemeColors; type: ThemeType }) {
  return (
    <View style={{ flexDirection: "row", gap: space.md, alignItems: "flex-start" }}>
      <View style={{
        width: 28, height: 28, borderRadius: 14, backgroundColor: colors.bgElev,
        borderWidth: 1, borderColor: colors.border,
        alignItems: "center", justifyContent: "center",
      }}>
        <Text style={{ color: colors.azure, fontWeight: "800", fontSize: 12 }}>{n}</Text>
      </View>
      <View style={{ flex: 1 }}>
        <Text style={[type.body, { fontWeight: "700" }]}>{title}</Text>
        <Text style={[type.bodySm, { marginTop: 2 }]}>{desc}</Text>
      </View>
    </View>
  );
}

function makeStyles(colors: ThemeColors, type: ThemeType) {
  return StyleSheet.create({
    safe: { flex: 1, backgroundColor: colors.bg },
    header: { paddingHorizontal: space.md, paddingTop: space.md, paddingBottom: space.lg },
    title: { ...type.h1, marginTop: 4 },
    section: { paddingHorizontal: space.md, marginBottom: space.lg },
    body: { padding: space.lg, backgroundColor: colors.bgCard, borderRadius: radii.md, borderWidth: 1, borderColor: colors.border, gap: space.sm },
    themeRow: { flexDirection: "row", gap: space.sm, marginTop: 4 },
    tierRow: { flexDirection: "row", alignItems: "center", gap: space.md, paddingVertical: 4 },
    tierPill: { width: 76, paddingVertical: 6, borderRadius: radii.sm, borderWidth: 1, alignItems: "center" },
    tierVal: { fontSize: 14, fontWeight: "800", fontVariant: ["tabular-nums"] },
  });
}

function makeBenchStyles(colors: ThemeColors) {
  return StyleSheet.create({
    row: { flexDirection: "row", paddingVertical: 8, borderBottomWidth: StyleSheet.hairlineWidth, borderColor: colors.border },
    head: { backgroundColor: colors.bgElev, borderRadius: radii.sm, paddingHorizontal: 4 },
    cell: { flex: 1, color: colors.textPri, fontSize: 12, fontVariant: ["tabular-nums"], textAlign: "right", paddingHorizontal: 4 },
    headCell: { color: colors.textSec, fontWeight: "700", letterSpacing: 0.5, fontSize: 11, textTransform: "uppercase" },
  });
}
