import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { Company, Skill } from "../../api/types";
import { ACTIVE_PROFILE_STORAGE_KEY } from "../../profile/ActiveProfileProvider";
import { createMockBackend } from "../../test/mockBackend";
import { makeJob } from "../../test/mockJobs";
import { makeProfile, renderApp } from "../../test/renderApp";
import { API, server } from "../../test/server";
import { formatRange, safeHttpUrl } from "./JobDetailPage";

const RIYA = makeProfile(1, "Riya Fictional", { max_acceptable_experience_years: 5, experience_years: 1.5 });
const SAM = makeProfile(2, "Sam Example");
const ACME: Company = { id: 1, name: "Acme Fictional Labs", website: "https://acme.example", created_at: "", updated_at: "" };
const SKILLS: Skill[] = [
  { id: 11, name: "Python", category: "TECHNOLOGY", profile: 1 },
  { id: 12, name: "Django", category: "FRAMEWORK", profile: 1 },
];

const SCREENED = makeJob(7, {
  title: "Python Backend Developer",
  source_url: "https://www.linkedin.com/feed/update/urn:li:activity:7300000000000000001/",
  posted_date: "2026-02-27T00:00:00Z",
  status: "SHORTLISTED",
  screening_outcome: "NEEDS_REVIEW",
  screening_reason: "POSSIBLE_DUPLICATE",
  screened_at: "2026-03-01T12:00:00Z",
  matched_keywords: ["Django", "Python"],
  screening_details: {
    rules_version: 1,
    profile_id: 1,
    matched_keywords: ["Python", "Django"],
    experience: {
      min: 2,
      max: 4,
      source: "field",
      matches: [{ text: "2-4 years", min: 2, max: 4 }],
      max_acceptable_years: 5,
      profile_experience_years: 1.5,
      above_actual_experience: true,
    },
    flags: ["POSSIBLE_DUPLICATE"],
    duplicate_candidates: [{ job_id: 3, matched_on: ["title", "company"] }],
  },
  contacts: [
    { id: 1, name: "Fictional Recruiter", email: "talent@acme.example", phone: "+1 555 0100", whatsapp: null, company: 1, created_at: "", updated_at: "" },
  ],
  full_jd: "We're hiring.\n<script>alert(1)</script>\n<b>Python</b> and Django.",
});

function setup(initial: Parameters<typeof createMockBackend>[0] = {}, path = "/jobs/7") {
  const backend = createMockBackend({ profiles: [RIYA], skills: SKILLS, companies: [ACME], jobs: [SCREENED], ...initial });
  server.use(...backend.handlers);
  const utils = renderApp({ path });
  return { backend, user: userEvent.setup(), ...utils };
}

const panel = (name: string) => screen.getByRole("region", { name });

describe("job detail", () => {
  it("shows everything the backend returns", async () => {
    setup();
    expect(await screen.findByRole("heading", { level: 1, name: "Python Backend Developer" })).toBeInTheDocument();
    const overview = panel("Overview");
    expect(overview).toHaveTextContent("Acme Fictional Labs");
    expect(await within(overview).findByRole("link", { name: /https:\/\/acme\.example/ })).toHaveAttribute("rel", "noopener noreferrer nofollow");
    expect(overview).toHaveTextContent("2-4 years (read as 2–4 years)");
    const source = within(overview).getByRole("link", { name: /Open the original posting/ });
    expect(source).toHaveAttribute("href", SCREENED.source_url);
    expect(source).toHaveAttribute("target", "_blank");
    expect(overview).toHaveTextContent("LinkedIn");

    const screening = panel("Screening");
    expect(within(screening).getAllByText("Needs review")[0]).toBeInTheDocument();
    expect(screening).toHaveTextContent("Possible duplicate");
    expect(screening).toHaveTextContent("for Riya Fictional");
    expect(within(screening).getByText("Django")).toBeInTheDocument();
    expect(screening).toHaveTextContent("Required: 2–4 years, read from the experience field");
    expect(screening).toHaveTextContent("Found: “2-4 years”");
    expect(screening).toHaveTextContent("Accepted up to 5 years; profile experience 1.5 years");
    expect(screening).toHaveTextContent("Asks for more than the profile's actual experience");
    expect(within(screening).getByRole("link", { name: "Job #3" })).toHaveAttribute("href", "/jobs/3");
    expect(screening).toHaveTextContent("(same title and company)");

    const contacts = panel("Contacts");
    expect(contacts).toHaveTextContent("Fictional Recruiter");
    expect(contacts).toHaveTextContent("talent@acme.example");
    expect(within(contacts).queryByRole("link")).not.toBeInTheDocument(); // nothing to click and send

    const description = panel("Job description");
    expect(description).toHaveTextContent("<script>alert(1)</script>");
    expect(description.querySelector("script, b")).toBeNull(); // plain text, never HTML
  });

  it("handles missing optional fields", async () => {
    setup({
      companies: [],
      jobs: [
        makeJob(7, {
          title: "Bare Job",
          company: null,
          location: null,
          work_mode: null,
          required_experience_text: null,
          parsed_min_experience: null,
          parsed_max_experience: null,
          source: "MANUAL",
          source_url: "javascript:alert(1)",
        }),
      ],
    });
    await screen.findByRole("heading", { level: 1, name: "Bare Job" });
    const overview = panel("Overview");
    expect(within(overview).getAllByText("Not given").length).toBeGreaterThanOrEqual(5);
    expect(within(overview).queryByRole("link")).not.toBeInTheDocument(); // unsafe link not rendered
    expect(overview).toHaveTextContent("Added manually");
    expect(panel("Contacts")).toHaveTextContent("No contact details were found");
    expect(panel("Screening")).toHaveTextContent("Not screened yet.");
  });

  it("collapses a long description", async () => {
    const { user } = setup({ jobs: [makeJob(7, { title: "Long", full_jd: "word ".repeat(600) })] });
    await screen.findByRole("heading", { level: 1, name: "Long" });
    const toggle = screen.getByRole("button", { name: "Show the full description" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await user.click(toggle);
    expect(screen.getByRole("button", { name: "Show less" })).toHaveAttribute("aria-expanded", "true");
  });

  it("reports a job that does not exist, and a malformed id without a request", async () => {
    const { backend } = setup({}, "/jobs/999");
    expect(await screen.findByRole("heading", { level: 1, name: "Job not found" })).toBeInTheDocument();
    expect(backend.calls.some((c) => c.path === "jobs/999/")).toBe(true);
  });

  it("does not request a malformed id", async () => {
    const { backend } = setup({}, "/jobs/abc");
    expect(await screen.findByRole("heading", { level: 1, name: "Job not found" })).toBeInTheDocument();
    expect(backend.calls.some((c) => c.path.startsWith("jobs/abc"))).toBe(false);
  });

  it("shows a retryable error", async () => {
    const backend = createMockBackend({ profiles: [RIYA], skills: SKILLS, companies: [ACME] });
    let fail = true;
    server.use(...backend.handlers);
    server.use(
      http.get(`${API}/jobs/:id/`, () => (fail ? HttpResponse.json({ detail: "Unavailable." }, { status: 400 }) : HttpResponse.json(SCREENED))),
    );
    renderApp({ path: "/jobs/7" });
    expect(await screen.findByText("Could not load this job")).toBeInTheDocument();
    fail = false;
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Python Backend Developer" })).toBeInTheDocument();
  });

  it("keeps the list filters on the way back", async () => {
    const { user } = setup({}, "/jobs?outcome=NEEDS_REVIEW");
    await user.click(await screen.findByRole("link", { name: "Python Backend Developer" }));
    expect(await screen.findByRole("link", { name: "← All jobs" })).toHaveAttribute("href", "/jobs?outcome=NEEDS_REVIEW");
  });
});

describe("screening a job", () => {
  it("screens only when asked, with the active profile, and shows the result", async () => {
    const { backend, user } = setup({ jobs: [makeJob(7, { title: "Fresh Job", full_jd: "Python services" })] });
    await screen.findByRole("heading", { level: 1, name: "Fresh Job" });
    await waitFor(() => expect(panel("Screening")).toHaveTextContent("Riya Fictional's skills (2)"));
    expect(backend.calls.filter((c) => c.method === "POST")).toHaveLength(0); // not on load

    await user.click(screen.getByRole("button", { name: "Screen with Riya Fictional" }));
    expect(await screen.findByText("Screened: Shortlisted")).toBeInTheDocument();
    expect(backend.writes("POST", "jobs/7/screen/")[0]!.body).toEqual({ profile: 1 });
    const screening = panel("Screening");
    expect(within(screening).getByText("Python")).toBeInTheDocument();
    expect(within(screening).getByRole("button", { name: "Re-screen with Riya Fictional" })).toBeInTheDocument();
  });

  it("refreshes the job list after screening", async () => {
    const { backend, user } = setup({ jobs: [makeJob(7, { title: "Fresh Job", full_jd: "Python services" })] }, "/jobs");
    await user.click(await screen.findByRole("link", { name: "Fresh Job" }));
    await user.click(await screen.findByRole("button", { name: "Screen with Riya Fictional" }));
    await screen.findByText("Screened: Shortlisted");
    await user.click(screen.getByRole("link", { name: "← All jobs" }));
    const table = await screen.findByRole("table", { name: "Jobs" });
    expect(await within(table).findByText("Shortlisted")).toBeInTheDocument();
    expect(backend.calls.filter((c) => c.method === "GET" && c.path === "jobs/").length).toBeGreaterThanOrEqual(2);
  });

  it("sends one request even when clicked repeatedly", async () => {
    const { backend, user } = setup({ jobs: [makeJob(7, { title: "Fresh Job", full_jd: "Python" })] });
    backend.options.writeDelay = 200;
    const button = await screen.findByRole("button", { name: "Screen with Riya Fictional" });
    await user.click(button);
    expect(button).toBeDisabled();
    await user.click(button);
    await screen.findByText("Screened: Shortlisted");
    expect(backend.writes("POST", "jobs/7/screen/")).toHaveLength(1);
  });

  it("shows why screening failed", async () => {
    const { user } = setup({ jobs: [makeJob(7, { title: "Fresh Job" })] });
    server.use(
      http.post(`${API}/jobs/:id/screen/`, () =>
        HttpResponse.json({ profile: ["Profile 'Riya Fictional' has no skills. Add skills before screening."] }, { status: 400 }),
      ),
    );
    await user.click(await screen.findByRole("button", { name: "Screen with Riya Fictional" }));
    expect(await within(panel("Screening")).findByRole("alert")).toHaveTextContent("has no skills");
  });

  it("is unavailable while the profile has no skills", async () => {
    setup({ skills: [], jobs: [makeJob(7, { title: "Fresh Job" })] });
    await screen.findByRole("heading", { level: 1, name: "Fresh Job" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Screen with Riya Fictional" })).toBeDisabled());
    expect(panel("Screening")).toHaveTextContent("Riya Fictional has no skills, so screening is not possible.");
  });

  it("notes a result produced for another profile", async () => {
    window.localStorage.setItem(ACTIVE_PROFILE_STORAGE_KEY, "2");
    setup({ profiles: [RIYA, SAM], skills: [...SKILLS, { id: 20, name: "Go", category: "TECHNOLOGY", profile: 2 }] });
    await screen.findByRole("heading", { level: 1, name: "Python Backend Developer" });
    const screening = panel("Screening");
    expect(screening).toHaveTextContent("This result was produced for another profile. Re-screen to check it against Sam Example.");
    expect(screening).toHaveTextContent("for Riya Fictional");
  });
});

describe("job status", () => {
  it("saves the user's status", async () => {
    const { backend, user } = setup();
    await screen.findByRole("heading", { level: 1, name: "Python Backend Developer" });
    const select = screen.getByLabelText("Your status");
    const save = screen.getByRole("button", { name: "Save status" });
    expect(save).toBeDisabled();
    await user.selectOptions(select, "Skipped");
    expect(screen.getByText(/You decided not to apply./)).toBeInTheDocument();
    await user.click(save);
    expect(await screen.findByText("Status: Skipped")).toBeInTheDocument();
    expect(backend.writes("PATCH", "jobs/7/")[0]!.body).toEqual({ status: "SKIPPED" });
    expect(within(panel("Overview")).getAllByText("Skipped").some((el) => el.classList.contains("badge"))).toBe(true);
  });

  it("shows a failed save", async () => {
    const { user } = setup();
    server.use(http.patch(`${API}/jobs/:id/`, () => HttpResponse.json({ status: ['"X" is not a valid choice.'] }, { status: 400 })));
    await user.selectOptions(await screen.findByLabelText("Your status"), "Applied");
    await user.click(screen.getByRole("button", { name: "Save status" }));
    expect(await within(panel("Overview")).findByRole("alert")).toHaveTextContent("is not a valid choice");
  });
});

describe("helpers", () => {
  it("formatRange and safeHttpUrl", () => {
    expect(formatRange(2, 4)).toBe("2–4 years");
    expect(formatRange(3, null)).toBe("3+ years");
    expect(formatRange(null, 4)).toBeNull();
    expect(safeHttpUrl("https://a.example/x")).toBe("https://a.example/x");
    expect(safeHttpUrl("javascript:alert(1)")).toBeNull();
    expect(safeHttpUrl("not a url")).toBeNull();
    expect(safeHttpUrl(null)).toBeNull();
  });
});
