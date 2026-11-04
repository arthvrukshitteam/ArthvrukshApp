/**
 * Portfolio Holding Review — drawer screen.
 *
 * Previous portfolio proposal (PDF) → automatic extraction → advisor verifies →
 * current FQS/WM/XIRR/CV from the SAME engine the proposal used → Previous vs
 * Current comparison → observations (auto from the existing FQS thresholds,
 * editable) → preview → Holding Review Report PDF.
 *
 * Pure orchestration UI: every number comes from /api/portfolio-review/*
 * (backend/holding_review_routes.py). No scoring or FQS rules live here.
 * The review is persisted from the first upload, so any step can be resumed
 * from "Recent reviews" and nothing has to be re-entered.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  View, Text, StyleSheet, ScrollView, Pressable, ActivityIndicator, Alert, Platform, TextInput,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import * as DocumentPicker from "expo-document-picker";
import * as FileSystem from "expo-file-system/legacy";
import * as Sharing from "expo-sharing";
import {
  AlertTriangle, Download, FilePlus2, FileText, Lock, Plus, RefreshCw, Trash2, Upload,
} from "lucide-react-native";
import {
  api, type HoldingReview, type HoldingReviewPatch, type HoldingReviewSummary, type ReviewFund,
} from "@/src/api";
import { useTheme } from "@/src/ThemeProvider";
import { radii, space, type ThemeColors } from "@/src/theme";
import { DrawerMenuButton } from "@/src/components/DrawerMenuButton";
import { FundSearchModal } from "@/src/components/FundSearchModal";
import { countLabel } from "@/src/utils/format";
import {
  ComparisonCard, FundEditCard, LabeledInput, PreviewTable, errMsg, fmtMetric,
} from "@/src/components/HoldingReviewParts";

const STEPS = ["Upload", "Verify", "Current data", "Compare & notes", "Preview"] as const;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

type PickedFile = { uri: string; name: string; webFile?: any };

function confirmAsk(title: string, msg: string): Promise<boolean> {
  if (Platform.OS === "web") return Promise.resolve(window.confirm(`${title}\n\n${msg}`));
  return new Promise((resolve) =>
    Alert.alert(title, msg, [
      { text: "Cancel", style: "cancel", onPress: () => resolve(false) },
      { text: "Continue", onPress: () => resolve(true) },
    ]),
  );
}

async function readBase64(file: PickedFile): Promise<string> {
  if (Platform.OS === "web") {
    const blob: Blob = file.webFile ?? (await (await fetch(file.uri)).blob());
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(String(r.result).split(",")[1] ?? "");
      r.onerror = () => reject(new Error("Couldn't read the selected file."));
      r.readAsDataURL(blob);
    });
  }
  return FileSystem.readAsStringAsync(file.uri, { encoding: FileSystem.EncodingType.Base64 });
}

const numOrNull = (t: string): number | null => (t.trim() === "" || !Number.isFinite(Number(t)) ? null : Number(t));
const str = (v: number | null | undefined) => (v == null ? "" : String(v));

export default function HoldingReviewScreen() {
  const { colors } = useTheme();
  const s = useMemo(() => makeStyles(colors), [colors]);

  const [step, setStep] = useState(0);
  const [review, setReview] = useState<HoldingReview | null>(null);
  const [recent, setRecent] = useState<HoldingReviewSummary[]>([]);
  const [recentLoading, setRecentLoading] = useState(true);
  const [file, setFile] = useState<PickedFile | null>(null);
  const [busy, setBusy] = useState<string | null>(null);   // label of the running action
  const [error, setError] = useState<string | null>(null);
  const [pickerFor, setPickerFor] = useState<string | null>(null);   // fid being mapped, "" = adding
  const [dirty, setDirty] = useState(false);
  const mounted = useRef(true);
  useEffect(() => () => { mounted.current = false; }, []);

  const locked = review?.status === "finalized";

  const loadRecent = useCallback(async () => {
    try { setRecent((await api.holdingReviewList()).results); } catch { /* list is a convenience */ }
    finally { if (mounted.current) setRecentLoading(false); }
  }, []);
  useEffect(() => { loadRecent(); }, [loadRecent]);

  /** Local edit → marks the review dirty; saved on the next step change/action. */
  const edit = useCallback((fn: (r: HoldingReview) => HoldingReview) => {
    setReview((r) => (r ? fn(r) : r));
    setDirty(true);
  }, []);

  const toPatch = (r: HoldingReview, confirm = false): HoldingReviewPatch => ({
    client: { name: r.client.name ?? "", pan: r.client.pan ?? "", phone: r.client.phone ?? "" },
    previous_review_date: r.previous_review_date || undefined,
    current_review_date: r.current_review_date || undefined,
    plan: r.plan.goal_years ? { goal_years: r.plan.goal_years } : undefined,
    market: { nifty: r.market.nifty, aum: r.market.aum },
    quarters_completed: r.quarters_completed ?? undefined,
    funds: r.funds.map((f) => ({
      fid: f.fid, scheme_code: f.scheme_code, rolling_year: f.rolling_year,
      investment_type: f.investment_type, previous: f.previous,
    })),
    observations: r.observations.map((o) => ({ id: o.id, text: o.text, included: o.included ?? true })),
    confirm_all: confirm || undefined,
  });

  /** Persist local edits; returns the server's copy (or null on failure). */
  const save = useCallback(async (confirm = false): Promise<HoldingReview | null> => {
    if (!review || locked) return review;
    if (!dirty && !confirm) return review;
    try {
      const res = await api.holdingReviewSave(review.review_id, toPatch(review, confirm));
      if (mounted.current) { setReview(res); setDirty(false); }
      return res;
    } catch (e) {
      if (mounted.current) setError(errMsg(e));
      return null;
    }
  }, [review, dirty, locked]);

  const run = useCallback(async (label: string, fn: () => Promise<void>) => {
    setBusy(label); setError(null);
    try { await fn(); } catch (e) { if (mounted.current) setError(errMsg(e)); }
    finally { if (mounted.current) setBusy(null); }
  }, []);

  // ── step 0: upload / open ───────────────────────────────────────────────────
  async function pickFile() {
    setError(null);
    try {
      const r = await DocumentPicker.getDocumentAsync({ type: "application/pdf", copyToCacheDirectory: true, multiple: false });
      if (r.canceled || !r.assets?.length) return;
      const a = r.assets[0];
      setFile({ uri: a.uri, name: a.name, webFile: (a as any).file });
    } catch (e) { setError(errMsg(e)); }
  }

  const upload = () => file && run("Reading the PDF…", async () => {
    const b64 = await readBase64(file);
    const res = await api.holdingReviewUpload(file.name, b64);
    setReview(res); setDirty(false); setFile(null); setStep(1); loadRecent();
  });

  const openReview = (id: string) => run("Opening…", async () => {
    const r = await api.holdingReviewGet(id);
    setReview(r); setDirty(false);
    setStep(r.status === "finalized" ? 4 : r.current_fetched_at ? 3 : 1);
  });

  const newReview = () => { setReview(null); setFile(null); setStep(0); setError(null); setDirty(false); loadRecent(); };

  // ── step transitions ────────────────────────────────────────────────────────
  const goVerifyNext = () => run("Saving…", async () => {
    const r = await save(true);
    if (r) setStep(2);
  });

  const fetchCurrent = () => run("Fetching current FQS data…", async () => {
    const saved = await save();
    if (!saved) return;
    setReview(await api.holdingReviewFetchCurrent(saved.review_id));
  });

  const generateObs = () => run("Generating observations…", async () => {
    const saved = await save();
    if (!saved) return;
    setReview(await api.holdingReviewObservations(saved.review_id));
  });

  const finalize = () => run("Finalizing…", async () => {
    if (!(await confirmAsk("Finalize report?", "A finalized review cannot be edited. You can create a revised copy later."))) return;
    const saved = await save();
    if (!saved) return;
    setReview(await api.holdingReviewFinalize(saved.review_id)); loadRecent();
  });

  const revise = () => run("Creating revision…", async () => {
    if (!review) return;
    const r = await api.holdingReviewRevise(review.review_id);
    setReview(r); setDirty(false); setStep(1); loadRecent();
  });

  const downloadPdf = () => run("Preparing PDF…", async () => {
    const saved = await save();
    if (!saved) return;
    const url = api.holdingReviewPdfUrl(saved.review_id);
    if (Platform.OS === "web") { window.open(url, "_blank"); return; }
    const probe = await FileSystem.downloadAsync(url, FileSystem.cacheDirectory + `holding_review_${saved.review_id}.pdf`);
    if (probe.status && probe.status >= 400) throw new Error(`HTTP ${probe.status}: ${JSON.stringify({ detail: "The report could not be generated — fix the issues listed above." })}`);
    if (await Sharing.isAvailableAsync()) await Sharing.shareAsync(probe.uri);
    else Alert.alert("Saved", probe.uri);
  });

  // ── fund edits ──────────────────────────────────────────────────────────────
  const patchFund = (fid: string, p: Partial<ReviewFund>) =>
    edit((r) => ({ ...r, funds: r.funds.map((f) => (f.fid === fid ? { ...f, ...p } : f)) }));

  const mapScheme = (fid: string, code: number, name: string) => {
    if (fid === "") {   // adding a new (COB / External) fund
      edit((r) => ({
        ...r,
        funds: [...r.funds, {
          fid: `new-${Date.now()}`, fund_name_pdf: null, scheme_code: code, scheme_name: name, category: null,
          rolling_year: r.plan.goal_years ?? 10, investment_type: "COB", allocation: null,
          match: { method: "advisor_selected", confidence: "high", candidates: [] },
          previous: { fqs: null, wm_p10: null, wm_med: null, irr_p10: null, irr_med: null, cv: null },
          current: null, current_status: null, current_error: null, changes: {}, status: "verified", issues: [],
        }],
      }));
    } else {
      patchFund(fid, { scheme_code: code, scheme_name: name, current: null, current_status: null,
        match: { method: "advisor_selected", confidence: "high", candidates: [] } });
    }
  };

  const dateBad = (v: string | null) => !!v && !DATE_RE.test(v);

  // ── render ──────────────────────────────────────────────────────────────────
  return (
    <SafeAreaView style={s.root} edges={["top", "bottom"]}>
      <ScrollView contentContainerStyle={s.scroll} keyboardShouldPersistTaps="handled">
        <View style={s.titleRow}>
          <DrawerMenuButton />
          <View style={{ flex: 1 }}>
            <Text style={s.h1}>Portfolio Holding Review</Text>
            <Text style={s.sub}>Previous proposal vs current FQS — one report</Text>
          </View>
          {review ? (
            <Pressable onPress={newReview} style={s.ghost}><Text style={s.ghostTxt}>New review</Text></Pressable>
          ) : null}
        </View>

        {review ? <Stepper step={step} setStep={(i) => run("Saving…", async () => { if (await save()) setStep(i); })} colors={colors} /> : null}

        {locked ? (
          <View style={[s.banner, { borderColor: colors.emerald }]}>
            <Lock size={14} color={colors.emerald} />
            <Text style={[s.bannerTxt, { flex: 1 }]}>Finalized — read only.</Text>
            <Pressable onPress={revise}><Text style={{ color: colors.azure, fontWeight: "700" }}>Revise</Text></Pressable>
          </View>
        ) : null}

        {error ? (
          <View style={[s.banner, { borderColor: colors.ruby }]}>
            <AlertTriangle size={14} color={colors.ruby} />
            <Text style={[s.bannerTxt, { flex: 1, color: colors.ruby }]}>{error}</Text>
          </View>
        ) : null}
        {busy ? (
          <View style={s.busy}><ActivityIndicator color={colors.azure} /><Text style={{ color: colors.textSec }}>{busy}</Text></View>
        ) : null}

        {/* ── STEP 0 ───────────────────────────────────────────────────────── */}
        {step === 0 && !review ? (
          <>
            <View style={s.card}>
              <Text style={s.h2}>1 · Upload the previous portfolio proposal</Text>
              <Text style={s.sub}>
                The PDF generated by ArthVruksh (or a previous Holding Review report). Client, dates, funds and
                FQS figures are read automatically — you only verify them.
              </Text>
              <Pressable onPress={pickFile} style={[s.drop, { borderColor: colors.borderStrong }]} accessibilityRole="button">
                <FileText size={26} color={colors.textSec} />
                <Text style={{ color: colors.textPri, fontWeight: "600" }}>{file ? file.name : "Browse for a PDF"}</Text>
                <Text style={s.sub}>PDF only · up to 10 MB</Text>
              </Pressable>
              <Pressable disabled={!file || !!busy} onPress={upload} style={[s.primary, (!file || !!busy) && { opacity: 0.5 }]}>
                <Upload size={16} color="#fff" /><Text style={s.primaryTxt}>Upload & extract</Text>
              </Pressable>
            </View>

            <Text style={s.h2}>Recent reviews</Text>
            {recentLoading
              ? <View style={{ paddingVertical: space.md, alignItems: "center" }}><ActivityIndicator color={colors.azure} /></View>
              : recent.length === 0 ? <Text style={s.sub}>No reviews yet.</Text> : null}
            {recent.map((r) => (
              <Pressable key={r.review_id} onPress={() => openReview(r.review_id)} style={s.card}>
                <View style={s.rowBetween}>
                  <Text style={{ color: colors.textPri, fontWeight: "700" }}>{r.client_name ?? "Unnamed client"}</Text>
                  <Text style={{ color: r.status === "finalized" ? colors.emerald : colors.amber, fontSize: 12, fontWeight: "600" }}>
                    {r.status === "finalized" ? "Finalized" : "Draft"}{r.version > 1 ? ` · v${r.version}` : ""}
                  </Text>
                </View>
                <Text style={s.sub}>
                  {r.previous_review_date ?? "–"} → {r.current_review_date ?? "–"} · {countLabel(r.fund_count, "fund")}{r.pan_masked ? ` · ${r.pan_masked}` : ""}
                </Text>
              </Pressable>
            ))}
          </>
        ) : null}

        {/* ── STEP 1: verify ───────────────────────────────────────────────── */}
        {review && step === 1 ? (
          <>
            {(() => {
              const total = review.funds.length;
              const matched = review.funds.filter((f) => f.scheme_code != null).length;
              const pending = total - matched;
              return (
                <View style={[s.banner, { borderColor: total && !pending ? colors.emerald : colors.amber }]}>
                  <Text style={[s.bannerTxt, { fontWeight: "700" }]}>
                    {total === 0
                      ? "No funds were found in this file — add them below."
                      : `${countLabel(matched, "fund")} successfully imported`
                        + (pending ? ` · ${countLabel(pending, "fund")} still ${pending === 1 ? "needs" : "need"} a scheme match` : "")
                        + "."}
                  </Text>
                </View>
              );
            })()}
            {review.parse_warnings.map((w) => (
              <View key={w} style={[s.banner, { borderColor: colors.amber }]}><Text style={s.bannerTxt}>{w}</Text></View>
            ))}
            <Text style={s.sub}>
              Read from “{review.source.filename}” ({review.source.parse_method === "embedded_reference" ? "exact data from the saved proposal" : "extracted from the PDF text"}).
              Correct anything that is wrong or missing.
            </Text>

            <View style={s.card}>
              <Text style={s.h2}>Client</Text>
              <View style={s.row}>
                <LabeledInput label="Client name" value={review.client.name ?? ""} editable={!locked}
                  flag={review.review_flags.includes("client.name")}
                  onChange={(v) => edit((r) => ({ ...r, client: { ...r.client, name: v } }))} />
                <LabeledInput label="PAN" value={review.client.pan ?? ""} editable={!locked}
                  onChange={(v) => edit((r) => ({ ...r, client: { ...r.client, pan: v.toUpperCase() } }))} />
              </View>
              <View style={s.row}>
                <LabeledInput label="Last / proposal review (YYYY-MM-DD)" value={review.previous_review_date ?? ""} editable={!locked}
                  flag={review.review_flags.includes("proposal_date") || dateBad(review.previous_review_date)}
                  onChange={(v) => edit((r) => ({ ...r, previous_review_date: v }))} />
                <LabeledInput label="Current review (YYYY-MM-DD)" value={review.current_review_date ?? ""} editable={!locked}
                  flag={dateBad(review.current_review_date)}
                  onChange={(v) => edit((r) => ({ ...r, current_review_date: v }))} />
              </View>
              <View style={s.row}>
                <LabeledInput label="Investment goal (years)" value={str(review.plan.goal_years)} keyboardType="numeric" editable={!locked}
                  flag={review.review_flags.includes("plan.horizon")}
                  onChange={(v) => edit((r) => ({ ...r, plan: { ...r.plan, goal_years: numOrNull(v) } }))} />
                <LabeledInput label="Quarters reviewed" value={str(review.quarters_completed)} keyboardType="numeric" editable={!locked}
                  onChange={(v) => edit((r) => ({ ...r, quarters_completed: numOrNull(v) }))} />
              </View>
            </View>

            <View style={s.card}>
              <Text style={s.h2}>Market / portfolio performance</Text>
              <Text style={s.sub}>Entered by you — the system holds no index level or client holdings value.</Text>
              {(["nifty", "aum"] as const).map((k) => (
                <View key={k} style={s.row}>
                  <LabeledInput label={`${k === "nifty" ? "Nifty Index" : "Client MF AUM / Invested"} · previous`}
                    value={str(review.market[k].previous)} keyboardType="numeric" editable={!locked}
                    onChange={(v) => edit((r) => ({ ...r, market: { ...r.market, [k]: { ...r.market[k], previous: numOrNull(v) } } }))} />
                  <LabeledInput label="current" value={str(review.market[k].current)} keyboardType="numeric" editable={!locked}
                    onChange={(v) => edit((r) => ({ ...r, market: { ...r.market, [k]: { ...r.market[k], current: numOrNull(v) } } }))} />
                </View>
              ))}
            </View>

            <Text style={s.h2}>Funds detected ({review.funds.length})</Text>
            {review.funds.map((f) => (
              <FundEditCard key={f.fid} fund={f} editable={!locked}
                onChange={(p) => patchFund(f.fid, p)}
                onRemove={() => edit((r) => ({ ...r, funds: r.funds.filter((x) => x.fid !== f.fid) }))}
                onPickScheme={(code) => {
                  if (code == null) return setPickerFor(f.fid);
                  const c = f.match.candidates.find((x) => x.scheme_code === code);
                  if (c) mapScheme(f.fid, c.scheme_code, c.scheme_name);
                }} />
            ))}
            {!locked ? (
              <Pressable onPress={() => setPickerFor("")} style={s.ghost}>
                <Plus size={14} color={colors.azure} /><Text style={s.ghostTxt}>Add a COB / External fund</Text>
              </Pressable>
            ) : null}

            {review.validation.errors.map((e) => <Text key={e} style={s.err}>• {e}</Text>)}
            <Pressable disabled={!!busy} onPress={locked ? () => setStep(2) : goVerifyNext} style={[s.primary, !!busy && { opacity: 0.5 }]}>
              <Text style={s.primaryTxt}>{locked ? "Next" : "Confirm & continue"}</Text>
            </Pressable>
          </>
        ) : null}

        {/* ── STEP 2: current data ─────────────────────────────────────────── */}
        {review && step === 2 ? (
          <>
            <View style={s.card}>
              <Text style={s.h2}>3 · Current review</Text>
              <LabeledInput label="Current review date (YYYY-MM-DD)" value={review.current_review_date ?? ""} editable={!locked}
                flag={dateBad(review.current_review_date)}
                onChange={(v) => edit((r) => ({ ...r, current_review_date: v }))} />
              <Text style={s.sub}>
                Current figures use each fund’s latest NAV history and the same FQS model as the proposal
                {review.data_as_of ? ` (data up to ${review.data_as_of})` : ""}.
              </Text>
              {!locked ? (
                <Pressable disabled={!!busy} onPress={fetchCurrent} style={[s.primary, !!busy && { opacity: 0.5 }]}>
                  <RefreshCw size={16} color="#fff" />
                  <Text style={s.primaryTxt}>{review.current_fetched_at ? "Re-fetch current data" : "Fetch current data"}</Text>
                </Pressable>
              ) : null}
            </View>
            {review.funds.map((f) => (
              <View key={f.fid} style={[s.card, s.rowBetween]}>
                <Text style={{ flex: 1, color: colors.textPri, fontWeight: "600" }}>{f.scheme_name ?? f.fund_name_pdf}</Text>
                <Text style={{ color: f.current_status === "ok" ? colors.emerald : f.current_status ? colors.ruby : colors.textTer, fontSize: 12 }}>
                  {f.current_status === "ok" ? `FQS ${fmtMetric("fqs", f.current?.fqs)}`
                    : f.current_status === "unavailable" ? "Not enough history"
                    : f.current_status === "error" ? (f.current_error ?? "Failed") : "Not fetched"}
                </Text>
              </View>
            ))}
            {review.validation.warnings.map((w) => <Text key={w} style={s.warn}>• {w}</Text>)}
            {review.validation.errors.map((e) => <Text key={e} style={s.err}>• {e}</Text>)}
            <Pressable disabled={!review.current_fetched_at || !!busy} onPress={() => setStep(3)}
              style={[s.primary, (!review.current_fetched_at || !!busy) && { opacity: 0.5 }]}>
              <Text style={s.primaryTxt}>Compare previous vs current</Text>
            </Pressable>
          </>
        ) : null}

        {/* ── STEP 3: compare + observations ───────────────────────────────── */}
        {review && step === 3 ? (
          <>
            <Text style={s.h2}>Previous → Current</Text>
            {review.funds.map((f) => <ComparisonCard key={f.fid} fund={f} />)}

            <View style={s.rowBetween}>
              <Text style={s.h2}>ArthVruksh observations</Text>
              {!locked ? (
                <Pressable onPress={generateObs} disabled={!!busy} style={s.ghost}>
                  <Text style={s.ghostTxt}>{review.observations.some((o) => o.source === "auto") ? "Regenerate auto" : "Auto-generate"}</Text>
                </Pressable>
              ) : null}
            </View>
            <Text style={s.sub}>
              Suggested from the existing FQS rules (below 75 review/redeem · 75–85 acceptable). Edit, remove or add your own — regenerating never touches your own notes.
            </Text>
            {review.observations.map((o, i) => (
              <View key={o.id} style={s.card}>
                <TextInput
                  multiline editable={!locked} value={o.text}
                  onChangeText={(t) => edit((r) => ({ ...r, observations: r.observations.map((x) => (x.id === o.id ? { ...x, text: t } : x)) }))}
                  style={{ color: colors.textPri, fontSize: 14, minHeight: 44 }}
                />
                <View style={s.rowBetween}>
                  <Text style={{ color: colors.textTer, fontSize: 11 }}>{i + 1} · {o.source === "auto" ? "auto-generated" : "advisor note"}</Text>
                  {!locked ? (
                    <Pressable onPress={() => edit((r) => ({ ...r, observations: r.observations.filter((x) => x.id !== o.id) }))} hitSlop={8}>
                      <Trash2 size={15} color={colors.textTer} />
                    </Pressable>
                  ) : null}
                </View>
              </View>
            ))}
            {!locked ? (
              <Pressable
                onPress={() => edit((r) => ({ ...r, observations: [...r.observations, { id: `local-${Date.now()}`, text: "", source: "manual", included: true }] }))}
                style={s.ghost}><FilePlus2 size={14} color={colors.azure} /><Text style={s.ghostTxt}>Add observation</Text></Pressable>
            ) : null}
            <Pressable onPress={() => run("Saving…", async () => { if (await save()) setStep(4); })} style={s.primary}>
              <Text style={s.primaryTxt}>Preview report</Text>
            </Pressable>
          </>
        ) : null}

        {/* ── STEP 4: preview ──────────────────────────────────────────────── */}
        {review && step === 4 ? (
          <>
            <View style={s.card}>
              <Text style={s.h2}>PORTFOLIO HOLDING REVIEW REPORT</Text>
              <Text style={s.sub}>ArthVruksh MFDGuru LLP · Your Goals, Our Guidance</Text>
              <View style={[s.row, { marginTop: 8 }]}>
                <Text style={s.kv}>Client: <Text style={s.kvv}>{review.client.name}</Text></Text>
                <Text style={s.kv}>PAN: <Text style={s.kvv}>{review.client.pan || "–"}</Text></Text>
              </View>
              <View style={s.row}>
                <Text style={s.kv}>Last / proposal review: <Text style={s.kvv}>{review.previous_review_date}</Text></Text>
                <Text style={s.kv}>Current review: <Text style={s.kvv}>{review.current_review_date}</Text></Text>
              </View>
              <View style={{ marginTop: 8 }}>
                {(["nifty", "aum"] as const).map((k) => {
                  const m = review.market_summary[k];
                  return (
                    <Text key={k} style={s.kv}>
                      {k === "nifty" ? "Nifty Index" : "Client MF AUM / Invested"}:{" "}
                      <Text style={s.kvv}>{m.previous ?? "–"} → {m.current ?? "–"}{m.change_pct != null ? `  (${m.change_pct > 0 ? "+" : ""}${m.change_pct.toFixed(2)}%)` : ""}</Text>
                    </Text>
                  );
                })}
                <Text style={s.kv}>Type of investment: <Text style={s.kvv}>{review.type_of_investment || "–"}</Text> · Goal: <Text style={s.kvv}>{review.plan.goal_years ?? "–"} years</Text> · Quarters: <Text style={s.kvv}>{review.quarters_completed ?? "–"}</Text></Text>
              </View>
            </View>
            <PreviewTable funds={review.funds} />
            <Text style={[s.h2, { marginTop: space.md }]}>Observations</Text>
            {review.observations.filter((o) => o.text.trim()).length === 0 ? <Text style={s.sub}>None.</Text> : null}
            {review.observations.filter((o) => o.text.trim()).map((o, i) => (
              <Text key={o.id} style={{ color: colors.textPri, fontSize: 13, marginBottom: 4 }}>{i + 1}) {o.text}</Text>
            ))}
            <Text style={[s.sub, { marginTop: 8 }]}>The standard ArthVruksh disclaimer is added to the PDF automatically.</Text>

            {review.validation.errors.map((e) => <Text key={e} style={s.err}>• {e}</Text>)}
            {review.validation.warnings.map((w) => <Text key={w} style={s.warn}>• {w}</Text>)}

            <Pressable disabled={!!busy || review.validation.errors.length > 0} onPress={downloadPdf}
              style={[s.primary, (!!busy || review.validation.errors.length > 0) && { opacity: 0.5 }]}>
              <Download size={16} color="#fff" /><Text style={s.primaryTxt}>Download PDF</Text>
            </Pressable>
            {!locked ? (
              <Pressable disabled={!!busy} onPress={finalize} style={s.ghost}>
                <Lock size={14} color={colors.azure} /><Text style={s.ghostTxt}>Finalize review</Text>
              </Pressable>
            ) : null}
          </>
        ) : null}
      </ScrollView>

      <FundSearchModal
        visible={pickerFor !== null}
        title={pickerFor ? "Choose the matching fund" : "Add fund"}
        onClose={() => setPickerFor(null)}
        excludeCodes={review?.funds.map((f) => f.scheme_code).filter((c): c is number => c != null) ?? []}
        onPick={(f) => { mapScheme(pickerFor ?? "", f.scheme_code, f.scheme_name); setPickerFor(null); }}
      />
    </SafeAreaView>
  );
}

function Stepper({ step, setStep, colors }: { step: number; setStep: (i: number) => void; colors: ThemeColors }) {
  return (
    <ScrollView horizontal showsHorizontalScrollIndicator={false} style={{ marginBottom: space.sm }}>
      {STEPS.map((label, i) => {
        const on = i === step;
        return (
          <Pressable key={label} onPress={() => i > 0 && setStep(i)} disabled={i === 0}
            style={{ paddingVertical: 8, paddingHorizontal: 12, marginRight: 6, borderRadius: radii.pill, borderWidth: 1,
              borderColor: on ? colors.azure : colors.border, backgroundColor: on ? colors.azureSoft : "transparent" }}>
            <Text style={{ color: on ? colors.azure : colors.textSec, fontWeight: "600", fontSize: 12 }}>{i + 1} · {label}</Text>
          </Pressable>
        );
      })}
    </ScrollView>
  );
}

function makeStyles(c: ThemeColors) {
  return StyleSheet.create({
    root: { flex: 1, backgroundColor: c.bg },
    scroll: { padding: space.md, paddingBottom: 60, maxWidth: 900, width: "100%", alignSelf: "center" },
    titleRow: { flexDirection: "row", alignItems: "center", gap: space.sm, marginBottom: space.md },
    h1: { color: c.textPri, fontSize: 22, fontWeight: "700" },
    h2: { color: c.textPri, fontSize: 16, fontWeight: "700", marginBottom: 4 },
    sub: { color: c.textSec, fontSize: 12, lineHeight: 17, marginBottom: 6 },
    card: { backgroundColor: c.bgCard, borderColor: c.border, borderWidth: 1, borderRadius: radii.md, padding: space.md, marginBottom: space.sm },
    row: { flexDirection: "row", flexWrap: "wrap", gap: space.sm, marginTop: 6 },
    rowBetween: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 8 },
    drop: { borderWidth: 1.5, borderStyle: "dashed", borderRadius: radii.md, padding: space.lg, alignItems: "center", gap: 6, marginVertical: space.sm },
    primary: { flexDirection: "row", gap: 8, alignItems: "center", justifyContent: "center", backgroundColor: c.azure, borderRadius: radii.md, paddingVertical: 13, marginTop: space.sm },
    primaryTxt: { color: "#fff", fontWeight: "700", fontSize: 14 },
    ghost: { flexDirection: "row", gap: 6, alignItems: "center", justifyContent: "center", paddingVertical: 10, paddingHorizontal: 12 },
    ghostTxt: { color: c.azure, fontWeight: "700", fontSize: 13 },
    banner: { flexDirection: "row", gap: 8, alignItems: "center", borderWidth: 1, borderRadius: radii.sm, padding: 10, marginBottom: space.sm },
    bannerTxt: { color: c.textPri, fontSize: 12 },
    busy: { flexDirection: "row", gap: 8, alignItems: "center", marginBottom: space.sm },
    err: { color: c.ruby, fontSize: 12, marginTop: 4 },
    warn: { color: c.amber, fontSize: 12, marginTop: 4 },
    kv: { color: c.textSec, fontSize: 12, marginRight: 14, marginBottom: 2 },
    kvv: { color: c.textPri, fontWeight: "600" },
  });
}
