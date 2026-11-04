// Visual FQS score gauge: a horizontal bar with the five tier bands shaded
// behind it (Weak, Average, Good, Strong, Elite) and a triangle marker
// indicating where the current score sits.

import { useMemo } from "react";
import { StyleSheet, Text, View } from "react-native";
import { radii, space, type ThemeColors, type ThemeType } from "@/src/theme";
import { useTheme } from "@/src/ThemeProvider";

interface Props {
  score: number;          // 0 .. 100
  tier: string | null;    // "Elite" | "Strong" | "Good" | "Average" | "Weak"
  horizon: number;        // shown as a small label, e.g. "10Y"
}

export function FqsGauge({ score, tier, horizon }: Props) {
  const { colors, type, scoreColor } = useTheme();
  const styles = useMemo(() => makeStyles(colors, type), [colors, type]);

  const fqsCol = scoreColor(score);
  const clamped = Math.max(0, Math.min(100, score));

  // Five segments — Weak (0-60), Average (60-70), Good (70-80), Strong (80-90), Elite (90-100)
  const segments = [
    { label: "Weak", width: 60, color: colors.tierWeak },
    { label: "Average", width: 10, color: colors.tierAvg },
    { label: "Good", width: 10, color: colors.tierGood },
    { label: "Strong", width: 10, color: colors.tierStrong },
    { label: "Elite", width: 10, color: colors.tierElite },
  ];

  return (
    <View style={styles.wrap}>
      <View style={[styles.glow, { backgroundColor: `${fqsCol}22` }]} />
      <View style={styles.header}>
        <Text style={[type.label, { color: fqsCol }]}>
          FUND QUALITY SCORE · {horizon}Y
        </Text>
      </View>
      <Text style={[styles.scoreNum, { color: fqsCol }]}>{clamped.toFixed(1)}</Text>
      <Text style={[type.body, { color: fqsCol, fontWeight: "800", letterSpacing: 1.6 }]}>
        {(tier ?? "").toUpperCase()}
      </Text>

      {/* Gauge bar */}
      <View style={styles.barTrack}>
        {segments.map((s, i) => (
          <View
            key={s.label}
            style={{
              flex: s.width,
              backgroundColor: `${s.color}44`,
              borderLeftWidth: i === 0 ? 0 : StyleSheet.hairlineWidth,
              borderColor: colors.border,
            }}
          />
        ))}
        {/* Marker */}
        <View
          style={[
            styles.marker,
            { left: `${clamped}%`, backgroundColor: fqsCol, borderColor: colors.bg },
          ]}
        />
      </View>
      <View style={styles.scaleRow}>
        <Text style={styles.scaleNum}>0</Text>
        <Text style={[styles.scaleNum, { textAlign: "right", flex: 1 }]}>100</Text>
      </View>

      {/* Tier legend chip */}
      <View style={styles.legend}>
        {segments.map((s) => (
          <View key={s.label} style={styles.legendItem}>
            <View style={[styles.legendDot, { backgroundColor: s.color }]} />
            <Text style={[type.bodySm, { color: colors.textSec, fontSize: 10 }]}>{s.label}</Text>
          </View>
        ))}
      </View>
    </View>
  );
}

function makeStyles(colors: ThemeColors, _type: ThemeType) {
  return StyleSheet.create({
    wrap: {
      marginHorizontal: space.md, marginTop: space.md,
      paddingHorizontal: space.lg, paddingTop: space.lg, paddingBottom: space.md,
      backgroundColor: colors.bgCard, borderRadius: radii.md,
      borderWidth: 1, borderColor: colors.border,
      alignItems: "center", overflow: "hidden",
    },
    glow: {
      position: "absolute", top: -40, left: -40, right: -40, height: 140,
      borderRadius: 100, opacity: 0.6,
    },
    header: { marginBottom: 4 },
    scoreNum: {
      fontSize: 64, fontWeight: "800", letterSpacing: -3,
      fontVariant: ["tabular-nums"], marginTop: 2,
    },
    barTrack: {
      position: "relative",
      width: "100%", height: 14,
      marginTop: space.md, borderRadius: 7,
      flexDirection: "row", overflow: "hidden",
      borderWidth: 1, borderColor: colors.border,
    },
    marker: {
      position: "absolute",
      width: 14, height: 14, borderRadius: 7,
      borderWidth: 2,
      transform: [{ translateX: -7 }],
    },
    scaleRow: {
      flexDirection: "row", width: "100%",
      marginTop: 4,
    },
    scaleNum: {
      fontSize: 10, color: colors.textTer, fontVariant: ["tabular-nums"],
    },
    legend: {
      flexDirection: "row", flexWrap: "wrap", justifyContent: "space-between",
      width: "100%", marginTop: space.sm, gap: 6,
    },
    legendItem: { flexDirection: "row", alignItems: "center", gap: 4 },
    legendDot: { width: 8, height: 8, borderRadius: 4 },
  });
}
