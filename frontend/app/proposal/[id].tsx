/**
 * Portfolio Proposal — Under Review screen.
 *
 * Loads the proposal computed by /proposal/build, then lets the advisor edit
 * client details and the financial inputs (allocations, investment amount,
 * SWP) and RECALCULATE in place. The selected funds come from the saved doc
 * and are never reset — editing recomputes on this same screen (reusing the
 * same proposal id) instead of sending the advisor back to fund selection.
 *
 * Also: download PDF/Excel, and email the proposal PDF to the client via the
 * device's mail app (recipient prompted, PDF attached).
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  View, Text, StyleSheet, ScrollView, ActivityIndicator, Pressable, Alert,
  Platform, TextInput, Switch,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { useLocalSearchParams } from "expo-router";
import { FileSpreadsheet, FileText, RefreshCw, Mail, Trash2, Plus } from "lucide-react-native";
import * as FileSystem from "expo-file-system/legacy";
import * as Sharing from "expo-sharing";
import * as MailComposer from "expo-mail-composer";
import { api, RECOMMENDED_BY, type ProposalDoc, type ProposalFundInput } from "@/src/api";
import { useTheme } from "@/src/ThemeProvider";
import { radii, space, type ThemeColors, type ThemeType } from "@/src/theme";
import { FundSearchModal, type PickedFund } from "@/src/components/FundSearchModal";
import { formatRupees, formatRupeesCompact, formatPercent } from "@/src/utils/format";

/** The funds currently in the portfolio, as the advisor is editing them.
 *  Held in edit state (not read from `doc`) so a fund can be removed and the
 *  recalculation sent with the reduced set. */
interface EditFund {
  scheme_code: number;
  scheme_name: string;
}

interface EditState {
  clientName: string;
  clientPhone: string;
  clientPan: string;
  investmentAmount: string;
  funds: EditFund[];
  allocations: Record<number, string>; // scheme_code -> percent as typed
  includeSwp: boolean;
  withdrawal: string;
  inflation: string;
  swpYears: string;
  overrideSwp: boolean;
  swpCorpus: string;
  swpReturn: string;
}

/* NOTE ON ALLOCATIONS — deliberate, do not "improve" this.
 *
 * Adding or removing a fund NEVER rewrites the other funds' percentages.
 * They are exact numbers the advisor typed for a client's portfolio, and
 * silently reweighting them — even by a mathematically tidy rule — changes a
 * recommendation the advisor didn't change. So:
 *
 *   Add    → the new fund starts blank (0%); every existing row is untouched.
 *   Delete → the removed fund's share is simply freed; every surviving row is
 *            untouched, and the total drops below 100%.
 *
 * Both leave the portfolio temporarily NOT summing to 100%, which is the
 * point: the advisor decides where that percentage goes, then presses
 * "Update & Recalculate". validate() blocks the recalculation until the
 * total is 100% again and no fund sits at 0%, so an in-between state can
 * never reach the server (the backend enforces both — see
 * proposal_routes.ProposalRequest._validate_funds).
 *
 * This is why NEITHER edit auto-recalculates: there is nothing valid to send
 * until the advisor has finished. An earlier version rescaled on delete and
 * recalculated immediately; that meant deleting one fund quietly rewrote
 * every other allocation in the proposal.
 */

function seedFromDoc(doc: ProposalDoc): EditState {
  const allocations: Record<number, string> = {};
  for (const f of doc.plan.funds) {
    allocations[f.scheme_code] = String(+(f.allocation_pct * 100).toFixed(2));
  }
  const swp = doc.swp; // null when this proposal has no withdrawal plan
  return {
    clientName: doc.client_name ?? "",
    clientPhone: doc.client_phone ?? "",
    clientPan: doc.client_pan ?? "",
    investmentAmount: String(Math.round(doc.plan.investment_amount)),
    funds: doc.plan.funds.map((f) => ({ scheme_code: f.scheme_code, scheme_name: f.scheme_name })),
    allocations,
    includeSwp: swp != null,
    withdrawal: swp ? String(Math.round(swp.monthly_withdrawal)) : "",
    inflation: swp ? String(+(swp.inflation * 100).toFixed(2)) : "6",
    swpYears: swp ? String(swp.years) : "10",
    overrideSwp: swp ? !swp.corpus_auto_derived : false,
    swpCorpus: swp ? String(Math.round(swp.corpus)) : "",
    swpReturn: swp ? String(+(swp.annual_return * 100).toFixed(2)) : "",
  };
}

export default function ProposalReviewScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const { colors, type } = useTheme();
  const styles = useMemo(() => makeStyles(colors, type), [colors, type]);


  const [doc, setDoc] = useState<ProposalDoc | null>(null);
  const [edit, setEdit] = useState<EditState | null>(null);
  const [loading, setLoading] = useState(true);
  const [recalculating, setRecalculating] = useState(false);
  const [downloading, setDownloading] = useState<"xlsx" | "pdf" | null>(null);
  const [emailing, setEmailing] = useState(false);
  const [emailTo, setEmailTo] = useState("");
  const [addOpen, setAddOpen] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!id) return;
    setLoading(true);
    try {
      const d = await api.getProposal(id);
      setDoc(d);
      setEdit(seedFromDoc(d));
      setErr(null);
    } catch (e: any) {
      setErr(e?.message ?? "Failed to load proposal");
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => { load(); }, [load]);

  // ── Editing helpers ─────────────────────────────────────────────────────────
  function patch(p: Partial<EditState>) {
    setEdit((prev) => (prev ? { ...prev, ...p } : prev));
  }
  function setAllocation(code: number, value: string) {
    setEdit((prev) =>
      prev
        ? { ...prev, allocations: { ...prev.allocations, [code]: value.replace(/[^0-9.]/g, "") } }
        : prev,
    );
  }

  const allocationTotal = edit
    ? edit.funds.reduce((s, f) => s + (parseFloat(edit.allocations[f.scheme_code]) || 0), 0)
    : 0;
  // A freshly-added fund sits at 0% until the advisor fills it in, which
  // leaves the TOTAL at 100% while the portfolio isn't actually valid yet —
  // so the total alone can't be the whole check. The backend requires every
  // allocation_pct > 0.
  const hasZeroAllocationFund = edit
    ? edit.funds.some((f) => !(parseFloat(edit.allocations[f.scheme_code]) > 0))
    : false;
  const allocationOk = Math.abs(allocationTotal - 100) < 0.5 && !hasZeroAllocationFund;

  function validate(state: EditState): string | null {
    if (!doc) return "Not loaded.";
    if (state.funds.length === 0) return "No funds in this proposal. Add at least one fund to recalculate.";
    const total = state.funds.reduce((s, f) => s + (parseFloat(state.allocations[f.scheme_code]) || 0), 0);
    if (Math.abs(total - 100) >= 0.5) return `Allocations must sum to 100% (currently ${total.toFixed(1)}%).`;
    // A fund added via "Add Fund" starts at 0% on purpose (see applyAddition)
    // so it never silently steals allocation from the funds already there —
    // but 0% itself is never a valid thing to submit (the backend requires
    // every allocation_pct > 0). Catch it here with a clear message naming
    // the fund, instead of letting it reach the server as a 422.
    const zeroFunds = state.funds.filter((f) => !(parseFloat(state.allocations[f.scheme_code]) > 0));
    if (zeroFunds.length > 0) {
      return `Assign an allocation greater than 0% to: ${zeroFunds.map((f) => f.scheme_name).join(", ")}.`;
    }
    if (!(parseFloat(state.investmentAmount) > 0)) return "Enter an investment amount.";
    if (state.includeSwp) {
      if (!(parseFloat(state.withdrawal) > 0)) return "Enter a monthly SWP withdrawal amount.";
      if (state.overrideSwp && !(parseFloat(state.swpCorpus) > 0)) return "Enter an SWP corpus, or turn off override.";
      if (state.overrideSwp && !(parseFloat(state.swpReturn) >= 0)) return "Enter an SWP return %, or turn off override.";
    }
    return null;
  }

  // ── Recalculate in place (same proposal id) ────────────────────────────────
  // Driven by the "Update & Recalculate" button. `snapshot` lets a caller
  // recalculate from a state it just computed, without waiting for the
  // setEdit re-render to land — no edit does that today (adding and removing
  // a fund both leave the allocations deliberately not summing to 100%, so
  // there is nothing valid to send until the advisor has redistributed it),
  // but the parameter costs nothing and keeps that option open.
  async function recalculate(snapshot?: EditState) {
    const state = snapshot ?? edit;
    if (!state || !doc || !id) return;
    const e = validate(state);
    if (e) { Alert.alert("Can't recalculate", e); return; }
    setRecalculating(true);
    try {
      const fundInputs: ProposalFundInput[] = state.funds.map((f) => ({
        scheme_code: f.scheme_code,
        allocation_pct: (parseFloat(state.allocations[f.scheme_code]) || 0) / 100,
      }));
      const res = await api.buildProposal({
        proposal_id: id, // recompute this same proposal — do NOT mint a new one
        client_name: state.clientName.trim() || undefined,
        client_phone: state.clientPhone.trim() || undefined,
        client_pan: state.clientPan.trim() || undefined,
        mode: doc.mode,
        horizon: doc.horizon,
        // Echo the ORIGINAL build's toggle state, not a reset to false — a
        // recalculation must keep using the same 2010/Mgmt data variant the
        // proposal was built with, or every edit here would silently
        // revert the whole proposal to plain since-inception numbers.
        since_2010: doc.since_2010 ?? false,
        separate_management: doc.separate_management ?? false,
        investment_amount: parseFloat(state.investmentAmount),
        funds: fundInputs,
        swp: state.includeSwp
          ? {
              corpus: state.overrideSwp ? parseFloat(state.swpCorpus) : null,
              annual_return: state.overrideSwp ? parseFloat(state.swpReturn) / 100 : null,
              inflation: (parseFloat(state.inflation) || 0) / 100,
              monthly_withdrawal: parseFloat(state.withdrawal),
              years: parseInt(state.swpYears, 10) || 10,
            }
          : undefined,
      });
      setDoc(res);
      setEdit(seedFromDoc(res)); // re-seed from canonical recomputed values
    } catch (e: any) {
      Alert.alert("Recalculation failed", e?.message ?? "Unknown error");
    } finally {
      setRecalculating(false);
    }
  }

  // ── Delete a fund ─────────────────────────────────────────────────────────
  // Drops the fund and nothing else: every surviving fund keeps the exact
  // allocation the advisor typed, and the freed percentage is left unassigned
  // for them to place. See the note above seedFromDoc for why.
  //
  // The removed fund's entry is deleted from `allocations` rather than left
  // behind, so a fund removed and later re-added starts blank instead of
  // silently inheriting its old percentage.
  function applyRemoval(state: EditState, code: number): EditState {
    const funds = state.funds.filter((f) => f.scheme_code !== code);
    const allocations = { ...state.allocations };
    delete allocations[code];
    return { ...state, funds, allocations };
  }

  // ── Add a fund ────────────────────────────────────────────────────────────
  // The exact counterpart of applyRemoval: the new fund arrives at 0% (a
  // blank input) and every existing allocation is left as the advisor typed
  // it. They dial the new one in, trim the others to suit, and press
  // "Update & Recalculate". See the note above seedFromDoc.
  function applyAddition(state: EditState, f: PickedFund): EditState {
    return {
      ...state,
      funds: [...state.funds, { scheme_code: f.scheme_code, scheme_name: f.scheme_name }],
      allocations: { ...state.allocations, [f.scheme_code]: "" },
    };
  }

  function addFund(f: PickedFund) {
    setAddOpen(false);
    if (!edit || recalculating) return;
    if (edit.funds.some((x) => x.scheme_code === f.scheme_code)) {
      Alert.alert("Already in this portfolio", `"${f.scheme_name}" is already included.`);
      return;
    }
    setEdit(applyAddition(edit, f));
  }

  function removeFund(code: number) {
    if (!edit || recalculating) return;
    const fund = edit.funds.find((f) => f.scheme_code === code);

    // Removing the last fund leaves an empty portfolio, which can never be
    // recalculated (the backend requires >= 1 fund) — worth confirming rather
    // than letting a stray tap empty the proposal.
    if (edit.funds.length === 1) {
      Alert.alert(
        "Remove the last fund?",
        `"${fund?.scheme_name ?? "This fund"}" is the only fund in this portfolio. ` +
          "Removing it leaves the proposal empty and it can't be recalculated until you add a fund again.",
        [
          { text: "Cancel", style: "cancel" },
          {
            text: "Remove",
            style: "destructive",
            onPress: () => setEdit((prev) => (prev ? applyRemoval(prev, code) : prev)),
          },
        ],
      );
      return;
    }

    setEdit(applyRemoval(edit, code));
  }

  /** ClientName_PAN_TENURE_MODE_AMOUNT — the name the client sees in WhatsApp.
   *  Must mirror `_safe_filename()` in backend/proposal_routes.py; the local
   *  cache filename is what the share sheet attaches, so the server's
   *  Content-Disposition alone is not enough. */
  const exportFilename = useCallback(
    (format: "xlsx" | "pdf") => {
      const slug = (v: unknown) =>
        String(v ?? "").replace(/[^A-Za-z0-9 _-]/g, "").trim().replace(/\s+/g, "_");
      const parts = [
        slug(doc?.client_name),
        slug(doc?.client_pan).toUpperCase(),
        doc?.horizon ? `${doc.horizon}Y` : "",
        (doc?.mode ?? "").toUpperCase(),
        doc?.plan?.investment_amount ? String(Math.round(doc.plan.investment_amount)) : "",
      ].filter(Boolean);
      return `${parts.join("_") || "Portfolio_Proposal"}.${format}`;
    },
    [doc],
  );

  async function downloadFile(format: "xlsx" | "pdf"): Promise<string | null> {
    if (!id) return null;
    const url = api.proposalExportUrl(id, format);
    const dest = FileSystem.cacheDirectory + exportFilename(format);
    const { uri } = await FileSystem.downloadAsync(url, dest);
    return uri;
  }

  async function onDownload(format: "xlsx" | "pdf") {
    if (!id) return;
    // BUG FIXED (2026-08-04): expo-file-system's legacy web shim implements
    // NONE of downloadAsync/cacheDirectory (see node_modules/expo-file-system
    // /src/legacy/ExponentFileSystemShim.ts — cacheDirectory is hardcoded
    // null, downloadAsync isn't defined at all), so downloadFile() below
    // always threw on web, silently caught by the catch block, and the user
    // saw... nothing, since Alert.alert() maps to a native window.alert()
    // that isn't part of the DOM. Confirmed the backend itself was never the
    // problem: a raw fetch() of the same export URL from this origin returns
    // 200 with a valid file. The backend already sends
    // `Content-Disposition: attachment` (proposal_routes.py), so on web the
    // correct fix is to skip expo-file-system entirely and let the browser
    // handle the download natively.
    if (Platform.OS === "web") {
      window.open(api.proposalExportUrl(id, format), "_blank");
      return;
    }
    setDownloading(format);
    try {
      const uri = await downloadFile(format);
      if (!uri) return;
      if (await Sharing.isAvailableAsync()) await Sharing.shareAsync(uri);
      else Alert.alert("Saved", `File saved to ${uri}`);
    } catch (e: any) {
      Alert.alert("Download failed", e?.message ?? "Unknown error");
    } finally {
      setDownloading(null);
    }
  }

  // ── Email the PDF to the client via the device mail app ─────────────────────
  async function onEmail() {
    const to = emailTo.trim();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(to)) {
      Alert.alert("Enter a valid email", "Please enter the client's email address before sending.");
      return;
    }
    setEmailing(true);
    try {
      const available = await MailComposer.isAvailableAsync();
      const uri = await downloadFile("pdf");
      if (!available || !uri) {
        // No mail client configured — fall back to the share sheet.
        if (uri && (await Sharing.isAvailableAsync())) { await Sharing.shareAsync(uri); return; }
        Alert.alert("Email unavailable", "No mail app is set up on this device.");
        return;
      }
      const who = doc?.client_name ? ` — ${doc.client_name}` : "";
      await MailComposer.composeAsync({
        recipients: [to],
        subject: `Portfolio Proposal${who}`,
        body: `Dear ${doc?.client_name || "Investor"},\n\nPlease find your portfolio proposal attached.\n\nRecommended by ${RECOMMENDED_BY}.`,
        attachments: [uri],
      });
    } catch (e: any) {
      Alert.alert("Couldn't open mail", e?.message ?? "Unknown error");
    } finally {
      setEmailing(false);
    }
  }

  if (loading) {
    return (
      <View style={[styles.safe, styles.center]}>
        <ActivityIndicator color={colors.azure} />
      </View>
    );
  }
  if (err || !doc || !edit) {
    return (
      <View style={[styles.safe, styles.center, { padding: space.xl }]}>
        <Text style={{ color: colors.textSec, textAlign: "center" }}>{err ?? "Proposal not found"}</Text>
        <Pressable onPress={load} style={styles.retryBtn}>
          <RefreshCw size={14} color={colors.azure} />
          <Text style={{ color: colors.azure, marginLeft: 6, fontWeight: "600" }}>Retry</Text>
        </Pressable>
      </View>
    );
  }

  const a = doc.plan.aggregate;

  // `doc` is the last SERVER-computed proposal; `edit.funds` is what the
  // advisor currently has on screen. They diverge only while a recalculation
  // is in flight, or after the last fund was removed (which can't be
  // recalculated) — in which case the results below are stale and say so.
  const extrapolatedCodes = new Set(
    doc.plan.funds.filter((f) => f.extrapolated).map((f) => f.scheme_code),
  );
  const docCodes = doc.plan.funds.map((f) => f.scheme_code).join(",");
  const editCodes = edit.funds.map((f) => f.scheme_code).join(",");
  const resultsStale = docCodes !== editCodes;
  // Exports regenerate from the SAVED doc, so an empty on-screen portfolio
  // would silently export the old fund list. Block them until it's non-empty.
  const exportsBlocked = edit.funds.length === 0;

  return (
    <SafeAreaView style={styles.safe} edges={["bottom"]}>
      <ScrollView contentContainerStyle={{ padding: space.md, paddingBottom: space.xl * 2 }} keyboardShouldPersistTaps="handled">
        <View style={styles.reviewBanner}>
          <Text style={styles.reviewBannerText}>UNDER REVIEW · edit values and recalculate</Text>
        </View>

        <Text style={styles.title}>{doc.client_name || "Portfolio Proposal"}</Text>
        <Text style={styles.subtitle}>
          {doc.proposal_date} · {doc.mode === "sip" ? "SIP" : "Lumpsum"} · {doc.horizon}Y · {RECOMMENDED_BY}
        </Text>

        {/* Client details (editable) */}
        <Text style={styles.sectionTitle}>Client Details</Text>
        <TextInput style={styles.input} placeholder="Client name" placeholderTextColor={colors.textTer}
          value={edit.clientName} onChangeText={(v) => patch({ clientName: v })} />
        <TextInput style={styles.input} placeholder="Phone number" placeholderTextColor={colors.textTer}
          keyboardType="phone-pad" value={edit.clientPhone} onChangeText={(v) => patch({ clientPhone: v })} />
        <TextInput style={styles.input} placeholder="PAN" placeholderTextColor={colors.textTer}
          autoCapitalize="characters" value={edit.clientPan} onChangeText={(v) => patch({ clientPan: v })} />

        <Text style={styles.sectionTitle}>Recommended By</Text>
        <View style={styles.readonlyField}><Text style={styles.readonlyText}>{RECOMMENDED_BY}</Text></View>

        {/* Allocations (editable; funds can be removed) */}
        <Text style={styles.sectionTitle}>
          Allocation ({edit.funds.length} fund{edit.funds.length === 1 ? "" : "s"})
        </Text>
        {edit.funds.map((f) => (
          <View key={f.scheme_code} style={styles.fundRow}>
            <Text style={styles.fundName} numberOfLines={2}>
              {f.scheme_name}{extrapolatedCodes.has(f.scheme_code) ? " *" : ""}
            </Text>
            <TextInput style={styles.allocInput} placeholder="%" placeholderTextColor={colors.textTer}
              keyboardType="decimal-pad" value={edit.allocations[f.scheme_code] ?? ""}
              onChangeText={(v) => setAllocation(f.scheme_code, v)} />
            <Pressable
              onPress={() => removeFund(f.scheme_code)}
              disabled={recalculating}
              hitSlop={10}
              accessibilityRole="button"
              accessibilityLabel={`Remove ${f.scheme_name} from this portfolio`}
              style={({ pressed }) => [styles.deleteBtn, (pressed || recalculating) && { opacity: 0.4 }]}
            >
              <Trash2 size={16} color="#EF4444" />
            </Pressable>
          </View>
        ))}
        <Pressable
          onPress={() => setAddOpen(true)}
          disabled={recalculating}
          style={({ pressed }) => [styles.addBtn, (pressed || recalculating) && { opacity: 0.6 }]}
          accessibilityRole="button"
          accessibilityLabel="Add a fund to this portfolio"
        >
          <Plus size={16} color={colors.azure} />
          <Text style={styles.addBtnText}>Add Fund</Text>
        </Pressable>

        {edit.funds.length === 0 ? (
          <Text style={styles.emptyPortfolioText}>
            This portfolio has no funds. Use Add Fund above to put one back —
            recalculation and exports stay disabled until at least one fund is present.
          </Text>
        ) : (
          <Text style={[styles.allocTotal, { color: allocationOk ? colors.azure : colors.ruby }]}>
            Total allocation: {allocationTotal.toFixed(1)}%
          </Text>
        )}

        <Text style={styles.sectionTitle}>{doc.mode === "sip" ? "Monthly SIP Amount (₹)" : "Lumpsum Amount (₹)"}</Text>
        <TextInput style={styles.input} keyboardType="numeric"
          value={edit.investmentAmount} onChangeText={(v) => patch({ investmentAmount: v.replace(/[^0-9.]/g, "") })} />

        {/* SWP (editable, optional) */}
        <View style={styles.switchRow}>
          <Text style={styles.sectionTitle}>Withdrawal Plan (SWP) — optional</Text>
          <Switch value={edit.includeSwp} onValueChange={(v) => patch({ includeSwp: v })} />
        </View>
        {edit.includeSwp && (
          <>
            <View style={styles.row2}>
              <View style={styles.col}>
                <Text style={styles.label}>Monthly withdrawal (₹)</Text>
                <TextInput style={styles.input} keyboardType="numeric" value={edit.withdrawal}
                  onChangeText={(v) => patch({ withdrawal: v.replace(/[^0-9.]/g, "") })} />
              </View>
              <View style={styles.col}>
                <Text style={styles.label}>Inflation (%)</Text>
                <TextInput style={styles.input} keyboardType="decimal-pad" value={edit.inflation}
                  onChangeText={(v) => patch({ inflation: v.replace(/[^0-9.]/g, "") })} />
              </View>
            </View>
            <View style={styles.row2}>
              <View style={styles.col}>
                <Text style={styles.label}>Withdrawal years</Text>
                <TextInput style={styles.input} keyboardType="numeric" value={edit.swpYears}
                  onChangeText={(v) => patch({ swpYears: v.replace(/[^0-9]/g, "") })} />
              </View>
              <View style={styles.col} />
            </View>
            <View style={styles.switchRow}>
              <Text style={styles.label}>Override auto-derived corpus & return</Text>
              <Switch value={edit.overrideSwp} onValueChange={(v) => patch({ overrideSwp: v })} />
            </View>
            {edit.overrideSwp && (
              <View style={styles.row2}>
                <View style={styles.col}>
                  <Text style={styles.label}>Corpus (₹)</Text>
                  <TextInput style={styles.input} keyboardType="numeric" value={edit.swpCorpus}
                    onChangeText={(v) => patch({ swpCorpus: v.replace(/[^0-9.]/g, "") })} />
                </View>
                <View style={styles.col}>
                  <Text style={styles.label}>Return (%)</Text>
                  <TextInput style={styles.input} keyboardType="decimal-pad" value={edit.swpReturn}
                    onChangeText={(v) => patch({ swpReturn: v.replace(/[^0-9.]/g, "") })} />
                </View>
              </View>
            )}
          </>
        )}

        <Pressable
          style={[styles.recalcBtn, (recalculating || edit.funds.length === 0) && { opacity: 0.5 }]}
          disabled={recalculating || edit.funds.length === 0}
          onPress={() => recalculate()}
        >
          {recalculating ? <ActivityIndicator color="#fff" /> : (
            <>
              <RefreshCw size={16} color="#fff" />
              <Text style={styles.recalcText}>Update & Recalculate</Text>
            </>
          )}
        </Pressable>

        {resultsStale && (
          <Text style={styles.staleText}>
            {edit.funds.length === 0
              ? "Showing the last calculated portfolio. It no longer matches the fund list above."
              : "Recalculating — the figures below are from the previous calculation."}
          </Text>
        )}

        {/* Recalculated results (read-only, reflect latest doc) */}
        {doc.warnings.length > 0 && (
          <View style={styles.warnBox}>
            {doc.warnings.map((w, i) => <Text key={i} style={styles.warnText}>• {w}</Text>)}
          </View>
        )}

        {/* Per-fund parameters. Every figure is read at the fund's own
            metrics_horizon — for an Additional Recommendation fund with a
            shorter track record than the tenure that is its own rolling
            period (e.g. 7Y inside a 10Y proposal), never the tenure. */}
        <Text style={styles.sectionTitle}>Fund Parameters</Text>
        {doc.plan.funds.map((f) => (
          <View key={f.scheme_code} style={styles.card}>
            <Text style={styles.fundCardName}>
              {f.scheme_name}{f.extrapolated ? " *" : ""}
            </Text>
            <Text style={styles.fundCardMeta}>
              {f.category} · figures at {f.metrics_horizon ?? doc.horizon}Y
            </Text>
            {f.available ? (
              <>
                <Row label="FQS" value={f.fqs == null ? "—" : f.fqs.toFixed(1)} colors={colors} />
                <Row label="Success %" value={fmtPct(f.success_pct)} colors={colors} />
                <Row label="Median WM / XIRR" value={`${f.median_wm?.toFixed(2) ?? "—"} · ${fmtPct(f.xirr_median)}`} colors={colors} />
                <Row label="P10 WM / XIRR" value={`${f.p10_wm?.toFixed(2) ?? "—"} · ${fmtPct(f.xirr_p10)}`} colors={colors} />
                <Row label="CV (consistency)" value={fmtPct(f.cv ?? null)} colors={colors} />
                <Row label="Benchmark XIRR" value={fmtPct(f.benchmark_xirr ?? null)} colors={colors} />
                <Row label="No. of Windows" value={f.n_windows == null ? "—" : String(f.n_windows)} colors={colors} />
              </>
            ) : (
              <Text style={styles.excludedText}>No usable rolling window — excluded from the blend.</Text>
            )}
          </View>
        ))}
        {(a.extrapolated_funds?.length ?? 0) > 0 && (
          <Text style={styles.footnoteText}>
            * Shorter track record than the {doc.horizon}-year horizon. FQS, Success %, Median WM,
            P10 WM and both XIRRs above are that fund&apos;s actual figures at its own rolling period,
            not at {doc.horizon} years. Expected wealth below applies that period&apos;s XIRR to the
            full {doc.horizon}-year horizon.
          </Text>
        )}

        {/* Portfolio Summary — same three-column layout as the PDF export:
              [ Capital Invested ]  [ Expected Wealth (P10) ]  [ Expected Wealth (Median) ]
                                    [ WM (P10)             ]  [ Blended WM (Median)      ]
                                    [ XIRR (P10)           ]  [ Blended XIRR (Median)    ]
            Capital Invested is the one input figure, so it stands alone in
            column 1; the P10 and Median cases read straight down as a pair. */}
        <Text style={styles.sectionTitle}>{doc.mode === "sip" ? "SIP" : "Lumpsum"} Plan · {doc.horizon}Y</Text>
        <View style={styles.summaryGrid}>
          <View style={styles.summaryCol}>
            <Metric styles={styles} label="Capital Invested" value={formatRupeesCompact(a.capital_invested)} color={colors.textPri} />
          </View>
          <View style={styles.summaryCol}>
            <Metric styles={styles} label="Expected Wealth (P10)" value={formatRupeesCompact(a.expected_wealth_p10)} color={colors.azure} />
            <Metric styles={styles} label="WM (P10)" value={`${a.blended_wm_p10.toFixed(2)}x`} color={SUMMARY_TEAL} />
            <Metric styles={styles} label="XIRR (P10)" value={fmtPct(a.blended_xirr_p10)} color={SUMMARY_GREEN} />
          </View>
          <View style={styles.summaryCol}>
            <Metric styles={styles} label="Expected Wealth (Median)" value={formatRupeesCompact(a.expected_wealth_median)} color={colors.azure} />
            <Metric styles={styles} label="Blended WM (Median)" value={`${a.blended_wm_median.toFixed(2)}x`} color={SUMMARY_TEAL} />
            <Metric styles={styles} label="Blended XIRR (Median)" value={fmtPct(a.blended_xirr_median)} color={SUMMARY_GREEN} />
          </View>
        </View>
        <View style={styles.summaryGrid}>
          <View style={styles.summaryCol}>
            <Metric styles={styles} label="Blended CV (portfolio)" value={fmtPct(a.blended_cv ?? null)} color={SUMMARY_TEAL} />
          </View>
        </View>
        {a.excluded_funds.length > 0 && <Text style={styles.excludedText}>Excluded: {a.excluded_funds.join(", ")}</Text>}

        {doc.swp && (
          <>
            <Text style={styles.sectionTitle}>SWP (Withdrawal Plan)</Text>
            <View style={styles.card}>
              <Row label="Corpus" value={`${formatRupees(doc.swp.corpus)}${doc.swp.corpus_auto_derived ? " (auto)" : ""}`} colors={colors} />
              <Row label="Assumed return" value={`${(doc.swp.annual_return * 100).toFixed(1)}%${doc.swp.annual_return_auto_derived ? " (auto)" : ""}`} colors={colors} />
              <Row label="Inflation" value={`${(doc.swp.inflation * 100).toFixed(1)}%`} colors={colors} />
              <Row label="Monthly withdrawal" value={formatRupees(doc.swp.monthly_withdrawal)} colors={colors} />
              <Row label={`Balance after ${doc.swp.years}Y`} value={formatRupees(doc.swp.ending_balance)} colors={colors} />
            </View>
          </>
        )}

        {/* Email to client */}
        <Text style={styles.sectionTitle}>Email to Client</Text>
        <View style={styles.emailRow}>
          <TextInput style={[styles.input, { flex: 1, marginBottom: 0 }]} placeholder="client@email.com"
            placeholderTextColor={colors.textTer} keyboardType="email-address" autoCapitalize="none"
            value={emailTo} onChangeText={setEmailTo} />
          <Pressable
            style={[styles.emailBtn, (emailing || exportsBlocked) && { opacity: 0.5 }]}
            disabled={emailing || exportsBlocked}
            onPress={onEmail}
          >
            {emailing ? <ActivityIndicator color="#fff" /> : (<><Mail size={16} color="#fff" /><Text style={styles.emailText}>Send</Text></>)}
          </Pressable>
        </View>

        <View style={styles.downloadRow}>
          <Pressable
            style={[styles.downloadBtn, exportsBlocked && { opacity: 0.5 }]}
            disabled={!!downloading || exportsBlocked}
            onPress={() => onDownload("xlsx")}
          >
            {downloading === "xlsx" ? <ActivityIndicator color={colors.azure} /> : <FileSpreadsheet size={18} color={colors.azure} />}
            <Text style={styles.downloadText}>Excel</Text>
          </Pressable>
          <Pressable
            style={[styles.downloadBtn, exportsBlocked && { opacity: 0.5 }]}
            disabled={!!downloading || exportsBlocked}
            onPress={() => onDownload("pdf")}
          >
            {downloading === "pdf" ? <ActivityIndicator color={colors.azure} /> : <FileText size={18} color={colors.azure} />}
            <Text style={styles.downloadText}>PDF</Text>
          </Pressable>
        </View>
        {exportsBlocked && (
          <Text style={styles.staleText}>
            Exports are disabled while the portfolio is empty — they would regenerate the previous
            fund list from the saved proposal.
          </Text>
        )}
      </ScrollView>

      {/* Gated on `addOpen` rather than relying on the Modal's own `visible`
          prop: on react-native-web the sheet stayed in the DOM and remained
          interactive after a fund was picked, so the search box sat on top of
          the recalculated proposal. Not rendering it at all guarantees it
          unmounts on every platform. */}
      {addOpen && (
        <FundSearchModal
          visible
          onClose={() => setAddOpen(false)}
          onPick={addFund}
          excludeCodes={edit.funds.map((f) => f.scheme_code)}
        />
      )}
    </SafeAreaView>
  );
}

function Row({ label, value, colors }: { label: string; value: string; colors: ThemeColors }) {
  return (
    <View style={{ flexDirection: "row", justifyContent: "space-between", paddingVertical: 4 }}>
      <Text style={{ fontSize: 13, color: colors.textSec }}>{label}</Text>
      <Text style={{ fontSize: 13, fontWeight: "700", color: colors.textPri }}>{value}</Text>
    </View>
  );
}

// Both delegate to the shared Indian formatter — see src/utils/format.ts.
const fmtPct = (v: number | null) => formatPercent(v);
const rupees = (v: number | null | undefined) => formatRupees(v);

/** One Portfolio Summary cell. `styles` is passed in rather than closed over so
 *  this stays a stable module-level component. The value colour mirrors the
 *  PDF export's palette. */
function Metric({
  label, value, color, styles,
}: { label: string; value: string; color: string; styles: ProposalStyles }) {
  return (
    <View style={styles.metricCard}>
      <Text style={styles.metricLabel} numberOfLines={2}>{label}</Text>
      <Text style={[styles.metricValue, { color }]} numberOfLines={1} adjustsFontSizeToFit>
        {value}
      </Text>
    </View>
  );
}

// Portfolio Summary accents — identical hexes to proposal_pdf.py's TEAL/GREEN
// so the app screen and the exported PDF read as the same document.
const SUMMARY_TEAL = "#0D9488";
const SUMMARY_GREEN = "#16A34A";

type ProposalStyles = ReturnType<typeof makeStyles>;

function makeStyles(colors: ThemeColors, type: ThemeType) {
  return StyleSheet.create({
    safe: { flex: 1, backgroundColor: colors.bg },
    center: { alignItems: "center", justifyContent: "center" },
    reviewBanner: {
      backgroundColor: colors.amber + "22", borderWidth: 1, borderColor: colors.amber + "66",
      borderRadius: radii.md, paddingVertical: 8, paddingHorizontal: space.md, marginBottom: space.sm,
    },
    reviewBannerText: { fontSize: 12, fontWeight: "800", color: colors.amber, letterSpacing: 0.5 },
    title: { fontSize: 22, fontWeight: "800", color: colors.textPri },
    subtitle: { fontSize: 13, color: colors.textTer, marginTop: 4, marginBottom: space.md },
    sectionTitle: {
      fontSize: 13, fontWeight: "700", color: colors.textTer,
      textTransform: "uppercase", letterSpacing: 0.6, marginTop: space.lg, marginBottom: space.sm,
    },
    input: {
      backgroundColor: colors.bgCard, borderWidth: 1, borderColor: colors.border,
      borderRadius: radii.md, paddingHorizontal: space.md, paddingVertical: 11,
      color: colors.textPri, fontSize: 15, marginBottom: space.sm,
    },
    readonlyField: {
      backgroundColor: colors.bgElev, borderWidth: 1, borderColor: colors.border,
      borderRadius: radii.md, paddingHorizontal: space.md, paddingVertical: 12, marginBottom: space.sm,
    },
    readonlyText: { color: colors.textPri, fontSize: 15, fontWeight: "700" },
    label: { fontSize: 12, color: colors.textTer, marginBottom: 4, fontWeight: "600" },
    row2: { flexDirection: "row", gap: space.sm },
    col: { flex: 1 },
    fundRow: {
      flexDirection: "row", alignItems: "center", gap: space.sm,
      paddingVertical: 10, borderBottomWidth: StyleSheet.hairlineWidth, borderColor: colors.border,
    },
    fundName: { flex: 1, color: colors.textPri, fontSize: 14, fontWeight: "600" },
    allocInput: {
      width: 56, textAlign: "center", backgroundColor: colors.bgElev,
      borderWidth: 1, borderColor: colors.border, borderRadius: radii.sm,
      paddingVertical: 6, color: colors.textPri, fontSize: 14,
    },
    allocTotal: { fontSize: 13, fontWeight: "700", marginTop: space.sm },
    deleteBtn: { padding: 4, borderRadius: radii.sm },
    addBtn: {
      flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 6,
      marginTop: space.sm, paddingVertical: 11,
      borderRadius: radii.md, borderWidth: 1.5, borderColor: colors.azure,
      borderStyle: "dashed",
    },
    addBtnText: { color: colors.azure, fontWeight: "700", fontSize: 14 },
    emptyPortfolioText: {
      fontSize: 12, color: colors.amber, marginTop: space.sm, lineHeight: 17,
    },
    staleText: {
      fontSize: 11, color: colors.textTer, marginTop: space.sm,
      textAlign: "center", fontStyle: "italic",
    },
    switchRow: {
      flexDirection: "row", alignItems: "center", justifyContent: "space-between",
      marginTop: space.sm, marginBottom: space.xs,
    },
    recalcBtn: {
      marginTop: space.lg, backgroundColor: colors.azure, borderRadius: radii.md,
      paddingVertical: 15, flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 8,
    },
    recalcText: { color: "#fff", fontSize: 15, fontWeight: "700" },
    card: {
      backgroundColor: colors.bgCard, borderWidth: 1, borderColor: colors.border,
      borderRadius: radii.md, padding: space.md, marginBottom: space.sm,
    },
    excludedText: { fontSize: 11, color: colors.ruby, marginTop: 4 },
    // Portfolio Summary grid — three columns, mirroring the PDF export.
    summaryGrid: { flexDirection: "row", gap: space.sm, marginTop: space.xs },
    summaryCol: { flex: 1, gap: space.sm },
    metricCard: {
      backgroundColor: colors.bgCard, borderWidth: 1, borderColor: colors.border,
      borderRadius: radii.md, paddingHorizontal: 10, paddingVertical: 9,
    },
    metricLabel: { fontSize: 10, color: colors.textTer, fontWeight: "600", lineHeight: 13 },
    metricValue: { fontSize: 15, fontWeight: "800", marginTop: 3 },
    fundCardName: { fontSize: 14, fontWeight: "700", color: colors.textPri },
    fundCardMeta: { fontSize: 11, color: colors.textTer, marginTop: 2, marginBottom: 6 },
    footnoteText: { fontSize: 11, color: colors.textTer, marginTop: space.sm, lineHeight: 15, fontStyle: "italic" },
    warnBox: {
      backgroundColor: colors.amber + "22", borderWidth: 1, borderColor: colors.amber + "55",
      borderRadius: radii.md, padding: space.sm, marginTop: space.md,
    },
    warnText: { fontSize: 12, color: colors.amber },
    emailRow: { flexDirection: "row", alignItems: "center", gap: space.sm },
    emailBtn: {
      backgroundColor: colors.azure, borderRadius: radii.md, paddingHorizontal: space.md,
      paddingVertical: 12, flexDirection: "row", alignItems: "center", gap: 6,
    },
    emailText: { color: "#fff", fontWeight: "700", fontSize: 14 },
    downloadRow: { flexDirection: "row", gap: space.sm, marginTop: space.md },
    downloadBtn: {
      flex: 1, flexDirection: "row", alignItems: "center", justifyContent: "center", gap: 8,
      borderWidth: 1.5, borderColor: colors.azure, borderRadius: radii.md, paddingVertical: 14,
    },
    downloadText: { color: colors.azure, fontWeight: "700", fontSize: 14 },
    retryBtn: { flexDirection: "row", alignItems: "center", marginTop: space.md },
  });
}
