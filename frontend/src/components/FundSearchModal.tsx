/**
 * Fund search picker, presented as a modal sheet.
 *
 * Reuses the SAME `api.search` endpoint and the same debounce/min-length
 * behaviour as the Funds tab — this is not a second search implementation.
 * Presenting it as a modal (rather than routing to the Funds tab) is what lets
 * the advisor add a fund without leaving the proposal and losing their edits.
 *
 * `excludeCodes` prevents adding a fund the portfolio already holds: duplicates
 * would double-count an allocation and the backend has no unique constraint on
 * scheme_code within a proposal.
 */

import { useEffect, useRef, useState } from "react";
import {
  View, Text, StyleSheet, TextInput, FlatList, Pressable,
  ActivityIndicator, Modal, KeyboardAvoidingView, Platform,
} from "react-native";
import { Search, X, Check } from "lucide-react-native";
import { api } from "@/src/api";
import { useTheme } from "@/src/ThemeProvider";
import { radii, space, type ThemeColors } from "@/src/theme";

export interface PickedFund {
  scheme_code: number;
  scheme_name: string;
}

/** Row shape this picker reads. Mirrors `FundSearchResult` in src/api.ts. */
interface SearchRow {
  scheme_code: number;
  scheme_name: string;
  /** Present when the lineage resolver matched a historical identifier. */
  resolved_from?: string | null;
  formerly_known_as?: string | null;
}

export function FundSearchModal({
  visible,
  onClose,
  onPick,
  excludeCodes = [],
  title = "Add Fund",
}: {
  visible: boolean;
  onClose: () => void;
  onPick: (fund: PickedFund) => void;
  excludeCodes?: number[];
  title?: string;
}) {
  const { colors } = useTheme();
  const s = makeStyles(colors);

  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchRow[]>([]);
  const [searching, setSearching] = useState(false);
  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!visible) { setQuery(""); setResults([]); }
  }, [visible]);

  // Same 280 ms debounce / 2-character minimum the Funds tab uses.
  useEffect(() => {
    if (debounce.current) clearTimeout(debounce.current);
    const q = query.trim();
    if (q.length < 2) { setResults([]); setSearching(false); return; }
    setSearching(true);
    debounce.current = setTimeout(async () => {
      // activeOnly: this picker adds a fund to a portfolio the client will
      // actually invest in, so wound-up and merged schemes must not be
      // offered here. The Funds tab's own search leaves them in (labelled),
      // because looking one up is a legitimate thing to do.
      try { setResults(((await api.search(q, { activeOnly: true })) as any).results ?? []); }
      catch { setResults([]); }
      finally { setSearching(false); }
    }, 280);
    return () => { if (debounce.current) clearTimeout(debounce.current); };
  }, [query]);

  const excluded = new Set(excludeCodes);

  return (
    <Modal visible={visible} animationType="slide" transparent onRequestClose={onClose}>
      <View style={s.backdrop}>
        <KeyboardAvoidingView
          behavior={Platform.OS === "ios" ? "padding" : undefined}
          style={s.sheet}
        >
          <View style={s.handle} />
          <View style={s.header}>
            <Text style={s.title}>{title}</Text>
            <Pressable onPress={onClose} hitSlop={12} accessibilityLabel="Close fund search">
              <X size={20} color={colors.textSec} />
            </Pressable>
          </View>

          <View style={s.searchWrap}>
            <Search size={18} color={colors.textTer} />
            <TextInput
              style={s.input}
              placeholder="Search by fund name, scheme code or ISIN…"
              placeholderTextColor={colors.textTer}
              value={query}
              onChangeText={setQuery}
              autoCorrect={false}
              autoCapitalize="none"
              autoFocus
              returnKeyType="search"
            />
            {query.length > 0 && (
              <Pressable onPress={() => setQuery("")} hitSlop={12}>
                <X size={16} color={colors.textTer} />
              </Pressable>
            )}
          </View>

          {searching ? (
            <View style={s.centre}><ActivityIndicator color={colors.azure} /></View>
          ) : query.trim().length < 2 ? (
            <View style={s.centre}>
              <Text style={s.hint}>Type at least 2 characters to search.</Text>
            </View>
          ) : results.length === 0 ? (
            <View style={s.centre}>
              <Text style={s.hint}>No matching schemes. Try fewer keywords.</Text>
            </View>
          ) : (
            <FlatList
              data={results}
              keyExtractor={(it) => String(it.scheme_code)}
              keyboardShouldPersistTaps="handled"
              contentContainerStyle={{ paddingBottom: space.xl }}
              renderItem={({ item }) => {
                const already = excluded.has(item.scheme_code);
                const former = item.resolved_from ?? item.formerly_known_as;
                return (
                  <Pressable
                    disabled={already}
                    onPress={() => onPick({ scheme_code: item.scheme_code, scheme_name: item.scheme_name })}
                    style={({ pressed }) => [
                      s.row,
                      pressed && { backgroundColor: colors.bgElev },
                      already && { opacity: 0.45 },
                    ]}
                  >
                    <View style={{ flex: 1 }}>
                      <Text style={s.rowName} numberOfLines={2}>{item.scheme_name}</Text>
                      <Text style={s.rowMeta}>
                        Scheme #{item.scheme_code}
                        {former ? ` · formerly ${former}` : ""}
                      </Text>
                    </View>
                    {already
                      ? <Check size={16} color={colors.azure} />
                      : <Text style={s.add}>ADD</Text>}
                  </Pressable>
                );
              }}
            />
          )}
        </KeyboardAvoidingView>
      </View>
    </Modal>
  );
}

function makeStyles(colors: ThemeColors) {
  return StyleSheet.create({
    backdrop: { flex: 1, backgroundColor: colors.backdrop, justifyContent: "flex-end" },
    sheet: {
      maxHeight: "85%", minHeight: "60%",
      backgroundColor: colors.bgCard,
      borderTopLeftRadius: 20, borderTopRightRadius: 20,
      borderWidth: 1, borderColor: colors.border,
      paddingHorizontal: space.md, paddingBottom: space.md,
    },
    handle: {
      alignSelf: "center", width: 40, height: 4, borderRadius: 2,
      backgroundColor: colors.border, marginTop: space.sm, marginBottom: space.sm,
    },
    header: {
      flexDirection: "row", alignItems: "center", justifyContent: "space-between",
      marginBottom: space.sm,
    },
    title: { fontSize: 17, fontWeight: "800", color: colors.textPri },
    searchWrap: {
      flexDirection: "row", alignItems: "center", gap: space.sm,
      paddingHorizontal: space.md, paddingVertical: 12,
      backgroundColor: colors.bg, borderRadius: radii.lg,
      borderWidth: 1.5, borderColor: colors.border, marginBottom: space.sm,
    },
    input: { flex: 1, color: colors.textPri, fontSize: 15, padding: 0 },
    centre: { flex: 1, alignItems: "center", justifyContent: "center", padding: space.xl },
    hint: { color: colors.textTer, fontSize: 13, textAlign: "center" },
    row: {
      flexDirection: "row", alignItems: "center", gap: space.md,
      paddingVertical: space.md, paddingHorizontal: space.sm,
      borderBottomWidth: StyleSheet.hairlineWidth, borderColor: colors.border,
    },
    rowName: { fontSize: 14, fontWeight: "600", color: colors.textPri },
    rowMeta: { fontSize: 11, color: colors.textTer, marginTop: 2 },
    add: { fontSize: 12, fontWeight: "800", color: colors.azure, letterSpacing: 0.6 },
  });
}
