/**
 * Holds the API token session and the API client built on it.
 *
 * - The token starts from sessionStorage (if a previous "session" connect stored it).
 * - connect(token, mode) stores it in sessionStorage ("session") or nowhere ("memory").
 * - disconnect() and any 401 response clear the token from memory and storage and
 *   drop all cached API data, which returns the app to the Connect screen.
 */

import { useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useMemo, useState, useSyncExternalStore, type ReactNode } from "react";

import { createApiClient, type ApiClient } from "../api/client";
import { TokenSession, type DisconnectReason, type TokenState } from "./tokenSession";
import { readStoredToken, type TokenMode } from "./tokenStorage";

export type { DisconnectReason } from "./tokenSession";

interface TokenContextValue extends TokenState {
  connect(token: string, mode: TokenMode): void;
  disconnect(reason?: DisconnectReason): void;
  api: ApiClient;
}

const TokenContext = createContext<TokenContextValue | null>(null);

export const API_BASE_URL: string | undefined = import.meta.env.VITE_API_BASE_URL;

export function TokenProvider({ children, baseUrl = API_BASE_URL }: { children: ReactNode; baseUrl?: string }) {
  const queryClient = useQueryClient();

  // Created once per provider. Cached API data never outlives the token.
  const [session] = useState(() => {
    const created = new TokenSession(readStoredToken());
    created.subscribe(() => {
      if (!created.getSnapshot().isConnected) {
        void queryClient.cancelQueries();
        queryClient.clear();
      }
    });
    return created;
  });
  const [api] = useState(() =>
    createApiClient({
      baseUrl,
      getToken: () => session.token,
      onUnauthorized: () => {
        if (session.token !== null) session.disconnect("unauthorized");
      },
    }),
  );

  const state = useSyncExternalStore(session.subscribe, session.getSnapshot);
  const value = useMemo<TokenContextValue>(
    () => ({
      ...state,
      connect: (token, mode) => session.connect(token, mode),
      disconnect: (reason) => session.disconnect(reason),
      api,
    }),
    [state, session, api],
  );
  return <TokenContext.Provider value={value}>{children}</TokenContext.Provider>;
}

export function useToken(): TokenContextValue {
  const value = useContext(TokenContext);
  if (!value) throw new Error("useToken must be used inside <TokenProvider>.");
  return value;
}

/** The authenticated API client. */
export function useApi(): ApiClient {
  return useToken().api;
}
