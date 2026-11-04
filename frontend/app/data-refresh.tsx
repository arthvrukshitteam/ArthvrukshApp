/**
 * Data Refresh — manual trigger for the two nightly batch jobs.
 *
 * Both jobs (FQS full-universe SIP scoring, Lumpsum full-universe scoring)
 * already run automatically at 22:00 UTC via .github/workflows/daily-refresh.yml.
 * This screen is the manual escape hatch for when an advisor wants a fresh
 * run right now (e.g. right after a deploy) instead of waiting for the next
 * cron tick, or wants to confirm from their phone that a run actually
 * finished — replacing what used to only be possible via curl against
 * /api/fqs/refresh and /api/lumpsum/refresh.
 *
 * Both endpoints are only token-gated when REFRESH_TOKEN is set as an env
 * var on the backend (see DEPLOY.md) — that's optional, and this deployment
 * currently doesn't set it (the same manual curl commands this screen
 * replaces never passed a token either). So the token field below is tucked
 * under an "Advanced" section, collapsed by default, rather than presented
 * as something you need before these buttons will work. If a refresh ever
 * IS token-gated later, a 401 auto-expands the section and explains why.
 * When used, the token is stored in the device Keychain/
 * EncryptedSharedPreferences (src/utils/storage's secure* methods), never
 * baked into the JS bundle.
 */

import { useCallback, useEffect, useState } from "react";
import {
  View,
  Text,
  StyleSheet,
  ScrollView,
  Pressable,
  TextInput,
  ActivityIndicator,
  RefreshControl,
  Alert,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { useFocusEffect } from "expo-router";
import {
  RefreshCw, Lock, CheckCircle2, AlertTriangle, Clock, Eye, EyeOff, ChevronDown, ChevronRight,
} from "lucide-react-native";
import { useTheme } from "@/src/ThemeProvider";
import { radii, space } from "@/src/theme";
import { api, type BatchRefreshStatus } from "@/src/api";
import { formatDataAge } from "@/src/utils/format";
import { storage } from "@/src/utils/storage";

const TOKEN_KEY = "refresh_token";
const POLL_MS = 20_000;

type JobKey = "fqs" | "lumpsum";

export default function DataRefreshScreen() {
  const { colors } = useTheme();

  const [token, setToken] = useState("");
  const [tokenInput, setTokenInput] = useState("");
  const [tokenVisible, setTokenVisible] = useState(false);
  const [tokenSaved, setTokenSaved] = useState(false);
  const [tokenLoaded, setTokenLoaded] = useState(false);
  // Collapsed unless a token is already saved — most deployments (including
  // this one right now) don't set REFRESH_TOKEN, so the buttons work with
  // this section left untouched. A 401 below forces it open.
  const [tokenSectionOpen, setTokenSectionOpen] = useState(false);

  const [fqsStatus, setFqsStatus] = useState<BatchRefreshStatus | null>(null);
  const [lumpsumStatus, setLumpsumStatus] = useState<BatchRefreshStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [triggering, setTriggering] = useState<JobKey | null>(null);
  const [jobMessage, setJobMessage] = useState<{ job: JobKey; text: string; isError: boolean } | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  // Load the saved token once on mount.
  useEffect(() => {
    (async () => {
      const saved = await storage.secureGet<string>(TOKEN_KEY, "");
      const t = saved ?? "";
      setToken(t);
      setTokenInput(t);
      setTokenSaved(t.length > 0);
      if (t.length > 0) setTokenSectionOpen(true);
      setTokenLoaded(true);
    })();
  }, []);

  const loadStatus = useCallback(async (silent = false) => {
    if (!silent) setStatusError(null);
    try {
      const [fqs, lumpsum] = await Promise.all([
        api.fqsRefreshStatus(),
        api.lumpsumRefreshStatus(),
      ]);
      setFqsStatus(fqs);
      setLumpsumStatus(lumpsum);
    } catch (e: any) {
      if (!silent) setStatusError(String(e?.message ?? e));
    }
  }, []);

  // Poll only while this screen is focused — no point burning battery/network
  // in the background, and a stuck poll here should never affect Home.
  useFocusEffect(
    useCallback(() => {
      loadStatus();
      const id = setInterval(() => loadStatus(true), POLL_MS);
      return () => clearInterval(id);
    }, [loadStatus]),
  );

  async function onSaveToken() {
    const trimmed = tokenInput.trim();
    await storage.secureSet(TOKEN_KEY, trimmed);
    setToken(trimmed);
    setTokenSaved(trimmed.length > 0);
  }

  function confirmAndTrigger(job: JobKey, label: string) {
    Alert.alert(
      `Refresh ${label}?`,
      "This recomputes every fund in the universe and runs for several hours in the background. Only start it if nothing else is about to be deployed.",
      [
        { text: "Cancel", style: "cancel" },
        { text: "Start refresh", onPress: () => trigger(job) },
      ],
    );
  }

  async function trigger(job: JobKey) {
    setJobMessage(null);
    setTriggering(job);
    try {
      const res = job === "fqs"
        ? await api.triggerFqsRefresh(token || undefined)
        : await api.triggerLumpsumRefresh(token || undefined);
      if (res.status === "already_running") {
        setJobMessage({ job, text: "Already running — nothing new started.", isError: false });
      } else {
        setJobMessage({
          job,
          text: "Started. This runs in the background and can take several hours — safe to close the app.",
          isError: false,
        });
      }
      await loadStatus(true);
    } catch (e: any) {
      const msg = String(e?.message ?? e);
      const unauthorized = msg.includes("401");
      if (unauthorized) setTokenSectionOpen(true);
      setJobMessage({
        job,
        text: unauthorized
          ? "Invalid or missing refresh token — enter the correct token below and try again."
          : `Failed to start: ${msg}`,
        isError: true,
      });
    } finally {
      setTriggering(null);
    }
  }

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }} edges={["bottom"]}>
      <ScrollView
        contentContainerStyle={{ padding: space.md, paddingBottom: space.xl, gap: space.md }}
        refreshControl={
          <RefreshControl
            refreshing={refreshing}
            onRefresh={async () => { setRefreshing(true); await loadStatus(); setRefreshing(false); }}
            tintColor={colors.azure}
          />
        }
      >
        <Text style={[styles.intro, { color: colors.textSec }]}>
          These jobs already run automatically every night. Use these buttons
          when you need a fresh run right now — after a deploy, or to confirm
          a scheduled run actually finished.
        </Text>

        <RefreshCard
          title="FQS Scores"
          subtitle="SIP universe · every fund, incl. Since-2010 numbers"
          status={fqsStatus}
          triggering={triggering === "fqs"}
          disabled={triggering !== null || !!fqsStatus?.running}
          onTrigger={() => confirmAndTrigger("fqs", "FQS Scores")}
          message={jobMessage?.job === "fqs" ? jobMessage : null}
          colors={colors}
          statCounts={(r) => [
            r.refreshed != null ? `${r.refreshed} refreshed` : null,
            r.refreshed_2010 != null ? `${r.refreshed_2010} since-2010` : null,
            r.refreshed_continuity != null ? `${r.refreshed_continuity} continuity` : null,
            r.failed != null ? `${r.failed} failed` : null,
          ]}
        />

        <RefreshCard
          title="Lumpsum Scores"
          subtitle="Lumpsum universe · every fund, incl. Since-2010 numbers"
          status={lumpsumStatus}
          triggering={triggering === "lumpsum"}
          disabled={triggering !== null || !!lumpsumStatus?.running}
          onTrigger={() => confirmAndTrigger("lumpsum", "Lumpsum Scores")}
          message={jobMessage?.job === "lumpsum" ? jobMessage : null}
          colors={colors}
          statCounts={(r) => [
            r.refreshed != null ? `${r.refreshed} refreshed` : null,
            r.failed != null ? `${r.failed} failed` : null,
            r.skipped != null ? `${r.skipped} skipped` : null,
          ]}
        />

        {statusError && (
          <Text style={[styles.errorText, { color: colors.ruby }]}>
            Couldn't load status: {statusError}
          </Text>
        )}

        <View style={[styles.warnCard, { backgroundColor: colors.amber + "14", borderColor: colors.amber + "40" }]}>
          <AlertTriangle size={15} color={colors.amber} />
          <Text style={[styles.warnText, { color: colors.textSec }]}>
            Avoid redeploying the backend while a refresh is running — a
            mid-run deploy silently kills the background job, and it won't
            resume on its own. Trigger a refresh only when nothing else is
            about to be pushed.
          </Text>
        </View>

        <View style={[styles.tokenCard, { backgroundColor: colors.bgCard, borderColor: colors.border }]}>
          <Pressable
            onPress={() => setTokenSectionOpen((v) => !v)}
            style={styles.tokenHeader}
            hitSlop={4}
          >
            {tokenSectionOpen ? <ChevronDown size={14} color={colors.textSec} /> : <ChevronRight size={14} color={colors.textSec} />}
            <Lock size={13} color={colors.textSec} />
            <Text style={[styles.tokenTitle, { color: colors.textPri }]}>
              Refresh token <Text style={{ fontWeight: "400", color: colors.textTer }}>(optional)</Text>
            </Text>
            {tokenSaved && (
              <View style={[styles.savedPill, { backgroundColor: colors.azureSoft }]}>
                <CheckCircle2 size={11} color={colors.azure} />
                <Text style={[styles.savedPillText, { color: colors.azure }]}>Saved</Text>
              </View>
            )}
          </Pressable>

          {tokenSectionOpen && (
            <>
              <Text style={[styles.tokenHelp, { color: colors.textTer }]}>
                Not needed right now — this backend doesn't require one (the
                curl commands it replaces never passed one either). Only fill
                this in if a refresh above fails with an authorization error,
                which means REFRESH_TOKEN has been set on Render since. Stored
                only in this device's secure storage, never sent anywhere
                except these two endpoints.
              </Text>
              <View style={[styles.tokenInputRow, { borderColor: colors.border, backgroundColor: colors.bg }]}>
                <TextInput
                  value={tokenInput}
                  onChangeText={setTokenInput}
                  placeholder="Paste REFRESH_TOKEN"
                  placeholderTextColor={colors.textTer}
                  secureTextEntry={!tokenVisible}
                  autoCapitalize="none"
                  autoCorrect={false}
                  style={[styles.tokenInput, { color: colors.textPri }]}
                  editable={tokenLoaded}
                />
                <Pressable onPress={() => setTokenVisible((v) => !v)} hitSlop={8}>
                  {tokenVisible ? <EyeOff size={16} color={colors.textTer} /> : <Eye size={16} color={colors.textTer} />}
                </Pressable>
              </View>
              <Pressable
                onPress={onSaveToken}
                disabled={!tokenLoaded || tokenInput === token}
                style={({ pressed }) => [
                  styles.saveBtn,
                  {
                    backgroundColor: tokenInput !== token ? colors.azure : colors.bgElev,
                    opacity: pressed ? 0.85 : 1,
                  },
                ]}
              >
                <Text style={[styles.saveBtnText, { color: tokenInput !== token ? "#fff" : colors.textTer }]}>
                  Save token
                </Text>
              </Pressable>
            </>
          )}
        </View>
      </ScrollView>
    </SafeAreaView>
  );
}

// ── Refresh card ─────────────────────────────────────────────────────────────

function RefreshCard({
  title,
  subtitle,
  status,
  triggering,
  disabled,
  onTrigger,
  message,
  colors,
  statCounts,
}: {
  title: string;
  subtitle: string;
  status: BatchRefreshStatus | null;
  triggering: boolean;
  disabled: boolean;
  onTrigger: () => void;
  message: { text: string; isError: boolean } | null;
  colors: ReturnType<typeof useTheme>["colors"];
  statCounts: (r: NonNullable<BatchRefreshStatus["last_run"]>) => (string | null)[];
}) {
  const running = !!status?.running;
  const age = formatDataAge(status?.last_run?.finished_at, "Last finished");
  const stats = status?.last_run ? statCounts(status.last_run).filter(Boolean) : [];

  return (
    <View style={[cardS.wrap, { backgroundColor: colors.bgCard, borderColor: colors.border }]}>
      <View style={cardS.headerRow}>
        <View style={{ flex: 1 }}>
          <Text style={[cardS.title, { color: colors.textPri }]}>{title}</Text>
          <Text style={[cardS.subtitle, { color: colors.textTer }]}>{subtitle}</Text>
        </View>
        <View
          style={[
            cardS.statusPill,
            { backgroundColor: running ? colors.azureSoft : colors.bgElev },
          ]}
        >
          {running
            ? <ActivityIndicator size="small" color={colors.azure} />
            : <CheckCircle2 size={12} color={colors.textSec} />}
          <Text style={[cardS.statusPillText, { color: running ? colors.azure : colors.textSec }]}>
            {running ? "Running" : "Idle"}
          </Text>
        </View>
      </View>

      {age && (
        <View style={cardS.ageRow}>
          <Clock size={11} color={age.stale ? colors.amber : colors.textTer} />
          <Text style={[cardS.ageText, { color: age.stale ? colors.amber : colors.textTer, fontWeight: age.stale ? "700" : "400" }]}>
            {age.text}
          </Text>
        </View>
      )}

      {stats.length > 0 && (
        <Text style={[cardS.statsText, { color: colors.textTer }]}>{stats.join(" · ")}</Text>
      )}

      {message && (
        <Text style={[cardS.messageText, { color: message.isError ? colors.ruby : colors.azure }]}>
          {message.text}
        </Text>
      )}

      <Pressable
        onPress={onTrigger}
        disabled={disabled}
        style={({ pressed }) => [
          cardS.triggerBtn,
          {
            backgroundColor: disabled ? colors.bgElev : colors.azure,
            opacity: pressed ? 0.85 : 1,
          },
        ]}
      >
        {triggering ? (
          <ActivityIndicator size="small" color={disabled ? colors.textTer : "#fff"} />
        ) : (
          <RefreshCw size={14} color={disabled ? colors.textTer : "#fff"} />
        )}
        <Text style={[cardS.triggerBtnText, { color: disabled ? colors.textTer : "#fff" }]}>
          {running ? "Refresh in progress…" : "Refresh Now"}
        </Text>
      </Pressable>
    </View>
  );
}

// ── Styles ────────────────────────────────────────────────────────────────────

const styles = StyleSheet.create({
  intro: { fontSize: 13, lineHeight: 19 },
  errorText: { fontSize: 12.5, fontWeight: "600" },

  warnCard: {
    flexDirection: "row",
    alignItems: "flex-start",
    gap: 8,
    borderWidth: 1,
    borderRadius: radii.md,
    padding: space.sm + 2,
  },
  warnText: { flex: 1, fontSize: 12, lineHeight: 17, fontWeight: "500" },

  tokenCard: {
    borderWidth: 1,
    borderRadius: radii.lg,
    padding: space.md,
    gap: 8,
  },
  tokenHeader: { flexDirection: "row", alignItems: "center", gap: 6 },
  tokenTitle: { fontSize: 13.5, fontWeight: "700" },
  savedPill: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: radii.pill,
    marginLeft: "auto",
  },
  savedPillText: { fontSize: 10, fontWeight: "700" },
  tokenHelp: { fontSize: 11.5, lineHeight: 16 },
  tokenInputRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    borderWidth: 1,
    borderRadius: radii.md,
    paddingHorizontal: 12,
    paddingVertical: 10,
  },
  tokenInput: { flex: 1, fontSize: 14, padding: 0 },
  saveBtn: {
    alignItems: "center",
    justifyContent: "center",
    borderRadius: radii.md,
    paddingVertical: 10,
  },
  saveBtnText: { fontSize: 13.5, fontWeight: "700" },
});

const cardS = StyleSheet.create({
  wrap: {
    borderWidth: 1,
    borderRadius: radii.lg,
    padding: space.md,
    gap: 8,
  },
  headerRow: { flexDirection: "row", alignItems: "flex-start", gap: 8 },
  title: { fontSize: 15, fontWeight: "700" },
  subtitle: { fontSize: 11.5, marginTop: 2 },
  statusPill: {
    flexDirection: "row",
    alignItems: "center",
    gap: 5,
    paddingHorizontal: 9,
    paddingVertical: 5,
    borderRadius: radii.pill,
  },
  statusPillText: { fontSize: 11, fontWeight: "700" },
  ageRow: { flexDirection: "row", alignItems: "center", gap: 4 },
  ageText: { fontSize: 11.5 },
  statsText: { fontSize: 11, lineHeight: 15 },
  messageText: { fontSize: 12, fontWeight: "600", lineHeight: 16 },
  triggerBtn: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 7,
    borderRadius: radii.md,
    paddingVertical: 11,
    marginTop: 2,
  },
  triggerBtnText: { fontSize: 13.5, fontWeight: "700" },
});
