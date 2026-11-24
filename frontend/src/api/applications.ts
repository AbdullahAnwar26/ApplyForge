/**
 * Applications API (backend/tracker/api/views.py ApplicationViewSet,
 * tracker/applications.py, tracker/generation.py):
 *
 *   GET   applications/                 queue_position (nulls last), then newest; filters profile,
 *                                       job, company, status (comma list), channel,
 *                                       awaiting_action=true|false; optional page/page_size
 *   POST  applications/                 {job, profile?, contact?, resume?, channel?, note?}
 *                                       -> 201 detail; 409 {detail, application} if it exists;
 *                                       job must be SHORTLISTED or APPLIED
 *   GET   applications/{id}/            detail (+ generated_body, generated_whatsapp_message, last_error)
 *   PATCH applications/{id}/            only status, channel, contact, resume, note, follow_up_on
 *                                       (YYYY-MM-DD or null) and the drafts
 *   GET   applications/{id}/events/     history (status changes and notes), newest first
 *   POST  applications/{id}/notes/      {note} -> 201 event; the status is unchanged
 *   GET   applications/summary/         {total, by_status, awaiting_action, sent_total,
 *                                       follow_ups_due, recent}; ?profile=
 *   GET   applications/?follow_up_due=true&today=YYYY-MM-DD   open applications whose reminder day has come
 *                                       (`today` = the browser's calendar day; also on summary/)
 *   POST  applications/{id}/generate/   NO body. 200 {application, model, truncated};
 *                                       400 input (nothing changed); 422/502/503/504/409 {detail,
 *                                       application} with last_error set, status and drafts kept.
 *
 * Sending lives in api/sending.ts (explicit confirmation only).
 */

import type { ApiClient } from "./client";
import type { ListResult } from "./pagination";
import type {
  ApplicationChannel,
  ApplicationDetail,
  ApplicationEvent,
  ApplicationsOverview,
  ApplicationStatus,
  ApplicationSummary,
  GenerationResult,
} from "./types";

export const APPLICATIONS_PAGE_SIZE = 25;

/** Statuses the backend sets itself (generation); never offered as a manual choice. */
export const SYSTEM_ONLY_STATUSES: ApplicationStatus[] = ["GENERATED", "FAILED", "SEND_FAILED", "COMPOSE_OPENED"];
export const PRE_SEND_STATUSES: ApplicationStatus[] = [
  "SHORTLISTED", "GENERATED", "OPENED", "READY_TO_SEND", "SKIPPED", "FAILED", "SEND_FAILED", "COMPOSE_OPENED",
];
export const POST_SEND_STATUSES: ApplicationStatus[] = ["SENT", "FOLLOW_UP", "INTERVIEW", "OFFER", "REJECTED"];
export const REQUIRES_SENT: ApplicationStatus[] = ["FOLLOW_UP", "INTERVIEW", "OFFER", "REJECTED"];
/** A reminder counts as due only while the application is still open (backend FOLLOW_UP_OPEN_STATUSES). */
export const FOLLOW_UP_OPEN_STATUSES: ApplicationStatus[] = ["SENT", "FOLLOW_UP", "INTERVIEW", "OFFER"];
/** Statuses from which drafts can be generated. */
export const GENERATABLE_STATUSES: ApplicationStatus[] = [
  "SHORTLISTED", "GENERATED", "OPENED", "READY_TO_SEND", "FAILED", "SEND_FAILED",
];
/** Job statuses that allow creating an application. */
export const APPLICABLE_JOB_STATUSES = ["SHORTLISTED", "APPLIED"] as const;

export function isSent(application: Pick<ApplicationSummary, "status" | "sent_at">): boolean {
  return application.sent_at !== null || POST_SEND_STATUSES.includes(application.status);
}

/**
 * Manual status changes the backend accepts from the current state
 * (tracker/applications.py check_status_change). The backend still validates.
 */
export function allowedStatusChanges(application: Pick<ApplicationSummary, "status" | "sent_at">): ApplicationStatus[] {
  const sent = isSent(application);
  const manual: ApplicationStatus[] = [
    "SHORTLISTED", "OPENED", "READY_TO_SEND", "SENT", "SKIPPED", "FOLLOW_UP", "INTERVIEW", "OFFER", "REJECTED",
  ];
  return manual.filter((status) => {
    if (status === application.status) return false;
    // Already sent: offering "Sent" again (e.g. from Interviewing) would only re-confirm the send.
    if (status === "SENT" && sent) return false;
    if (REQUIRES_SENT.includes(status) && !sent) return false;
    if (sent && PRE_SEND_STATUSES.includes(status)) return false;
    return true;
  });
}

export function canEditDrafts(application: Pick<ApplicationSummary, "status" | "sent_at">): boolean {
  return !isSent(application);
}

export interface ApplicationFilters {
  profile: number;
  status?: ApplicationStatus;
  channel?: ApplicationChannel;
  company?: number;
  job?: number;
  awaiting_action?: boolean;
  follow_up_due?: boolean;
}

export const applicationKeys = {
  all: ["applications"] as const,
  lists: () => ["applications", "list"] as const,
  list: (filters: ApplicationFilters, page: number) => ["applications", "list", filters, page] as const,
  forJob: (profile: number, job: number) => ["applications", "list", { profile, job }, "all"] as const,
  detail: (id: number) => ["applications", "detail", id] as const,
  events: (id: number) => ["applications", "events", id] as const,
  summary: (profile: number) => ["applications", "summary", profile] as const,
};

export function listApplications(
  api: ApiClient,
  filters: ApplicationFilters,
  page: number,
  signal?: AbortSignal,
): Promise<ListResult<ApplicationSummary>> {
  return api.list<ApplicationSummary>("applications/", {
    // "Due" is decided by the user's calendar day, not the server's time zone.
    query: filters.follow_up_due ? { ...filters, today: localToday() } : { ...filters },
    page: { page, pageSize: APPLICATIONS_PAGE_SIZE },
    signal,
  });
}

/** Applications in any of `statuses` (the backend accepts a comma-separated list), up to `size`. */
export function listApplicationsInStatuses(
  api: ApiClient,
  profile: number,
  statuses: ApplicationStatus[],
  page: number,
  size = 50,
  signal?: AbortSignal,
): Promise<ListResult<ApplicationSummary>> {
  return api.list<ApplicationSummary>("applications/", {
    query: { profile, status: statuses.join(",") },
    page: { page, pageSize: size },
    signal,
  });
}

/** The (at most one) application of `profile` for `job`. */
export async function findApplicationForJob(api: ApiClient, profile: number, job: number, signal?: AbortSignal) {
  const result = await api.list<ApplicationSummary>("applications/", { query: { profile, job }, signal });
  return result.items[0] ?? null;
}

export function getApplication(api: ApiClient, id: number, signal?: AbortSignal): Promise<ApplicationDetail> {
  return api.get<ApplicationDetail>(`applications/${id}/`, { signal });
}

export function getApplicationEvents(api: ApiClient, id: number, signal?: AbortSignal): Promise<ApplicationEvent[]> {
  return api.get<ApplicationEvent[]>(`applications/${id}/events/`, { signal });
}

export function getApplicationsOverview(api: ApiClient, profile: number, signal?: AbortSignal): Promise<ApplicationsOverview> {
  return api.get<ApplicationsOverview>("applications/summary/", { query: { profile, today: localToday() }, signal });
}

export interface ApplicationCreateInput {
  job: number;
  profile: number;
  contact?: number | null;
  resume?: number | null;
  channel?: ApplicationChannel;
  note?: string;
}

export function createApplication(api: ApiClient, input: ApplicationCreateInput): Promise<ApplicationDetail> {
  return api.post<ApplicationDetail>("applications/", input);
}

export interface ApplicationUpdate {
  status?: ApplicationStatus;
  channel?: ApplicationChannel;
  contact?: number | null;
  resume?: number | null;
  note?: string;
  follow_up_on?: string | null;
  generated_subject?: string | null;
  generated_body?: string | null;
  generated_whatsapp_message?: string | null;
}

export function updateApplication(api: ApiClient, id: number, changes: ApplicationUpdate): Promise<ApplicationDetail> {
  return api.patch<ApplicationDetail>(`applications/${id}/`, changes);
}

/** A note in the history without a status change. */
export function addApplicationNote(api: ApiClient, id: number, note: string): Promise<ApplicationEvent> {
  return api.post<ApplicationEvent>(`applications/${id}/notes/`, { note });
}

/** Today in the browser's local time zone, as YYYY-MM-DD (the backend compares calendar days). */
export function localToday(now: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

/** Add days to a YYYY-MM-DD day (calendar arithmetic, no time zone involved). */
export function addDays(day: string, days: number): string {
  const [y, m, d] = day.split("-").map(Number);
  return new Date(Date.UTC(y!, m! - 1, d! + days)).toISOString().slice(0, 10);
}

/** Days ahead for the reminder set when an application is marked sent (backend FOLLOW_UP_DEFAULT_DAYS). */
export const FOLLOW_UP_DEFAULT_DAYS = 7;

/** Whether a follow-up reminder is due: its day has come and the application is still open. */
export function isFollowUpDue(
  application: Pick<ApplicationSummary, "status" | "follow_up_on">,
  today = localToday(),
): boolean {
  return (
    application.follow_up_on !== null &&
    application.follow_up_on <= today &&
    FOLLOW_UP_OPEN_STATUSES.includes(application.status)
  );
}

/** Explicit, user-triggered generation. The backend rejects any request body. */
export function generateDrafts(api: ApiClient, id: number): Promise<GenerationResult> {
  return api.post<GenerationResult>(`applications/${id}/generate/`, {});
}
