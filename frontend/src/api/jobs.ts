/**
 * Jobs API (backend/tracker/api/views.py JobViewSet, CompanyViewSet):
 *
 *   GET   jobs/                  newest discovered first; filters status, screening_outcome
 *                                (or UNSCREENED), screening_reason, source, company,
 *                                profile (= screened against); optional page/page_size
 *   GET   jobs/{id}/             detail: + full_jd, screening_details, contacts
 *   PATCH jobs/{id}/             {status} and review corrections (title, company_name, location,
 *                                work_mode, recruiter_name, full_jd, contacts, contacts_verified)
 *   POST  jobs/{id}/select/      select for application: shortlist + ONE application (201 / 200 existing)
 *   POST  jobs/select/           {jobs: [...]} the same for several jobs
 *   POST  jobs/bulk-status/      {jobs, status} mark to review / selected / skipped
 *   POST  jobs/{id}/screen/      {profile?} -> job detail (never changes status)
 *   POST  jobs/screen/           {profile?, jobs?} -> {profile, screened, outcomes}
 *   POST  jobs/ingest/           manual, idempotent ingest (screens on create)
 *   GET   companies/, companies/{id}/   read-only
 *
 * Filters also: search (text), run (jobs a collection run found), jd_complete.
 */

import type { ApiClient } from "./client";
import type { ListResult } from "./pagination";
import type {
  Company,
  JobDetail,
  JobIngestInput,
  JobIngestResult,
  JobReviewUpdate,
  JobSource,
  JobStatus,
  JobSummary,
  ScreenAllResult,
  ScreeningOutcome,
  ScreeningReason,
  SelectJobResult,
  SelectJobsResult,
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
  /** Text search: every word must appear in the title, company, location or description. */
  search?: string;
  /** Jobs found by this collection run. */
  run?: number;
  jd_complete?: boolean;
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

export function updateJob(api: ApiClient, id: number, update: JobReviewUpdate): Promise<JobDetail> {
  return api.patch<JobDetail>(`jobs/${id}/`, update);
}

export function selectJob(api: ApiClient, id: number, profile?: number): Promise<SelectJobResult> {
  return api.post<SelectJobResult>(`jobs/${id}/select/`, profile ? { profile } : {});
}

export function selectJobs(api: ApiClient, jobs: number[], profile?: number): Promise<SelectJobsResult> {
  return api.post<SelectJobsResult>("jobs/select/", profile ? { jobs, profile } : { jobs });
}

export function bulkJobStatus(api: ApiClient, jobs: number[], status: JobStatus): Promise<{ updated: number }> {
  return api.post<{ updated: number }>("jobs/bulk-status/", { jobs, status });
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
