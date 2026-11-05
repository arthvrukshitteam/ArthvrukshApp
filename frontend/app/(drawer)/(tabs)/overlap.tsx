/**
 * Portfolio Overlap — pick two of the app's funds and see how much of their
 * equity books are literally the same securities, so an advisor can decide
 * which one to keep.
 *
 * The headline number is weight-based (Σ min(weight_a, weight_b) over common
 * stocks, rebased to each fund's equity sleeve), not a count of shared names
 * — see backend/overlap_engine.py.
 *
 * Fund pickers list the app's OWN funds (GET /overlap/candidates), with the
 * ones that have a portfolio disclosure on file first. The old picker was fed
 * only from the holdings store, so on a server where no disclosure had been
 * ingested it was simply empty and search looked broken. A fund without a
 * disclosure can still be picked; the screen then offers to upload the AMC's
 * monthly portfolio workbook, which is parsed and matched to schemes
 * server-side. Nothing here is estimated or made up: no disclosure, no
 * comparison.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  KeyboardAvoidingView,
  Platform,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import * as DocumentPicker from "expo-document-picker";
import Svg, { Circle, Text as SvgText } from "react-native-svg";
import {
  Layers,
  Search,
  X,
  ArrowLeftRight,
  ChevronDown,
  AlertTriangle,
  Info,
  Upload,
  CheckCircle2,
  FileSpreadsheet,
} from "lucide-react-native";
import {
  api,
  type OverlapComparison,
  type OverlapCandidate,
  type OverlapCommonHolding,
  type OverlapUniqueHolding,
  type OverlapSide,
  type OverlapIngestResult,
} from "@/src/api";
import { useTheme } from "@/src/ThemeProvider";
import { radii, space, type ThemeColors } from "@/src/theme";
import { DrawerMenuButton } from "@/src/components/DrawerMenuButton";
import { readPickedFileBase64 } from "@/src/utils/readPickedFile";

// SVG text doesn't inherit the app font on web; without this it falls back
// to the browser's serif default.
const SVG_FONT = Platform.select({ web: "-apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif", default: undefined });

const EXCEL_MIME_TYPES = [
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "application/vnd.ms-excel",
];

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

function lastMonth(): string {
  const d = new Date();
  d.setDate(1);
  d.setMonth(d.getMonth() - 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

// ── Fund picker ───────────────────────────────────────────────────────────────

function FundPickerModal({
  visible,
  excludeCode,
  title,
  onClose,
  onPick,
  refreshKey,
}: {
  visible: boolean;
  excludeCode: number | null;
  title: string;
  onClose: () => void;
  onPick: (f: OverlapCandidate) => void;
  refreshKey: number;
}) {
  const { colors } = useTheme();
  const [query, setQuery] = useState("");
  const [rows, setRows] = useState<OverlapCandidate[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  useEffect(() => {
    if (!visible) return;
    const mine = ++seq.current;
    const t = setTimeout(async () => {
      setLoading(true);
      try {
        const r = await api.overlapCandidates(query);
        if (mine !== seq.current) return;
        setRows(r.results);
        setError(null);
      } catch {
        if (mine !== seq.current) return;
        setError("Couldn't load funds. Check your connection and try again.");
      } finally {
        if (mine === seq.current) setLoading(false);
      }
    }, query ? 250 : 0);
    return () => clearTimeout(t);
  }, [visible, query, refreshKey]);

  const close = () => { setQuery(""); onClose(); };

  const data = useMemo(() => rows.filter((f) => f.scheme_code !== excludeCode), [rows, excludeCode]);
  const firstWithout = data.findIndex((f) => !f.has_holdings);

  return (
    <Modal visible={visible} animationType="slide" onRequestClose={close} transparent statusBarTranslucent>
      <KeyboardAvoidingView
        behavior={Platform.OS === "ios" ? "padding" : "height"}
        style={[s.modalBackdrop, { backgroundColor: colors.backdrop }]}
      >
        <SafeAreaView style={[s.modalSheet, { backgroundColor: colors.bg }]} edges={["bottom"]}>
          <View style={[s.modalHeader, { borderBottomColor: colors.border }]}>
            <Text style={[s.modalTitle, { color: colors.textPri }]}>{title}</Text>
            <Pressable onPress={close} hitSlop={10} accessibilityLabel="Close">
              <X size={22} color={colors.textSec} />
            </Pressable>
          </View>

          <View style={[s.searchBox, { backgroundColor: colors.bgCard, borderColor: colors.border }]}>
            <Search size={16} color={colors.textTer} />
            <TextInput
              value={query}
              onChangeText={setQuery}
              placeholder="Search ArthVruksh funds, e.g. hdfc flexi"
              placeholderTextColor={colors.textTer}
              style={[s.searchInput, { color: colors.textPri }]}
              autoCorrect={false}
              autoCapitalize="none"
              returnKeyType="search"
              accessibilityLabel="Search funds"
            />
            {loading ? <ActivityIndicator size="small" color={colors.azure} /> : query ? (
              <Pressable onPress={() => setQuery("")} hitSlop={10} accessibilityLabel="Clear search">
                <X size={15} color={colors.textTer} />
              </Pressable>
            ) : null}
          </View>

          <FlatList
            data={data}
            keyExtractor={(f) => String(f.scheme_code)}
            keyboardShouldPersistTaps="handled"
            keyboardDismissMode="on-drag"
            initialNumToRender={15}
            ListHeaderComponent={
              data.length && data[0].has_holdings ? (
                <Text style={[s.sectionLabel, { color: colors.textTer }]}>PORTFOLIO DATA ON FILE — READY TO COMPARE</Text>
              ) : null
            }
            ListEmptyComponent={
              loading ? (
                <View style={{ padding: space.lg, alignItems: "center" }}>
                  <ActivityIndicator color={colors.azure} />
                </View>
              ) : (
                <Text style={[s.emptyText, { color: colors.textTer }]}>
                  {error ?? (query ? "No ArthVruksh fund matches that search." : "No funds found.")}
                </Text>
              )
            }
            renderItem={({ item, index }) => (
              <>
                {index === firstWithout && (
                  <Text style={[s.sectionLabel, { color: colors.textTer }]}>
                    OTHER ARTHVRUKSH FUNDS — UPLOAD A DISCLOSURE TO COMPARE
                  </Text>
                )}
                <Pressable
                  onPress={() => { setQuery(""); onPick(item); }}
                  style={({ pressed }) => [
                    s.pickRow,
                    { borderBottomColor: colors.border, opacity: pressed ? 0.6 : 1 },
                  ]}
                >
                  <Text style={[s.pickName, { color: colors.textPri }]} numberOfLines={2}>
                    {item.scheme_name}
                  </Text>
                  <View style={s.pickMetaRow}>
                    {item.has_holdings ? (
                      <Text style={[s.pickBadge, { color: colors.tierElite, backgroundColor: colors.tierElite + "18" }]}>
                        {item.equity_count ?? "—"} stocks · {item.period}
                      </Text>
                    ) : (
                      <Text style={[s.pickBadge, { color: colors.amber, backgroundColor: colors.amber + "18" }]}>
                        No portfolio on file
                      </Text>
                    )}
                    {item.amc ? <Text style={[s.pickMeta, { color: colors.textTer }]} numberOfLines={1}>{item.amc}</Text> : null}
                  </View>
                </Pressable>
              </>
            )}
          />
        </SafeAreaView>
      </KeyboardAvoidingView>
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
  fund: OverlapCandidate | null;
  onPress: () => void;
}) {
  const { colors } = useTheme();
  const accent = slot === "A" ? colors.azure : colors.tierGood;
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={fund ? `Fund ${slot}: ${fund.scheme_name}. Tap to change.` : `Select fund ${slot}`}
      style={({ pressed }) => [
        s.slot,
        {
          backgroundColor: colors.bgCard,
          borderColor: fund ? accent + "70" : colors.border,
          opacity: pressed ? 0.8 : 1,
        },
      ]}
    >
      <View style={s.slotTopRow}>
        <View style={[s.slotBadge, { backgroundColor: accent + "22" }]}>
          <Text style={[s.slotBadgeText, { color: accent }]}>Fund {slot}</Text>
        </View>
        <ChevronDown size={16} color={colors.textTer} />
      </View>
      <Text
        style={[s.slotName, { color: fund ? colors.textPri : colors.textTer }]}
        numberOfLines={3}
      >
        {fund ? fund.scheme_name : `Tap to select fund ${slot}`}
      </Text>
      {fund && (
        fund.has_holdings ? (
          <Text style={[s.slotMeta, { color: colors.textTer }]}>
            {fund.equity_count} equity holdings · {fund.period}
          </Text>
        ) : (
          <Text style={[s.slotMeta, { color: colors.amber, fontWeight: "700" }]}>No portfolio on file</Text>
        )
      )}
    </Pressable>
  );
}

// ── Venn chart ────────────────────────────────────────────────────────────────

function OverlapVenn({ result }: { result: OverlapComparison }) {
  const { colors } = useTheme();
  const W = 300, H = 160, r = 64;
  // Circle spacing follows the weighted overlap: identical books sit on top
  // of each other, unrelated books barely touch.
  const share = Math.max(0, Math.min(100, result.overlap_pct)) / 100;
  const d = Math.max(r * 0.35, Math.min(r * 1.9, 2 * r * (1 - share)));
  const cxA = W / 2 - d / 2, cxB = W / 2 + d / 2, cy = H / 2;
  const colA = colors.azure, colB = colors.tierGood;
  return (
    <View style={[s.card, { backgroundColor: colors.bgCard, borderColor: colors.border, alignItems: "center" }]}>
      <Text style={[s.cardTitle, { color: colors.textPri, alignSelf: "flex-start" }]}>Holdings at a glance</Text>
      <Svg width="100%" height={H} viewBox={`0 0 ${W} ${H}`} accessibilityLabel="Venn diagram of holdings">
        <Circle cx={cxA} cy={cy} r={r} fill={colA} fillOpacity={0.22} stroke={colA} strokeWidth={2} />
        <Circle cx={cxB} cy={cy} r={r} fill={colB} fillOpacity={0.22} stroke={colB} strokeWidth={2} />
        <SvgText x={cxA - r * 0.5} y={cy + 6} fontSize={20} fontWeight="bold" fill={colA} textAnchor="middle" fontFamily={SVG_FONT}>
          {String(result.only_in_a)}
        </SvgText>
        <SvgText x={W / 2} y={cy + 6} fontSize={22} fontWeight="bold" fill={colors.textPri} textAnchor="middle" fontFamily={SVG_FONT}>
          {String(result.common_count)}
        </SvgText>
        <SvgText x={cxB + r * 0.5} y={cy + 6} fontSize={20} fontWeight="bold" fill={colB} textAnchor="middle" fontFamily={SVG_FONT}>
          {String(result.only_in_b)}
        </SvgText>
      </Svg>
      <View style={s.vennLegend}>
        <Text style={[s.vennLegendText, { color: colA }]}>Only in A</Text>
        <Text style={[s.vennLegendText, { color: colors.textPri }]}>In both</Text>
        <Text style={[s.vennLegendText, { color: colB }]}>Only in B</Text>
      </View>

      {result.common_weight_a_equity != null && result.common_weight_b_equity != null && (
        <View style={{ alignSelf: "stretch", marginTop: space.md, gap: 10 }}>
          <Text style={[s.cardHint, { color: colors.textTer, marginTop: 0 }]}>
            Share of each fund&apos;s equity invested in the common stocks
          </Text>
          <ShareBar label="Fund A" value={result.common_weight_a_equity} color={colA} />
          <ShareBar label="Fund B" value={result.common_weight_b_equity} color={colB} />
        </View>
      )}
    </View>
  );
}

function ShareBar({ label, value, color }: { label: string; value: number; color: string }) {
  const { colors } = useTheme();
  return (
    <View>
      <View style={{ flexDirection: "row", justifyContent: "space-between" }}>
        <Text style={{ fontSize: 12, fontWeight: "700", color }}>{label}</Text>
        <Text style={{ fontSize: 12, fontWeight: "700", color: colors.textPri }}>
          {value.toFixed(1)}% in common · {(100 - value).toFixed(1)}% unique
        </Text>
      </View>
      <View style={[s.heroBarTrack, { backgroundColor: colors.bgElev, marginTop: 5, height: 10 }]}>
        <View style={[s.heroBarFill, { backgroundColor: color, width: `${Math.min(100, value)}%` }]} />
      </View>
    </View>
  );
}

// ── Decision support ──────────────────────────────────────────────────────────

// Headline FQS can be read at different horizons for the two funds (each
// fund's own longest of 15/10/5/3Y), so the horizon is shown with it.
function fqsText(f: OverlapSide): string {
  if (f.headline_fqs == null) return "—";
  return `${f.headline_fqs.toFixed(0)} ${f.headline_tier ?? ""}${f.headline_horizon ? ` (${f.headline_horizon}Y)` : ""}`.replace(/\s+/g, " ").trim();
}

function DecisionCard({ result }: { result: OverlapComparison }) {
  const { colors } = useTheme();
  const a = result.fund_a, b = result.fund_b;
  const higher = (x?: number | null, y?: number | null) => (x != null && y != null ? (x > y ? "a" : y > x ? "b" : null) : null);
  const fqsLead = higher(a.headline_fqs, b.headline_fqs);
  const rows: { label: string; a: string; b: string; lead?: "a" | "b" | null }[] = [
    { label: "Fund house", a: a.amc ?? "—", b: b.amc ?? "—" },
    { label: "Category", a: a.category ?? "—", b: b.category ?? "—" },
    {
      label: "FQS (headline)",
      a: fqsText(a),
      b: fqsText(b),
      lead: fqsLead,
    },
    { label: "Equity holdings", a: String(a.equity_count), b: String(b.equity_count) },
    { label: "Top-10 weight", a: pct(a.top10_weight), b: pct(b.top10_weight) },
    { label: "Equity in common stocks", a: pct(result.common_weight_a_equity), b: pct(result.common_weight_b_equity) },
    { label: "Stocks the other fund lacks", a: String(result.only_in_a), b: String(result.only_in_b) },
  ];

  const takeaway =
    result.overlap_pct >= 60
      ? "Holding both adds little diversification, so one of them is usually enough. Weigh the scores above against how much you trust each fund manager and fund house."
      : result.overlap_pct >= 40
        ? "They share a sizeable core but each also holds stocks the other doesn't. Keeping both is defensible if you want both styles; otherwise the rows above show which one carries its weight better."
        : "Most of each portfolio is different, so the two funds complement each other rather than duplicate.";

  return (
    <View style={[s.card, { backgroundColor: colors.bgCard, borderColor: colors.border }]}>
      <Text style={[s.cardTitle, { color: colors.textPri }]}>Which one to keep?</Text>
      <View style={[s.tableHead, { borderBottomColor: colors.border }]}>
        <Text style={[s.thName, { color: colors.textTer }]}> </Text>
        <Text style={[s.thNum, { color: colors.azure, textAlign: "center" }]}>FUND A</Text>
        <Text style={[s.thNum, { color: colors.tierGood, textAlign: "center" }]}>FUND B</Text>
      </View>
      {rows.map((r, i) => (
        <View key={r.label} style={[s.tr, { borderBottomColor: colors.border, backgroundColor: i % 2 ? colors.rowAlt : "transparent" }]}>
          <Text style={[s.tdLabel, { color: colors.textSec }]}>{r.label}</Text>
          <Text style={[s.tdVal, { color: colors.textPri, fontWeight: r.lead === "a" ? "800" : "600" }]} numberOfLines={2}>
            {r.a}{r.lead === "a" ? " ▲" : ""}
          </Text>
          <Text style={[s.tdVal, { color: colors.textPri, fontWeight: r.lead === "b" ? "800" : "600" }]} numberOfLines={2}>
            {r.b}{r.lead === "b" ? " ▲" : ""}
          </Text>
        </View>
      ))}
      <Text style={[s.cardHint, { color: colors.textSec, marginTop: space.sm }]}>{takeaway}</Text>
    </View>
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
  const maxWeight = Math.max(...rows.map((r) => Math.max(r.weight_a, r.weight_b)), 1);

  return (
    <View style={[s.card, { backgroundColor: colors.bgCard, borderColor: colors.border }]}>
      <Text style={[s.cardTitle, { color: colors.textPri }]}>
        Common holdings ({rows.length})
      </Text>
      <Text style={[s.cardHint, { color: colors.textTer }]}>
        Ordered by how much each stock adds to the overlap. Fund A / Fund B are % of each
        fund&apos;s net assets; &quot;Overlap&quot; is the stock&apos;s share of the headline figure.
      </Text>

      <View style={[s.tableHead, { borderBottomColor: colors.border }]}>
        <Text style={[s.thName, { color: colors.textTer }]}>STOCK</Text>
        <Text style={[s.thNum, { color: colors.azure }]}>FUND A</Text>
        <Text style={[s.thNum, { color: colors.tierGood }]}>FUND B</Text>
        <Text style={[s.thNumSm, { color: colors.textTer }]}>OVERLAP</Text>
      </View>

      {shown.map((r, i) => (
        <View
          key={(r.isin || r.name) + i}
          style={[s.tr, { borderBottomColor: colors.border, backgroundColor: i % 2 ? colors.rowAlt : "transparent" }]}
        >
          <View style={s.tdName}>
            <Text style={[s.stockName, { color: colors.textPri }]} numberOfLines={2}>{r.name}</Text>
            {r.matched_on === "name" && (
              <Text style={[s.matchNote, { color: colors.textTer }]}>matched by name</Text>
            )}
          </View>
          <View style={s.tdNum}>
            <Text style={[s.weight, { color: colors.textPri }]}>{pct(r.weight_a, 2)}</Text>
            <View style={[s.barTrack, { backgroundColor: colors.bgElev }]}>
              <View style={[s.barFill, { backgroundColor: colors.azure, width: `${(r.weight_a / maxWeight) * 100}%` }]} />
            </View>
          </View>
          <View style={s.tdNum}>
            <Text style={[s.weight, { color: colors.textPri }]}>{pct(r.weight_b, 2)}</Text>
            <View style={[s.barTrack, { backgroundColor: colors.bgElev }]}>
              <View style={[s.barFill, { backgroundColor: colors.tierGood, width: `${(r.weight_b / maxWeight) * 100}%` }]} />
            </View>
          </View>
          <View style={s.tdNumSm}>
            <Text style={[s.weight, { color: colors.textSec }]}>{pct(r.contribution, 2)}</Text>
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

function UniqueHoldings({
  side, label, rows, color,
}: { side: OverlapSide; label: string; rows: OverlapUniqueHolding[]; color: string }) {
  const { colors } = useTheme();
  const [expanded, setExpanded] = useState(false);
  const shown = expanded ? rows : rows.slice(0, 8);
  return (
    <View style={[s.card, { backgroundColor: colors.bgCard, borderColor: colors.border }]}>
      <Text style={[s.cardTitle, { color: colors.textPri }]}>
        <Text style={{ color }}>{label}</Text> only ({rows.length})
      </Text>
      <Text style={[s.cardHint, { color: colors.textTer }]} numberOfLines={2}>
        Held by {side.scheme_name} but not by the other fund — largest first.
      </Text>
      {rows.length === 0 ? (
        <Text style={[s.cardHint, { color: colors.textSec }]}>Every stock here is also in the other fund.</Text>
      ) : (
        shown.map((r, i) => (
          <View key={(r.isin || r.name) + i} style={[s.uniqueRow, { borderBottomColor: colors.border }]}>
            <Text style={[s.stockName, { color: colors.textPri, flex: 1 }]} numberOfLines={1}>{r.name}</Text>
            <Text style={[s.weight, { color: colors.textSec }]}>{pct(r.weight, 2)}</Text>
          </View>
        ))
      )}
      {rows.length > 8 && (
        <Pressable onPress={() => setExpanded((v) => !v)} style={s.moreBtn}>
          <Text style={[s.moreText, { color: colors.azure }]}>{expanded ? "Show less" : `Show all ${rows.length}`}</Text>
        </Pressable>
      )}
    </View>
  );
}

// ── Disclosure upload ────────────────────────────────────────────────────────

function DisclosureUpload({
  missing, onUploaded,
}: { missing: OverlapCandidate[]; onUploaded: (r: OverlapIngestResult) => void }) {
  const { colors } = useTheme();
  const [period, setPeriod] = useState(lastMonth());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [last, setLast] = useState<OverlapIngestResult | null>(null);
  const periodOk = /^\d{4}-(0[1-9]|1[0-2])$/.test(period);

  async function pickAndUpload() {
    setError(null);
    try {
      const res = await DocumentPicker.getDocumentAsync({ type: EXCEL_MIME_TYPES, copyToCacheDirectory: true, multiple: false });
      if (res.canceled || !res.assets?.length) return;
      const asset = res.assets[0];
      setBusy(true);
      const b64 = await readPickedFileBase64({ uri: asset.uri, file: (asset as any).file });
      const r = await api.overlapIngestUpload(asset.name, b64, period);
      setLast(r);
      onUploaded(r);
    } catch (e: any) {
      setError(String(e?.message ?? e).replace(/^HTTP \d+:\s*/, "").slice(0, 220));
    } finally {
      setBusy(false);
    }
  }

  return (
    <View style={[s.card, { backgroundColor: colors.bgCard, borderColor: colors.amber + "55" }]}>
      <View style={{ flexDirection: "row", gap: 8, alignItems: "center" }}>
        <FileSpreadsheet size={18} color={colors.amber} />
        <Text style={[s.cardTitle, { color: colors.textPri, flex: 1 }]}>Portfolio disclosure needed</Text>
      </View>
      <Text style={[s.cardHint, { color: colors.textSec }]}>
        {missing.length
          ? `${missing.map((m) => m.scheme_name).join(" and ")} ${missing.length === 1 ? "has" : "have"} no portfolio on file yet. `
          : ""}
        Upload the AMC&apos;s monthly portfolio disclosure (.xlsx from the fund house or AMFI website).
        Each sheet is matched to its scheme; anything that can&apos;t be matched is listed, never guessed.
      </Text>
      <View style={{ flexDirection: "row", alignItems: "center", gap: 8, marginTop: space.sm }}>
        <Text style={{ fontSize: 12, fontWeight: "700", color: colors.textSec }}>Disclosure month</Text>
        <TextInput
          value={period}
          onChangeText={setPeriod}
          placeholder="YYYY-MM"
          placeholderTextColor={colors.textTer}
          autoCapitalize="none"
          autoCorrect={false}
          maxLength={7}
          style={[s.periodInput, { color: colors.textPri, borderColor: periodOk ? colors.border : colors.ruby, backgroundColor: colors.bg }]}
          accessibilityLabel="Disclosure month"
        />
      </View>
      <Pressable
        onPress={pickAndUpload}
        disabled={busy || !periodOk}
        style={({ pressed }) => [
          s.compareBtn,
          { backgroundColor: colors.amber, marginTop: space.sm, opacity: busy || !periodOk ? 0.6 : pressed ? 0.85 : 1, flexDirection: "row", gap: 8 },
        ]}
      >
        {busy ? <ActivityIndicator color="#fff" /> : <Upload size={16} color="#fff" />}
        <Text style={[s.compareBtnText, { color: "#fff" }]}>{busy ? "Reading disclosure…" : "Upload disclosure (.xlsx)"}</Text>
      </Pressable>
      {error && <Text style={{ color: colors.ruby, fontSize: 12, marginTop: 8 }}>{error}</Text>}
      {last && (
        <View style={{ marginTop: 10, gap: 4 }}>
          <View style={{ flexDirection: "row", gap: 6, alignItems: "center" }}>
            <CheckCircle2 size={14} color={colors.tierElite} />
            <Text style={{ color: colors.textPri, fontSize: 12.5, fontWeight: "700" }}>
              {last.stored.length} fund portfolio{last.stored.length === 1 ? "" : "s"} saved for {last.period}
            </Text>
          </View>
          {last.unresolved.length > 0 && (
            <Text style={{ color: colors.textTer, fontSize: 12 }}>
              {last.unresolved.length} sheet{last.unresolved.length === 1 ? "" : "s"} couldn&apos;t be matched to a scheme and {last.unresolved.length === 1 ? "was" : "were"} skipped.
            </Text>
          )}
        </View>
      )}
    </View>
  );
}

// ── Screen ────────────────────────────────────────────────────────────────────

export default function PortfolioOverlapScreen() {
  const { colors } = useTheme();

  const [fundA, setFundA] = useState<OverlapCandidate | null>(null);
  const [fundB, setFundB] = useState<OverlapCandidate | null>(null);
  const [picking, setPicking] = useState<"A" | "B" | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [holdingsCount, setHoldingsCount] = useState<number | null>(null);

  const [result, setResult] = useState<OverlapComparison | null>(null);
  const [comparing, setComparing] = useState(false);
  const [compareError, setCompareError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api.overlapCandidates()
      .then((r) => { if (alive) setHoldingsCount(r.with_holdings); })
      .catch(() => { if (alive) setHoldingsCount(null); });
    return () => { alive = false; };
  }, [refreshKey]);

  // Any change of fund invalidates a displayed result — leaving the old
  // comparison on screen under new fund names would be actively misleading.
  const pick = useCallback((slot: "A" | "B", f: OverlapCandidate) => {
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
      setCompareError(String(e?.message || e).replace(/^HTTP \d+:\s*/, "").replace(/^\{"detail":"(.*)"\}$/, "$1"));
    } finally {
      setComparing(false);
    }
  }, [fundA, fundB]);

  // After an upload, re-read both slots' disclosure status from the server.
  const onUploaded = useCallback(async (r: OverlapIngestResult) => {
    setRefreshKey((k) => k + 1);
    const stored = new Map(r.stored.map((x) => [x.scheme_code, x]));
    const refresh = (f: OverlapCandidate | null) => {
      const hit = f ? stored.get(f.scheme_code) : undefined;
      return f && hit ? { ...f, has_holdings: true, period: hit.period, equity_count: hit.equity_count } : f;
    };
    setFundA(refresh);
    setFundB(refresh);
  }, []);

  const band = result ? overlapBand(result.overlap_pct, colors) : null;
  const bothReady = !!fundA?.has_holdings && !!fundB?.has_holdings;
  const canCompare = !!fundA && !!fundB && bothReady && !comparing;
  const missing = [fundA, fundB].filter((f): f is OverlapCandidate => !!f && !f.has_holdings);

  return (
    <SafeAreaView style={[s.screen, { backgroundColor: colors.bg }]} edges={["top"]}>
      <View style={[s.header, { borderBottomColor: colors.border }]}>
        <DrawerMenuButton />
        <Layers size={18} color={colors.azure} />
        <Text style={[s.headerTitle, { color: colors.textPri }]}>Portfolio Overlap</Text>
      </View>

      <ScrollView contentContainerStyle={s.body} keyboardShouldPersistTaps="handled">
        <Text style={[s.intro, { color: colors.textSec }]}>
          Pick two ArthVruksh funds to see which stocks they both own, how much of each fund
          sits in those stocks, and what each holds on its own — so you can decide which one to keep.
        </Text>

        {holdingsCount === 0 && (
          <View style={[s.errorBox, { backgroundColor: colors.bgCard, borderColor: colors.border }]}>
            <Info size={16} color={colors.textTer} />
            <Text style={[s.errorText, { color: colors.textSec }]}>
              No portfolio disclosures are on file yet. Pick two funds and upload their AMC&apos;s
              monthly portfolio workbook below to compare them.
            </Text>
          </View>
        )}

        {/* ── Selection ── */}
        <View style={s.slotRow}>
          <FundSlot slot="A" fund={fundA} onPress={() => setPicking("A")} />
          <Pressable
            onPress={swap}
            disabled={!fundA && !fundB}
            hitSlop={8}
            accessibilityLabel="Swap funds"
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
            <Text style={[s.compareBtnText, { color: canCompare ? "#fff" : colors.textTer }]}>
              {!fundA || !fundB ? "Select two funds to compare" : !bothReady ? "Portfolio data needed to compare" : "Compare portfolios"}
            </Text>
          )}
        </Pressable>

        {missing.length > 0 && <DisclosureUpload missing={missing} onUploaded={onUploaded} />}

        {compareError && (
          <View style={[s.errorBox, { backgroundColor: colors.bgCard, borderColor: colors.ruby + "55" }]}>
            <AlertTriangle size={16} color={colors.ruby} />
            <Text style={[s.errorText, { color: colors.textSec }]}>{compareError}</Text>
          </View>
        )}

        {/* ── Result ── */}
        {result && band && (
          <>
            <View style={[s.heroCard, { backgroundColor: colors.bgCard, borderColor: band.color + "55" }]}>
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
                <View style={[s.heroBarFill, { backgroundColor: band.color, width: `${result.overlap_pct}%` }]} />
              </View>
              <Text style={[s.heroExplain, { color: colors.textTer }]}>
                Weight-based: about {result.overlap_pct.toFixed(0)}% of every rupee in these funds
                is invested in the same securities. {result.common_count} common stock
                {result.common_count === 1 ? "" : "s"} out of {result.fund_a_holdings} in A and {result.fund_b_holdings} in B.
              </Text>
            </View>

            <OverlapVenn result={result} />

            <View style={s.statGrid}>
              <Stat label="Holdings in A" value={String(result.fund_a_holdings)} />
              <Stat label="Holdings in B" value={String(result.fund_b_holdings)} />
              <Stat label="Common stocks" value={String(result.common_count)} />
              <Stat label="Overlap (raw weights)" value={pct(result.overlap_pct_raw)} />
              <Stat label="Overlap by count" value={pct(result.count_overlap_pct)} />
              <Stat label="Equity share A / B" value={`${pct(result.equity_sleeve_a, 0)} / ${pct(result.equity_sleeve_b, 0)}`} />
            </View>

            <DecisionCard result={result} />

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

            {result.unique_a && (
              <UniqueHoldings side={result.fund_a} label="Fund A" rows={result.unique_a} color={colors.azure} />
            )}
            {result.unique_b && (
              <UniqueHoldings side={result.fund_b} label="Fund B" rows={result.unique_b} color={colors.tierGood} />
            )}
          </>
        )}
      </ScrollView>

      <FundPickerModal
        visible={picking !== null}
        excludeCode={picking === "A" ? fundB?.scheme_code ?? null : fundA?.scheme_code ?? null}
        title={`Select fund ${picking ?? ""}`}
        onClose={() => setPicking(null)}
        onPick={(f) => pick(picking!, f)}
        refreshKey={refreshKey}
      />
    </SafeAreaView>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  const { colors } = useTheme();
  return (
    <View style={[s.stat, { backgroundColor: colors.bgCard, borderColor: colors.border }]}>
      <Text style={[s.statValue, { color: colors.textPri }]} numberOfLines={1} adjustsFontSizeToFit>{value}</Text>
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
    paddingTop: space.sm,
    paddingBottom: space.sm,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  headerTitle: { fontSize: 17, fontWeight: "700", letterSpacing: -0.3 },
  body: { padding: space.md, gap: space.md, paddingBottom: space.xl * 2, width: "100%", maxWidth: 820, alignSelf: "center" },
  intro: { fontSize: 13, lineHeight: 19 },

  slotRow: { flexDirection: "row", alignItems: "center", gap: space.sm },
  slot: {
    flex: 1,
    borderWidth: 1.5,
    borderRadius: radii.md,
    padding: space.sm + 2,
    minHeight: 104,
  },
  slotTopRow: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" },
  slotBadge: { paddingHorizontal: 8, paddingVertical: 2, borderRadius: radii.pill },
  slotBadgeText: { fontSize: 10, fontWeight: "700", letterSpacing: 0.4 },
  slotName: { fontSize: 13, fontWeight: "600", marginTop: 8, lineHeight: 17 },
  slotMeta: { fontSize: 10.5, marginTop: 4 },
  swapBtn: { padding: 8, borderRadius: radii.pill, borderWidth: 1 },

  compareBtn: {
    borderRadius: radii.md,
    paddingVertical: 14,
    alignItems: "center",
    justifyContent: "center",
  },
  compareBtnText: { fontSize: 15, fontWeight: "700" },

  errorBox: {
    flexDirection: "row",
    gap: space.sm,
    alignItems: "flex-start",
    borderWidth: 1,
    borderRadius: radii.md,
    padding: space.sm + 4,
  },
  errorText: { flex: 1, fontSize: 12.5, lineHeight: 18 },

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
  heroExplain: { fontSize: 11.5, marginTop: space.sm, textAlign: "center", lineHeight: 16 },

  vennLegend: { flexDirection: "row", justifyContent: "space-around", alignSelf: "stretch" },
  vennLegendText: { fontSize: 11, fontWeight: "700" },

  statGrid: { flexDirection: "row", flexWrap: "wrap", gap: space.sm },
  stat: {
    flexGrow: 1,
    flexBasis: "30%",
    borderWidth: 1,
    borderRadius: radii.md,
    paddingVertical: space.sm + 2,
    paddingHorizontal: 6,
    alignItems: "center",
  },
  statValue: { fontSize: 17, fontWeight: "700" },
  statLabel: { fontSize: 10, marginTop: 2, textAlign: "center" },

  card: { borderWidth: 1, borderRadius: radii.md, padding: space.md },
  cardTitle: { fontSize: 14, fontWeight: "700" },
  cardHint: { fontSize: 11.5, marginTop: 4, lineHeight: 16 },

  tableHead: {
    flexDirection: "row",
    borderBottomWidth: StyleSheet.hairlineWidth,
    paddingBottom: 6,
    marginTop: space.sm + 4,
  },
  thName: { flex: 1.6, fontSize: 10, fontWeight: "700", letterSpacing: 0.4 },
  thNum: { flex: 1, fontSize: 10, fontWeight: "700", letterSpacing: 0.4, textAlign: "right" },
  thNumSm: { flex: 0.8, fontSize: 10, fontWeight: "700", letterSpacing: 0.4, textAlign: "right" },
  tr: { flexDirection: "row", paddingVertical: 8, borderBottomWidth: StyleSheet.hairlineWidth, alignItems: "center" },
  tdName: { flex: 1.6, paddingRight: space.sm },
  tdNum: { flex: 1, alignItems: "flex-end", paddingLeft: 4 },
  tdNumSm: { flex: 0.8, alignItems: "flex-end" },
  tdLabel: { flex: 1.3, fontSize: 12, fontWeight: "600", paddingRight: 6 },
  tdVal: { flex: 1, fontSize: 12, textAlign: "center", paddingHorizontal: 2 },
  stockName: { fontSize: 12, lineHeight: 16 },
  matchNote: { fontSize: 9, marginTop: 2, fontStyle: "italic" },
  weight: { fontSize: 12, fontWeight: "600" },
  barTrack: { height: 4, borderRadius: 2, width: "100%", marginTop: 4, overflow: "hidden" },
  barFill: { height: "100%", borderRadius: 2 },
  uniqueRow: { flexDirection: "row", gap: space.sm, paddingVertical: 7, borderBottomWidth: StyleSheet.hairlineWidth },
  moreBtn: { paddingVertical: space.sm, alignItems: "center" },
  moreText: { fontSize: 12, fontWeight: "600" },
  periodInput: { borderWidth: 1, borderRadius: radii.sm, paddingHorizontal: 10, paddingVertical: 6, fontSize: 14, width: 96, fontWeight: "700" },

  modalBackdrop: { flex: 1, justifyContent: "flex-end" },
  modalSheet: { height: "88%", borderTopLeftRadius: radii.lg, borderTopRightRadius: radii.lg, overflow: "hidden" },
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
  sectionLabel: { fontSize: 10, fontWeight: "800", letterSpacing: 0.7, paddingHorizontal: space.md, paddingTop: space.sm, paddingBottom: 4 },
  pickRow: { paddingHorizontal: space.md, paddingVertical: 12, borderBottomWidth: StyleSheet.hairlineWidth },
  pickName: { fontSize: 13.5, fontWeight: "600", lineHeight: 18 },
  pickMetaRow: { flexDirection: "row", alignItems: "center", gap: 8, marginTop: 5, flexWrap: "wrap" },
  pickBadge: { fontSize: 10.5, fontWeight: "700", paddingHorizontal: 7, paddingVertical: 2, borderRadius: radii.pill, overflow: "hidden" },
  pickMeta: { fontSize: 11, flexShrink: 1 },
  emptyText: { textAlign: "center", padding: space.lg, fontSize: 13 },
});
