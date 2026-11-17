import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { Company, JobDetail, Skill } from "../../api/types";
import { ACTIVE_PROFILE_STORAGE_KEY } from "../../profile/ActiveProfileProvider";
import { createMockBackend } from "../../test/mockBackend";
import { makeJob } from "../../test/mockJobs";
import { makeProfile, renderApp } from "../../test/renderApp";
import { API, server } from "../../test/server";
import { readParams, toApiFilters, writeParams } from "./filters";

const RIYA = makeProfile(1, "Riya Fictional");
const SAM = makeProfile(2, "Sam Example");
const ACME: Company = { id: 1, name: "Acme Fictional Labs", website: null, created_at: "", updated_at: "" };
const GLOBEX: Company = { id: 2, name: "Globex Example", website: null, created_at: "", updated_at: "" };
const SKILLS: Skill[] = [
  { id: 11, name: "Python", category: "TECHNOLOGY", profile: 1 },
  { id: 12, name: "Django", category: "FRAMEWORK", profile: 1 },
];

function manyJobs(count: number): JobDetail[] {
  return Array.from({ length: count }, (_, i) => makeJob(count - i, { title: `Fictional Role ${count - i}` }));
}

function setup(initial: Parameters<typeof createMockBackend>[0] = {}, path = "/jobs") {
  const backend = createMockBackend({ profiles: [RIYA], skills: SKILLS, companies: [ACME, GLOBEX], ...initial });
  server.use(...backend.handlers);
  const utils = renderApp({ path });
  return { backend, user: userEvent.setup(), ...utils };
}

const jobRequests = (backend: ReturnType<typeof createMockBackend>) =>
  backend.calls.filter((c) => c.method === "GET" && c.path === "jobs/");
const lastJobQuery = (backend: ReturnType<typeof createMockBackend>) => {
  const all = backend.calls.filter((c) => c.method === "GET" && c.path === "jobs/");
  return all.at(-1);
};

describe("job list", () => {
  it("shows the first page with screening and status badges", async () => {
    const { backend } = setup({
      jobs: [
        makeJob(7, {
          title: "Python Backend Developer",
          screening_outcome: "NEEDS_REVIEW",
          screening_reason: "EXPERIENCE_UNKNOWN",
          status: "SHORTLISTED",
          matched_keywords: ["Django", "PostgreSQL", "Python", "REST APIs"],
          required_experience_text: null,
        }),
        makeJob(6, { title: "Data Engineer", company: null, location: null, work_mode: null }),
      ],
    });
    const table = await screen.findByRole("table", { name: "Jobs" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);

    expect(within(rows[0]!).getByRole("link", { name: "Python Backend Developer" })).toHaveAttribute("href", "/jobs/7");
    expect(rows[0]).toHaveTextContent("Acme Fictional Labs · Remote · Remote");
    expect(within(rows[0]!).getByText("Needs review")).toHaveClass("badge--warning");
    expect(rows[0]).toHaveTextContent("Experience unknown");
    expect(within(rows[0]!).getByText("Shortlisted")).toHaveClass("badge--success");
    expect(rows[0]).toHaveTextContent("Django, PostgreSQL, Python +1");
    expect(rows[1]).toHaveTextContent("Company not given");
    expect(within(rows[1]!).getByText("Unscreened")).toBeInTheDocument();

    expect(screen.getByText("2 jobs")).toBeInTheDocument();
    expect(lastJobQuery(backend)).toBeDefined();
  });

  it("requests pages from the backend and keeps the page in the URL", async () => {
    const seen: URLSearchParams[] = [];
    const { user, backend } = setup({ jobs: manyJobs(30) });
    server.events.on("request:start", ({ request }) => {
      if (new URL(request.url).pathname === "/api/v1/jobs/") seen.push(new URL(request.url).searchParams);
    });
    expect(await screen.findByText("1–25 of 30")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Fictional Role 30" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("26–30 of 30")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Fictional Role 5" })).toBeInTheDocument();
    expect(seen.at(-1)?.get("page")).toBe("2");
    expect(seen.at(-1)?.get("page_size")).toBe("25");
    expect(jobRequests(backend).length).toBeGreaterThanOrEqual(2);
    server.events.removeAllListeners();
  });

  it("sends only supported filters and returns to page 1 when a filter changes", async () => {
    const seen: URLSearchParams[] = [];
    server.events.on("request:start", ({ request }) => {
      if (new URL(request.url).pathname === "/api/v1/jobs/") seen.push(new URL(request.url).searchParams);
    });
    const { user } = setup({ jobs: manyJobs(30) }, "/jobs?page=2");
    expect(await screen.findByText("26–30 of 30")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Screening"), "Unscreened");
    await waitFor(() => expect(seen.at(-1)?.get("screening_outcome")).toBe("UNSCREENED"));
    expect(seen.at(-1)?.get("page")).toBe("1");

    await user.selectOptions(screen.getByLabelText("Reason"), "No keyword match");
    await user.selectOptions(screen.getByLabelText("Your status"), "Skipped");
    await user.selectOptions(screen.getByLabelText("Source"), "LinkedIn");
    await user.selectOptions(screen.getByLabelText("Company"), "Globex Example");
    await user.click(screen.getByRole("checkbox", { name: "Only screened for Riya Fictional" }));

    await waitFor(() =>
      expect(Object.fromEntries(seen.at(-1)!)).toEqual({
        screening_outcome: "UNSCREENED",
        screening_reason: "NO_KEYWORD_MATCH",
        status: "SKIPPED",
        source: "LINKEDIN",
        company: "2",
        profile: "1",
        page: "1",
        page_size: "25",
      }),
    );
    expect(await screen.findByText("No jobs match these filters")).toBeInTheDocument();

    await user.click(screen.getAllByRole("button", { name: "Clear filters" })[0]!);
    await waitFor(() => expect(Object.fromEntries(seen.at(-1)!)).toEqual({ page: "1", page_size: "25" }));
    server.events.removeAllListeners();
  });

  it("restores filters from the URL and ignores invalid values", async () => {
    const seen: URLSearchParams[] = [];
    server.events.on("request:start", ({ request }) => {
      if (new URL(request.url).pathname === "/api/v1/jobs/") seen.push(new URL(request.url).searchParams);
    });
    setup({ jobs: [] }, "/jobs?outcome=REJECTED&status=BOGUS&company=abc&source=LINKEDIN");
    await screen.findByText("No jobs match these filters");
    expect(Object.fromEntries(seen.at(-1)!)).toEqual({ screening_outcome: "REJECTED", source: "LINKEDIN", page: "1", page_size: "25" });
    expect(screen.getByLabelText("Screening")).toHaveValue("REJECTED");
    expect(screen.getByLabelText("Your status")).toHaveValue("");
    server.events.removeAllListeners();
  });

  it("explains an empty list", async () => {
    setup({ jobs: [] });
    expect(await screen.findByText("No jobs yet")).toBeInTheDocument();
    expect(screen.getByText(/after a collector run, or add one manually/)).toBeInTheDocument();
  });

  it("shows loading, then a retryable error", async () => {
    let fail = true;
    const backend = createMockBackend({ profiles: [RIYA], skills: SKILLS });
    server.use(...backend.handlers);
    server.use(
      http.get(`${API}/jobs/`, async () => {
        await delay(50);
        return fail
          ? HttpResponse.json({ detail: "Database is locked." }, { status: 400 })
          : HttpResponse.json({ count: 0, next: null, previous: null, results: [] });
      }),
    );
    renderApp({ path: "/jobs" });
    expect(await screen.findByRole("status", { name: "Loading jobs" })).toBeInTheDocument();
    expect(await screen.findByText("Could not load jobs")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Database is locked.");
    fail = false;
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("No jobs yet")).toBeInTheDocument();
  });

  it("handles a page that no longer exists", async () => {
    const { user } = setup({ jobs: manyJobs(3) }, "/jobs?page=4");
    expect(await screen.findByText("This page no longer exists")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Go to the first page" }));
    expect(await screen.findByText("1–3 of 3")).toBeInTheDocument();
  });

  it("opens a job from the keyboard", async () => {
    const { user } = setup({ jobs: [makeJob(7, { title: "Python Backend Developer" })] });
    const link = await screen.findByRole("link", { name: "Python Backend Developer" });
    link.focus();
    await user.keyboard("{Enter}");
    expect(await screen.findByRole("heading", { level: 1, name: "Python Backend Developer" })).toBeInTheDocument();
  });

  it("never screens on load or when the profile changes", async () => {
    window.localStorage.setItem(ACTIVE_PROFILE_STORAGE_KEY, "1");
    const { backend, user } = setup({ profiles: [RIYA, SAM], jobs: manyJobs(2) }, "/jobs?mine=1");
    await screen.findByText("No jobs match these filters");
    expect(lastJobQuery(backend)).toBeDefined();

    const seen: URLSearchParams[] = [];
    server.events.on("request:start", ({ request }) => {
      if (new URL(request.url).pathname === "/api/v1/jobs/") seen.push(new URL(request.url).searchParams);
    });
    await user.selectOptions(screen.getByLabelText("Profile", { selector: "select.profile-switcher__select" }), "Sam Example");
    // The "screened for" filter follows the active profile; nothing is screened.
    await waitFor(() => expect(seen.at(-1)?.get("profile")).toBe("2"));
    expect(screen.getByRole("checkbox", { name: "Only screened for Sam Example" })).toBeChecked();
    expect(backend.calls.filter((c) => c.method === "POST")).toHaveLength(0);
    server.events.removeAllListeners();
  });
});

describe("re-screen all", () => {
  it("asks first, explains the skills used, then screens and refreshes the list", async () => {
    const { backend, user } = setup({ jobs: [makeJob(1, { full_jd: "Python role" }), makeJob(2, { full_jd: "Rust role" })] });
    await screen.findByRole("table", { name: "Jobs" });
    const before = jobRequests(backend).length;

    await user.click(screen.getByRole("button", { name: "Re-screen all jobs" }));
    const dialog = screen.getByRole("dialog", { name: "Re-screen every job with Riya Fictional?" });
    expect(dialog).toHaveTextContent("Riya Fictional's 2 skills");
    expect(dialog).toHaveTextContent("Your job statuses do not change.");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(backend.writes("POST", "jobs/screen")).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: "Re-screen all jobs" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Re-screen all" }));

    expect(await screen.findByText("Screened 2 jobs: 1 shortlisted, 0 need review, 1 rejected.")).toBeInTheDocument();
    expect(backend.writes("POST", "jobs/screen/")[0]!.body).toEqual({ profile: 1 });
    await waitFor(() => expect(jobRequests(backend).length).toBeGreaterThan(before));
    const table = screen.getByRole("table", { name: "Jobs" });
    expect(await within(table).findByText("Rejected")).toHaveClass("badge--danger");
  });

  it("cannot be submitted twice while running", async () => {
    const { backend, user } = setup({ jobs: [makeJob(1)] });
    backend.options.writeDelay = 200;
    await screen.findByRole("table", { name: "Jobs" });
    await user.click(screen.getByRole("button", { name: "Re-screen all jobs" }));
    const confirm = within(screen.getByRole("dialog")).getByRole("button", { name: "Re-screen all" });
    await user.click(confirm);
    expect(confirm).toBeDisabled();
    await user.click(confirm);
    await screen.findByText(/^Screened 1 job:/);
    expect(backend.writes("POST", "jobs/screen/")).toHaveLength(1);
  });

  it("says when the profile has no skills, and shows the backend's refusal", async () => {
    const { user } = setup({ skills: [], jobs: [makeJob(1)] });
    await screen.findByRole("table", { name: "Jobs" });
    await user.click(screen.getByRole("button", { name: "Re-screen all jobs" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("Riya Fictional has no skills yet");
    await user.click(within(dialog).getByRole("button", { name: "Re-screen all" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("has no skills");
  });
});

describe("add a job manually", () => {
  it("checks the form before sending", async () => {
    const { backend, user } = setup({ jobs: [] });
    await user.click(await screen.findByRole("button", { name: "Add job manually" }));
    const dialog = screen.getByRole("dialog", { name: "Add a job manually" });
    await user.type(within(dialog).getByLabelText("Link to the posting"), "ftp://example.test/job");
    await user.click(within(dialog).getByRole("button", { name: "Add contact" }));
    await user.type(within(dialog).getByLabelText("Name (contact 1)"), "Fictional Recruiter");
    await user.click(within(dialog).getByRole("button", { name: "Add job" }));

    expect(within(dialog).getByLabelText(/^Job title/)).toHaveAccessibleDescription("Enter the job title.");
    expect(within(dialog).getByLabelText(/^Full job description/)).toHaveAccessibleDescription(expect.stringContaining("Paste the full job description"));
    expect(within(dialog).getByLabelText("Link to the posting")).toHaveAccessibleDescription(expect.stringContaining("Enter an http(s) link."));
    expect(within(dialog).getByText("A contact needs an email, phone or WhatsApp number.")).toBeInTheDocument();
    expect(backend.writes("POST", "jobs/ingest")).toHaveLength(0);
  });

  it("adds the job, screened for the active profile, and opens it", async () => {
    const { backend, user } = setup({ jobs: [] });
    await user.click(await screen.findByRole("button", { name: "Add job manually" }));
    const dialog = screen.getByRole("dialog", { name: "Add a job manually" });
    await user.type(within(dialog).getByLabelText(/^Job title/), "Django Developer");
    await user.type(within(dialog).getByLabelText("Company"), "Globex Example");
    await user.type(within(dialog).getByLabelText("Experience required"), "1-3 years");
    await user.type(within(dialog).getByLabelText(/^Full job description/), "Django and Python developer wanted.");
    await user.click(within(dialog).getByRole("button", { name: "Add contact" }));
    await user.type(within(dialog).getByLabelText("Email (contact 1)"), "jobs@globex.example");
    await user.click(within(dialog).getByRole("button", { name: "Add job" }));

    expect(await screen.findByRole("heading", { level: 1, name: "Django Developer" })).toBeInTheDocument();
    expect(backend.writes("POST", "jobs/ingest/")[0]!.body).toEqual({
      title: "Django Developer",
      full_jd: "Django and Python developer wanted.",
      profile: 1,
      source: "MANUAL",
      company_name: "Globex Example",
      required_experience_text: "1-3 years",
      contacts: [{ email: "jobs@globex.example" }],
    });
    expect(screen.getByText("Added “Django Developer” - Shortlisted")).toBeInTheDocument();
    expect(screen.getByText("jobs@globex.example")).toBeInTheDocument();
  });

  it("shows the existing job instead of creating a duplicate", async () => {
    const { backend, user } = setup({ jobs: [makeJob(5, { title: "Existing Role", full_jd: "Same text" })] });
    await user.click(await screen.findByRole("button", { name: "Add job manually" }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText(/^Job title/), "Another title");
    await user.type(within(dialog).getByLabelText(/^Full job description/), "Same text");
    await user.click(within(dialog).getByRole("button", { name: "Add job" }));

    expect(await screen.findByRole("heading", { level: 1, name: "Existing Role" })).toBeInTheDocument();
    expect(screen.getByText("This job already exists (same description); showing the existing job.")).toBeInTheDocument();
    expect(backend.state.jobs).toHaveLength(1);
  });

  it("shows backend validation next to the field", async () => {
    const { user } = setup({ jobs: [] });
    server.use(http.post(`${API}/jobs/ingest/`, () => HttpResponse.json({ source_url: ["Enter a valid URL."] }, { status: 400 })));
    await user.click(await screen.findByRole("button", { name: "Add job manually" }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText(/^Job title/), "Role");
    await user.type(within(dialog).getByLabelText(/^Full job description/), "Text");
    await user.type(within(dialog).getByLabelText("Link to the posting"), "https://x");
    await user.click(within(dialog).getByRole("button", { name: "Add job" }));
    await waitFor(() =>
      expect(within(dialog).getByLabelText("Link to the posting")).toHaveAccessibleDescription(expect.stringContaining("Enter a valid URL.")),
    );
  });
});

describe("filter helpers", () => {
  it("round-trips URL params and maps them to API filters", () => {
    const params = readParams(new URLSearchParams("status=APPLIED&outcome=UNSCREENED&reason=POSSIBLE_DUPLICATE&source=MANUAL&company=3&mine=1&page=2"));
    expect(toApiFilters(params, 9)).toEqual({
      status: "APPLIED",
      screening_outcome: "UNSCREENED",
      screening_reason: "POSSIBLE_DUPLICATE",
      source: "MANUAL",
      company: 3,
      profile: 9,
    });
    expect(writeParams(params, { status: undefined }).toString()).toBe("outcome=UNSCREENED&reason=POSSIBLE_DUPLICATE&source=MANUAL&company=3&mine=1");
    expect(writeParams(params, { page: 3 }).get("page")).toBe("3");
    expect(readParams(new URLSearchParams("page=0&company=-1")).page).toBe(1);
  });
});
