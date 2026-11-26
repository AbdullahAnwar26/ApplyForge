/**
 * Job Automation: Search -> Collect -> Review -> Generate -> Send, against the fake API.
 * Nothing is collected or sent; the assertions are about which requests the UI makes
 * (and does not make) and what the user sees.
 */

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { validateCollection } from "../../api/collector";
import type { Skill } from "../../api/types";
import { makeApplication } from "../../test/mockApplications";
import { createMockBackend } from "../../test/mockBackend";
import { makeRun } from "../../test/mockCollector";
import { makeJob } from "../../test/mockJobs";
import { makeProfile, renderApp } from "../../test/renderApp";
import { API, server } from "../../test/server";

const RIYA = makeProfile(1, "Riya Fictional", { target_roles: "Python Backend Developer, Django Developer" });
const SKILLS: Skill[] = [{ id: 11, name: "Python", category: "TECHNOLOGY", profile: 1 }];

function setup(initial: Parameters<typeof createMockBackend>[0] = {}, path = "/automation") {
  const backend = createMockBackend({ profiles: [RIYA], skills: SKILLS, ...initial });
  server.use(...backend.handlers);
  renderApp({ path });
  return { backend, user: userEvent.setup() };
}

const starts = (backend: ReturnType<typeof createMockBackend>) =>
  backend.calls.filter((c) => c.method === "POST" && c.path === "collector/runs/");

describe("1. find jobs", () => {
  it("validates like the backend", () => {
    const base = { query: "Python", target: "50", dateRange: "past-week" as const, dateFrom: "", dateTo: "" };
    expect(validateCollection(base, "2026-03-04")).toEqual({});
    expect(validateCollection({ ...base, target: "200" }, "2026-03-04")).toEqual({});
    expect(validateCollection({ ...base, query: "  " }, "2026-03-04").query).toBeTruthy();
    expect(validateCollection({ ...base, target: "0" }, "2026-03-04").target).toBeTruthy();
    expect(validateCollection({ ...base, target: "501" }, "2026-03-04").target).toMatch(/at most 500/);
    const custom = { ...base, dateRange: "custom" as const };
    expect(validateCollection(custom, "2026-03-04").dateFrom).toBeTruthy();
    expect(validateCollection({ ...custom, dateFrom: "2026-03-09" }, "2026-03-04").dateFrom).toMatch(/future/);
    expect(validateCollection({ ...custom, dateFrom: "2026-03-01", dateTo: "2026-02-01" }, "2026-03-04").dateTo).toBeTruthy();
  });

  it("starts one background collection only after confirmation, shows progress and leads to review", async () => {
    const { backend, user } = setup();
    const query = await screen.findByLabelText(/Search for/);
    expect(query).toHaveValue("Python Backend Developer"); // from the profile's target roles
    await user.selectOptions(screen.getByLabelText("Date posted"), "Custom range");
    await user.type(screen.getByLabelText(/From/), "2026-02-20");
    const target = screen.getByLabelText(/Number of jobs/);
    await user.clear(target);
    await user.type(target, "200");
    await user.click(screen.getByRole("button", { name: "Start collecting" }));

    const dialog = screen.getByRole("dialog", { name: "Start collecting from LinkedIn?" });
    expect(dialog).toHaveTextContent("Python Backend Developer");
    expect(dialog).toHaveTextContent("2026-02-20 to today");
    expect(dialog).toHaveTextContent("200");
    const confirm = within(dialog).getByRole("button", { name: "Start collecting" });
    expect(confirm).toBeDisabled();
    expect(starts(backend)).toHaveLength(0);
    await user.click(within(dialog).getByRole("checkbox"));
    await user.click(confirm);

    await waitFor(() => expect(starts(backend)).toHaveLength(1));
    expect(starts(backend)[0]!.body).toEqual({
      query: "Python Backend Developer", target: 200, date_range: "custom", date_from: "2026-02-20", date_to: null, profile: 1,
    });
    // Live progress, then the finished run (the fake finishes on the next poll).
    expect(await screen.findByText(/of 200 jobs ready to apply/)).toBeInTheDocument();
    expect(await screen.findByText("Reached the number of jobs you asked for.", undefined, { timeout: 4000 })).toBeInTheDocument();
    expect(starts(backend)).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: /Pick from 200 jobs/ }));
    expect(await screen.findByRole("heading", { name: "Pick jobs" })).toBeInTheDocument();
    expect(screen.getByText(/From your latest search/)).toBeInTheDocument();
  });

  it("cancels a running collection and offers to continue it", async () => {
    const { backend, user } = setup({
      holdRuns: true,
      runs: [makeRun(41, { status: "RUNNING", limit: 100, remaining: 99, counts: { found: 3, created: 1 }, finished_at: null, stop_reason: "", stage: "Loading more results" })],
    });
    expect(await screen.findByText("Searching now")).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: "Cancel collection" }));
    expect(screen.getByRole("button", { name: "Start collecting" })).toBeDisabled();
    await waitFor(() => expect(backend.calls.some((c) => c.method === "POST" && c.path === "collector/runs/41/cancel/")).toBe(true));

    backend.state.holdRuns = false; // the next poll shows the run cancelled
    expect(await screen.findByText(/You cancelled the collection/, undefined, { timeout: 4000 })).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: /Continue collecting \(98 more\)/ }));
    await waitFor(() => expect(backend.calls.some((c) => c.path === "collector/runs/41/continue/")).toBe(true));
  });

  it("explains a conflict without retrying", async () => {
    const { backend, user } = setup();
    backend.options.collector = 409;
    await user.click(await screen.findByRole("button", { name: "Start collecting" }));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("checkbox"));
    await user.click(within(dialog).getByRole("button", { name: "Start collecting" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Another collection is running on this server");
    expect(starts(backend)).toHaveLength(1);
  });
});

const PICK_JOBS = [
  makeJob(7, {
    title: "Python Backend Developer",
    source_url: "https://www.linkedin.com/feed/update/urn:li:activity:7300000000000000007/",
    contact_summary: { emails: ["riya@fictional-labs.example"], phones: [], whatsapp: ["+44 7700 900123"] },
    matching_resume: { id: 41, name: "Backend CV", reason: "Matches “backend” in the job title" },
    screening_outcome: "SHORTLISTED",
    matched_keywords: ["Python"],
  }),
  makeJob(9, {
    title: "Senior Django Engineer",
    contact_summary: { emails: ["hr@fictional-two.example"], phones: [], whatsapp: [] },
    screening_outcome: "SHORTLISTED",
    matched_keywords: ["Django", "Python"],
  }),
  makeJob(8, { title: "Django Developer", jd_complete: false, contact_summary: { emails: ["x@fictional.example"], phones: [], whatsapp: [] } }),
  makeJob(10, { title: "Marketing Lead", screening_outcome: "REJECTED", contact_summary: { emails: ["m@fictional.example"], phones: [], whatsapp: [] } }),
  makeJob(11, { title: "Java Developer", screening_outcome: "SHORTLISTED" }), // no email or phone in the post
];

describe("2. pick jobs", () => {
  it("lists matching jobs with a contact, best match first and all ticked; writes the applications", async () => {
    const { backend, user } = setup({ jobs: PICK_JOBS }, "/automation?step=pick");
    const list = await screen.findByRole("list", { name: "Matching jobs" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows.map((r) => within(r).getAllByRole("link")[0]!.textContent)).toEqual(["Senior Django Engineer", "Python Backend Developer"]);
    expect(rows[1]).toHaveTextContent("Matches your skills: Python");
    expect(rows[1]).toHaveTextContent("riya@fictional-labs.example");
    expect(rows[1]).toHaveTextContent("WhatsApp +44 7700 900123");
    expect(rows[1]).toHaveTextContent("Resume: Backend CV");
    expect(within(list).getByRole("checkbox", { name: "Pick Python Backend Developer" })).toBeChecked();
    // Cut off, not matching or without a contact: kept aside.
    expect(screen.queryByRole("link", { name: "Django Developer" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Show 3 other posts/ }));
    const others = screen.getByRole("list", { name: "Other jobs" });
    expect(others).toHaveTextContent("Post was cut off");
    expect(others).toHaveTextContent("Does not match your skills");
    expect(others).toHaveTextContent("No email or phone in the post");

    await user.click(within(list).getByRole("checkbox", { name: "Pick Senior Django Engineer" }));
    await user.click(screen.getByRole("button", { name: "Write applications for 1 job" }));
    await waitFor(() => expect(backend.writes("POST", "jobs/select/")).toHaveLength(1));
    expect(backend.writes("POST", "jobs/select/")[0]!.body).toEqual({ jobs: [7], profile: 1 });
    await waitFor(() => expect(backend.calls.filter((c) => c.method === "POST" && c.path.endsWith("/generate/"))).toHaveLength(1));
    // Done: on to Apply.
    expect(await screen.findByRole("heading", { name: "Apply" })).toBeInTheDocument();
  });

  it("shows a failed draft on its own job and still leads on", async () => {
    const { backend, user } = setup({ jobs: [PICK_JOBS[0]!] }, "/automation?step=pick");
    backend.options.generation = 502;
    await user.click(await screen.findByRole("button", { name: "Write applications for 1 job" }));
    expect(await screen.findByText("Wrote 0 of 1 applications.")).toBeInTheDocument();
    expect(screen.getByText("Wrote 0 of 1 applications.").closest("[role=status]")).toHaveTextContent("Python Backend Developer:");
    await user.click(screen.getByRole("button", { name: "Go to Apply →" }));
    expect(await screen.findByRole("heading", { name: "Apply" })).toBeInTheDocument();
  });

  it("waits for the server's per-minute limit instead of failing", async () => {
    const { backend, user } = setup({ jobs: [PICK_JOBS[0]!] }, "/automation?step=pick");
    let limited = false;
    server.use(
      http.post(`${API}/applications/:id/generate/`, () => {
        if (limited) return; // let the normal fake answer
        limited = true;
        return HttpResponse.json({ detail: "Request was throttled." }, { status: 429, headers: { "Retry-After": "1" } });
      }),
    );
    await user.click(await screen.findByRole("button", { name: "Write applications for 1 job" }));
    expect(await screen.findByText(/waiting 1 s for the server's per-minute limit/)).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Apply" }, { timeout: 4000 })).toBeInTheDocument();
    expect(backend.state.applications[0]!.status).toBe("GENERATED");
  });

  it("limits the list to the latest search", async () => {
    const seen: string[] = [];
    server.events.on("request:start", ({ request }) => {
      const url = new URL(request.url);
      if (url.pathname === "/api/v1/jobs/" && url.searchParams.has("run")) seen.push(url.search);
    });
    setup({ jobs: PICK_JOBS, runs: [makeRun(5, { job_ids: [7] })] }, "/automation?step=review&run=5"); // an old link still works
    expect(await screen.findByRole("link", { name: "Python Backend Developer" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Senior Django Engineer" })).not.toBeInTheDocument();
    expect(seen.some((s) => s.includes("run=5") && s.includes("status=DISCOVERED"))).toBe(true);
    server.events.removeAllListeners();
  });
});

describe("3. apply in one click", () => {
  const WRITTEN = makeApplication(80, {
    status: "GENERATED",
    contact: 5,
    resume: 41,
    recipient_email: "riya@fictional-labs.example",
    recipient_phone: "+44 7700 900123",
    generated_subject: "Application for Python Backend Developer",
    generated_body: "Dear Riya,\nI am applying for the Python Backend Developer role.\nRiya Fictional",
    generated_whatsapp_message: "Hi Riya, applying for the Python Backend Developer role.",
  });
  const GMAIL = {
    from_name: "Riya Fictional", from_email: "riya@example.org", smtp_host: "smtp.gmail.com", smtp_port: 587,
    security: "starttls" as const, username: "riya@example.org", imap_host: "", imap_port: 993, drafts_server: "imap.gmail.com",
    drafts_page: "https://mail.google.com/mail/?authuser=riya@example.org#drafts",
    has_password: true, verified_at: null, last_error: "", updated_at: "2026-03-01T10:00:00Z",
  };
  afterEach(() => vi.restoreAllMocks());
  beforeEach(() => window.localStorage.clear()); // "resume already downloaded" is remembered per browser

  function stubTab() {
    const tab = { closed: false, opener: {} as unknown, location: { href: "about:blank" }, close: vi.fn() };
    vi.spyOn(window, "open").mockReturnValue(tab as unknown as Window);
    return tab;
  }

  it("Send email opens Gmail with the finished email; it turns Sent when found in the Sent folder", async () => {
    const tab = stubTab();
    const { backend, user } = setup({ applications: [WRITTEN], sendingAccount: GMAIL }, "/automation?step=apply");
    const card = within((await screen.findByRole("link", { name: "Python Backend Developer" })).closest("li")!);
    expect(await card.findByText("Application for Python Backend Developer")).toBeInTheDocument();
    await user.click(card.getByRole("button", { name: "Read message" }));
    expect(card.getByText(/I am applying for the Python Backend Developer role/)).toBeInTheDocument();

    // One click: no approval step, no dialog.
    await user.click(card.getByRole("button", { name: "Send email" }));
    await waitFor(() => expect(backend.writes("POST", "applications/80/compose/")).toHaveLength(1));
    expect(backend.writes("POST", "applications/80/compose/")[0]!.body).toEqual({ channel: "EMAIL", method: "mailbox", version: "" });
    await waitFor(() => expect(tab.location.href).toBe(GMAIL.drafts_page));
    expect(await card.findByText(/Opened in your email/)).toBeInTheDocument();
    expect(backend.state.applications[0]!.status).toBe("COMPOSE_OPENED"); // opened, not sent
    expect(backend.writes("POST", "applications/80/send/")).toHaveLength(0);

    // Back from Gmail after pressing Send there: checked by itself.
    backend.state.sentFolderHasDraft = true;
    window.dispatchEvent(new Event("focus"));
    await waitFor(() => expect(backend.writes("POST", "applications/80/compose/verify/").length).toBeGreaterThan(0));
    await waitFor(() => expect(backend.state.applications[0]!.status).toBe("SENT"));
  });

  it("Send WhatsApp downloads the resume and opens the chat with the message; I sent it marks it sent", async () => {
    const tab = stubTab();
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    const { backend, user } = setup({ applications: [WRITTEN], sendingAccount: GMAIL }, "/automation?step=apply");
    const card = within((await screen.findByRole("link", { name: "Python Backend Developer" })).closest("li")!);
    await user.click(await card.findByRole("button", { name: "Send WhatsApp" }));
    expect((click.mock.contexts[0] as HTMLAnchorElement).href).toBe(`${API}/resumes/41/download/`);
    await waitFor(() => expect(tab.location.href).toMatch(/^https:\/\/wa\.me\/447700900123\?text=Hi%20Riya/));
    expect(await card.findByText(/attach your resume from your Downloads folder/)).toBeInTheDocument();
    await user.click(card.getByRole("button", { name: "I sent it" }));
    await waitFor(() => expect(backend.state.applications[0]!.status).toBe("SENT"));
    expect(backend.state.applications[0]!.channel).toBe("WHATSAPP");
  });

  it("downloads the resume only the first time, and shows the exact number with its country code", async () => {
    stubTab();
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    const inferred = { ...WRITTEN, recipient_phone: "98765 43210", whatsapp: { number: "+919876543210", note: "The number has no country code: +91 (India) was used because the job is in India.", problem: "" } };
    const { backend, user } = setup({ applications: [inferred], sendingAccount: GMAIL }, "/automation?step=apply");
    const card = within((await screen.findByRole("link", { name: "Python Backend Developer" })).closest("li")!);
    expect(await card.findByText(/to \+919876543210/)).toBeInTheDocument();
    expect(card.getByText(/\+91 \(India\) was used/)).toBeInTheDocument();
    await user.click(card.getByRole("button", { name: "Send WhatsApp" }));
    await card.findByRole("button", { name: "Not sent" });
    expect(click).toHaveBeenCalledTimes(1);
    await user.click(card.getByRole("button", { name: "Not sent" }));
    await user.click(await card.findByRole("button", { name: "Send WhatsApp" }));
    await card.findByRole("button", { name: "Not sent" });
    expect(click).toHaveBeenCalledTimes(1); // not downloaded again
    expect(card.getByRole("link", { name: "Download resume again" })).toHaveAttribute("href", `${API}/resumes/41/download/`);
    expect(backend.calls.filter((c) => c.method === "POST" && c.path === "applications/80/compose/")).toHaveLength(2);
  });

  it("warns before sending when the saved Gmail password is not an app password", async () => {
    setup({ applications: [WRITTEN], sendingAccount: { ...GMAIL, app_password_ok: false } }, "/automation?step=apply");
    const card = within((await screen.findByRole("link", { name: "Python Backend Developer" })).closest("li")!);
    expect(await card.findByRole("link", { name: "Replace it with an app password" })).toHaveAttribute("href", "/account#email-sending");
    expect(card.queryByRole("button", { name: "Send email" })).not.toBeInTheDocument();
    expect(card.getByRole("button", { name: "Send WhatsApp" })).toBeInTheDocument(); // WhatsApp needs no password
  });

  it("asks for the country code instead of guessing", async () => {
    const unknown = { ...WRITTEN, recipient_phone: "98765 43210", whatsapp: { number: "", note: "", problem: "The number has no country code." } };
    setup({ applications: [unknown], sendingAccount: GMAIL }, "/automation?step=apply");
    const card = within((await screen.findByRole("link", { name: "Python Backend Developer" })).closest("li")!);
    expect(await card.findByRole("link", { name: "Set your country code" })).toHaveAttribute("href", "/profile");
    expect(card.queryByRole("button", { name: "Send WhatsApp" })).not.toBeInTheDocument();
  });

  it("no WhatsApp button without a number, and asks to connect Gmail once", async () => {
    const noPhone = { ...WRITTEN, recipient_phone: "" };
    setup({ applications: [noPhone] }, "/automation?step=apply");
    const card = within((await screen.findByRole("link", { name: "Python Backend Developer" })).closest("li")!);
    expect(await card.findByRole("link", { name: "connect your Gmail once" })).toHaveAttribute("href", "/account#email-sending");
    expect(card.queryByRole("button", { name: "Send WhatsApp" })).not.toBeInTheDocument();
  });
});

describe("application page: full send panel", () => {
  const DRAFTED = makeApplication(70, {
    status: "GENERATED",
    contact: 5,
    resume: 41,
    generated_subject: "Application for Python Backend Developer",
    generated_body: "Dear Riya,\nI am applying for the Python Backend Developer role.\nRiya Fictional",
    generated_whatsapp_message: "Hi Riya, applying for the Python Backend Developer role.",
    generated_at: "2026-03-03T10:00:00Z",
    generation_notes: { gaps: ["Kubernetes"], unsupported_terms: ["aws"], resume_name: "Backend CV" },
  });
  const APPROVED = { ...DRAFTED, status: "READY_TO_SEND" as const };
  const ACCOUNT = {
    from_name: "Riya Fictional", from_email: "riya@example.org", smtp_host: "smtp.gmail.com", smtp_port: 587,
    security: "starttls" as const, username: "riya@example.org", imap_host: "", imap_port: 993, drafts_server: "imap.gmail.com",
    drafts_page: "https://mail.google.com/mail/?authuser=riya@example.org#drafts",
    has_password: true, verified_at: null, last_error: "", updated_at: "2026-03-01T10:00:00Z",
  };

  /** window.open is stubbed: the reserved tab records where it was sent. */
  function stubWindow() {
    const tab = { closed: false, opener: {} as unknown, location: { href: "about:blank" }, close: vi.fn() };
    const open = vi.spyOn(window, "open").mockReturnValue(tab as unknown as Window);
    return { tab, open };
  }
  afterEach(() => vi.restoreAllMocks());

  const group = (panel: HTMLElement, name: "Email" | "WhatsApp") => within(within(panel).getByRole("group", { name }));

  it("opens the approved email as a draft in the user's mailbox; it is sent only when found in the Sent folder", async () => {
    const { tab, open } = stubWindow();
    const { backend, user } = setup({ applications: [DRAFTED], sendingAccount: ACCOUNT, jobs: [makeJob(7)] }, "/applications/70");
    const panel = await screen.findByRole("region", { name: "Send" });
    expect(screen.getByRole("region", { name: "Draft" })).toHaveTextContent("the draft mentions aws");
    expect(group(panel, "Email").getByRole("button", { name: "Send Email…" })).toBeDisabled();
    expect(group(panel, "Email").getByText("Approve the drafts first.")).toBeInTheDocument();
    await user.click(within(panel).getByRole("button", { name: "Approve drafts" }));
    await waitFor(() => expect(backend.writes("PATCH", "applications/70/")[0]!.body).toMatchObject({ status: "READY_TO_SEND" }));

    await waitFor(() => expect(group(panel, "Email").getByRole("button", { name: "Send Email…" })).toBeEnabled());
    await user.click(group(panel, "Email").getByRole("button", { name: "Send Email…" }));
    const dialog = await screen.findByRole("dialog", { name: "Send by email" });
    expect(await within(dialog).findByText("riya@fictional-labs.example")).toBeInTheDocument();
    expect(dialog).toHaveTextContent("found in the post");
    expect(dialog).toHaveTextContent("Maya_Example_Resume.pdf");
    expect(dialog).toHaveTextContent("Application for Python Backend Developer");
    await user.click(within(dialog).getByRole("button", { name: "Open as a draft in my email" }));

    await waitFor(() => expect(backend.writes("POST", "applications/70/compose/")).toHaveLength(1));
    expect(backend.writes("POST", "applications/70/compose/")[0]!.body).toEqual({
      channel: "EMAIL", method: "mailbox", version: expect.stringMatching(/^v-70-READY_TO_SEND-/),
    });
    expect(open).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(tab.location.href).toBe(ACCOUNT.drafts_page));
    expect(tab.opener).toBeNull();
    // Opened is not sent.
    expect(await group(panel, "Email").findByText("Opened, not sent yet")).toBeInTheDocument();
    expect(backend.state.applications[0]!.status).toBe("COMPOSE_OPENED");
    expect(backend.state.applications[0]!.sent_at).toBeNull();
    expect(backend.writes("POST", "applications/70/send/")).toHaveLength(0);
    expect(group(panel, "Email").getByRole("link", { name: "Open my drafts ↗" })).toHaveAttribute("href", ACCOUNT.drafts_page);
    // WhatsApp waits until the email is finished.
    expect(group(panel, "WhatsApp").getByRole("button", { name: "Send WhatsApp…" })).toBeDisabled();

    await user.click(group(panel, "Email").getByRole("button", { name: "Check my Sent folder" }));
    expect(await screen.findByText(/No email to riya@fictional-labs.example sent after/)).toBeInTheDocument();
    expect(backend.state.applications[0]!.status).toBe("COMPOSE_OPENED");

    backend.state.sentFolderHasDraft = true; // the user pressed Send in Gmail
    await user.click(group(panel, "Email").getByRole("button", { name: "Check my Sent folder" }));
    expect(await group(panel, "Email").findByText(/found in your Sent folder/)).toBeInTheDocument();
    expect(backend.state.applications[0]!.status).toBe("SENT");
    expect(await within(panel).findByText("Sent (found in your Sent folder)")).toBeInTheDocument(); // history
  });

  it("a downloaded draft is confirmed by the user, once; an unsent draft can be opened again", async () => {
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    const open = vi.spyOn(window, "open");
    const { backend, user } = setup({ applications: [APPROVED], sendingAccount: ACCOUNT, jobs: [makeJob(7)] }, "/applications/70");
    const panel = await screen.findByRole("region", { name: "Send" });
    await user.click(group(panel, "Email").getByRole("button", { name: "Send Email…" }));
    const dialog = await screen.findByRole("dialog", { name: "Send by email" });
    await user.click(await within(dialog).findByRole("button", { name: "Download draft (.eml)" }));
    await waitFor(() => expect(click).toHaveBeenCalled());
    const anchor = click.mock.contexts[0] as HTMLAnchorElement;
    expect(anchor.href).toMatch(new RegExp(`${API}/applications/70/compose/draft/\\?record=\\d+$`));
    expect(open).not.toHaveBeenCalled(); // a file, not a page

    // Not sent after all: back to approved, and it can be opened again.
    await user.click(await group(panel, "Email").findByRole("button", { name: "I didn't send it" }));
    await waitFor(() => expect(backend.state.applications[0]!.status).toBe("READY_TO_SEND"));
    await user.click(await group(panel, "Email").findByRole("button", { name: "Send Email…" }));
    const again = await screen.findByRole("dialog", { name: "Send by email" });
    await user.click(await within(again).findByRole("button", { name: "Download draft (.eml)" }));
    await user.click(await group(panel, "Email").findByRole("button", { name: "I sent it" }));
    const confirm = screen.getByRole("dialog", { name: "Did you send the email?" });
    await user.click(within(confirm).getByRole("button", { name: "Yes, I sent it" }));
    await waitFor(() => expect(backend.writes("POST", "applications/70/compose/confirm/")).toHaveLength(1));
    const records = backend.state.sendRecords[70]!;
    expect(backend.writes("POST", "applications/70/compose/confirm/")[0]!.body).toEqual({ record: records[0]!.id, confirm: true });
    expect(await group(panel, "Email").findByText(/confirmed by you/)).toBeInTheDocument();
    expect(records.map((r) => r.status)).toEqual(["CONFIRMED", "ABANDONED"]);
    expect(group(panel, "Email").queryByRole("button", { name: "I sent it" })).not.toBeInTheDocument();
  });

  it("WhatsApp opens with the message; the resume is attached by hand; sent after confirmation", async () => {
    const { tab } = stubWindow();
    const { backend, user } = setup({ applications: [APPROVED], jobs: [makeJob(7)] }, "/applications/70");
    const panel = await screen.findByRole("region", { name: "Send" });
    await user.click(group(panel, "WhatsApp").getByRole("button", { name: "Send WhatsApp…" }));
    const dialog = await screen.findByRole("dialog", { name: "Send by WhatsApp" });
    expect(await within(dialog).findByRole("link", { name: "Download resume" })).toHaveAttribute("href", `${API}/resumes/41/download/`);
    expect(dialog).toHaveTextContent("attach your resume in WhatsApp");
    await user.click(within(dialog).getByRole("button", { name: "Open WhatsApp" }));
    await waitFor(() => expect(tab.location.href).toMatch(/^https:\/\/wa\.me\/447700900123\?text=Hi%20Riya/));
    expect(backend.writes("POST", "applications/70/compose/")[0]!.body).toMatchObject({ channel: "WHATSAPP", method: "link" });
    expect(await group(panel, "WhatsApp").findByRole("link", { name: "Open WhatsApp again ↗" })).toHaveAttribute(
      "href", expect.stringMatching(/^https:\/\/wa\.me\//),
    );
    expect(backend.state.applications[0]!.status).toBe("COMPOSE_OPENED");
    await user.click(group(panel, "WhatsApp").getByRole("button", { name: "I sent it" }));
    await user.click(within(screen.getByRole("dialog", { name: "Did you send the WhatsApp message?" })).getByRole("button", { name: "Yes, I sent it" }));
    await waitFor(() => expect(backend.state.applications[0]!.status).toBe("SENT"));
    expect(backend.state.applications[0]!.channel).toBe("WHATSAPP");
    // The email can still be sent once; without an account the dialog says how to connect it.
    await user.click(await group(panel, "Email").findByRole("button", { name: "Send Email…" }));
    const emailDialog = await screen.findByRole("dialog", { name: "Send by email" });
    expect(await within(emailDialog).findByRole("link", { name: "Connect it now" })).toHaveAttribute("href", "/account#email-sending");
    expect(within(emailDialog).getByRole("button", { name: "Open as a draft in my email" })).toBeDisabled();
    expect(backend.writes("POST", "applications/70/send/")).toHaveLength(0);
  });

  it("direct sending from ApplyForge still needs the explicit checkbox and the previewed version", async () => {
    const { backend, user } = setup({ applications: [APPROVED], sendingAccount: ACCOUNT, jobs: [makeJob(7)] }, "/applications/70");
    const panel = await screen.findByRole("region", { name: "Send" });
    await user.click(group(panel, "Email").getByRole("button", { name: "Send Email…" }));
    const dialog = await screen.findByRole("dialog", { name: "Send by email" });
    await user.click(await within(dialog).findByText("Or send it directly from ApplyForge"));
    const now = within(dialog).getByRole("button", { name: "Send email now" });
    expect(now).toBeDisabled();
    await user.click(within(dialog).getByRole("checkbox"));
    await user.click(now);
    await waitFor(() => expect(backend.writes("POST", "applications/70/send/")).toHaveLength(1));
    expect(backend.writes("POST", "applications/70/send/")[0]!.body).toEqual({
      channel: "EMAIL", version: expect.stringMatching(/^v-70-READY_TO_SEND-/), confirm: true,
    });
    expect(await group(panel, "Email").findByText(/sent by ApplyForge/)).toBeInTheDocument();
  });
});

describe("email account", () => {
  it("saves the mailbox without ever showing the password, and sends a test email", async () => {
    const { backend, user } = setup({ sendingMode: "filebased" }, "/account");
    const section = await screen.findByRole("region", { name: "Email sending" });
    expect(await within(section).findByText(/Test mode/)).toBeInTheDocument();
    await user.click(within(section).getByRole("button", { name: "Gmail" }));
    await user.type(within(section).getByLabelText(/Your email address/), "riya@example.org");
    await user.type(within(section).getByLabelText(/App password/), "fictional-app-password");
    await user.click(within(section).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(backend.writes("PUT", "sending/email-account/")).toHaveLength(1));
    expect(backend.writes("PUT", "sending/email-account/")[0]!.body).toMatchObject({
      from_email: "riya@example.org", smtp_host: "smtp.gmail.com", smtp_port: 587, security: "starttls", username: "riya@example.org", password: "fictional-app-password",
    });
    expect(await within(section).findByLabelText(/New app password/)).toHaveValue("");
    await user.click(within(section).getByRole("button", { name: "Send a test email to myself" }));
    expect(await within(section).findByText(/Working: test email sent/)).toBeInTheDocument();
    expect(await screen.findByText(/Drafts folder found: Drafts/)).toBeInTheDocument();
  });
});
