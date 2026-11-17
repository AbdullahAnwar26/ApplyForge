/**
 * A small in-memory stand-in for the profile, skill and resume endpoints, shaped
 * like the real serializers and following their main validation rules. Fictional
 * data only. Every request is recorded in `calls`.
 */

import { delay, http, HttpResponse, type HttpHandler } from "msw";

import type { ApplicationDetail, ApplicationEvent, CollectorRun, Company, JobDetail, Profile, Resume, Skill, SkillCategoryOption } from "../api/types";
import { applicationHandlers, type GenerationMode } from "./mockApplications";
import { collectorHandlers, type CollectorMode } from "./mockCollector";
import { jobHandlers } from "./mockJobs";
import { makeProfile, TEST_TOKEN } from "./renderApp";
import { API } from "./server";

export const CATEGORIES: SkillCategoryOption[] = [
  { value: "SKILL", label: "Skill" },
  { value: "ROLE", label: "Role" },
  { value: "TECHNOLOGY", label: "Technology" },
  { value: "FRAMEWORK", label: "Framework" },
  { value: "OTHER", label: "Other" },
];

export interface Call {
  method: string;
  path: string;
  body?: unknown;
  form?: Record<string, string | { name: string; size: number }>;
}

export interface MockState {
  profiles: Profile[];
  skills: Skill[];
  resumes: Resume[];
  texts: Record<number, string>;
  /** Jobs in list order (newest first, as the backend orders them). */
  jobs: JobDetail[];
  companies: Company[];
  applications: ApplicationDetail[];
  events: Record<number, ApplicationEvent[]>;
  runs: CollectorRun[];
}

const NOW = "2026-03-01T10:00:00Z";
let sequence = 1000;
const nextId = () => ++sequence;

export function makeResume(id: number, profile: number, overrides: Partial<Resume> = {}): Resume {
  return {
    id,
    profile,
    name: `Resume ${id}`,
    file_name: `resume_${id}.pdf`,
    file_type: "pdf",
    resume_type: null,
    is_active: true,
    is_default: false,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

/**
 * Minimal multipart/form-data parser for tests: request.formData() cannot parse
 * the body that jsdom's XMLHttpRequest produces. Files become {name, size}.
 */
export async function parseMultipart(request: Request): Promise<Record<string, string | { name: string; size: number }>> {
  const boundary = /boundary=(.+)$/.exec(request.headers.get("content-type") ?? "")?.[1];
  if (!boundary) return {};
  const body = new TextDecoder("latin1").decode(await request.arrayBuffer());
  const fields: Record<string, string | { name: string; size: number }> = {};
  for (const part of body.split(`--${boundary}`)) {
    const split = part.indexOf("\r\n\r\n");
    if (split < 0) continue;
    const headers = part.slice(0, split);
    const value = part.slice(split + 4).replace(/\r\n$/, "");
    const name = /name="([^"]*)"/.exec(headers)?.[1];
    if (!name) continue;
    const filename = /filename="([^"]*)"/.exec(headers)?.[1];
    fields[name] = filename !== undefined ? { name: filename, size: value.length } : value;
  }
  return fields;
}

function unauthorized(request: Request) {
  return request.headers.get("authorization") !== `Bearer ${TEST_TOKEN}`
    ? HttpResponse.json({ detail: "Invalid token." }, { status: 401 })
    : null;
}

function required(body: Record<string, unknown>, fields: string[]) {
  const errors: Record<string, string[]> = {};
  for (const field of fields) {
    const value = body[field];
    if (value === undefined) errors[field] = ["This field is required."];
    else if (typeof value === "string" && !value.trim()) errors[field] = ["This field may not be blank."];
  }
  return errors;
}

export function createMockBackend(seed: Partial<MockState> = {}) {
  // A deep copy: each backend owns its data, so changes made during one test (new
  // rows, re-screened jobs) can never leak into another test through shared fixtures.
  const initial = structuredClone(seed);
  const state: MockState = {
    profiles: initial.profiles ?? [],
    skills: initial.skills ?? [],
    resumes: initial.resumes ?? [],
    texts: initial.texts ?? {},
    jobs: initial.jobs ?? [],
    companies: initial.companies ?? [],
    applications: initial.applications ?? [],
    events: initial.events ?? {},
    runs: initial.runs ?? [],
  };
  const calls: Call[] = [];
  /** Optional per-test delay before each mutating response (ms). */
  const options: {
    writeDelay: number;
    generation: GenerationMode;
    generationDelay: number;
    collector: CollectorMode;
    collectorDelay: number;
  } = { writeDelay: 0, generation: "ok", generationDelay: 0, collector: "succeeded", collectorDelay: 0 };

  const record = (request: Request, extra: Partial<Call> = {}) =>
    calls.push({ method: request.method, path: new URL(request.url).pathname.replace("/api/v1/", ""), ...extra });

  const handlers: HttpHandler[] = [
    http.get(`${API}/profiles/`, ({ request }) => {
      record(request);
      return unauthorized(request) ?? HttpResponse.json(state.profiles);
    }),
    http.post(`${API}/profiles/`, async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      record(request, { body });
      await delay(options.writeDelay);
      const errors = required(body, ["name", "target_roles"]);
      if (Object.keys(errors).length) return HttpResponse.json(errors, { status: 400 });
      const profile = makeProfile(nextId(), String(body.name), {
        experience_years: 1,
        max_acceptable_experience_years: 9,
        ...(body as Partial<Profile>),
      });
      state.profiles.push(profile);
      return HttpResponse.json(profile, { status: 201 });
    }),
    http.patch(`${API}/profiles/:id/`, async ({ request, params }) => {
      const body = (await request.json()) as Partial<Profile>;
      record(request, { body });
      await delay(options.writeDelay);
      const profile = state.profiles.find((p) => p.id === Number(params.id));
      if (!profile) return HttpResponse.json({ detail: "No Profile matches the given query." }, { status: 404 });
      Object.assign(profile, body, { updated_at: "2026-03-02T09:30:00Z" });
      return HttpResponse.json(profile);
    }),

    http.get(`${API}/skill-categories/`, ({ request }) => {
      record(request);
      return unauthorized(request) ?? HttpResponse.json(CATEGORIES);
    }),
    http.get(`${API}/skills/`, ({ request }) => {
      record(request);
      const profile = Number(new URL(request.url).searchParams.get("profile"));
      return HttpResponse.json(state.skills.filter((s) => s.profile === profile));
    }),
    http.post(`${API}/skills/`, async ({ request }) => {
      const body = (await request.json()) as Skill;
      record(request, { body });
      await delay(options.writeDelay);
      if (state.skills.some((s) => s.profile === body.profile && s.name === body.name)) {
        return HttpResponse.json({ non_field_errors: ["The fields name, profile must make a unique set."] }, { status: 400 });
      }
      const skill = { ...body, id: nextId() };
      state.skills.push(skill);
      return HttpResponse.json(skill, { status: 201 });
    }),
    http.delete(`${API}/skills/:id/`, ({ request, params }) => {
      record(request);
      state.skills = state.skills.filter((s) => s.id !== Number(params.id));
      return new HttpResponse(null, { status: 204 });
    }),

    http.get(`${API}/resumes/`, ({ request }) => {
      record(request);
      const profile = Number(new URL(request.url).searchParams.get("profile"));
      return HttpResponse.json(state.resumes.filter((r) => r.profile === profile));
    }),
    http.post(`${API}/resumes/`, async ({ request }) => {
      const form = await parseMultipart(request);
      record(request, { form });
      await delay(options.writeDelay);
      const file = form.file;
      if (!file || typeof file === "string") return HttpResponse.json({ file: ["A PDF or DOCX resume file is required."] }, { status: 400 });
      if (file.name.includes("scanned")) {
        return HttpResponse.json(
          { file: ["No text could be extracted. Scanned or image-only resumes are not supported."] },
          { status: 400 },
        );
      }
      const profile = Number(form.profile);
      const isDefault = form.is_default === "true";
      if (isDefault) state.resumes.forEach((r) => r.profile === profile && (r.is_default = false));
      const resume = makeResume(nextId(), profile, {
        name: typeof form.name === "string" ? form.name : file.name.replace(/\.[^.]+$/, ""),
        file_name: file.name,
        file_type: file.name.split(".").pop()!.toLowerCase(),
        resume_type: typeof form.resume_type === "string" ? form.resume_type : null,
        is_default: isDefault,
      });
      state.resumes.push(resume);
      return HttpResponse.json(resume, { status: 201 });
    }),
    http.patch(`${API}/resumes/:id/`, async ({ request, params }) => {
      const body = (await request.json()) as Partial<Resume>;
      record(request, { body });
      const resume = state.resumes.find((r) => r.id === Number(params.id));
      if (!resume) return HttpResponse.json({ detail: "Not found." }, { status: 404 });
      if (body.is_default) state.resumes.forEach((r) => r.profile === resume.profile && (r.is_default = false));
      Object.assign(resume, body);
      return HttpResponse.json(resume);
    }),
    http.delete(`${API}/resumes/:id/`, ({ request, params }) => {
      record(request);
      state.resumes = state.resumes.filter((r) => r.id !== Number(params.id));
      return new HttpResponse(null, { status: 204 });
    }),
    http.get(`${API}/resumes/:id/text/`, ({ request, params }) => {
      record(request);
      const id = Number(params.id);
      const text = state.texts[id];
      if (text === undefined) return HttpResponse.json({ detail: "The resume file is missing from storage." }, { status: 409 });
      return HttpResponse.json({ id, text, characters: text.length });
    }),
  ];

  handlers.push(...jobHandlers(state, record, options, nextId));
  handlers.push(...applicationHandlers(state, record, options, nextId));
  handlers.push(...collectorHandlers(state, record, options, nextId));

  const writes = (method: string, prefix: string) => calls.filter((c) => c.method === method && c.path.startsWith(prefix));

  return { state, calls, handlers, options, writes };
}
