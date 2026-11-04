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
import { Search, X, ChevronRight, Database, Clock, TrendingUp } from "lucide-react-native";
import { api, type RecentFund } from "@/src/api";
import { radii, space, type ThemeColors, type ThemeType } from "@/src/theme";
import { useTheme } from "@/src/ThemeProvider";
import { BrandLogo } from "@/src/components/BrandLogo";

export default function FundsScreen() {
  const router = useRouter();
  const { colors, type, tierColor } = useTheme();
  const styles = useMemo(() => makeStyles(colors, type), [colors, type]);

  const [query, setQuery] = useState("");
  const [results, setResults] = useState<{ scheme_code: number; scheme_name: string }[]>([]);
  const [searching, setSearching] = useState(false);
  const [recent, setRecent] = useState<RecentFund[]>([]);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshInfo, setRefreshInfo] = useState<{ last_run?: string | null; schemes_refreshed?: number } | null>(null);
  const [searchFocused, setSearchFocused] = useState(false);
  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null);

  const loadRecent = useCallback(async () => {
    try {
      const r = await api.recent();
      setRecent(r.results);
      const s = await api.refreshStatus();
      setRefreshInfo(s);
    } catch {
      // ignore
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
        {/* Compact navbar */}
        <View style={styles.navbar}>
          <BrandLogo size={100} style={styles.navLogo} />
          <View style={styles.navText}>
            <Text style={styles.navBrand}>ArthVruksh</Text>
            <Text style={styles.navSub}>FQS Model</Text>
          </View>
        </View>

        {/* Search section */}
        <View style={styles.searchSection}>
          <Text style={styles.pageLabel}>Analyse a Fund · 37,000+ AMFI schemes</Text>
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
            onPick={(c) => router.push(`/fund/${c}` as any)}
          />
        ) : (
          <FlatList
            data={recent}
            keyExtractor={(it) => String(it.scheme_code)}
            ListHeaderComponent={
              <View>
                {refreshInfo && (
                  <View style={styles.refreshCard}>
                    <Clock size={13} color={colors.textSec} />
                    <Text style={[type.bodySm, { flex: 1, marginLeft: 8 }]}>
                      {refreshInfo.last_run
                        ? `Last NAV refresh ${formatRelative(refreshInfo.last_run)} · ${refreshInfo.schemes_refreshed} schemes`
                        : "Daily NAV refresh runs at 03:30 IST."}
                    </Text>
                  </View>
                )}
                <View style={styles.sectionHeader}>
                  <TrendingUp size={13} color={colors.textTer} />
                  <Text style={[type.label, { color: colors.textTer, marginLeft: 6 }]}>
                    RECENTLY ANALYSED
                  </Text>
                </View>
              </View>
            }
            renderItem={({ item }) => <RecentRow fund={item} />}
            refreshControl={<RefreshControl tintColor={colors.textSec} refreshing={refreshing} onRefresh={onRefresh} />}
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

  function RecentRow({ fund }: { fund: RecentFund }) {
    const tCol = tierColor(fund.headline_tier ?? null);
    const xirrPct = fund.headline_xirr != null ? (fund.headline_xirr * 100).toFixed(1) + "%" : "—";
    const horizons = fund.available_horizons ?? [];
    const horizonLabel = horizons.length ? `${horizons[0]}–${horizons[horizons.length - 1]}Y` : "—";

    return (
      <Pressable
        onPress={() => router.push(`/fund/${fund.scheme_code}` as any)}
        style={({ pressed }) => [styles.row, pressed && styles.pressed]}
      >
        <View style={{ flex: 1 }}>
          <Text style={styles.rowName} numberOfLines={2}>{fund.scheme_name}</Text>
          <View style={styles.rowMeta}>
            {fund.category && (
              <View style={styles.chip}><Text style={styles.chipText}>{fund.category}</Text></View>
            )}
            <Text style={styles.metaText}>XIRR {xirrPct}</Text>
            <Text style={styles.metaText}>{horizonLabel}</Text>
          </View>
        </View>
        {fund.headline_fqs != null && (
          <View style={[styles.scorePill, { borderColor: tCol, backgroundColor: `${tCol}22` }]}>
            <Text style={[styles.scoreVal, { color: tCol }]}>{fund.headline_fqs.toFixed(0)}</Text>
            <Text style={[styles.scoreTier, { color: tCol }]}>
              {(fund.headline_tier ?? "").toUpperCase()}
            </Text>
          </View>
        )}
        <ChevronRight size={16} color={colors.textTer} style={{ marginLeft: 6 }} />
      </Pressable>
    );
  }

  function SearchResults({
    loading, results, onPick,
  }: { loading: boolean; results: { scheme_code: number; scheme_name: string }[]; onPick: (c: number) => void }) {
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
              <Text style={[type.bodySm, { color: colors.textTer, marginTop: 2 }]}>Scheme #{item.scheme_code}</Text>
            </View>
            <ChevronRight size={16} color={colors.textTer} />
          </Pressable>
        )}
      />
    );
  }
}

function formatRelative(iso: string): string {
  const d = new Date(iso);
  const sec = Math.max(0, (Date.now() - d.getTime()) / 1000);
  if (sec < 60) return `${Math.round(sec)}s ago`;
  if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.round(sec / 3600)}h ago`;
  return `${Math.round(sec / 86400)}d ago`;
}

function makeStyles(colors: ThemeColors, type: ThemeType) {
  return StyleSheet.create({
    safe: { flex: 1, backgroundColor: colors.bg },

    // Navbar
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

    // Search section
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

    // Content
    refreshCard: {
      flexDirection: "row",
      alignItems: "center",
      marginHorizontal: space.md,
      marginTop: space.md,
      marginBottom: space.xs,
      paddingHorizontal: space.md,
      paddingVertical: 10,
      backgroundColor: colors.bgCard,
      borderRadius: radii.sm,
      borderWidth: 1,
      borderColor: colors.border,
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
