/**
 * The single HTTP client for the ApplyForge API. Every request goes through here.
 *
 * - Signed-in state is the Django session cookie (HttpOnly; JavaScript never sees it),
 *   sent with every same-origin request (`credentials: "same-origin"`).
 * - Writes (POST/PUT/PATCH/DELETE) carry the CSRF token from the `csrftoken` cookie in
 *   `X-CSRFToken`. If that cookie is missing it is fetched once from `auth/csrf/`.
 * - Only relative API paths are accepted, so requests only go to the configured API
 *   base (same-origin `/api/v1` by default).
 * - Error responses become ApiError (see errors.ts). A 401 calls `onUnauthorized`
 *   so the app can show the sign-in page.
 * - List endpoints are normalised from both pagination formats (see pagination.ts).
 */

import { ApiError, parseErrorBody } from "./errors";
import { normalizeList, pageQuery, type ListResult, type PageParams } from "./pagination";

export type QueryValue = string | number | boolean | null | undefined | ReadonlyArray<string | number>;
export type Query = Record<string, QueryValue>;

export interface RequestOptions {
  query?: Query;
  signal?: AbortSignal;
}

export interface ApiClientConfig {
  /** API base: a same-origin path ("/api/v1") or an absolute http(s) URL. */
  baseUrl?: string;
  /** Called after any 401 response. */
  onUnauthorized?: () => void;
  fetchImpl?: typeof fetch;
  /** Reads the CSRF cookie (default: `csrftoken` from document.cookie). */
  readCsrfToken?: () => string | null;
}

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

/** The `csrftoken` cookie set by Django, or null. */
export function readCsrfCookie(cookieString: string = typeof document === "undefined" ? "" : document.cookie): string | null {
  for (const part of cookieString.split(";")) {
    const [name, ...rest] = part.trim().split("=");
    if (name === "csrftoken") return decodeURIComponent(rest.join("=")) || null;
  }
  return null;
}

export interface ApiClient {
  /** Absolute or root-relative URL of an API path, for plain links (e.g. file downloads). */
  url(path: string): string;
  get<T>(path: string, options?: RequestOptions): Promise<T>;
  list<T>(path: string, options?: RequestOptions & { page?: PageParams }): Promise<ListResult<T>>;
  post<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T>;
  patch<T>(path: string, body: unknown, options?: RequestOptions): Promise<T>;
  put<T>(path: string, body: unknown, options?: RequestOptions): Promise<T>;
  delete<T = void>(path: string, options?: RequestOptions): Promise<T>;
  /** Multipart request (resume uploads). The browser sets the multipart boundary. */
  upload<T>(method: "POST" | "PATCH" | "PUT", path: string, form: FormData, options?: RequestOptions): Promise<T>;
  /**
   * Multipart request with upload progress (XMLHttpRequest; fetch cannot report
   * upload progress). Same cookies, CSRF, error and 401 handling as every other request.
   */
  uploadWithProgress<T>(
    method: "POST" | "PATCH" | "PUT",
    path: string,
    form: FormData,
    options?: RequestOptions & { onProgress?: (fraction: number) => void },
  ): Promise<T>;
}

function abortError(): Error {
  const error = new Error("The upload was cancelled.");
  error.name = "AbortError";
  return error;
}

function parseXhrBody(xhr: XMLHttpRequest): unknown {
  const text = xhr.responseText;
  if (!text) return undefined;
  if ((xhr.getResponseHeader("content-type") ?? "").includes("json")) {
    try {
      return JSON.parse(text);
    } catch {
      return text;
    }
  }
  return text;
}

export const DEFAULT_API_BASE_URL = "/api/v1";

export function resolveBaseUrl(raw: string | undefined): string {
  const value = (raw ?? "").trim() || DEFAULT_API_BASE_URL;
  if (value.startsWith("/") && !value.startsWith("//")) return value.replace(/\/+$/, "");
  if (/^https?:\/\/[^/]/i.test(value)) return value.replace(/\/+$/, "");
  throw new Error("VITE_API_BASE_URL must be a path starting with '/' or an http(s) URL.");
}

function assertRelativePath(path: string): void {
  if (!path || /^[a-z][a-z0-9+.-]*:/i.test(path) || path.startsWith("//") || path.includes("..")) {
    throw new Error("API paths must be relative to the API base.");
  }
}

export function buildQueryString(query: Query | undefined): string {
  if (!query) return "";
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) {
      if (value.length) params.set(key, value.join(","));
    } else {
      params.set(key, String(value));
    }
  }
  const text = params.toString();
  return text ? `?${text}` : "";
}

export function buildUrl(baseUrl: string, path: string, query?: Query): string {
  assertRelativePath(path);
  const cleanPath = path.replace(/^\/+/, "");
  return `${baseUrl}/${cleanPath}${buildQueryString(query)}`;
}

async function readBody(response: Response): Promise<unknown> {
  if (response.status === 204 || response.status === 205) return undefined;
  const text = await response.text();
  if (!text) return undefined;
  const type = response.headers.get("content-type") ?? "";
  if (type.includes("json")) {
    try {
      return JSON.parse(text);
    } catch {
      return text;
    }
  }
  return text;
}

function isAbort(error: unknown, signal: AbortSignal | undefined): boolean {
  // Checked by name: DOMException classes differ between environments.
  return Boolean(signal?.aborted) || (error instanceof Error && error.name === "AbortError");
}

export function createApiClient(config: ApiClientConfig): ApiClient {
  const baseUrl = resolveBaseUrl(config.baseUrl);
  const doFetch = config.fetchImpl ?? ((input: RequestInfo | URL, init?: RequestInit) => fetch(input, init));
  const readCsrf = config.readCsrfToken ?? (() => readCsrfCookie());

  /** The CSRF token for a write; fetches the cookie once when it is missing. */
  async function csrfFor(method: string): Promise<string | null> {
    if (SAFE_METHODS.has(method)) return null;
    let token = readCsrf();
    if (!token) {
      try {
        await doFetch(buildUrl(baseUrl, "auth/csrf/"), { credentials: "same-origin", cache: "no-store" });
      } catch {
        throw new ApiError({ status: 0 });
      }
      token = readCsrf();
    }
    return token;
  }

  async function request<T>(
    method: string,
    path: string,
    options: RequestOptions & { json?: unknown; form?: FormData } = {},
  ): Promise<T> {
    const url = buildUrl(baseUrl, path, options.query);
    const headers = new Headers({ Accept: "application/json" });
    const csrf = await csrfFor(method);
    if (csrf) headers.set("X-CSRFToken", csrf);
    let body: BodyInit | undefined;
    if (options.form) {
      body = options.form;
    } else if (options.json !== undefined) {
      headers.set("Content-Type", "application/json");
      body = JSON.stringify(options.json);
    }

    let response: Response;
    try {
      response = await doFetch(url, {
        method,
        headers,
        body,
        signal: options.signal,
        credentials: "same-origin",
        cache: "no-store",
        referrerPolicy: "no-referrer",
      });
    } catch (error) {
      if (isAbort(error, options.signal)) throw error;
      // The original error is not attached: it may echo request details.
      throw new ApiError({ status: 0 });
    }

    const data = await readBody(response);
    if (!response.ok) {
      const error = parseErrorBody(response.status, data, response.headers.get("retry-after"));
      if (response.status === 401) config.onUnauthorized?.();
      throw error;
    }
    return data as T;
  }

  function uploadWithProgress<T>(
    method: string,
    path: string,
    form: FormData,
    options: RequestOptions & { onProgress?: (fraction: number) => void } = {},
  ): Promise<T> {
    const url = buildUrl(baseUrl, path, options.query);
    if (options.signal?.aborted) return Promise.reject(abortError());

    return csrfFor(method).then((csrf) => new Promise<T>((resolve, reject) => {
      // Cancelled while the CSRF cookie was being fetched: never start the upload.
      if (options.signal?.aborted) {
        reject(abortError());
        return;
      }
      const xhr = new XMLHttpRequest();
      xhr.open(method, url);
      xhr.setRequestHeader("Accept", "application/json");
      if (csrf) xhr.setRequestHeader("X-CSRFToken", csrf);

      const onAbortSignal = () => xhr.abort();
      options.signal?.addEventListener("abort", onAbortSignal, { once: true });
      const cleanup = () => options.signal?.removeEventListener("abort", onAbortSignal);

      if (options.onProgress && xhr.upload) {
        xhr.upload.addEventListener("progress", (event) => {
          if (event.lengthComputable && event.total > 0) options.onProgress!(Math.min(1, event.loaded / event.total));
        });
      }
      xhr.addEventListener("load", () => {
        cleanup();
        const data = parseXhrBody(xhr);
        if (xhr.status >= 200 && xhr.status < 300) {
          options.onProgress?.(1);
          resolve(data as T);
          return;
        }
        const error = parseErrorBody(xhr.status, data, xhr.getResponseHeader("retry-after"));
        if (xhr.status === 401) config.onUnauthorized?.();
        reject(error);
      });
      xhr.addEventListener("error", () => {
        cleanup();
        reject(new ApiError({ status: 0 }));
      });
      xhr.addEventListener("abort", () => {
        cleanup();
        reject(abortError());
      });
      xhr.send(form);
    }));
  }

  return {
    url: (path) => {
      assertRelativePath(path);
      return buildUrl(baseUrl, path);
    },
    get: (path, options) => request("GET", path, options),
    async list<T>(path: string, options: RequestOptions & { page?: PageParams } = {}) {
      const query = { ...options.query, ...pageQuery(options.page) };
      const body = await request<unknown>("GET", path, { ...options, query });
      return normalizeList<T>(body);
    },
    post: (path, body, options) => request("POST", path, { ...options, json: body ?? {} }),
    patch: (path, body, options) => request("PATCH", path, { ...options, json: body }),
    put: (path, body, options) => request("PUT", path, { ...options, json: body }),
    delete: (path, options) => request("DELETE", path, options),
    upload: (method, path, form, options) => request(method, path, { ...options, form }),
    uploadWithProgress,
  };
}
