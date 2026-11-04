/**
 * Portfolio Overlap — pick two funds, see how much of their equity book is
 * literally the same securities.
 *
 * The headline number is weight-based (Σ min(weight_a, weight_b) over common
 * stocks, rebased to each fund's equity sleeve), not a count of shared names.
 * See backend/overlap_engine.py for why. The UI leans into that: the common-
 * holdings list is ordered by each stock's CONTRIBUTION to the headline, and
 * every row shows both funds' weights side by side, so a user can see which
 * few positions are actually driving the score instead of reading a long
 * alphabetical list of names.
 *
 * Fund pickers are populated from /overlap/funds — only funds with a
 * disclosure on file — because offering a fund we have no portfolio for can
 * only end in an error one tap later.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  View,
  Text,
  StyleSheet,
  Pressable,
  ScrollView,
  ActivityIndicator,
  Modal,
  TextInput,
  FlatList,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import {
  Layers,
  Search,
  X,
  ArrowLeftRight,
  ChevronDown,
  AlertTriangle,
  Info,
} from "lucide-react-native";
import {
  api,
  type OverlapComparison,
  type OverlapFund,
  type OverlapCommonHolding,
} from "@/src/api";
import { useTheme } from "@/src/ThemeProvider";
import { radii, space, type ThemeColors } from "@/src/theme";
import { DrawerMenuButton } from "@/src/components/DrawerMenuButton";

// Overlap bands. Chosen to match how an advisor actually talks about this:
// under ~30% the funds are genuinely diversifying each other; over ~60% the
// second fund is mostly buying what the first already owns.
function overlapBand(pct: number, c: ThemeColors) {
  if (pct >= 75) return { label: "Very high", color: c.tierWeak,   note: "These two are close to the same fund." };
  if (pct >= 60) return { label: "High",      color: c.tierAvg,    note: "Heavy duplication — limited diversification benefit." };
  if (pct >= 40) return { label: "Moderate",  color: c.tierGood,   note: "Meaningful shared core, some differentiation." };
  if (pct >= 20) return { label: "Low",       color: c.tierStrong, note: "Mostly different books." };
  return           { label: "Very low",  color: c.tierElite,  note: "These funds diversify each other well." };
}

function pct(n: number | null | undefined, dp = 1) {
  return n === null || n === undefined ? "—" : `${n.toFixed(dp)}%`;
}

// ── Fund picker ───────────────────────────────────────────────────────────────

function FundPickerModal({
  visible,
  funds,
  excludeCode,
  title,
  onClose,
  onPick,
}: {
  visible: boolean;
  funds: OverlapFund[];
  excludeCode: number | null;
  title: string;
  onClose: () => void;
  onPick: (f: OverlapFund) => void;
}) {
  const { colors } = useTheme();
  const [query, setQuery] = useState("");

  const results = useMemo(() => {
    const q = query.trim().toLowerCase();
    return funds
      .filter((f) => f.scheme_code !== excludeCode)
      .filter((f) => !q || f.scheme_name.toLowerCase().includes(q));
  }, [funds, excludeCode, query]);

  return (
    <Modal visible={visible} animationType="slide" onRequestClose={onClose} transparent>
      <View style={[s.modalBackdrop, { backgroundColor: colors.backdrop }]}>
        <SafeAreaView style={[s.modalSheet, { backgroundColor: colors.bg }]} edges={["bottom"]}>
          <View style={[s.modalHeader, { borderBottomColor: colors.border }]}>
            <Text style={[s.modalTitle, { color: colors.textPri }]}>{title}</Text>
            <Pressable onPress={onClose} hitSlop={10}>
              <X size={22} color={colors.textSec} />
            </Pressable>
          </View>

          <View style={[s.searchBox, { backgroundColor: colors.bgCard, borderColor: colors.border }]}>
            <Search size={16} color={colors.textTer} />
            <TextInput
              value={query}
              onChangeText={setQuery}
              placeholder="Search funds with portfolio data"
              placeholderTextColor={colors.textTer}
              style={[s.searchInput, { color: colors.textPri }]}
              autoCorrect={false}
            />
          </View>

          <FlatList
            data={results}
            keyExtractor={(f) => String(f.scheme_code)}
            keyboardShouldPersistTaps="handled"
            ListEmptyComponent={
              <Text style={[s.emptyText, { color: colors.textTer }]}>
                {funds.length === 0
                  ? "No funds have portfolio disclosures on file yet."
                  : "No fund matches that search."}
              </Text>
            }
            renderItem={({ item }) => (
              <Pressable
                onPress={() => {
                  onPick(item);
                  setQuery("");
                }}
                style={({ pressed }) => [
                  s.pickRow,
                  { borderBottomColor: colors.border, opacity: pressed ? 0.6 : 1 },
                ]}
              >
                <Text style={[s.pickName, { color: colors.textPri }]} numberOfLines={2}>
                  {item.scheme_name}
                </Text>
                <Text style={[s.pickMeta, { color: colors.textTer }]}>
                  {item.equity_count} holdings · {item.period}
                  {item.amc ? ` · ${item.amc}` : ""}
                </Text>
              </Pressable>
            )}
          />
        </SafeAreaView>
      </View>
    </Modal>
  );
}

// ── Selector button ───────────────────────────────────────────────────────────

function FundSlot({
  slot,
  fund,
  onPress,
}: {
  slot: "A" | "B";
  fund: OverlapFund | null;
  onPress: () => void;
}) {
  const { colors } = useTheme();
  return (
    <Pressable
      onPress={onPress}
      style={({ pressed }) => [
        s.slot,
        {
          backgroundColor: colors.bgCard,
          borderColor: fund ? colors.azure + "60" : colors.border,
          opacity: pressed ? 0.8 : 1,
        },
      ]}
    >
      <View style={s.slotTopRow}>
        <View style={[s.slotBadge, { backgroundColor: colors.azureSoft }]}>
          <Text style={[s.slotBadgeText, { color: colors.azure }]}>Fund {slot}</Text>
        </View>
        <ChevronDown size={16} color={colors.textTer} />
      </View>
      <Text
        style={[s.slotName, { color: fund ? colors.textPri : colors.textTer }]}
        numberOfLines={2}
      >
        {fund ? fund.scheme_name : `Select fund ${slot}`}
      </Text>
      {fund && (
        <Text style={[s.slotMeta, { color: colors.textTer }]}>
          {fund.equity_count} equity holdings · {fund.period}
        </Text>
      )}
    </Pressable>
  );
}

// ── Common holdings table ─────────────────────────────────────────────────────

function HoldingsTable({ rows }: { rows: OverlapCommonHolding[] }) {
  const { colors } = useTheme();
  const [expanded, setExpanded] = useState(false);
  const shown = expanded ? rows : rows.slice(0, 15);

  // Bar widths are scaled against the largest weight in view, not against
  // 100 — at typical weights (0.5–9%) a 100-scale bar is a sliver and the
  // comparison the row exists to make becomes invisible.
  const maxWeight = Math.max(
    ...rows.map((r) => Math.max(r.weight_a, r.weight_b)),
    1,
  );

  return (
    <View style={[s.card, { backgroundColor: colors.bgCard, borderColor: colors.border }]}>
      <Text style={[s.cardTitle, { color: colors.textPri }]}>
        Common holdings ({rows.length})
      </Text>
      <Text style={[s.cardHint, { color: colors.textTer }]}>
        Ordered by how much each stock contributes to the overlap. Weights are
        % of each fund&apos;s net assets.
      </Text>

      <View style={[s.tableHead, { borderBottomColor: colors.border }]}>
        <Text style={[s.thName, { color: colors.textTer }]}>Stock</Text>
        <Text style={[s.thNum, { color: colors.textTer }]}>Fund A</Text>
        <Text style={[s.thNum, { color: colors.textTer }]}>Fund B</Text>
      </View>

      {shown.map((r, i) => (
        <View
          key={(r.isin || r.name) + i}
          style={[
            s.tr,
            {
              borderBottomColor: colors.border,
              backgroundColor: i % 2 ? colors.rowAlt : "transparent",
            },
          ]}
        >
          <View style={s.tdName}>
            <Text style={[s.stockName, { color: colors.textPri }]} numberOfLines={2}>
              {r.name}
            </Text>
            {r.matched_on === "name" && (
              <Text style={[s.matchNote, { color: colors.textTer }]}>matched by name</Text>
            )}
          </View>
          <View style={s.tdNum}>
            <Text style={[s.weight, { color: colors.textPri }]}>{pct(r.weight_a, 2)}</Text>
            <View style={[s.barTrack, { backgroundColor: colors.bgElev }]}>
              <View
                style={[
                  s.barFill,
                  { backgroundColor: colors.azure, width: `${(r.weight_a / maxWeight) * 100}%` },
                ]}
              />
            </View>
          </View>
          <View style={s.tdNum}>
            <Text style={[s.weight, { color: colors.textPri }]}>{pct(r.weight_b, 2)}</Text>
            <View style={[s.barTrack, { backgroundColor: colors.bgElev }]}>
              <View
                style={[
                  s.barFill,
                  { backgroundColor: colors.tierGood, width: `${(r.weight_b / maxWeight) * 100}%` },
                ]}
              />
            </View>
          </View>
        </View>
      ))}

      {rows.length > 15 && (
        <Pressable onPress={() => setExpanded((v) => !v)} style={s.moreBtn}>
          <Text style={[s.moreText, { color: colors.azure }]}>
            {expanded ? "Show less" : `Show all ${rows.length}`}
          </Text>
        </Pressable>
      )}
    </View>
  );
}

// ── Screen ────────────────────────────────────────────────────────────────────

export default function PortfolioOverlapScreen() {
  const { colors } = useTheme();

  const [funds, setFunds] = useState<OverlapFund[]>([]);
  const [fundsLoading, setFundsLoading] = useState(true);
  const [fundsError, setFundsError] = useState<string | null>(null);

  const [fundA, setFundA] = useState<OverlapFund | null>(null);
  const [fundB, setFundB] = useState<OverlapFund | null>(null);
  const [picking, setPicking] = useState<"A" | "B" | null>(null);

  const [result, setResult] = useState<OverlapComparison | null>(null);
  const [comparing, setComparing] = useState(false);
  const [compareError, setCompareError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .overlapFunds()
      .then((r) => alive && (setFunds(r.funds), setFundsError(null)))
      .catch((e) => alive && setFundsError(String(e?.message || e)))
      .finally(() => alive && setFundsLoading(false));
    return () => {
      alive = false;
    };
  }, []);

  // Any change of fund invalidates a displayed result — leaving the old
  // comparison on screen under new fund names would be actively misleading.
  const pick = useCallback((slot: "A" | "B", f: OverlapFund) => {
    (slot === "A" ? setFundA : setFundB)(f);
    setResult(null);
    setCompareError(null);
    setPicking(null);
  }, []);

  const swap = useCallback(() => {
    setFundA(fundB);
    setFundB(fundA);
    setResult(null);
    setCompareError(null);
  }, [fundA, fundB]);

  const compare = useCallback(async () => {
    if (!fundA || !fundB) return;
    setComparing(true);
    setCompareError(null);
    try {
      setResult(await api.overlapCompare(fundA.scheme_code, fundB.scheme_code));
    } catch (e: any) {
      setResult(null);
      setCompareError(String(e?.message || e));
    } finally {
      setComparing(false);
    }
  }, [fundA, fundB]);

  const band = result ? overlapBand(result.overlap_pct, colors) : null;
  const canCompare = !!fundA && !!fundB && !comparing;

  return (
    <SafeAreaView style={[s.screen, { backgroundColor: colors.bg }]} edges={["top"]}>
      <View style={[s.header, { borderBottomColor: colors.border }]}>
        <DrawerMenuButton />
        <Layers size={18} color={colors.azure} />
        <Text style={[s.headerTitle, { color: colors.textPri }]}>Portfolio Overlap</Text>
      </View>

      <ScrollView contentContainerStyle={s.body} keyboardShouldPersistTaps="handled">
        <Text style={[s.intro, { color: colors.textSec }]}>
          Compare two funds&apos; equity portfolios to see how much they actually
          hold in common — even when their names and stated strategies differ.
        </Text>

        {/* ── Selection ── */}
        <View style={s.slotRow}>
          <FundSlot slot="A" fund={fundA} onPress={() => setPicking("A")} />
          <Pressable
            onPress={swap}
            disabled={!fundA && !fundB}
            hitSlop={8}
            style={({ pressed }) => [
              s.swapBtn,
              {
                backgroundColor: colors.bgElev,
                borderColor: colors.border,
                opacity: pressed ? 0.6 : !fundA && !fundB ? 0.4 : 1,
              },
            ]}
          >
            <ArrowLeftRight size={16} color={colors.textSec} />
          </Pressable>
          <FundSlot slot="B" fund={fundB} onPress={() => setPicking("B")} />
        </View>

        <Pressable
          onPress={compare}
          disabled={!canCompare}
          style={({ pressed }) => [
            s.compareBtn,
            {
              backgroundColor: canCompare ? colors.azure : colors.bgElev,
              opacity: pressed && canCompare ? 0.85 : 1,
            },
          ]}
        >
          {comparing ? (
            <ActivityIndicator color="#fff" />
          ) : (
            <Text
              style={[
                s.compareBtnText,
                { color: canCompare ? "#fff" : colors.textTer },
              ]}
            >
              Compare portfolios
            </Text>
          )}
        </Pressable>

        {fundsLoading && (
          <View style={s.centered}>
            <ActivityIndicator color={colors.azure} />
          </View>
        )}

        {(fundsError || compareError) && (
          <View
            style={[
              s.errorBox,
              { backgroundColor: colors.bgCard, borderColor: colors.ruby + "55" },
            ]}
          >
            <AlertTriangle size={16} color={colors.ruby} />
            <Text style={[s.errorText, { color: colors.textSec }]}>
              {fundsError || compareError}
            </Text>
          </View>
        )}

        {!fundsLoading && !fundsError && funds.length === 0 && (
          <View style={[s.errorBox, { backgroundColor: colors.bgCard, borderColor: colors.border }]}>
            <Info size={16} color={colors.textTer} />
            <Text style={[s.errorText, { color: colors.textSec }]}>
              No portfolio disclosures have been ingested yet. Once monthly AMC
              disclosures are loaded, funds will appear here.
            </Text>
          </View>
        )}

        {/* ── Result ── */}
        {result && band && (
          <>
            <View
              style={[
                s.heroCard,
                { backgroundColor: colors.bgCard, borderColor: band.color + "55" },
              ]}
            >
              <Text style={[s.heroLabel, { color: colors.textTer }]}>
                PORTFOLIO OVERLAP · {result.period}
              </Text>
              <Text style={[s.heroValue, { color: band.color }]}>
                {result.overlap_pct.toFixed(1)}%
              </Text>
              <View style={[s.bandPill, { backgroundColor: band.color + "22" }]}>
                <Text style={[s.bandText, { color: band.color }]}>{band.label}</Text>
              </View>
              <Text style={[s.heroNote, { color: colors.textSec }]}>{band.note}</Text>
              <View style={[s.heroBarTrack, { backgroundColor: colors.bgElev }]}>
                <View
                  style={[
                    s.heroBarFill,
                    { backgroundColor: band.color, width: `${result.overlap_pct}%` },
                  ]}
                />
              </View>
              <Text style={[s.heroExplain, { color: colors.textTer }]}>
                Weight-based: about {result.overlap_pct.toFixed(0)}% of every rupee
                in these funds is invested in the same securities.
              </Text>
            </View>

            <View style={s.statGrid}>
              <Stat label="Holdings in A" value={String(result.fund_a_holdings)} />
              <Stat label="Holdings in B" value={String(result.fund_b_holdings)} />
              <Stat label="Common stocks" value={String(result.common_count)} />
              <Stat label="Only in A" value={String(result.only_in_a)} />
              <Stat label="Only in B" value={String(result.only_in_b)} />
              <Stat label="By count" value={pct(result.count_overlap_pct)} />
            </View>

            <View style={[s.card, { backgroundColor: colors.bgCard, borderColor: colors.border }]}>
              <Text style={[s.cardTitle, { color: colors.textPri }]}>Funds compared</Text>
              {[result.fund_a, result.fund_b].map((f, i) => (
                <View key={f.scheme_code} style={s.fundLine}>
                  <View
                    style={[
                      s.fundDot,
                      { backgroundColor: i === 0 ? colors.azure : colors.tierGood },
                    ]}
                  />
                  <View style={{ flex: 1 }}>
                    <Text style={[s.fundLineName, { color: colors.textPri }]}>
                      {f.scheme_name}
                    </Text>
                    <Text style={[s.fundLineMeta, { color: colors.textTer }]}>
                      {f.equity_count} equity holdings · {pct(f.equity_weight)} of net assets in equity
                    </Text>
                  </View>
                </View>
              ))}
            </View>

            {result.common.length > 0 ? (
              <HoldingsTable rows={result.common} />
            ) : (
              <View style={[s.card, { backgroundColor: colors.bgCard, borderColor: colors.border }]}>
                <Text style={[s.cardTitle, { color: colors.textPri }]}>No common holdings</Text>
                <Text style={[s.cardHint, { color: colors.textTer }]}>
                  These two funds share no equity positions in {result.period}.
                </Text>
              </View>
            )}
          </>
        )}
      </ScrollView>

      <FundPickerModal
        visible={picking !== null}
        funds={funds}
        excludeCode={picking === "A" ? fundB?.scheme_code ?? null : fundA?.scheme_code ?? null}
        title={`Select fund ${picking ?? ""}`}
        onClose={() => setPicking(null)}
        onPick={(f) => pick(picking!, f)}
      />
    </SafeAreaView>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  const { colors } = useTheme();
  return (
    <View style={[s.stat, { backgroundColor: colors.bgCard, borderColor: colors.border }]}>
      <Text style={[s.statValue, { color: colors.textPri }]}>{value}</Text>
      <Text style={[s.statLabel, { color: colors.textTer }]}>{label}</Text>
    </View>
  );
}

const s = StyleSheet.create({
  screen: { flex: 1 },
  header: {
    flexDirection: "row",
    alignItems: "center",
    gap: space.sm,
    paddingHorizontal: space.md,
    paddingBottom: space.sm,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  headerTitle: { fontSize: 17, fontWeight: "700", letterSpacing: -0.3 },
  body: { padding: space.md, gap: space.md, paddingBottom: space.xl },
  intro: { fontSize: 13, lineHeight: 19 },

  slotRow: { flexDirection: "row", alignItems: "center", gap: space.sm },
  slot: {
    flex: 1,
    borderWidth: 1,
    borderRadius: radii.md,
    padding: space.sm + 2,
    minHeight: 96,
  },
  slotTopRow: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" },
  slotBadge: { paddingHorizontal: 8, paddingVertical: 2, borderRadius: radii.pill },
  slotBadgeText: { fontSize: 10, fontWeight: "700", letterSpacing: 0.4 },
  slotName: { fontSize: 13, fontWeight: "600", marginTop: 8, lineHeight: 17 },
  slotMeta: { fontSize: 10, marginTop: 4 },
  swapBtn: { padding: 8, borderRadius: radii.pill, borderWidth: 1 },

  compareBtn: {
    borderRadius: radii.md,
    paddingVertical: 14,
    alignItems: "center",
    justifyContent: "center",
  },
  compareBtnText: { fontSize: 15, fontWeight: "700" },

  centered: { paddingVertical: space.lg, alignItems: "center" },
  errorBox: {
    flexDirection: "row",
    gap: space.sm,
    alignItems: "flex-start",
    borderWidth: 1,
    borderRadius: radii.md,
    padding: space.sm + 4,
  },
  errorText: { flex: 1, fontSize: 12, lineHeight: 18 },

  heroCard: { borderWidth: 1, borderRadius: radii.lg, padding: space.md, alignItems: "center" },
  heroLabel: { fontSize: 10, fontWeight: "700", letterSpacing: 0.8 },
  heroValue: { fontSize: 46, fontWeight: "700", letterSpacing: -1.5, marginTop: 4 },
  bandPill: { paddingHorizontal: 12, paddingVertical: 4, borderRadius: radii.pill, marginTop: 4 },
  bandText: { fontSize: 12, fontWeight: "700" },
  heroNote: { fontSize: 12, marginTop: 8, textAlign: "center" },
  heroBarTrack: {
    height: 8,
    borderRadius: radii.pill,
    width: "100%",
    marginTop: space.sm + 4,
    overflow: "hidden",
  },
  heroBarFill: { height: "100%", borderRadius: radii.pill },
  heroExplain: { fontSize: 11, marginTop: space.sm, textAlign: "center", lineHeight: 16 },

  statGrid: { flexDirection: "row", flexWrap: "wrap", gap: space.sm },
  stat: {
    flexGrow: 1,
    flexBasis: "30%",
    borderWidth: 1,
    borderRadius: radii.md,
    paddingVertical: space.sm + 2,
    alignItems: "center",
  },
  statValue: { fontSize: 18, fontWeight: "700" },
  statLabel: { fontSize: 10, marginTop: 2, textAlign: "center" },

  card: { borderWidth: 1, borderRadius: radii.md, padding: space.md },
  cardTitle: { fontSize: 14, fontWeight: "700" },
  cardHint: { fontSize: 11, marginTop: 4, lineHeight: 16 },

  fundLine: { flexDirection: "row", gap: space.sm, marginTop: space.sm, alignItems: "flex-start" },
  fundDot: { width: 10, height: 10, borderRadius: 5, marginTop: 4 },
  fundLineName: { fontSize: 13, fontWeight: "600", lineHeight: 18 },
  fundLineMeta: { fontSize: 11, marginTop: 2 },

  tableHead: {
    flexDirection: "row",
    borderBottomWidth: StyleSheet.hairlineWidth,
    paddingBottom: 6,
    marginTop: space.sm + 4,
  },
  thName: { flex: 1.6, fontSize: 10, fontWeight: "700", letterSpacing: 0.4 },
  thNum: { flex: 1, fontSize: 10, fontWeight: "700", letterSpacing: 0.4, textAlign: "right" },
  tr: { flexDirection: "row", paddingVertical: 8, borderBottomWidth: StyleSheet.hairlineWidth },
  tdName: { flex: 1.6, paddingRight: space.sm },
  tdNum: { flex: 1, alignItems: "flex-end" },
  stockName: { fontSize: 12, lineHeight: 16 },
  matchNote: { fontSize: 9, marginTop: 2, fontStyle: "italic" },
  weight: { fontSize: 12, fontWeight: "600" },
  barTrack: { height: 4, borderRadius: 2, width: "100%", marginTop: 4, overflow: "hidden" },
  barFill: { height: "100%", borderRadius: 2 },
  moreBtn: { paddingVertical: space.sm, alignItems: "center" },
  moreText: { fontSize: 12, fontWeight: "600" },

  modalBackdrop: { flex: 1, justifyContent: "flex-end" },
  modalSheet: { height: "85%", borderTopLeftRadius: radii.lg, borderTopRightRadius: radii.lg },
  modalHeader: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    padding: space.md,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  modalTitle: { fontSize: 16, fontWeight: "700" },
  searchBox: {
    flexDirection: "row",
    alignItems: "center",
    gap: space.sm,
    margin: space.md,
    paddingHorizontal: space.sm + 4,
    paddingVertical: 10,
    borderWidth: 1,
    borderRadius: radii.md,
  },
  searchInput: { flex: 1, fontSize: 14, padding: 0 },
  pickRow: { paddingHorizontal: space.md, paddingVertical: 12, borderBottomWidth: StyleSheet.hairlineWidth },
  pickName: { fontSize: 13, fontWeight: "600", lineHeight: 18 },
  pickMeta: { fontSize: 11, marginTop: 3 },
  emptyText: { textAlign: "center", padding: space.lg, fontSize: 13 },
});
