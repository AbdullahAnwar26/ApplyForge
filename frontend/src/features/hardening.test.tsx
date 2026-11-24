/**
 * Regression tests for the post-integration hardening:
 *   1. generation outcomes that cannot be confirmed are shown as unknown;
 *   2. job status changes / screening refresh job AND application queries;
 *   3. a finished collector run refreshes the companies (filter) query;
 *   5. internal details (URLs, model, exception names) stay out of the main UI text.
 */

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { ApplicationDetail, Contact, Skill } from "../api/types";
import { makeApplication } from "../test/mockApplications";
import { createMockBackend, makeResume } from "../test/mockBackend";
import { makeItem, makeRun } from "../test/mockCollector";
import { makeJob } from "../test/mockJobs";
import { makeProfile, renderApp } from "../test/renderApp";
import { API, server } from "../test/server";

const RIYA = makeProfile(1, "Riya Fictional");
const SKILLS: Skill[] = [{ id: 11, name: "Python", category: "TECHNOLOGY", profile: 1 }];
const RECRUITER: Contact = { id: 31, name: "Fictional Recruiter", email: "talent@acme.example", phone: null, whatsapp: null, company: 1, created_at: "", updated_at: "" };
const SAVED = { generated_subject: "My saved subject", generated_body: "My saved body.", generated_whatsapp_message: null };

function app(overrides: Partial<ApplicationDetail> = {}) {
  return makeApplication(50, {
    contact: 31,
    resume: 5,
    job: { id: 7, title: "Python Backend Developer", company: { id: 1, name: "Acme Fictional Labs" }, status: "SHORTLISTED", screening_outcome: "SHORTLISTED" },
    ...overrides,
  });
}

function setup(initial: Parameters<typeof createMockBackend>[0] = {}, path = "/applications/50") {
  const backend = createMockBackend({
    profiles: [RIYA],
    skills: SKILLS,
    jobs: [makeJob(7, { title: "Python Backend Developer", status: "SHORTLISTED", full_jd: "Python services", contacts: [RECRUITER] })],
    resumes: [makeResume(5, 1, { name: "Backend CV", is_default: true })],
    applications: [app({ status: "GENERATED", generated_at: "2026-03-02T00:00:00Z", ...SAVED })],
    companies: [{ id: 1, name: "Acme Fictional Labs", website: null, created_at: "", updated_at: "" }],
    ...initial,
  });
  server.use(...backend.handlers);
  const utils = renderApp({ path });
  return { backend, user: userEvent.setup(), ...utils };
}

const gets = (backend: ReturnType<typeof createMockBackend>, path: string) =>
  backend.calls.filter((c) => c.method === "GET" && c.path === path).length;
const draftPanel = () => screen.getByRole("region", { name: "Draft" });
const nav = () => within(screen.getByRole("navigation", { name: "Main" }));

async function generateAgain(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Generate a new draft" }));
  await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Generate a new draft" }));
}

describe("1. uncertain generation outcomes", () => {
  it("a gateway 504 (not from ApplyForge) is unknown: no 'unchanged' claim, and a refresh is offered", async () => {
    const { backend, user } = setup();
    server.use(
      http.post(`${API}/applications/:id/generate/`, () =>
        new HttpResponse("<html><body>504 Gateway Time-out</body></html>", { status: 504, headers: { "Content-Type": "text/html" } }),
      ),
    );
    await generateAgain(user);
    const alert = await within(draftPanel()).findByRole("alert");
    expect(alert).toHaveTextContent("No confirmed result from the backend");
    expect(alert).toHaveTextContent("Generation may still finish on the backend");
    expect(alert).not.toHaveTextContent("kept your existing drafts");
    expect(alert).not.toHaveTextContent("took too long"); // not mistaken for the model's own timeout
    expect(alert).not.toHaveTextContent("Gateway Time-out"); // raw HTML is never shown

    // Simulate the backend finishing in the meantime; "Refresh application" picks it up.
    Object.assign(backend.state.applications[0]!, { generated_subject: "Generated later", generated_at: "2026-03-03T10:00:00Z" });
    const before = gets(backend, "applications/50/");
    await user.click(within(alert).getByRole("button", { name: "Refresh application" }));
    await waitFor(() => expect(gets(backend, "applications/50/")).toBeGreaterThan(before));
    await waitFor(() => expect(within(draftPanel()).getByLabelText(/^Subject/)).toHaveValue("Generated later"));
  });

  it("a JSON 502 without the application marker (e.g. a proxy) is unknown too", async () => {
    const { user } = setup();
    server.use(http.post(`${API}/applications/:id/generate/`, () => HttpResponse.json({ detail: "Bad Gateway" }, { status: 502 })));
    await generateAgain(user);
    const alert = await within(draftPanel()).findByRole("alert");
    expect(alert).toHaveTextContent("No confirmed result from the backend");
    expect(alert).not.toHaveTextContent("kept your existing drafts");
  });

  it("a dropped connection is unknown", async () => {
    const { user } = setup();
    server.use(http.post(`${API}/applications/:id/generate/`, () => HttpResponse.error()));
    await generateAgain(user);
    const alert = await within(draftPanel()).findByRole("alert");
    expect(alert).toHaveTextContent("No confirmed result from the backend");
    expect(within(alert).getByRole("button", { name: "Refresh application" })).toBeInTheDocument();
  });

  it("the backend's own 504 is definite: drafts and status confirmed kept", async () => {
    const { backend, user } = setup();
    backend.options.generation = 504;
    await generateAgain(user);
    const alert = await within(draftPanel()).findByRole("alert");
    expect(alert).toHaveTextContent("The local model took too long");
    expect(alert).toHaveTextContent("The backend kept your existing drafts and the status unchanged.");
    expect(within(alert).queryByRole("button", { name: "Refresh application" })).not.toBeInTheDocument();
  });
});

describe("5. internal details stay out of the main text", () => {
  it("a 503 shows the explanation; the Ollama URL and OS error only in cleaned technical details", async () => {
    const { backend, user } = setup();
    backend.options.generation = 503;
    await generateAgain(user);
    const alert = await within(draftPanel()).findByRole("alert");
    const details = within(alert).getByText("Technical details").closest("details")!;
    expect(details).not.toHaveAttribute("open");
    expect(details).toHaveTextContent("Cannot reach Ollama at [address]");
    expect(alert).not.toHaveTextContent("http://localhost:11434");
    const visible = alert.textContent!.replace(details.textContent!, "");
    expect(visible).toContain("The local model is not available");
    expect(visible).not.toMatch(/Ollama at|qwen3|Connection refused/);

    // The application's stored last_error is shown the same way.
    const overview = screen.getByRole("region", { name: "Overview" });
    await waitFor(() => expect(overview).toHaveTextContent("The last draft generation failed."));
    expect(overview).not.toHaveTextContent("http://localhost:11434");
  });

  it("collector run detail replaces exception text, keeping it in technical details", async () => {
    setup({ runs: [makeRun(6, { status: "PARTIAL", error: "OperationalError: database is locked" })] }, "/collector/runs/6");
    const progress = await screen.findByRole("region", { name: "Progress" });
    expect(await within(progress).findByText(/An internal error occurred; the details are in the backend log\./)).toBeInTheDocument();
    const details = within(progress).getByText("Technical details").closest("details")!;
    expect(details).toHaveTextContent("OperationalError: database is locked");
  });

  it("collector messages written for users stay visible, with links removed", async () => {
    setup({ runs: [makeRun(6, { status: "FAILED", counts: {}, error: "Search page https://www.linkedin.com/search/results/content/?keywords=python did not load." })] }, "/collector/runs/6");
    const progress = await screen.findByRole("region", { name: "Progress" });
    expect(await within(progress).findByText("Search page [address] did not load.")).toBeInTheDocument();
    expect(progress).not.toHaveTextContent("linkedin.com");
    expect(within(progress).queryByText("Technical details")).not.toBeInTheDocument();
  });

  it("per-post exception reasons are replaced in the post list", async () => {
    setup(
      {
        runs: [makeRun(80, { status: "PARTIAL", counts: { found: 1, created: 0, existing: 0, skipped: 0, invalid: 0, failed: 1, out_of_range: 0 } })],
        runItems: { 80: [makeItem(1, { status: "failed", title: "Data Engineer", job: null, reason: "IntegrityError: UNIQUE constraint failed" })] },
      },
      "/collector/runs/80",
    );
    const items = await screen.findByRole("region", { name: "Posts examined" });
    expect(await within(items).findByText("Data Engineer")).toBeInTheDocument();
    expect(items).toHaveTextContent("An internal error occurred; the details are in the backend log.");
    expect(items).not.toHaveTextContent("IntegrityError");
  });
});

describe("3. a finished collection refreshes companies", () => {
  it("the Jobs company filter reloads after a collection", async () => {
    const { backend, user } = setup({}, "/jobs");
    await screen.findByRole("table", { name: "Jobs" });
    await waitFor(() => expect(gets(backend, "companies/")).toBeGreaterThan(0));
    const before = gets(backend, "companies/");
    await user.click(nav().getByRole("link", { name: "Job Automation" }));
    const query = await screen.findByLabelText(/Search for/);
    await user.clear(query);
    await user.type(query, "Python Developer");
    await user.click(screen.getByRole("button", { name: "Start collecting" }));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("checkbox"));
    await user.click(within(dialog).getByRole("button", { name: "Start collecting" }));
    await screen.findByText(/Done: \d+ job\(s\) ready to apply/, undefined, { timeout: 5000 });
    await user.click(nav().getByRole("link", { name: "Jobs" }));
    await screen.findByRole("table", { name: "Jobs" });
    await waitFor(() => expect(gets(backend, "companies/")).toBeGreaterThan(before));
  });
});

describe("2. job changes refresh application data", () => {
  it("screening a job makes the application page refetch", async () => {
    const { backend, user } = setup();
    await screen.findByRole("heading", { level: 1, name: "Python Backend Developer" });
    const before = gets(backend, "applications/50/");
    await user.click(within(screen.getByRole("region", { name: "Overview" })).getByRole("link", { name: "Python Backend Developer" }));
    await user.click(await screen.findByRole("button", { name: /[Ss]creen with Riya Fictional/ }));
    await screen.findByText(/^Screened: /);
    await user.click(await within(screen.getByRole("region", { name: "Application" })).findByRole("link", { name: "Open application" }));
    await screen.findByRole("region", { name: "Draft" });
    await waitFor(() => expect(gets(backend, "applications/50/")).toBeGreaterThan(before));
  });

  it("changing a job's status makes the application page refetch", async () => {
    const { backend, user } = setup();
    await screen.findByRole("heading", { level: 1, name: "Python Backend Developer" });
    const before = gets(backend, "applications/50/");
    await user.click(within(screen.getByRole("region", { name: "Overview" })).getByRole("link", { name: "Python Backend Developer" }));
    await user.selectOptions(await screen.findByLabelText("Your status"), "Applied");
    await user.click(screen.getByRole("button", { name: "Save status" }));
    await screen.findByText("Status: Applied");
    await user.click(await within(screen.getByRole("region", { name: "Application" })).findByRole("link", { name: "Open application" }));
    await screen.findByRole("region", { name: "Draft" });
    await waitFor(() => expect(gets(backend, "applications/50/")).toBeGreaterThan(before));
  });

  it("re-screening all jobs makes the applications list refetch", async () => {
    const { backend, user } = setup({}, "/applications");
    await screen.findByRole("table", { name: "Applications" });
    const before = gets(backend, "applications/");
    await user.click(nav().getByRole("link", { name: "Jobs" }));
    await user.click(await screen.findByRole("button", { name: "Re-screen all jobs" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Re-screen all" }));
    await screen.findByText(/^Screened 1 job:/);
    await user.click(nav().getByRole("link", { name: "Applications" }));
    await screen.findByRole("table", { name: "Applications" });
    await waitFor(() => expect(gets(backend, "applications/")).toBeGreaterThan(before));
  });
});

