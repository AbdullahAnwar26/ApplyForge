/**
 * Jobs API (backend/tracker/api/views.py JobViewSet, CompanyViewSet):
 *
 *   GET   jobs/                  newest discovered first; filters status, screening_outcome
 *                                (or UNSCREENED), screening_reason, source, company,
 *                                profile (= screened against); optional page/page_size
 *   GET   jobs/{id}/             detail: + full_jd, screening_details, contacts
 *   PATCH jobs/{id}/             {status} only
 *   POST  jobs/{id}/screen/      {profile?} -> job detail (never changes status)
 *   POST  jobs/screen/           {profile?, jobs?} -> {profile, screened, outcomes}
 *   POST  jobs/ingest/           manual, idempotent ingest (screens on create)
 *   GET   companies/, companies/{id}/   read-only
 *
 * There is no text search and no sort parameter.
 */

import type { ApiClient } from "./client";
import type { ListResult } from "./pagination";
import type {
  Company,
  JobDetail,
  JobIngestInput,
  JobIngestResult,
  JobSource,
  JobStatus,
  JobSummary,
  ScreenAllResult,
  ScreeningOutcome,
  ScreeningReason,
} from "./types";

export const JOBS_PAGE_SIZE = 25;

export interface JobFilters {
  status?: JobStatus;
  screening_outcome?: ScreeningOutcome | "UNSCREENED";
  screening_reason?: ScreeningReason;
  source?: JobSource;
  company?: number;
  /** Only jobs last screened against this profile. */
  profile?: number;
}

export const jobKeys = {
  all: ["jobs"] as const,
  lists: () => ["jobs", "list"] as const,
  list: (filters: JobFilters, page: number) => ["jobs", "list", filters, page] as const,
  // Under "list" so every action that refreshes job lists (screening, status, ingest,
  // collection) refreshes these too.
  count: (filters: JobFilters) => ["jobs", "list", "count", filters] as const,
  recent: (size: number) => ["jobs", "list", "recent", size] as const,
  detail: (id: number) => ["jobs", "detail", id] as const,
};

export const companyKeys = {
  all: ["companies"] as const,
  detail: (id: number) => ["companies", id] as const,
};

export function listJobs(api: ApiClient, filters: JobFilters, page: number, signal?: AbortSignal): Promise<ListResult<JobSummary>> {
  return api.list<JobSummary>("jobs/", {
    query: { ...filters },
    page: { page, pageSize: JOBS_PAGE_SIZE },
    signal,
  });
}

/** Exact number of jobs matching `filters`: one row requested, the page's `count` used. */
export async function countJobs(api: ApiClient, filters: JobFilters, signal?: AbortSignal): Promise<number> {
  return (await api.list<JobSummary>("jobs/", { query: { ...filters }, page: { page: 1, pageSize: 1 }, signal })).count;
}

/** The newest `size` jobs (the backend orders by discovered date, newest first). */
export async function recentJobs(api: ApiClient, size: number, signal?: AbortSignal): Promise<JobSummary[]> {
  return (await api.list<JobSummary>("jobs/", { page: { page: 1, pageSize: size }, signal })).items;
}

export function getJob(api: ApiClient, id: number, signal?: AbortSignal): Promise<JobDetail> {
  return api.get<JobDetail>(`jobs/${id}/`, { signal });
}

export function updateJobStatus(api: ApiClient, id: number, status: JobStatus): Promise<JobDetail> {
  return api.patch<JobDetail>(`jobs/${id}/`, { status });
}

export function screenJob(api: ApiClient, id: number, profile: number): Promise<JobDetail> {
  return api.post<JobDetail>(`jobs/${id}/screen/`, { profile });
}

export function screenAllJobs(api: ApiClient, profile: number): Promise<ScreenAllResult> {
  return api.post<ScreenAllResult>("jobs/screen/", { profile });
}

export function ingestJob(api: ApiClient, input: JobIngestInput): Promise<JobIngestResult> {
  return api.post<JobIngestResult>("jobs/ingest/", input);
}

export async function listCompanies(api: ApiClient, signal?: AbortSignal): Promise<Company[]> {
  return (await api.list<Company>("companies/", { signal })).items;
}

export function getCompany(api: ApiClient, id: number, signal?: AbortSignal): Promise<Company> {
  return api.get<Company>(`companies/${id}/`, { signal });
}
