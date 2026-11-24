/**
 * The signed-in account and the API client.
 *
 * - On start it asks the backend who is signed in (GET auth/me/): the session cookie is
 *   HttpOnly, so the browser keeps it and only the backend can read it.
 * - signIn / signUp / signOut call the account endpoints and update the state.
 * - Any 401 (the session expired, the password was changed elsewhere, the account was
 *   removed) returns to the sign-in page. Leaving the signed-in state drops all cached API
 *   data, so one account's data is never shown to the next person who signs in.
 */

import { useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useMemo, useState, useSyncExternalStore, type ReactNode } from "react";

import { getMe, login, logout, signup, type LoginInput, type SignupInput, type User } from "../api/auth";
import { createApiClient, type ApiClient } from "../api/client";
import { isApiError } from "../api/errors";
import { AuthSession, type AuthState } from "./authSession";

export type { AuthStatus, SignOutReason } from "./authSession";

interface AuthContextValue extends AuthState {
  api: ApiClient;
  signIn(input: LoginInput): Promise<User>;
  signUp(input: SignupInput): Promise<User>;
  signOut(): Promise<void>;
  /** Ask the backend again (e.g. after "server unreachable"). */
  retry(): void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export const API_BASE_URL: string | undefined = import.meta.env.VITE_API_BASE_URL;

export function AuthProvider({ children, baseUrl = API_BASE_URL }: { children: ReactNode; baseUrl?: string }) {
  const queryClient = useQueryClient();

  // Created once. Whenever the account changes or the session ends, cached data is dropped.
  const [session] = useState(() => {
    const created = new AuthSession();
    let lastUser: number | null = null;
    created.subscribe(() => {
      const { status, user } = created.getSnapshot();
      const current = status === "signedIn" ? (user?.id ?? null) : null;
      if (current !== lastUser) {
        void queryClient.cancelQueries();
        queryClient.clear();
      }
      lastUser = current;
    });
    return created;
  });
  const [api] = useState(() => createApiClient({ baseUrl, onUnauthorized: () => session.expired() }));
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    getMe(api, controller.signal)
      .then((me) => session.signedIn(me))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (isApiError(error) && error.kind === "network") session.unreachable();
        else session.signedOut(null);
      });
    return () => controller.abort();
  }, [api, session, attempt]);

  const state = useSyncExternalStore(session.subscribe, session.getSnapshot);
  const value = useMemo<AuthContextValue>(
    () => ({
      ...state,
      api,
      async signIn(input) {
        const me = await login(api, input);
        session.signedIn(me);
        return me;
      },
      async signUp(input) {
        const me = await signup(api, input);
        session.signedIn(me);
        return me;
      },
      async signOut() {
        try {
          await logout(api);
        } finally {
          session.signedOut("user");
        }
      },
      retry() {
        session.loading();
        setAttempt((n) => n + 1);
      },
    }),
    [state, api, session],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside <AuthProvider>.");
  return value;
}

/** The API client (requests carry the session cookie). */
export function useApi(): ApiClient {
  return useAuth().api;
}
