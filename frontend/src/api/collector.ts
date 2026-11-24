/**
 * Collector API (backend/collector/api.py, collector/progressive.py):
 *
 *   GET  collector/runs/               newest first; filters status, trigger, profile; optional paging
 *   GET  collector/runs/{id}/          one run, with live progress (counts, stage) while RUNNING
 *   GET  collector/runs/{id}/items/    what happened to each examined post
 *   POST collector/runs/               {query, target, date_range, date_from?, date_to?, profile?}
 *        Starts a collection in the BACKGROUND and answers at once:
 *          202 {run}                     the run (poll it for progress)
 *          400 {<field>: [..]}           invalid input (nothing recorded)
 *          409 {detail, run}             a collection is already running on the server
 *          429                           throttled ("collector-run")
 *   POST collector/runs/{id}/cancel/   stop after the current post/page (keeps what was collected)
 *   POST collector/runs/{id}/continue/ new run with the same search for the jobs still missing
 *
 * POSTs are never retried (api/queryClient.ts: mutations never retry).
 */

import type { ApiClient } from "./client";
import type { ListResult } from "./pagination";
import type {
  CollectedPost,
  CollectedPostStatus,
  CollectionInput,
  CollectorRun,
  CollectorRunStatus,
  CollectorRunTrigger,
  DateRange,
} from "./types";

/** Backend default ceiling (COLLECTOR_API_MAX_POSTS_PER_RUN); the server has the final say. */
export const MAX_TARGET = 500;
export const DEFAULT_TARGET = 25;
export const MAX_QUERY_LENGTH = 200;
export const RUNS_PAGE_SIZE = 20;
export const ITEMS_PAGE_SIZE = 50;
/** How often a running collection is polled. */
export const RUN_POLL_MS = 1500;

export interface RunFilters {
  status?: CollectorRunStatus;
  trigger?: CollectorRunTrigger;
  profile?: number;
}

export const collectorKeys = {
  all: ["collector"] as const,
  runs: (filters: RunFilters, page: number) => ["collector", "runs", filters, page] as const,
  run: (id: number) => ["collector", "run", id] as const,
  items: (id: number, status: string, page: number) => ["collector", "items", id, status, page] as const,
  latest: ["collector", "latest"] as const,
};

export function listRuns(api: ApiClient, filters: RunFilters, page: number, signal?: AbortSignal): Promise<ListResult<CollectorRun>> {
  return api.list<CollectorRun>("collector/runs/", { query: { ...filters }, page: { page, pageSize: RUNS_PAGE_SIZE }, signal });
}

export function getRun(api: ApiClient, id: number, signal?: AbortSignal): Promise<CollectorRun> {
  return api.get<CollectorRun>(`collector/runs/${id}/`, { signal });
}

export function listRunItems(
  api: ApiClient,
  id: number,
  status: CollectedPostStatus | "",
  page: number,
  signal?: AbortSignal,
): Promise<ListResult<CollectedPost>> {
  return api.list<CollectedPost>(`collector/runs/${id}/items/`, {
    query: { status: status || undefined },
    page: { page, pageSize: ITEMS_PAGE_SIZE },
    signal,
  });
}

/** Start ONE background collection. Only ever called from an explicit, confirmed user action. */
export function startCollection(api: ApiClient, input: CollectionInput): Promise<{ run: CollectorRun }> {
  return api.post<{ run: CollectorRun }>("collector/runs/", input);
}

export function cancelRun(api: ApiClient, id: number): Promise<CollectorRun> {
  return api.post<CollectorRun>(`collector/runs/${id}/cancel/`, {});
}

export function continueRun(api: ApiClient, id: number): Promise<{ run: CollectorRun }> {
  return api.post<{ run: CollectorRun }>(`collector/runs/${id}/continue/`, {});
}

export interface CollectionFormValues {
  query: string;
  target: string;
  dateRange: DateRange;
  dateFrom: string;
  dateTo: string;
}

export type CollectionFormErrors = Partial<Record<"query" | "target" | "dateFrom" | "dateTo", string>>;

/** Client-side checks that mirror the backend's (the backend still validates everything). */
export function validateCollection(values: CollectionFormValues, today: string): CollectionFormErrors {
  const errors: CollectionFormErrors = {};
  const query = values.query.trim();
  if (!query) errors.query = "Enter what to search for, e.g. Python Backend Developer.";
  else if (query.length > MAX_QUERY_LENGTH) errors.query = `Use at most ${MAX_QUERY_LENGTH} characters.`;
  if (!/^\d+$/.test(values.target.trim()) || Number(values.target) < 1) {
    errors.target = "Enter how many jobs to collect (a whole number).";
  } else if (Number(values.target) > MAX_TARGET) {
    errors.target = `Collect at most ${MAX_TARGET} jobs in one run.`;
  }
  if (values.dateRange === "custom") {
    if (!values.dateFrom) errors.dateFrom = "Choose the first day.";
    else if (values.dateFrom > today) errors.dateFrom = "The range cannot start in the future.";
    if (values.dateTo && values.dateFrom && values.dateTo < values.dateFrom) {
      errors.dateTo = "The last day must be on or after the first day.";
    }
  }
  return errors;
}

export function collectionInput(values: CollectionFormValues, profile?: number): CollectionInput {
  const custom = values.dateRange === "custom";
  return {
    query: values.query.trim().replace(/\s+/g, " "),
    target: Number(values.target),
    date_range: values.dateRange,
    date_from: custom ? values.dateFrom || null : null,
    date_to: custom ? values.dateTo || null : null,
    ...(profile ? { profile } : {}),
  };
}

export const isActive = (run: CollectorRun | null | undefined): boolean => run?.status === "RUNNING";
