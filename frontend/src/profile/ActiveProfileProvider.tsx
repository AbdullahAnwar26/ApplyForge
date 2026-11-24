/**
 * The active profile. The backend has no "active" flag, so the choice is made here.
 *
 * Rules:
 * - Profiles are loaded with the API client (GET profiles/, all of them).
 * - The chosen id is remembered in localStorage and ALWAYS checked against the
 *   latest profiles response before it is used.
 * - Exactly one profile and nothing chosen yet: it is selected (and remembered).
 * - A remembered id that no longer exists is never replaced silently, even when
 *   only one profile is left: the user must choose ("missing").
 * - Several profiles and nothing chosen: the user must choose ("unselected").
 * - No profiles, and loading errors, are explicit states.
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useSyncExternalStore, type ReactNode } from "react";

import type { Profile } from "../api/types";
import { useApi } from "../auth/AuthProvider";
import { createPreference } from "../lib/preferences";

export const ACTIVE_PROFILE_STORAGE_KEY = "applyforge.activeProfileId";
export const PROFILES_QUERY_KEY = ["profiles", "all"] as const;

export const activeProfilePreference = createPreference<number | null>(
  ACTIVE_PROFILE_STORAGE_KEY,
  null,
  (raw) => (/^[1-9]\d*$/.test(raw) ? Number(raw) : undefined),
  (value) => (value === null ? null : String(value)),
);

export type ActiveProfileState =
  | { status: "loading" }
  | { status: "error"; error: unknown }
  | { status: "empty" }
  | { status: "needs-selection"; reason: "unselected" | "missing"; profiles: Profile[] }
  | { status: "ready"; profile: Profile; profiles: Profile[] };

/** Pure decision from the profiles response and the remembered id. */
export function resolveActiveProfile(
  profiles: Profile[] | undefined,
  storedId: number | null,
  error: unknown,
): ActiveProfileState {
  // Once a list has loaded it stays in use, even if a later refresh fails.
  if (profiles === undefined) return error ? { status: "error", error } : { status: "loading" };
  if (profiles.length === 0) return { status: "empty" };
  if (storedId !== null) {
    const stored = profiles.find((p) => p.id === storedId);
    return stored
      ? { status: "ready", profile: stored, profiles }
      : { status: "needs-selection", reason: "missing", profiles };
  }
  if (profiles.length === 1) return { status: "ready", profile: profiles[0]!, profiles };
  return { status: "needs-selection", reason: "unselected", profiles };
}

interface ActiveProfileContextValue {
  state: ActiveProfileState;
  /** The active profile, or null unless state is "ready". */
  profile: Profile | null;
  profiles: Profile[];
  /** Select a profile from the latest list. Returns false for an unknown id. */
  select(id: number): boolean;
  refetch(): void;
  isRefreshing: boolean;
  /**
   * Put a created or updated profile into the profiles list at once (no reload),
   * then refresh from the backend. With `select`, also make it active - only
   * for an explicit user action such as "Create profile".
   */
  storeProfile(profile: Profile, options?: { select?: boolean }): void;
}

const ActiveProfileContext = createContext<ActiveProfileContextValue | null>(null);

export function ActiveProfileProvider({ children }: { children: ReactNode }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const storedId = useSyncExternalStore(activeProfilePreference.subscribe, activeProfilePreference.get);

  const query = useQuery({
    queryKey: PROFILES_QUERY_KEY,
    queryFn: async ({ signal }) => (await api.list<Profile>("profiles/", { signal })).items,
  });

  const state = useMemo(
    () => resolveActiveProfile(query.data, storedId, query.error),
    [query.data, storedId, query.error],
  );
  const autoSelectedId = state.status === "ready" && storedId === null ? state.profile.id : null;

  // Remember an automatic single-profile selection, so a later second profile
  // does not silently change which profile is active.
  useEffect(() => {
    if (autoSelectedId !== null) activeProfilePreference.set(autoSelectedId);
  }, [autoSelectedId]);

  const profiles = query.data;
  const select = useCallback(
    (id: number) => {
      if (!profiles?.some((p) => p.id === id)) return false;
      activeProfilePreference.set(id);
      return true;
    },
    [profiles],
  );

  const storeProfile = useCallback(
    (profile: Profile, options: { select?: boolean } = {}) => {
      queryClient.setQueryData<Profile[]>(PROFILES_QUERY_KEY, (current = []) => {
        const exists = current.some((p) => p.id === profile.id);
        const next = exists ? current.map((p) => (p.id === profile.id ? profile : p)) : [...current, profile];
        return next.sort((a, b) => a.id - b.id); // backend order
      });
      if (options.select) activeProfilePreference.set(profile.id);
      void queryClient.invalidateQueries({ queryKey: PROFILES_QUERY_KEY });
    },
    [queryClient],
  );

  const refetch = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: PROFILES_QUERY_KEY });
  }, [queryClient]);

  const value = useMemo<ActiveProfileContextValue>(
    () => ({
      state,
      profile: state.status === "ready" ? state.profile : null,
      profiles: profiles ?? [],
      select,
      refetch,
      isRefreshing: query.isFetching,
      storeProfile,
    }),
    [state, profiles, select, refetch, query.isFetching, storeProfile],
  );
  return <ActiveProfileContext.Provider value={value}>{children}</ActiveProfileContext.Provider>;
}

export function useActiveProfile(): ActiveProfileContextValue {
  const value = useContext(ActiveProfileContext);
  if (!value) throw new Error("useActiveProfile must be used inside <ActiveProfileProvider>.");
  return value;
}
