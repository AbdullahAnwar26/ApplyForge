/**
 * Fake applications endpoints following ApplicationViewSet, tracker/applications.py
 * and tracker/generation.py: supported filters (bad values -> 400), optional
 * pagination, create rules (job SHORTLISTED/APPLIED, 409 duplicate), PATCH field
 * whitelist and status rules, drafts locked after sending, SENT -> job APPLIED,
 * events, summary, and generation with configurable failures. No real model.
 */

import { delay, http, HttpResponse, type HttpHandler } from "msw";

import type {
  ApplicationDetail,
  ApplicationEvent,
  ApplicationStatus,
  ApplicationSummary,
} from "../api/types";
import { APPLICATION_CHANNELS, APPLICATION_STATUSES } from "../api/types";
import type { Call, MockState } from "./mockBackend";
import { API } from "./server";

const NOW = "2026-03-03T09:00:00Z";
const AWAITING: ApplicationStatus[] = ["SHORTLISTED", "GENERATED", "OPENED", "READY_TO_SEND", "FAILED", "FOLLOW_UP"];
const PRE_SEND: ApplicationStatus[] = ["SHORTLISTED", "GENERATED", "OPENED", "READY_TO_SEND", "SKIPPED", "FAILED"];
const POST_SEND: ApplicationStatus[] = ["SENT", "FOLLOW_UP", "REJECTED"];
const GENERATABLE: ApplicationStatus[] = ["SHORTLISTED", "GENERATED", "OPENED", "READY_TO_SEND", "FAILED"];
const UPDATABLE = ["status", "channel", "contact", "resume", "note", "generated_subject", "generated_body", "generated_whatsapp_message"];
const DRAFTS = ["generated_subject", "generated_body", "generated_whatsapp_message"] as const;

export type GenerationMode = "ok" | 503 | 504 | 502 | 422 | 409 | 429;

export const GENERATED_DRAFT = {
  generated_subject: "Application for Python Backend Developer",
  generated_body: "Dear Fictional Recruiter,\n\nI am applying for the Python Backend Developer role.\n\nRegards,\nRiya Fictional",
  generated_whatsapp_message: "Hi, I am applying for the Python Backend Developer role at Acme Fictional Labs.",
};

const FAILURE_DETAIL: Record<Exclude<GenerationMode, "ok" | 429>, string> = {
  503: "Cannot reach Ollama at http://localhost:11434/v1 (Connection refused). Is Ollama running with model 'qwen3:1.7b' pulled?",
  504: "Ollama did not respond within 180 seconds.",
  502: "The model response contained placeholder text such as [Your Name].",
  422: "The resume file is missing from storage.",
  409: "Generated content discarded: the application moved to SENT during generation.",
};

export function makeApplication(id: number, overrides: Partial<ApplicationDetail> = {}): ApplicationDetail {
  return {
    id,
    profile: 1,
    job: { id: 7, title: "Python Backend Developer", company: { id: 1, name: "Acme Fictional Labs" }, status: "SHORTLISTED", screening_outcome: "SHORTLISTED" },
    contact: null,
    resume: null,
    status: "SHORTLISTED",
    channel: "EMAIL",
    generated_subject: null,
    queue_position: null,
    created_at: NOW,
    generated_at: null,
    opened_at: null,
    sent_at: null,
    updated_at: NOW,
    generated_body: null,
    generated_whatsapp_message: null,
    last_error: null,
    ...overrides,
  };
}

function toSummary(a: ApplicationDetail): ApplicationSummary {
  const summary: Partial<ApplicationDetail> = { ...a };
  delete summary.generated_body;
  delete summary.generated_whatsapp_message;
  delete summary.last_error;
  return summary as ApplicationSummary;
}

const bad = (field: string, message: string) => HttpResponse.json({ [field]: [message] }, { status: 400 });

export function applicationHandlers(
  state: MockState,
  record: (request: Request, extra?: Partial<Call>) => void,
  options: { writeDelay: number; generation: GenerationMode; generationDelay: number },
  nextId: () => number,
): HttpHandler[] {
  const find = (id: unknown) => state.applications.find((a) => a.id === Number(id));
  const addEvent = (id: number, status: ApplicationStatus, notes: string) => {
    const event: ApplicationEvent = { id: nextId(), status, notes, timestamp: `2026-03-03T09:${String(state.events[id]?.length ?? 0).padStart(2, "0")}:00Z` };
    state.events[id] = [event, ...(state.events[id] ?? [])];
  };
  const sent = (a: ApplicationDetail) => a.sent_at !== null || POST_SEND.includes(a.status);
  const contactsOf = (jobId: number) => state.jobs.find((j) => j.id === jobId)?.contacts ?? [];
  const channelError = (channel: string, contactId: number | null | undefined, jobId: number) => {
    if (!contactId) return null;
    const contact = contactsOf(jobId).find((c) => c.id === contactId);
    if (!contact) return bad("contact", "This contact is not linked to the job.");
    if (channel === "EMAIL" && !contact.email) return bad("channel", "The selected contact has no email address.");
    if (channel === "WHATSAPP" && !contact.whatsapp && !contact.phone) return bad("channel", "The selected contact has no WhatsApp or phone number.");
    return null;
  };
  const resumeError = (resumeId: number | null | undefined, profile: number) => {
    if (!resumeId) return null;
    const resume = state.resumes.find((r) => r.id === resumeId);
    if (!resume) return bad("resume", `Invalid pk "${resumeId}" - object does not exist.`);
    if (resume.profile !== profile) return bad("resume", "This resume belongs to a different profile.");
    if (!resume.is_active) return bad("resume", "This resume is inactive.");
    return null;
  };

  return [
    http.get(`${API}/applications/summary/`, ({ request }) => {
      record(request);
      const profile = new URL(request.url).searchParams.get("profile");
      const apps = state.applications.filter((a) => !profile || a.profile === Number(profile));
      const by_status = Object.fromEntries(APPLICATION_STATUSES.map((s) => [s, apps.filter((a) => a.status === s).length]));
      const awaiting = apps.filter((a) => AWAITING.includes(a.status));
      return HttpResponse.json({
        total: apps.length,
        by_status,
        awaiting_action: { count: awaiting.length, statuses: AWAITING, oldest: awaiting.slice(0, 10).map(toSummary) },
        recent: apps.slice(0, 10).map(toSummary),
      });
    }),
    http.get(`${API}/applications/`, ({ request }) => {
      record(request);
      const q = new URL(request.url).searchParams;
      let apps = state.applications;
      for (const [name, pick] of [
        ["profile", (a: ApplicationDetail) => a.profile],
        ["job", (a: ApplicationDetail) => a.job.id],
        ["company", (a: ApplicationDetail) => a.job.company?.id],
      ] as const) {
        const value = q.get(name);
        if (value) apps = apps.filter((a) => pick(a) === Number(value));
      }
      const status = q.get("status");
      if (status) {
        const wanted = status.split(",");
        if (wanted.some((s) => !(APPLICATION_STATUSES as readonly string[]).includes(s))) return bad("status", "Must be one or more of: ...");
        apps = apps.filter((a) => wanted.includes(a.status));
      }
      const channel = q.get("channel");
      if (channel) {
        if (!(APPLICATION_CHANNELS as readonly string[]).includes(channel)) return bad("channel", "Must be one of: EMAIL, WHATSAPP, OTHER.");
        apps = apps.filter((a) => a.channel === channel);
      }
      const awaiting = q.get("awaiting_action");
      if (awaiting === "true") apps = apps.filter((a) => AWAITING.includes(a.status));
      if (awaiting === "false") apps = apps.filter((a) => !AWAITING.includes(a.status));

      const items = apps.map(toSummary);
      if (!q.has("page") && !q.has("page_size")) return HttpResponse.json(items);
      const size = Math.min(200, Number(q.get("page_size")) || 50);
      const page = Number(q.get("page") ?? "1");
      const pages = Math.max(1, Math.ceil(items.length / size));
      if (page < 1 || page > pages) return HttpResponse.json({ detail: "Invalid page." }, { status: 404 });
      return HttpResponse.json({
        count: items.length,
        next: page < pages ? "next" : null,
        previous: page > 1 ? "previous" : null,
        results: items.slice((page - 1) * size, page * size),
      });
    }),
    http.post(`${API}/applications/`, async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      record(request, { body });
      await delay(options.writeDelay);
      const job = state.jobs.find((j) => j.id === Number(body.job));
      if (!job) return bad("job", `Invalid pk "${String(body.job)}" - object does not exist.`);
      if (!["SHORTLISTED", "APPLIED"].includes(job.status)) {
        return bad("job", `Job ${job.id} has status ${job.status}. Set the job to SHORTLISTED before creating an application.`);
      }
      const profile = Number(body.profile);
      const existing = state.applications.find((a) => a.profile === profile && a.job.id === job.id);
      if (existing) return HttpResponse.json({ detail: "An application for this job already exists.", application: existing.id }, { status: 409 });
      const channel = (body.channel as string) ?? "EMAIL";
      const resumeId = (body.resume as number | undefined) ?? state.resumes.find((r) => r.profile === profile && r.is_active && r.is_default)?.id ?? null;
      const problem = channelError(channel, body.contact as number | undefined, job.id) ?? resumeError(resumeId, profile);
      if (problem) return problem;
      const application = makeApplication(nextId(), {
        profile,
        job: { id: job.id, title: job.title, company: job.company, status: job.status, screening_outcome: job.screening_outcome },
        contact: (body.contact as number | undefined) ?? null,
        resume: resumeId,
        channel: channel as ApplicationDetail["channel"],
      });
      state.applications.unshift(application);
      addEvent(application.id, "SHORTLISTED", (body.note as string) || "Application created.");
      return HttpResponse.json(application, { status: 201 });
    }),
    http.get(`${API}/applications/:id/`, ({ request, params }) => {
      record(request);
      const app = find(params.id);
      return app ? HttpResponse.json(app) : HttpResponse.json({ detail: "No Application matches the given query." }, { status: 404 });
    }),
    http.get(`${API}/applications/:id/events/`, ({ request, params }) => {
      record(request);
      const app = find(params.id);
      return app ? HttpResponse.json(state.events[app.id] ?? []) : HttpResponse.json({ detail: "Not found." }, { status: 404 });
    }),
    http.patch(`${API}/applications/:id/`, async ({ request, params }) => {
      const body = (await request.json()) as Record<string, unknown>;
      record(request, { body });
      await delay(options.writeDelay);
      const app = find(params.id);
      if (!app) return HttpResponse.json({ detail: "Not found." }, { status: 404 });
      const unexpected = Object.keys(body).filter((k) => !UPDATABLE.includes(k));
      if (unexpected.length) return HttpResponse.json(Object.fromEntries(unexpected.map((k) => [k, ["This field is read-only."]])), { status: 400 });
      if (!Object.keys(body).some((k) => k !== "note")) return bad("non_field_errors", "Provide at least one of: ...");
      const channel = (body.channel as string) ?? app.channel;
      const contact = "contact" in body ? (body.contact as number | null) : app.contact;
      const problem =
        ("contact" in body || "channel" in body ? channelError(channel, contact, app.job.id) : null) ??
        ("resume" in body ? resumeError(body.resume as number | null, app.profile) : null);
      if (problem) return problem;
      const target = body.status as ApplicationStatus | undefined;
      if (target && target !== app.status) {
        if (target === "GENERATED" || target === "FAILED") return bad("status", `${target} is set by content generation, not manually.`);
        if ((target === "FOLLOW_UP" || target === "REJECTED") && !sent(app)) return bad("status", `${target} is only allowed after the application was sent.`);
        if (sent(app) && PRE_SEND.includes(target)) return bad("status", `The application was already sent; it cannot move back to ${target}.`);
      }
      if (DRAFTS.some((f) => f in body) && sent(app)) return bad("non_field_errors", "Drafts of an application that was already sent cannot be edited.");

      for (const field of ["contact", "resume", "channel", ...DRAFTS] as const) {
        if (field in body) (app as unknown as Record<string, unknown>)[field] = body[field];
      }
      if (target && target !== app.status) {
        const old = app.status;
        app.status = target;
        if (target === "OPENED" && !app.opened_at) app.opened_at = NOW;
        if (target === "SENT" && !app.sent_at) {
          app.sent_at = NOW;
          const job = state.jobs.find((j) => j.id === app.job.id);
          if (job) job.status = "APPLIED";
          app.job.status = "APPLIED";
        }
        addEvent(app.id, target, (body.note as string) || `Status changed from ${old} to ${target}.`);
      }
      return HttpResponse.json(app);
    }),
    http.post(`${API}/applications/:id/generate/`, async ({ request, params }) => {
      const text = await request.text();
      const body = text ? (JSON.parse(text) as Record<string, unknown>) : {};
      record(request, { body });
      const app = find(params.id);
      if (!app) return HttpResponse.json({ detail: "Not found." }, { status: 404 });
      if (Object.keys(body).length) return bad("non_field_errors", "This endpoint takes no body.");
      if (!GENERATABLE.includes(app.status)) return bad("status", `Cannot generate content for an application with status ${app.status}.`);
      await delay(options.generationDelay);
      const mode = options.generation;
      if (mode === 429) {
        return HttpResponse.json({ detail: "Request was throttled." }, { status: 429, headers: { "Retry-After": "30" } });
      }
      if (mode !== "ok") {
        app.last_error = FAILURE_DETAIL[mode];
        return HttpResponse.json({ detail: FAILURE_DETAIL[mode], application: app.id }, { status: mode });
      }
      Object.assign(app, GENERATED_DRAFT, { generated_at: "2026-03-03T10:00:00Z", last_error: null });
      if (app.status !== "GENERATED") {
        addEvent(app.id, "GENERATED", "Content generated with qwen3:1.7b.");
        app.status = "GENERATED";
      }
      return HttpResponse.json({ application: app, model: "qwen3:1.7b", truncated: { job_description: false, resume: false } });
    }),
  ];
}
