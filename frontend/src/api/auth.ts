/**
 * Account API (backend/tracker/api/account_views.py), all under /api/v1/auth/:
 *
 *   GET  me/        -> {user} or 401 when signed out
 *   POST signup/    {username, password, email?, remember?} -> 201 {user}; signed in
 *   POST login/     {username, password, remember?} -> {user}; 400 {detail} on a wrong username/password
 *   POST logout/    -> 204
 *   POST password/  {current_password, new_password} -> 204; this session stays signed in
 *
 * Sessions are HttpOnly cookies; the client adds the CSRF header to every write.
 */

import type { ApiClient } from "./client";

export interface User {
  id: number;
  username: string;
  email: string;
  date_joined: string;
}

export interface SignupInput {
  username: string;
  password: string;
  email?: string;
  remember?: boolean;
}

export interface LoginInput {
  username: string;
  password: string;
  remember?: boolean;
}

export async function getMe(api: ApiClient, signal?: AbortSignal): Promise<User> {
  return (await api.get<{ user: User }>("auth/me/", { signal })).user;
}

export async function signup(api: ApiClient, input: SignupInput): Promise<User> {
  return (await api.post<{ user: User }>("auth/signup/", input)).user;
}

export async function login(api: ApiClient, input: LoginInput): Promise<User> {
  return (await api.post<{ user: User }>("auth/login/", input)).user;
}

export function logout(api: ApiClient): Promise<void> {
  return api.post<void>("auth/logout/", {});
}

export function changePassword(api: ApiClient, currentPassword: string, newPassword: string): Promise<void> {
  return api.post<void>("auth/password/", { current_password: currentPassword, new_password: newPassword });
}
