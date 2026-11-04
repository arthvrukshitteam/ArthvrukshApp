import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  View,
  Text,
  StyleSheet,
  TextInput,
  FlatList,
  ActivityIndicator,
  RefreshControl,
  Pressable,
  Keyboard,
  KeyboardAvoidingView,
  Platform,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { useRouter } from "expo-router";
import { Search, X, ChevronRight, Database, Clock, TrendingUp, Star, Zap } from "lucide-react-native";
import { api, type RecentFund, type FullRefreshStatus, type FundSearchResult } from "@/src/api";
import { radii, space, type ThemeColors, type ThemeType } from "@/src/theme";
import { useTheme } from "@/src/ThemeProvider";
import { useTenure } from "@/src/TenureProvider";
import { BrandLogo } from "@/src/components/BrandLogo";

// ── main screen ──────────────────────────────────────────────────────────────

export default function FundsScreen() {
  const router = useRouter();
  const { colors, type, tierColor } = useTheme();
  const { tenure } = useTenure();
  const styles = useMemo(() => makeStyles(colors, type), [colors, type]);

  const [query, setQuery] = useState("");
  const [results, setResults] = useState<FundSearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [recent, setRecent] = useState<RecentFund[]>([]);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshStatus, setRefreshStatus] = useState<FullRefreshStatus | null>(null);
  const [searchFocused, setSearchFocused] = useState(false);
  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null);

  const loadRecent = useCallback(async () => {
    try {
      const [recentRes, statusRes] = await Promise.all([
        api.recent(),
        api.fullRefreshStatus(),   // new combined endpoint
      ]);
      setRecent(recentRes.results);
      setRefreshStatus(statusRes);
    } catch {
      // ignore — stale data is fine
    }
  }, []);

  useEffect(() => { loadRecent(); }, [loadRecent]);

  useEffect(() => {
    if (debounce.current) clearTimeout(debounce.current);
    const q = query.trim();
    if (q.length < 2) { setResults([]); setSearching(false); return; }
    setSearching(true);
    debounce.current = setTimeout(async () => {
      try { setResults((await api.search(q)).results); }
      catch { setResults([]); }
      finally { setSearching(false); }
    }, 280);
  }, [query]);

  const onRefresh = useCallback(async () => {
    setRefreshing(true);
    await loadRecent();
    setRefreshing(false);
  }, [loadRecent]);

  const showSearch = query.trim().length >= 2;

  return (
    <SafeAreaView style={styles.safe} edges={["top"]}>
      <KeyboardAvoidingView
        style={{ flex: 1 }}
        behavior={Platform.OS === "ios" ? "padding" : undefined}
      >
        {/* Navbar */}
        <View style={styles.navbar}>
          <BrandLogo size={100} style={styles.navLogo} />
          <View style={styles.navText}>
            <Text style={styles.navBrand}>ArthVruksh</Text>
            <Text style={styles.navSub}>FQS Model</Text>
          </View>
        </View>

        {/* Search section */}
        <View style={styles.searchSection}>
          <Text style={styles.pageLabel}>Analyse a Fund · AMFI schemes, duplicate listings merged</Text>
          <View style={[
            styles.searchWrap,
            searchFocused && { borderColor: colors.azure, shadowColor: colors.azure, shadowOpacity: 0.18, elevation: 8 },
          ]}>
            <Search size={18} color={searchFocused ? colors.azure : colors.textTer} />
            <TextInput
              style={styles.searchInput}
              placeholder="Search by fund name or scheme code…"
              placeholderTextColor={colors.textTer}
              value={query}
              onChangeText={setQuery}
              onFocus={() => setSearchFocused(true)}
              onBlur={() => setSearchFocused(false)}
              autoCorrect={false}
              autoCapitalize="none"
              returnKeyType="search"
              onSubmitEditing={() => Keyboard.dismiss()}
            />
            {query.length > 0 && (
              <Pressable onPress={() => setQuery("")} hitSlop={12}>
                <X size={16} color={colors.textTer} />
              </Pressable>
            )}
          </View>
        </View>

        {/* Content */}
        {showSearch ? (
          <SearchResults
            loading={searching}
            results={results}
            onPick={(c) =>
              router.push(
                `/fund/${c}${tenure ? `?horizon=${tenure}` : ""}` as any
              )
            }
            colors={colors}
            type={type}
            styles={styles}
          />
        ) : (
          <FlatList
            data={recent}
            keyExtractor={(it) => String(it.scheme_code)}
            ListHeaderComponent={
              <View>
                {/* Dual refresh status card */}
                {refreshStatus && (
                  <RefreshStatusCard status={refreshStatus} colors={colors} type={type} styles={styles} />
                )}
                <View style={styles.sectionHeader}>
                  <TrendingUp size={13} color={colors.textTer} />
                  <Text style={[type.label, { color: colors.textTer, marginLeft: 6 }]}>
                    RECENTLY ANALYSED
                  </Text>
                </View>
              </View>
            }
            renderItem={({ item }) => (
              <RecentRow
                fund={item}
                colors={colors}
                type={type}
                styles={styles}
                tierColor={tierColor}
                onPress={() =>
                  router.push(
                    `/fund/${item.scheme_code}${tenure ? `?horizon=${tenure}` : ""}` as any
                  )
                }
              />
            )}
            refreshControl={
              <RefreshControl tintColor={colors.azure} refreshing={refreshing} onRefresh={onRefresh} />
            }
            ListEmptyComponent={
              <View style={styles.emptyWrap}>
                <Database size={32} color={colors.textTer} />
                <Text style={[type.body, { color: colors.textSec, marginTop: 14, textAlign: "center", fontWeight: "600" }]}>
                  No funds analysed yet
                </Text>
                <Text style={[type.bodySm, { color: colors.textTer, marginTop: 6, textAlign: "center", maxWidth: 240 }]}>
                  Search for any mutual fund above to get started.
                </Text>
              </View>
            }
            contentContainerStyle={{ paddingBottom: 40 }}
          />
        )}
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

// ── RefreshStatusCard ─────────────────────────────────────────────────────────
// Shows NAV refresh and FQS batch refresh in a single compact card.
// Two rows: one for NAV, one for FQS.

function RefreshStatusCard({
  status, colors, type, styles,
}: {
  status: FullRefreshStatus;
  colors: ThemeColors;
  type: ThemeType;
  styles: ReturnType<typeof makeStyles>;
}) {
  const { nav, fqs } = status;

  const navLabel = nav.running
    ? "NAV refresh running…"
    : nav.last_run
      ? `NAV refreshed ${formatRelative(nav.last_run)} · ${nav.schemes_refreshed} schemes`
      : "NAV refresh runs at 03:30 IST";

  const fqsLabel = fqs.running
    ? `FQS ranking running… (${fqs.refreshed ?? 0}/${fqs.total ?? "?"} done)`
    : fqs.last_run
      ? `FQS ranked ${formatRelative(fqs.last_run)} · ${fqs.refreshed}/${fqs.total} funds`
      : "FQS ranking runs at 04:00 IST";

  return (
    <View style={styles.refreshCard}>
      <View style={styles.refreshRow}>
        <Clock size={12} color={nav.running ? colors.azure : colors.textTer} />
        <Text style={[type.bodySm, { flex: 1, marginLeft: 7, color: nav.running ? colors.azure : colors.textSec }]}>
          {navLabel}
        </Text>
        {nav.running && <ActivityIndicator size="small" color={colors.azure} />}
      </View>
      <View style={[styles.refreshRow, { marginTop: 6 }]}>
        <Star size={12} color={fqs.running ? colors.azure : colors.textTer} fill={fqs.running ? colors.azure : "none"} />
        <Text style={[type.bodySm, { flex: 1, marginLeft: 7, color: fqs.running ? colors.azure : colors.textSec }]}>
          {fqsLabel}
        </Text>
        {fqs.running && <ActivityIndicator size="small" color={colors.azure} />}
      </View>
    </View>
  );
}

// ── RecentRow ─────────────────────────────────────────────────────────────────
// headline_fqs here is the longest-horizon FQS (from fund_analysis) — appropriate
// for the general recent-funds context where no specific rolling period is selected.
// The Home Universe screen shows horizon-specific FQS instead.

function RecentRow({
  fund, colors, type, styles, tierColor, onPress,
}: {
  fund: RecentFund;
  colors: ThemeColors;
  type: ThemeType;
  styles: ReturnType<typeof makeStyles>;
  tierColor: (tier: string | null) => string;
  onPress: () => void;
}) {
  const tCol = tierColor(fund.headline_tier ?? null);
  const xirrPct = fund.headline_xirr != null ? (fund.headline_xirr * 100).toFixed(1) + "%" : "—";
  const horizons = fund.available_horizons ?? [];
  const horizonLabel = horizons.length ? `${horizons[0]}–${horizons[horizons.length - 1]}Y` : "—";
  const headlineHorizon = fund.headline_horizon;

  return (
    <Pressable
      onPress={onPress}
      style={({ pressed }) => [styles.row, pressed && styles.pressed]}
    >
      <View style={{ flex: 1 }}>
        <Text style={styles.rowName} numberOfLines={2}>{fund.scheme_name}</Text>
        <View style={styles.rowMeta}>
          {fund.category && (
            <View style={styles.chip}><Text style={styles.chipText}>{fund.category}</Text></View>
          )}
          <Text style={styles.metaText}>XIRR {xirrPct}</Text>
          {headlineHorizon && (
            <Text style={styles.metaText}>· {headlineHorizon}Y basis</Text>
          )}
          {!headlineHorizon && horizons.length > 0 && (
            <Text style={styles.metaText}>{horizonLabel}</Text>
          )}
        </View>
      </View>
      {fund.headline_fqs != null ? (
        <View style={[styles.scorePill, { borderColor: tCol, backgroundColor: `${tCol}22` }]}>
          <Text style={[styles.scoreVal, { color: tCol }]}>{fund.headline_fqs.toFixed(0)}</Text>
          <Text style={[styles.scoreTier, { color: tCol }]}>
            {(fund.headline_tier ?? "").toUpperCase()}
          </Text>
        </View>
      ) : (
        // No FQS yet — show a pending indicator so user understands why it's absent
        <View style={[styles.scorePill, { borderColor: colors.border, backgroundColor: colors.bgCard }]}>
          <Zap size={12} color={colors.textTer} />
          <Text style={[styles.scoreTier, { color: colors.textTer, marginTop: 2 }]}>PENDING</Text>
        </View>
      )}
      <ChevronRight size={16} color={colors.textTer} style={{ marginLeft: 6 }} />
    </Pressable>
  );
}

// ── SearchResults ─────────────────────────────────────────────────────────────

function SearchResults({
  loading, results, onPick, colors, type, styles,
}: {
  loading: boolean;
  results: FundSearchResult[];
  onPick: (c: number) => void;
  colors: ThemeColors;
  type: ThemeType;
  styles: ReturnType<typeof makeStyles>;
}) {
  if (loading) {
    return <View style={{ padding: space.xl, alignItems: "center" }}><ActivityIndicator color={colors.azure} /></View>;
  }
  if (!results.length) {
    return (
      <View style={styles.emptyWrap}>
        <Search size={28} color={colors.textTer} />
        <Text style={[type.bodySm, { color: colors.textSec, marginTop: 12, textAlign: "center" }]}>
          No matching schemes. Try fewer keywords.
        </Text>
      </View>
    );
  }
  return (
    <FlatList
      data={results}
      keyExtractor={(it) => String(it.scheme_code)}
      keyboardShouldPersistTaps="handled"
      contentContainerStyle={{ paddingBottom: 40 }}
      renderItem={({ item }) => (
        <Pressable
          onPress={() => onPick(item.scheme_code)}
          style={({ pressed }) => [styles.searchRow, pressed && { backgroundColor: colors.bgElev }]}
        >
          <View style={{ flex: 1 }}>
            <Text style={[type.body, { fontWeight: "600" }]} numberOfLines={2}>{item.scheme_name}</Text>
            <Text style={[type.bodySm, { color: colors.textTer, marginTop: 2 }]}>
              Scheme #{item.scheme_code}
              {item.variant_count
                ? ` · ${item.variant_count} listings merged`
                : ""}
            </Text>
            {/* This tab is a LOOKUP surface, so a wound-up or merged scheme is
                still findable here — it is labelled rather than hidden. The
                Add Fund picker, where a fund is chosen to invest in, filters
                them out instead (see FundSearchModal). */}
            {item.is_active === false && (
              <Text style={[type.bodySm, { color: colors.amber, marginTop: 2, fontWeight: "600" }]}>
                {item.inactive_reason ?? "No longer available"}
              </Text>
            )}
          </View>
          <ChevronRight size={16} color={colors.textTer} />
        </Pressable>
      )}
    />
  );
}

// ── helpers ───────────────────────────────────────────────────────────────────

function formatRelative(iso: string): string {
  const d = new Date(iso);
  const sec = Math.max(0, (Date.now() - d.getTime()) / 1000);
  if (sec < 60) return `${Math.round(sec)}s ago`;
  if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.round(sec / 3600)}h ago`;
  return `${Math.round(sec / 86400)}d ago`;
}

// ── styles ────────────────────────────────────────────────────────────────────

function makeStyles(colors: ThemeColors, type: ThemeType) {
  return StyleSheet.create({
    safe: { flex: 1, backgroundColor: colors.bg },

    navbar: {
      flexDirection: "row",
      alignItems: "center",
      paddingHorizontal: space.md,
      paddingVertical: space.sm,
      borderBottomWidth: StyleSheet.hairlineWidth,
      borderBottomColor: colors.border,
      gap: space.sm,
    },
    navLogo: { marginBottom: 0 },
    navText: { flexDirection: "column", justifyContent: "center" },
    navBrand: {
      fontSize: 17,
      fontWeight: "700",
      color: colors.textPri,
      letterSpacing: -0.3,
    },
    navSub: {
      fontSize: 11,
      fontWeight: "600",
      color: colors.azure,
      letterSpacing: 0.8,
      textTransform: "uppercase",
    },

    searchSection: {
      paddingHorizontal: space.md,
      paddingTop: space.md,
      paddingBottom: space.sm,
      borderBottomWidth: StyleSheet.hairlineWidth,
      borderBottomColor: colors.border,
    },
    pageLabel: {
      fontSize: 12,
      fontWeight: "500",
      color: colors.textTer,
      marginBottom: space.sm,
      letterSpacing: 0.1,
    },
    searchWrap: {
      flexDirection: "row",
      alignItems: "center",
      gap: space.sm,
      paddingHorizontal: space.md,
      paddingVertical: 13,
      backgroundColor: colors.bgCard,
      borderRadius: radii.lg,
      borderWidth: 1.5,
      borderColor: colors.border,
      shadowColor: "#000",
      shadowOpacity: 0.06,
      shadowRadius: 8,
      shadowOffset: { width: 0, height: 2 },
      elevation: 3,
    },
    searchInput: {
      flex: 1,
      color: colors.textPri,
      fontSize: 15,
      padding: 0,
    },

    refreshCard: {
      marginHorizontal: space.md,
      marginTop: space.md,
      marginBottom: space.xs,
      paddingHorizontal: space.md,
      paddingVertical: space.sm,
      backgroundColor: colors.bgCard,
      borderRadius: radii.sm,
      borderWidth: 1,
      borderColor: colors.border,
    },
    refreshRow: {
      flexDirection: "row",
      alignItems: "center",
    },

    sectionHeader: {
      flexDirection: "row",
      alignItems: "center",
      paddingHorizontal: space.md,
      paddingTop: space.md,
      paddingBottom: space.sm,
    },
    row: {
      flexDirection: "row",
      alignItems: "center",
      gap: space.sm,
      paddingVertical: space.md,
      paddingHorizontal: space.md,
      borderBottomWidth: StyleSheet.hairlineWidth,
      borderColor: colors.border,
    },
    pressed: { backgroundColor: colors.bgCard, opacity: 0.85 },
    rowName: { ...type.body, color: colors.textPri, fontWeight: "600" },
    rowMeta: { flexDirection: "row", gap: space.sm, marginTop: 4, alignItems: "center", flexWrap: "wrap" },
    chip: {
      paddingHorizontal: 8,
      paddingVertical: 2,
      borderRadius: radii.sm,
      borderWidth: 1,
      borderColor: colors.border,
    },
    chipText: { ...type.bodySm, color: colors.textSec, fontSize: 11 },
    metaText: { ...type.bodySm, color: colors.textTer, fontVariant: ["tabular-nums"] },
    scorePill: {
      minWidth: 56,
      paddingHorizontal: 8,
      paddingVertical: 4,
      borderRadius: radii.sm,
      borderWidth: 1,
      alignItems: "center",
    },
    scoreVal: { fontSize: 16, fontWeight: "700", fontVariant: ["tabular-nums"] },
    scoreTier: { fontSize: 9, fontWeight: "700", letterSpacing: 1, marginTop: 1 },
    searchRow: {
      flexDirection: "row",
      alignItems: "center",
      gap: space.md,
      paddingVertical: space.md,
      paddingHorizontal: space.md,
      borderBottomWidth: StyleSheet.hairlineWidth,
      borderColor: colors.border,
    },
    emptyWrap: {
      padding: space.xl,
      alignItems: "center",
      marginTop: space.lg,
    },
  });
}