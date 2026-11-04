// Tracks the user's preferred density on the fund detail screen.
// "simple"   — gauge + plain-English summary + insights + histogram only
// "analyst"  — adds the full distribution metrics, sub-scores, and rolling windows table

import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { storage } from "@/src/utils/storage";

export type ViewMode = "simple" | "analyst";

interface ViewModeCtx {
  mode: ViewMode;
  setMode: (m: ViewMode) => void;
}

const STORAGE_KEY = "view_mode";
const ViewModeContext = createContext<ViewModeCtx | null>(null);

export function ViewModeProvider({ children }: { children: React.ReactNode }) {
  const [mode, setModeState] = useState<ViewMode>("simple");

  useEffect(() => {
    let cancelled = false;
    storage.getItem<string>(STORAGE_KEY, "simple").then((v) => {
      if (cancelled) return;
      if (v === "simple" || v === "analyst") setModeState(v);
    });
    return () => { cancelled = true; };
  }, []);

  const value = useMemo<ViewModeCtx>(
    () => ({
      mode,
      setMode: (m: ViewMode) => {
        setModeState(m);
        void storage.setItem(STORAGE_KEY, m);
      },
    }),
    [mode],
  );

  return <ViewModeContext.Provider value={value}>{children}</ViewModeContext.Provider>;
}

export function useViewMode(): ViewModeCtx {
  const ctx = useContext(ViewModeContext);
  if (!ctx) throw new Error("useViewMode() must be called inside <ViewModeProvider>");
  return ctx;
}
