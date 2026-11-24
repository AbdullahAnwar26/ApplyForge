/**
 * Who is signed in, as a small store outside React (read with useSyncExternalStore).
 * The API client's 401 callback can update it at any time without touching React state.
 */

import type { User } from "../api/auth";

export type AuthStatus = "loading" | "signedOut" | "signedIn" | "unreachable";
/** Why the sign-in page is shown (shown there). */
export type SignOutReason = "user" | "expired" | null;

export interface AuthState {
  status: AuthStatus;
  user: User | null;
  signOutReason: SignOutReason;
}

export class AuthSession {
  private state: AuthState = { status: "loading", user: null, signOutReason: null };
  private listeners = new Set<() => void>();

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = () => this.state;

  private set(next: AuthState) {
    this.state = next;
    this.listeners.forEach((listener) => listener());
  }

  loading() {
    this.set({ ...this.state, status: "loading" });
  }

  signedIn(user: User) {
    this.set({ status: "signedIn", user, signOutReason: null });
  }

  signedOut(reason: SignOutReason) {
    this.set({ status: "signedOut", user: null, signOutReason: reason });
  }

  unreachable() {
    this.set({ status: "unreachable", user: null, signOutReason: null });
  }

  /** A 401 while signed in: the session ended (expired, password changed elsewhere...). */
  expired() {
    if (this.state.status === "signedIn") this.signedOut("expired");
  }
}
