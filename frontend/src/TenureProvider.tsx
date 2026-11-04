/**
 * TenureProvider — persists the user's chosen investment tenure across sessions.
 *
 * Used by both SIP (any positive year count, capped at 15Y for backend calculations)
 * and Lumpsum (free entry, mapped to 3/5/7Y internally).
 * A null tenure means the user hasn't entered one yet this session.
 */

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react";
import AsyncStorage from "@react-native-async-storage/async-storage";

const STORAGE_KEY = "@arthvruksh:tenure"
interface TenureContextValue {
  tenure: number | null;
  loading: boolean;
  setTenure: (t: number) => Promise<void>;
  clearTenure: () => Promise<void>;
}

const TenureContext = createContext<TenureContextValue>({
  tenure: null,
  loading: true,
  setTenure: async () => {},
  clearTenure: async () => {},
});

export function TenureProvider({ children }: { children: React.ReactNode }) {
  const [tenure, setTenureState] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    AsyncStorage.getItem(STORAGE_KEY)
      .then((v) => {
        const n = v ? parseInt(v, 10) : NaN;
        if (!isNaN(n) && n >= 1) setTenureState(n);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const setTenure = useCallback(async (t: number) => {
    setTenureState(t);
    try {
      await AsyncStorage.setItem(STORAGE_KEY, String(t));
    } catch {
      // non-fatal
    }
  }, []);

  const clearTenure = useCallback(async () => {
    setTenureState(null);
    try {
      await AsyncStorage.removeItem(STORAGE_KEY);
    } catch {}
  }, []);

  return (
    <TenureContext.Provider value={{ tenure, loading, setTenure, clearTenure }}>
      {children}
    </TenureContext.Provider>
  );
}

export function useTenure() {
  return useContext(TenureContext);
}
