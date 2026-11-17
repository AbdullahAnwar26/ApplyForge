/**
 * Small per-browser preferences in localStorage (theme, active profile id),
 * readable from React through useSyncExternalStore.
 *
 * Never used for the API token (see auth/tokenStorage.ts). Every access is
 * wrapped: storage can be unavailable (private mode, blocked site data), in
 * which case the value lives in memory for this page only.
 */

type Listener = () => void;

export interface Preference<T> {
  get(): T;
  set(value: T): void;
  subscribe(listener: Listener): () => void;
}

export function createPreference<T>(
  key: string,
  fallback: T,
  parse: (raw: string) => T | undefined,
  serialize: (value: T) => string | null = (value) => String(value),
): Preference<T> {
  const listeners = new Set<Listener>();
  let memory: T | undefined;
  let cachedRaw: string | null | undefined;
  let cachedValue: T = fallback;

  function read(): T {
    let raw: string | null;
    try {
      raw = window.localStorage.getItem(key);
    } catch {
      return memory ?? fallback;
    }
    if (raw === cachedRaw) return cachedValue; // stable snapshot for React
    cachedRaw = raw;
    cachedValue = raw === null ? fallback : (parse(raw) ?? fallback);
    return cachedValue;
  }

  function notify() {
    for (const listener of listeners) listener();
  }

  function onStorage(event: StorageEvent) {
    if (event.key === key || event.key === null) notify(); // another tab
  }

  return {
    get: read,
    set(value: T) {
      memory = value;
      const raw = serialize(value);
      try {
        if (raw === null) window.localStorage.removeItem(key);
        else window.localStorage.setItem(key, raw);
      } catch {
        cachedRaw = undefined; // storage blocked: memory value only
      }
      notify();
    },
    subscribe(listener: Listener) {
      if (listeners.size === 0) window.addEventListener("storage", onStorage);
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
        if (listeners.size === 0) window.removeEventListener("storage", onStorage);
      };
    },
  };
}
