// Theme provider: resolves user preference (light / dark / system) against the
// device color scheme, persists the choice to AsyncStorage, and exposes
// reactive colors + typography to every consumer via useTheme().

import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { useColorScheme } from "react-native";
import { storage } from "@/src/utils/storage";
import {
  darkColors,
  lightColors,
  makeType,
  scoreColor as scoreColorFn,
  tierColor as tierColorFn,
  type ThemeColors,
  type ThemeType,
} from "@/src/theme";

export type ThemeMode = "light" | "dark" | "system";
export type ResolvedScheme = "light" | "dark";

interface ThemeCtx {
  mode: ThemeMode;                 // user preference
  scheme: ResolvedScheme;          // actually rendered scheme
  setMode: (m: ThemeMode) => void;
  colors: ThemeColors;
  type: ThemeType;
  tierColor: (tier: string | null | undefined) => string;
  scoreColor: (score: number | null | undefined) => string;
}

const STORAGE_KEY = "theme_mode";
const ThemeContext = createContext<ThemeCtx | null>(null);

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const systemScheme = useColorScheme();   // 'light' | 'dark' | null
  const [mode, setModeState] = useState<ThemeMode>("system");

  // Restore saved preference once on mount.
  useEffect(() => {
    let cancelled = false;
    storage.getItem<string>(STORAGE_KEY, "system").then((v) => {
      if (cancelled) return;
      if (v === "light" || v === "dark" || v === "system") setModeState(v);
    });
    return () => { cancelled = true; };
  }, []);

  const scheme: ResolvedScheme =
    mode === "system" ? (systemScheme === "light" ? "light" : "dark") : mode;

  const value = useMemo<ThemeCtx>(() => {
    const colors = scheme === "light" ? lightColors : darkColors;
    return {
      mode,
      scheme,
      setMode: (m: ThemeMode) => {
        setModeState(m);
        void storage.setItem(STORAGE_KEY, m);
      },
      colors,
      type: makeType(colors),
      tierColor: (t) => tierColorFn(colors, t),
      scoreColor: (s) => scoreColorFn(colors, s),
    };
  }, [scheme, mode]);

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeCtx {
  const ctx = useContext(ThemeContext);
  if (!ctx) {
    throw new Error("useTheme() must be called inside <ThemeProvider>");
  }
  return ctx;
}
