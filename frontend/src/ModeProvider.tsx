/**
 * ModeProvider — SIP vs Lumpsum investment mode.
 *
 * The mode is persisted to AsyncStorage so it survives app restarts.
 * A null mode means the user hasn't chosen yet (first launch).
 *
 * Usage:
 *   const { mode, setMode } = useMode();
 */

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react";
import AsyncStorage from "@react-native-async-storage/async-storage";

export type InvestmentMode = "sip" | "lumpsum";

interface ModeContextValue {
  /** null = not yet chosen (first launch / loading) */
  mode: InvestmentMode | null;
  loading: boolean;
  setMode: (m: InvestmentMode) => Promise<void>;
}

const STORAGE_KEY = "@arthvruksh:investment_mode";

const ModeContext = createContext<ModeContextValue>({
  mode: null,
  loading: true,
  setMode: async () => {},
});

export function ModeProvider({ children }: { children: React.ReactNode }) {
  const [mode, setModeState] = useState<InvestmentMode | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    AsyncStorage.getItem(STORAGE_KEY)
      .then((v) => {
        if (v === "sip" || v === "lumpsum") setModeState(v);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const setMode = useCallback(async (m: InvestmentMode) => {
    setModeState(m);
    try {
      await AsyncStorage.setItem(STORAGE_KEY, m);
    } catch {
      // non-fatal — the in-memory value is still set
    }
  }, []);

  return (
    <ModeContext.Provider value={{ mode, loading, setMode }}>
      {children}
    </ModeContext.Provider>
  );
}

export function useMode() {
  return useContext(ModeContext);
}
