/**
 * PIN lock UI: keypad, set-PIN flow, recovery-code card, the lock screen,
 * and the gate that overlays the app while it is locked.
 *
 * The keypad is drawn in-app rather than using the system keyboard, so the
 * PIN entry never fights the keyboard for screen space and never offers
 * autofill/suggestions for the digits.
 */

import { useCallback, useEffect, useState } from "react";
import {
  View, Text, Pressable, StyleSheet, TextInput, ActivityIndicator, Share,
  KeyboardAvoidingView, Platform, ScrollView,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { useRouter } from "expo-router";
import { Delete, Lock, ShieldCheck, KeyRound } from "lucide-react-native";
import { useTheme } from "@/src/ThemeProvider";
import { useAppLock } from "@/src/AppLockProvider";
import { BrandLogo } from "@/src/components/BrandLogo";
import { radii, space, type ThemeColors } from "@/src/theme";
import { PIN_LENGTH, type VerifyResult } from "@/src/security/pin";

// ── Keypad ───────────────────────────────────────────────────────────────────

export function PinPad({
  title, subtitle, error, disabled = false, busy = false, onComplete,
}: {
  title: string;
  subtitle?: string | null;
  error?: string | null;
  disabled?: boolean;
  busy?: boolean;
  onComplete: (pin: string) => void;
}) {
  const { colors } = useTheme();
  // Cleared right after each completed entry (see press), so a wrong PIN
  // always starts the next attempt from empty dots.
  const [digits, setDigits] = useState("");

  const press = useCallback((d: string) => {
    if (disabled || busy) return;
    setDigits((cur) => {
      if (cur.length >= PIN_LENGTH) return cur;
      const next = cur + d;
      if (next.length === PIN_LENGTH) {
        setTimeout(() => { onComplete(next); setDigits(""); }, 60);
      }
      return next;
    });
  }, [disabled, busy, onComplete]);

  const back = useCallback(() => setDigits((cur) => cur.slice(0, -1)), []);

  const keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "", "0", "del"];

  return (
    <View style={pad.wrap}>
      <Text style={[pad.title, { color: colors.textPri }]}>{title}</Text>
      {subtitle ? <Text style={[pad.subtitle, { color: colors.textSec }]}>{subtitle}</Text> : null}

      <View style={pad.dots} accessibilityLabel={`${digits.length} of ${PIN_LENGTH} digits entered`}>
        {Array.from({ length: PIN_LENGTH }, (_, i) => (
          <View
            key={i}
            style={[
              pad.dot,
              {
                borderColor: error ? colors.ruby : colors.azure,
                backgroundColor: i < digits.length ? (error ? colors.ruby : colors.azure) : "transparent",
              },
            ]}
          />
        ))}
      </View>

      <View style={{ minHeight: 36, justifyContent: "center" }}>
        {busy ? (
          <ActivityIndicator color={colors.azure} />
        ) : error ? (
          <Text style={[pad.error, { color: colors.ruby }]} accessibilityLiveRegion="assertive">{error}</Text>
        ) : null}
      </View>

      <View style={pad.grid}>
        {keys.map((k, i) => {
          if (k === "") return <View key={i} style={pad.key} />;
          const isDel = k === "del";
          return (
            <Pressable
              key={i}
              onPress={() => (isDel ? back() : press(k))}
              disabled={disabled || busy}
              accessibilityRole="button"
              accessibilityLabel={isDel ? "Delete digit" : `Digit ${k}`}
              style={({ pressed }) => [
                pad.key,
                !isDel && { backgroundColor: pressed ? colors.azureSoft : colors.bgCard, borderColor: colors.border },
                (disabled || busy) && { opacity: 0.4 },
              ]}
            >
              {isDel
                ? <Delete size={24} color={colors.textSec} />
                : <Text style={[pad.keyText, { color: colors.textPri }]}>{k}</Text>}
            </Pressable>
          );
        })}
      </View>
    </View>
  );
}

const pad = StyleSheet.create({
  wrap: { alignItems: "center", width: "100%", maxWidth: 340, alignSelf: "center" },
  title: { fontSize: 20, fontWeight: "800", textAlign: "center" },
  subtitle: { fontSize: 13.5, textAlign: "center", marginTop: 6, lineHeight: 19 },
  dots: { flexDirection: "row", gap: 18, marginTop: space.lg },
  dot: { width: 16, height: 16, borderRadius: 8, borderWidth: 2 },
  error: { fontSize: 13, fontWeight: "700", textAlign: "center" },
  grid: { flexDirection: "row", flexWrap: "wrap", justifyContent: "center", gap: 14, marginTop: space.sm, width: 276 },
  key: {
    width: 76, height: 64, borderRadius: radii.lg, borderWidth: StyleSheet.hairlineWidth,
    borderColor: "transparent", alignItems: "center", justifyContent: "center",
  },
  keyText: { fontSize: 26, fontWeight: "600", fontVariant: ["tabular-nums"] },
});

// ── Recovery code (shown once) ───────────────────────────────────────────────

export function RecoveryCodeCard({ code, onDone }: { code: string; onDone: () => void }) {
  const { colors } = useTheme();
  return (
    <View style={[rc.card, { backgroundColor: colors.bgCard, borderColor: colors.border }]}>
      <KeyRound size={26} color={colors.azure} />
      <Text style={[rc.title, { color: colors.textPri }]}>Save your recovery code</Text>
      <Text style={[rc.body, { color: colors.textSec }]}>
        If you forget your PIN, this code is the only way to reset it on this device. It is shown
        once and is not stored anywhere readable, so write it down or save it somewhere safe now.
      </Text>
      <Text selectable style={[rc.code, { color: colors.textPri, borderColor: colors.azure, backgroundColor: colors.azureSoft }]}>
        {code}
      </Text>
      <Pressable
        onPress={() => Share.share({ message: `ArthVruksh app recovery code: ${code}` }).catch(() => {})}
        style={({ pressed }) => [rc.secondary, { borderColor: colors.border, opacity: pressed ? 0.7 : 1 }]}
      >
        <Text style={{ color: colors.textSec, fontWeight: "700" }}>Share / save code</Text>
      </Pressable>
      <Pressable
        onPress={onDone}
        style={({ pressed }) => [rc.primary, { backgroundColor: colors.azure, opacity: pressed ? 0.85 : 1 }]}
      >
        <Text style={{ color: "#fff", fontWeight: "800", fontSize: 15 }}>I&apos;ve saved it</Text>
      </Pressable>
    </View>
  );
}

const rc = StyleSheet.create({
  card: { borderRadius: radii.lg, borderWidth: 1, padding: space.lg, alignItems: "center", gap: 10, width: "100%", maxWidth: 420, alignSelf: "center" },
  title: { fontSize: 18, fontWeight: "800" },
  body: { fontSize: 13.5, lineHeight: 19, textAlign: "center" },
  code: {
    fontSize: 26, fontWeight: "800", letterSpacing: 4, paddingHorizontal: 18, paddingVertical: 10,
    borderRadius: radii.md, borderWidth: 1.5, marginVertical: 6, fontVariant: ["tabular-nums"],
  },
  secondary: { borderWidth: 1, borderRadius: radii.md, paddingVertical: 10, paddingHorizontal: 18 },
  primary: { borderRadius: radii.md, paddingVertical: 14, alignSelf: "stretch", alignItems: "center", marginTop: 4 },
});

// ── New PIN: enter → confirm → save → show recovery code ─────────────────────

export function PinSetupFlow({
  heading, onFinished,
}: { heading?: string; onFinished: () => void }) {
  const { savePin } = useAppLock();
  const [step, setStep] = useState<"new" | "confirm" | "saving" | "recovery">("new");
  const [first, setFirst] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [recovery, setRecovery] = useState<string | null>(null);

  if (step === "recovery" && recovery) {
    return <RecoveryCodeCard code={recovery} onDone={onFinished} />;
  }
  if (step === "new") {
    return (
      <PinPad
        key="new"
        title={heading ?? "Choose a 4-digit PIN"}
        subtitle="You'll enter it each time you open the app."
        error={error}
        onComplete={(p) => {
          if (/^(\d)\1{3}$/.test(p) || p === "1234" || p === "4321") {
            setError("Too easy to guess — pick a less obvious PIN.");
            return;
          }
          setError(null);
          setFirst(p);
          setStep("confirm");
        }}
      />
    );
  }
  return (
    <PinPad
      key="confirm"
      title="Confirm your PIN"
      subtitle="Enter the same 4 digits again."
      error={error}
      busy={step === "saving"}
      onComplete={async (p) => {
        if (p !== first) {
          setError("PINs didn't match. Start again.");
          setFirst("");
          setStep("new");
          return;
        }
        setStep("saving");
        try {
          setRecovery(await savePin(p));
          setStep("recovery");
        } catch (e: any) {
          setError(e?.message ?? "Couldn't save the PIN.");
          setStep("new");
        }
      }}
    />
  );
}

// ── Helpers ──────────────────────────────────────────────────────────────────

export function describeFailure(r: VerifyResult, now: number): string | null {
  if (r.ok) return null;
  if (r.reason === "locked") return lockoutText(r.lockedUntil, now);
  return r.attemptsLeft > 0
    ? `Incorrect. ${r.attemptsLeft} attempt${r.attemptsLeft === 1 ? "" : "s"} left before a short lockout.`
    : "Incorrect.";
}

function lockoutText(until: number, now: number): string {
  const s = Math.max(0, Math.ceil((until - now) / 1000));
  const mm = Math.floor(s / 60);
  const ss = String(s % 60).padStart(2, "0");
  return `Too many attempts. Try again in ${mm}:${ss}.`;
}

function useNow(active: boolean) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [active]);
  return now;
}

// ── Lock screen ──────────────────────────────────────────────────────────────

function LockScreen() {
  const { colors } = useTheme();
  const { unlock, verifyRecovery, markUnlocked } = useAppLock();
  const [mode, setMode] = useState<"enter" | "recover" | "reset">("enter");
  const [error, setError] = useState<string | null>(null);
  const [lockedUntil, setLockedUntil] = useState(0);
  const [busy, setBusy] = useState(false);
  const [code, setCode] = useState("");
  const now = useNow(lockedUntil > 0);
  const lockedOut = lockedUntil > now;
  // Once a cooldown has run out its message no longer applies.
  const shownError = lockedOut ? lockoutText(lockedUntil, now) : lockedUntil ? null : error;

  const apply = (r: VerifyResult) => {
    setLockedUntil(!r.ok && r.reason === "locked" ? r.lockedUntil : 0);
    setError(describeFailure(r, Date.now()));
  };

  return (
    <SafeAreaView style={[StyleSheet.absoluteFill, { backgroundColor: colors.bg, zIndex: 1000 }]}>
      <KeyboardAvoidingView behavior={Platform.OS === "ios" ? "padding" : undefined} style={{ flex: 1 }}>
        <ScrollView contentContainerStyle={lock.scroll} keyboardShouldPersistTaps="handled">
          <View style={lock.brand}>
            <BrandLogo size={56} />
            <View style={[lock.badge, { backgroundColor: colors.azureSoft }]}>
              <Lock size={12} color={colors.azure} />
              <Text style={{ color: colors.azure, fontWeight: "800", fontSize: 11 }}>LOCKED</Text>
            </View>
          </View>

          {mode === "enter" && (
            <>
              <PinPad
                title="Enter your PIN"
                subtitle="ArthVruksh is locked."
                error={shownError}
                disabled={lockedOut}
                busy={busy}
                onComplete={async (p) => {
                  setBusy(true);
                  try { apply(await unlock(p)); } finally { setBusy(false); }
                }}
              />
              <Pressable onPress={() => { setMode("recover"); setError(null); }} style={lock.link} hitSlop={8}>
                <Text style={{ color: colors.azure, fontWeight: "700" }}>Forgot PIN?</Text>
              </Pressable>
            </>
          )}

          {mode === "recover" && (
            <View style={[lock.recoverCard, { backgroundColor: colors.bgCard, borderColor: colors.border }]}>
              <Text style={[lock.recoverTitle, { color: colors.textPri }]}>Reset with your recovery code</Text>
              <Text style={{ color: colors.textSec, fontSize: 13.5, lineHeight: 19 }}>
                Enter the 8-character code you saved when you set up your PIN.
              </Text>
              <TextInput
                value={code}
                onChangeText={setCode}
                placeholder="XXXX-XXXX"
                placeholderTextColor={colors.textTer}
                autoCapitalize="characters"
                autoCorrect={false}
                editable={!lockedOut && !busy}
                style={[lock.input, { color: colors.textPri, borderColor: colors.border, backgroundColor: colors.bg }]}
                accessibilityLabel="Recovery code"
              />
              {shownError && (
                <Text style={{ color: colors.ruby, fontWeight: "700", fontSize: 13 }}>{shownError}</Text>
              )}
              <Pressable
                disabled={lockedOut || busy || code.trim().length < 8}
                onPress={async () => {
                  setBusy(true);
                  try {
                    const r = await verifyRecovery(code);
                    if (r.ok) { setCode(""); setError(null); setMode("reset"); }
                    else apply(r);
                  } finally { setBusy(false); }
                }}
                style={({ pressed }) => [
                  lock.primary,
                  { backgroundColor: colors.azure, opacity: lockedOut || code.trim().length < 8 ? 0.5 : pressed ? 0.85 : 1 },
                ]}
              >
                {busy ? <ActivityIndicator color="#fff" /> : <Text style={lock.primaryText}>Verify code</Text>}
              </Pressable>
              <Pressable onPress={() => { setMode("enter"); setError(null); }} style={lock.link} hitSlop={8}>
                <Text style={{ color: colors.textSec, fontWeight: "700" }}>Back to PIN</Text>
              </Pressable>
              <Text style={{ color: colors.textTer, fontSize: 12, lineHeight: 17 }}>
                Lost the recovery code too? Clearing the app&apos;s data (or reinstalling it) removes the
                PIN along with all other saved settings on this device.
              </Text>
            </View>
          )}

          {mode === "reset" && (
            <PinSetupFlow heading="Choose a new PIN" onFinished={markUnlocked} />
          )}
        </ScrollView>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const lock = StyleSheet.create({
  scroll: { flexGrow: 1, justifyContent: "center", padding: space.lg, gap: space.md },
  brand: { alignItems: "center", gap: 10, marginBottom: space.sm },
  badge: { flexDirection: "row", alignItems: "center", gap: 5, paddingHorizontal: 10, paddingVertical: 4, borderRadius: radii.pill },
  link: { alignSelf: "center", paddingVertical: 10 },
  recoverCard: { borderWidth: 1, borderRadius: radii.lg, padding: space.lg, gap: 12, width: "100%", maxWidth: 420, alignSelf: "center" },
  recoverTitle: { fontSize: 18, fontWeight: "800" },
  input: {
    borderWidth: 1, borderRadius: radii.md, paddingHorizontal: space.md, paddingVertical: 12,
    fontSize: 20, fontWeight: "700", letterSpacing: 3, textAlign: "center",
  },
  primary: { borderRadius: radii.md, paddingVertical: 14, alignItems: "center" },
  primaryText: { color: "#fff", fontWeight: "800", fontSize: 15 },
});

// ── First-run prompt ─────────────────────────────────────────────────────────

function SetupPrompt({ colors }: { colors: ThemeColors }) {
  const router = useRouter();
  const { dismissSetupPrompt } = useAppLock();
  return (
    <View style={[StyleSheet.absoluteFill, prompt.backdrop, { backgroundColor: colors.backdrop }]}>
      <View style={[prompt.card, { backgroundColor: colors.bgCard, borderColor: colors.border }]}>
        <ShieldCheck size={30} color={colors.azure} />
        <Text style={[prompt.title, { color: colors.textPri }]}>Protect ArthVruksh with a PIN</Text>
        <Text style={[prompt.body, { color: colors.textSec }]}>
          Client proposals and portfolio reviews hold personal details. A 4-digit PIN keeps them
          private if someone else picks up this device.
        </Text>
        <Pressable
          onPress={() => { dismissSetupPrompt(); router.push("/security?setup=1" as any); }}
          style={({ pressed }) => [prompt.primary, { backgroundColor: colors.azure, opacity: pressed ? 0.85 : 1 }]}
        >
          <Text style={{ color: "#fff", fontWeight: "800", fontSize: 15 }}>Set up PIN</Text>
        </Pressable>
        <Pressable onPress={dismissSetupPrompt} hitSlop={8} style={{ paddingVertical: 8 }}>
          <Text style={{ color: colors.textSec, fontWeight: "700" }}>Not now</Text>
        </Pressable>
        <Text style={{ color: colors.textTer, fontSize: 11.5, textAlign: "center" }}>
          You can turn it on later from the menu → App Lock (PIN).
        </Text>
      </View>
    </View>
  );
}

const prompt = StyleSheet.create({
  backdrop: { alignItems: "center", justifyContent: "center", padding: space.lg, zIndex: 900 },
  card: { borderWidth: 1, borderRadius: radii.lg, padding: space.lg, alignItems: "center", gap: 10, width: "100%", maxWidth: 400 },
  title: { fontSize: 18, fontWeight: "800", textAlign: "center" },
  body: { fontSize: 13.5, lineHeight: 19, textAlign: "center" },
  primary: { borderRadius: radii.md, paddingVertical: 14, alignSelf: "stretch", alignItems: "center", marginTop: 6 },
});

// ── Gate ─────────────────────────────────────────────────────────────────────

/** Renders the app, and on top of it the lock screen while locked. Until the
 *  stored lock state has been read, an opaque cover hides the app so nothing
 *  flashes before the lock screen. `onboarded` delays the first-run prompt
 *  until mode/tenure onboarding is done. */
export function AppLockGate({ children, onboarded }: { children: React.ReactNode; onboarded: boolean }) {
  const { colors } = useTheme();
  const { ready, enabled, locked, setupPromptVisible } = useAppLock();
  return (
    <View style={{ flex: 1 }}>
      {children}
      {!ready && <View style={[StyleSheet.absoluteFill, { backgroundColor: colors.bg, zIndex: 1000 }]} />}
      {ready && enabled && locked && <LockScreen />}
      {ready && !enabled && setupPromptVisible && onboarded && <SetupPrompt colors={colors} />}
    </View>
  );
}
