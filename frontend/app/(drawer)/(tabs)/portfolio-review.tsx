/**
 * Portfolio Review tab — Bulk Fund Analysis.
 *
 * Upload an Excel of an existing client portfolio (any columns — Fund Name,
 * Category, AMC, ISIN, AUM, whatever the advisor's own tracker already has)
 * and get back ONE sheet: their own sheet with our analysis merged into it,
 * in ArthVruksh styling, ready to send a client as-is.
 *
 * Every column they uploaded survives. A column of theirs that means the same
 * as one of ours is filled rather than duplicated. Ours sit left-to-right in
 * the specified order — Fund Name, Category, Benchmark XIRR, Allocation, No
 * of Windows, FQS, Success%, Median WM, XIRR (Med), P10 WM, CV (Stability),
 * Remarks — and theirs follow. See backend/bulk_analysis.py's APP_COLUMNS for
 * the exact contract.
 *
 * FQS is the 10Y rolling score, or the fund's best available shorter horizon
 * when it doesn't have 10Y yet (annotated e.g. "70.7 (6 YR)").
 *
 * This screen is pure orchestration UI — every number comes from
 * POST /api/bulk-analysis/*, which itself only reads the existing engine's
 * cached/live output (see backend/bulk_analysis.py). No calculation happens
 * here or in that endpoint.
 *
 * Flow: pick file -> upload (parses + previews, computes nothing yet) ->
 * "Process Excel" (kicks off the batch) -> poll real progress -> download.
 */

import { memo, useCallback, useEffect, useRef, useState } from "react";
import {
  View, Text, StyleSheet, ScrollView, Pressable, ActivityIndicator, Alert, Platform,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import * as DocumentPicker from "expo-document-picker";
import * as FileSystem from "expo-file-system/legacy";
import * as Sharing from "expo-sharing";
import {
  FileSpreadsheet, Upload, XCircle, AlertTriangle,
  Download, RotateCcw, ListChecks,
} from "lucide-react-native";
import { api, type BulkAnalysisUploadResponse, type BulkAnalysisJobStatus } from "@/src/api";
import { useTheme } from "@/src/ThemeProvider";
import { radii, space, type ThemeColors, type ThemeType } from "@/src/theme";

const EXCEL_MIME_TYPES = [
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", // .xlsx
  "application/vnd.ms-excel", // .xls
];

const POLL_INTERVAL_MS = 1500;
/** Backoff ceiling when /status keeps failing — see startPolling. */
const MAX_POLL_INTERVAL_MS = 10000;

type PickedFile = { uri: string; name: string; mimeType?: string | null; webFile?: any };

/** Whether a file-read failure is the known Expo Go Android limitation.
 *
 * Matched on the two things that identify it rather than on the whole message
 * (which carries a per-pick cache path): the unreadable-location IOException,
 * and the Expo Go package name in the URI. A read that fails for a real
 * reason — a deleted file, a permissions denial in a proper build — must
 * still surface its own error. */
function isExpoGoReadFailure(error: unknown): boolean {
  const message = String((error as any)?.message ?? error ?? "");
  return message.includes("host.exp.exponent")
    && (message.includes("isn't readable") || message.includes("IOException"));
}

/** Resolves after the next frame has been painted.
 *
 * Used to let a state change reach the screen before starting synchronous
 * work that will block the JS thread — see uploadFile.
 *
 * Deliberately NOT InteractionManager.runAfterInteractions: that is
 * deprecated as of React Native 0.86 (this project's version) and logs a
 * warning banner over the app on every upload. requestAnimationFrame fires
 * before the frame is drawn, so the nested setTimeout yields once more and
 * lets the commit actually land. */
function nextFrame(): Promise<void> {
  return new Promise((resolve) => {
    requestAnimationFrame(() => setTimeout(resolve, 0));
  });
}

/** Uploads the picked Excel file and returns the parsed preview.
 *
 * Native RN's file-upload story proved unreliable across two different
 * approaches during testing: `new FormData().append("file", {uri,name,type})`
 * (the classic "file-like object" trick) throws "Unsupported FormDataPart
 * implementation" under the new architecture/Hermes, and
 * expo-file-system's uploadAsync threw an IOException reading its own app's
 * DocumentPicker cache file inside Expo Go. Base64-over-JSON sidesteps all
 * of that — it's a plain string in a JSON body, the same code path as every
 * other request this app already makes successfully. Web keeps real
 * multipart (a real Blob/File there works fine and avoids the ~33% base64
 * size bump).
 *
 * `readAsStringAsync` is, from React Native's point of view, synchronous work:
 * on a multi-MB workbook it blocks the JS thread long enough that the button's
 * spinner never appears and the screen reads as frozen. Yielding first lets
 * the "Reading file…" state paint BEFORE the blocking read starts, so the tap
 * is always visibly acknowledged. `onReadDone` then flips the label to
 * "Uploading…" for the part that is actually network-bound. */
async function uploadFile(
  file: PickedFile,
  onReadDone?: () => void,
): Promise<BulkAnalysisUploadResponse> {
  if (Platform.OS === "web") {
    const form = new FormData();
    form.append("file", file.webFile ?? file.uri, file.name);
    const res = await fetch(api.bulkAnalysisUploadUrl(), { method: "POST", body: form });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    return res.json();
  }

  await nextFrame();
  let base64: string;
  try {
    base64 = await FileSystem.readAsStringAsync(file.uri, {
      encoding: FileSystem.EncodingType.Base64,
    });
  } catch (e: any) {
    // Expo Go on Android cannot read back the very file expo-document-picker
    // just wrote into its own cache — every expo-file-system native call
    // fails with `java.io.IOException: Location ... isn't readable` for that
    // URI. It is an Expo Go sandboxing limitation, not something this app can
    // fix: the same code works in a dev build, in a production build, and on
    // web. Left unhandled it surfaces as a raw Java stack trace, which tells
    // the advisor nothing about what to do next.
    if (isExpoGoReadFailure(e)) {
      throw new Error(
        "Expo Go on Android can't open files picked from storage. This is a limitation "
        + "of Expo Go itself, not the file — the same upload works in a development or "
        + "production build of the app, and in the web version.",
      );
    }
    throw e;
  }
  onReadDone?.();
  return api.bulkAnalysisUploadBase64(file.name, base64);
}

export default function PortfolioReviewScreen() {
  const { colors, type } = useTheme();
  const styles = makeStyles(colors, type);

  const [file, setFile] = useState<PickedFile | null>(null);
  const [uploading, setUploading] = useState(false);
  /** Native only: the file is being read off disk into base64 before the
   *  request goes out. Shown separately from "uploading" because on a large
   *  workbook it is the longer of the two and would otherwise look like the
   *  app had stopped responding. */
  const [reading, setReading] = useState(false);
  const [upload, setUpload] = useState<BulkAnalysisUploadResponse | null>(null);
  const [starting, setStarting] = useState(false);
  const [status, setStatus] = useState<BulkAnalysisJobStatus | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Polling is a CHAINED TIMEOUT, not setInterval: on a slow connection a
  // fixed interval fires again while the previous /status request is still in
  // flight, so requests pile up, land out of order, and the progress bar
  // jumps backwards. Scheduling the next poll only after the previous one
  // settles makes that impossible and cuts the request count on exactly the
  // connections that can least afford it.
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollingRef = useRef(false);
  const mountedRef = useRef(true);

  const stopPolling = useCallback(() => {
    pollingRef.current = false;
    if (pollRef.current) {
      clearTimeout(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      stopPolling();
    };
  }, [stopPolling]);

  /** Polls until the job reaches a terminal state. One request in flight at a
   *  time; transient failures back off instead of hammering a backend that is
   *  already struggling (Render's free tier can be slow to wake). */
  const startPolling = useCallback((jobId: string) => {
    stopPolling();
    pollingRef.current = true;
    let delay = POLL_INTERVAL_MS;

    const tick = async () => {
      if (!pollingRef.current || !mountedRef.current) return;
      try {
        const s = await api.bulkAnalysisStatus(jobId);
        if (!pollingRef.current || !mountedRef.current) return;
        setStatus(s);
        delay = POLL_INTERVAL_MS;
        if (s.status === "completed" || s.status === "failed") {
          stopPolling();
          return;
        }
      } catch {
        // Transient network hiccup — keep polling, but slow down.
        delay = Math.min(delay * 2, MAX_POLL_INTERVAL_MS);
      }
      if (!pollingRef.current || !mountedRef.current) return;
      pollRef.current = setTimeout(tick, delay);
    };

    pollRef.current = setTimeout(tick, delay);
  }, [stopPolling]);

  function reset() {
    stopPolling();
    setFile(null);
    setUpload(null);
    setStatus(null);
    setError(null);
  }

  async function onPickFile() {
    setError(null);
    try {
      const result = await DocumentPicker.getDocumentAsync({
        type: EXCEL_MIME_TYPES,
        copyToCacheDirectory: true,
        multiple: false,
      });
      if (result.canceled || !result.assets?.length) return;
      const asset = result.assets[0];
      setFile({ uri: asset.uri, name: asset.name, mimeType: asset.mimeType, webFile: (asset as any).file });
      setUpload(null);
      setStatus(null);
    } catch (e: any) {
      setError(e?.message ?? "Couldn't open the file picker.");
    }
  }

  async function onUpload() {
    if (!file) return;
    setUploading(true);
    setReading(Platform.OS !== "web");
    setError(null);
    try {
      const res = await uploadFile(file, () => setReading(false));
      if (!mountedRef.current) return;
      setUpload(res);
    } catch (e: any) {
      if (!mountedRef.current) return;
      setError(e?.message ?? "Upload failed. Check the file and try again.");
    } finally {
      if (mountedRef.current) {
        setReading(false);
        setUploading(false);
      }
    }
  }

  async function onProcess() {
    if (!upload) return;
    setStarting(true);
    setError(null);
    try {
      await api.bulkAnalysisProcess(upload.job_id);
      // First status read right away, then poll — real progress only,
      // never a simulated bar.
      const first = await api.bulkAnalysisStatus(upload.job_id);
      setStatus(first);
      if (first.status === "processing" || first.status === "pending") {
        startPolling(upload.job_id);
      }
    } catch (e: any) {
      setError(e?.message ?? "Couldn't start processing.");
    } finally {
      setStarting(false);
    }
  }

  async function onDownload() {
    if (!upload || status?.status !== "completed") return;
    setDownloading(true);
    try {
      const url = api.bulkAnalysisDownloadUrl(upload.job_id);
      const fname = (status.filename || "funds").replace(/\.(xlsx|xls)$/i, "") + "_analyzed.xlsx";
      if (Platform.OS === "web") {
        window.open(url, "_blank");
        return;
      }
      const dest = FileSystem.cacheDirectory + fname;
      const { uri } = await FileSystem.downloadAsync(url, dest);
      if (await Sharing.isAvailableAsync()) await Sharing.shareAsync(uri);
      else Alert.alert("Saved", `File saved to ${uri}`);
    } catch (e: any) {
      Alert.alert("Download failed", e?.message ?? "Unknown error");
    } finally {
      setDownloading(false);
    }
  }

  const progressPct = status && status.total > 0 ? Math.min(1, status.processed / status.total) : 0;
  const isDone = status?.status === "completed";
  const isFailed = status?.status === "failed";
  const isRunning = status?.status === "processing" || status?.status === "pending";

  return (
    <SafeAreaView style={styles.safe} edges={["bottom"]}>
      <ScrollView contentContainerStyle={{ padding: space.md, paddingBottom: space.xl * 2 }}>
        <View style={styles.headerRow}>
          <ListChecks size={20} color={colors.azure} />
          <Text style={styles.title}>Portfolio Review</Text>
        </View>
        <Text style={styles.subtitle}>
          Upload a client&apos;s portfolio Excel — get back the same sheet with FQS,
          Success%, Median WM, XIRR, P10 WM, CV and Benchmark XIRR filled in, every
          column of yours kept, formatted and branded ready to send to the client.
        </Text>

        {/* ── Step 1: pick + upload ── */}
        {!upload && (
          <View style={styles.card}>
            <Pressable
              onPress={onPickFile}
              style={({ pressed }) => [styles.dropzone, { opacity: pressed ? 0.8 : 1 }]}
            >
              <FileSpreadsheet size={28} color={colors.azure} />
              <Text style={styles.dropzoneTitle}>
                {file ? file.name : "Choose Excel File"}
              </Text>
              <Text style={styles.dropzoneSub}>Supported: .xlsx, .xls</Text>
            </Pressable>

            {file && (
              <Pressable
                onPress={onUpload}
                disabled={uploading}
                style={({ pressed }) => [
                  styles.primaryBtn,
                  { backgroundColor: colors.azure, opacity: pressed || uploading ? 0.85 : 1, marginTop: space.md },
                ]}
              >
                {uploading ? (
                  <>
                    <ActivityIndicator color="#fff" />
                    <Text style={styles.primaryBtnText}>
                      {reading ? "Reading file…" : "Uploading…"}
                    </Text>
                  </>
                ) : (
                  <>
                    <Upload size={16} color="#fff" />
                    <Text style={styles.primaryBtnText}>Upload &amp; Preview</Text>
                  </>
                )}
              </Pressable>
            )}
          </View>
        )}

        {error && (
          <View style={styles.errorBox}>
            <AlertTriangle size={14} color={colors.ruby} />
            <Text style={[styles.errorText, { color: colors.ruby }]}>{error}</Text>
          </View>
        )}

        {/* ── Step 2: preview + process trigger ── */}
        {upload && !status && (
          <View style={styles.card}>
            <Text style={styles.cardTitle}>File: {upload.filename}</Text>
            <Text style={styles.cardMeta}>Total Funds: {upload.total_funds}</Text>

            <Text style={[styles.label, { marginTop: space.md }]}>DETECTED FUNDS</Text>
            <View style={styles.previewList}>
              {upload.detected_funds.map((name, i) => (
                <Text key={i} style={styles.previewItem} numberOfLines={1}>
                  {i + 1}. {name}
                </Text>
              ))}
              {upload.total_funds > upload.detected_funds.length && (
                <Text style={styles.previewMore}>
                  + {upload.total_funds - upload.detected_funds.length} more
                </Text>
              )}
            </View>

            <View style={styles.rowGap}>
              <Pressable
                onPress={onProcess}
                disabled={starting}
                style={({ pressed }) => [
                  styles.primaryBtn,
                  { backgroundColor: colors.azure, opacity: pressed || starting ? 0.85 : 1, flex: 1 },
                ]}
              >
                {starting ? <ActivityIndicator color="#fff" /> : <Text style={styles.primaryBtnText}>Process Excel</Text>}
              </Pressable>
              <Pressable onPress={reset} style={styles.secondaryBtn}>
                <RotateCcw size={14} color={colors.textSec} />
              </Pressable>
            </View>
          </View>
        )}

        {/* ── Step 3: progress / results ── */}
        {status && (
          <View style={styles.card}>
            <Text style={styles.cardTitle}>{isDone ? "Analysis Complete" : isFailed ? "Processing Failed" : "Processing Funds…"}</Text>

            <View style={styles.progressTrack}>
              <View
                style={[
                  styles.progressFill,
                  { width: `${Math.round(progressPct * 100)}%`, backgroundColor: isFailed ? colors.ruby : colors.azure },
                ]}
              />
            </View>
            <Text style={styles.progressLabel}>
              {status.processed} / {status.total} {isRunning ? `(${Math.round(progressPct * 100)}%)` : ""}
            </Text>

            <View style={styles.summaryGrid}>
              <SummaryStat label="Total" value={status.total} color={colors.textPri} colors={colors} />
              <SummaryStat label="Successful" value={status.matched} color={colors.emerald} colors={colors} />
              <SummaryStat label="Not Found" value={status.not_found} color={colors.amber} colors={colors} />
              <SummaryStat label="Errors" value={status.errors} color={colors.ruby} colors={colors} />
            </View>

            {!!status.not_found_funds?.length && (
              <View style={{ marginTop: space.sm }}>
                <Text style={styles.label}>NOT FOUND — ADD THESE MANUALLY</Text>
                <View style={styles.previewList}>
                  {status.not_found_funds.map((f) => (
                    <Text key={f.row} style={styles.previewItem} numberOfLines={1}>
                      Row {f.row}: {f.label}
                    </Text>
                  ))}
                </View>
              </View>
            )}

            {isDone && (
              <Pressable
                onPress={onDownload}
                disabled={downloading}
                style={({ pressed }) => [
                  styles.primaryBtn,
                  { backgroundColor: colors.emerald, opacity: pressed || downloading ? 0.85 : 1, marginTop: space.md },
                ]}
              >
                {downloading ? (
                  <ActivityIndicator color="#fff" />
                ) : (
                  <>
                    <Download size={16} color="#fff" />
                    <Text style={styles.primaryBtnText}>Download Processed Excel</Text>
                  </>
                )}
              </Pressable>
            )}

            {isFailed && (
              <View style={styles.errorBox}>
                <XCircle size={14} color={colors.ruby} />
                <Text style={[styles.errorText, { color: colors.ruby }]}>
                  Something went wrong while processing this file. Try again, or check the file&apos;s
                  format if this keeps happening.
                </Text>
              </View>
            )}

            {(isDone || isFailed) && (
              <Pressable onPress={reset} style={[styles.secondaryBtnWide, { marginTop: space.sm }]}>
                <RotateCcw size={14} color={colors.textSec} />
                <Text style={{ color: colors.textSec, fontWeight: "600", fontSize: 13 }}>Start Over</Text>
              </Pressable>
            )}
          </View>
        )}
      </ScrollView>
    </SafeAreaView>
  );
}

/** Memoised: the status poll updates state every ~1.5s while a job runs, and
 *  without this every tick re-renders all four tiles even when their numbers
 *  haven't moved. Props are plain scalars, so the default shallow compare is
 *  exactly right. */
const SummaryStat = memo(function SummaryStat({
  label, value, color, colors,
}: { label: string; value: number; color: string; colors: ThemeColors }) {
  return (
    <View style={{ flex: 1, alignItems: "center" }}>
      <Text style={{ fontSize: 20, fontWeight: "800", color }}>{value}</Text>
      <Text style={{ fontSize: 10, fontWeight: "700", color: colors.textTer, letterSpacing: 0.6, marginTop: 2 }}>
        {label.toUpperCase()}
      </Text>
    </View>
  );
});

function makeStyles(colors: ThemeColors, type: ThemeType) {
  return StyleSheet.create({
    safe: { flex: 1, backgroundColor: colors.bg },
    headerRow: { flexDirection: "row", alignItems: "center", gap: 8, marginBottom: 4 },
    title: { ...type.h2 },
    subtitle: { ...type.bodySm, marginTop: 6, marginBottom: space.md },

    card: {
      backgroundColor: colors.bgCard,
      borderRadius: radii.lg,
      borderWidth: StyleSheet.hairlineWidth,
      borderColor: colors.border,
      padding: space.md,
      marginTop: space.sm,
    },

    dropzone: {
      alignItems: "center",
      justifyContent: "center",
      gap: 6,
      paddingVertical: space.xl,
      borderRadius: radii.md,
      borderWidth: 1.5,
      borderStyle: "dashed",
      borderColor: colors.border,
      backgroundColor: colors.bgElev,
    },
    dropzoneTitle: { fontSize: 14, fontWeight: "700", color: colors.textPri, marginTop: 4 },
    dropzoneSub: { fontSize: 11, color: colors.textTer },

    primaryBtn: {
      flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 8,
      paddingVertical: 12, borderRadius: radii.md,
    },
    primaryBtnText: { color: "#fff", fontWeight: "700", fontSize: 14 },
    secondaryBtn: {
      alignItems: "center", justifyContent: "center", paddingHorizontal: 14,
      borderRadius: radii.md, borderWidth: 1, borderColor: colors.border,
    },
    secondaryBtnWide: {
      flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 6,
      paddingVertical: 10, borderRadius: radii.md, borderWidth: 1, borderColor: colors.border,
    },
    rowGap: { flexDirection: "row", gap: space.sm, marginTop: space.md },

    cardTitle: { fontSize: 15, fontWeight: "700", color: colors.textPri },
    cardMeta: { fontSize: 12, color: colors.textSec, marginTop: 2 },
    label: { fontSize: 10.5, fontWeight: "700", color: colors.textTer, letterSpacing: 0.8 },

    previewList: { marginTop: 6, gap: 3 },
    previewItem: { fontSize: 12.5, color: colors.textSec },
    previewMore: { fontSize: 12, color: colors.textTer, fontStyle: "italic", marginTop: 2 },

    progressTrack: {
      height: 8, borderRadius: 4, backgroundColor: colors.bgElev, overflow: "hidden", marginTop: space.sm,
    },
    progressFill: { height: "100%", borderRadius: 4 },
    progressLabel: { fontSize: 11, color: colors.textTer, marginTop: 6 },

    summaryGrid: { flexDirection: "row", marginTop: space.md, paddingVertical: space.sm },

    errorBox: {
      flexDirection: "row", alignItems: "flex-start", gap: 6,
      backgroundColor: colors.ruby + "15", borderRadius: radii.sm,
      padding: 10, marginTop: space.sm,
    },
    errorText: { flex: 1, fontSize: 12.5, lineHeight: 17 },
  });
}
