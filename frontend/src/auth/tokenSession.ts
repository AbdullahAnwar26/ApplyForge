/**
 * The current API token, outside React so the API client can read it on every
 * request. React reads its state with useSyncExternalStore (see TokenProvider).
 */

import { clearStoredToken, storeToken, type TokenMode } from "./tokenStorage";

export type DisconnectReason = "user" | "unauthorized";

export interface TokenState {
  isConnected: boolean;
  mode: TokenMode | null;
  /** Why the last disconnect happened (shown on the Connect screen). */
  lastDisconnect: DisconnectReason | null;
}

export class TokenSession {
  #token: string | null;
  #state: TokenState;
  #listeners = new Set<() => void>();

  constructor(initialToken: string | null) {
    this.#token = initialToken;
    this.#state = { isConnected: initialToken !== null, mode: initialToken ? "session" : null, lastDisconnect: null };
  }

  get token(): string | null {
    return this.#token;
  }

  getSnapshot = (): TokenState => this.#state;

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  /** Keep `token` in sessionStorage ("session") or nowhere ("memory"). */
  connect(token: string, mode: TokenMode): void {
    let effectiveMode = mode;
    if (mode === "session") {
      if (!storeToken(token)) effectiveMode = "memory"; // storage blocked
    } else {
      clearStoredToken();
    }
    this.#token = token;
    this.#set({ isConnected: true, mode: effectiveMode, lastDisconnect: null });
  }

  disconnect(reason: DisconnectReason = "user"): void {
    this.#token = null;
    clearStoredToken();
    this.#set({ isConnected: false, mode: null, lastDisconnect: reason });
  }

  #set(state: TokenState): void {
    this.#state = state;
    for (const listener of this.#listeners) listener();
  }
}
