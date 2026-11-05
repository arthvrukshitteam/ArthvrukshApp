/**
 * Tab layout — custom tab bar with azure active tint + top-line indicator.
 *
 * Active tab:  azure icon + azure label + azure top indicator line
 * Inactive:    textTer icon + textTer label
 *
 * The active indicator is a short pill (32px wide, 3px tall) sitting at the
 * very top edge of the tab bar — a common premium pattern seen in top-tier apps.
 *
 * Four slots. Methodology and Fund Search live in the drawer (see
 * ../_layout.tsx): Methodology is reference material, and Fund Search is a
 * lookup tool reached from the menu rather than a place the advisor tabs
 * between while working. Five is the ceiling before labels truncate.
 */

import { View, Text, Pressable, StyleSheet, Platform } from "react-native";
import { Tabs, useSegments } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { HomeIcon, Repeat, ListChecks, Layers } from "lucide-react-native";
import { useTheme } from "@/src/ThemeProvider";
import { space, radii } from "@/src/theme";

// ── Tab definitions (icon + label) ───────────────────────────────────────────

type TabDef = {
  name: string;
  label: string;
  Icon: typeof HomeIcon;
};

const TABS: TabDef[] = [
  { name: "home",             label: "Home",        Icon: HomeIcon    },
  { name: "rolling",          label: "Rolling",     Icon: Repeat      },
  { name: "portfolio-review", label: "Review",      Icon: ListChecks  },
  { name: "overlap",          label: "Overlap",     Icon: Layers      },
];

// ── Custom tab bar ────────────────────────────────────────────────────────────

// Minimal structural props for the custom bar. expo-router 57 vendors its own
// copy of the react-navigation types (expo-router/build/react-navigation/…)
// whose nominal PrivateValueStore makes them incompatible with the app's
// @react-navigation/bottom-tabs BottomTabBarProps — so we type only the exact
// shape this component consumes instead of importing either copy.
type TabBarProps = {
  state: { index: number; routes: Array<{ key: string; name: string }> };
  navigation: {
    // Deliberately loose: the concrete emit/navigate generics differ between
    // expo-router's vendored types and @react-navigation's, and both are
    // callable with what this component passes.
    emit: (e: any) => any;
    navigate: (name: any) => void;
  };
};

function CustomTabBar({ state, navigation }: TabBarProps) {
  const { colors } = useTheme();
  const insets     = useSafeAreaInsets();

  return (
    <View
      style={[
        tabS.bar,
        {
          backgroundColor:  colors.bgCard,
          borderTopColor:   colors.border,
          paddingBottom:    insets.bottom > 0 ? insets.bottom : 12,
        },
      ]}
    >
      {state.routes.map((route, idx) => {
        const def       = TABS.find((t) => t.name === route.name);
        if (!def) return null;
        const isFocused = state.index === idx;
        const color     = isFocused ? colors.azure : colors.textTer;
        const { Icon }  = def;

        return (
          <Pressable
            key={route.key}
            onPress={() => {
              const event = navigation.emit({
                type: "tabPress",
                target: route.key,
                canPreventDefault: true,
              });
              if (!isFocused && !event.defaultPrevented) {
                navigation.navigate(route.name);
              }
            }}
            style={({ pressed }) => [
              tabS.item,
              { opacity: pressed ? 0.7 : 1 },
            ]}
            accessibilityRole="button"
            accessibilityState={{ selected: isFocused }}
          >
            {/* Top indicator pill */}
            <View
              style={[
                tabS.indicator,
                {
                  backgroundColor: isFocused ? colors.azure : "transparent",
                },
              ]}
            />

            {/* Icon */}
            <Icon
              size={22}
              color={color}
              strokeWidth={isFocused ? 2.2 : 1.8}
            />

            {/* Label */}
            <Text
              style={[
                tabS.label,
                {
                  color,
                  fontWeight: isFocused ? "700" : "500",
                },
              ]}
            >
              {def.label}
            </Text>
          </Pressable>
        );
      })}
    </View>
  );
}

const tabS = StyleSheet.create({
  bar: {
    flexDirection:  "row",
    borderTopWidth: StyleSheet.hairlineWidth,
    paddingTop:     8,
  },
  item: {
    flex:           1,
    alignItems:     "center",
    gap:            3,
    paddingTop:     6,
  },
  indicator: {
    position:     "absolute",
    top:          0,
    width:        32,
    height:       3,
    borderRadius: 2,
  },
  label: {
    fontSize:      10,
    letterSpacing: 0.2,
  },
});

// ── Layout ────────────────────────────────────────────────────────────────────

export default function TabsLayout() {
  const { colors } = useTheme();

  return (
    <Tabs
      tabBar={(props) => <CustomTabBar {...props} />}
      screenOptions={{
        headerStyle:        { backgroundColor: colors.bg },
        headerTintColor:    colors.textPri,
        headerShadowVisible: false,
        headerTitleStyle:   { fontWeight: "700", fontSize: 16 },
      }}
    >
      <Tabs.Screen
        name="home"
        options={{
          title:         "Home",
          headerShown:   false,
          tabBarIcon:    ({ color, size }) => <HomeIcon color={color} size={size} />,
        }}
      />
      <Tabs.Screen
        name="rolling"
        options={{
          title:         "Rolling",
          headerShown:   false,
          tabBarIcon:    ({ color, size }) => <Repeat color={color} size={size} />,
        }}
      />
      <Tabs.Screen
        name="portfolio-review"
        options={{
          title:         "Portfolio Review",
          headerShown:   false,
          tabBarIcon:    ({ color, size }) => <ListChecks color={color} size={size} />,
        }}
      />
      <Tabs.Screen
        name="overlap"
        options={{
          title:         "Portfolio Overlap",
          headerShown:   false,
          tabBarIcon:    ({ color, size }) => <Layers color={color} size={size} />,
        }}
      />
    </Tabs>
  );
}
