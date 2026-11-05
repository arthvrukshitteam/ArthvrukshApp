/**
 * App Lock settings — set up, change or turn off the 4-digit PIN.
 * Every change to an existing PIN requires the current PIN first.
 */

import { useState } from "react";
import { View, Text, Pressable, StyleSheet, ScrollView } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { Stack, useLocalSearchParams, useRouter } from "expo-router";
import { Lock, LockOpen, KeyRound, ShieldCheck } from "lucide-react-native";
import { useTheme } from "@/src/ThemeProvider";
import { useAppLock } from "@/src/AppLockProvider";
import { PinPad, PinSetupFlow, describeFailure } from "@/src/components/AppLock";
import { radii, space } from "@/src/theme";

type Step = "home" | "setup" | "verify-change" | "change" | "verify-off";

export default function SecurityScreen() {
  const { colors } = useTheme();
  const router = useRouter();
  const { setup } = useLocalSearchParams<{ setup?: string }>();
  const { enabled, verify, disable, lockNow } = useAppLock();
  const [step, setStep] = useState<Step>(setup === "1" && !enabled ? "setup" : "home");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);

  const finish = (msg: string) => { setDone(msg); setError(null); setStep("home"); };

  const checkCurrent = async (pin: string, next: () => void) => {
    setBusy(true);
    try {
      const r = await verify(pin);
      if (r.ok) { setError(null); next(); }
      else setError(describeFailure(r, Date.now()));
    } finally {
      setBusy(false);
    }
  };

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }} edges={["bottom"]}>
      <Stack.Screen options={{ title: "App Lock (PIN)" }} />
      <ScrollView contentContainerStyle={st.scroll}>
        {step === "home" && (
          <>
            <View style={[st.card, { backgroundColor: colors.bgCard, borderColor: colors.border }]}>
              <View style={st.row}>
                {enabled ? <Lock size={22} color={colors.tierElite} /> : <LockOpen size={22} color={colors.textTer} />}
                <View style={{ flex: 1 }}>
                  <Text style={[st.title, { color: colors.textPri }]}>
                    {enabled ? "PIN lock is on" : "PIN lock is off"}
                  </Text>
                  <Text style={[st.body, { color: colors.textSec }]}>
                    {enabled
                      ? "The app asks for your PIN when it opens, and again after a minute in the background."
                      : "Anyone holding this device can open the app and see client details."}
                  </Text>
                </View>
              </View>
            </View>

            {done && (
              <View style={[st.notice, { backgroundColor: colors.azureSoft }]}>
                <ShieldCheck size={16} color={colors.azure} />
                <Text style={{ color: colors.azure, fontWeight: "700", flex: 1 }}>{done}</Text>
              </View>
            )}

            {!enabled ? (
              <Action label="Set up PIN" Icon={KeyRound} primary onPress={() => { setDone(null); setStep("setup"); }} />
            ) : (
              <>
                <Action label="Change PIN" Icon={KeyRound} onPress={() => { setDone(null); setError(null); setStep("verify-change"); }} />
                <Action label="Lock now" Icon={Lock} onPress={() => { lockNow(); router.back(); }} />
                <Action label="Turn off PIN" Icon={LockOpen} danger onPress={() => { setDone(null); setError(null); setStep("verify-off"); }} />
              </>
            )}

            <Text style={[st.footnote, { color: colors.textTer }]}>
              The PIN is stored only as a salted hash in this device&apos;s secure storage — never in
              plain text, and never sent to the server. Five wrong attempts start a cooldown that
              grows with each further miss.
            </Text>
          </>
        )}

        {step === "setup" && (
          <PinSetupFlow onFinished={() => finish("PIN set. The app will now ask for it when it opens.")} />
        )}

        {step === "verify-change" && (
          <PinPad
            title="Enter current PIN"
            subtitle="Confirm it's you before choosing a new PIN."
            error={error}
            busy={busy}
            onComplete={(p) => checkCurrent(p, () => setStep("change"))}
          />
        )}

        {step === "change" && (
          <PinSetupFlow heading="Choose a new PIN" onFinished={() => finish("PIN changed. Your old recovery code no longer works.")} />
        )}

        {step === "verify-off" && (
          <PinPad
            title="Enter current PIN"
            subtitle="Turning the lock off removes the PIN from this device."
            error={error}
            busy={busy}
            onComplete={(p) => checkCurrent(p, async () => {
              await disable();
              finish("PIN lock turned off.");
            })}
          />
        )}

        {step !== "home" && (
          <Pressable onPress={() => { setStep("home"); setError(null); }} style={st.cancel} hitSlop={8}>
            <Text style={{ color: colors.textSec, fontWeight: "700" }}>Cancel</Text>
          </Pressable>
        )}
      </ScrollView>
    </SafeAreaView>
  );
}

function Action({
  label, Icon, onPress, primary = false, danger = false,
}: {
  label: string; Icon: typeof Lock; onPress: () => void; primary?: boolean; danger?: boolean;
}) {
  const { colors } = useTheme();
  const fg = primary ? "#fff" : danger ? colors.ruby : colors.textPri;
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      style={({ pressed }) => [
        st.action,
        {
          backgroundColor: primary ? colors.azure : colors.bgCard,
          borderColor: primary ? colors.azure : danger ? colors.ruby + "55" : colors.border,
          opacity: pressed ? 0.85 : 1,
        },
      ]}
    >
      <Icon size={18} color={fg} />
      <Text style={{ color: fg, fontWeight: "700", fontSize: 15 }}>{label}</Text>
    </Pressable>
  );
}

const st = StyleSheet.create({
  scroll: { padding: space.md, gap: space.md, paddingBottom: space.xl * 2 },
  card: { borderWidth: 1, borderRadius: radii.lg, padding: space.md },
  row: { flexDirection: "row", gap: 12, alignItems: "flex-start" },
  title: { fontSize: 16, fontWeight: "800" },
  body: { fontSize: 13, lineHeight: 19, marginTop: 4 },
  notice: { flexDirection: "row", gap: 8, alignItems: "center", padding: 12, borderRadius: radii.md },
  action: {
    flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 8,
    borderWidth: 1, borderRadius: radii.md, paddingVertical: 14,
  },
  footnote: { fontSize: 12, lineHeight: 17, marginTop: space.sm },
  cancel: { alignSelf: "center", paddingVertical: 12 },
});
