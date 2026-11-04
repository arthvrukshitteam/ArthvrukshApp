import { useMemo } from "react";
import { View, Text, StyleSheet } from "react-native";
import { radii, space, type ThemeColors, type ThemeType } from "@/src/theme";
import { useTheme } from "@/src/ThemeProvider";

interface Props {
  label: string;
  value: string;
  hint?: string;
  // 0-100 for the micro bar; pass null to hide
  pct?: number | null;
  pctColor?: string;
  testID?: string;
}

export function KPICard({ label, value, hint, pct, pctColor, testID }: Props) {
  const { colors, type } = useTheme();
  const styles = useMemo(() => makeStyles(colors, type), [colors, type]);
  const w = pct == null ? 0 : Math.max(0, Math.min(100, pct));
  return (
    <View style={styles.card} testID={testID}>
      <Text style={[type.label, { color: colors.textSec, fontSize: 10 }]} numberOfLines={1}>
        {label}
      </Text>
      <Text style={styles.value} numberOfLines={1}>
        {value}
      </Text>
      {hint != null && (
        <Text style={styles.hint} numberOfLines={1}>
          {hint}
        </Text>
      )}
      {pct != null && (
        <View style={styles.barTrack}>
          <View
            style={[
              styles.barFill,
              { width: `${w}%`, backgroundColor: pctColor ?? colors.azure },
            ]}
          />
        </View>
      )}
    </View>
  );
}

function makeStyles(colors: ThemeColors, type: ThemeType) {
  return StyleSheet.create({
    card: {
      flex: 1,
      minWidth: 110,
      backgroundColor: colors.bgCard,
      borderRadius: radii.sm,
      padding: space.md,
      borderWidth: 1,
      borderColor: colors.border,
      gap: 4,
    },
    value: {
      fontSize: 20,
      fontWeight: "700",
      color: colors.textPri,
      fontVariant: ["tabular-nums"],
      marginTop: 2,
    },
    hint: { ...type.bodySm, color: colors.textTer, fontSize: 11 },
    barTrack: {
      height: 4,
      backgroundColor: colors.bgElev,
      borderRadius: radii.pill,
      marginTop: space.sm,
      overflow: "hidden",
    },
    barFill: { height: "100%", borderRadius: radii.pill },
  });
}
