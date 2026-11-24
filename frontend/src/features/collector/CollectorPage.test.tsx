/**
 * Collection history and run detail. Every request is answered by the fake API
 * (test/mockCollector.ts); nothing is collected and LinkedIn is never contacted. These
 * pages only read; collections are started from Job Automation (AutomationPage.test).
 */

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it } from "vitest";

import type { Skill } from "../../api/types";
import { createMockBackend } from "../../test/mockBackend";
import { makeItem, makeRun } from "../../test/mockCollector";
import { makeJob } from "../../test/mockJobs";
import { makeProfile, renderApp } from "../../test/renderApp";
import { API, server } from "../../test/server";

const RIYA = makeProfile(1, "Riya Fictional", { target_roles: "Python Developer, Backend Engineer" });
const SKILLS: Skill[] = [{ id: 11, name: "Python", category: "TECHNOLOGY", profile: 1 }];
const JOBS = [makeJob(7, { title: "Python Backend Developer" })];

afterEach(() => server.events.removeAllListeners());

function setup(initial: Parameters<typeof createMockBackend>[0] = {}, path = "/collector") {
  const backend = createMockBackend({ profiles: [RIYA], skills: SKILLS, jobs: JOBS, ...initial });
  server.use(...backend.handlers);
  const utils = renderApp({ path });
  return { backend, user: userEvent.setup(), ...utils };
}

const posts = (backend: ReturnType<typeof createMockBackend>) => backend.calls.filter((c) => c.method === "POST");

describe("collection history", () => {
  it("lists runs, points to Job Automation for starting, and starts nothing", async () => {
    const { backend } = setup({
      runs: [
        makeRun(3, { status: "PARTIAL", search_query: "Python Backend Developer", limit: 50, counts: { found: 4, created: 2, existing: 1, skipped: 1, invalid: 0, failed: 0, out_of_range: 0 } }),
        makeRun(2, { status: "FAILED", trigger: "command", counts: {}, job_ids: [] }),
      ],
    });
    const table = await screen.findByRole("table", { name: "Collector runs" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(within(rows[0]!).getByRole("link", { name: "Run #3" })).toHaveAttribute("href", "/collector/runs/3");
    expect(rows[0]).toHaveTextContent("Python Backend Developer");
    expect(rows[0]).toHaveTextContent("50");
    expect(rows[0]).toHaveTextContent("2 new · 1 duplicates · 4 checked");
    expect(rows[1]).toHaveTextContent("Command line");
    expect(within(screen.getByRole("main")).getByRole("link", { name: "Job Automation" })).toHaveAttribute("href", "/automation");
    expect(screen.queryByRole("button", { name: /start/i })).not.toBeInTheDocument();
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
    await user.selectOptions(within(filters).getByLabelText("Status"), "Cancelled");
    await user.selectOptions(within(filters).getByLabelText("Started from"), "Command line");
    await user.click(within(filters).getByRole("checkbox", { name: "Only runs for Riya Fictional" }));
    await waitFor(() =>
      expect(Object.fromEntries(seen.at(-1)!)).toEqual({ status: "CANCELLED", trigger: "command", profile: "1", page: "1", page_size: "20" }),
    );
    expect(await screen.findByText("No runs match these filters")).toBeInTheDocument();
  });

  it("links a running collection", async () => {
    setup({ holdRuns: true, runs: [makeRun(41, { status: "RUNNING", finished_at: null, duration_seconds: null })] });
    const note = await screen.findByRole("note");
    expect(note).toHaveTextContent("Collection #41 is running now.");
  });
});

describe("run detail", () => {
  it("shows progress, settings and every examined post", async () => {
    setup(
      {
        runs: [
          makeRun(5, {
            status: "INTERRUPTED",
            stop_reason: "interrupted",
            search_query: "Python Backend Developer",
            date_range: "custom",
            date_from: "2026-02-20",
            date_to: "2026-03-01",
            limit: 50,
            remaining: 48,
            counts: { found: 6, created: 2, existing: 1, skipped: 1, invalid: 1, failed: 0, out_of_range: 1 },
            error: "The collection stopped unexpectedly (the server was restarted or stopped).",
            duration_seconds: 125,
          }),
        ],
        runItems: {
          5: [
            makeItem(1, { status: "created", title: "Python Backend Developer", job: 7 }),
            makeItem(2, { status: "existing", title: "Django Developer", job: 8, truncated: true }),
            makeItem(3, { status: "skipped", title: "", job: null, reason: "Job-seeker post (someone looking for work), not a job opening." }),
          ],
        },
      },
      "/collector/runs/5",
    );
    expect(await screen.findByRole("heading", { level: 1, name: "Collection #5" })).toBeInTheDocument();
    const progress = screen.getByRole("region", { name: "Progress" });
    expect(await within(progress).findByText("2 of 50 jobs ready to apply")).toBeInTheDocument();
    expect(progress).toHaveTextContent("The server stopped during the collection.");
    expect(within(progress).getByRole("button", { name: "Continue collecting (48 more)" })).toBeInTheDocument();
    const settings = screen.getByRole("region", { name: "Settings" });
    expect(settings).toHaveTextContent("Custom range (2026-02-20 to 2026-03-01)");
    expect(settings).toHaveTextContent("Riya Fictional");
    const items = screen.getByRole("region", { name: "Posts examined" });
    expect(await within(items).findByRole("link", { name: "Python Backend Developer" })).toHaveAttribute("href", "/jobs/7");
    const list = within(items).getByRole("list");
    expect(within(list).getByText("Duplicate")).toBeInTheDocument();
    expect(within(list).getByText("cut off")).toBeInTheDocument();
    expect(within(list).getByText(/Job-seeker post/)).toBeInTheDocument();
  });

  it("reports a run that does not exist", async () => {
    setup({}, "/collector/runs/77");
    expect(await screen.findByRole("heading", { level: 1, name: "Run not found" })).toBeInTheDocument();
  });
});
