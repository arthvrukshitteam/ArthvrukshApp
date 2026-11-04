// Small ⓘ icon that opens a plain-English explanation in a centred popover.
// Tap the icon to open; tap outside to close. No external lib needed.

import { useMemo, useState } from "react";
import { Modal, Pressable, StyleSheet, Text, View } from "react-native";
import { Info } from "lucide-react-native";
import { glossaryEntry } from "@/src/glossary";
import { radii, space } from "@/src/theme";
import { useTheme } from "@/src/ThemeProvider";

interface Props {
  term: string;          // key in GLOSSARY (e.g. "es10", "blended_wm")
  size?: number;
  title?: string;        // optional title shown in the popover; defaults to term uppercased
}

export function InfoHint({ term, size = 12, title }: Props) {
  const { colors, type } = useTheme();
  const [open, setOpen] = useState(false);
  const explanation = useMemo(() => glossaryEntry(term) ?? "No explanation available.", [term]);
  const shownTitle = title ?? term.toUpperCase().replace(/_/g, " ");

  return (
    <>
      <Pressable hitSlop={8} onPress={() => setOpen(true)} style={{ marginLeft: 4 }}>
        <Info size={size} color={colors.textTer} />
      </Pressable>
      <Modal visible={open} transparent animationType="fade" onRequestClose={() => setOpen(false)}>
        <Pressable style={[styles.backdrop, { backgroundColor: colors.backdrop }]} onPress={() => setOpen(false)}>
          <Pressable
            onPress={() => {}}  // swallow tap so the inner card doesn't dismiss
            style={[
              styles.card,
              { backgroundColor: colors.bgCard, borderColor: colors.borderStrong },
            ]}
          >
            <Text style={[type.label, { color: colors.azure, marginBottom: 6 }]}>{shownTitle}</Text>
            <Text style={[type.body, { color: colors.textPri }]}>{explanation}</Text>
            <Pressable
              style={({ pressed }) => [styles.closeBtn, { borderColor: colors.border }, pressed && { opacity: 0.8 }]}
              onPress={() => setOpen(false)}
            >
              <Text style={[type.body, { color: colors.textSec, fontWeight: "700" }]}>GOT IT</Text>
            </Pressable>
          </Pressable>
        </Pressable>
      </Modal>
    </>
  );
}

const styles = StyleSheet.create({
  backdrop: { flex: 1, alignItems: "center", justifyContent: "center", paddingHorizontal: space.xl },
  card: {
    width: "100%", maxWidth: 380, padding: space.lg,
    borderRadius: radii.md, borderWidth: 1,
  },
  closeBtn: {
    marginTop: space.md, alignItems: "center",
    paddingVertical: 10, borderRadius: radii.sm, borderWidth: 1,
  },
});
