import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { ACTIVE_PROFILE_STORAGE_KEY } from "../../profile/ActiveProfileProvider";
import { createMockBackend } from "../../test/mockBackend";
import { makeProfile, renderApp } from "../../test/renderApp";
import { API, server } from "../../test/server";
import { findDuplicate, normalizeSkillName } from "./SkillsPanel";
import { validateProfile } from "./ProfileForm";

const RIYA = () => makeProfile(1, "Riya Fictional", { target_roles: "Python Developer, Backend Engineer" });
const SAM = () => makeProfile(2, "Sam Example", { target_roles: "Django Developer" });

function setup(initial: Parameters<typeof createMockBackend>[0] = {}, path = "/profile") {
  const backend = createMockBackend(initial);
  server.use(...backend.handlers);
  const utils = renderApp({ path });
  return { backend, user: userEvent.setup(), ...utils };
}

const switcher = () => screen.getByLabelText("Profile", { selector: "select.profile-switcher__select" });

describe("first profile", () => {
  it("validates required fields without sending anything", async () => {
    const { backend, user } = setup();
    await screen.findByRole("heading", { name: "Create your first profile" });
    await user.click(screen.getByRole("button", { name: "Create profile" }));

    expect(screen.getByLabelText(/^Name/)).toHaveAccessibleDescription("Enter a name.");
    expect(screen.getByLabelText(/^Target roles/)).toHaveAccessibleDescription(expect.stringContaining("Enter at least one target role."));
    expect(backend.writes("POST", "profiles")).toHaveLength(0);
  });

  it("creates the profile, makes it active without a reload, and unlocks the other pages", async () => {
    const { backend, user } = setup();
    await screen.findByRole("heading", { name: "Create your first profile" });
    expect(switcher()).toBeDisabled();

    await user.type(screen.getByLabelText(/^Name/), "  Riya Fictional ");
    await user.type(screen.getByLabelText(/^Target roles/), "Python Developer, Backend Engineer");
    await user.type(screen.getByLabelText(/^Years of experience/), "1.5");
    await user.click(screen.getByRole("button", { name: "Create profile" }));

    expect(await screen.findByRole("heading", { level: 2, name: "Riya Fictional" })).toBeInTheDocument();
    const [post] = backend.writes("POST", "profiles");
    expect(post!.body).toMatchObject({ name: "Riya Fictional", target_roles: "Python Developer, Backend Engineer", experience_years: 1.5 });
    expect(post!.body).not.toHaveProperty("max_acceptable_experience_years"); // empty -> backend default
    expect(switcher()).toHaveDisplayValue("Riya Fictional");
    const created = backend.state.profiles[0]!;
    expect(window.localStorage.getItem(ACTIVE_PROFILE_STORAGE_KEY)).toBe(String(created.id));
    expect(screen.getByText("Created Riya Fictional")).toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "Jobs" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Jobs" })).toBeInTheDocument();
    expect(screen.queryByText("No profile yet")).not.toBeInTheDocument();
  });

  it("shows backend validation errors next to the field", async () => {
    const { user } = setup();
    server.use(
      http.post(`${API}/profiles/`, () =>
        HttpResponse.json({ name: ["Ensure this field has no more than 255 characters."] }, { status: 400 }),
      ),
    );
    await screen.findByRole("heading", { name: "Create your first profile" });
    await user.type(screen.getByLabelText(/^Name/), "Riya");
    await user.type(screen.getByLabelText(/^Target roles/), "Developer");
    await user.click(screen.getByRole("button", { name: "Create profile" }));

    await waitFor(() =>
      expect(screen.getByLabelText(/^Name/)).toHaveAccessibleDescription("Ensure this field has no more than 255 characters."),
    );
    expect(screen.getByLabelText(/^Name/)).toHaveValue("Riya"); // input kept
  });

  it("reports a server failure without losing the input", async () => {
    const { user } = setup();
    server.use(http.post(`${API}/profiles/`, () => HttpResponse.json({ detail: "Database is locked." }, { status: 503 })));
    await screen.findByRole("heading", { name: "Create your first profile" });
    await user.type(screen.getByLabelText(/^Name/), "Riya");
    await user.type(screen.getByLabelText(/^Target roles/), "Developer");
    await user.click(screen.getByRole("button", { name: "Create profile" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Database is locked.");
    expect(screen.getByLabelText(/^Target roles/)).toHaveValue("Developer");
    expect(switcher()).toBeDisabled();
  });

  it("sends one request even when submitted twice", async () => {
    const { backend, user } = setup();
    backend.options.writeDelay = 150;
    await screen.findByRole("heading", { name: "Create your first profile" });
    await user.type(screen.getByLabelText(/^Name/), "Riya");
    await user.type(screen.getByLabelText(/^Target roles/), "Developer");
    const submit = screen.getByRole("button", { name: "Create profile" });
    await user.click(submit);
    await user.click(submit);
    await user.keyboard("{Enter}");

    expect(await screen.findByRole("heading", { level: 2, name: "Riya" })).toBeInTheDocument();
    expect(backend.writes("POST", "profiles")).toHaveLength(1);
  });
});

describe("editing the active profile", () => {
  it("shows the details and saves changes", async () => {
    const { backend, user } = setup({ profiles: [RIYA()] });
    expect(await screen.findByRole("heading", { level: 2, name: "Riya Fictional" })).toBeInTheDocument();
    expect(screen.getByText("Python Developer")).toBeInTheDocument();
    expect(screen.getByText("Backend Engineer")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Edit profile" }));
    const roles = screen.getByLabelText(/^Target roles/);
    expect(roles).toHaveValue("Python Developer, Backend Engineer");
    await user.clear(roles);
    await user.type(roles, "Django Developer");
    await user.clear(screen.getByLabelText(/^Name/));
    await user.type(screen.getByLabelText(/^Name/), "Riya F.");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(await screen.findByRole("heading", { level: 2, name: "Riya F." })).toBeInTheDocument();
    expect(backend.writes("PATCH", "profiles/1/")[0]!.body).toMatchObject({ name: "Riya F.", target_roles: "Django Developer" });
    expect(screen.getByText("Django Developer")).toBeInTheDocument();
    expect(switcher()).toHaveDisplayValue("Riya F."); // provider updated
    expect(screen.getByText("Profile saved")).toBeInTheDocument();
  });

  it("checks numbers before saving", async () => {
    const { backend, user } = setup({ profiles: [RIYA()] });
    await user.click(await screen.findByRole("button", { name: "Edit profile" }));
    const years = screen.getByLabelText(/^Years of experience/);
    const ceiling = screen.getByLabelText(/^Highest experience requirement/);
    await user.clear(years);
    await user.type(years, "-1");
    await user.clear(ceiling);
    await user.type(ceiling, "4.5");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(years).toHaveAccessibleDescription(expect.stringContaining("Enter a number of years, 0 or more."));
    expect(ceiling).toHaveAccessibleDescription(expect.stringContaining("Enter a whole number of years, 0 or more."));
    expect(backend.writes("PATCH", "profiles")).toHaveLength(0);

    await user.clear(years);
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(years).toHaveAccessibleDescription(expect.stringContaining("Enter your years of experience."));
  });

  it("cancel discards changes", async () => {
    const { backend, user } = setup({ profiles: [RIYA()] });
    await user.click(await screen.findByRole("button", { name: "Edit profile" }));
    await user.type(screen.getByLabelText(/^Name/), " changed");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("heading", { level: 2, name: "Riya Fictional" })).toBeInTheDocument();
    expect(backend.writes("PATCH", "profiles")).toHaveLength(0);
  });

  it("a new profile becomes active and is remembered", async () => {
    const { user } = setup({ profiles: [RIYA()] });
    await user.click(await screen.findByRole("button", { name: "New profile" }));
    const dialog = screen.getByRole("dialog", { name: "New profile" });
    await user.type(within(dialog).getByLabelText(/^Name/), "Sam Example");
    await user.type(within(dialog).getByLabelText(/^Target roles/), "Django Developer");
    await user.click(within(dialog).getByRole("button", { name: "Create profile" }));

    expect(await screen.findByRole("heading", { level: 2, name: "Sam Example" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(switcher()).toHaveDisplayValue("Sam Example");
    expect(within(switcher()).getAllByRole("option").map((o) => o.textContent)).toEqual(["Riya Fictional", "Sam Example"]);
  });

  it("switching profiles shows that profile and its skills", async () => {
    window.localStorage.setItem(ACTIVE_PROFILE_STORAGE_KEY, "1");
    const { user } = setup({
      profiles: [RIYA(), SAM()],
      skills: [
        { id: 11, name: "Python", category: "TECHNOLOGY", profile: 1 },
        { id: 12, name: "Django", category: "FRAMEWORK", profile: 2 },
      ],
    });
    expect(await screen.findByText("Python")).toBeInTheDocument();
    await user.selectOptions(switcher(), "Sam Example");
    expect(await screen.findByRole("heading", { level: 2, name: "Sam Example" })).toBeInTheDocument();
    expect(await screen.findByText("Django")).toBeInTheDocument();
    expect(screen.queryByText("Python")).not.toBeInTheDocument();
    expect(window.localStorage.getItem(ACTIVE_PROFILE_STORAGE_KEY)).toBe("2");
  });

  it("asks for a choice when the remembered profile is gone", async () => {
    window.localStorage.setItem(ACTIVE_PROFILE_STORAGE_KEY, "99");
    setup({ profiles: [RIYA(), SAM()] });
    expect(await screen.findByText("The selected profile no longer exists")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit profile" })).not.toBeInTheDocument();
  });
});

describe("skills", () => {
  const withSkills = () => ({
    profiles: [RIYA()],
    skills: [
      { id: 11, name: "Python", category: "TECHNOLOGY" as const, profile: 1 },
      { id: 12, name: "Django", category: "FRAMEWORK" as const, profile: 1 },
      { id: 13, name: "REST APIs", category: "OTHER" as const, profile: 1 },
    ],
  });

  it("loads the categories and groups the skills", async () => {
    setup(withSkills());
    const category = await screen.findByLabelText("Category");
    expect(within(category).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "Skill",
      "Role",
      "Technology",
      "Framework",
      "Other",
    ]);
    expect(within(screen.getByRole("region", { name: "Technology skills" })).getByText("Python")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Framework skills" })).getByText("Django")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Role skills" })).not.toBeInTheDocument(); // empty groups hidden
  });

  it("shows an empty state when the profile has no skills", async () => {
    setup({ profiles: [RIYA()] });
    expect(await screen.findByText("No skills yet")).toBeInTheDocument();
  });

  it("adds a skill in the chosen category", async () => {
    const { backend, user } = setup(withSkills());
    const name = await screen.findByLabelText("Skill or keyword");
    await user.type(name, "  Celery  ");
    await user.selectOptions(screen.getByLabelText("Category"), "Technology");
    await user.click(screen.getByRole("button", { name: "Add skill" }));

    const group = screen.getByRole("region", { name: "Technology skills" });
    expect(await within(group).findByText("Celery")).toBeInTheDocument();
    expect(backend.writes("POST", "skills")[0]!.body).toEqual({ name: "Celery", category: "TECHNOLOGY", profile: 1 });
    expect(name).toHaveValue("");
  });

  it("refuses a duplicate, ignoring case and spaces, without a request", async () => {
    const { backend, user } = setup(withSkills());
    await user.type(await screen.findByLabelText("Skill or keyword"), "  python ");
    await user.click(screen.getByRole("button", { name: "Add skill" }));
    expect(screen.getByRole("alert")).toHaveTextContent("“Python” is already one of this profile's skills.");
    expect(backend.writes("POST", "skills")).toHaveLength(0);
    expect(screen.getByLabelText("Skill or keyword")).toHaveAttribute("aria-invalid", "true");
  });

  it("explains a duplicate reported by the backend", async () => {
    const { backend, user } = setup(withSkills());
    await screen.findByLabelText("Skill or keyword");
    // Added elsewhere after this page loaded.
    backend.state.skills.push({ id: 14, name: "Redis", category: "TECHNOLOGY", profile: 1 });
    await user.type(screen.getByLabelText("Skill or keyword"), "Redis");
    await user.click(screen.getByRole("button", { name: "Add skill" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("This skill is already on the profile.");
  });

  it("adds only once when submitted repeatedly", async () => {
    const { backend, user } = setup(withSkills());
    backend.options.writeDelay = 150;
    await user.type(await screen.findByLabelText("Skill or keyword"), "Docker");
    await user.click(screen.getByRole("button", { name: "Add skill" }));
    await user.click(screen.getByRole("button", { name: "Add skill" }));
    expect(await screen.findByText("Docker")).toBeInTheDocument();
    expect(backend.writes("POST", "skills")).toHaveLength(1);
  });

  it("removes a skill only after confirmation", async () => {
    const { backend, user } = setup(withSkills());
    await user.click(await screen.findByRole("button", { name: "Remove Django" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
    expect(backend.writes("DELETE", "skills")).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: "Remove Django" }));
    const dialog = screen.getByRole("dialog", { name: "Remove “Django”?" });
    await user.click(within(dialog).getByRole("button", { name: "Remove skill" }));

    await waitFor(() => expect(screen.queryByText("Django")).not.toBeInTheDocument());
    expect(backend.writes("DELETE", "skills/12/")).toHaveLength(1);
    expect(screen.getByText("Removed Django")).toBeInTheDocument();
  });

  it("keeps the dialog open with the reason when removal fails", async () => {
    const { user } = setup(withSkills());
    server.use(http.delete(`${API}/skills/:id/`, () => HttpResponse.json({ detail: "Not found." }, { status: 404 })));
    await user.click(await screen.findByRole("button", { name: "Remove Python" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Remove skill" }));
    expect(await within(screen.getByRole("dialog")).findByRole("alert")).toHaveTextContent("Not found.");
  });

  it("shows a retryable error when categories fail to load", async () => {
    const backend = createMockBackend(withSkills());
    let fail = true;
    server.use(...backend.handlers);
    // Registered last, so it takes precedence over the mock's categories handler.
    server.use(
      http.get(`${API}/skill-categories/`, () =>
        fail ? HttpResponse.json({ detail: "Unavailable." }, { status: 400 }) : HttpResponse.json([{ value: "SKILL", label: "Skill" }]),
      ),
    );
    renderApp({ path: "/profile" });
    expect(await screen.findByText("Could not load skills")).toBeInTheDocument();
    fail = false;
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByLabelText("Skill or keyword")).toBeInTheDocument();
  });
});

describe("helpers", () => {
  it("validateProfile trims text and maps numbers", () => {
    const { errors, payload } = validateProfile(
      {
        name: " A ",
        target_roles: " Dev ",
        professional_summary: "",
        experience_info: "",
        preferred_locations: "",
        preferred_work_modes: "",
        experience_years: "0",
        max_acceptable_experience_years: "0",
      },
      "edit",
    );
    expect(errors).toEqual({});
    expect(payload).toMatchObject({ name: "A", target_roles: "Dev", experience_years: 0, max_acceptable_experience_years: 0 });
  });

  it("normalises and finds duplicate skill names", () => {
    expect(normalizeSkillName("  Machine   Learning ")).toBe("Machine Learning");
    const skills = [{ id: 1, name: "Machine Learning", category: "SKILL" as const, profile: 1 }];
    expect(findDuplicate(skills, "machine  learning")?.id).toBe(1);
    expect(findDuplicate(skills, "Machine")).toBeUndefined();
  });
});
