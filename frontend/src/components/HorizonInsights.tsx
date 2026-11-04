// Three customer-facing widgets layered on top of a HorizonStats payload:
//
//   PlainEnglishSummary   — 3 auto-generated bullets translating the numbers
//   SmartInsights         — colour-coded call-outs surfacing notable facts
//   WmHistogram           — visual bucket-bar chart of WM distribution
//
// Each is theme-aware and contains no business logic beyond mapping stats →
// human-readable strings or chart geometry.

import { useMemo } from "react";
import { StyleSheet, Text, View } from "react-native";
import { CheckCircle2, AlertCircle, TrendingUp } from "lucide-react-native";
import type { HorizonStats, LumpsumHorizonStats } from "@/src/api";
import { radii, space, type ThemeColors, type ThemeType } from "@/src/theme";
import { useTheme } from "@/src/ThemeProvider";

// These three widgets only ever touch the fields that are named identically
// on both HorizonStats (SIP) and LumpsumHorizonStats (Lumpsum) — median_wm,
// p10_wm, success_pct, min_wm_required, cv, n_windows, horizon and
// windows[].wealth_multiple — so one component serves both calculation
// engines. `mode` only swaps the copy ("SIP" vs "Lumpsum" wording); the
// numbers themselves already come from the right engine via `h`.
export type AnyHorizonStats = HorizonStats | LumpsumHorizonStats;
export type InvestmentMode = "sip" | "lumpsum";

const MODE_WORD: Record<InvestmentMode, { noun: string; plural: string; verb: string }> = {
  sip: { noun: "SIP", plural: "SIPs", verb: "invested" },
  lumpsum: { noun: "lumpsum investment", plural: "lumpsum investments", verb: "invested" },
};

// ─── Plain-English summary ─────────────────────────────────────────────────

export function PlainEnglishSummary({ h, mode = "sip" }: { h: AnyHorizonStats; mode?: InvestmentMode }) {
  const { colors, type } = useTheme();
  const styles = useMemo(() => makeStyles(colors, type), [colors, type]);
  const w = MODE_WORD[mode];

  const median = h.median_wm;
  const p10 = h.p10_wm;
  const successPct = (h.success_pct * 100).toFixed(0);
  const minReq = h.min_wm_required;

  return (
    <View style={styles.summary}>
      <Text style={[type.label, { color: colors.azure, marginBottom: space.sm }]}>
        WHAT THIS MEANS · {h.horizon}Y {w.noun.toUpperCase()}
      </Text>
      <Bullet
        text={`Half of all ${h.horizon}-year ${w.plural} ended at `}
        boldText={`${median.toFixed(2)}× or more`}
        suffix={` of your invested money.`}
        colors={colors}
        type={type}
      />
      <Bullet
        text="Even in the worst 10% of windows, you'd have ended at "
        boldText={`${p10.toFixed(2)}×`}
        suffix={` your investment.`}
        colors={colors}
        type={type}
      />
      <Bullet
        text=""
        boldText={`${successPct}% of windows`}
        suffix={` beat the category benchmark of ${minReq.toFixed(2)}×.`}
        colors={colors}
        type={type}
      />
    </View>
  );
}

function Bullet({
  text, boldText, suffix, colors, type,
}: { text: string; boldText: string; suffix: string; colors: ThemeColors; type: ThemeType }) {
  return (
    <View style={{ flexDirection: "row", marginBottom: 6 }}>
      <Text style={[type.body, { color: colors.textSec, marginRight: 8 }]}>•</Text>
      <Text style={[type.body, { color: colors.textPri, flex: 1, lineHeight: 22 }]}>
        {text}
        <Text style={{ fontWeight: "800", color: colors.textPri }}>{boldText}</Text>
        {suffix}
      </Text>
    </View>
  );
}

// ─── Smart insights ────────────────────────────────────────────────────────

type InsightKind = "good" | "warn" | "bad";

interface Insight {
  kind: InsightKind;
  text: string;
}

function generateInsights(h: AnyHorizonStats, mode: InvestmentMode): Insight[] {
  const out: Insight[] = [];
  const w = MODE_WORD[mode];

  if (h.success_pct >= 0.99) {
    out.push({
      kind: "good",
      text: `100% success rate — every ${h.horizon}Y ${w.noun} window beat the benchmark.`,
    });
  } else if (h.success_pct >= 0.9) {
    out.push({
      kind: "good",
      text: `${(h.success_pct * 100).toFixed(0)}% of ${h.horizon}Y windows beat the benchmark.`,
    });
  } else if (h.success_pct < 0.5) {
    out.push({
      kind: "bad",
      text: `${((1 - h.success_pct) * 100).toFixed(0)}% failure rate at ${h.horizon}Y — be patient at this horizon.`,
    });
  }

  if (h.median_wm >= 3) {
    out.push({
      kind: "good",
      text: `Strong median outcome — most ${h.horizon}Y ${w.plural} more than tripled your invested capital.`,
    });
  } else if (h.median_wm >= 2) {
    out.push({
      kind: "good",
      text: `Most ${h.horizon}Y ${w.plural} at least doubled your invested capital.`,
    });
  } else if (h.median_wm < 1 && h.horizon >= 3) {
    out.push({
      kind: "bad",
      text: `Median outcome below 1× — typical investor lost money at ${h.horizon}Y.`,
    });
  }

  if (h.p10_wm < 1) {
    out.push({
      kind: "bad",
      text: `Bottom 10% of windows ended below 1× — real capital loss is possible at this horizon.`,
    });
  }

  if (h.cv > 0.30) {
    out.push({
      kind: "warn",
      text: `High variance (CV ${(h.cv * 100).toFixed(0)}%) — outcomes spread widely.`,
    });
  } else if (h.cv < 0.10) {
    out.push({
      kind: "good",
      text: `Low variance (CV ${(h.cv * 100).toFixed(0)}%) — outcomes are tightly clustered.`,
    });
  }

  return out.slice(0, 3);
}

export function SmartInsights({ h, mode = "sip" }: { h: AnyHorizonStats; mode?: InvestmentMode }) {
  const { colors, type } = useTheme();
  const insights = useMemo(() => generateInsights(h, mode), [h, mode]);
  if (insights.length === 0) return null;

  return (
    <View style={{ marginHorizontal: space.md, marginTop: space.md, gap: space.sm }}>
      {insights.map((it, i) => {
        const palette =
          it.kind === "good" ? { fg: colors.tierElite, bg: `${colors.tierElite}1a` } :
          it.kind === "bad"  ? { fg: colors.tierWeak,  bg: `${colors.tierWeak}1a` } :
                               { fg: colors.tierGood,  bg: `${colors.tierGood}1a` };
        const Icon = it.kind === "good" ? CheckCircle2
                   : it.kind === "bad"  ? AlertCircle
                   : TrendingUp;
        return (
          <View
            key={i}
            style={{
              flexDirection: "row", alignItems: "center",
              padding: space.md, borderRadius: radii.sm,
              borderWidth: 1, borderColor: palette.fg + "55",
              backgroundColor: palette.bg,
            }}
          >
            <Icon size={16} color={palette.fg} />
            <Text style={[type.body, { color: colors.textPri, flex: 1, marginLeft: 10, lineHeight: 20 }]}>
              {it.text}
            </Text>
          </View>
        );
      })}
    </View>
  );
}

// ─── WM distribution histogram ────────────────────────────────────────────

export function WmHistogram({ h }: { h: AnyHorizonStats }) {
  const { colors, type } = useTheme();
  const styles = useMemo(() => makeStyles(colors, type), [colors, type]);

  const { buckets, maxCount, lo, hi, benchmarkRatio } = useMemo(() => {
    const wms = h.windows.map((w) => w.wealth_multiple);
    if (wms.length === 0) return { buckets: [] as { count: number; passed: boolean; midpoint: number }[], maxCount: 0, lo: 0, hi: 0, benchmarkRatio: 0 };
    const lo = Math.min(...wms);
    const hi = Math.max(...wms);
    const span = Math.max(0.0001, hi - lo);
    const B = 12;
    const arr: { count: number; passed: boolean; midpoint: number }[] = Array.from(
      { length: B },
      (_, i) => ({
        count: 0,
        passed: false,
        midpoint: lo + ((i + 0.5) / B) * span,
      }),
    );
    for (const wm of wms) {
      const idx = Math.min(B - 1, Math.max(0, Math.floor(((wm - lo) / span) * B)));
      arr[idx].count++;
    }
    for (const b of arr) b.passed = b.midpoint >= h.min_wm_required;
    const maxCount = Math.max(1, ...arr.map((b) => b.count));
    const benchmarkRatio = Math.max(0, Math.min(1, (h.min_wm_required - lo) / span));
    return { buckets: arr, maxCount, lo, hi, benchmarkRatio };
  }, [h]);

  if (buckets.length === 0) return null;

  return (
    <View style={styles.histWrap}>
      <Text style={[type.label, { color: colors.textTer, marginBottom: space.sm }]}>
        WM DISTRIBUTION — ALL {h.n_windows} WINDOWS
      </Text>
      <View style={styles.histArea}>
        {buckets.map((b, i) => (
          <View
            key={i}
            style={{
              flex: 1,
              height: `${(b.count / maxCount) * 100}%`,
              backgroundColor: b.passed ? colors.tierElite : colors.tierWeak,
              opacity: b.count === 0 ? 0.15 : 0.85,
              marginHorizontal: 1,
              borderTopLeftRadius: 2, borderTopRightRadius: 2,
            }}
          />
        ))}
        {/* Benchmark vertical line */}
        <View
          style={[
            styles.benchmarkLine,
            { left: `${benchmarkRatio * 100}%`, backgroundColor: colors.amber },
          ]}
        />
      </View>
      <View style={styles.histScale}>
        <Text style={styles.scaleNum}>{lo.toFixed(2)}×</Text>
        <Text style={[styles.scaleNum, { color: colors.amber, fontWeight: "700" }]}>
          {h.min_wm_required.toFixed(2)}× ← bar
        </Text>
        <Text style={styles.scaleNum}>{hi.toFixed(2)}×</Text>
      </View>
      <View style={styles.histLegend}>
        <View style={styles.legendItem}>
          <View style={[styles.legendDot, { backgroundColor: colors.tierElite }]} />
          <Text style={styles.legendText}>Beat benchmark</Text>
        </View>
        <View style={styles.legendItem}>
          <View style={[styles.legendDot, { backgroundColor: colors.tierWeak }]} />
          <Text style={styles.legendText}>Below benchmark</Text>
        </View>
      </View>
    </View>
  );
}

// ─── styles ────────────────────────────────────────────────────────────────

function makeStyles(colors: ThemeColors, type: ThemeType) {
  return StyleSheet.create({
    summary: {
      marginHorizontal: space.md, marginTop: space.md,
      padding: space.lg,
      backgroundColor: colors.bgCard, borderRadius: radii.md,
      borderWidth: 1, borderColor: colors.border,
    },
    histWrap: {
      marginHorizontal: space.md, marginTop: space.md,
      padding: space.lg,
      backgroundColor: colors.bgCard, borderRadius: radii.md,
      borderWidth: 1, borderColor: colors.border,
    },
    histArea: {
      flexDirection: "row",
      height: 100,
      alignItems: "flex-end",
      position: "relative",
      paddingHorizontal: 2,
    },
    benchmarkLine: {
      position: "absolute",
      top: 0, bottom: 0,
      width: 2,
    },
    histScale: {
      flexDirection: "row",
      justifyContent: "space-between",
      marginTop: 6,
    },
    scaleNum: {
      ...type.bodySm,
      fontSize: 10, color: colors.textTer, fontVariant: ["tabular-nums"],
    },
    histLegend: {
      flexDirection: "row", justifyContent: "center",
      gap: space.lg, marginTop: space.sm,
    },
    legendItem: { flexDirection: "row", alignItems: "center", gap: 6 },
    legendDot: { width: 8, height: 8, borderRadius: 4 },
    legendText: { fontSize: 11, color: colors.textSec, fontWeight: "600" },
  });
}
