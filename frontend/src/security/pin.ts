// 4-digit app PIN: storage, verification, lockout and recovery.
//
// Never stored in plain text. The PIN (and the one-time recovery code) are
// kept only as salted, iterated SHA-256 digests, in the device keychain
// (expo-secure-store) on iOS/Android. A 4-digit PIN has only 10,000 values,
// so the hash alone cannot stop an attacker who can read the keychain; what
// actually protects it is the keychain itself plus the attempt lockout below.
// On web there is no keychain — the digest lives in browser storage.

import { storage } from "@/src/utils/storage";
import { sha256Hex } from "./sha256";

export const PIN_LENGTH = 4;
const RECORD_KEY = "app_lock_pin_v1";
const ATTEMPTS_KEY = "app_lock_attempts_v1";
const SETUP_DISMISSED_KEY = "app_lock_setup_dismissed_v1";
const ITERATIONS = 2000;

/** Wrong attempts allowed before a cooldown starts. */
export const FREE_ATTEMPTS = 5;

const RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"; // no 0/O, 1/I

interface PinRecord {
  v: 1;
  salt: string;
  hash: string;
  iterations: number;
  recoverySalt: string;
  recoveryHash: string;
  createdAt: string;
}

interface Attempts {
  failed: number;
  lockUntil: number;
}

export type VerifyResult =
  | { ok: true }
  | { ok: false; reason: "wrong"; attemptsLeft: number }
  | { ok: false; reason: "locked"; lockedUntil: number };

function randomBytes(n: number): Uint8Array {
  const out = new Uint8Array(n);
  const c: any = (globalThis as any).crypto;
  if (c?.getRandomValues) {
    c.getRandomValues(out);
  } else {
    for (let i = 0; i < n; i++) out[i] = Math.floor(Math.random() * 256);
  }
  return out;
}

function randomHex(n: number): string {
  return Array.from(randomBytes(n), (b) => b.toString(16).padStart(2, "0")).join("");
}

function digest(secret: string, salt: string, iterations: number): string {
  let h = sha256Hex(`${salt}:${secret}`);
  for (let i = 1; i < iterations; i++) h = sha256Hex(h + salt);
  return h;
}

/** Constant-time string compare, so timing doesn't leak how much matched. */
function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export function isValidPin(pin: string): boolean {
  return new RegExp(`^\\d{${PIN_LENGTH}}$`).test(pin);
}

/** "AB3D-7KQ2". Shown once at setup; only its digest is kept. */
export function generateRecoveryCode(): string {
  const bytes = randomBytes(8);
  const chars = Array.from(bytes, (b) => RECOVERY_ALPHABET[b % RECOVERY_ALPHABET.length]);
  return `${chars.slice(0, 4).join("")}-${chars.slice(4).join("")}`;
}

export function normaliseRecoveryCode(code: string): string {
  return code.toUpperCase().replace(/[^A-Z0-9]/g, "");
}

async function readRecord(): Promise<PinRecord | null> {
  const raw = await storage.secureGet<string | null>(RECORD_KEY, null);
  if (!raw) return null;
  try {
    const r = JSON.parse(raw) as PinRecord;
    return r && r.v === 1 && r.hash && r.salt ? r : null;
  } catch {
    return null;
  }
}

async function readAttempts(): Promise<Attempts> {
  const raw = await storage.secureGet<string | null>(ATTEMPTS_KEY, null);
  if (!raw) return { failed: 0, lockUntil: 0 };
  try {
    const a = JSON.parse(raw) as Attempts;
    return { failed: a.failed || 0, lockUntil: a.lockUntil || 0 };
  } catch {
    return { failed: 0, lockUntil: 0 };
  }
}

async function writeAttempts(a: Attempts): Promise<void> {
  await storage.secureSet(ATTEMPTS_KEY, JSON.stringify(a));
}

/** 30 s after the 5th miss, doubling each further miss, capped at 15 min. */
export function cooldownMs(failed: number): number {
  if (failed < FREE_ATTEMPTS) return 0;
  return Math.min(30_000 * 2 ** (failed - FREE_ATTEMPTS), 15 * 60_000);
}

export async function hasPin(): Promise<boolean> {
  return (await readRecord()) !== null;
}

export async function lockedUntil(): Promise<number> {
  const a = await readAttempts();
  return a.lockUntil > Date.now() ? a.lockUntil : 0;
}

/** Saves a new PIN and returns a fresh recovery code to show the user once. */
export async function setPin(pin: string): Promise<string> {
  if (!isValidPin(pin)) throw new Error(`PIN must be exactly ${PIN_LENGTH} digits.`);
  const recovery = generateRecoveryCode();
  const salt = randomHex(16);
  const recoverySalt = randomHex(16);
  const record: PinRecord = {
    v: 1,
    salt,
    hash: digest(pin, salt, ITERATIONS),
    iterations: ITERATIONS,
    recoverySalt,
    recoveryHash: digest(normaliseRecoveryCode(recovery), recoverySalt, ITERATIONS),
    createdAt: new Date().toISOString(),
  };
  const ok = await storage.secureSet(RECORD_KEY, JSON.stringify(record));
  if (!ok) throw new Error("Couldn't save the PIN on this device.");
  await writeAttempts({ failed: 0, lockUntil: 0 });
  return recovery;
}

export async function verifyPin(pin: string): Promise<VerifyResult> {
  const record = await readRecord();
  if (!record) return { ok: true };
  const attempts = await readAttempts();
  if (attempts.lockUntil > Date.now()) return { ok: false, reason: "locked", lockedUntil: attempts.lockUntil };

  if (isValidPin(pin) && safeEqual(digest(pin, record.salt, record.iterations), record.hash)) {
    await writeAttempts({ failed: 0, lockUntil: 0 });
    return { ok: true };
  }
  const failed = attempts.failed + 1;
  const wait = cooldownMs(failed);
  const lockUntil = wait ? Date.now() + wait : 0;
  await writeAttempts({ failed, lockUntil });
  if (lockUntil) return { ok: false, reason: "locked", lockedUntil: lockUntil };
  return { ok: false, reason: "wrong", attemptsLeft: FREE_ATTEMPTS - failed };
}

/** Checks a recovery code. Counts against the same lockout as the PIN. */
export async function verifyRecoveryCode(code: string): Promise<VerifyResult> {
  const record = await readRecord();
  if (!record) return { ok: false, reason: "wrong", attemptsLeft: 0 };
  const attempts = await readAttempts();
  if (attempts.lockUntil > Date.now()) return { ok: false, reason: "locked", lockedUntil: attempts.lockUntil };
  const candidate = digest(normaliseRecoveryCode(code), record.recoverySalt, record.iterations);
  if (safeEqual(candidate, record.recoveryHash)) {
    await writeAttempts({ failed: 0, lockUntil: 0 });
    return { ok: true };
  }
  const failed = attempts.failed + 1;
  const wait = cooldownMs(failed);
  const lockUntil = wait ? Date.now() + wait : 0;
  await writeAttempts({ failed, lockUntil });
  if (lockUntil) return { ok: false, reason: "locked", lockedUntil: lockUntil };
  return { ok: false, reason: "wrong", attemptsLeft: FREE_ATTEMPTS - failed };
}

export async function clearPin(): Promise<void> {
  await storage.secureRemove(RECORD_KEY);
  await storage.secureRemove(ATTEMPTS_KEY);
}

export async function setupDismissed(): Promise<boolean> {
  return (await storage.getItem<boolean>(SETUP_DISMISSED_KEY, false)) === true;
}

export async function dismissSetup(): Promise<void> {
  await storage.setItem(SETUP_DISMISSED_KEY, true);
}
