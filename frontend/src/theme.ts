// Design tokens — sourced from /app/design_guidelines.json
// Two palettes (dark + light) selected at runtime via ThemeProvider.

export const radii = { sm: 8, md: 12, lg: 16, pill: 9999 };
export const space = { xs: 4, sm: 8, md: 16, lg: 24, xl: 32 };

// Tier accents — same on both schemes so a green stays green in either mode.
const tierAccents = {
  tierElite: "#10B981",
  tierStrong: "#3B82F6",
  tierGood: "#F59E0B",
  tierAvg: "#F97316",
  tierWeak: "#EF4444",
};

export const darkColors = {
  bg: "#07090E",
  bgCard: "#11141C",
  bgElev: "#1A1F2B",
  textPri: "#F8FAFC",
  textSec: "#94A3B8",
  textTer: "#64748B",
  textDis: "#334155",
  emerald: "#059669",
  amber: "#D97706",
  ruby: "#DC2626",
  azure: "#2563EB",
  azureSoft: "rgba(37,99,235,0.18)",
  border: "rgba(255,255,255,0.08)",
  borderStrong: "rgba(255,255,255,0.18)",
  rowAlt: "rgba(255,255,255,0.02)",
  backdrop: "rgba(0,0,0,0.6)",
  ...tierAccents,
};

export const lightColors: typeof darkColors = {
  bg: "#FFFFFF",
  bgCard: "#F8FAFC",
  bgElev: "#F1F5F9",
  textPri: "#0F172A",
  textSec: "#475569",
  textTer: "#64748B",
  textDis: "#94A3B8",
  emerald: "#047857",
  amber: "#B45309",
  ruby: "#B91C1C",
  azure: "#1D4ED8",
  azureSoft: "rgba(29,78,216,0.10)",
  border: "rgba(15,23,42,0.10)",
  borderStrong: "rgba(15,23,42,0.20)",
  rowAlt: "rgba(15,23,42,0.03)",
  backdrop: "rgba(15,23,42,0.45)",
  ...tierAccents,
};

export type ThemeColors = typeof darkColors;

export function makeType(colors: ThemeColors) {
  return {
    h1: { fontSize: 32, fontWeight: "700" as const, color: colors.textPri, letterSpacing: -1 },
    h2: { fontSize: 24, fontWeight: "600" as const, color: colors.textPri, letterSpacing: -0.5 },
    h3: { fontSize: 20, fontWeight: "600" as const, color: colors.textPri },
    body: { fontSize: 15, fontWeight: "400" as const, color: colors.textPri, lineHeight: 22 },
    bodySm: { fontSize: 13, fontWeight: "400" as const, color: colors.textSec, lineHeight: 18 },
    data: { fontSize: 18, fontWeight: "600" as const, color: colors.textPri },
    dataLarge: { fontSize: 64, fontWeight: "700" as const, color: colors.textPri, letterSpacing: -2 },
    label: {
      fontSize: 11,
      fontWeight: "600" as const,
      color: colors.textSec,
      letterSpacing: 1.4,
      textTransform: "uppercase" as const,
    },
  };
}

export type ThemeType = ReturnType<typeof makeType>;

export function tierColor(colors: ThemeColors, tier: string | null | undefined) {
  switch (tier) {
    case "Elite": return colors.tierElite;
    case "Strong": return colors.tierStrong;
    case "Good": return colors.tierGood;
    case "Average": return colors.tierAvg;
    default: return colors.tierWeak;
  }
}

export function scoreColor(colors: ThemeColors, score: number | null | undefined) {
  if (score == null) return colors.textTer;
  if (score >= 90) return colors.tierElite;
  if (score >= 80) return colors.tierStrong;
  if (score >= 70) return colors.tierGood;
  if (score >= 60) return colors.tierAvg;
  return colors.tierWeak;
}
