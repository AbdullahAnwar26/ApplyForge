/**
 * Jobs list filters live in the URL (?status=…&outcome=…&page=2) so they survive
 * navigation and back/forward. Only the backend's supported filters exist here;
 * unknown or invalid values are ignored rather than sent.
 */

import type { JobFilters } from "../../api/jobs";
import {
  JOB_SOURCES,
  JOB_STATUSES,
  SCREENING_OUTCOMES,
  SCREENING_REASONS,
  UNSCREENED,
  type JobSource,
  type JobStatus,
  type ScreeningOutcome,
  type ScreeningReason,
} from "../../api/types";

export interface JobListParams {
  status?: JobStatus;
  outcome?: ScreeningOutcome | typeof UNSCREENED;
  reason?: ScreeningReason;
  source?: JobSource;
  company?: number;
  /** "Only jobs screened for the active profile". */
  mine: boolean;
  page: number;
}


function oneOf<T extends string>(value: string | null, allowed: readonly T[]): T | undefined {
  return value !== null && (allowed as readonly string[]).includes(value) ? (value as T) : undefined;
}

function positiveInt(value: string | null): number | undefined {
  return value !== null && /^[1-9]\d{0,9}$/.test(value) ? Number(value) : undefined;
}

export function readParams(search: URLSearchParams): JobListParams {
  return {
    status: oneOf(search.get("status"), JOB_STATUSES),
    outcome: oneOf(search.get("outcome"), [...SCREENING_OUTCOMES, UNSCREENED] as const),
    reason: oneOf(search.get("reason"), SCREENING_REASONS),
    source: oneOf(search.get("source"), JOB_SOURCES),
    company: positiveInt(search.get("company")),
    mine: search.get("mine") === "1",
    page: positiveInt(search.get("page")) ?? 1,
  };
}

/** New search params with `changes` applied; any filter change returns to page 1. */
export function writeParams(current: JobListParams, changes: Partial<JobListParams>): URLSearchParams {
  const next = { ...current, ...changes };
  if (!("page" in changes)) next.page = 1;
  const search = new URLSearchParams();
  if (next.status) search.set("status", next.status);
  if (next.outcome) search.set("outcome", next.outcome);
  if (next.reason) search.set("reason", next.reason);
  if (next.source) search.set("source", next.source);
  if (next.company) search.set("company", String(next.company));
  if (next.mine) search.set("mine", "1");
  if (next.page > 1) search.set("page", String(next.page));
  return search;
}

export function hasFilters(params: JobListParams): boolean {
  return Boolean(params.status || params.outcome || params.reason || params.source || params.company || params.mine);
}

/** API filters for the backend; `profile` only when "screened for my profile" is on. */
export function toApiFilters(params: JobListParams, activeProfileId: number | null): JobFilters {
  const filters: JobFilters = {};
  if (params.status) filters.status = params.status;
  if (params.outcome) filters.screening_outcome = params.outcome;
  if (params.reason) filters.screening_reason = params.reason;
  if (params.source) filters.source = params.source;
  if (params.company) filters.company = params.company;
  if (params.mine && activeProfileId !== null) filters.profile = activeProfileId;
  return filters;
}

