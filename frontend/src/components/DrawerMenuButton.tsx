/**
 * Hamburger that opens the drawer.
 *
 * Every tab screen draws its own header with `headerShown: false`, so there
 * is no navigator header for the drawer to hang its button on. This is that
 * button, dropped into a screen's own header row instead. Edge-swipe still
 * works — this exists because a swipe is undiscoverable on its own.
 */

import { Pressable, StyleSheet } from "react-native";
import { useNavigation } from "expo-router";
import { Menu } from "lucide-react-native";
import { useTheme } from "@/src/ThemeProvider";
import { radii } from "@/src/theme";

export function DrawerMenuButton({ size = 22 }: { size?: number }) {
  const { colors } = useTheme();
  // Scoped to the drawer navigator explicitly: an unscoped useNavigation()
  // inside a tab screen resolves to the TAB navigator, which has no
  // openDrawer(). Cast because expo-router types useNavigation's return
  // generically rather than per-navigator.
  const navigation = useNavigation("/(drawer)") as any;

  return (
    <Pressable
      onPress={() => navigation.openDrawer()}
      hitSlop={10}
      accessibilityRole="button"
      accessibilityLabel="Open menu"
      style={({ pressed }) => [s.btn, { opacity: pressed ? 0.6 : 1 }]}
    >
      <Menu size={size} color={colors.textSec} />
    </Pressable>
  );
}

const s = StyleSheet.create({
  btn: {
    paddingVertical: 4,
    paddingRight: 8,
    borderRadius: radii.sm,
  },
});
