/**
 * Where the API token lives in the browser.
 *
 * - "session" (default): sessionStorage - survives a reload, cleared when the
 *   browser tab/session ends.
 * - "memory": never written to any storage; lost on reload.
 *
 * Never localStorage, cookies, URLs or logs. Storage access is wrapped because it
 * can throw (private mode, blocked site data); failures fall back to memory only.
 */

export type TokenMode = "session" | "memory";

export const TOKEN_STORAGE_KEY = "applyforge.apiToken";

function storage(): Storage | null {
  try {
    return typeof window !== "undefined" ? window.sessionStorage : null;
  } catch {
    return null;
  }
}

export function readStoredToken(): string | null {
  try {
    const value = storage()?.getItem(TOKEN_STORAGE_KEY) ?? null;
    return value && value.trim() ? value : null;
  } catch {
    return null;
  }
}

/** Store the token for "session" mode. Returns false when storage is unavailable. */
export function storeToken(token: string): boolean {
  try {
    const store = storage();
    if (!store) return false;
    store.setItem(TOKEN_STORAGE_KEY, token);
    return true;
  } catch {
    return false;
  }
}

export function clearStoredToken(): void {
  try {
    storage()?.removeItem(TOKEN_STORAGE_KEY);
  } catch {
    // Nothing stored, or storage blocked: nothing to clear.
  }
}

/** Trim and sanity-check a pasted token. Returns null when unusable. */
export function normalizeToken(raw: string): string | null {
  const token = raw.trim();
  if (!token) return null;
  // A bearer token is a single header value: no whitespace or control characters.
  for (const char of token) {
    const code = char.charCodeAt(0);
    if (code <= 0x20 || code === 0x7f || /\s/.test(char)) return null;
  }
  return token;
}
