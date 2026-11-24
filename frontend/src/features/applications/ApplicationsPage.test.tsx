import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { afterEach, describe, expect, it } from "vitest";

import type { ApplicationDetail, Company } from "../../api/types";
import { ACTIVE_PROFILE_STORAGE_KEY } from "../../profile/ActiveProfileProvider";
import { makeApplication } from "../../test/mockApplications";
import { createMockBackend } from "../../test/mockBackend";
import { makeProfile, renderApp } from "../../test/renderApp";
import { API, server } from "../../test/server";
import { readApplicationParams, writeApplicationParams } from "./ApplicationsPage";

const RIYA = makeProfile(1, "Riya Fictional");
const SAM = makeProfile(2, "Sam Example");
const ACME: Company = { id: 1, name: "Acme Fictional Labs", website: null, created_at: "", updated_at: "" };
const GLOBEX: Company = { id: 2, name: "Globex Example", website: null, created_at: "", updated_at: "" };

const job = (id: number, title: string, company: Company) => ({
  id,
  title,
  company: { id: company.id, name: company.name },
  status: "SHORTLISTED" as const,
  screening_outcome: "SHORTLISTED" as const,
});

function many(count: number, profile = 1): ApplicationDetail[] {
  return Array.from({ length: count }, (_, i) =>
    makeApplication(100 + i, { profile, job: job(200 + i, `Fictional Role ${i + 1}`, ACME) }),
  );
}

const seen: URLSearchParams[] = [];
function watchList() {
  seen.length = 0;
  server.events.on("request:start", ({ request }) => {
    const url = new URL(request.url);
    if (url.pathname === "/api/v1/applications/") seen.push(url.searchParams);
  });
}
afterEach(() => server.events.removeAllListeners());

function setup(initial: Parameters<typeof createMockBackend>[0] = {}, path = "/applications") {
  const backend = createMockBackend({ profiles: [RIYA], companies: [ACME, GLOBEX], ...initial });
  server.use(...backend.handlers);
  watchList();
  const utils = renderApp({ path });
  return { backend, user: userEvent.setup(), ...utils };
}

describe("summary", () => {
  it("shows the counts from the summary endpoint and filters from them", async () => {
    const { backend, user } = setup({
      applications: [
        makeApplication(1, { status: "GENERATED", job: job(11, "Python Backend Developer", ACME) }),
        makeApplication(2, { status: "SENT", sent_at: "2026-03-02T00:00:00Z", job: job(12, "Django Developer", GLOBEX) }),
        makeApplication(3, { status: "SHORTLISTED", job: job(13, "Data Engineer", ACME) }),
        makeApplication(4, { profile: 2, status: "SKIPPED", job: job(14, "Other Profile Role", ACME) }),
      ],
    });
    const summary = await screen.findByRole("region", { name: "Summary" });
    await waitFor(() => expect(within(summary).getByRole("button", { name: /Applications/ })).toHaveTextContent("3"));
    expect(within(summary).getByRole("button", { name: /Waiting for you/ })).toHaveTextContent("2");
    const byStatus = within(summary).getByRole("list", { name: "By status" });
    expect(within(byStatus).getAllByRole("button").map((b) => b.textContent)).toEqual(["Shortlisted1", "Draft ready1", "Sent1"]);
    expect(backend.calls.some((c) => c.path === "applications/summary/")).toBe(true);

    await user.click(within(summary).getByRole("button", { name: /Waiting for you/ }));
    await waitFor(() => expect(seen.at(-1)?.get("awaiting_action")).toBe("true"));
    expect(screen.getByRole("checkbox", { name: "Only those waiting for me" })).toBeChecked();

    await user.click(within(byStatus).getByRole("button", { name: /Sent/ }));
    await waitFor(() => expect(seen.at(-1)?.get("status")).toBe("SENT"));
    expect(seen.at(-1)?.get("awaiting_action")).toBeNull();
  });

  it("shows a summary error with retry, while the list still works", async () => {
    let fail = true;
    const backend = createMockBackend({ profiles: [RIYA], applications: many(1) });
    server.use(...backend.handlers);
    server.use(
      http.get(`${API}/applications/summary/`, () =>
        fail ? HttpResponse.json({ detail: "Summary unavailable." }, { status: 400 }) : HttpResponse.json({ total: 1, by_status: { SHORTLISTED: 1 }, awaiting_action: { count: 1, statuses: [], oldest: [] }, recent: [] }),
      ),
    );
    renderApp({ path: "/applications" });
    expect(await screen.findByText("Could not load the summary")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Fictional Role 1" })).toBeInTheDocument();
    fail = false;
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("button", { name: /Waiting for you/ })).toBeInTheDocument();
  });
});

describe("application list", () => {
  it("lists the active profile's applications with job, status, channel and draft", async () => {
    setup({
      applications: [
        makeApplication(1, { status: "GENERATED", generated_subject: "Application for Python Backend Developer", job: job(11, "Python Backend Developer", ACME) }),
        makeApplication(2, { status: "SHORTLISTED", channel: "WHATSAPP", job: job(12, "Django Developer", GLOBEX) }),
      ],
    });
    const table = await screen.findByRole("table", { name: "Applications" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(within(rows[0]!).getByRole("link", { name: "Python Backend Developer" })).toHaveAttribute("href", "/applications/1");
    expect(rows[0]).toHaveTextContent("Acme Fictional Labs");
    expect(within(rows[0]!).getByText("Draft ready")).toHaveClass("badge--accent");
    expect(rows[0]).toHaveTextContent("Application for Python Backend Developer");
    expect(rows[1]).toHaveTextContent("WhatsApp");
    expect(rows[1]).toHaveTextContent("No draft yet");
    expect(Object.fromEntries(seen.at(-1)!)).toEqual({ profile: "1", page: "1", page_size: "25" });
  });

  it("pages through results", async () => {
    const { user } = setup({ applications: many(30) });
    expect(await screen.findByText("1–25 of 30")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("26–30 of 30")).toBeInTheDocument();
    expect(seen.at(-1)?.get("page")).toBe("2");
  });

  it("sends only supported filters, from the controls and the URL", async () => {
    const { user } = setup({ applications: many(2) }, "/applications?channel=EMAIL&status=NOPE");
    await screen.findByRole("table", { name: "Applications" });
    expect(Object.fromEntries(seen.at(-1)!)).toEqual({ profile: "1", channel: "EMAIL", page: "1", page_size: "25" });

    await user.selectOptions(screen.getByLabelText("Status"), "Approved");
    await user.selectOptions(screen.getByLabelText("Company"), "Globex Example");
    await user.click(screen.getByRole("checkbox", { name: "Only those waiting for me" }));
    await waitFor(() =>
      expect(Object.fromEntries(seen.at(-1)!)).toEqual({
        profile: "1",
        status: "READY_TO_SEND",
        channel: "EMAIL",
        company: "2",
        awaiting_action: "true",
        page: "1",
        page_size: "25",
      }),
    );
    expect(await screen.findByText("No applications match these filters")).toBeInTheDocument();
    await user.click(screen.getAllByRole("button", { name: "Clear filters" })[0]!);
    await waitFor(() => expect(Object.fromEntries(seen.at(-1)!)).toEqual({ profile: "1", page: "1", page_size: "25" }));
  });

  it("explains an empty list and points to shortlisted jobs", async () => {
    setup();
    expect(await screen.findByText("No applications yet")).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Jobs" }).some((l) => l.getAttribute("href") === "/jobs?status=SHORTLISTED")).toBe(true);
  });

  it("shows loading, then a retryable error", async () => {
    let fail = true;
    const backend = createMockBackend({ profiles: [RIYA] });
    server.use(...backend.handlers);
    server.use(
      http.get(`${API}/applications/`, async () => {
        await delay(50);
        return fail ? HttpResponse.json({ detail: "Database is locked." }, { status: 400 }) : HttpResponse.json({ count: 0, next: null, previous: null, results: [] });
      }),
    );
    renderApp({ path: "/applications" });
    expect(await screen.findByRole("status", { name: "Loading applications" })).toBeInTheDocument();
    expect(await screen.findByText("Could not load applications")).toBeInTheDocument();
    fail = false;
    await userEvent.setup().click(screen.getAllByRole("button", { name: "Try again" }).at(-1)!);
    expect(await screen.findByText("No applications yet")).toBeInTheDocument();
  });

  it("follows the active profile", async () => {
    window.localStorage.setItem(ACTIVE_PROFILE_STORAGE_KEY, "1");
    const { user, backend } = setup({ profiles: [RIYA, SAM], applications: [...many(1, 1), makeApplication(300, { profile: 2, job: job(301, "Sam's Role", GLOBEX) })] });
    expect(await screen.findByRole("link", { name: "Fictional Role 1" })).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Profile", { selector: "select.profile-switcher__select" }), "Sam Example");
    expect(await screen.findByRole("link", { name: "Sam's Role" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Fictional Role 1" })).not.toBeInTheDocument();
    expect(seen.at(-1)?.get("profile")).toBe("2");
    expect(backend.calls.some((c) => c.path === "applications/summary/")).toBe(true);
    expect(backend.calls.filter((c) => c.method === "POST")).toHaveLength(0); // nothing generated
  });

  it("opens an application from the keyboard", async () => {
    const { user } = setup({ applications: many(1) });
    const link = await screen.findByRole("link", { name: "Fictional Role 1" });
    link.focus();
    await user.keyboard("{Enter}");
    expect(await screen.findByRole("heading", { level: 1, name: "Fictional Role 1" })).toBeInTheDocument();
  });
});

describe("URL params", () => {
  it("round-trips and resets the page on filter changes", () => {
    const params = readApplicationParams(new URLSearchParams("status=SENT&channel=WHATSAPP&company=4&awaiting=1&page=3"));
    expect(params).toEqual({ status: "SENT", channel: "WHATSAPP", company: 4, awaiting: true, due: false, page: 3 });
    expect(writeApplicationParams(params, { channel: undefined }).toString()).toBe("status=SENT&company=4&awaiting=1");
    const due = readApplicationParams(new URLSearchParams("due=1&status=INTERVIEW"));
    expect(due).toMatchObject({ due: true, status: "INTERVIEW" });
    expect(writeApplicationParams(due, { status: undefined }).toString()).toBe("due=1");
    expect(writeApplicationParams(params, { page: 2 }).get("page")).toBe("2");
  });
});
