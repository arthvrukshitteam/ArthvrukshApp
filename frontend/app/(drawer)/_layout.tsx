/**
 * Drawer layout — wraps the whole tab stack.
 *
 * Why this exists: the bottom bar only has room for five destinations before
 * the labels start truncating, and Methodology is reference material — read
 * once, revisited occasionally — not a place you tab between while working.
 * It moved in here so the fifth bottom slot could go to Portfolio Overlap,
 * which IS day-to-day work.
 *
 * The drawer sits ABOVE the tabs in the tree, so the bottom bar stays visible
 * and keeps its own state while the drawer is open. Screens listed here that
 * are not `(tabs)` become drawer-only destinations.
 */

import { View, Text, StyleSheet, Pressable } from "react-native";
// Expo SDK 56+ forbids importing @react-navigation/* directly — expo-router
// vendors its own copies and Metro hard-errors on the direct import.
// expo-router/drawer re-exports everything this file needs.
import { Drawer, DrawerContentScrollView } from "expo-router/drawer";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { BookOpen, ClipboardCheck, LayoutGrid, RefreshCw, SlidersHorizontal } from "lucide-react-native";
import { useRouter } from "expo-router";
import { useTheme } from "@/src/ThemeProvider";
import { BrandLogo } from "@/src/components/BrandLogo";
import { space, radii } from "@/src/theme";

/** Destinations that live only in the drawer, in the order they're shown. */
const DRAWER_ITEMS = [
  { route: "/holding-review", label: "Holding Review", Icon: ClipboardCheck, hint: "Previous proposal vs current FQS report" },
  { route: "/insights",     label: "Methodology",  Icon: BookOpen,           hint: "How AV-FQS scores a fund" },
  { route: "/data-refresh", label: "Data Refresh", Icon: RefreshCw,          hint: "NAV and score freshness" },
  { route: "/mode-picker",  label: "Analysis Mode", Icon: SlidersHorizontal, hint: "SIP or Lumpsum" },
] as const;

/**
 * Minimal structural props for the custom drawer content — the same reason
 * the tab bar in (tabs)/_layout.tsx types its own props by hand: expo-router
 * 57 vendors its own copy of the react-navigation types, and that copy's
 * nominal PrivateValueStore is not assignable to the one @react-navigation/
 * drawer exports. Typing only the shape this component actually consumes
 * sidesteps both copies instead of casting between them.
 */
type DrawerContentProps = {
  state: { index: number; routes: { key: string; name: string }[] };
  navigation: { closeDrawer: () => void };
};

function DrawerContent(props: DrawerContentProps) {
  const { colors } = useTheme();
  const insets = useSafeAreaInsets();
  const router = useRouter();

  // The focused route name, so the active drawer row can be highlighted the
  // same way the active tab is.
  const activeRoute = props.state.routes[props.state.index]?.name;

  return (
    <DrawerContentScrollView
      {...(props as any)}
      contentContainerStyle={{ paddingTop: insets.top + space.md }}
      style={{ backgroundColor: colors.bgCard }}
    >
      <View style={[s.header, { borderBottomColor: colors.border }]}>
        <BrandLogo size={34} />
        <View style={{ flex: 1 }}>
          <Text style={[s.brand, { color: colors.textPri }]}>ArthVruksh</Text>
          <Text style={[s.brandSub, { color: colors.textTer }]}>Fund Analyst</Text>
        </View>
      </View>

      <Pressable
        onPress={() => {
          props.navigation.closeDrawer();
          router.push("/home" as any);
        }}
        style={({ pressed }) => [
          s.item,
          {
            backgroundColor: activeRoute === "(tabs)" ? colors.azureSoft : "transparent",
            opacity: pressed ? 0.7 : 1,
          },
        ]}
      >
        <LayoutGrid
          size={20}
          color={activeRoute === "(tabs)" ? colors.azure : colors.textSec}
        />
        <View style={{ flex: 1 }}>
          <Text
            style={[
              s.itemLabel,
              { color: activeRoute === "(tabs)" ? colors.azure : colors.textPri },
            ]}
          >
            Dashboard
          </Text>
          <Text style={[s.itemHint, { color: colors.textTer }]}>
            Home, Funds, Rolling, Review, Overlap
          </Text>
        </View>
      </Pressable>

      <View style={[s.divider, { backgroundColor: colors.border }]} />

      {DRAWER_ITEMS.map(({ route, label, Icon, hint }) => {
        const isActive = activeRoute === route.replace("/", "");
        return (
          <Pressable
            key={route}
            onPress={() => {
              props.navigation.closeDrawer();
              router.push(route as any);
            }}
            style={({ pressed }) => [
              s.item,
              {
                backgroundColor: isActive ? colors.azureSoft : "transparent",
                opacity: pressed ? 0.7 : 1,
              },
            ]}
          >
            <Icon size={20} color={isActive ? colors.azure : colors.textSec} />
            <View style={{ flex: 1 }}>
              <Text
                style={[s.itemLabel, { color: isActive ? colors.azure : colors.textPri }]}
              >
                {label}
              </Text>
              <Text style={[s.itemHint, { color: colors.textTer }]}>{hint}</Text>
            </View>
          </Pressable>
        );
      })}
    </DrawerContentScrollView>
  );
}

export default function DrawerLayout() {
  const { colors } = useTheme();

  return (
    <Drawer
      drawerContent={(props) => <DrawerContent {...props} />}
      screenOptions={{
        headerStyle: { backgroundColor: colors.bg },
        headerTintColor: colors.textPri,
        headerShadowVisible: false,
        headerTitleStyle: { fontWeight: "700", fontSize: 16 },
        drawerStyle: { backgroundColor: colors.bgCard, width: 290 },
        drawerType: "front",
        swipeEdgeWidth: 40,
      }}
    >
      {/* The tab stack. Its own header is hidden — each tab screen draws its
          own, and the drawer button is injected by the tabs layout. */}
      <Drawer.Screen name="(tabs)" options={{ headerShown: false, title: "Dashboard" }} />
      <Drawer.Screen name="holding-review" options={{ title: "Holding Review", headerShown: false }} />
      <Drawer.Screen name="insights" options={{ title: "Methodology" }} />
    </Drawer>
  );
}

const s = StyleSheet.create({
  header: {
    flexDirection: "row",
    alignItems: "center",
    gap: space.sm,
    paddingHorizontal: space.md,
    paddingBottom: space.md,
    marginBottom: space.sm,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  brand: { fontSize: 17, fontWeight: "700", letterSpacing: -0.3 },
  brandSub: { fontSize: 11, marginTop: 1 },
  item: {
    flexDirection: "row",
    alignItems: "center",
    gap: space.sm + 4,
    paddingVertical: 11,
    paddingHorizontal: space.md,
    marginHorizontal: space.sm,
    borderRadius: radii.md,
  },
  itemLabel: { fontSize: 15, fontWeight: "600" },
  itemHint: { fontSize: 11, marginTop: 2 },
  divider: {
    height: StyleSheet.hairlineWidth,
    marginVertical: space.sm,
    marginHorizontal: space.md,
  },
});
