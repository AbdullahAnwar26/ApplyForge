/**
 * Typed API errors.
 *
 * The backend (Django REST Framework) answers errors as:
 *   - field errors        {"name": ["This field is required."], "non_field_errors": [...]}
 *   - a general message   {"detail": "Not found."}
 *   - extras on some      {"detail": "...", "application": 12} / {"run": 7}
 *   - throttling          429 with a Retry-After header
 *   - result bodies       e.g. collector 502 {run: {...}, outcomes: [...], detail}
 *
 * Field errors are read from 400/422 responses only (DRF validation). For other
 * statuses only `detail` and scalar extras are read; the decoded body is kept
 * as `body` for callers that expect a structured result (collector 502).
 *
 * Messages are built from the response body only. Request headers (and so the
 * API token) are never part of an ApiError.
 */

export type FieldErrors = Record<string, string[]>;

export type ApiErrorKind =
  | "network" // backend unreachable
  | "unauthorized" // 401
  | "forbidden" // 403
  | "not_found" // 404
  | "conflict" // 409
  | "validation" // 400 / 422
  | "throttled" // 429
  | "server" // 5xx
  | "unknown";

export type ErrorExtra = Record<string, string | number | boolean | null>;

export class ApiError extends Error {
  /** HTTP status; 0 when the backend could not be reached. */
  readonly status: number;
  readonly kind: ApiErrorKind;
  readonly detail: string | null;
  readonly fieldErrors: FieldErrors;
  /** Scalar values returned next to the error, e.g. {application: 12} or {run: 7}. */
  readonly extra: ErrorExtra;
  /** Seconds to wait before retrying (429 Retry-After), when given. */
  readonly retryAfterSeconds: number | null;
  /** The decoded response body (never contains request headers). */
  readonly body: unknown;

  constructor(init: {
    status: number;
    detail?: string | null;
    fieldErrors?: FieldErrors;
    extra?: ErrorExtra;
    retryAfterSeconds?: number | null;
    body?: unknown;
    message?: string;
  }) {
    const detail = init.detail ?? null;
    const fieldErrors = init.fieldErrors ?? {};
    super(init.message ?? buildMessage(init.status, detail, fieldErrors));
    this.name = "ApiError";
    this.status = init.status;
    this.kind = kindForStatus(init.status);
    this.detail = detail;
    this.fieldErrors = fieldErrors;
    this.extra = init.extra ?? {};
    this.retryAfterSeconds = init.retryAfterSeconds ?? null;
    this.body = init.body;
  }

  /** First message for a field, or for non-field errors when no field is given. */
  fieldMessage(field = "non_field_errors"): string | null {
    return this.fieldErrors[field]?.[0] ?? null;
  }
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError;
}

export function kindForStatus(status: number): ApiErrorKind {
  if (status === 0) return "network";
  if (status === 401) return "unauthorized";
  if (status === 403) return "forbidden";
  if (status === 404) return "not_found";
  if (status === 409) return "conflict";
  if (status === 400 || status === 422) return "validation";
  if (status === 429) return "throttled";
  if (status >= 500) return "server";
  return "unknown";
}

const FALLBACK_MESSAGES: Partial<Record<ApiErrorKind, string>> = {
  network: "Cannot reach the ApplyForge backend.",
  unauthorized: "The API token was rejected.",
  forbidden: "This action is not allowed.",
  not_found: "Not found.",
  conflict: "The request conflicts with the current state.",
  validation: "Some values are invalid.",
  throttled: "Too many requests. Please wait and try again.",
  server: "The backend reported an error.",
};

function buildMessage(status: number, detail: string | null, fieldErrors: FieldErrors): string {
  if (detail) return detail;
  const first = Object.entries(fieldErrors)[0];
  if (first && first[1][0]) {
    return first[0] === "non_field_errors" ? first[1][0] : `${first[0]}: ${first[1][0]}`;
  }
  return FALLBACK_MESSAGES[kindForStatus(status)] ?? `Request failed (HTTP ${status}).`;
}

function toMessages(value: unknown): string[] | null {
  if (typeof value === "string") return [value];
  if (Array.isArray(value)) {
    const out: string[] = [];
    for (const item of value) {
      if (typeof item === "string") out.push(item);
      else if (item && typeof item === "object") {
        // Nested serializer errors, e.g. contacts: [{email: ["..."]}]
        for (const [key, nested] of Object.entries(item)) {
          for (const message of toMessages(nested) ?? []) out.push(`${key}: ${message}`);
        }
      }
    }
    return out;
  }
  if (value && typeof value === "object") {
    // A nested serializer's errors, e.g. {"contact": {"email": ["..."]}}
    const out: string[] = [];
    for (const [key, nested] of Object.entries(value)) {
      for (const message of toMessages(nested) ?? []) out.push(`${key}: ${message}`);
    }
    return out;
  }
  return null;
}

/** Build an ApiError from an error response body (decoded JSON, text, or null). */
export function parseErrorBody(status: number, body: unknown, retryAfterHeader: string | null = null): ApiError {
  let detail: string | null = null;
  const fieldErrors: FieldErrors = {};
  const extra: ErrorExtra = {};
  const isValidation = status === 400 || status === 422;

  if (Array.isArray(body)) {
    if (!isValidation) return new ApiError({ status, body, retryAfterSeconds: parseRetryAfter(retryAfterHeader) });
    const messages = toMessages(body);
    if (messages?.length) fieldErrors.non_field_errors = messages;
  } else if (body && typeof body === "object") {
    for (const [key, value] of Object.entries(body as Record<string, unknown>)) {
      if (key === "detail" && typeof value === "string") {
        detail = value;
      } else if (typeof value === "number" || typeof value === "boolean" || value === null) {
        extra[key] = value;
      } else if (!isValidation) {
        if (typeof value === "string") extra[key] = value;
      } else if (typeof value === "string") {
        // A bare string under a field name is still a field error.
        fieldErrors[key] = [value];
      } else {
        const messages = toMessages(value);
        if (messages?.length) fieldErrors[key] = messages;
      }
    }
  } else if (typeof body === "string" && status < 500) {
    // Short plain-text bodies only; HTML error pages are never shown.
    const text = body.trim();
    if (text && !text.startsWith("<") && text.length <= 300) detail = text;
  }

  return new ApiError({
    status,
    detail,
    fieldErrors,
    extra,
    body,
    retryAfterSeconds: parseRetryAfter(retryAfterHeader),
  });
}

export function parseRetryAfter(value: string | null): number | null {
  if (!value) return null;
  const trimmed = value.trim();
  if (/^\d+(\.\d+)?$/.test(trimmed)) return Math.ceil(Number(trimmed));
  const date = Date.parse(trimmed);
  if (!Number.isNaN(date)) return Math.max(0, Math.ceil((date - Date.now()) / 1000));
  return null;
}
