import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { createMockBackend, makeResume } from "../../test/mockBackend";
import { makeProfile, renderApp } from "../../test/renderApp";
import { API, server } from "../../test/server";
import { checkResumeFile, MAX_RESUME_BYTES } from "./ResumeUpload";

const RIYA = () => makeProfile(1, "Riya Fictional");

function setup(initial: Parameters<typeof createMockBackend>[0] = {}) {
  const backend = createMockBackend({ profiles: [RIYA()], ...initial });
  server.use(...backend.handlers);
  renderApp({ path: "/resumes" });
  // applyAccept off: lets tests pick files the input's `accept` would hide.
  return { backend, user: userEvent.setup({ applyAccept: false }) };
}

const pdf = (name = "riya_backend.pdf", content = "%PDF-1.4 fictional") => new File([content], name, { type: "application/pdf" });
const fileInput = () => screen.getByLabelText(/^Resume file/) as HTMLInputElement;
const table = () => screen.getByRole("table", { name: "Resumes for Riya Fictional" });
const findTable = () => screen.findByRole("table", { name: "Resumes for Riya Fictional" });

describe("resume list", () => {
  it("shows each resume's metadata", async () => {
    setup({
      resumes: [
        makeResume(5, 1, { name: "Backend CV", file_name: "backend_cv.pdf", resume_type: "Backend", is_default: true }),
        makeResume(6, 1, { name: "Old CV", file_name: "old.docx", file_type: "docx", is_active: false }),
      ],
    });
    const rows = within(await screen.findByRole("table", { name: "Resumes for Riya Fictional" })).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("Backend CV");
    expect(rows[0]).toHaveTextContent("backend_cv.pdf · PDF");
    expect(rows[0]).toHaveTextContent("Backend");
    expect(within(rows[0]!).getByText("Default")).toBeInTheDocument();
    expect(within(rows[0]!).getByText("Active")).toBeInTheDocument();
    expect(within(rows[0]!).queryByRole("button", { name: "Make default" })).not.toBeInTheDocument();
    expect(rows[1]).toHaveTextContent("old.docx · DOCX");
    expect(within(rows[1]!).getByText("Inactive")).toBeInTheDocument();
    // No download is offered: the backend exposes no file URL.
    expect(screen.queryByRole("link", { name: /download/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /download/i })).not.toBeInTheDocument();
  });

  it("explains an empty list", async () => {
    setup();
    expect(await screen.findByText("No resumes yet")).toBeInTheDocument();
  });

  it("needs an active profile", async () => {
    const backend = createMockBackend({ profiles: [] });
    server.use(...backend.handlers);
    renderApp({ path: "/resumes" });
    expect(await screen.findByText("No profile yet")).toBeInTheDocument();
    expect(backend.calls.some((c) => c.path.startsWith("resumes"))).toBe(false);
  });

  it("shows a retryable error when the list fails", async () => {
    const backend = createMockBackend({ profiles: [RIYA()] });
    let fail = true;
    server.use(...backend.handlers);
    server.use(
      http.get(`${API}/resumes/`, () => (fail ? HttpResponse.json({ detail: "Unavailable." }, { status: 400 }) : HttpResponse.json([]))),
    );
    renderApp({ path: "/resumes" });
    expect(await screen.findByText("Unavailable.")).toBeInTheDocument();
    fail = false;
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("No resumes yet")).toBeInTheDocument();
  });
});

describe("upload", () => {
  it("uploads a PDF with its details and adds it to the list", async () => {
    const { backend, user } = setup({ resumes: [makeResume(5, 1, { name: "Backend CV", is_default: true })] });
    await screen.findByRole("table", { name: "Resumes for Riya Fictional" });
    expect(screen.getByRole("checkbox", { name: /Make this the default/ })).not.toBeChecked(); // a default exists

    await user.upload(fileInput(), pdf());
    expect(screen.getByText("riya_backend.pdf")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Name"), "Full-stack CV");
    await user.type(screen.getByLabelText("Resume type"), "Full-stack");
    await user.click(screen.getByRole("checkbox", { name: /Make this the default/ }));
    await user.click(screen.getByRole("button", { name: "Upload resume" }));

    expect(await within(await findTable()).findByText("Full-stack CV")).toBeInTheDocument();
    const [post] = backend.writes("POST", "resumes");
    expect(post!.form).toEqual({
      profile: "1",
      file: { name: "riya_backend.pdf", size: 18 },
      name: "Full-stack CV",
      resume_type: "Full-stack",
      is_default: "true",
    });
    expect(screen.getByText("Uploaded Full-stack CV")).toBeInTheDocument();
    // The new default replaces the old one.
    await waitFor(() => {
      const rows = within(table()).getAllByRole("row").slice(1);
      expect(rows.filter((row) => within(row).queryByText("Default"))).toHaveLength(1);
    });
    expect(fileInput().value).toBe("");
  });

  it("defaults the first resume to being the default", async () => {
    const { backend, user } = setup();
    await screen.findByText("No resumes yet");
    expect(screen.getByRole("checkbox", { name: /Make this the default/ })).toBeChecked();
    await user.upload(fileInput(), pdf("cv.docx"));
    await user.click(screen.getByRole("button", { name: "Upload resume" }));
    expect(await within(await findTable()).findByText("cv")).toBeInTheDocument();
    expect(backend.writes("POST", "resumes")[0]!.form).toMatchObject({ is_default: "true" });
    expect(backend.writes("POST", "resumes")[0]!.form).not.toHaveProperty("name"); // backend uses the file name
  });

  it("refuses unsupported, empty and oversized files before uploading", async () => {
    const { backend, user } = setup();
    await screen.findByText("No resumes yet");

    await user.upload(fileInput(), new File(["hello"], "notes.txt", { type: "text/plain" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Choose a PDF or DOCX file.");
    expect(screen.getByRole("button", { name: "Upload resume" })).toBeDisabled();

    await user.upload(fileInput(), new File([], "empty.pdf"));
    expect(screen.getByRole("alert")).toHaveTextContent("The file is empty.");

    await user.upload(fileInput(), new File([new Uint8Array(MAX_RESUME_BYTES + 1)], "huge.pdf"));
    expect(screen.getByRole("alert")).toHaveTextContent("the maximum is 10 MB");

    expect(backend.writes("POST", "resumes")).toHaveLength(0);
  });

  it("shows the backend's reason when it rejects the file", async () => {
    const { user } = setup();
    await screen.findByText("No resumes yet");
    await user.upload(fileInput(), pdf("scanned_cv.pdf"));
    await user.click(screen.getByRole("button", { name: "Upload resume" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Scanned or image-only resumes are not supported.");
    expect(fileInput()).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("No resumes yet")).toBeInTheDocument();
  });

  it("reports a server failure", async () => {
    const { user } = setup();
    server.use(http.post(`${API}/resumes/`, () => HttpResponse.json({ detail: "Storage is full." }, { status: 500 })));
    await screen.findByText("No resumes yet");
    await user.upload(fileInput(), pdf());
    await user.click(screen.getByRole("button", { name: "Upload resume" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Storage is full.");
    expect(screen.getByText("riya_backend.pdf")).toBeInTheDocument(); // file kept for a retry
  });

  it("shows progress while uploading and sends only one request", async () => {
    const { backend, user } = setup();
    backend.options.writeDelay = 200;
    await screen.findByText("No resumes yet");
    await user.upload(fileInput(), pdf());
    await user.click(screen.getByRole("button", { name: "Upload resume" }));

    expect(await screen.findByRole("progressbar", { name: "Upload progress" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Upload resume" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Upload resume" }));

    expect(await within(await findTable()).findByText("riya_backend")).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    expect(backend.writes("POST", "resumes")).toHaveLength(1);
  });

  it("can cancel an upload in progress", async () => {
    const { user } = setup();
    server.use(
      http.post(`${API}/resumes/`, async () => {
        await delay("infinite");
        return HttpResponse.json({});
      }),
    );
    await screen.findByText("No resumes yet");
    await user.upload(fileInput(), pdf());
    await user.click(screen.getByRole("button", { name: "Upload resume" }));
    await user.click(await screen.findByRole("button", { name: "Cancel upload" }));
    expect(await screen.findByText("Upload cancelled. Nothing was saved.")).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("checkResumeFile accepts upper-case extensions", () => {
    expect(checkResumeFile(new File(["x"], "CV.PDF"))).toBeNull();
    expect(checkResumeFile(new File(["x"], "CV.Docx"))).toBeNull();
    expect(checkResumeFile(new File(["x"], "CV.doc"))).toBe("Choose a PDF or DOCX file.");
  });
});

describe("resume actions", () => {
  const two = () => ({
    resumes: [makeResume(5, 1, { name: "Backend CV", is_default: true }), makeResume(6, 1, { name: "Data CV" })],
    texts: { 5: "Riya Fictional\nPython, Django, REST APIs" },
  });

  it("makes another resume the default", async () => {
    const { backend, user } = setup(two());
    const row = within(await screen.findByRole("row", { name: /Data CV/ }));
    await user.click(row.getByRole("button", { name: "Make default" }));
    await waitFor(() => expect(within(screen.getByRole("row", { name: /Data CV/ })).getByText("Default")).toBeInTheDocument());
    expect(within(screen.getByRole("row", { name: /Backend CV/ })).queryByText("Default")).not.toBeInTheDocument();
    expect(backend.writes("PATCH", "resumes/6/")[0]!.body).toEqual({ is_default: true });
  });

  it("deactivates and flags a default that is no longer used", async () => {
    const { backend, user } = setup(two());
    const row = within(await screen.findByRole("row", { name: /Backend CV/ }));
    await user.click(row.getByRole("button", { name: "Deactivate" }));
    await waitFor(() => expect(within(screen.getByRole("row", { name: /Backend CV/ })).getByText("Not used")).toBeInTheDocument());
    expect(backend.writes("PATCH", "resumes/5/")[0]!.body).toEqual({ is_active: false });
  });

  it("shows the extracted text as plain text", async () => {
    const { user } = setup({
      ...two(),
      texts: { 5: "<b>Riya</b> Fictional\nPython" },
    });
    const row = within(await screen.findByRole("row", { name: /Backend CV/ }));
    await user.click(row.getByRole("button", { name: "View text" }));
    const dialog = screen.getByRole("dialog", { name: "Text of Backend CV" });
    expect(await within(dialog).findByText(/<b>Riya<\/b> Fictional/)).toBeInTheDocument(); // not rendered as HTML
    expect(within(dialog).getByText(`${"<b>Riya</b> Fictional\nPython".length} characters`)).toBeInTheDocument();
    expect(dialog.querySelector("b")).toBeNull();
  });

  it("explains when the text cannot be extracted", async () => {
    const { user } = setup(two());
    const row = within(await screen.findByRole("row", { name: /Data CV/ }));
    await user.click(row.getByRole("button", { name: "View text" }));
    expect(await within(screen.getByRole("dialog")).findByText("The resume file is missing from storage.")).toBeInTheDocument();
  });

  it("deletes only after confirmation", async () => {
    const { backend, user } = setup(two());
    await user.click(await screen.findByRole("button", { name: "Delete Data CV" }));
    const dialog = screen.getByRole("dialog", { name: "Delete “Data CV”?" });
    expect(dialog).toHaveTextContent("cannot be downloaded or recovered");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(backend.writes("DELETE", "resumes")).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: "Delete Data CV" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Delete resume" }));
    await waitFor(() => expect(screen.queryByRole("row", { name: /Data CV/ })).not.toBeInTheDocument());
    expect(backend.writes("DELETE", "resumes/6/")).toHaveLength(1);
  });

  it("reports a failed update", async () => {
    const { user } = setup(two());
    server.use(http.patch(`${API}/resumes/:id/`, () => HttpResponse.json({ detail: "Not found." }, { status: 404 })));
    const row = within(await screen.findByRole("row", { name: /Data CV/ }));
    await user.click(row.getByRole("button", { name: "Make default" }));
    expect(await screen.findByText("Not found.")).toBeInTheDocument();
  });
});
