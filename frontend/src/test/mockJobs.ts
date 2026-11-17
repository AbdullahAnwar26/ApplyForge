/**
 * Fake jobs and companies endpoints for tests, following JobViewSet: supported
 * filters only (invalid values -> 400), optional pagination (bad page -> 404),
 * PATCH status only, explicit screening, idempotent ingest. Screening here is a
 * simplified stand-in (skill name contained in the description), enough to test
 * the UI - not the backend's rules.
 */

import { delay, http, HttpResponse, type HttpHandler } from "msw";

import type { Company, JobDetail, JobSummary, ScreeningOutcome } from "../api/types";
import { JOB_SOURCES, JOB_STATUSES, SCREENING_OUTCOMES, SCREENING_REASONS } from "../api/types";
import type { Call, MockState } from "./mockBackend";
import { API } from "./server";

const NOW = "2026-03-01T10:00:00Z";

export function makeJob(id: number, overrides: Partial<JobDetail> = {}): JobDetail {
  return {
    id,
    title: `Backend Developer ${id}`,
    company: { id: 1, name: "Acme Fictional Labs" },
    source: "LINKEDIN",
    source_url: null,
    location: "Remote",
    work_mode: "Remote",
    posted_date: null,
    discovered_date: NOW,
    required_experience_text: "2-4 years",
    parsed_min_experience: 2,
    parsed_max_experience: 4,
    status: "DISCOVERED",
    screening_outcome: null,
    screening_reason: null,
    screened_at: null,
    matched_keywords: [],
    created_at: NOW,
    updated_at: NOW,
    full_jd: "We're hiring a Backend Developer. Python and Django. 2-4 years of experience.",
    screening_details: {},
    contacts: [],
    ...overrides,
  };
}

export function toSummary(job: JobDetail): JobSummary {
  const summary: Partial<JobDetail> = { ...job };
  delete summary.full_jd;
  delete summary.screening_details;
  delete summary.contacts;
  return summary as JobSummary;
}

function badChoice(name: string, allowed: readonly string[]) {
  return HttpResponse.json({ [name]: [`Must be one of: ${allowed.join(", ")}.`] }, { status: 400 });
}

export function jobHandlers(
  state: MockState,
  record: (request: Request, extra?: Partial<Call>) => void,
  options: { writeDelay: number },
  nextId: () => number,
): HttpHandler[] {
  function screen(job: JobDetail, profileId: number | undefined) {
    const profile = profileId !== undefined ? state.profiles.find((p) => p.id === profileId) : state.profiles.length === 1 ? state.profiles[0] : undefined;
    if (!profile) return { profile: ["Multiple profiles exist. Specify a profile."] };
    const skills = state.skills.filter((s) => s.profile === profile.id);
    if (!skills.length) return { profile: [`Profile '${profile.name}' has no skills. Add skills before screening.`] };
    const matched = skills.filter((s) => job.full_jd.toLowerCase().includes(s.name.toLowerCase())).map((s) => s.name);
    const outcome: ScreeningOutcome = matched.length ? "SHORTLISTED" : "REJECTED";
    Object.assign(job, {
      screening_outcome: outcome,
      screening_reason: matched.length ? "KEYWORD_MATCH" : "NO_KEYWORD_MATCH",
      screened_at: "2026-03-02T08:00:00Z",
      matched_keywords: matched.sort(),
      screening_details: {
        rules_version: 1,
        profile_id: profile.id,
        matched_keywords: matched,
        experience: { min: job.parsed_min_experience, max: job.parsed_max_experience, source: "field", matches: [] },
        flags: matched.length ? [] : ["NO_KEYWORD_MATCH"],
        duplicate_candidates: [],
      },
    });
    return null;
  }

  const find = (id: unknown) => state.jobs.find((j) => j.id === Number(id));

  return [
    http.get(`${API}/jobs/`, ({ request }) => {
      record(request);
      const q = new URL(request.url).searchParams;
      let jobs = state.jobs;
      const status = q.get("status");
      if (status) {
        if (!(JOB_STATUSES as readonly string[]).includes(status)) return badChoice("status", JOB_STATUSES);
        jobs = jobs.filter((j) => j.status === status);
      }
      const outcome = q.get("screening_outcome");
      if (outcome) {
        const allowed = [...SCREENING_OUTCOMES, "UNSCREENED"];
        if (!allowed.includes(outcome)) return badChoice("screening_outcome", allowed);
        jobs = jobs.filter((j) => (outcome === "UNSCREENED" ? j.screening_outcome === null : j.screening_outcome === outcome));
      }
      const reason = q.get("screening_reason");
      if (reason) {
        if (!(SCREENING_REASONS as readonly string[]).includes(reason)) return badChoice("screening_reason", SCREENING_REASONS);
        jobs = jobs.filter((j) => j.screening_reason === reason);
      }
      const source = q.get("source");
      if (source) {
        if (!(JOB_SOURCES as readonly string[]).includes(source)) return badChoice("source", JOB_SOURCES);
        jobs = jobs.filter((j) => j.source === source);
      }
      const company = q.get("company");
      if (company) jobs = jobs.filter((j) => j.company?.id === Number(company));
      const profile = q.get("profile");
      if (profile) jobs = jobs.filter((j) => j.screening_details.profile_id === Number(profile));

      const items = jobs.map(toSummary);
      if (!q.has("page") && !q.has("page_size")) return HttpResponse.json(items);
      const size = Math.min(200, Number(q.get("page_size")) || 50);
      const page = Number(q.get("page") ?? "1");
      const pages = Math.max(1, Math.ceil(items.length / size));
      if (!Number.isInteger(page) || page < 1 || page > pages) return HttpResponse.json({ detail: "Invalid page." }, { status: 404 });
      const base = `${API}/jobs/?page_size=${size}`;
      return HttpResponse.json({
        count: items.length,
        next: page < pages ? `${base}&page=${page + 1}` : null,
        previous: page > 1 ? `${base}&page=${page - 1}` : null,
        results: items.slice((page - 1) * size, page * size),
      });
    }),
    http.get(`${API}/jobs/:id/`, ({ request, params }) => {
      record(request);
      const job = find(params.id);
      return job ? HttpResponse.json(job) : HttpResponse.json({ detail: "No JobOpportunity matches the given query." }, { status: 404 });
    }),
    http.patch(`${API}/jobs/:id/`, async ({ request, params }) => {
      const body = (await request.json()) as Record<string, unknown>;
      record(request, { body });
      await delay(options.writeDelay);
      const job = find(params.id);
      if (!job) return HttpResponse.json({ detail: "Not found." }, { status: 404 });
      const unexpected = Object.keys(body).filter((k) => k !== "status");
      if (unexpected.length) return HttpResponse.json(Object.fromEntries(unexpected.map((k) => [k, ["This field is read-only."]])), { status: 400 });
      if (!(JOB_STATUSES as readonly string[]).includes(String(body.status))) {
        return HttpResponse.json({ status: [`"${String(body.status)}" is not a valid choice.`] }, { status: 400 });
      }
      job.status = body.status as JobDetail["status"];
      return HttpResponse.json(job);
    }),
    http.post(`${API}/jobs/:id/screen/`, async ({ request, params }) => {
      const body = (await request.json()) as { profile?: number };
      record(request, { body });
      await delay(options.writeDelay);
      const job = find(params.id);
      if (!job) return HttpResponse.json({ detail: "Not found." }, { status: 404 });
      const error = screen(job, body.profile);
      return error ? HttpResponse.json(error, { status: 400 }) : HttpResponse.json(job);
    }),
    http.post(`${API}/jobs/screen/`, async ({ request }) => {
      const body = (await request.json()) as { profile?: number };
      record(request, { body });
      await delay(options.writeDelay);
      const outcomes = { SHORTLISTED: 0, REJECTED: 0, NEEDS_REVIEW: 0 };
      for (const job of state.jobs) {
        const error = screen(job, body.profile);
        if (error) return HttpResponse.json(error, { status: 400 });
        outcomes[job.screening_outcome!] += 1;
      }
      return HttpResponse.json({ profile: body.profile, screened: state.jobs.length, outcomes });
    }),
    http.post(`${API}/jobs/ingest/`, async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      record(request, { body });
      await delay(options.writeDelay);
      const errors: Record<string, string[]> = {};
      for (const key of ["title", "full_jd"]) if (!String(body[key] ?? "").trim()) errors[key] = ["This field is required."];
      if (Object.keys(errors).length) return HttpResponse.json(errors, { status: 400 });
      const byUrl = body.source_url ? state.jobs.find((j) => j.source_url === body.source_url) : undefined;
      const byText = state.jobs.find((j) => j.full_jd.trim().toLowerCase() === String(body.full_jd).trim().toLowerCase());
      const existing = byUrl ?? byText;
      if (existing) return HttpResponse.json({ created: false, matched_by: byUrl ? "source_url" : "jd_fingerprint", job: existing });

      let company: Company | undefined;
      if (body.company_name) {
        company = state.companies.find((c) => c.name.toLowerCase() === String(body.company_name).toLowerCase());
        if (!company) {
          company = { id: nextId(), name: String(body.company_name), website: (body.company_website as string) ?? null, created_at: NOW, updated_at: NOW };
          state.companies.push(company);
        }
      }
      const contacts = ((body.contacts as Record<string, string>[] | undefined) ?? []).map((c) => ({
        id: nextId(),
        name: c.name ?? null,
        email: c.email ?? null,
        phone: c.phone ?? null,
        whatsapp: c.whatsapp ?? null,
        company: company?.id ?? null,
        created_at: NOW,
        updated_at: NOW,
      }));
      const job = makeJob(nextId(), {
        title: String(body.title),
        full_jd: String(body.full_jd),
        company: company ? { id: company.id, name: company.name } : null,
        source: (body.source as JobDetail["source"]) ?? "OTHER",
        source_url: (body.source_url as string) ?? null,
        location: (body.location as string) ?? null,
        work_mode: (body.work_mode as string) ?? null,
        required_experience_text: (body.required_experience_text as string) ?? null,
        parsed_min_experience: null,
        parsed_max_experience: null,
        posted_date: (body.posted_date as string) ?? null,
        contacts,
      });
      const error = screen(job, body.profile as number | undefined);
      if (error) return HttpResponse.json(error, { status: 400 });
      state.jobs.unshift(job);
      return HttpResponse.json({ created: true, matched_by: null, job }, { status: 201 });
    }),
    http.get(`${API}/companies/`, ({ request }) => {
      record(request);
      return HttpResponse.json(state.companies);
    }),
    http.get(`${API}/companies/:id/`, ({ request, params }) => {
      record(request);
      const company = state.companies.find((c) => c.id === Number(params.id));
      return company ? HttpResponse.json(company) : HttpResponse.json({ detail: "Not found." }, { status: 404 });
    }),
  ];
}
