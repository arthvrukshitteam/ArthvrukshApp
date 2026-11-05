/**
 * Home Tab — Fund Results Screen
 *
 * ONE screen (UniverseScreen) serves both investment modes. The UI is
 * identical; `mode` selects only the endpoint, the WM ↔ XIRR formula and the
 * header's mode label.
 *
 * SIP mode:     api.universe("sip", tenure)     → /home/universe?years=N
 * Lumpsum mode: api.universe("lumpsum", tenure) → /lumpsum/universe?tenure=N
 *               (supports all rolling periods 1–50Y, same as SIP)
 *
 * Both endpoints return the same `UniverseResponse` shape — see
 * _universe_metrics() in backend/server.py.
 *
 * Fund card metrics (same for both modes):
 *   Row 1: Blended WM | FQS Score | Success %
 *   Row 2: N Windows  | CV (color-coded by consistency band)
 *
 * CV color bands:
 *   < 10% → #10B981 Exceptional Consistency
 *   10–20% → #34D399 High Consistency
 *   20–30% → #F59E0B Moderate Consistency
 *   30–40% → #F97316 High Variability
 *   > 40%  → #EF4444 Very Timing-Dependent
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  View,
  Text,
  TextInput,
  StyleSheet,
  FlatList,
  Pressable,
  RefreshControl,
  ScrollView,
  ActivityIndicator,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { LinearGradient } from "expo-linear-gradient";
import { useRouter } from "expo-router";
import {
  ArrowRight,
  AlertTriangle,
  SlidersHorizontal,
  Info,
  TreePine,
  ChevronRight,
  Target,
  X,
  CheckCircle2,
  XCircle,
  CheckSquare,
  Square,
  Briefcase,
  Search,
  History,
  GitBranch,
  Clock,
} from "lucide-react-native";
import {
  api,
  invalidateApiCache,
  type EligibleFund,
  type UniverseResponse,
  type BonusFund,
  type AmcBreak,
} from "@/src/api";
import { amcPeriodLabel } from "@/src/components/FundDetailShared";
import { useTheme } from "@/src/ThemeProvider";
import { useMode } from "@/src/ModeProvider";
import { useTenure } from "@/src/TenureProvider";
import { useProposalDraft } from "@/src/ProposalDraftProvider";
import { radii, space, type ThemeColors } from "@/src/theme";
import { DrawerMenuButton } from "@/src/components/DrawerMenuButton";
import { formatDataAge } from "@/src/utils/format";
import {
  sipWmToXirr, lumpsumWmToXirr, fmtXirr,
  sipXirrToMinWm, lumpsumXirrToMinWm,
} from "@/src/utils/xirr";

// ── Tier colour map ───────────────────────────────────────────────────────────

const TIER_COLORS: Record<string, string> = {
  Elite:   "#10B981",
  Strong:  "#3B82F6",
  Good:    "#F59E0B",
  Average: "#F97316",
  Weak:    "#EF4444",
};

// ── CV color coding (Coefficient of Variation as %) ──────────────────────────
// cv is stored as a 0–1 decimal; multiply by 100 for percentage

function cvColor(cv: number | null): string {
  if (cv == null) return "#94A3B8";
  const pct = cv * 100;
  if (pct < 10) return "#10B981";  // Exceptional Consistency
  if (pct < 20) return "#34D399";  // High Consistency
  if (pct < 30) return "#F59E0B";  // Moderate Consistency
  if (pct < 40) return "#F97316";  // High Variability
  return "#EF4444";                // Very Timing-Dependent
}

function cvLabel(cv: number | null): string {
  if (cv == null) return "—";
  return `${(cv * 100).toFixed(1)}%`;
}

/** "2018-04-12" → "Apr 2018" — used by the "Separate Management Periods"
 *  toggle's fund-card tag, mirroring the extrapolated-fund footnote pattern
 *  already used in the Proposal PDF. Never silently truncate a fund's
 *  history without saying why. */
function fmtSince(dateStr: string | null | undefined): string | null {
  if (!dateStr) return null;
  const d = new Date(dateStr);
  if (isNaN(d.getTime())) return null;
  return `since ${d.toLocaleDateString("en-US", { month: "short", year: "numeric" })}`;
}

// ── AMC period choice (Management toggle on) ─────────────────────────────────
//
// A fund whose fund house changed hands (several times, for some) can be
// judged from any of those changes: e.g. 2018–2026, 2016–2026 or 2010–2026.
// The nightly batch only precomputes the latest break, so picking another
// period recomputes THAT card live from the fund's NAV history starting at
// that date — real recalculation, not a relabel — via the same analyze
// endpoint Fund Detail uses for its start-date filter.

const FULL_HISTORY = "full";

type PeriodOption = { value: string; label: string };

/** The subset of a card's numbers that a period recompute replaces. */
type CardStats = {
  blended_wm: number | null;
  fqs: number | null;
  fqs_tier: string | null;
  success_pct: number | null;
  n_windows: number | null;
  cv: number | null;
  p10_wm: number | null;
};

type PeriodCalc = { loading: boolean; stats: CardStats | null; error: string | null; noData: boolean };

function tierFor(fqs: number | null | undefined): string | null {
  if (fqs == null) return null;
  if (fqs >= 90) return "Elite";
  if (fqs >= 80) return "Strong";
  if (fqs >= 70) return "Good";
  if (fqs >= 60) return "Average";
  return "Weak";
}

/** Newest break first, then the fund's server-default break if it isn't one
 *  of them, then full history. Null when there's no choice to offer. */
function periodOptionsFor(
  breaks: AmcBreak[] | null | undefined,
  continuityDate: string | null | undefined,
): PeriodOption[] | null {
  const dates = (breaks ?? []).map((b) => b.date);
  if (continuityDate && !dates.includes(continuityDate)) dates.unshift(continuityDate);
  if (!dates.length) return null;
  const uniq = [...new Set(dates)].sort().reverse();
  return [
    ...uniq.map((d) => ({ value: d, label: amcPeriodLabel(d) })),
    { value: FULL_HISTORY, label: "All years" },
  ];
}

function PeriodPicker({
  options, active, onSelect, busy, note, colors,
}: {
  options: PeriodOption[];
  active: string;
  onSelect: (value: string) => void;
  busy: boolean;
  note: string | null;
  colors: ThemeColors;
}) {
  return (
    <View style={periodS.wrap}>
      <View style={periodS.head}>
        <GitBranch size={11} color={colors.textTer} />
        <Text style={[periodS.title, { color: colors.textTer }]}>MANAGEMENT PERIOD</Text>
        {busy && <ActivityIndicator size="small" color={colors.azure} style={{ marginLeft: 4 }} />}
      </View>
      <View style={periodS.row}>
        {options.map((o) => {
          const on = o.value === active;
          return (
            <Pressable
              key={o.value}
              onPress={() => onSelect(o.value)}
              hitSlop={4}
              accessibilityRole="radio"
              accessibilityState={{ selected: on }}
              accessibilityLabel={o.value === FULL_HISTORY ? "Score on all years" : `Score from ${o.value}`}
              style={({ pressed }) => [
                periodS.chip,
                {
                  borderColor: on ? colors.azure : colors.border,
                  backgroundColor: on ? colors.azureSoft : colors.bgElev,
                  opacity: pressed ? 0.75 : 1,
                },
              ]}
            >
              <Text style={[periodS.chipText, { color: on ? colors.azure : colors.textSec }]}>{o.label}</Text>
            </Pressable>
          );
        })}
      </View>
      {note ? <Text style={[periodS.note, { color: colors.amber }]}>{note}</Text> : null}
    </View>
  );
}

const periodS = StyleSheet.create({
  wrap: { marginBottom: 12, gap: 6 },
  head: { flexDirection: "row", alignItems: "center", gap: 5 },
  title: { fontSize: 9.5, fontWeight: "800", letterSpacing: 0.8 },
  row: { flexDirection: "row", flexWrap: "wrap", gap: 6 },
  chip: { paddingHorizontal: 10, paddingVertical: 5, borderRadius: radii.pill, borderWidth: 1.2 },
  chipText: { fontSize: 11.5, fontWeight: "700", fontVariant: ["tabular-nums"] },
  note: { fontSize: 11, fontWeight: "600", lineHeight: 15 },
});

// ── Skeleton loading card ─────────────────────────────────────────────────────

function SkeletonCard({ colors }: { colors: ThemeColors }) {
  const bg = colors.bgElev;
  return (
    <View
      style={[
        cardS.card,
        {
          backgroundColor: colors.bgCard,
          borderColor: colors.border,
          marginHorizontal: space.md,
        },
      ]}
    >
      <View style={[cardS.accent, { backgroundColor: colors.bgElev }]} />
      <View style={{ flex: 1, padding: 16, gap: 10 }}>
        <View style={{ flexDirection: "row", gap: 8, alignItems: "center" }}>
          <View style={{ width: 34, height: 22, borderRadius: 6, backgroundColor: bg }} />
          <View style={{ flex: 1, height: 15, borderRadius: 4, backgroundColor: bg }} />
          <View style={{ width: 22, height: 22, borderRadius: 4, backgroundColor: bg }} />
        </View>
        <View style={{ height: 10, width: "55%", borderRadius: 4, backgroundColor: bg }} />
        <View
          style={{
            borderTopWidth: StyleSheet.hairlineWidth,
            borderTopColor: colors.border,
            marginTop: 6,
            paddingTop: 10,
            gap: 8,
          }}
        >
          {/* Primary row */}
          <View style={{ flexDirection: "row" }}>
            {[1, 2, 3].map((i) => (
              <View key={i} style={{ flex: 1, alignItems: "center", gap: 4 }}>
                <View style={{ width: 48, height: 20, borderRadius: 4, backgroundColor: bg }} />
                <View style={{ width: 44, height: 8, borderRadius: 3, backgroundColor: bg }} />
              </View>
            ))}
          </View>
          {/* Secondary row */}
          <View style={{ flexDirection: "row" }}>
            {[1, 2].map((i) => (
              <View key={i} style={{ flex: 1, alignItems: "center", gap: 4 }}>
                <View style={{ width: 40, height: 16, borderRadius: 4, backgroundColor: bg }} />
                <View style={{ width: 36, height: 8, borderRadius: 3, backgroundColor: bg }} />
              </View>
            ))}
          </View>
        </View>
      </View>
    </View>
  );
}

function SkeletonList({ colors }: { colors: ThemeColors }) {
  return (
    <View style={{ gap: space.sm, paddingTop: space.sm }}>
      {[1, 2, 3, 4, 5].map((i) => (
        <SkeletonCard key={i} colors={colors} />
      ))}
    </View>
  );
}

// ── Two-level category filter ─────────────────────────────────────────────────

const BROAD_ORDER = ["Equity", "Debt", "Hybrid", "Solution Oriented", "Other"] as const;

/** AMFI sub-categories shown in "All" tab — core equity + all hybrid */
const CORE_SUBS = new Set([
  "Multi Cap Fund",
  "Large Cap Fund",
  "Large & Mid Cap Fund",
  "Mid Cap Fund",
  "Small Cap Fund",
  "ELSS",
  "Flexi Cap Fund",
]);

function Chip({
  label,
  active,
  onPress,
  solid = false,
  colors,
}: {
  label: string;
  active: boolean;
  onPress: () => void;
  solid?: boolean;
  colors: ThemeColors;
}) {
  return (
    <Pressable
      onPress={onPress}
      style={({ pressed }) => [
        chipS.chip,
        {
          borderColor: active ? colors.azure : colors.border,
          backgroundColor: active
            ? solid ? colors.azure : colors.azureSoft
            : colors.bgCard,
          opacity: pressed ? 0.75 : 1,
        },
      ]}
    >
      <Text
        style={[
          chipS.label,
          {
            color: active
              ? solid ? "#fff" : colors.azure
              : colors.textSec,
            fontWeight: active ? "700" : "500",
          },
        ]}
      >
        {label}
      </Text>
    </Pressable>
  );
}

function TwoLevelFilter({
  funds,
  broad,
  sub,
  onBroad,
  onSub,
  colors,
}: {
  funds: Array<{ broad_category?: string; amfi_sub?: string }>;
  broad: string | null;
  sub: string | null;
  onBroad: (b: string | null) => void;
  onSub: (s: string | null) => void;
  colors: ThemeColors;
}) {
  // Which broad categories have at least one fund?
  const availableBroads = useMemo(() => {
    const set = new Set(funds.map((f) => f.broad_category).filter(Boolean) as string[]);
    return BROAD_ORDER.filter((b) => set.has(b));
  }, [funds]);

  // Subcategories for the selected broad, sorted by sub name
  const subs = useMemo(() => {
    if (!broad) return [];
    const set = new Set(
      funds
        .filter((f) => f.broad_category === broad && f.amfi_sub)
        .map((f) => f.amfi_sub as string),
    );
    return [...set].sort();
  }, [funds, broad]);

  return (
    <View>
      {/* ── Row 1: broad tabs ── */}
      <ScrollView
        horizontal
        showsHorizontalScrollIndicator={false}
        contentContainerStyle={chipS.row}
      >
        <Chip
          label="All"
          active={broad === null}
          solid
          onPress={() => { onBroad(null); onSub(null); }}
          colors={colors}
        />
        {availableBroads.map((b) => (
          <Chip
            key={b}
            label={b}
            active={broad === b}
            onPress={() => {
              if (broad === b) { onBroad(null); onSub(null); }
              else { onBroad(b); onSub(null); }
            }}
            colors={colors}
          />
        ))}
      </ScrollView>

      {/* ── Row 2: subcategory chips (only when broad is selected) ── */}
      {broad !== null && subs.length > 0 && (
        <ScrollView
          horizontal
          showsHorizontalScrollIndicator={false}
          contentContainerStyle={[chipS.row, chipS.subRow]}
        >
          <Chip
            label={`All ${broad}`}
            active={sub === null}
            onPress={() => onSub(null)}
            colors={colors}
          />
          {subs.map((s) => (
            <Chip
              key={s}
              label={s}
              active={sub === s}
              onPress={() => onSub(sub === s ? null : s)}
              colors={colors}
            />
          ))}
        </ScrollView>
      )}
    </View>
  );
}

const chipS = StyleSheet.create({
  row:    { paddingHorizontal: space.md, gap: 7, paddingVertical: 9 },
  subRow: {
    paddingTop: 2,
    paddingBottom: 10,
  },
  chip: {
    paddingHorizontal: 13,
    paddingVertical: 6,
    borderRadius: radii.pill,
    borderWidth: 1.5,
  },
  label: { fontSize: 13, fontWeight: "600" },
});

// ── FQS tier badge ────────────────────────────────────────────────────────────

function TierBadge({ tier, colors }: { tier: string; colors: ThemeColors }) {
  const accent = TIER_COLORS[tier] ?? colors.textTer;
  return (
    <View
      style={[
        badgeS.wrap,
        { borderColor: accent + "40", backgroundColor: accent + "15" },
      ]}
    >
      <View style={[badgeS.dot, { backgroundColor: accent }]} />
      <Text style={[badgeS.text, { color: accent }]}>{tier.toUpperCase()}</Text>
    </View>
  );
}

/** "since Apr 2018" tag — shown when the "Separate Management Periods"
 *  toggle is truncating a fund's history to a verified continuity break.
 *  Mirrors the extrapolated-fund asterisk+footnote pattern in the Proposal
 *  PDF: never truncate silently, always say why (reason available via
 *  accessibilityHint / long-press context, kept out of the compact chip
 *  text to save space). */
function ContinuityTag({
  since,
  reason,
  colors,
}: {
  since: string;
  reason?: string | null;
  colors: ThemeColors;
}) {
  const label = fmtSince(since);
  if (!label) return null;
  const accent = "#F59E0B"; // amber — same family as the CV "moderate" band
  return (
    <View
      style={[
        badgeS.wrap,
        { borderColor: accent + "40", backgroundColor: accent + "15" },
      ]}
      accessibilityHint={reason ?? undefined}
    >
      <GitBranch size={9} color={accent} />
      <Text style={[badgeS.text, { color: accent }]}>{label.toUpperCase()}</Text>
    </View>
  );
}

const badgeS = StyleSheet.create({
  wrap: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: radii.pill,
    borderWidth: 1,
  },
  dot:  { width: 5, height: 5, borderRadius: 3 },
  text: { fontSize: 10, fontWeight: "800", letterSpacing: 0.6 },
});

// ── Rank medal colours ────────────────────────────────────────────────────────

const RANK_ACCENT: Record<number, string> = {
  1: "#F59E0B",  // gold
  2: "#94A3B8",  // silver
  3: "#C77D42",  // bronze
};

// ── Fund card ─────────────────────────────────────────────────────────────────

function FundCard({
  rank,
  name,
  category,
  blendedWm,
  fqs,
  fqsTier,
  successPct,
  nWindows,
  cv,
  p10Wm = null,
  mode,
  horizon,
  onPress,
  colors,
  overrideMinWm,
  selectionMode = false,
  selected = false,
  continuitySince = null,
  continuityReason = null,
  periodOptions = null,
  activePeriod = FULL_HISTORY,
  onSelectPeriod,
  periodBusy = false,
  periodNote = null,
}: {
  periodOptions?: PeriodOption[] | null;
  activePeriod?: string;
  onSelectPeriod?: (value: string) => void;
  periodBusy?: boolean;
  periodNote?: string | null;
  rank: number;
  name: string;
  category?: string;
  blendedWm: number | null;
  fqs: number | null;
  fqsTier: string | null;
  successPct: number | null;
  nWindows: number | null;
  cv: number | null;
  p10Wm?: number | null;
  mode: "sip" | "lumpsum";
  horizon: number;
  onPress: () => void;
  colors: ThemeColors;
  overrideMinWm?: number | null;
  selectionMode?: boolean;
  selected?: boolean;
  /** Set when the "Separate Management Periods" toggle is on and this
   *  fund's numbers are truncated to a verified continuity break — the
   *  break date, e.g. "2018-04-12". Renders a "since Apr 2018" tag. */
  continuitySince?: string | null;
  continuityReason?: string | null;
}) {
  const { scheme } = useTheme();
  const tc     = fqsTier ? (TIER_COLORS[fqsTier] ?? colors.textTer) : colors.border;
  const rankAc = RANK_ACCENT[rank];
  const isDark = scheme === "dark";

  const blendedXirr = blendedWm != null
    ? (mode === "sip"
        ? sipWmToXirr(blendedWm, horizon)
        : lumpsumWmToXirr(blendedWm, horizon))
    : null;
  const p10Xirr = p10Wm != null
    ? (mode === "sip"
        ? sipWmToXirr(p10Wm, horizon)
        : lumpsumWmToXirr(p10Wm, horizon))
    : null;

  const cardShadow = isDark
    ? {}
    : {
        shadowColor: "#0F172A",
        shadowOpacity: 0.08,
        shadowRadius: 12,
        shadowOffset: { width: 0, height: 3 },
        elevation: 3,
      };

  return (
    <Pressable
      onPress={onPress}
      style={({ pressed }) => [
        cardS.card,
        cardShadow,
        {
          backgroundColor: colors.bgCard,
          borderColor:      rankAc ? rankAc + "50" : colors.border,
          borderWidth:      rankAc ? 1.5 : StyleSheet.hairlineWidth,
          marginHorizontal: space.md,
          transform:        [{ scale: pressed ? 0.985 : 1 }],
        },
      ]}
    >
      {/* Left tier accent strip */}
      <View style={[cardS.accent, { backgroundColor: tc }]} />

      <View style={cardS.inner}>
        {/* ── Row 1: [checkbox] + rank + name + arrow ── */}
        <View style={cardS.row1}>
          {selectionMode && (
            selected
              ? <CheckSquare size={20} color={colors.azure} />
              : <Square size={20} color={colors.textTer} />
          )}

          <View
            style={[
              cardS.rankWrap,
              {
                backgroundColor: rankAc ? rankAc + "18" : colors.bgElev,
                borderColor:     rankAc ? rankAc + "60" : colors.border,
              },
            ]}
          >
            <Text style={[cardS.rankNum, { color: rankAc ?? colors.textTer }]}>
              {rank}
            </Text>
          </View>

          <Text
            style={[cardS.name, { color: colors.textPri, flex: 1 }]}
            numberOfLines={2}
          >
            {name}
          </Text>

          {!selectionMode && <ChevronRight size={15} color={colors.textTer} />}
        </View>

        {/* ── Row 2: category chip + tier badge + continuity tag ── */}
        <View style={cardS.row2}>
          {category ? (
            <View
              style={[
                cardS.catChip,
                { borderColor: colors.border, backgroundColor: colors.bgElev },
              ]}
            >
              <Text style={{ fontSize: 11, fontWeight: "600", color: colors.textSec }}>
                {category}
              </Text>
            </View>
          ) : null}
          {fqsTier ? (
            <TierBadge tier={fqsTier} colors={colors} />
          ) : (
            <Text style={{ fontSize: 11, color: colors.textTer }}>Unrated</Text>
          )}
          {continuitySince && fmtSince(continuitySince) ? (
            <ContinuityTag since={continuitySince} reason={continuityReason} colors={colors} />
          ) : null}
        </View>

        {periodOptions && onSelectPeriod && !selectionMode ? (
          <PeriodPicker
            options={periodOptions}
            active={activePeriod}
            onSelect={onSelectPeriod}
            busy={periodBusy}
            note={periodNote}
            colors={colors}
          />
        ) : null}

        {/* ── Divider ── */}
        <View style={[cardS.divider, { backgroundColor: colors.border }]} />

        {/* ── Primary metrics row: Blended WM | FQS | Success % ── */}
        <View style={[cardS.metricsRow, periodBusy && { opacity: 0.45 }]}>
          <MetricCell
            value={blendedWm != null ? `${blendedWm.toFixed(2)}×` : "—"}
            subValue={fmtXirr(blendedXirr)}
            label="Blended WM"
            valueColor={tc}
            colors={colors}
          />
          <View style={[cardS.divV, { backgroundColor: colors.border }]} />
          <MetricCell
            value={fqs != null ? fqs.toFixed(1) : "—"}
            label="FQS"
            valueColor={fqsTier ? (TIER_COLORS[fqsTier] ?? colors.textSec) : colors.textSec}
            colors={colors}
          />
          <View style={[cardS.divV, { backgroundColor: colors.border }]} />
          <MetricCell
            value={successPct != null ? `${(successPct * 100).toFixed(0)}%` : "—"}
            label="Success"
            valueColor={successPct != null && successPct >= 0.8 ? "#10B981" : colors.textSec}
            colors={colors}
          />
        </View>

        {/* ── Secondary metrics row: Windows | P10 WM | CV ── */}
        <View style={[cardS.metricsRow, { marginTop: 8 }, periodBusy && { opacity: 0.45 }]}>
          <MetricCell
            value={nWindows != null ? String(nWindows) : "—"}
            label="Windows"
            valueColor={colors.textSec}
            colors={colors}
            small
          />
          <View style={[cardS.divV, { backgroundColor: colors.border }]} />
          <MetricCell
            value={p10Wm != null ? `${p10Wm.toFixed(2)}×` : "—"}
            subValue={fmtXirr(p10Xirr)}
            label="P10 WM"
            valueColor={colors.textSec}
            colors={colors}
            small
          />
          <View style={[cardS.divV, { backgroundColor: colors.border }]} />
          <MetricCell
            value={cvLabel(cv)}
            label="CV"
            valueColor={cvColor(cv)}
            colors={colors}
            small
          />
        </View>

        {/* ── Continuity strip — WHY this fund's history was truncated.
             The row-2 pill is a quick glance cue; screen-reader-only
             accessibilityHint isn't enough for a decision this material, so
             the reason is spelled out here too, same visual language as the
             benchmark strip below. ── */}
        {continuitySince && fmtSince(continuitySince) ? (
          <View
            style={[
              cardS.bmStrip,
              { backgroundColor: "#F59E0B15", borderTopColor: "#F59E0B30" },
            ]}
          >
            <GitBranch size={11} color="#F59E0B" />
            <Text style={[cardS.bmStripText, { color: "#F59E0B" }]}>
              {`History scored ${fmtSince(continuitySince)}`}
              {continuityReason ? ` — ${continuityReason}` : " — management change"}
            </Text>
          </View>
        ) : null}
        {periodOptions && activePeriod === FULL_HISTORY && !selectionMode ? (
          <View style={[cardS.bmStrip, { borderTopColor: colors.border }]}>
            <GitBranch size={11} color={colors.textTer} />
            <Text style={[cardS.bmStripText, { color: colors.textTer }]}>
              Scored on all years — includes every management this fund has had.
            </Text>
          </View>
        ) : null}

        {/* ── Custom benchmark strip (only when override is active) ── */}
        {overrideMinWm != null && blendedWm != null && (() => {
          const passes = blendedWm >= overrideMinWm;
          const margin = ((blendedWm / overrideMinWm) - 1) * 100;
          const stripColor = passes ? "#10B981" : "#EF4444";
          return (
            <View
              style={[
                cardS.bmStrip,
                { backgroundColor: stripColor + "15", borderTopColor: stripColor + "30" },
              ]}
            >
              {passes
                ? <CheckCircle2 size={11} color={stripColor} />
                : <XCircle size={11} color={stripColor} />}
              <Text style={[cardS.bmStripText, { color: stripColor }]}>
                {passes
                  ? `Beats benchmark · Blended WM +${margin.toFixed(1)}% above target`
                  : `Below benchmark · Blended WM ${Math.abs(margin).toFixed(1)}% short of target`}
              </Text>
            </View>
          );
        })()}
      </View>
    </Pressable>
  );
}

function MetricCell({
  value,
  subValue,
  label,
  valueColor,
  colors,
  small = false,
}: {
  value: string;
  subValue?: string;
  label: string;
  valueColor: string;
  colors: ThemeColors;
  small?: boolean;
}) {
  return (
    <View style={cardS.metricCell}>
      <Text
        style={[
          small ? cardS.metricValSm : cardS.metricVal,
          { color: valueColor },
        ]}
      >
        {value}
      </Text>
      {subValue && subValue !== "—" ? (
        <Text style={[cardS.metricSub, { color: colors.textTer }]}>
          {subValue} p.a.
        </Text>
      ) : null}
      <Text style={[cardS.metricLbl, { color: colors.textTer }]}>{label}</Text>
    </View>
  );
}

const cardS = StyleSheet.create({
  card: {
    borderRadius: radii.lg,
    marginBottom: 10,
    flexDirection: "row",
    overflow: "hidden",
  },
  accent: { width: 4 },
  inner:  { flex: 1, paddingHorizontal: 14, paddingVertical: 14, gap: 0 },

  row1: {
    flexDirection: "row",
    alignItems: "flex-start",
    gap: 9,
    marginBottom: 8,
  },
  rankWrap: {
    minWidth: 32,
    height: 26,
    borderRadius: radii.sm,
    borderWidth: 1,
    alignItems: "center",
    justifyContent: "center",
    paddingHorizontal: 6,
  },
  rankNum: { fontSize: 13, fontWeight: "800", fontVariant: ["tabular-nums"] },
  name:    { fontSize: 14, fontWeight: "700", lineHeight: 20, letterSpacing: -0.2 },

  row2: {
    flexDirection: "row",
    alignItems: "center",
    gap: 7,
    flexWrap: "wrap",
    marginBottom: 12,
  },
  catChip: {
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: radii.sm,
    borderWidth: 1,
  },

  divider: { height: 1, marginBottom: 10 },

  metricsRow:  { flexDirection: "row", alignItems: "center" },
  metricCell:  { flex: 1, alignItems: "center", gap: 2 },
  metricVal: {
    fontSize: 18,
    fontWeight: "800",
    fontVariant: ["tabular-nums"],
    letterSpacing: -0.5,
  },
  metricValSm: {
    fontSize: 14,
    fontWeight: "700",
    fontVariant: ["tabular-nums"],
    letterSpacing: -0.3,
  },
  metricSub: {
    fontSize: 10,
    fontWeight: "600",
    fontVariant: ["tabular-nums"],
  },
  metricLbl: {
    fontSize: 9,
    fontWeight: "700",
    letterSpacing: 0.8,
    textTransform: "uppercase",
  },
  divV: { width: 1, height: 42, marginHorizontal: 4 },

  // Custom benchmark strip (below CV row)
  bmStrip: {
    flexDirection: "row",
    alignItems: "center",
    gap: 5,
    marginTop: 10,
    paddingTop: 8,
    borderTopWidth: StyleSheet.hairlineWidth,
  },
  bmStripText: {
    fontSize: 10,
    fontWeight: "600",
    flex: 1,
  },
});

// ── Bonus section header ──────────────────────────────────────────────────────

const BONUS_AMBER = "#F59E0B";

function BonusSectionHeader({
  count,
  collapsed,
  onToggle,
  colors,
}: {
  count: number;
  collapsed: boolean;
  onToggle: () => void;
  colors: ThemeColors;
}) {
  return (
    <Pressable
      onPress={onToggle}
      style={({ pressed }) => ({
        flexDirection: "row",
        alignItems: "center",
        gap: 8,
        marginHorizontal: space.md,
        marginTop: 14,
        marginBottom: 6,
        opacity: pressed ? 0.7 : 1,
      })}
    >
      <AlertTriangle size={12} color={BONUS_AMBER} />
      <Text
        style={{
          fontSize: 10,
          fontWeight: "800",
          letterSpacing: 1.1,
          textTransform: "uppercase",
          color: BONUS_AMBER,
        }}
      >
        Additional Recommendations
      </Text>
      {/* count badge */}
      <View
        style={{
          backgroundColor: BONUS_AMBER + "20",
          borderRadius: 10,
          paddingHorizontal: 6,
          paddingVertical: 2,
        }}
      >
        <Text style={{ fontSize: 10, fontWeight: "700", color: BONUS_AMBER }}>
          {count}
        </Text>
      </View>
      <View style={{ flex: 1, height: 1, backgroundColor: BONUS_AMBER + "35" }} />
      {/* chevron rotates when collapsed */}
      <ChevronRight
        size={14}
        color={BONUS_AMBER}
        style={{ transform: [{ rotate: collapsed ? "0deg" : "90deg" }] }}
      />
    </Pressable>
  );
}

// ── Bonus fund card (amber-accented, shows best-horizon metrics) ──────────────

// No `mode` prop: this card's XIRR is pre-computed by the backend at
// best_horizon, so nothing here depends on which engine produced the numbers.
function BonusFundCard({
  fund,
  onPress,
  colors,
  selectionMode = false,
  selected = false,
  period = null,
}: {
  fund: BonusFund;
  onPress: () => void;
  colors: ThemeColors;
  selectionMode?: boolean;
  selected?: boolean;
  period?: {
    options: PeriodOption[]; active: string; busy: boolean; note: string | null;
    onSelect: (v: string) => void;
  } | null;
}) {
  // Both modes deliver these under the same names (UniverseMetrics), so there
  // is no per-mode field selection here — only the engine that produced them
  // differs. Metrics are read at the fund's own best_horizon.
  const { blended_wm: blendedWm, fqs, fqs_tier: fqsTier,
          success_pct: successPct, n_windows: nWindows, cv, p10_wm: p10Wm } = fund;
  const xirr = fund.blended_xirr; // pre-computed by backend at best_horizon
  const p10Xirr = fund.p10_xirr;  // pre-computed by backend at best_horizon
  const tc   = fqsTier ? (TIER_COLORS[fqsTier] ?? BONUS_AMBER) : BONUS_AMBER;

  return (
    <Pressable
      onPress={onPress}
      style={({ pressed }) => [
        cardS.card,
        {
          backgroundColor: colors.bgCard,
          borderColor: BONUS_AMBER + "55",
          borderWidth: 1.5,
          marginHorizontal: space.md,
          transform: [{ scale: pressed ? 0.985 : 1 }],
        },
      ]}
    >
      {/* Amber left accent strip */}
      <View style={[cardS.accent, { backgroundColor: BONUS_AMBER }]} />

      <View style={cardS.inner}>
        {/* Row 1: [checkbox] + star badge + name + arrow */}
        <View style={cardS.row1}>
          {selectionMode && (
            selected
              ? <CheckSquare size={20} color={colors.azure} />
              : <Square size={20} color={colors.textTer} />
          )}

          <View
            style={[
              cardS.rankWrap,
              { backgroundColor: BONUS_AMBER + "18", borderColor: BONUS_AMBER + "60" },
            ]}
          >
            <Text style={[cardS.rankNum, { color: BONUS_AMBER }]}>★</Text>
          </View>

          <Text
            style={[cardS.name, { color: colors.textPri, flex: 1 }]}
            numberOfLines={2}
          >
            {fund.scheme_name}
          </Text>

          {!selectionMode && <ChevronRight size={15} color={colors.textTer} />}
        </View>

        {/* Row 2: category chip + tier badge + continuity tag */}
        <View style={cardS.row2}>
          {(fund.amfi_sub ?? fund.category) ? (
            <View
              style={[
                cardS.catChip,
                { borderColor: colors.border, backgroundColor: colors.bgElev },
              ]}
            >
              <Text style={{ fontSize: 11, fontWeight: "600", color: colors.textSec }}>
                {fund.amfi_sub ?? fund.category}
              </Text>
            </View>
          ) : null}
          {fqsTier ? (
            <TierBadge tier={fqsTier} colors={colors} />
          ) : (
            <Text style={{ fontSize: 11, color: colors.textTer }}>Unrated</Text>
          )}
          {fund.continuity_reset_date ? (
            <ContinuityTag since={fund.continuity_reset_date} reason={fund.continuity_reason} colors={colors} />
          ) : null}
        </View>

        {period && !selectionMode ? (
          <PeriodPicker
            options={period.options}
            active={period.active}
            onSelect={period.onSelect}
            busy={period.busy}
            note={period.note}
            colors={colors}
          />
        ) : null}

        {/* Disclaimer strip */}
        <View
          style={{
            flexDirection: "row",
            alignItems: "flex-start",
            gap: 6,
            backgroundColor: BONUS_AMBER + "12",
            borderRadius: radii.sm,
            paddingHorizontal: 9,
            paddingVertical: 7,
            marginBottom: 10,
          }}
        >
          <AlertTriangle size={11} color={BONUS_AMBER} style={{ marginTop: 1 }} />
          <Text style={{ flex: 1, fontSize: 11, color: BONUS_AMBER, lineHeight: 16 }}>
            {fund.history_years != null ? `${fund.history_years}Y` : "Limited"} track record · Evaluated at {fund.best_horizon}Y rolling · XIRR above 15%
          </Text>
        </View>

        {/* Divider */}
        <View style={[cardS.divider, { backgroundColor: colors.border }]} />

        {/* Primary metrics */}
        <View style={cardS.metricsRow}>
          <MetricCell
            value={blendedWm != null ? `${blendedWm.toFixed(2)}×` : "—"}
            subValue={fmtXirr(xirr)}
            label={`WM @${fund.best_horizon}Y`}
            valueColor={BONUS_AMBER}
            colors={colors}
          />
          <View style={[cardS.divV, { backgroundColor: colors.border }]} />
          <MetricCell
            value={fqs != null ? fqs.toFixed(1) : "—"}
            label="FQS"
            valueColor={tc}
            colors={colors}
          />
          <View style={[cardS.divV, { backgroundColor: colors.border }]} />
          <MetricCell
            value={successPct != null ? `${(successPct * 100).toFixed(0)}%` : "—"}
            label="Success"
            valueColor={successPct != null && successPct >= 0.8 ? "#10B981" : colors.textSec}
            colors={colors}
          />
        </View>

        {/* Secondary metrics */}
        <View style={[cardS.metricsRow, { marginTop: 8 }]}>
          <MetricCell
            value={nWindows != null ? String(nWindows) : "—"}
            label="Windows"
            valueColor={colors.textSec}
            colors={colors}
            small
          />
          <View style={[cardS.divV, { backgroundColor: colors.border }]} />
          <MetricCell
            value={p10Wm != null ? `${p10Wm.toFixed(2)}×` : "—"}
            subValue={fmtXirr(p10Xirr)}
            label="P10 WM"
            valueColor={colors.textSec}
            colors={colors}
            small
          />
          <View style={[cardS.divV, { backgroundColor: colors.border }]} />
          <MetricCell
            value={cvLabel(cv)}
            label="CV"
            valueColor={cvColor(cv)}
            colors={colors}
            small
          />
        </View>
      </View>
    </Pressable>
  );
}

// ── Custom Benchmark Bar ──────────────────────────────────────────────────────
//
// Bidirectional XIRR ↔ WM input. Lives in the FlatList header above the fund
// cards. When active, each FundCard shows a pass/fail strip against the custom
// min WM derived from the entered XIRR.
//
// State ownership: customXirr lives in the parent screen component (so the
// FundCard render gets the live value). The expand/collapse toggle is local.

function BenchmarkBar({
  mode,
  tenure,
  customXirr,
  onChangeXirr,
  passingCount,
  totalCount,
  colors,
}: {
  mode: "sip" | "lumpsum";
  tenure: number;
  customXirr: number | null;
  onChangeXirr: (v: number | null) => void;
  passingCount: number;
  totalCount: number;
  colors: ThemeColors;
}) {
  const [open, setOpen]       = useState(false);
  const [xirrStr, setXirrStr] = useState("");
  const [wmStr,   setWmStr]   = useState("");
  // Guard: prevent the computed update from triggering the opposite handler
  const updatingFrom = useRef<"xirr" | "wm" | null>(null);

  const toMinWm = (xirr: number) =>
    mode === "sip" ? sipXirrToMinWm(xirr, tenure) : lumpsumXirrToMinWm(xirr, tenure);
  const toXirr = (wm: number) =>
    mode === "sip" ? sipWmToXirr(wm, tenure) : lumpsumWmToXirr(wm, tenure);

  // Sync display strings whenever customXirr changes from outside (e.g. tenure change)
  useEffect(() => {
    if (customXirr === null) {
      setXirrStr("");
      setWmStr("");
    } else {
      if (updatingFrom.current !== "xirr") {
        setXirrStr((customXirr * 100).toFixed(1));
      }
      if (updatingFrom.current !== "wm") {
        setWmStr(toMinWm(customXirr).toFixed(2));
      }
      updatingFrom.current = null;
    }
  }, [customXirr, tenure]);  // eslint-disable-line react-hooks/exhaustive-deps

  const handleXirrChange = (text: string) => {
    setXirrStr(text);
    const n = parseFloat(text);
    if (!isNaN(n) && n > 0 && n < 100) {
      updatingFrom.current = "xirr";
      const xirr = n / 100;
      onChangeXirr(xirr);
      setWmStr(toMinWm(xirr).toFixed(2));
    } else if (text === "" || text === ".") {
      onChangeXirr(null);
      setWmStr("");
    }
  };

  const handleWmChange = (text: string) => {
    setWmStr(text);
    const wm = parseFloat(text);
    if (!isNaN(wm) && wm > 0) {
      const xirr = toXirr(wm);
      if (xirr !== null && isFinite(xirr)) {
        updatingFrom.current = "wm";
        onChangeXirr(xirr);
        setXirrStr((xirr * 100).toFixed(1));
      }
    } else if (text === "" || text === ".") {
      onChangeXirr(null);
      setXirrStr("");
    }
  };

  const reset = () => {
    onChangeXirr(null);
    setXirrStr("");
    setWmStr("");
  };

  const isActive = customXirr !== null;
  const accentCol = isActive ? "#6366F1" : colors.textTer; // indigo when active

  return (
    <View
      style={[
        bmS.wrap,
        {
          borderColor: isActive ? "#6366F130" : colors.border,
          backgroundColor: isActive ? "#6366F108" : colors.bgCard,
          marginHorizontal: space.md,
          marginBottom: space.sm,
          marginTop: 10,
        },
      ]}
    >
      {/* ── Header row (always visible) ── */}
      <Pressable
        onPress={() => setOpen((v) => !v)}
        style={({ pressed }) => [bmS.headerRow, { opacity: pressed ? 0.7 : 1 }]}
      >
        <Target size={13} color={accentCol} />
        <Text style={[bmS.headerLabel, { color: accentCol }]}>
          {isActive
            ? `Custom Benchmark: ${(customXirr * 100).toFixed(1)}% XIRR → ${toMinWm(customXirr).toFixed(2)}× WM`
            : "Set Custom Benchmark"}
        </Text>
        {isActive && (
          <View style={[bmS.pill, { backgroundColor: "#6366F120" }]}>
            <Text style={[bmS.pillText, { color: "#6366F1" }]}>
              {passingCount}/{totalCount}
            </Text>
          </View>
        )}
        <ChevronRight
          size={13}
          color={accentCol}
          style={{ transform: [{ rotate: open ? "90deg" : "0deg" }], marginLeft: "auto" }}
        />
      </Pressable>

      {/* ── Expanded edit area ── */}
      {open && (
        <View style={bmS.body}>
          <View style={bmS.inputRow}>
            {/* XIRR input */}
            <View style={[bmS.inputWrap, { borderColor: colors.border, backgroundColor: colors.bgElev }]}>
              <TextInput
                style={[bmS.input, { color: colors.textPri }]}
                placeholder="12.0"
                placeholderTextColor={colors.textTer}
                keyboardType="decimal-pad"
                value={xirrStr}
                onChangeText={handleXirrChange}
                maxLength={6}
              />
              <Text style={[bmS.inputUnit, { color: colors.textSec }]}>% XIRR</Text>
            </View>

            {/* divider arrow */}
            <Text style={[bmS.arrow, { color: colors.textTer }]}>↔</Text>

            {/* WM input */}
            <View style={[bmS.inputWrap, { borderColor: colors.border, backgroundColor: colors.bgElev }]}>
              <TextInput
                style={[bmS.input, { color: colors.textPri }]}
                placeholder="3.52"
                placeholderTextColor={colors.textTer}
                keyboardType="decimal-pad"
                value={wmStr}
                onChangeText={handleWmChange}
                maxLength={7}
              />
              <Text style={[bmS.inputUnit, { color: colors.textSec }]}>× WM</Text>
            </View>

            {/* Reset button */}
            {isActive && (
              <Pressable
                onPress={reset}
                hitSlop={8}
                style={({ pressed }) => [bmS.resetBtn, { opacity: pressed ? 0.6 : 1 }]}
              >
                <X size={14} color={colors.textSec} />
              </Pressable>
            )}
          </View>

          {/* Pass count summary */}
          {isActive && (
            <Text style={[bmS.summary, { color: colors.textSec }]}>
              <Text style={{ fontWeight: "700", color: passingCount > 0 ? "#10B981" : colors.tierWeak }}>
                {passingCount}
              </Text>
              {" of "}
              <Text style={{ fontWeight: "700", color: colors.textPri }}>{totalCount}</Text>
              {" funds have Blended WM ≥ "}
              <Text style={{ fontWeight: "700", color: colors.textPri }}>
                {toMinWm(customXirr).toFixed(2)}×
              </Text>
            </Text>
          )}
        </View>
      )}
    </View>
  );
}

const bmS = StyleSheet.create({
  wrap: {
    borderRadius: radii.md,
    borderWidth: 1,
    overflow: "hidden",
  },
  headerRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 7,
    paddingHorizontal: 13,
    paddingVertical: 11,
  },
  headerLabel: {
    fontSize: 12,
    fontWeight: "700",
    flex: 1,
    letterSpacing: -0.1,
  },
  pill: {
    borderRadius: 10,
    paddingHorizontal: 7,
    paddingVertical: 2,
  },
  pillText: {
    fontSize: 11,
    fontWeight: "800",
    fontVariant: ["tabular-nums"],
  },
  body: {
    paddingHorizontal: 13,
    paddingBottom: 13,
    gap: 10,
  },
  inputRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
  inputWrap: {
    flex: 1,
    flexDirection: "row",
    alignItems: "center",
    borderRadius: radii.sm,
    borderWidth: 1,
    paddingHorizontal: 10,
    paddingVertical: 8,
    gap: 4,
  },
  input: {
    flex: 1,
    fontSize: 16,
    fontWeight: "700",
    fontVariant: ["tabular-nums"],
    padding: 0,
    margin: 0,
  },
  inputUnit: {
    fontSize: 11,
    fontWeight: "600",
  },
  arrow: {
    fontSize: 16,
    fontWeight: "600",
  },
  resetBtn: {
    padding: 6,
  },
  summary: {
    fontSize: 12,
    lineHeight: 18,
  },
});

// ── Shared header ─────────────────────────────────────────────────────────────

function HomeHeader({
  mode,
  tenure,
  onPressSettings,
  colors,
  selectionMode,
  selectedCount,
  onEnterSelect,
  onCancelSelect,
  onBuildProposal,
  since2010,
  onToggleSince2010,
  separateManagement,
  onToggleSeparateManagement,
}: {
  mode: "sip" | "lumpsum";
  tenure: number | null;
  onPressSettings: () => void;
  colors: ThemeColors;
  selectionMode: boolean;
  selectedCount: number;
  onEnterSelect: () => void;
  onCancelSelect: () => void;
  onBuildProposal: () => void;
  since2010: boolean;
  onToggleSince2010: () => void;
  separateManagement: boolean;
  onToggleSeparateManagement: () => void;
}) {
  const { scheme } = useTheme();
  const isDark = scheme === "dark";

  if (selectionMode) {
    return (
      <View style={[headerS.selectionWrap, { borderBottomColor: colors.border }]}>
        <Pressable onPress={onCancelSelect} hitSlop={8}>
          <Text style={[headerS.cancelText, { color: colors.textSec }]}>Cancel</Text>
        </Pressable>
        <Pressable
          onPress={onBuildProposal}
          disabled={selectedCount === 0}
          style={({ pressed }) => [
            headerS.buildBtn,
            {
              backgroundColor: selectedCount > 0 ? colors.azure : colors.bgElev,
              opacity: pressed ? 0.85 : 1,
            },
          ]}
        >
          <Briefcase size={13} color={selectedCount > 0 ? "#fff" : colors.textTer} />
          <Text style={[headerS.buildBtnText, { color: selectedCount > 0 ? "#fff" : colors.textTer }]}>
            Build Proposal{selectedCount > 0 ? ` (${selectedCount})` : ""}
          </Text>
        </Pressable>
      </View>
    );
  }

  return (
    <View style={[headerS.wrap, { borderBottomColor: colors.border }]}>
      {/* Row 1: brand (left) + tenure/mode pill + settings (right) — the
          controls used every visit, so they must never get squeezed off
          the edge by the filter chips below. */}
      <View style={headerS.topRow}>
        {/* Opens the drawer (Methodology, Data Refresh, Analysis Mode). The
            drawer also opens on an edge swipe, but a swipe alone is
            undiscoverable — this is the affordance that advertises it. */}
        <DrawerMenuButton />
        <Text style={[headerS.brand, { color: colors.textPri }]}>ArthVruksh</Text>

        {tenure !== null && (
          <Pressable
            onPress={onPressSettings}
            style={({ pressed }) => [
              headerS.tenurePill,
              {
                backgroundColor: colors.azureSoft,
                borderColor: colors.azure + "50",
                opacity: pressed ? 0.75 : 1,
              },
            ]}
          >
            <Text style={[headerS.tenureYear, { color: colors.azure }]}>
              {tenure}Y
            </Text>
            <View style={[headerS.pillDivider, { backgroundColor: colors.azure + "40" }]} />
            <Text style={[headerS.tenureMode, { color: colors.azure }]}>
              {mode === "lumpsum" ? "LUMP" : "SIP"}
            </Text>
            <SlidersHorizontal size={12} color={colors.azure} />
          </Pressable>
        )}
      </View>

      {/* Row 2: filter/action chips — since-2010, mgmt-continuity, select.
          A separate row (not squeezed onto row 1 with the brand/tenure
          pill) so adding a chip here never risks clipping row 1's
          controls again on a narrow screen. */}
      <View style={headerS.chipRow}>
        <Pressable
          onPress={onToggleSince2010}
          hitSlop={8}
          style={({ pressed }) => [
            headerS.since2010Btn,
            {
              borderColor: since2010 ? colors.azure : colors.border,
              backgroundColor: since2010 ? colors.azureSoft : "transparent",
              opacity: pressed ? 0.75 : 1,
            },
          ]}
        >
          <History size={12} color={since2010 ? colors.azure : colors.textSec} />
          <Text style={[headerS.since2010BtnText, { color: since2010 ? colors.azure : colors.textSec }]}>
            2010
          </Text>
        </Pressable>

        <Pressable
          onPress={onToggleSeparateManagement}
          hitSlop={8}
          style={({ pressed }) => [
            headerS.since2010Btn,
            {
              borderColor: separateManagement ? colors.azure : colors.border,
              backgroundColor: separateManagement ? colors.azureSoft : "transparent",
              opacity: pressed ? 0.75 : 1,
            },
          ]}
        >
          <GitBranch size={12} color={separateManagement ? colors.azure : colors.textSec} />
          <Text style={[headerS.since2010BtnText, { color: separateManagement ? colors.azure : colors.textSec }]}>
            Management
          </Text>
        </Pressable>

        <Pressable
          onPress={onEnterSelect}
          hitSlop={8}
          style={({ pressed }) => [
            headerS.selectBtn,
            { borderColor: colors.border, opacity: pressed ? 0.75 : 1 },
          ]}
        >
          <Briefcase size={12} color={colors.textSec} />
          <Text style={[headerS.selectBtnText, { color: colors.textSec }]}>Select</Text>
        </Pressable>
      </View>
    </View>
  );
}

const headerS = StyleSheet.create({
  // Column container: row 1 (brand + tenure pill) stacked above row 2
  // (filter chips) — splitting these into two rows is what stops the chip
  // row (which grows as more toggles are added) from ever squeezing the
  // tenure pill + settings icon off the edge of a narrow screen again.
  wrap: {
    paddingHorizontal: space.md,
    paddingVertical: 11,
    borderBottomWidth: StyleSheet.hairlineWidth,
    gap: 10,
  },
  // Selection-mode header (Cancel / Build Proposal) stays a single row —
  // only the normal (non-selection) header needed the two-row split.
  selectionWrap: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: space.md,
    paddingVertical: 13,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  topRow: { flexDirection: "row", alignItems: "center", justifyContent: "space-between" },
  brand: { fontSize: 18, fontWeight: "800", letterSpacing: -0.4 },
  chipRow: { flexDirection: "row", alignItems: "center", gap: space.sm },

  tenurePill: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingHorizontal: 11,
    paddingVertical: 6,
    borderRadius: radii.pill,
    borderWidth: 1.5,
  },
  tenureYear: { fontSize: 13, fontWeight: "800", fontVariant: ["tabular-nums"] },
  tenureMode: { fontSize: 10, fontWeight: "700", letterSpacing: 0.8 },
  pillDivider: { width: 1, height: 12 },

  since2010Btn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    paddingHorizontal: 9,
    paddingVertical: 6,
    borderRadius: radii.pill,
    borderWidth: 1.5,
  },
  since2010BtnText: { fontSize: 11, fontWeight: "800" },

  selectBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 5,
    paddingHorizontal: 10,
    paddingVertical: 6,
    borderRadius: radii.pill,
    borderWidth: 1,
  },
  selectBtnText: { fontSize: 11.5, fontWeight: "700" },
  cancelText: { fontSize: 14, fontWeight: "600" },
  buildBtn: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingHorizontal: 12,
    paddingVertical: 8,
    borderRadius: radii.pill,
  },
  buildBtnText: { fontSize: 12.5, fontWeight: "700" },
});

// ── Sort label strip ──────────────────────────────────────────────────────────

function SortStrip({
  count,
  minHistory,
  sortLabel,
  colors,
  since2010 = false,
  separateManagement = false,
  scoresLastRefreshed,
}: {
  separateManagement?: boolean;
  count: number;
  minHistory: number;
  sortLabel: string;
  colors: ThemeColors;
  /** "Since 2010" toggle is on — must say so explicitly here, not just via
   *  the header pill, since it silently changes every number on the screen
   *  (start date forced to 2010-01-01 instead of each fund's own
   *  inception). Never let a global recalculation like this be silent. */
  since2010?: boolean;
  /** UniverseResponse.scores_last_refreshed — when the nightly FQS/Lumpsum
   *  batch that produced these numbers last actually finished. Surfaced so a
   *  refresh that silently died mid-run (e.g. killed by a redeploy) is
   *  visible on screen instead of requiring a manual API status check. */
  scoresLastRefreshed?: string | null;
}) {
  const age = formatDataAge(scoresLastRefreshed);
  return (
    <View style={[stripS.wrap, { borderBottomColor: colors.border }]}>
      <View style={stripS.textRow}>
        <Text style={[stripS.text, { color: colors.textTer }]}>
          <Text style={[stripS.bold, { color: colors.textSec }]}>{count} funds</Text>
          {"  ·  "}≥{minHistory}Y history{"  ·  "}{sortLabel}
        </Text>
        {age && (
          <View style={stripS.ageRow}>
            <Clock size={10} color={age.stale ? colors.amber : colors.textTer} />
            <Text
              style={[
                stripS.ageText,
                { color: age.stale ? colors.amber : colors.textTer, fontWeight: age.stale ? "700" : "400" },
              ]}
            >
              {age.text}
            </Text>
          </View>
        )}
      </View>
      {since2010 && (
        <View style={[stripS.noticeRow, { backgroundColor: colors.azureSoft }]}>
          <History size={11} color={colors.azure} />
          <Text style={[stripS.noticeText, { color: colors.azure }]}>
            Showing every fund&apos;s history from 2010-01-01 onward (or its own inception, if later) — not since inception.
          </Text>
        </View>
      )}
      {separateManagement && (
        <View style={[stripS.noticeRow, { backgroundColor: colors.azureSoft }]}>
          <GitBranch size={11} color={colors.azure} />
          <Text style={[stripS.noticeText, { color: colors.azure }]}>
            Funds whose AMC changed are scored from their latest change. Where a fund changed hands more
            than once, pick a different starting period on its card to recalculate it.
          </Text>
        </View>
      )}
    </View>
  );
}

const stripS = StyleSheet.create({
  wrap: {
    paddingHorizontal: space.md,
    paddingVertical: 9,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  text: { fontSize: 12, fontWeight: "400" },
  bold: { fontWeight: "700" },
  textRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    flexWrap: "wrap",
    gap: 6,
  },
  ageRow: { flexDirection: "row", alignItems: "center", gap: 3 },
  ageText: { fontSize: 10.5 },
  noticeRow: {
    flexDirection: "row",
    alignItems: "flex-start",
    gap: 6,
    borderRadius: radii.sm,
    paddingHorizontal: 9,
    paddingVertical: 6,
    marginTop: 7,
  },
  noticeText: { flex: 1, fontSize: 11, fontWeight: "600", lineHeight: 15 },
});

// ── Search within the ranked list ─────────────────────────────────────────────
// Filters the already-ranked/eligible funds by name — this is not a global
// fund search (that's the Funds tab); it only narrows what's already shown.

function FundSearchBar({
  value,
  onChange,
  colors,
}: {
  value: string;
  onChange: (v: string) => void;
  colors: ThemeColors;
}) {
  return (
    <View
      style={[
        searchBarS.wrap,
        { backgroundColor: colors.bgCard, borderColor: colors.border },
      ]}
    >
      <Search size={16} color={colors.textTer} />
      <TextInput
        style={[searchBarS.input, { color: colors.textPri }]}
        placeholder="Search ranked funds by name…"
        placeholderTextColor={colors.textTer}
        value={value}
        onChangeText={onChange}
        autoCorrect={false}
        autoCapitalize="none"
        returnKeyType="search"
      />
      {value.length > 0 && (
        <Pressable onPress={() => onChange("")} hitSlop={10}>
          <X size={15} color={colors.textTer} />
        </Pressable>
      )}
    </View>
  );
}

const searchBarS = StyleSheet.create({
  wrap: {
    flexDirection: "row",
    alignItems: "center",
    gap: space.sm,
    marginHorizontal: space.md,
    marginTop: space.sm,
    marginBottom: space.xs,
    paddingHorizontal: space.md,
    paddingVertical: 10,
    borderRadius: radii.lg,
    borderWidth: 1,
  },
  input: {
    flex: 1,
    fontSize: 14,
    padding: 0,
  },
});

// ── Error / empty state ───────────────────────────────────────────────────────

function EmptyState({
  msg,
  colors,
}: {
  msg: string;
  colors: ThemeColors;
}) {
  return (
    <View style={{ padding: space.xl, alignItems: "center", marginTop: space.lg }}>
      <AlertTriangle size={28} color={colors.textTer} />
      <Text
        style={{
          color: colors.textSec,
          marginTop: 12,
          textAlign: "center",
          fontWeight: "600",
          fontSize: 15,
        }}
      >
        {msg}
      </Text>
    </View>
  );
}

// ── Horizon mismatch banner ───────────────────────────────────────────────────

function HorizonBanner({
  tenure,
  resolvedHorizon,
  onDismiss,
  colors,
}: {
  tenure: number;
  resolvedHorizon: number;
  onDismiss: () => void;
  colors: ThemeColors;
}) {
  return (
    <View
      style={[
        bannerS.wrap,
        {
          backgroundColor: colors.azureSoft,
          borderColor: colors.azure + "35",
        },
      ]}
    >
      <Info size={14} color={colors.azure} style={{ marginTop: 1, flexShrink: 0 }} />
      <Text style={[bannerS.text, { color: colors.azure }]}>
        Showing{" "}
        <Text style={{ fontWeight: "800" }}>{resolvedHorizon}Y</Text> data.
        Some funds may show "—" until the nightly analysis completes.
      </Text>
      <Pressable onPress={onDismiss} hitSlop={10}>
        <Text style={{ color: colors.azure, fontWeight: "700", fontSize: 16, lineHeight: 20 }}>
          ×
        </Text>
      </Pressable>
    </View>
  );
}

const bannerS = StyleSheet.create({
  wrap: {
    flexDirection: "row",
    alignItems: "flex-start",
    gap: 9,
    marginHorizontal: space.md,
    marginTop: space.sm,
    padding: 12,
    borderRadius: radii.md,
    borderWidth: 1,
  },
  text: { flex: 1, fontSize: 13, lineHeight: 19 },
});
// ── Universe screen (shared by SIP and Lumpsum) ───────────────────────────────
//
// ONE screen renders both modes. Everything visual — header, benchmark bar,
// sort strip, category filters, fund cards, Additional Recommendations,
// loading, empty and error states — is identical. `mode` selects only:
//
//   1. which endpoint supplies the data  (api.universe)
//   2. which WM ↔ XIRR formula is used   (sip* vs lumpsum* in utils/xirr)
//   3. the mode label in the header      ("SIP" / "LUMP")
//
// Both endpoints return the same `UniverseResponse` shape (see
// _universe_metrics() in backend/server.py), so there is no field-name
// branching left anywhere in this file.

function UniverseScreen({ mode }: { mode: "sip" | "lumpsum" }) {
  const router = useRouter();
  const { colors } = useTheme();
  const { tenure } = useTenure();
  const {
    selectionMode, selectedFunds, enterSelectionMode, exitSelectionMode, toggleFund, isSelected,
    setToggleContext,
  } = useProposalDraft();

  const [loading,    setLoading]    = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error,      setError]      = useState<string | null>(null);
  const [result,     setResult]     = useState<UniverseResponse | null>(null);
  const [broadFilter,    setBroadFilter]    = useState<string | null>(null);
  const [subFilter,      setSubFilter]      = useState<string | null>(null);
  const [bonusCollapsed, setBonusCollapsed] = useState(false);
  const [customXirr,     setCustomXirr]     = useState<number | null>(null);
  const [searchQuery,    setSearchQuery]    = useState("");
  // "Since 2010" toggle — forces every fund's start date to 2010-01-01
  // instead of its own inception. See api.universe / backend/server.py
  // home_universe & lumpsum_universe.
  const [since2010, setSince2010] = useState(false);
  // "Separate Management Periods" toggle — scores funds with a verified
  // continuity break (merger/successor change or a manually-curated
  // management change) from that break date forward instead of their full
  // stitched history. See api.universe / backend/server.py home_universe &
  // lumpsum_universe / _continuity_applies.
  const [separateManagement, setSeparateManagement] = useState(false);
  // Per-card AMC period override (scheme_code → ISO start date or
  // FULL_HISTORY) and its live recomputation. Absent = the server default.
  const [periodChoice, setPeriodChoice] = useState<Record<number, string>>({});
  const [periodCalc, setPeriodCalc] = useState<Record<number, PeriodCalc>>({});
  const periodReq = useRef<Record<number, number>>({});

  const load = useCallback(async (silent = false) => {
    if (!tenure) return;
    if (!silent) setLoading(true);
    setError(null);
    try {
      if (silent) invalidateApiCache("/home/universe");
      if (silent) invalidateApiCache("/lumpsum/universe");
      const r = await api.universe(mode, tenure, since2010, separateManagement);
      setResult(r);
      setBroadFilter(null);
      setSubFilter(null);
      setBonusCollapsed(false);
      // A new universe (tenure/mode/toggle change) means new card numbers;
      // per-card overrides computed for the old one no longer apply.
      setPeriodChoice({});
      setPeriodCalc({});
      periodReq.current = {};
    } catch (e: any) {
      // The SIP universe returns 503 until the nav-meta index has been built.
      setError(
        /HTTP 503/.test(String(e?.message ?? ""))
          ? "Fund index not built yet. Come back shortly."
          : "Couldn't load funds. Pull down to retry."
      );
    } finally {
      setLoading(false);
    }
  }, [tenure, mode, since2010, separateManagement]);

  useEffect(() => { load(); }, [load]);

  // The backend caps fund calculations at 15Y even when the user's entered
  // tenure is higher — `resolved_horizon` echoes back the horizon it actually
  // used, so horizon-dependent display math stays consistent with the data.
  const calcHorizon = result?.resolved_horizon ?? tenure ?? 10;

  // Fund Detail must show the SAME basis as the card the user just tapped —
  // otherwise the 2010/Management toggles here silently stop applying the moment
  // you drill in, and FQS/XIRR/Success% can differ with no indication why.
  // `continuityResetDate` is only non-null on a fund when the Management toggle is
  // actually in effect for it (server.py only echoes it back when `applied`
  // is true), so its presence alone tells Fund Detail which basis to load.
  const fundDetailHref = useCallback(
    (schemeCode: number, horizon: number, continuityResetDate?: string | null) => {
      const params = new URLSearchParams();
      params.set("horizon", String(horizon));
      const choice = periodChoice[schemeCode];
      if (choice && choice !== FULL_HISTORY) {
        // The card's chosen AMC period travels with it so Fund Detail opens
        // on the same history the card was scored on.
        params.set("amcStart", choice);
      } else if (choice === FULL_HISTORY) {
        if (since2010) params.set("since2010", "1");
      } else {
        if (since2010) params.set("since2010", "1");
        if (separateManagement) params.set("mgmt", "1");
        if (continuityResetDate) params.set("contDate", continuityResetDate);
      }
      return `/fund/${schemeCode}?${params.toString()}`;
    },
    [since2010, separateManagement, periodChoice],
  );

  /** Pick (or reset) a card's AMC period and recompute its numbers from
   *  that date at `horizon` (the tenure, or a bonus fund's best_horizon). */
  const selectPeriod = useCallback(
    async (schemeCode: number, horizon: number, value: string, serverDefault: string) => {
      if (value === serverDefault) {
        periodReq.current[schemeCode] = (periodReq.current[schemeCode] ?? 0) + 1;
        setPeriodChoice((p) => { const n = { ...p }; delete n[schemeCode]; return n; });
        setPeriodCalc((p) => { const n = { ...p }; delete n[schemeCode]; return n; });
        return;
      }
      const req = (periodReq.current[schemeCode] ?? 0) + 1;
      periodReq.current[schemeCode] = req;
      setPeriodChoice((p) => ({ ...p, [schemeCode]: value }));
      setPeriodCalc((p) => ({
        ...p,
        [schemeCode]: { loading: true, stats: p[schemeCode]?.stats ?? null, error: null, noData: false },
      }));
      try {
        const opts = {
          startDate: value === FULL_HISTORY ? undefined : value,
          includePredecessor: true,
          summary: true,
        };
        const d = mode === "lumpsum"
          ? await api.analyzeLumpsum(schemeCode, opts)
          : await api.analyze(schemeCode, opts);
        if (periodReq.current[schemeCode] !== req) return;
        const h = (d.horizons as any[]).find((x) => x.horizon === horizon);
        const stats: CardStats | null = h
          ? {
              blended_wm: h.blended_wm ?? null,
              fqs: h.fqs ?? null,
              fqs_tier: tierFor(h.fqs),
              success_pct: h.success_pct ?? null,
              n_windows: h.n_windows ?? null,
              cv: h.cv ?? null,
              p10_wm: h.p10_wm ?? null,
            }
          : null;
        setPeriodCalc((p) => ({ ...p, [schemeCode]: { loading: false, stats, error: null, noData: !h } }));
      } catch (e: any) {
        if (periodReq.current[schemeCode] !== req) return;
        setPeriodCalc((p) => ({
          ...p,
          [schemeCode]: { loading: false, stats: null, error: String(e?.message ?? e), noData: false },
        }));
      }
    },
    [mode],
  );

  /** Card-ready numbers + picker props for one fund. */
  const periodView = useCallback(
    (f: { scheme_code: number; amc_breaks?: AmcBreak[] | null; continuity_reset_date?: string | null }, horizon: number) => {
      if (!separateManagement) return null;
      const options = periodOptionsFor(f.amc_breaks, f.continuity_reset_date);
      if (!options) return null;
      const serverDefault = f.continuity_reset_date ?? FULL_HISTORY;
      const active = periodChoice[f.scheme_code] ?? serverDefault;
      const calc = periodChoice[f.scheme_code] ? periodCalc[f.scheme_code] : undefined;
      const year = active === FULL_HISTORY ? null : active.slice(0, 4);
      const note = calc?.error
        ? "Couldn't recalculate this period — showing the previous numbers. Tap again to retry."
        : calc?.noData
          ? `Not enough history from ${year} for a ${horizon}Y rolling window.`
          : null;
      // undefined = show the server's numbers; null = this period has no
      // window at this horizon (card shows dashes); otherwise the recompute.
      let display: CardStats | null | undefined;
      if (!calc || calc.error) display = undefined;
      else if (calc.loading) display = calc.stats ?? undefined;
      else display = calc.stats;
      return {
        options,
        active,
        busy: !!calc?.loading,
        note,
        display,
        onSelect: (v: string) => selectPeriod(f.scheme_code, horizon, v, serverDefault),
      };
    },
    [separateManagement, periodChoice, periodCalc, selectPeriod],
  );

  /** Proposal basis for a fund whose card has an AMC period chosen. */
  const draftBasis = useCallback(
    (schemeCode: number) => {
      const choice = periodChoice[schemeCode];
      if (!choice) return {};
      return {
        start_date: choice === FULL_HISTORY ? null : choice,
        include_predecessor: true,
        explicit_basis: true,
      };
    },
    [periodChoice],
  );

  const displayFunds = useMemo(() => {
    if (!result) return [];
    const funds = result.eligible_funds;
    const byWm  = (a: EligibleFund, b: EligibleFund) =>
      (b.blended_wm ?? -1) - (a.blended_wm ?? -1);

    if (broadFilter === null) {
      // "All" — core equity subs + all Hybrid, sorted by Blended WM
      return [...funds]
        .filter((f) => CORE_SUBS.has(f.amfi_sub ?? "") || f.broad_category === "Hybrid")
        .sort(byWm);
    }
    if (subFilter === null) {
      // All within a broad category
      return [...funds].filter((f) => f.broad_category === broadFilter).sort(byWm);
    }
    // Specific subcategory — sort by FQS desc
    return [...funds]
      .filter((f) => f.amfi_sub === subFilter)
      .sort((a, b) => (b.fqs ?? -1) - (a.fqs ?? -1));
  }, [result, broadFilter, subFilter]);

  /** Bonus funds filtered + sorted by best_horizon desc, then blended_xirr desc */
  const displayBonus = useMemo(() => {
    // Additional Recommendations: backend decides eligibility (history 5Y up to
    // just below Core, longest window with ≥10 rolling windows, XIRR ≥ 15%).
    if (!result?.bonus_funds?.length || !tenure) return [];
    const byHorizon = (a: BonusFund, b: BonusFund) =>
      b.best_horizon !== a.best_horizon
        ? b.best_horizon - a.best_horizon
        : (b.blended_xirr ?? -1) - (a.blended_xirr ?? -1);
    const bf = result.bonus_funds;
    if (broadFilter === null) {
      // "All" — same whitelist as the core list: core equity subs + Hybrid.
      // Debt / Index / Gold / Sectoral / Other only appear via explicit
      // category or sub-category filters.
      return bf
        .filter((f) => CORE_SUBS.has(f.amfi_sub ?? "") || f.broad_category === "Hybrid")
        .sort(byHorizon);
    }
    if (subFilter === null) return bf.filter((f) => f.broad_category === broadFilter).sort(byHorizon);
    return bf.filter((f) => f.amfi_sub === subFilter).sort(byHorizon);
  }, [result, broadFilter, subFilter, tenure]);

  const sortLabel = subFilter
    ? `${subFilter} · by FQS`
    : broadFilter
      ? `${broadFilter} · by Blended WM`
      : "Core funds · by Blended WM";

  // Custom benchmark derived values — the one genuinely mode-dependent
  // calculation on this screen (SIP annuity vs lumpsum compounding).
  const customMinWm = useMemo(() =>
    customXirr !== null && tenure
      ? (mode === "sip"
          ? sipXirrToMinWm(customXirr, calcHorizon)
          : lumpsumXirrToMinWm(customXirr, calcHorizon))
      : null,
    [customXirr, tenure, calcHorizon, mode],
  );
  const passingCount = useMemo(() =>
    customMinWm !== null
      ? displayFunds.filter((f) => (f.blended_wm ?? 0) >= customMinWm).length
      : 0,
    [displayFunds, customMinWm],
  );

  // Search narrows the already-ranked lists by name — it never changes the
  // ranking/sort order, only which of the ranked rows are shown.
  const searchTerm = searchQuery.trim().toLowerCase();
  const searchedFunds = useMemo(
    () => searchTerm
      ? displayFunds.filter((f) => f.scheme_name.toLowerCase().includes(searchTerm))
      : displayFunds,
    [displayFunds, searchTerm],
  );
  const searchedBonus = useMemo(
    () => searchTerm
      ? displayBonus.filter((f) => f.scheme_name.toLowerCase().includes(searchTerm))
      : displayBonus,
    [displayBonus, searchTerm],
  );

  return (
    <SafeAreaView style={{ flex: 1, backgroundColor: colors.bg }} edges={["top"]}>
      <HomeHeader
        mode={mode}
        tenure={tenure}
        onPressSettings={() => router.push("/tenure-picker" as any)}
        colors={colors}
        selectionMode={selectionMode}
        selectedCount={selectedFunds.length}
        onEnterSelect={enterSelectionMode}
        onCancelSelect={exitSelectionMode}
        onBuildProposal={() => {
          // Lock in whatever Home is showing right now — the Proposal
          // Builder screen (and the backend build endpoint) use this so the
          // proposal matches the numbers the advisor actually picked funds
          // from, instead of silently reverting to plain since-inception data.
          setToggleContext(since2010, separateManagement);
          exitSelectionMode();
          router.push("/proposal/build" as any);
        }}
        since2010={since2010}
        onToggleSince2010={() => setSince2010((v) => !v)}
        separateManagement={separateManagement}
        onToggleSeparateManagement={() => setSeparateManagement((v) => !v)}
      />

      {loading && <SkeletonList colors={colors} />}

      {!loading && error && <EmptyState msg={error} colors={colors} />}

      {!loading && !error && result && (
        <FlatList
          data={searchedFunds}
          keyExtractor={(f) => String(f.scheme_code)}
          refreshControl={
            <RefreshControl
              refreshing={refreshing}
              onRefresh={async () => {
                setRefreshing(true);
                await load(true);
                setRefreshing(false);
              }}
              tintColor={colors.azure}
            />
          }
          ListHeaderComponent={
            <>
              <BenchmarkBar
                mode={mode}
                tenure={calcHorizon}
                customXirr={customXirr}
                onChangeXirr={setCustomXirr}
                passingCount={passingCount}
                totalCount={displayFunds.length}
                colors={colors}
              />
              <SortStrip
                count={searchedFunds.length}
                minHistory={result.required_history_years}
                sortLabel={sortLabel}
                colors={colors}
                since2010={since2010}
                separateManagement={separateManagement}
                scoresLastRefreshed={result.scores_last_refreshed}
              />
              <TwoLevelFilter
                funds={result.eligible_funds}
                broad={broadFilter}
                sub={subFilter}
                onBroad={setBroadFilter}
                onSub={setSubFilter}
                colors={colors}
              />
              <FundSearchBar
                value={searchQuery}
                onChange={setSearchQuery}
                colors={colors}
              />
              {searchedBonus.length > 0 && (
                <>
                  <BonusSectionHeader
                    count={searchedBonus.length}
                    collapsed={bonusCollapsed}
                    onToggle={() => setBonusCollapsed((v) => !v)}
                    colors={colors}
                  />
                  {!bonusCollapsed && searchedBonus.map((bf) => {
                    const pv = periodView(bf, bf.best_horizon);
                    const choice = periodChoice[bf.scheme_code];
                    const toXirr = mode === "sip" ? sipWmToXirr : lumpsumWmToXirr;
                    const fund: BonusFund = pv?.display === undefined || !pv
                      ? bf
                      : {
                          ...bf,
                          ...(pv.display ?? {
                            blended_wm: null, fqs: null, fqs_tier: null, success_pct: null,
                            n_windows: null, cv: null, p10_wm: null,
                          }),
                          blended_xirr: pv.display?.blended_wm != null ? toXirr(pv.display.blended_wm, bf.best_horizon) : null,
                          p10_xirr: pv.display?.p10_wm != null ? toXirr(pv.display.p10_wm, bf.best_horizon) : null,
                          continuity_reset_date: choice && choice !== FULL_HISTORY ? choice : null,
                        };
                    return (
                      <BonusFundCard
                        key={bf.scheme_code}
                        fund={fund}
                        onPress={() =>
                          selectionMode
                            ? toggleFund({
                                scheme_code: bf.scheme_code, scheme_name: bf.scheme_name,
                                ...draftBasis(bf.scheme_code),
                              })
                            : router.push(fundDetailHref(bf.scheme_code, bf.best_horizon, bf.continuity_reset_date) as any)
                        }
                        colors={colors}
                        selectionMode={selectionMode}
                        selected={isSelected(bf.scheme_code)}
                        period={pv}
                      />
                    );
                  })}
                  {!bonusCollapsed && <View style={{ height: 6 }} />}
                </>
              )}
            </>
          }
          initialNumToRender={8}
          maxToRenderPerBatch={8}
          windowSize={9}
          renderItem={({ item, index }) => {
            const pv = periodView(item, calcHorizon);
            const s: CardStats | EligibleFund = pv?.display !== undefined && pv?.display !== null
              ? pv.display
              : item;
            const dashes = pv?.display === null;
            const choice = periodChoice[item.scheme_code];
            const since = choice
              ? (choice === FULL_HISTORY ? null : choice)
              : item.continuity_reset_date;
            return (
              <FundCard
                rank={index + 1}
                name={item.scheme_name}
                category={item.amfi_sub ?? item.category}
                blendedWm={dashes ? null : s.blended_wm}
                fqs={dashes ? null : s.fqs}
                fqsTier={dashes ? null : s.fqs_tier}
                successPct={dashes ? null : s.success_pct}
                nWindows={dashes ? null : s.n_windows}
                cv={dashes ? null : s.cv}
                p10Wm={dashes ? null : s.p10_wm}
                mode={mode}
                horizon={calcHorizon}
                onPress={() =>
                  selectionMode
                    ? toggleFund({
                        scheme_code: item.scheme_code, scheme_name: item.scheme_name,
                        ...draftBasis(item.scheme_code),
                      })
                    : router.push(fundDetailHref(item.scheme_code, calcHorizon, item.continuity_reset_date) as any)
                }
                colors={colors}
                overrideMinWm={customMinWm}
                selectionMode={selectionMode}
                selected={isSelected(item.scheme_code)}
                continuitySince={since}
                continuityReason={choice ? "AMC period you selected" : item.continuity_reason}
                periodOptions={pv?.options ?? null}
                activePeriod={pv?.active}
                onSelectPeriod={pv?.onSelect}
                periodBusy={pv?.busy ?? false}
                periodNote={pv?.note ?? null}
              />
            );
          }}
          ListEmptyComponent={
            <EmptyState
              msg={
                searchTerm
                  ? `No funds match "${searchQuery.trim()}"`
                  : subFilter
                    ? `No funds in "${subFilter}" for this tenure`
                    : broadFilter
                      ? `No ${broadFilter} funds for this tenure`
                      : since2010
                        ? "No funds with Since-2010 scores yet for this tenure — the nightly refresh may still be catching up."
                        : "No eligible funds found"
              }
              colors={colors}
            />
          }
          contentContainerStyle={{ paddingBottom: 48, paddingTop: 4 }}
        />
      )}
    </SafeAreaView>
  );
}

// ── Root export ───────────────────────────────────────────────────────────────

export default function HomeScreen() {
  const { mode } = useMode();
  return <UniverseScreen mode={mode === "lumpsum" ? "lumpsum" : "sip"} />;
}
