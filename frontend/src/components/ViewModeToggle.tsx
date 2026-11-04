// Pill toggle: Simple | Analyst.

import { Pressable, StyleSheet, Text, View } from "react-native";
import { Sparkles, BarChart3 } from "lucide-react-native";
import { radii, space } from "@/src/theme";
import { useTheme } from "@/src/ThemeProvider";
import { useViewMode } from "@/src/ViewModeProvider";

export function ViewModeToggle() {
  const { colors, type } = useTheme();
  const { mode, setMode } = useViewMode();

  return (
    <View style={[styles.wrap, { backgroundColor: colors.bgElev, borderColor: colors.border }]}>
      <Pressable
        onPress={() => setMode("simple")}
        style={({ pressed }) => [
          styles.pill,
          mode === "simple" && { backgroundColor: colors.bg, borderColor: colors.azure },
          mode !== "simple" && { borderColor: "transparent" },
          pressed && { opacity: 0.9 },
        ]}
      >
        <Sparkles
          size={13}
          color={mode === "simple" ? colors.azure : colors.textSec}
        />
        <Text
          style={[
            type.bodySm,
            { fontWeight: "700", marginLeft: 6 },
            { color: mode === "simple" ? colors.textPri : colors.textSec },
          ]}
        >
          Simple
        </Text>
      </Pressable>
      <Pressable
        onPress={() => setMode("analyst")}
        style={({ pressed }) => [
          styles.pill,
          mode === "analyst" && { backgroundColor: colors.bg, borderColor: colors.azure },
          mode !== "analyst" && { borderColor: "transparent" },
          pressed && { opacity: 0.9 },
        ]}
      >
        <BarChart3
          size={13}
          color={mode === "analyst" ? colors.azure : colors.textSec}
        />
        <Text
          style={[
            type.bodySm,
            { fontWeight: "700", marginLeft: 6 },
            { color: mode === "analyst" ? colors.textPri : colors.textSec },
          ]}
        >
          Analyst
        </Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: {
    flexDirection: "row", alignSelf: "center",
    marginHorizontal: space.md, marginTop: space.md,
    padding: 4, borderRadius: radii.pill, borderWidth: 1,
    gap: 2,
  },
  pill: {
    flexDirection: "row", alignItems: "center",
    paddingHorizontal: 14, paddingVertical: 7,
    borderRadius: radii.pill,
    borderWidth: 1,
  },
});
