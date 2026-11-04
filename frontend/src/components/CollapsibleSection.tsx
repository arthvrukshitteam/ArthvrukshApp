// Section card with a tappable header. Tap to expand/collapse the body.
// Used to bucket dense analyst metrics into digestible groups.

import { useMemo, useState } from "react";
import { LayoutAnimation, Platform, Pressable, StyleSheet, Text, UIManager, View } from "react-native";
import { ChevronDown } from "lucide-react-native";
import { radii, space, type ThemeColors, type ThemeType } from "@/src/theme";
import { useTheme } from "@/src/ThemeProvider";

if (Platform.OS === "android" && UIManager.setLayoutAnimationEnabledExperimental) {
  UIManager.setLayoutAnimationEnabledExperimental(true);
}

interface Props {
  title: string;
  subtitle?: string;
  initiallyOpen?: boolean;
  badge?: React.ReactNode;   // optional chip (e.g. metric count) shown on the right
  dense?: boolean;            // body without inner padding (for tables)
  children: React.ReactNode;
}

export function CollapsibleSection({
  title, subtitle, initiallyOpen = false, badge, dense, children,
}: Props) {
  const { colors, type } = useTheme();
  const styles = useMemo(() => makeStyles(colors, type), [colors, type]);
  const [open, setOpen] = useState(initiallyOpen);

  const toggle = () => {
    LayoutAnimation.configureNext(LayoutAnimation.Presets.easeInEaseOut);
    setOpen((v) => !v);
  };

  return (
    <View style={styles.section}>
      <Pressable
        onPress={toggle}
        style={({ pressed }) => [styles.header, pressed && { opacity: 0.85 }]}
      >
        <View style={{ flex: 1 }}>
          <Text style={[type.label, { color: colors.textTer }]}>{title}</Text>
          {subtitle && <Text style={[type.bodySm, { color: colors.textSec, marginTop: 2 }]}>{subtitle}</Text>}
        </View>
        {badge}
        <ChevronDown
          size={18}
          color={colors.textSec}
          style={{ marginLeft: 8, transform: [{ rotate: open ? "180deg" : "0deg" }] }}
        />
      </Pressable>
      {open && (
        <View style={[styles.body, dense && { padding: 0, overflow: "hidden" }]}>
          {children}
        </View>
      )}
    </View>
  );
}

function makeStyles(colors: ThemeColors, _type: ThemeType) {
  return StyleSheet.create({
    section: {
      marginHorizontal: space.md, marginTop: space.md,
      backgroundColor: colors.bgCard, borderRadius: radii.md,
      borderWidth: 1, borderColor: colors.border, overflow: "hidden",
    },
    header: {
      flexDirection: "row", alignItems: "center",
      paddingHorizontal: space.lg, paddingVertical: 14,
    },
    body: {
      paddingHorizontal: space.lg, paddingTop: 0, paddingBottom: space.lg,
      borderTopWidth: StyleSheet.hairlineWidth, borderColor: colors.border,
    },
  });
}
