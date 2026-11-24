/**
 * Fake accounts for tests, following backend/tracker/api/account_views.py:
 * session state (who is signed in), the auth/ endpoints, and a gate in front of every
 * other API route: 401 when signed out, 403 for a write without the CSRF header.
 */

import { http, HttpResponse, type HttpHandler } from "msw";

import type { User } from "../api/auth";
import { API } from "./server";

export const TEST_USER: User = { id: 1, username: "riya", email: "riya@example.com", date_joined: "2026-01-01T00:00:00Z" };
export const TEST_PASSWORD = "Fictional-Passw0rd-1";
export const TEST_CSRF = "test-csrf-token";

interface Account {
  user: User;
  password: string;
}

export const mockSession: { user: User | null; remember: boolean | null; accounts: Account[] } = {
  user: null,
  remember: null,
  accounts: [],
};

/** Reset to one known account, signed in or not, with the CSRF cookie set. */
export function resetMockSession(signedIn: boolean) {
  mockSession.accounts = [{ user: { ...TEST_USER }, password: TEST_PASSWORD }];
  mockSession.user = signedIn ? { ...TEST_USER } : null;
  mockSession.remember = null;
  document.cookie = `csrftoken=${TEST_CSRF}; path=/`;
}

export function clearMockSession() {
  mockSession.user = null;
  mockSession.accounts = [];
  document.cookie = "csrftoken=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
}

const SAFE = new Set(["GET", "HEAD", "OPTIONS"]);

/** Django's CSRF answer for a write without the right X-CSRFToken header, else null. */
export function csrfProblem(request: Request) {
  if (SAFE.has(request.method) || request.headers.get("x-csrftoken") === TEST_CSRF) return null;
  return HttpResponse.json({ detail: "CSRF Failed: CSRF token missing." }, { status: 403 });
}

export function signedOutResponse() {
  return HttpResponse.json(
    { detail: "Authentication credentials were not provided." },
    { status: 401, headers: { "WWW-Authenticate": 'Session realm="api"' } },
  );
}

/** In front of every non-auth API route: falls through (returns undefined) when allowed. */
export function authGate(): HttpHandler {
  return http.all(`${API}/*`, ({ request }) => {
    if (new URL(request.url).pathname.startsWith("/api/v1/auth/")) return undefined;
    if (!mockSession.user) return signedOutResponse();
    return csrfProblem(request) ?? undefined;
  });
}

const bad = (body: Record<string, unknown>, status = 400) => HttpResponse.json(body, { status });

export function authHandlers(): HttpHandler[] {
  return [
    http.get(`${API}/auth/csrf/`, () => {
      document.cookie = `csrftoken=${TEST_CSRF}; path=/`;
      return HttpResponse.json({ detail: "CSRF cookie set." });
    }),
    http.get(`${API}/auth/me/`, () => (mockSession.user ? HttpResponse.json({ user: mockSession.user }) : signedOutResponse())),
    http.post(`${API}/auth/login/`, async ({ request }) => {
      const problem = csrfProblem(request);
      if (problem) return problem;
      const body = (await request.json()) as { username?: string; password?: string; remember?: boolean };
      const account = mockSession.accounts.find(
        (a) => a.user.username.toLowerCase() === (body.username ?? "").trim().toLowerCase() && a.password === body.password,
      );
      if (!account) return bad({ detail: "Incorrect username or password." });
      mockSession.user = account.user;
      mockSession.remember = body.remember ?? true;
      return HttpResponse.json({ user: account.user });
    }),
    http.post(`${API}/auth/signup/`, async ({ request }) => {
      const problem = csrfProblem(request);
      if (problem) return problem;
      const body = (await request.json()) as { username?: string; password?: string; email?: string };
      const username = (body.username ?? "").trim();
      if (mockSession.accounts.some((a) => a.user.username.toLowerCase() === username.toLowerCase())) {
        return bad({ username: ["This username is already taken."] });
      }
      if ((body.password ?? "").toLowerCase().includes("password")) {
        return bad({ password: ["This password is too common."] });
      }
      const user: User = { id: mockSession.accounts.length + 1, username, email: body.email ?? "", date_joined: "2026-03-03T09:00:00Z" };
      mockSession.accounts.push({ user, password: body.password ?? "" });
      mockSession.user = user;
      return HttpResponse.json({ user }, { status: 201 });
    }),
    http.post(`${API}/auth/logout/`, () => {
      mockSession.user = null;
      return new HttpResponse(null, { status: 204 });
    }),
    http.post(`${API}/auth/password/`, async ({ request }) => {
      if (!mockSession.user) return signedOutResponse();
      const problem = csrfProblem(request);
      if (problem) return problem;
      const body = (await request.json()) as { current_password?: string; new_password?: string };
      const account = mockSession.accounts.find((a) => a.user.id === mockSession.user!.id)!;
      if (body.current_password !== account.password) return bad({ current_password: ["The current password is not correct."] });
      if ((body.new_password ?? "").length < 8) return bad({ new_password: ["This password is too short."] });
      account.password = body.new_password!;
      return new HttpResponse(null, { status: 204 });
    }),
  ];
}
