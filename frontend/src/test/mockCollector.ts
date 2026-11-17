/**
 * Fake collector endpoints (backend/collector/api.py). NOTHING is collected: a POST
 * only returns the response chosen by `options.collector` and records the call, so
 * tests can assert how many start requests were sent (normally zero or one).
 */

import { delay, http, HttpResponse, type HttpHandler } from "msw";

import type { CollectorPostOutcome, CollectorRun } from "../api/types";
import { COLLECTOR_RUN_STATUSES, COLLECTOR_RUN_TRIGGERS } from "../api/types";
import type { Call, MockState } from "./mockBackend";
import { API } from "./server";

export type CollectorMode = "succeeded" | "partial" | "failed" | 400 | 409 | 429 | 500 | "network";

export function makeRun(id: number, overrides: Partial<CollectorRun> = {}): CollectorRun {
  return {
    id,
    status: "SUCCEEDED",
    trigger: "api",
    source: "linkedin",
    profile: 1,
    keywords: ["Python Developer"],
    limit: 5,
    counts: { found: 3, created: 2, existing: 1, skipped: 0, invalid: 0, failed: 0 },
    job_ids: [7, 8, 9],
    error: "",
    started_at: "2026-03-04T09:00:00Z",
    finished_at: "2026-03-04T09:01:30Z",
    duration_seconds: 90,
    ...overrides,
  };
}

const OUTCOMES: CollectorPostOutcome[] = [
  { index: 0, status: "created", url: null, title: "Python Backend Developer", job_id: 7, matched_by: null, screening_outcome: "SHORTLISTED", reason: null },
  { index: 1, status: "existing", url: null, title: "Django Developer", job_id: 8, matched_by: "source_url", screening_outcome: "NEEDS_REVIEW", reason: null },
  { index: 2, status: "skipped", url: null, title: null, job_id: null, matched_by: null, screening_outcome: null, reason: "Job seeker's post (not a hiring post)." },
];

export function collectorHandlers(
  state: MockState,
  record: (request: Request, extra?: Partial<Call>) => void,
  options: { collector: CollectorMode; collectorDelay: number },
  nextId: () => number,
): HttpHandler[] {
  return [
    http.get(`${API}/collector/runs/`, ({ request }) => {
      record(request);
      const q = new URL(request.url).searchParams;
      let runs = state.runs;
      const status = q.get("status");
      if (status) {
        if (!(COLLECTOR_RUN_STATUSES as readonly string[]).includes(status)) return HttpResponse.json({ status: ["Must be one of: ..."] }, { status: 400 });
        runs = runs.filter((r) => r.status === status);
      }
      const trigger = q.get("trigger");
      if (trigger) {
        if (!(COLLECTOR_RUN_TRIGGERS as readonly string[]).includes(trigger)) return HttpResponse.json({ trigger: ["Must be one of: ..."] }, { status: 400 });
        runs = runs.filter((r) => r.trigger === trigger);
      }
      const profile = q.get("profile");
      if (profile) runs = runs.filter((r) => r.profile === Number(profile));
      if (!q.has("page") && !q.has("page_size")) return HttpResponse.json(runs);
      const size = Math.min(200, Number(q.get("page_size")) || 50);
      const page = Number(q.get("page") ?? "1");
      const pages = Math.max(1, Math.ceil(runs.length / size));
      if (page < 1 || page > pages) return HttpResponse.json({ detail: "Invalid page." }, { status: 404 });
      return HttpResponse.json({
        count: runs.length,
        next: page < pages ? "next" : null,
        previous: page > 1 ? "previous" : null,
        results: runs.slice((page - 1) * size, page * size),
      });
    }),
    http.get(`${API}/collector/runs/:id/`, ({ request, params }) => {
      record(request);
      const run = state.runs.find((r) => r.id === Number(params.id));
      return run ? HttpResponse.json(run) : HttpResponse.json({ detail: "No CollectorRun matches the given query." }, { status: 404 });
    }),
    http.post(`${API}/collector/runs/`, async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      record(request, { body });
      await delay(options.collectorDelay);
      const unknown = Object.keys(body).filter((k) => !["keywords", "limit", "profile"].includes(k));
      if (unknown.length) return HttpResponse.json(Object.fromEntries(unknown.map((k) => [k, ["Unknown field."]])), { status: 400 });
      const base = { keywords: (body.keywords as string[]) ?? [], limit: (body.limit as number) ?? 5, profile: (body.profile as number) ?? null };
      switch (options.collector) {
        case "network":
          return HttpResponse.error();
        case 409:
          return HttpResponse.json({ detail: "Another collector run is in progress.", run: 41 }, { status: 409 });
        case 429:
          return HttpResponse.json({ detail: "Request was throttled. Expected available in 37 seconds." }, { status: 429, headers: { "Retry-After": "37" } });
        case 400: {
          const run = makeRun(nextId(), { ...base, status: "FAILED", counts: {}, job_ids: [], error: "No profile exists. Create a profile first.", duration_seconds: 0 });
          state.runs.unshift(run);
          return HttpResponse.json({ profile: ["No profile exists. Create a profile first."], run: run.id }, { status: 400 });
        }
        case 500: {
          const run = makeRun(nextId(), { ...base, status: "FAILED", counts: {}, job_ids: [], error: "RuntimeError: boom" });
          state.runs.unshift(run);
          return HttpResponse.json({ detail: "The collector run failed unexpectedly; see the server log.", run: run.id }, { status: 500 });
        }
        case "partial": {
          const run = makeRun(nextId(), { ...base, status: "PARTIAL", counts: { found: 2, created: 1, existing: 1, skipped: 0, invalid: 0, failed: 0 }, job_ids: [7, 8], error: "LinkedIn stopped responding after 2 posts." });
          state.runs.unshift(run);
          return HttpResponse.json({ run, outcomes: OUTCOMES.slice(0, 2), detail: run.error }, { status: 502 });
        }
        case "failed": {
          const run = makeRun(nextId(), { ...base, status: "FAILED", counts: { found: 0, created: 0, existing: 0, skipped: 0, invalid: 0, failed: 0 }, job_ids: [], error: "Not logged in to LinkedIn. Run linkedin_login on the backend machine." });
          state.runs.unshift(run);
          return HttpResponse.json({ run, outcomes: [], detail: run.error }, { status: 502 });
        }
        default: {
          const run = makeRun(nextId(), { ...base });
          state.runs.unshift(run);
          return HttpResponse.json({ run, outcomes: OUTCOMES }, { status: 201 });
        }
      }
    }),
  ];
}
