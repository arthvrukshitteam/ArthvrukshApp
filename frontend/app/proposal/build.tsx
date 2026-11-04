/**
 * Portfolio Proposal builder.
 *
 * Funds are picked on the Home ranked-fund list (checkbox "Select" mode →
 * "Build Proposal"), not here. The proposal is single-mode (SIP-only or
 * Lumpsum-only, matching whichever mode Home was in when funds were picked
 * — no Lumpsum section shows up if the advisor is working in SIP mode, and
 * vice versa) and single-horizon (the advisor's own tenure, auto-filled and
 * not re-asked here).
 *
 * This screen does not touch the existing single-fund analysis flow.
 */

import { useMemo, useState } from "react";
import {
  View,
  Text,
  StyleSheet,
  TextInput,
  Pressable,
  ScrollView,
  ActivityIndicator,
  Switch,
  Alert,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { useRouter } from "expo-router";
import { Trash2, Inbox } from "lucide-react-native";
import { api, MAX_CALC_YEARS, RECOMMENDED_BY, type ProposalFundInput } from "@/src/api";
import { useTheme } from "@/src/ThemeProvider";
import { useMode } from "@/src/ModeProvider";
import { useTenure } from "@/src/TenureProvider";
import { useProposalDraft } from "@/src/ProposalDraftProvider";
import { radii, space, type ThemeColors, type ThemeType } from "@/src/theme";

interface DraftAllocation {
  scheme_code: number;
  scheme_name: string;
  allocation: string; // percent, as typed, e.g. "25"
}

export default function ProposalBuildScreen() {
  const router = useRouter();
  const { colors, type } = useTheme();
  const { mode } = useMode();
  const { tenure } = useTenure();
  const { selectedFunds, clearSelection, since2010, separateManagement } = useProposalDraft();
  const styles = useMemo(() => makeStyles(colors, type), [colors, type]);

  const effectiveMode = mode ?? "sip";
  const horizon = Math.min(tenure ?? 7, MAX_CALC_YEARS);

  const [funds, setFunds] = useState<DraftAllocation[]>(() =>
    selectedFunds.map((f) => ({ scheme_code: f.scheme_code, scheme_name: f.scheme_name, allocation: "" })),
  );

  const [clientName, setClientName] = useState("");
  const [clientPhone, setClientPhone] = useState("");
  const [clientPan, setClientPan] = useState("");

  const [investmentAmount, setInvestmentAmount] = useState(effectiveMode === "sip" ? "10000" : "1000000");

  const [includeSwp, setIncludeSwp] = useState(false);
  const [withdrawal, setWithdrawal] = useState("");
  const [inflation, setInflation] = useState("6");
  const [swpYears, setSwpYears] = useState("10");
  const [overrideSwp, setOverrideSwp] = useState(false);
  const [swpCorpus, setSwpCorpus] = useState("");
  const [swpReturn, setSwpReturn] = useState("");

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function removeFund(code: number) {
    setFunds((prev) => prev.filter((f) => f.scheme_code !== code));
  }

  function setAllocation(code: number, value: string) {
    setFunds((prev) => prev.map((f) => (f.scheme_code === code ? { ...f, allocation: value.replace(/[^0-9.]/g, "") } : f)));
  }

  const allocationTotal = funds.reduce((sum, f) => sum + (parseFloat(f.allocation) || 0), 0);
  const allocationOk = funds.length > 0 && Math.abs(allocationTotal - 100) < 0.5;

  function validate(): string | null {
    if (funds.length === 0) return "No funds selected.";
    if (!allocationOk) return `Allocations must sum to 100% (currently ${allocationTotal.toFixed(1)}%).`;
    if (!(parseFloat(investmentAmount) > 0)) return "Enter an investment amount.";
    if (includeSwp) {
      if (!(parseFloat(withdrawal) > 0)) return "Enter a monthly SWP withdrawal amount.";
      if (overrideSwp && !(parseFloat(swpCorpus) > 0)) return "Enter an SWP corpus, or turn off override.";
      if (overrideSwp && !(parseFloat(swpReturn) >= 0)) return "Enter an SWP return %, or turn off override.";
    }
    return null;
  }

  async function onGenerate() {
    const err = validate();
    if (err) { setError(err); return; }
    setError(null);
    setSubmitting(true);
    try {
      const fundInputs: ProposalFundInput[] = funds.map((f) => ({
        scheme_code: f.scheme_code,
        allocation_pct: (parseFloat(f.allocation) || 0) / 100,
      }));
      const res = await api.buildProposal({
        client_name: clientName.trim() || undefined,
        client_phone: clientPhone.trim() || undefined,
        client_pan: clientPan.trim() || undefined,
        mode: effectiveMode,
        horizon,
        since_2010: since2010,
        separate_management: separateManagement,
        investment_amount: parseFloat(investmentAmount),
        funds: fundInputs,
        swp: includeSwp
          ? {
              corpus: overrideSwp ? parseFloat(swpCorpus) : null,
              annual_return: overrideSwp ? parseFloat(swpReturn) / 100 : null,
              inflation: (parseFloat(inflation) || 0) / 100,
              monthly_withdrawal: parseFloat(withdrawal),
              years: parseInt(swpYears, 10) || 10,
            }
          : undefined,
      });
      clearSelection();
      router.replace(`/proposal/${res.proposal_id}` as any);
    } catch (e: any) {
      Alert.alert("Couldn't build proposal", e?.message ?? "Unknown error");
    } finally {
      setSubmitting(false);
    }
  }

  if (funds.length === 0) {
    return (
      <SafeAreaView style={styles.safe} edges={["bottom"]}>
        <View style={styles.emptyWrap}>
          <Inbox size={32} color={colors.textTer} />
          <Text style={styles.emptyTitle}>No funds selected</Text>
          <Text style={styles.emptyBody}>
            Go to the Home tab, tap "Select", pick the funds for this portfolio, then tap "Build Proposal".
          </Text>
          <Pressable style={styles.emptyBtn} onPress={() => router.replace("/home" as any)}>
            <Text style={styles.emptyBtnText}>Go to Home</Text>
          </Pressable>
        </View>
      </SafeAreaView>
    );
  }

  return (
    <SafeAreaView style={styles.safe} edges={["bottom"]}>
      <ScrollView contentContainerStyle={styles.scroll} keyboardShouldPersistTaps="handled">
        <View style={styles.modeBanner}>
          <Text style={styles.modeBannerText}>
            {effectiveMode === "sip" ? "SIP" : "Lumpsum"} proposal · {horizon}Y horizon (from your tenure)
          </Text>
          {(since2010 || separateManagement) && (
            <Text style={[styles.modeBannerText, { marginTop: 4 }]}>
              {since2010 && separateManagement
                ? "Using Since-2010 data, with management-change funds scored from their break date"
                : since2010
                  ? "Using Since-2010 data (as on the Home screen)"
                  : "Using management-change funds' data from their break date (as on the Home screen)"}
            </Text>
          )}
        </View>

        <Text style={styles.sectionTitle}>Client Details</Text>
        <TextInput
          style={styles.input}
          placeholder="Client name (optional)"
          placeholderTextColor={colors.textTer}
          value={clientName}
          onChangeText={setClientName}
        />
        <TextInput
          style={styles.input}
          placeholder="Phone number (optional)"
          placeholderTextColor={colors.textTer}
          keyboardType="phone-pad"
          value={clientPhone}
          onChangeText={setClientPhone}
        />
        <TextInput
          style={styles.input}
          placeholder="PAN (optional)"
          placeholderTextColor={colors.textTer}
          autoCapitalize="characters"
          value={clientPan}
          onChangeText={setClientPan}
        />

        <Text style={styles.sectionTitle}>Recommended By</Text>
        <View style={styles.readonlyField}>
          <Text style={styles.readonlyText}>{RECOMMENDED_BY}</Text>
        </View>

        <Text style={styles.sectionTitle}>Allocation ({funds.length} fund{funds.length > 1 ? "s" : ""} selected)</Text>
        {funds.map((f) => (
          <View key={f.scheme_code} style={styles.fundRow}>
            <Text style={styles.fundName} numberOfLines={2}>{f.scheme_name}</Text>
            <TextInput
              style={styles.allocInput}
              placeholder="%"
              placeholderTextColor={colors.textTer}
              keyboardType="decimal-pad"
              value={f.allocation}
              onChangeText={(v) => setAllocation(f.scheme_code, v)}
            />
            <Pressable onPress={() => removeFund(f.scheme_code)} hitSlop={10}>
              <Trash2 size={16} color={colors.textTer} />
            </Pressable>
          </View>
        ))}
        <Text style={[styles.allocTotal, { color: allocationOk ? colors.azure : colors.ruby }]}>
          Total allocation: {allocationTotal.toFixed(1)}%
        </Text>

        <Text style={styles.sectionTitle}>
          {effectiveMode === "sip" ? "Monthly SIP Amount (₹)" : "Lumpsum Amount (₹)"}
        </Text>
        <TextInput style={styles.input} keyboardType="numeric" value={investmentAmount} onChangeText={setInvestmentAmount} />

        <View style={styles.switchRow}>
          <Text style={styles.sectionTitle}>Withdrawal Plan (SWP) — optional</Text>
          <Switch value={includeSwp} onValueChange={setIncludeSwp} />
        </View>
        {includeSwp && (
          <>
            <View style={styles.row2}>
              <View style={styles.col}>
                <Text style={styles.label}>Monthly withdrawal (₹)</Text>
                <TextInput style={styles.input} keyboardType="numeric" value={withdrawal} onChangeText={setWithdrawal} />
              </View>
              <View style={styles.col}>
                <Text style={styles.label}>Inflation (%)</Text>
                <TextInput style={styles.input} keyboardType="decimal-pad" value={inflation} onChangeText={setInflation} />
              </View>
            </View>
            <View style={styles.row2}>
              <View style={styles.col}>
                <Text style={styles.label}>Withdrawal years</Text>
                <TextInput style={styles.input} keyboardType="numeric" value={swpYears} onChangeText={setSwpYears} />
              </View>
              <View style={styles.col} />
            </View>

            <View style={styles.switchRow}>
              <Text style={styles.label}>Override auto-derived corpus & return</Text>
              <Switch value={overrideSwp} onValueChange={setOverrideSwp} />
            </View>
            {overrideSwp && (
              <View style={styles.row2}>
                <View style={styles.col}>
                  <Text style={styles.label}>Corpus (₹)</Text>
                  <TextInput style={styles.input} keyboardType="numeric" value={swpCorpus} onChangeText={setSwpCorpus} />
                </View>
                <View style={styles.col}>
                  <Text style={styles.label}>Return (%)</Text>
                  <TextInput style={styles.input} keyboardType="decimal-pad" value={swpReturn} onChangeText={setSwpReturn} />
                </View>
              </View>
            )}
            {!overrideSwp && (
              <Text style={styles.hint}>
                Corpus & return will be auto-derived from your {effectiveMode === "sip" ? "SIP" : "Lumpsum"} projection
                at the {horizon}Y horizon. A fund with a shorter track record is shown at its own rolling period
                (marked with *) and its wealth extrapolated to {horizon}Y at that period&apos;s XIRR. If no selected
                fund has a usable rolling period at all, generating will fail with a clear message instead of
                silently using a 0 corpus.
              </Text>
            )}
          </>
        )}

        {error && <Text style={styles.error}>{error}</Text>}

        <Pressable
          style={[styles.cta, submitting && { opacity: 0.7 }]}
          disabled={submitting}
          onPress={onGenerate}
        >
          {submitting ? <ActivityIndicator color="#fff" /> : <Text style={styles.ctaText}>Generate Proposal</Text>}
        </Pressable>
      </ScrollView>
    </SafeAreaView>
  );
}

function makeStyles(colors: ThemeColors, type: ThemeType) {
  return StyleSheet.create({
    safe: { flex: 1, backgroundColor: colors.bg },
    scroll: { padding: space.md, paddingBottom: space.xl * 2, gap: space.xs },
    modeBanner: {
      backgroundColor: colors.azureSoft, borderWidth: 1, borderColor: colors.azure + "40",
      borderRadius: radii.md, paddingVertical: 10, paddingHorizontal: space.md, marginBottom: space.sm,
    },
    modeBannerText: { fontSize: 13, fontWeight: "700", color: colors.azure },
    sectionTitle: {
      fontSize: 13, fontWeight: "700", color: colors.textTer,
      textTransform: "uppercase", letterSpacing: 0.6,
      marginTop: space.lg, marginBottom: space.sm,
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
    switchRow: {
      flexDirection: "row", alignItems: "center", justifyContent: "space-between",
      marginTop: space.sm, marginBottom: space.xs,
    },
    hint: { fontSize: 12, color: colors.textTer, marginTop: 4, marginBottom: space.sm, lineHeight: 17 },
    error: { color: colors.ruby, fontSize: 13, fontWeight: "600", marginTop: space.md },
    cta: {
      marginTop: space.xl, backgroundColor: colors.azure, borderRadius: radii.md,
      paddingVertical: 16, alignItems: "center",
    },
    ctaText: { color: "#fff", fontSize: 16, fontWeight: "700" },
    emptyWrap: { flex: 1, alignItems: "center", justifyContent: "center", padding: space.xl, gap: 10 },
    emptyTitle: { fontSize: 16, fontWeight: "700", color: colors.textPri },
    emptyBody: { fontSize: 13, color: colors.textTer, textAlign: "center", lineHeight: 19, maxWidth: 280 },
    emptyBtn: {
      marginTop: space.md, backgroundColor: colors.azure, borderRadius: radii.md,
      paddingHorizontal: space.lg, paddingVertical: 12,
    },
    emptyBtnText: { color: "#fff", fontWeight: "700", fontSize: 14 },
  });
}
