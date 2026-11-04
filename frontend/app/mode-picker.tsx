import { useState } from "react";
import {
  View,
  Text,
  StyleSheet,
  Pressable,
  ActivityIndicator,
  Dimensions,
  ScrollView,
  Image,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { LinearGradient } from "expo-linear-gradient";
import { useRouter } from "expo-router";
import {
  TrendingUp,
  BarChart2,
  ArrowRight,
  Check,
  TreePine,
} from "lucide-react-native";
import { useTheme } from "@/src/ThemeProvider";
import { useMode, type InvestmentMode } from "@/src/ModeProvider";
import { radii, space } from "@/src/theme";

const { width: SW } = Dimensions.get("window");
const CARD_W = (SW - space.md * 2 - space.sm) / 2;

// ── Highlight row inside a mode card ─────────────────────────────────────────

function Highlight({ text, selected }: { text: string; selected: boolean }) {
  return (
    <View style={styles.hlRow}>
      <View
        style={[
          styles.hlDot,
          { backgroundColor: selected ? "rgba(255,255,255,0.55)" : "#2563EB" },
        ]}
      />
      <Text
        style={[
          styles.hlText,
          { color: selected ? "rgba(255,255,255,0.8)" : "#64748B" },
        ]}
      >
        {text}
      </Text>
    </View>
  );
}

// ── Mode card ─────────────────────────────────────────────────────────────────

function ModeCard({
  title,
  tag,
  highlights,
  icon,
  selected,
  onPress,
  colors,
}: {
  title: string;
  tag: string;
  highlights: string[];
  icon: React.ReactNode;
  selected: boolean;
  onPress: () => void;
  colors: any;
}) {
  return (
    <Pressable
      onPress={onPress}
      style={({ pressed }) => [
        styles.card,
        {
          width: CARD_W,
          backgroundColor: selected ? colors.azure : colors.bgCard,
          borderColor: selected ? colors.azure : colors.borderStrong,
          shadowColor: selected ? colors.azure : "#000",
          shadowOpacity: selected ? 0.3 : 0.07,
          shadowRadius: selected ? 18 : 6,
          shadowOffset: { width: 0, height: selected ? 8 : 2 },
          elevation: selected ? 10 : 2,
          transform: [{ scale: pressed ? 0.97 : 1 }],
        },
      ]}
    >
      {/* Check circle — top-right */}
      <View
        style={[
          styles.checkCircle,
          {
            backgroundColor: selected
              ? "rgba(255,255,255,0.22)"
              : colors.bgElev,
            borderColor: selected
              ? "rgba(255,255,255,0.35)"
              : colors.borderStrong,
          },
        ]}
      >
        <Check
          size={11}
          color={selected ? "#fff" : colors.textDis}
          strokeWidth={3}
        />
      </View>

      {/* Icon bubble */}
      <View
        style={[
          styles.iconBubble,
          {
            backgroundColor: selected
              ? "rgba(255,255,255,0.15)"
              : colors.azureSoft,
          },
        ]}
      >
        {icon}
      </View>

      {/* Mode name */}
      <Text
        style={[
          styles.cardTitle,
          { color: selected ? "#fff" : colors.textPri },
        ]}
      >
        {title}
      </Text>

      {/* Tag */}
      <Text
        style={[
          styles.cardTag,
          { color: selected ? "rgba(255,255,255,0.65)" : colors.textTer },
        ]}
      >
        {tag}
      </Text>

      {/* Divider */}
      <View
        style={[
          styles.cardDivider,
          {
            backgroundColor: selected
              ? "rgba(255,255,255,0.15)"
              : colors.border,
          },
        ]}
      />

      {/* Feature highlights */}
      <View style={styles.hlList}>
        {highlights.map((h) => (
          <Highlight key={h} text={h} selected={selected} />
        ))}
      </View>
    </Pressable>
  );
}

// ── Screen ────────────────────────────────────────────────────────────────────

export default function ModePickerScreen() {
  const router = useRouter();
  const { colors } = useTheme();
  const { mode: currentMode, setMode } = useMode();
  const [selected, setSelected] = useState<InvestmentMode | null>(currentMode);
  const [saving, setSaving] = useState(false);

  async function confirm() {
    if (!selected) return;
    setSaving(true);
    await setMode(selected);
    setSaving(false);
    router.replace("/tenure-picker" as any);
  }

  return (
    <View style={{ flex: 1, backgroundColor: colors.bg }}>
      <ScrollView
        contentContainerStyle={{ flexGrow: 1 }}
        keyboardShouldPersistTaps="handled"
        showsVerticalScrollIndicator={false}
      >
        <SafeAreaView style={{ flex: 1 }} edges={["top"]}>
          {/* ── Hero — always dark gradient for brand impact ─── */}
          <LinearGradient
            colors={["#061220", "#0B1D38", "#101F40"]}
            start={{ x: 0.2, y: 0 }}
            end={{ x: 0.8, y: 1 }}
            style={styles.hero}
          >
            {/* Brand mark */}
            <View style={styles.brandMark}>
              <View style={styles.logoRing}>
                <Image
                  source={require("../assets/images/icon-transparent.png")}
                  style={styles.logo}
                />
                {/* <TreePine size={26} color="#fff" /> */}
              </View>
            </View>

            {/* Brand name */}
            <Text style={styles.heroName}>ArthVruksh</Text>
            <Text style={styles.heroLabel}>Your Goals, Our Guidance</Text>

            {/* Tagline */}
            <Text style={styles.heroTagline}>
              Powered by daily AMFI NAVs.{"\n"}No estimates. No guesswork.
            </Text>

            {/* Subtle decorative pills */}
            <View style={styles.heroPills}>
              {["Rolling Returns", "XIRR", "Wealth Multiple", "Cycle-Tested"].map(
                (t) => (
                  <View key={t} style={styles.heroPill}>
                    <Text style={styles.heroPillText}>{t}</Text>
                  </View>
                )
              )}
            </View>
          </LinearGradient>
        </SafeAreaView>

        {/* ── Selection section ───────────────────────────── */}
        <View style={[styles.selectionSection, { backgroundColor: colors.bg }]}>
          <Text style={[styles.selectionQ, { color: colors.textPri }]}>
            How do you invest?
          </Text>
          <Text style={[styles.selectionHint, { color: colors.textTer }]}>
            Pick the model that matches your strategy.
          </Text>

          <View style={styles.cardsRow}>
            <ModeCard
              title="SIP"
              tag="Monthly Plan"
              highlights={["1–50 year horizon", "Rolling windows", "XIRR precision", "Cycle robustness"]}
              icon={
                <TrendingUp
                  size={22}
                  color={selected === "sip" ? "#fff" : colors.azure}
                />
              }
              selected={selected === "sip"}
              onPress={() => setSelected("sip")}
              colors={colors}
            />
            <ModeCard
              title="Lumpsum"
              tag="One-time"
              highlights={["1–50 year horizon", "Rolling windows", "XIRR precision", "Wealth Multiple", "Cycle robustness"]}
              icon={
                <BarChart2
                  size={22}
                  color={selected === "lumpsum" ? "#fff" : colors.azure}
                />
              }
              selected={selected === "lumpsum"}
              onPress={() => setSelected("lumpsum")}
              colors={colors}
            />
          </View>

          {/* CTA */}
          <View style={styles.ctaWrap}>
            <Pressable
              onPress={confirm}
              disabled={!selected || saving}
              style={({ pressed }) => [
                styles.cta,
                {
                  backgroundColor: selected ? colors.azure : colors.bgElev,
                  opacity: pressed ? 0.87 : 1,
                  shadowColor: selected ? colors.azure : "transparent",
                  shadowOpacity: 0.35,
                  shadowRadius: 14,
                  shadowOffset: { width: 0, height: 6 },
                  elevation: selected ? 8 : 0,
                },
              ]}
            >
              {saving ? (
                <ActivityIndicator color="#fff" />
              ) : (
                <>
                  <Text
                    style={[
                      styles.ctaText,
                      { color: selected ? "#fff" : colors.textTer },
                    ]}
                  >
                    {currentMode ? "Switch Mode" : "Continue"}
                  </Text>
                  <ArrowRight
                    size={18}
                    color={selected ? "#fff" : colors.textTer}
                  />
                </>
              )}
            </Pressable>
          </View>
        </View>
      </ScrollView>

      {/* Bottom safe area fill */}
      <SafeAreaView edges={["bottom"]} style={{ backgroundColor: colors.bg }} />
    </View>
  );
}

// ── Styles ────────────────────────────────────────────────────────────────────

const styles = StyleSheet.create({
  // Hero
  hero: {
    paddingHorizontal: space.lg,
    paddingTop: space.md,
    paddingBottom: 36,
    alignItems: "center",
    gap: 8,
  },
  brandMark: {
    marginBottom: 6,
   
    
  },
  logo: {
  width: 108,
  height: 108,
  resizeMode: "contain",
  backgroundColor: "#FFFFFF",
  // borderColor: "red",
  borderWidth: 1,
  borderRadius:100,
  

},

  heroName: {
    fontSize: 30,
    fontWeight: "800",
    color: "#fff",
    letterSpacing: -0.8,
  },
  heroLabel: {
    fontSize: 10,
    fontWeight: "700",
    color: "rgba(255,255,255,0.38)",
    letterSpacing: 3.5,
    textTransform: "uppercase",
  },
  heroTagline: {
    fontSize: 13,
    color: "rgba(255,255,255,0.48)",
    textAlign: "center",
    lineHeight: 20,
    marginTop: 4,
  },
  heroPills: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 6,
    justifyContent: "center",
    marginTop: 12,
  },
  heroPill: {
    borderRadius: radii.pill,
    borderWidth: 1,
    borderColor: "rgba(255,255,255,0.1)",
    paddingHorizontal: 10,
    paddingVertical: 4,
  },
  heroPillText: {
    fontSize: 11,
    fontWeight: "500",
    color: "rgba(255,255,255,0.4)",
  },

  // Selection
  selectionSection: {
    flex: 1,
    paddingHorizontal: space.md,
    paddingTop: space.lg,
  },
  selectionQ: {
    fontSize: 22,
    fontWeight: "800",
    letterSpacing: -0.5,
  },
  selectionHint: {
    fontSize: 13,
    fontWeight: "400",
    marginTop: 3,
    marginBottom: space.md,
  },
  cardsRow: {
    flexDirection: "row",
    gap: space.sm,
  },
  logoRing: {
    width: 90,
    height: 90,
    borderRadius: 45,
    backgroundColor: "rgba(255,255,255,0.08)",
    justifyContent: "center",
    alignItems: "center",
  },

  card: {
    borderRadius: radii.lg + 2,
    borderWidth: 1.5,
    padding: space.md,
    gap: 6,
    position: "relative",
    minHeight: 220,
  },
  checkCircle: {
    position: "absolute",
    top: 12,
    right: 12,
    width: 22,
    height: 22,
    borderRadius: 11,
    borderWidth: 1.5,
    alignItems: "center",
    justifyContent: "center",
  },
  iconBubble: {
    width: 46,
    height: 46,
    borderRadius: radii.md,
    alignItems: "center",
    justifyContent: "center",
    marginBottom: 2,
  },
  cardTitle: {
    fontSize: 22,
    fontWeight: "800",
    letterSpacing: -0.5,
  },
  cardTag: {
    fontSize: 12,
    fontWeight: "500",
    marginTop: -3,
  },
  cardDivider: {
    height: 1,
    marginVertical: 4,
  },
  hlList: {
    gap: 5,
  },
  hlRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 7,
  },
  hlDot: {
    width: 4,
    height: 4,
    borderRadius: 2,
  },
  hlText: {
    fontSize: 12,
    fontWeight: "500",
    flexShrink: 1,
  },

  // CTA
  ctaWrap: {
    marginTop: space.lg,
    paddingBottom: space.lg,
  },
  cta: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: space.sm,
    borderRadius: radii.md,
    paddingVertical: 17,
  },
  ctaText: {
    fontSize: 17,
    fontWeight: "700",
    letterSpacing: 0.1,
  },
});
