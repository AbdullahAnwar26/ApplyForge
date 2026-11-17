import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import type { ApplicationDetail, Skill } from "../../api/types";
import { ACTIVE_PROFILE_STORAGE_KEY } from "../../profile/ActiveProfileProvider";
import { makeApplication } from "../../test/mockApplications";
import { createMockBackend, makeResume } from "../../test/mockBackend";
import { makeRun } from "../../test/mockCollector";
import { makeJob } from "../../test/mockJobs";
import { makeProfile, renderApp } from "../../test/renderApp";
import { API, server } from "../../test/server";

const RIYA = makeProfile(1, "Riya Fictional", { target_roles: "Python Developer, Backend Engineer", experience_years: 1.5, max_acceptable_experience_years: 5 });
const SAM = makeProfile(2, "Sam Example", { target_roles: "Django Developer" });
const SKILLS: Skill[] = [
  { id: 11, name: "Python", category: "TECHNOLOGY", profile: 1 },
  { id: 12, name: "Django", category: "FRAMEWORK", profile: 1 },
  { id: 21, name: "Go", category: "TECHNOLOGY", profile: 2 },
];
// Newest first, as the backend orders them: 2 shortlisted, 1 needs review, 1 rejected, 2 unscreened.
const JOBS = [
  makeJob(16, { title: "Python Backend Developer", screening_outcome: "SHORTLISTED", screening_reason: "KEYWORD_MATCH" }),
  makeJob(15, { title: "Django Developer", screening_outcome: "NEEDS_REVIEW", screening_reason: "EXPERIENCE_UNKNOWN", company: { id: 2, name: "Globex Example" } }),
  makeJob(14, { title: "Rust Engineer", screening_outcome: "REJECTED", screening_reason: "NO_KEYWORD_MATCH" }),
  makeJob(13, { title: "Backend Engineer", screening_outcome: null }),
  makeJob(12, { title: "Data Engineer", screening_outcome: null }),
  makeJob(11, { title: "Platform Engineer", screening_outcome: "SHORTLISTED", screening_reason: "KEYWORD_MATCH" }),
];
const job = (id: number, title: string) => ({ id, title, company: { id: 1, name: "Acme Fictional Labs" }, status: "SHORTLISTED" as const, screening_outcome: "SHORTLISTED" as const });
const APPS: ApplicationDetail[] = [
  makeApplication(1, { status: "GENERATED", job: job(16, "Python Backend Developer") }),
  makeApplication(2, { status: "SENT", sent_at: "2026-03-02T00:00:00Z", job: job(11, "Platform Engineer") }),
  makeApplication(3, { status: "SHORTLISTED", job: job(15, "Django Developer") }),
  makeApplication(4, { profile: 2, status: "READY_TO_SEND", job: job(13, "Backend Engineer") }),
];
const RUNS = [
  makeRun(9, { status: "PARTIAL", keywords: ["Python Developer"], counts: { found: 4, created: 2, existing: 1, skipped: 0, invalid: 0, failed: 1 }, error: "1 post(s) failed to ingest." }),
  makeRun(8),
];

function setup(initial: Parameters<typeof createMockBackend>[0] = {}, path = "/") {
  const backend = createMockBackend({
    profiles: [RIYA],
    skills: SKILLS,
    resumes: [makeResume(5, 1, { name: "Backend CV", is_default: true }), makeResume(6, 1, { name: "Data CV" })],
    jobs: JOBS,
    applications: APPS,
    runs: RUNS,
    ...initial,
  });
  server.use(...backend.handlers);
  const utils = renderApp({ path });
  return { backend, user: userEvent.setup(), ...utils };
}

const card = (name: string) => screen.getByRole("region", { name });
/** The metric with this label (its value and label are in one element). */
const metric = (region: HTMLElement, label: string) =>
  within(region).getAllByText((_, el) => el?.classList.contains("metric__label") === true && el.textContent?.startsWith(label) === true)[0]!.closest(".metric")!;
const writes = (backend: ReturnType<typeof createMockBackend>) => backend.calls.filter((c) => c.method !== "GET");
const gets = (backend: ReturnType<typeof createMockBackend>, path: string) => backend.calls.filter((c) => c.method === "GET" && c.path === path).length;

describe("dashboard metrics", () => {
  it("shows the profile, jobs, applications and latest run from the backend data", async () => {
    const { backend } = setup();
    expect(await screen.findByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();

    const profile = card("Profile");
    expect(profile).toHaveTextContent("Riya Fictional");
    expect(within(profile).getByRole("list", { name: "Target roles" })).toHaveTextContent("Python DeveloperBackend Engineer");
    expect(profile).toHaveTextContent("1.5 years of experience · accepts requirements up to 5 years");
    await waitFor(() => expect(metric(profile, "Skills")).toHaveTextContent("2Skills"));
    expect(metric(profile, "Resumes")).toHaveTextContent("2Resumes");
    expect(profile).toHaveTextContent("Default resume: Backend CV");

    const jobs = card("Jobs");
    await waitFor(() => expect(metric(jobs, "Jobs in total")).toHaveTextContent("6Jobs in total"));
    expect(metric(jobs, "Shortlisted")).toHaveTextContent("2");
    expect(metric(jobs, "Needs review")).toHaveTextContent("1");
    expect(metric(jobs, "Rejected")).toHaveTextContent("1");
    expect(metric(jobs, "Unscreened")).toHaveTextContent("2");
    expect(metric(jobs, "Needs review")).toHaveAttribute("href", "/jobs?outcome=NEEDS_REVIEW");
    const recent = within(jobs).getByRole("list", { name: "Recently found jobs" });
    expect(within(recent).getAllByRole("link").map((a) => a.textContent)).toEqual([
      "Python Backend Developer",
      "Django Developer",
      "Rust Engineer",
      "Backend Engineer",
      "Data Engineer",
    ]);
    expect(within(recent).getByRole("link", { name: "Django Developer" })).toHaveAttribute("href", "/jobs/15");
    expect(recent).toHaveTextContent("Experience unknown");

    const apps = card("Applications");
    await waitFor(() => expect(metric(apps, "Applications")).toHaveTextContent("3")); // profile 1 only
    expect(metric(apps, "Waiting for you")).toHaveTextContent("2");
    expect(metric(apps, "Waiting for you")).toHaveAttribute("href", "/applications?awaiting=1");
    const byStatus = within(apps).getByRole("list", { name: "Applications by status" });
    expect(within(byStatus).getAllByRole("link").map((a) => [a.textContent, a.getAttribute("href")])).toEqual([
      ["Shortlisted1", "/applications?status=SHORTLISTED"],
      ["Draft ready1", "/applications?status=GENERATED"],
      ["Sent1", "/applications?status=SENT"],
    ]);
    const waiting = within(apps).getByRole("list", { name: "Applications waiting for you" });
    expect(within(waiting).getAllByRole("link").map((a) => a.getAttribute("href"))).toEqual(["/applications/1", "/applications/3"]);
    expect(within(apps).getByRole("list", { name: "Recently created applications" })).toHaveTextContent("Platform Engineer");

    const collector = card("Latest collector run");
    expect(await within(collector).findByText(/Run #9 ·/)).toBeInTheDocument();
    expect(within(collector).getByText("Partial")).toHaveClass("badge--warning");
    expect(metric(collector, "New jobs")).toHaveTextContent("2");
    expect(metric(collector, "Failed")).toHaveTextContent("1");
    expect(collector).toHaveTextContent("1 post(s) failed to ingest.");
    expect(collector).toHaveTextContent("2 runs in total.");
    expect(within(collector).getByRole("link", { name: "Run #9 details" })).toHaveAttribute("href", "/collector/runs/9");

    expect(writes(backend)).toHaveLength(0);
  });

  it("links to every section", async () => {
    setup();
    await screen.findByRole("heading", { level: 1, name: "Dashboard" });
    const links = (name: string) => within(card(name)).getAllByRole("link").map((a) => a.getAttribute("href"));
    expect(links("Jobs")).toContain("/jobs");
    expect(links("Applications")).toEqual(expect.arrayContaining(["/applications", "/applications?awaiting=1"]));
    expect(links("Profile")).toEqual(expect.arrayContaining(["/profile", "/resumes"]));
    await waitFor(() => expect(links("Latest collector run")).toEqual(expect.arrayContaining(["/collector", "/collector/runs/9"])));
  });

  it("follows the active profile", async () => {
    window.localStorage.setItem(ACTIVE_PROFILE_STORAGE_KEY, "1");
    const { backend, user } = setup({ profiles: [RIYA, SAM] });
    await waitFor(() => expect(metric(card("Applications"), "Applications")).toHaveTextContent("3"));
    await user.selectOptions(screen.getByLabelText("Profile", { selector: "select.profile-switcher__select" }), "Sam Example");

    await waitFor(() => expect(card("Profile")).toHaveTextContent("Sam Example"));
    await waitFor(() => expect(metric(card("Applications"), "Applications")).toHaveTextContent("1"));
    await waitFor(() => expect(metric(card("Profile"), "Skills")).toHaveTextContent("1Skills"));
    expect(card("Profile")).toHaveTextContent("No active default resume");
    expect(within(card("Applications")).getByRole("list", { name: "Applications by status" })).toHaveTextContent("Ready to send1");
    expect(writes(backend)).toHaveLength(0);
  });
});

describe("empty, loading and failure states", () => {
  it("explains an empty workspace, and zero is shown as zero", async () => {
    setup({ jobs: [], applications: [], runs: [], skills: [], resumes: [] });
    expect(await within(await screen.findByRole("region", { name: "Jobs" })).findByText("No jobs yet")).toBeInTheDocument();
    await waitFor(() => expect(metric(card("Jobs"), "Jobs in total")).toHaveTextContent("0Jobs in total"));
    expect(metric(card("Jobs"), "Jobs in total")).not.toHaveTextContent("unavailable");
    expect(await within(card("Applications")).findByText("No applications for Riya Fictional yet")).toBeInTheDocument();
    expect(await within(card("Latest collector run")).findByText("No collector runs yet")).toBeInTheDocument();
    expect(card("Profile")).toHaveTextContent("No skills yet - jobs cannot be screened.");
    expect(card("Profile")).toHaveTextContent("No active default resume");
  });

  it("shows loading skeletons", async () => {
    const backend = createMockBackend({ profiles: [RIYA] });
    server.use(...backend.handlers);
    const slow = (path: string) => http.get(`${API}/${path}`, async () => {
      await delay(200);
      return HttpResponse.json({ count: 0, next: null, previous: null, results: [] });
    });
    server.use(slow("jobs/"), slow("collector/runs/"));
    renderApp({ path: "/" });
    expect(await screen.findByRole("status", { name: "Loading recent jobs" })).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Loading collector runs" })).toBeInTheDocument();
    expect(metric(card("Jobs"), "Jobs in total")).toHaveTextContent("…");
    expect(await within(card("Jobs")).findByText("No jobs yet")).toBeInTheDocument();
  });

  it("keeps other sections working when one fails, and retries only that one", async () => {
    let failSummary = true;
    const { backend, user } = setup();
    server.use(
      http.get(`${API}/applications/summary/`, () =>
        failSummary ? HttpResponse.json({ detail: "Summary unavailable." }, { status: 503 }) : HttpResponse.json({ total: 7, by_status: { SHORTLISTED: 7 }, awaiting_action: { count: 7, statuses: [], oldest: [] }, recent: [] }),
      ),
    );
    const apps = await screen.findByRole("region", { name: "Applications" });
    expect(await within(apps).findByText("Could not load applications", {}, { timeout: 6000 })).toBeInTheDocument();
    expect(apps).toHaveTextContent("Summary unavailable.");
    // Unrelated sections are unaffected.
    await waitFor(() => expect(metric(card("Jobs"), "Jobs in total")).toHaveTextContent("6"));
    expect(await within(card("Latest collector run")).findByText(/Run #9 ·/)).toBeInTheDocument();

    failSummary = false;
    await user.click(within(apps).getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(metric(card("Applications"), "Applications")).toHaveTextContent("7"));
    expect(writes(backend)).toHaveLength(0);
  });

  it("marks a count that could not be loaded as unavailable, not zero", async () => {
    let fail = true;
    const { user } = setup();
    server.use(
      http.get(`${API}/jobs/`, ({ request }) =>
        fail && new URL(request.url).searchParams.get("screening_outcome") === "REJECTED"
          ? HttpResponse.json({ screening_outcome: ["Temporarily unavailable."] }, { status: 400 })
          : undefined,
      ),
    );
    const jobs = await screen.findByRole("region", { name: "Jobs" });
    await waitFor(() => expect(metric(jobs, "Rejected")).toHaveTextContent("—"));
    expect(metric(jobs, "Rejected")).toHaveTextContent("unavailable");
    expect(metric(jobs, "Rejected").tagName).toBe("DIV"); // not a link to a misleading list
    await waitFor(() => expect(metric(jobs, "Shortlisted")).toHaveTextContent("2"));
    expect(within(jobs).getByText("Some job counts could not be loaded")).toBeInTheDocument();

    fail = false;
    await user.click(within(jobs).getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(metric(card("Jobs"), "Rejected")).toHaveTextContent("1Rejected"));
  });
});

describe("refreshing", () => {
  it("refreshes every section on request, with GET requests only", async () => {
    const { backend, user } = setup();
    await waitFor(() => expect(metric(card("Applications"), "Applications")).toHaveTextContent("3"));
    await within(card("Latest collector run")).findByText(/Run #9 ·/);
    expect(card("Jobs")).toHaveTextContent(/As of \d/);
    const before = { jobs: gets(backend, "jobs/"), summary: gets(backend, "applications/summary/"), runs: gets(backend, "collector/runs/"), skills: gets(backend, "skills/") };

    backend.state.applications.push(makeApplication(30, { status: "OPENED", job: job(12, "Data Engineer") }));
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(metric(card("Applications"), "Applications")).toHaveTextContent("4"));
    expect(gets(backend, "jobs/")).toBeGreaterThan(before.jobs);
    expect(gets(backend, "applications/summary/")).toBe(before.summary + 1);
    expect(gets(backend, "collector/runs/")).toBe(before.runs + 1);
    expect(gets(backend, "skills/")).toBe(before.skills + 1);
    expect(writes(backend)).toHaveLength(0);
  });

  it("shows changes made in other sections", async () => {
    const { user } = setup();
    await waitFor(() => expect(metric(card("Jobs"), "Unscreened")).toHaveTextContent("2"));
    // Re-screen every job on the Jobs page (an explicit, confirmed action there).
    await user.click(within(screen.getByRole("navigation", { name: "Main" })).getByRole("link", { name: "Jobs" }));
    await user.click(await screen.findByRole("button", { name: "Re-screen all jobs" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Re-screen all" }));
    await screen.findByText(/^Screened 6 jobs:/);
    await user.click(within(screen.getByRole("navigation", { name: "Main" })).getByRole("link", { name: "Dashboard" }));
    await waitFor(() => expect(metric(card("Jobs"), "Unscreened")).toHaveTextContent("0"));
  });

  it("never screens, generates, collects or saves anything", async () => {
    const { backend, user } = setup();
    await waitFor(() => expect(metric(card("Applications"), "Applications")).toHaveTextContent("3"));
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Refresh" })).not.toHaveAttribute("aria-busy"));
    expect(writes(backend)).toEqual([]);
    expect(screen.queryByRole("button", { name: /screen|generate|start|collect/i })).not.toBeInTheDocument();
  });
});

describe("keyboard", () => {
  it("opens the waiting applications from the keyboard", async () => {
    const { user } = setup();
    await waitFor(() => expect(metric(card("Applications"), "Waiting for you")).toHaveAttribute("href"));
    (metric(card("Applications"), "Waiting for you") as HTMLElement).focus();
    await user.keyboard("{Enter}");
    expect(await screen.findByRole("heading", { level: 1, name: "Applications" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Only those waiting for me" })).toBeChecked();
  });
});
