/**
 * Fake collector endpoints (backend/collector/api.py). NOTHING is collected. A POST records
 * the call and answers like the backend (202 with a RUNNING run, or the error chosen by
 * `options.collector`). Each GET of a running run advances it: the next read shows it
 * finished (or cancelled, when cancel was requested), so polling can be tested.
 */

import { delay, http, HttpResponse, type HttpHandler } from "msw";

import type { CollectedPost, CollectorRun } from "../api/types";
import { COLLECTOR_RUN_STATUSES, COLLECTOR_RUN_TRIGGERS } from "../api/types";
import type { Call, MockState } from "./mockBackend";
import { API } from "./server";

export type CollectorMode = "succeeded" | 400 | 409 | 429 | "network";

export function makeRun(id: number, overrides: Partial<CollectorRun> = {}): CollectorRun {
  return {
    id,
    status: "SUCCEEDED",
    trigger: "api",
    source: "linkedin",
    profile: 1,
    keywords: ["Python Developer"],
    limit: 5,
    counts: { found: 3, created: 2, existing: 1, skipped: 0, invalid: 0, failed: 0, out_of_range: 0 },
    job_ids: [7, 8, 9],
    error: "",
    started_at: "2026-03-04T09:00:00Z",
    finished_at: "2026-03-04T09:01:30Z",
    duration_seconds: 90,
    search_query: "Python Developer",
    date_range: "past-week",
    date_from: null,
    date_to: null,
    cancel_requested: false,
    stage: "Done: no more matching posts",
    stop_reason: "exhausted",
    heartbeat_at: "2026-03-04T09:01:30Z",
    remaining: 3,
    ...overrides,
  };
}

export function makeItem(id: number, overrides: Partial<CollectedPost> = {}): CollectedPost {
  return {
    id,
    position: id - 1,
    status: "created",
    post_url: `https://www.linkedin.com/feed/update/urn:li:activity:${7300000000000000000 + id}/`,
    title: `Python Developer ${id}`,
    job: 7,
    matched_by: "",
    reason: "",
    posted_at: "2026-03-03T09:00:00Z",
    truncated: false,
    created_at: "2026-03-04T09:00:10Z",
    ...overrides,
  };
}

const START_FIELDS = ["query", "target", "date_range", "date_from", "date_to", "profile"];

function finish(run: CollectorRun): void {
  const target = run.limit ?? 0;
  if (run.cancel_requested) {
    const created = Math.min(target, 2);
    Object.assign(run, {
      status: "CANCELLED", stop_reason: "cancelled", stage: "Cancelled", finished_at: "2026-03-04T09:02:00Z",
      counts: { found: 4, created, existing: 1, skipped: 1, invalid: 0, failed: 0, out_of_range: 0, ready: created },
      remaining: target - created,
    });
    return;
  }
  Object.assign(run, {
    status: "SUCCEEDED", stop_reason: "target_reached", stage: `Done: ${target} job(s) ready to apply`,
    finished_at: "2026-03-04T09:05:00Z", duration_seconds: 300,
    counts: { found: target + 4, created: target + 1, existing: 2, skipped: 2, invalid: 0, failed: 0, out_of_range: 0, ready: target },
    remaining: 0,
  });
}

export function collectorHandlers(
  state: MockState,
  record: (request: Request, extra?: Partial<Call>) => void,
  options: { collector: CollectorMode; collectorDelay: number },
  nextId: () => number,
): HttpHandler[] {
  const start = (base: Partial<CollectorRun>) => {
    const target = base.limit ?? 25;
    const run = makeRun(nextId(), {
      ...base, status: "RUNNING", stop_reason: "", finished_at: null, duration_seconds: null,
      counts: { found: 2, created: 1, existing: 0, skipped: 1, invalid: 0, failed: 0, out_of_range: 0 },
      remaining: target - 1, stage: "Reading search results", job_ids: [],
    });
    state.runs.unshift(run);
    return HttpResponse.json({ run }, { status: 202 });
  };
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
      if (!run) return HttpResponse.json({ detail: "No CollectorRun matches the given query." }, { status: 404 });
      const snapshot = structuredClone(run);
      if (run.status === "RUNNING" && !state.holdRuns) finish(run); // the NEXT read shows it finished
      return HttpResponse.json(snapshot);
    }),
    http.get(`${API}/collector/runs/:id/items/`, ({ request, params }) => {
      record(request);
      const items = (state.runItems[Number(params.id)] ?? []).filter((i) => {
        const status = new URL(request.url).searchParams.get("status");
        return !status || i.status === status;
      });
      return HttpResponse.json({ count: items.length, next: null, previous: null, results: items });
    }),
    http.post(`${API}/collector/runs/:id/cancel/`, ({ request, params }) => {
      record(request);
      const run = state.runs.find((r) => r.id === Number(params.id));
      if (!run) return HttpResponse.json({ detail: "Not found." }, { status: 404 });
      if (run.status !== "RUNNING") return HttpResponse.json({ status: ["This run is not running."] }, { status: 400 });
      run.cancel_requested = true;
      run.stage = "Cancelling…";
      return HttpResponse.json(run, { status: 202 });
    }),
    http.post(`${API}/collector/runs/:id/continue/`, ({ request, params }) => {
      record(request);
      const run = state.runs.find((r) => r.id === Number(params.id));
      if (!run) return HttpResponse.json({ detail: "Not found." }, { status: 404 });
      if (!run.remaining) return HttpResponse.json({ status: ["Nothing is left to collect for this run."] }, { status: 400 });
      return start({ search_query: run.search_query, keywords: run.keywords, limit: run.remaining, date_range: run.date_range });
    }),
    http.post(`${API}/collector/runs/`, async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      record(request, { body });
      await delay(options.collectorDelay);
      const unknown = Object.keys(body).filter((k) => !START_FIELDS.includes(k));
      if (unknown.length) return HttpResponse.json(Object.fromEntries(unknown.map((k) => [k, ["Unknown field."]])), { status: 400 });
      switch (options.collector) {
        case "network":
          return HttpResponse.error();
        case 409:
          return HttpResponse.json({ detail: "Another collection is running on this server. Try again in a few minutes.", run: null }, { status: 409 });
        case 429:
          return HttpResponse.json({ detail: "Request was throttled. Expected available in 37 seconds." }, { status: 429, headers: { "Retry-After": "37" } });
        case 400:
          return HttpResponse.json({ query: ["Enter what to search for, e.g. Python Backend Developer."] }, { status: 400 });
        default:
          return start({
            search_query: String(body.query ?? ""),
            keywords: [String(body.query ?? "")],
            limit: Number(body.target ?? 25),
            date_range: (body.date_range as CollectorRun["date_range"]) ?? "any",
            date_from: (body.date_from as string | null) ?? null,
            date_to: (body.date_to as string | null) ?? null,
            profile: (body.profile as number) ?? null,
          });
      }
    }),
  ];
}
