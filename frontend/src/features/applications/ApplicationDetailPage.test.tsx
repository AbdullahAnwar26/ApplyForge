import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import { allowedStatusChanges } from "../../api/applications";
import type { ApplicationDetail, Contact, JobDetail, Skill } from "../../api/types";
import { ACTIVE_PROFILE_STORAGE_KEY } from "../../profile/ActiveProfileProvider";
import { GENERATED_DRAFT, makeApplication } from "../../test/mockApplications";
import { createMockBackend, makeResume } from "../../test/mockBackend";
import { makeJob } from "../../test/mockJobs";
import { makeProfile, renderApp } from "../../test/renderApp";
import { API, server } from "../../test/server";
import { describeGenerationError } from "./generationErrors";
import { ApiError } from "../../api/errors";

const RIYA = makeProfile(1, "Riya Fictional");
const SAM = makeProfile(2, "Sam Example");
const SKILLS: Skill[] = [{ id: 11, name: "Python", category: "TECHNOLOGY", profile: 1 }];
const RECRUITER: Contact = { id: 31, name: "Fictional Recruiter", email: "talent@acme.example", phone: null, whatsapp: null, company: 1, created_at: "", updated_at: "" };
const JOB: JobDetail = makeJob(7, { title: "Python Backend Developer", status: "SHORTLISTED", screening_outcome: "SHORTLISTED", contacts: [RECRUITER] });
const SAVED_DRAFT = {
  generated_subject: "My saved subject",
  generated_body: "My saved body.",
  generated_whatsapp_message: "My saved WhatsApp text.",
};

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
    jobs: [JOB],
    resumes: [makeResume(5, 1, { name: "Backend CV", is_default: true }), makeResume(6, 1, { name: "Data CV" })],
    applications: [app()],
    events: {
      50: [{ id: 2, status: "SHORTLISTED", notes: "Application created.", timestamp: "2026-03-01T09:00:00Z" }],
    },
    ...initial,
  });
  server.use(...backend.handlers);
  const utils = renderApp({ path });
  return { backend, user: userEvent.setup(), ...utils };
}

const panel = (name: string) => screen.getByRole("region", { name });
const generations = (backend: ReturnType<typeof createMockBackend>) => backend.writes("POST", "applications/50/generate/");
const field = (label: string) => within(panel("Draft")).getByLabelText(new RegExp(`^${label}`));

describe("application detail", () => {
  it("shows the job, references, timestamps and history - and generates nothing on load", async () => {
    const { backend } = setup({
      applications: [app({ status: "OPENED", opened_at: "2026-03-02T10:00:00Z", last_error: "Ollama did not respond within 180 seconds." })],
      events: {
        50: [
          { id: 3, status: "OPENED", notes: "Status changed from SHORTLISTED to OPENED.", timestamp: "2026-03-02T10:00:00Z" },
          { id: 2, status: "SHORTLISTED", notes: "Application created.", timestamp: "2026-03-01T09:00:00Z" },
        ],
      },
    });
    expect(await screen.findByRole("heading", { level: 1, name: "Python Backend Developer" })).toBeInTheDocument();
    const overview = panel("Overview");
    expect(within(overview).getByRole("link", { name: "Python Backend Developer" })).toHaveAttribute("href", "/jobs/7");
    expect(overview).toHaveTextContent("Acme Fictional Labs");
    expect(overview).toHaveTextContent("Riya Fictional");
    expect(await within(overview).findByText("Fictional Recruiter - talent@acme.example")).toBeInTheDocument();
    expect(await within(overview).findByText("Backend CV")).toBeInTheDocument();
    expect(overview).toHaveTextContent("The last draft generation failed. Earlier drafts were kept.");
    // The backend text is available, collapsed, under "Technical details".
    expect(within(overview).getByText("Technical details")).toBeInTheDocument();
    expect(within(overview).getByText("Ollama did not respond within 180 seconds.")).toBeInTheDocument();
    expect(within(overview).getAllByText("Opened").some((el) => el.classList.contains("badge"))).toBe(true);

    const history = panel("History");
    const items = within(history).getAllByRole("listitem");
    expect(items[0]).toHaveTextContent("Status changed from SHORTLISTED to OPENED.");
    expect(items[1]).toHaveTextContent("Application created.");
    expect(generations(backend)).toHaveLength(0);
  });

  it("reports a missing application and a malformed id", async () => {
    const { backend } = setup({}, "/applications/999");
    expect(await screen.findByRole("heading", { level: 1, name: "Application not found" })).toBeInTheDocument();
    expect(backend.calls.some((c) => c.path === "applications/999/")).toBe(true);
  });

  it("does not request a malformed id", async () => {
    const { backend } = setup({}, "/applications/x1");
    expect(await screen.findByRole("heading", { level: 1, name: "Application not found" })).toBeInTheDocument();
    expect(backend.calls.some((c) => c.path.startsWith("applications/x1"))).toBe(false);
  });

  it("notes when the application belongs to another profile", async () => {
    window.localStorage.setItem(ACTIVE_PROFILE_STORAGE_KEY, "2");
    setup({ profiles: [RIYA, SAM] });
    expect(await screen.findByText("This application belongs to another profile than the active one (Sam Example).")).toBeInTheDocument();
  });
});

describe("generation", () => {
  it("generates only on request, shows progress, blocks repeats and shows the draft", async () => {
    const { backend, user } = setup();
    backend.options.generationDelay = 150;
    const button = await screen.findByRole("button", { name: "Generate draft" });
    await user.click(button);
    expect(await screen.findByText(/Writing the draft with the local model… 0:00/)).toBeInTheDocument();
    expect(button).toBeDisabled();
    await user.click(button);

    await waitFor(() => expect(field("Subject")).toHaveValue(GENERATED_DRAFT.generated_subject));
    expect(field("Message body")).toHaveValue(GENERATED_DRAFT.generated_body);
    expect(field("WhatsApp message")).toHaveValue(GENERATED_DRAFT.generated_whatsapp_message);
    expect(generations(backend)).toHaveLength(1);
    expect(generations(backend)[0]!.body).toEqual({}); // the endpoint takes no body
    expect(screen.getByText("Generated by the local model.")).toBeInTheDocument();
    expect(within(panel("Draft")).queryByText(/qwen3/)).not.toBeInTheDocument(); // no model details in the draft panel
    expect(screen.getByText("Draft generated. Review it before you send anything.")).toBeInTheDocument();
    expect(await within(panel("History")).findByText("Content generated with qwen3:1.7b.")).toBeInTheDocument();
    expect(within(panel("Overview")).getAllByText("Draft ready").length).toBeGreaterThan(0);
  });

  it.each([
    [503, "The local model is not available", "Cannot reach Ollama"],
    [504, "The local model took too long", "did not respond within 180 seconds"],
    [502, "The model's answer could not be used", "placeholder text"],
    [422, "The resume could not be read", "missing from storage"],
  ] as const)("a %s keeps the saved draft and the user's edits", async (code, title, detail) => {
    const { backend, user } = setup({ applications: [app({ status: "GENERATED", generated_at: "2026-03-02T00:00:00Z", ...SAVED_DRAFT })] });
    backend.options.generation = code;
    await waitFor(() => expect(field("Subject")).toHaveValue("My saved subject"));
    await user.type(field("Message body"), " Plus my edit.");

    await user.click(screen.getByRole("button", { name: "Generate a new draft" }));
    const dialog = screen.getByRole("dialog", { name: "Replace the current draft?" });
    expect(dialog).toHaveTextContent("your unsaved edits are lost if generation succeeds");
    await user.click(within(dialog).getByRole("button", { name: "Generate a new draft" }));

    const alert = await within(panel("Draft")).findByRole("alert");
    expect(alert).toHaveTextContent(title);
    expect(alert).toHaveTextContent(detail);
    expect(alert).toHaveTextContent("The backend kept your existing drafts and the status unchanged.");
    expect(alert).not.toHaveTextContent("localhost:11434"); // internal URLs never shown
    expect(field("Subject")).toHaveValue("My saved subject");
    expect(field("Message body")).toHaveValue("My saved body. Plus my edit.");
    expect(backend.state.applications[0]!.generated_body).toBe("My saved body.");
    expect(backend.state.applications[0]!.status).toBe("GENERATED");
    expect(await within(panel("Overview")).findByText(/The last draft generation failed/)).toBeInTheDocument();
  });

  it("asks before replacing a draft, and cancelling sends nothing", async () => {
    const { backend, user } = setup({ applications: [app({ status: "GENERATED", ...SAVED_DRAFT })] });
    await user.click(await screen.findByRole("button", { name: "Generate a new draft" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
    expect(generations(backend)).toHaveLength(0);
  });

  it("explains throttling and input problems", async () => {
    const { backend, user } = setup();
    backend.options.generation = 429;
    await user.click(await screen.findByRole("button", { name: "Generate draft" }));
    expect(await within(panel("Draft")).findByRole("alert")).toHaveTextContent("Wait 30 seconds, then try again.");

    server.use(
      http.post(`${API}/applications/:id/generate/`, () =>
        HttpResponse.json({ resume: ["Attach a resume (or set a default resume for the profile) before generating."] }, { status: 400 }),
      ),
    );
    await user.click(screen.getByRole("button", { name: "Generate draft" }));
    await waitFor(() => expect(within(panel("Draft")).getByRole("alert")).toHaveTextContent("Attach a resume"));
    expect(within(panel("Draft")).getByRole("alert")).toHaveTextContent("Nothing was changed.");
  });

  it("is unavailable for sent applications", async () => {
    setup({ applications: [app({ status: "SENT", sent_at: "2026-03-02T00:00:00Z", ...SAVED_DRAFT })] });
    expect(await screen.findByRole("button", { name: "Generate a new draft" })).toBeDisabled();
    expect(screen.getByText("Drafts cannot be generated for a sent application.")).toBeInTheDocument();
  });

  it("describeGenerationError covers network errors", () => {
    expect(describeGenerationError(new ApiError({ status: 0 }))).toMatchObject({ title: "No confirmed result from the backend", uncertain: true, confirmedUnchanged: false });
  });
});

describe("draft review", () => {
  it("copies the subject and body separately, with feedback", async () => {
    const { user } = setup({ applications: [app({ status: "GENERATED", ...SAVED_DRAFT })] });
    const write = vi.spyOn(navigator.clipboard, "writeText");
    await waitFor(() => expect(field("Subject")).toHaveValue("My saved subject"));

    await user.click(within(panel("Draft")).getByRole("button", { name: "Copy subject" }));
    expect(write).toHaveBeenLastCalledWith("My saved subject");
    expect(within(panel("Draft")).getByText("Subject copied to the clipboard.")).toBeInTheDocument();
    expect(within(panel("Draft")).getByRole("button", { name: "Copied" })).toBeInTheDocument();

    await user.click(within(panel("Draft")).getByRole("button", { name: "Copy message body" }));
    expect(write).toHaveBeenLastCalledWith("My saved body.");
    await user.click(within(panel("Draft")).getByRole("button", { name: "Copy whatsapp message" }));
    expect(write).toHaveBeenLastCalledWith("My saved WhatsApp text.");
    // Copy only: no link that could open a mail or messaging app.
    expect(within(panel("Draft")).queryByRole("link")).not.toBeInTheDocument();
  });

  it("says when copying failed", async () => {
    const { user } = setup({ applications: [app({ status: "GENERATED", ...SAVED_DRAFT })] });
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("denied"));
    await waitFor(() => expect(field("Subject")).toHaveValue("My saved subject"));
    await user.click(within(panel("Draft")).getByRole("button", { name: "Copy subject" }));
    expect(await within(panel("Draft")).findByText("Could not copy. Select the text and copy it manually.")).toBeInTheDocument();
  });

  it("disables copying an empty draft", async () => {
    setup();
    expect(await within(await screen.findByRole("region", { name: "Draft" })).findByRole("button", { name: "Copy subject" })).toBeDisabled();
  });

  it("saves edits only when the backend confirms them", async () => {
    const { backend, user } = setup({ applications: [app({ status: "GENERATED", ...SAVED_DRAFT })] });
    backend.options.writeDelay = 100;
    await waitFor(() => expect(field("Subject")).toHaveValue("My saved subject"));
    await user.clear(field("Subject"));
    await user.type(field("Subject"), "Edited subject");
    expect(within(panel("Draft")).getByText("· unsaved")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save draft changes" }));
    expect(screen.queryByText("Draft changes saved")).not.toBeInTheDocument(); // not before the response
    expect(await screen.findByText("Draft changes saved")).toBeInTheDocument();
    expect(backend.writes("PATCH", "applications/50/")[0]!.body).toEqual({ generated_subject: "Edited subject" });
    expect(within(panel("Draft")).queryByText("· unsaved")).not.toBeInTheDocument();
  });

  it("shows a failed save and keeps the edits", async () => {
    const { user } = setup({ applications: [app({ status: "GENERATED", ...SAVED_DRAFT })] });
    server.use(
      http.patch(`${API}/applications/:id/`, () =>
        HttpResponse.json({ non_field_errors: ["Drafts of an application that was already sent cannot be edited."] }, { status: 400 }),
      ),
    );
    await waitFor(() => expect(field("Subject")).toHaveValue("My saved subject"));
    await user.type(field("Subject"), "!");
    await user.click(screen.getByRole("button", { name: "Save draft changes" }));
    expect(await within(panel("Draft")).findByRole("alert")).toHaveTextContent("already sent cannot be edited");
    expect(screen.queryByText("Draft changes saved")).not.toBeInTheDocument();
    expect(field("Subject")).toHaveValue("My saved subject!");
  });

  it("discards edits", async () => {
    const { user } = setup({ applications: [app({ status: "GENERATED", ...SAVED_DRAFT })] });
    await waitFor(() => expect(field("Subject")).toHaveValue("My saved subject"));
    await user.type(field("Subject"), " x");
    await user.click(within(panel("Draft")).getByRole("button", { name: "Discard changes" }));
    expect(field("Subject")).toHaveValue("My saved subject");
  });

  it("makes sent drafts read-only", async () => {
    setup({ applications: [app({ status: "SENT", sent_at: "2026-03-02T00:00:00Z", ...SAVED_DRAFT })] });
    await waitFor(() => expect(field("Subject")).toHaveValue("My saved subject"));
    expect(field("Subject")).toHaveAttribute("readonly");
    expect(field("Message body")).toHaveAttribute("readonly");
    expect(screen.queryByRole("button", { name: "Save draft changes" })).not.toBeInTheDocument();
    expect(screen.getByText("This application was sent, so its drafts are read-only.")).toBeInTheDocument();
  });
});

describe("status", () => {
  it("offers only the changes the backend accepts", () => {
    expect(allowedStatusChanges({ status: "SHORTLISTED", sent_at: null })).toEqual(["OPENED", "READY_TO_SEND", "SENT", "SKIPPED"]);
    expect(allowedStatusChanges({ status: "GENERATED", sent_at: null })).toEqual(["SHORTLISTED", "OPENED", "READY_TO_SEND", "SENT", "SKIPPED"]);
    expect(allowedStatusChanges({ status: "SENT", sent_at: "x" })).toEqual(["FOLLOW_UP", "REJECTED"]);
    expect(allowedStatusChanges({ status: "SKIPPED", sent_at: null })).toEqual(["SHORTLISTED", "OPENED", "READY_TO_SEND", "SENT"]);
  });

  it("changes status with a note and records it in the history", async () => {
    const { backend, user } = setup();
    const select = await screen.findByLabelText("Change status");
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "Choose…",
      "Opened",
      "Ready to send",
      "Sent (I sent it myself)",
      "Skipped",
    ]);
    await user.selectOptions(select, "Ready to send");
    await user.type(screen.getByLabelText(/^Note/), "Reviewed the draft");
    await user.click(screen.getByRole("button", { name: "Update status" }));

    expect(await screen.findByText("Status: Ready to send")).toBeInTheDocument();
    expect(backend.writes("PATCH", "applications/50/")[0]!.body).toEqual({ status: "READY_TO_SEND", note: "Reviewed the draft" });
    expect(await within(panel("History")).findByText("Reviewed the draft")).toBeInTheDocument();
  });

  it("asks before marking as sent, then locks the drafts and refreshes the job", async () => {
    const { backend, user } = setup({ applications: [app({ status: "GENERATED", ...SAVED_DRAFT })] });
    await user.selectOptions(await screen.findByLabelText("Change status"), "Sent (I sent it myself)");
    await user.click(screen.getByRole("button", { name: "Update status" }));
    const dialog = screen.getByRole("dialog", { name: "Mark as sent?" });
    expect(dialog).toHaveTextContent("ApplyForge does not send it");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(backend.writes("PATCH", "applications")).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: "Update status" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Yes, I sent it" }));
    expect(await screen.findByText("Status: Sent")).toBeInTheDocument();
    expect(field("Subject")).toHaveAttribute("readonly");
    expect(backend.state.jobs[0]!.status).toBe("APPLIED");

    await user.click(within(panel("Overview")).getByRole("link", { name: "Python Backend Developer" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Python Backend Developer" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("Your status")).toHaveValue("APPLIED"));
  });

  it("shows a status the backend refuses", async () => {
    const { user } = setup();
    server.use(
      http.patch(`${API}/applications/:id/`, () =>
        HttpResponse.json({ status: ["SKIPPED is not allowed right now."] }, { status: 400 }),
      ),
    );
    await user.selectOptions(await screen.findByLabelText("Change status"), "Skipped");
    await user.click(screen.getByRole("button", { name: "Update status" }));
    expect(await within(panel("Overview")).findByRole("alert")).toHaveTextContent("SKIPPED is not allowed right now.");
  });
});

describe("contact, channel and resume", () => {
  it("checks the channel against the contact before saving", async () => {
    const { backend, user } = setup();
    const settings = await screen.findByRole("region", { name: "Contact, channel and resume" });
    await user.selectOptions(within(settings).getByLabelText("Channel"), "WhatsApp");
    await user.click(within(settings).getByRole("button", { name: "Save details" }));
    expect(within(settings).getByLabelText("Channel")).toHaveAccessibleDescription("This contact has no WhatsApp or phone number.");
    expect(backend.writes("PATCH", "applications")).toHaveLength(0);
  });

  it("saves only what changed", async () => {
    const { backend, user } = setup();
    const settings = await screen.findByRole("region", { name: "Contact, channel and resume" });
    await waitFor(() => expect(within(settings).getByRole("option", { name: "Data CV" })).toBeInTheDocument());
    await user.selectOptions(within(settings).getByLabelText(/^Resume/), "Data CV");
    await user.click(within(settings).getByRole("button", { name: "Save details" }));
    expect(await screen.findByText("Application details saved")).toBeInTheDocument();
    expect(backend.writes("PATCH", "applications/50/")[0]!.body).toEqual({ resume: 6 });
  });
});

describe("creating an application from a job", () => {
  const jobPage = "/jobs/7";

  it("asks to shortlist the job first", async () => {
    setup({ applications: [], jobs: [{ ...JOB, status: "DISCOVERED" }] }, jobPage);
    const section = await screen.findByRole("region", { name: "Application" });
    expect(await within(section).findByText(/to start an application for this job/)).toBeInTheDocument();
    expect(within(section).queryByRole("button", { name: "Create application" })).not.toBeInTheDocument();
  });

  it("creates the application, opens it, and does not generate", async () => {
    const { backend, user } = setup({ applications: [] }, jobPage);
    const section = await screen.findByRole("region", { name: "Application" });
    const create = await within(section).findByRole("button", { name: "Create application" });
    expect(within(section).getByLabelText("Contact")).toHaveValue("31"); // first contact with an email
    await user.type(within(section).getByLabelText(/^Note/), "Referred by a friend");
    await user.click(create);

    expect(await screen.findByText("Application created. Generate a draft when you are ready.")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Generate draft" })).toBeInTheDocument();
    expect(backend.writes("POST", "applications/")[0]!.body).toEqual({
      job: 7,
      profile: 1,
      channel: "EMAIL",
      contact: 31,
      note: "Referred by a friend",
    });
    expect(backend.calls.some((c) => c.path.endsWith("/generate/"))).toBe(false);
  });

  it("links to an existing application instead", async () => {
    setup({}, jobPage);
    const section = await screen.findByRole("region", { name: "Application" });
    expect(await within(section).findByRole("link", { name: "Open application" })).toHaveAttribute("href", "/applications/50");
  });

  it("explains a duplicate reported by the backend", async () => {
    const { backend, user } = setup({ applications: [] }, jobPage);
    const section = await screen.findByRole("region", { name: "Application" });
    const create = await within(section).findByRole("button", { name: "Create application" });
    backend.state.applications.push(app({ id: 77 }));
    await user.click(create);
    expect(await within(section).findByRole("link", { name: "Open it" })).toHaveAttribute("href", "/applications/77");
  });

  it("checks the channel against the chosen contact", async () => {
    const { backend, user } = setup({ applications: [] }, jobPage);
    const section = await screen.findByRole("region", { name: "Application" });
    await within(section).findByRole("button", { name: "Create application" });
    await user.selectOptions(within(section).getByLabelText(/^Channel/), "WhatsApp");
    await user.click(within(section).getByRole("button", { name: "Create application" }));
    expect(within(section).getByLabelText(/^Channel/)).toHaveAccessibleDescription(expect.stringContaining("no WhatsApp or phone number"));
    expect(backend.writes("POST", "applications")).toHaveLength(0);
  });

  it("creates only once when clicked repeatedly", async () => {
    const { backend, user } = setup({ applications: [] }, jobPage);
    backend.options.writeDelay = 150;
    const section = await screen.findByRole("region", { name: "Application" });
    const create = await within(section).findByRole("button", { name: "Create application" });
    await user.click(create);
    await act(async () => create.click());
    await screen.findByRole("button", { name: "Generate draft" });
    expect(backend.writes("POST", "applications/")).toHaveLength(1);
  });
});
