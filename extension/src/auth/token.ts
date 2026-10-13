/**
 * Local storage for the extension token issued by the dashboard (spec §7).
 *
 * The raw token stays in chrome.storage.local, in this extension's own origin. Rules:
 * - Only the service worker may ever read it back to send a Bearer header (M2). Content scripts
 *   run on instagram.com and must never see it.
 * - The popup writes and clears it but never displays it again.
 * - It is never logged, and never sent anywhere except the REF server.
 */

export const TOKEN_KEY = 'extensionToken';

/** 32 random bytes as unpadded URL-safe base64, exactly what the server issues. */
const TOKEN_PATTERN = /^[A-Za-z0-9_-]{43}$/;

export interface TokenStorage {
  get(keys: string[]): Promise<Record<string, unknown>>;
  set(items: Record<string, unknown>): Promise<void>;
  remove(keys: string[]): Promise<void>;
}

export function parseToken(input: string): string | null {
  const trimmed = input.trim();
  return TOKEN_PATTERN.test(trimmed) ? trimmed : null;
}

export async function saveToken(storage: TokenStorage, input: string): Promise<boolean> {
  const token = parseToken(input);
  if (token === null) return false;
  await storage.set({ [TOKEN_KEY]: token });
  return true;
}

export async function hasToken(storage: TokenStorage): Promise<boolean> {
  const stored = await storage.get([TOKEN_KEY]);
  const value = stored[TOKEN_KEY];
  return typeof value === 'string' && TOKEN_PATTERN.test(value);
}

export async function clearToken(storage: TokenStorage): Promise<void> {
  await storage.remove([TOKEN_KEY]);
}

/** For the service worker's upload code (M2). Not exported to the popup. */
export async function readToken(storage: TokenStorage): Promise<string | null> {
  const stored = await storage.get([TOKEN_KEY]);
  const value = stored[TOKEN_KEY];
  return typeof value === 'string' && TOKEN_PATTERN.test(value) ? value : null;
}
