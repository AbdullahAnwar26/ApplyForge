/**
 * Collector screens. Every request is answered by the fake API (test/mockCollector.ts);
 * nothing is collected and LinkedIn is never contacted. The key assertion throughout:
 * how many POST collector/runs/ requests were sent (0 unless confirmed, never more than 1).
 */

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it } from "vitest";

import { parseKeywords, validateRunInput } from "../../api/collector";
import { ApiError } from "../../api/errors";
import type { Skill } from "../../api/types";
import { ACTIVE_PROFILE_STORAGE_KEY } from "../../profile/ActiveProfileProvider";
import { createMockBackend } from "../../test/mockBackend";
import { makeRun } from "../../test/mockCollector";
import { makeJob } from "../../test/mockJobs";
import { makeProfile, renderApp } from "../../test/renderApp";
import { API, server } from "../../test/server";
import { describeStartError } from "./runText";

const RIYA = makeProfile(1, "Riya Fictional", { target_roles: "Python Developer, Backend Engineer" });
const SAM = makeProfile(2, "Sam Example", { target_roles: "Django Developer" });
const SKILLS: Skill[] = [{ id: 11, name: "Python", category: "TECHNOLOGY", profile: 1 }];
const JOBS = [
  makeJob(7, { title: "Python Backend Developer", screening_outcome: "SHORTLISTED" }),
  makeJob(8, { title: "Django Developer", screening_outcome: "NEEDS_REVIEW" }),
  makeJob(9, { title: "Backend Engineer" }),
];

afterEach(() => server.events.removeAllListeners());

function setup(initial: Parameters<typeof createMockBackend>[0] = {}, path = "/collector") {
  const backend = createMockBackend({ profiles: [RIYA], skills: SKILLS, jobs: JOBS, ...initial });
  server.use(...backend.handlers);
  const utils = renderApp({ path });
  return { backend, user: userEvent.setup(), ...utils };
}

const posts = (backend: ReturnType<typeof createMockBackend>) => backend.writes("POST", "collector/runs");
const historyReads = (backend: ReturnType<typeof createMockBackend>) =>
  backend.calls.filter((c) => c.method === "GET" && c.path === "collector/runs/").length;

async function openConfirmation(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Review and start…" }));
  return screen.getByRole("dialog", { name: "Start a LinkedIn collection run?" });
}

async function confirmRun(user: ReturnType<typeof userEvent.setup>) {
  const dialog = await openConfirmation(user);
  await user.click(within(dialog).getByRole("checkbox", { name: /I understand this run accesses LinkedIn/ }));
  await user.click(within(dialog).getByRole("button", { name: "Start collection run" }));
}

describe("run history", () => {
  it("lists runs and starts nothing on load", async () => {
    const { backend } = setup({
      runs: [
        makeRun(3, { status: "PARTIAL", keywords: ["Python Developer", "Backend Engineer"], counts: { found: 4, created: 2, existing: 1, skipped: 1, invalid: 0, failed: 0 } }),
        makeRun(2, { status: "FAILED", trigger: "command", counts: {}, job_ids: [] }),
      ],
    });
    const table = await screen.findByRole("table", { name: "Collector runs" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(within(rows[0]!).getByRole("link", { name: "Run #3" })).toHaveAttribute("href", "/collector/runs/3");
    expect(within(rows[0]!).getByText("Partial")).toHaveClass("badge--warning");
    expect(rows[0]).toHaveTextContent("Python Developer, Backend Engineer");
    expect(rows[0]).toHaveTextContent("2 new · 1 known · 4 read");
    expect(rows[0]).toHaveTextContent("This app");
    expect(rows[1]).toHaveTextContent("Command line");
    expect(within(rows[1]!).getByText("Failed")).toHaveClass("badge--danger");
    expect(posts(backend)).toHaveLength(0);
  });

  it("explains an empty history", async () => {
    setup();
    expect(await screen.findByText("No collector runs yet")).toBeInTheDocument();
  });

  it("shows a retryable error - and retrying only reads", async () => {
    let fail = true;
    const backend = createMockBackend({ profiles: [RIYA], skills: SKILLS });
    server.use(...backend.handlers);
    server.use(http.get(`${API}/collector/runs/`, () => (fail ? HttpResponse.json({ detail: "Unavailable." }, { status: 400 }) : HttpResponse.json({ count: 0, next: null, previous: null, results: [] }))));
    renderApp({ path: "/collector" });
    expect(await screen.findByText("Could not load the run history")).toBeInTheDocument();
    fail = false;
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("No collector runs yet")).toBeInTheDocument();
    expect(posts(backend)).toHaveLength(0);
  });

  it("filters by status, trigger and profile, and pages", async () => {
    const seen: URLSearchParams[] = [];
    server.events.on("request:start", ({ request }) => {
      const url = new URL(request.url);
      if (url.pathname === "/api/v1/collector/runs/" && request.method === "GET") seen.push(url.searchParams);
    });
    const runs = Array.from({ length: 25 }, (_, i) => makeRun(100 - i));
    const { user } = setup({ runs });
    expect(await screen.findByText("1–20 of 25")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("21–25 of 25")).toBeInTheDocument();

    const filters = screen.getByRole("group", { name: "Run filters" });
    await user.selectOptions(within(filters).getByLabelText("Status"), "Partial");
    await user.selectOptions(within(filters).getByLabelText("Started from"), "Command line");
    await user.click(within(filters).getByRole("checkbox", { name: "Only runs for Riya Fictional" }));
    await waitFor(() =>
      expect(Object.fromEntries(seen.at(-1)!)).toEqual({ status: "PARTIAL", trigger: "command", profile: "1", page: "1", page_size: "20" }),
    );
    expect(await screen.findByText("No runs match these filters")).toBeInTheDocument();
  });

  it("warns when a run is shown as running", async () => {
    setup({ runs: [makeRun(41, { status: "RUNNING", finished_at: null, duration_seconds: null })] });
    expect(await screen.findByRole("note")).toHaveTextContent("Run #41 is shown as running.");
  });

  it("refreshes only when asked, and only reads", async () => {
    const { backend, user } = setup({ runs: [makeRun(1)] });
    await screen.findByRole("table", { name: "Collector runs" });
    const before = historyReads(backend);
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(historyReads(backend)).toBe(before + 1));
    expect(posts(backend)).toHaveLength(0);
  });
});

describe("run detail", () => {
  it("shows every count, the settings, the error and the job links", async () => {
    setup(
      {
        runs: [
          makeRun(5, {
            status: "PARTIAL",
            keywords: ["Python Developer"],
            limit: 8,
            counts: { found: 6, created: 2, existing: 1, skipped: 1, invalid: 1, failed: 1 },
            job_ids: [7, 8, 404],
            error: "LinkedIn stopped responding after 6 posts.",
            duration_seconds: 125,
          }),
        ],
      },
      "/collector/runs/5",
    );
    expect(await screen.findByRole("heading", { level: 1, name: "Collector run #5" })).toBeInTheDocument();
    const result = screen.getByRole("region", { name: "Result" });
    expect(result).toHaveTextContent("The run stopped early or some posts could not be saved.");
    expect(result).toHaveTextContent("Reported problem: LinkedIn stopped responding after 6 posts.");
    for (const [label, value] of [["Posts read", "6"], ["New jobs", "2"], ["Already known", "1"], ["Skipped", "1"], ["Invalid", "1"], ["Failed", "1"]]) {
      const item = within(result).getByText(label!).closest("div")!;
      expect(item).toHaveTextContent(`${label}${value}`);
    }
    const settings = screen.getByRole("region", { name: "Run settings" });
    expect(settings).toHaveTextContent("Python Developer");
    expect(settings).toHaveTextContent("8");
    expect(settings).toHaveTextContent("Riya Fictional");
    expect(settings).toHaveTextContent("(2 min 5 s)");
    const jobs = screen.getByRole("region", { name: "Jobs from this run" });
    expect(await within(jobs).findByRole("link", { name: "Python Backend Developer" })).toHaveAttribute("href", "/jobs/7");
    expect(await within(jobs).findByRole("link", { name: "Django Developer" })).toBeInTheDocument();
    expect(await within(jobs).findByText("(no longer exists)")).toBeInTheDocument();
  });

  it("explains a run without counts and a missing run", async () => {
    setup({ runs: [makeRun(6, { status: "FAILED", counts: {}, job_ids: [], profile: null })] }, "/collector/runs/6");
    expect(await screen.findByText("No posts were processed, so there are no counts.")).toBeInTheDocument();
    expect(screen.getByText("None (deleted or not set)")).toBeInTheDocument();
    expect(screen.getByText("No jobs were created or matched by this run.")).toBeInTheDocument();
  });

  it("reports a run that does not exist", async () => {
    setup({}, "/collector/runs/77");
    expect(await screen.findByRole("heading", { level: 1, name: "Run not found" })).toBeInTheDocument();
  });
});

describe("starting a run - safeguards", () => {
  it("validates the form before showing the confirmation", async () => {
    const { backend, user } = setup();
    const keywords = await screen.findByLabelText(/^Search keywords/);
    expect(keywords).toHaveValue("Python Developer, Backend Engineer"); // from the profile's target roles
    await user.clear(keywords);
    await user.clear(screen.getByLabelText(/^Maximum posts/));
    await user.type(screen.getByLabelText(/^Maximum posts/), "11");
    await user.click(screen.getByRole("button", { name: "Review and start…" }));
    expect(keywords).toHaveAccessibleDescription(expect.stringContaining("Enter at least one keyword."));
    expect(screen.getByLabelText(/^Maximum posts/)).toHaveAccessibleDescription(expect.stringContaining("from 1 to 10"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(posts(backend)).toHaveLength(0);
  });

  it("shows exactly what will be sent and requires an acknowledgement", async () => {
    const { backend, user } = setup();
    await user.clear(await screen.findByLabelText(/^Maximum posts/));
    await user.type(screen.getByLabelText(/^Maximum posts/), "3");
    const dialog = await openConfirmation(user);
    expect(within(dialog).getAllByRole("listitem").slice(0, 2).map((li) => li.textContent)).toEqual(["Python Developer", "Backend Engineer"]);
    expect(dialog).toHaveTextContent("Maximum posts3");
    expect(dialog).toHaveTextContent("Riya Fictional");
    expect(dialog).toHaveTextContent("using the LinkedIn session already saved there");
    expect(dialog).toHaveTextContent("creates or updates job records");
    expect(dialog).toHaveTextContent("does not apply for any job and does not contact anyone");
    const start = within(dialog).getByRole("button", { name: "Start collection run" });
    expect(start).toBeDisabled();
    await user.click(start);
    expect(posts(backend)).toHaveLength(0);
  });

  it("cancelling (button or Escape) sends nothing", async () => {
    const { backend, user } = setup();
    let dialog = await openConfirmation(user);
    await user.click(within(dialog).getByRole("checkbox"));
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    dialog = await openConfirmation(user);
    expect(within(dialog).getByRole("checkbox")).not.toBeChecked(); // acknowledgement is per run
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(posts(backend)).toHaveLength(0);
  });

  it("a successful run sends one request, shows the result and refreshes the history", async () => {
    const { backend, user } = setup();
    await screen.findByText("No collector runs yet");
    const before = historyReads(backend);
    await confirmRun(user);

    expect(await screen.findByText(/Run #\d+ finished\./)).toBeInTheDocument();
    expect(posts(backend)).toHaveLength(1);
    expect(posts(backend)[0]!.body).toEqual({ keywords: ["Python Developer", "Backend Engineer"], limit: 5, profile: 1 });
    const startPanel = screen.getByRole("region", { name: "Start a collection run" });
    expect(within(startPanel).getByRole("link", { name: "Python Backend Developer" })).toHaveAttribute("href", "/jobs/7");
    expect(startPanel).toHaveTextContent("Job seeker's post (not a hiring post).");
    expect(within(startPanel).getByText("New jobs").closest("div")).toHaveTextContent("New jobs2");
    await waitFor(() => expect(historyReads(backend)).toBeGreaterThan(before));
    expect(await screen.findByRole("table", { name: "Collector runs" })).toBeInTheDocument();
  });

  it.each([
    ["partial", "Partial", "The run stopped early", "LinkedIn stopped responding after 2 posts."],
    ["failed", "Failed", "Nothing was collected", "Not logged in to LinkedIn."],
  ] as const)("a %s run (502) is shown as a finished run, with the reason", async (mode, badge, explanation, detail) => {
    const { backend, user } = setup();
    backend.options.collector = mode;
    await confirmRun(user);
    const panel = screen.getByRole("region", { name: "Start a collection run" });
    expect(await within(panel).findByText(/Run #\d+ finished\./)).toBeInTheDocument();
    expect(within(panel).getAllByText(badge).some((el) => el.classList.contains("badge"))).toBe(true);
    expect(panel).toHaveTextContent(explanation);
    expect(panel).toHaveTextContent(detail);
    expect(posts(backend)).toHaveLength(1);
  });

  it("a 409 points to the running run and does not refresh or retry", async () => {
    const { backend, user } = setup();
    backend.options.collector = 409;
    await screen.findByText("No collector runs yet");
    const before = historyReads(backend);
    await confirmRun(user);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Another collector run is in progress.");
    expect(within(alert).getByRole("link", { name: "Open run #41" })).toHaveAttribute("href", "/collector/runs/41");
    expect(posts(backend)).toHaveLength(1);
    expect(historyReads(backend)).toBe(before);
  });

  it("a 429 says how long to wait", async () => {
    const { backend, user } = setup();
    backend.options.collector = 429;
    await confirmRun(user);
    expect(await screen.findByRole("alert")).toHaveTextContent("Wait 37 seconds before starting another.");
    expect(posts(backend)).toHaveLength(1);
  });

  it("a 400 shows the reason and links the recorded run", async () => {
    const { backend, user } = setup();
    backend.options.collector = 400;
    await confirmRun(user);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("profile: No profile exists. Create a profile first.");
    expect(within(alert).getByRole("link", { name: /Open run #\d+/ })).toBeInTheDocument();
  });

  it("a 500 shows a generic message, not the exception", async () => {
    const { backend, user } = setup();
    backend.options.collector = 500;
    await confirmRun(user);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("details are only in the server log");
    expect(alert).not.toHaveTextContent("RuntimeError");
  });

  it("a network failure is not retried and does not refresh until asked", async () => {
    const { backend, user } = setup();
    backend.options.collector = "network";
    await screen.findByText("No collector runs yet");
    const before = historyReads(backend);
    await confirmRun(user);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The run may still have started on the backend");
    expect(alert).toHaveTextContent("It was NOT retried.");
    await new Promise((resolve) => setTimeout(resolve, 300)); // give any (wrong) retry a chance
    expect(posts(backend)).toHaveLength(1);
    expect(historyReads(backend)).toBe(before);

    await user.click(within(alert).getByRole("button", { name: "Refresh history" }));
    await waitFor(() => expect(historyReads(backend)).toBe(before + 1));
    expect(posts(backend)).toHaveLength(1);
  });

  it("a gateway timeout is treated as uncertain and not retried", async () => {
    const { backend, user } = setup();
    server.use(
      http.post(`${API}/collector/runs/`, async ({ request }) => {
        backend.calls.push({ method: "POST", path: "collector/runs/", body: await request.json() });
        return new HttpResponse("Gateway Timeout", { status: 504 });
      }),
    );
    await confirmRun(user);
    expect(await screen.findByRole("alert")).toHaveTextContent("The run may have started and may still be running. It was NOT retried.");
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(posts(backend)).toHaveLength(1);
  });

  it("cannot be submitted twice while a run is in progress", async () => {
    const { backend, user } = setup();
    backend.options.collectorDelay = 300;
    await confirmRun(user);
    expect(await screen.findByText(/Collecting on the backend machine…/)).toBeInTheDocument();
    const review = screen.getByRole("button", { name: "Review and start…" });
    expect(review).toBeDisabled();
    await user.click(review);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await screen.findByText(/Run #\d+ finished\./);
    expect(posts(backend)).toHaveLength(1);
  });

  it("never starts a run on navigation, refresh or profile switching", async () => {
    window.localStorage.setItem(ACTIVE_PROFILE_STORAGE_KEY, "1");
    const { backend, user } = setup({ profiles: [RIYA, SAM], runs: [makeRun(1)] });
    await screen.findByRole("table", { name: "Collector runs" });
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await user.selectOptions(screen.getByLabelText("Profile", { selector: "select.profile-switcher__select" }), "Sam Example");
    expect(await screen.findByLabelText(/^Search keywords/)).toHaveValue("Django Developer");
    await user.click(screen.getByRole("link", { name: "Jobs" }));
    await screen.findByRole("heading", { level: 1, name: "Jobs" });
    await user.click(screen.getByRole("link", { name: "Collector" }));
    await screen.findByRole("heading", { level: 1, name: "Collector" });
    await user.click(screen.getByRole("link", { name: "Run #1" }));
    await screen.findByRole("heading", { level: 1, name: "Collector run #1" });
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    expect(posts(backend)).toHaveLength(0);
  });

  it("can be completed from the keyboard", async () => {
    const { backend, user } = setup();
    const limit = await screen.findByLabelText(/^Maximum posts/);
    limit.focus();
    await user.keyboard("{Enter}"); // submits the form -> confirmation
    const dialog = screen.getByRole("dialog", { name: "Start a LinkedIn collection run?" });
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus(); // the safe choice first
    within(dialog).getByRole("checkbox").focus();
    await user.keyboard(" ");
    await user.tab(); // Cancel
    await user.tab(); // Start collection run
    expect(within(dialog).getByRole("button", { name: "Start collection run" })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(await screen.findByText(/Run #\d+ finished\./)).toBeInTheDocument();
    expect(posts(backend)).toHaveLength(1);
  });
});

describe("helpers", () => {
  it("parses and validates keywords and limits like the backend", () => {
    expect(parseKeywords(" Python Developer ,python developer,\nBackend  Engineer,, ")).toEqual(["Python Developer", "Backend Engineer"]);
    expect(validateRunInput([], "5")).toEqual({ keywords: "Enter at least one keyword." });
    expect(validateRunInput(Array.from({ length: 11 }, (_, i) => `k${i}`), "5").keywords).toBe("Use at most 10 keywords.");
    expect(validateRunInput(["x".repeat(101)], "5").keywords).toMatch(/at most 100 characters/);
    expect(validateRunInput(["a"], "0").limit).toBeDefined();
    expect(validateRunInput(["a"], "2.5").limit).toBeDefined();
    expect(validateRunInput(["a"], "10")).toEqual({});
  });

  it("describeStartError never claims certainty after a network failure", () => {
    expect(describeStartError(new ApiError({ status: 0 })).uncertain).toBe(true);
    expect(describeStartError(new ApiError({ status: 409, extra: { run: 3 } }))).toMatchObject({ runId: 3, uncertain: false });
  });
});
