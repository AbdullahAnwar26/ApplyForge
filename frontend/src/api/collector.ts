/**
 * Collector API (backend/collector/api.py, collector/runs.py):
 *
 *   GET  collector/runs/        newest first; filters status, trigger, profile; optional paging
 *   GET  collector/runs/{id}/   one run (read-only; no PATCH/PUT/DELETE)
 *   POST collector/runs/        {keywords?, limit?, profile?} - unknown fields rejected.
 *        Starts a LinkedIn collection through the backend machine's saved browser
 *        session and WAITS for it (synchronous):
 *          201 {run, outcomes}               SUCCEEDED
 *          502 {run, outcomes, detail}       PARTIAL or FAILED
 *          400 {<field>: [..], run?}         invalid input (run recorded when the service rejected it)
 *          409 {detail, run}                 another run is RUNNING
 *          429                               throttled ("collector-run", 6/min)
 *          500 {detail, run}                 unexpected error (details only in the server log)
 *        One run at a time; RUNNING rows older than COLLECTOR_STALE_RUN_MINUTES (30) are
 *        marked FAILED when the next run is attempted.
 *
 * This module never retries a POST (see api/queryClient.ts: mutations never retry).
 */

import type { ApiClient } from "./client";
import type { ListResult } from "./pagination";
import type { CollectorRun, CollectorRunInput, CollectorRunResult, CollectorRunStatus, CollectorRunTrigger } from "./types";

/** Backend limits: MAX_KEYWORDS, MAX_KEYWORD_LENGTH, COLLECTOR_API_MAX/DEFAULT_POSTS_PER_RUN (defaults). */
export const MAX_KEYWORDS = 10;
export const MAX_KEYWORD_LENGTH = 100;
export const MAX_RUN_LIMIT = 10;
export const DEFAULT_RUN_LIMIT = 5;
export const STALE_RUN_MINUTES = 30;
export const RUNS_PAGE_SIZE = 20;

export interface RunFilters {
  status?: CollectorRunStatus;
  trigger?: CollectorRunTrigger;
  profile?: number;
}

export const collectorKeys = {
  all: ["collector"] as const,
  runs: (filters: RunFilters, page: number) => ["collector", "runs", filters, page] as const,
  run: (id: number) => ["collector", "run", id] as const,
};

export function listRuns(api: ApiClient, filters: RunFilters, page: number, signal?: AbortSignal): Promise<ListResult<CollectorRun>> {
  return api.list<CollectorRun>("collector/runs/", { query: { ...filters }, page: { page, pageSize: RUNS_PAGE_SIZE }, signal });
}

export function getRun(api: ApiClient, id: number, signal?: AbortSignal): Promise<CollectorRun> {
  return api.get<CollectorRun>(`collector/runs/${id}/`, { signal });
}

/**
 * Start ONE collection run. Only ever called from an explicit, confirmed user action.
 * A 502 is a completed-but-partial/failed run; its body is returned via ApiError.body.
 */
export function startRun(api: ApiClient, input: CollectorRunInput): Promise<CollectorRunResult> {
  return api.post<CollectorRunResult>("collector/runs/", input);
}

/** Keywords from free text: comma- or newline-separated, trimmed, de-duplicated (case-insensitive). */
export function parseKeywords(text: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of text.split(/[,\n]/)) {
    const keyword = raw.trim().replace(/\s+/g, " ");
    const key = keyword.toLocaleLowerCase();
    if (keyword && !seen.has(key)) {
      seen.add(key);
      out.push(keyword);
    }
  }
  return out;
}

export function validateRunInput(keywords: string[], limit: string): { keywords?: string; limit?: string } {
  const errors: { keywords?: string; limit?: string } = {};
  if (keywords.length === 0) errors.keywords = "Enter at least one keyword.";
  else if (keywords.length > MAX_KEYWORDS) errors.keywords = `Use at most ${MAX_KEYWORDS} keywords.`;
  else if (keywords.some((k) => k.length > MAX_KEYWORD_LENGTH)) errors.keywords = `Each keyword can have at most ${MAX_KEYWORD_LENGTH} characters.`;
  if (!/^\d+$/.test(limit.trim())) errors.limit = `Enter a whole number from 1 to ${MAX_RUN_LIMIT}.`;
  else {
    const value = Number(limit);
    if (value < 1 || value > MAX_RUN_LIMIT) errors.limit = `Enter a whole number from 1 to ${MAX_RUN_LIMIT}.`;
  }
  return errors;
}
