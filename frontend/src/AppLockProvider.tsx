/**
 * App lock — optional 4-digit PIN guarding the whole app.
 *
 * When a PIN is set the app opens locked, and locks again after it has been
 * in the background for RELOCK_AFTER_MS. Verification, lockout and recovery
 * live in src/security/pin.ts; this provider only holds the session state
 * (locked or not) and exposes the actions the lock screen and the App Lock
 * settings screen need.
 */

import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { AppState, type AppStateStatus } from "react-native";
import * as pin from "@/src/security/pin";

const RELOCK_AFTER_MS = 60_000;

interface AppLockValue {
  /** False until the stored PIN state has been read — render nothing sensitive before then. */
  ready: boolean;
  enabled: boolean;
  locked: boolean;
  /** First launch with no PIN and the prompt not yet dismissed. */
  setupPromptVisible: boolean;
  unlock: (code: string) => Promise<pin.VerifyResult>;
  verify: (code: string) => Promise<pin.VerifyResult>;
  verifyRecovery: (code: string) => Promise<pin.VerifyResult>;
  /** Sets or replaces the PIN; resolves to the new one-time recovery code. */
  savePin: (code: string) => Promise<string>;
  disable: () => Promise<void>;
  lockNow: () => void;
  /** Marks the session unlocked (after a successful recovery + new PIN). */
  markUnlocked: () => void;
  dismissSetupPrompt: () => Promise<void>;
}

const Ctx = createContext<AppLockValue | null>(null);

export function AppLockProvider({ children }: { children: React.ReactNode }) {
  const [ready, setReady] = useState(false);
  const [enabled, setEnabled] = useState(false);
  const [locked, setLocked] = useState(false);
  const [setupPromptVisible, setSetupPromptVisible] = useState(false);
  const backgroundedAt = useRef<number | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      const has = await pin.hasPin();
      const dismissed = has ? true : await pin.setupDismissed();
      if (!alive) return;
      setEnabled(has);
      setLocked(has);
      setSetupPromptVisible(!has && !dismissed);
      setReady(true);
    })();
    return () => { alive = false; };
  }, []);

  useEffect(() => {
    const onChange = (state: AppStateStatus) => {
      if (state === "background" || state === "inactive") {
        if (backgroundedAt.current == null) backgroundedAt.current = Date.now();
        return;
      }
      if (state === "active") {
        const since = backgroundedAt.current;
        backgroundedAt.current = null;
        if (enabled && since != null && Date.now() - since >= RELOCK_AFTER_MS) setLocked(true);
      }
    };
    const sub = AppState.addEventListener("change", onChange);
    return () => sub.remove();
  }, [enabled]);

  const unlock = useCallback(async (code: string) => {
    const r = await pin.verifyPin(code);
    if (r.ok) setLocked(false);
    return r;
  }, []);

  const verify = useCallback((code: string) => pin.verifyPin(code), []);
  const verifyRecovery = useCallback((code: string) => pin.verifyRecoveryCode(code), []);

  const savePin = useCallback(async (code: string) => {
    const recovery = await pin.setPin(code);
    setEnabled(true);
    setSetupPromptVisible(false);
    return recovery;
  }, []);

  const disable = useCallback(async () => {
    await pin.clearPin();
    await pin.dismissSetup();
    setEnabled(false);
    setLocked(false);
  }, []);

  const lockNow = useCallback(() => { if (enabled) setLocked(true); }, [enabled]);
  const markUnlocked = useCallback(() => setLocked(false), []);

  const dismissSetupPrompt = useCallback(async () => {
    setSetupPromptVisible(false);
    await pin.dismissSetup();
  }, []);

  const value = useMemo<AppLockValue>(() => ({
    ready, enabled, locked, setupPromptVisible,
    unlock, verify, verifyRecovery, savePin, disable, lockNow, markUnlocked, dismissSetupPrompt,
  }), [ready, enabled, locked, setupPromptVisible,
       unlock, verify, verifyRecovery, savePin, disable, lockNow, markUnlocked, dismissSetupPrompt]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAppLock(): AppLockValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAppLock must be used inside AppLockProvider");
  return v;
}
