/**
 * Tenure Picker — premium redesign.
 *
 * The investment horizon number is the STAR of this screen.
 * A giant centered display (TextInput styled at 72px) dominates the view.
 * Chips below set the value instantly. Everything else steps back.
 *
 * During onboarding  (tenure === null): no cancel, full focus on input.
 * Mid-session         (tenure is set):  cancel top-left, Change Mode at bottom.
 */

import { useRef, useState } from "react";
import {
  View,
  Text,
  StyleSheet,
  TextInput,
  Pressable,
  ActivityIndicator,
  Keyboard,
  KeyboardAvoidingView,
  Platform,
  TouchableOpacity,
  ScrollView,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { LinearGradient } from "expo-linear-gradient";
import { useRouter } from "expo-router";
import { ArrowRight, AlertCircle, RefreshCw, Database } from "lucide-react-native";
import { useTheme } from "@/src/ThemeProvider";
import { MAX_CALC_YEARS } from "@/src/api";
import { useMode } from "@/src/ModeProvider";
import { useTenure } from "@/src/TenureProvider";
import { radii, space } from "@/src/theme";

const MIN_YEARS = 1;
// A horizon beyond a fund's NAV history simply yields no rolling windows for
// that fund rather than an error — the backend echoes the horizon it
// actually used back as `resolved_horizon`. Lumpsum's engine computes every
// integer horizon 1..50 exactly like SIP's does (lumpsum_engine.ALL_HORIZONS)
// — there is no "nearest available horizon" rounding on the backend for
// either mode, so this screen must not claim there is one (see the removed
// resolveLumpsumHorizon hint below, which hardcoded a stale {3,5,7}-only
// assumption that stopped matching the backend a while ago).
const MAX_YEARS = MAX_CALC_YEARS;
const QUICK_SELECT_MAX = MAX_CALC_YEARS; // quick-select chips cover the full allowed range (1..50)
const DANGER    = "#EF4444";

// ── Quick-tap year chips ──────────────────────────────────────────────────────

const YEAR_CHIPS = Array.from({ length: QUICK_SELECT_MAX - MIN_YEARS + 1 }, (_, i) => MIN_YEARS + i);
const SIP_CHIPS     = YEAR_CHIPS;
const LUMPSUM_CHIPS = YEAR_CHIPS;

// ── Screen ────────────────────────────────────────────────────────────────────

export default function TenurePickerScreen() {
  const router       = useRouter();
  const { colors, scheme } = useTheme();
  const { mode }     = useMode();
  const { tenure: currentTenure, setTenure } = useTenure();
  const inputRef     = useRef<TextInput>(null);

  const [input,   setInput]   = useState(currentTenure !== null ? String(currentTenure) : "0");
  const [saving,  setSaving]  = useState(false);
  const [focused, setFocused] = useState(false);

  const isOnboarding = currentTenure === null;

  const parsed = (() => {
    const n = parseInt(input, 10);
    return Number.isFinite(n) ? n : null;
  })();

  const isValid   = parsed !== null && parsed >= MIN_YEARS && parsed <= MAX_YEARS;
  const rangeError =
    input.length > 0 && !isValid
      ? `Enter ${MIN_YEARS}–${MAX_YEARS} years`
      : null;
  const unchanged = currentTenure !== null && parsed === currentTenure;

  const chips = mode === "lumpsum" ? LUMPSUM_CHIPS : SIP_CHIPS;

  // ── Actions ──────────────────────────────────────────────────────────────────

  async function confirm() {
    if (!isValid || parsed === null || unchanged) return;
    Keyboard.dismiss();
    setSaving(true);
    await setTenure(parsed);
    setSaving(false);
    router.replace("/home" as any);
  }

  function cancel() {
    Keyboard.dismiss();
    if (router.canGoBack()) router.back();
    else router.replace("/home" as any);
  }

  function pickChip(y: number) {
    setInput(String(y));
    Keyboard.dismiss();
  }

  // ── Display string ────────────────────────────────────────────────────────

  const displayNum   = input.length > 0 ? input : "0";
  const displaySuffix =
    parsed !== null && isValid
      ? parsed === 1
        ? "year"
        : "years"
      : null;

  const isDark = scheme === "dark";

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <View style={{ flex: 1, backgroundColor: colors.bg }}>
      <SafeAreaView style={{ flex: 1 }} edges={["top"]}>
        <KeyboardAvoidingView
          style={{ flex: 1 }}
          behavior={Platform.OS === "ios" ? "padding" : undefined}
        >
          {/* ── Top bar ──────────────────────────────────── */}
          <View style={styles.topBar}>
            {!isOnboarding ? (
              <TouchableOpacity onPress={cancel} hitSlop={12}>
                <Text style={[styles.cancelBtn, { color: colors.azure }]}>
                  Cancel
                </Text>
              </TouchableOpacity>
            ) : (
              <View style={{ width: 64 }} />
            )}
            <Text style={[styles.topTitle, { color: colors.textPri }]}>
              {isOnboarding ? "Investment Horizon" : "Change Horizon"}
            </Text>
            <View style={{ width: 64 }} />
          </View>

          <ScrollView
            style={{ flex: 1 }}
            contentContainerStyle={{ flexGrow: 1 }}
            keyboardShouldPersistTaps="handled"
            showsVerticalScrollIndicator={false}
          >
          {/* ── Sub-heading ───────────────────────────────── */}
          <Text style={[styles.subHead, { color: colors.textTer }]}>
            {mode === "lumpsum"
              ? "How many years do you plan to stay invested?"
              : "How many years of rolling SIP returns to evaluate?"}
          </Text>

          {/* ── Giant number display ─────────────────────── */}
          <Pressable
            onPress={() => inputRef.current?.focus()}
            style={styles.displayArea}
          >
            <View
              style={[
                styles.displayCard,
                {
                  backgroundColor: colors.bgCard,
                  borderColor: rangeError
                    ? DANGER + "60"
                    : focused
                    ? colors.azure + "50"
                    : colors.border,
                  shadowColor: rangeError
                    ? DANGER
                    : focused
                    ? colors.azure
                    : isDark
                    ? "transparent"
                    : "#0F172A",
                  shadowOpacity: focused || rangeError ? 0.2 : 0.06,
                  shadowRadius: 16,
                  shadowOffset: { width: 0, height: 4 },
                  elevation: focused ? 6 : 2,
                },
              ]}
            >
              {/* Invisible TextInput — sits behind the display text */}
              <TextInput
                ref={inputRef}
                value={input}
                onChangeText={(v) => setInput(v.replace(/[^0-9]/g, ""))}
                onFocus={() => setFocused(true)}
                onBlur={() => setFocused(false)}
                keyboardType="number-pad"
                returnKeyType="done"
                maxLength={3}
                onSubmitEditing={confirm}
                autoFocus
                selectTextOnFocus
                style={styles.hiddenInput}
                caretHidden={false}
                cursorColor={rangeError ? DANGER : colors.azure}
                selectionColor={rangeError ? DANGER : colors.azure}
              />

              {/* Display number */}
              <Text
                style={[
                  styles.bigNum,
                  {
                    color: rangeError
                      ? DANGER
                      : isValid
                      ? colors.azure
                      : colors.textTer,
                  },
                ]}
              >
                {displayNum}
              </Text>

              {/* "years" suffix */}
              {displaySuffix && (
                <Text style={[styles.yearsSuffix, { color: colors.textSec }]}>
                  {displaySuffix}
                </Text>
              )}

              {/* Tap hint */}
              {input.length === 0 && !focused && (
                <Text style={[styles.tapHint, { color: colors.textTer }]}>
                  tap to type
                </Text>
              )}
            </View>

            {/* Error message */}
            {rangeError && (
              <View style={styles.errorRow}>
                <AlertCircle size={13} color={DANGER} />
                <Text style={[styles.errorText, { color: DANGER }]}>{rangeError}</Text>
              </View>
            )}
          </Pressable>

          {/* ── Chips ─────────────────────────────────────── */}
          <View style={styles.chipsSection}>
            <Text style={[styles.chipsLabel, { color: colors.textTer }]}>
              Quick select
            </Text>
            <View style={styles.chipsRow}>
              {chips.map((y) => {
                const isSelected = parsed === y;
                return (
                  <Pressable
                    key={y}
                    onPress={() => pickChip(y)}
                    style={({ pressed }) => [
                      styles.chip,
                      {
                        borderColor: isSelected ? colors.azure : colors.border,
                        backgroundColor: isSelected
                          ? colors.azure
                          : colors.bgCard,
                        opacity: pressed ? 0.75 : 1,
                      },
                    ]}
                  >
                    <Text
                      style={[
                        styles.chipText,
                        { color: isSelected ? "#fff" : colors.textSec },
                      ]}
                    >
                      {y}Y
                    </Text>
                  </Pressable>
                );
              })}
            </View>
          </View>
          </ScrollView>

          {/* ── Footer ────────────────────────────────────── */}
          <SafeAreaView edges={["bottom"]} style={{ backgroundColor: colors.bg }}>
            <View style={[styles.footer, { borderTopColor: colors.border }]}>
              {/* Confirm */}
              <Pressable
                onPress={confirm}
                disabled={!isValid || saving || unchanged}
                style={({ pressed }) => [
                  styles.confirmBtn,
                  {
                    backgroundColor:
                      isValid && !unchanged ? colors.azure : colors.bgElev,
                    opacity: pressed ? 0.85 : 1,
                    shadowColor: isValid && !unchanged ? colors.azure : "transparent",
                    shadowOpacity: 0.35,
                    shadowRadius: 12,
                    shadowOffset: { width: 0, height: 5 },
                    elevation: isValid && !unchanged ? 6 : 0,
                  },
                ]}
              >
                {saving ? (
                  <ActivityIndicator color="#fff" />
                ) : (
                  <>
                    <Text
                      style={[
                        styles.confirmText,
                        {
                          color:
                            isValid && !unchanged ? "#fff" : colors.textTer,
                        },
                      ]}
                    >
                      {isOnboarding ? "View Results" : "Apply Changes"}
                    </Text>
                    <ArrowRight
                      size={18}
                      color={isValid && !unchanged ? "#fff" : colors.textTer}
                    />
                  </>
                )}
              </Pressable>

              {/* Change Mode link */}
              <TouchableOpacity
                onPress={() => {
                  Keyboard.dismiss();
                  router.push("/mode-picker" as any);
                }}
                style={styles.modeRow}
                hitSlop={8}
              >
                <RefreshCw size={12} color={colors.textTer} />
                <Text style={[styles.modeText, { color: colors.textTer }]}>
                  In{" "}
                  <Text style={{ color: colors.azure, fontWeight: "700" }}>
                    {mode === "lumpsum" ? "Lumpsum" : "SIP"}
                  </Text>{" "}
                  mode · Change
                </Text>
              </TouchableOpacity>

              {/* Manual data refresh link — the app-side alternative to
                  curling /api/fqs/refresh and /api/lumpsum/refresh. */}
              <TouchableOpacity
                onPress={() => {
                  Keyboard.dismiss();
                  router.push("/data-refresh" as any);
                }}
                style={styles.modeRow}
                hitSlop={8}
              >
                <Database size={12} color={colors.textTer} />
                <Text style={[styles.modeText, { color: colors.textTer }]}>
                  Manual data refresh
                </Text>
              </TouchableOpacity>
            </View>
          </SafeAreaView>
        </KeyboardAvoidingView>
      </SafeAreaView>
    </View>
  );
}

// ── Styles ────────────────────────────────────────────────────────────────────

const styles = StyleSheet.create({
  topBar: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: space.md,
    paddingTop: space.sm,
    paddingBottom: 4,
  },
  topTitle:   { fontSize: 16, fontWeight: "700" },
  cancelBtn:  { fontSize: 15, fontWeight: "600" },

  subHead: {
    fontSize: 14,
    textAlign: "center",
    paddingHorizontal: space.xl,
    marginTop: 4,
    lineHeight: 20,
  },

  // Giant display
  displayArea: {
    alignItems: "center",
    paddingHorizontal: space.md,
    marginTop: space.lg,
    gap: 12,
  },
  displayCard: {
    width: "100%",
    alignItems: "center",
    justifyContent: "center",
    borderRadius: radii.lg + 4,
    borderWidth: 1.5,
    paddingVertical: 32,
    paddingHorizontal: 24,
    gap: 4,
    position: "relative",
    minHeight: 150,
  },
  hiddenInput: {
    position: "absolute",
    width: "100%",
    height: "100%",
    color: "transparent",
    fontSize: 72,
    fontWeight: "800",
    textAlign: "center",
    textAlignVertical: "center",
  },
  bigNum: {
    fontSize: 72,
    fontWeight: "800",
    letterSpacing: -3,
    fontVariant: ["tabular-nums"],
    lineHeight: 76,
  },
  yearsSuffix: {
    fontSize: 16,
    fontWeight: "600",
    letterSpacing: 0.5,
    textTransform: "lowercase",
    marginTop: -4,
  },
  tapHint: {
    fontSize: 13,
    fontWeight: "500",
    position: "absolute",
    bottom: 12,
  },

  errorRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
  },
  errorText: { fontSize: 13, fontWeight: "600" },

  // Chips
  chipsSection: {
    paddingHorizontal: space.md,
    marginTop: space.lg,
    gap: space.sm,
  },
  chipsLabel: {
    fontSize: 11,
    fontWeight: "700",
    letterSpacing: 1.2,
    textTransform: "uppercase",
  },
  chipsRow: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 8,
  },
  chip: {
    width: 46,
    paddingVertical: 11,
    borderRadius: radii.md,
    borderWidth: 1.5,
    alignItems: "center",
  },
  chipText: { fontSize: 14, fontWeight: "700" },

  // Footer
  footer: {
    paddingHorizontal: space.md,
    paddingTop: space.md,
    paddingBottom: space.md,
    borderTopWidth: StyleSheet.hairlineWidth,
    gap: 10,
  },
  confirmBtn: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: space.sm,
    borderRadius: radii.md,
    paddingVertical: 17,
  },
  confirmText: {
    fontSize: 17,
    fontWeight: "700",
    letterSpacing: 0.1,
  },
  modeRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 6,
    paddingVertical: 4,
  },
  modeText: { fontSize: 13, fontWeight: "400" },
});
