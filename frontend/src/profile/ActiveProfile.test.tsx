import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { ApiError } from "../api/errors";
import { API, server } from "../test/server";
import { createMockBackend } from "../test/mockBackend";
import { makeProfile, renderApp } from "../test/renderApp";
import { ACTIVE_PROFILE_STORAGE_KEY, resolveActiveProfile } from "./ActiveProfileProvider";

const A = makeProfile(1, "Riya Fictional");
const B = makeProfile(2, "Sam Example");
const C = makeProfile(3, "Alex Sample");

const stored = () => window.localStorage.getItem(ACTIVE_PROFILE_STORAGE_KEY);
const switcher = () => screen.getByLabelText("Profile", { selector: "select.profile-switcher__select" });

describe("resolveActiveProfile", () => {
  it("covers every state", () => {
    expect(resolveActiveProfile(undefined, null, null)).toEqual({ status: "loading" });
    const error = new ApiError({ status: 500 });
    expect(resolveActiveProfile(undefined, 1, error)).toEqual({ status: "error", error });
    expect(resolveActiveProfile([], 1, null)).toEqual({ status: "empty" });
    expect(resolveActiveProfile([A], null, null)).toMatchObject({ status: "ready", profile: A });
    expect(resolveActiveProfile([A, B], null, null)).toMatchObject({ status: "needs-selection", reason: "unselected" });
    expect(resolveActiveProfile([A, B], 2, null)).toMatchObject({ status: "ready", profile: B });
    expect(resolveActiveProfile([A, B], 9, null)).toMatchObject({ status: "needs-selection", reason: "missing" });
    // A stale id is never silently replaced, even by the only remaining profile.
    expect(resolveActiveProfile([A], 9, null)).toMatchObject({ status: "needs-selection", reason: "missing" });
    // A loaded list stays in use when a later refresh fails.
    expect(resolveActiveProfile([A], 1, error)).toMatchObject({ status: "ready", profile: A });
  });
});

describe("active profile in the app", () => {
  it("zero profiles: explains it, links to Profile & Skills, and disables the switcher", async () => {
    server.use(...createMockBackend({ profiles: [] }).handlers);
    renderApp({ path: "/jobs" });
    expect(await screen.findByText("No profile yet")).toBeInTheDocument();
    expect(switcher()).toBeDisabled();
    expect(screen.queryByRole("heading", { level: 1, name: "Jobs" })).not.toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole("link", { name: "Go to Profile & Skills" }));
    // The profile page is reachable without a profile (that is where one is created).
    expect(await screen.findByRole("heading", { level: 1, name: "Profile & Skills" })).toBeInTheDocument();
  });

  it("one profile: selected automatically and remembered", async () => {
    server.use(...createMockBackend({ profiles: [A] }).handlers);
    renderApp();
    expect(await screen.findByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
    expect(switcher()).toHaveValue("1");
    await waitFor(() => expect(stored()).toBe("1"));
  });

  it("several profiles and none chosen: asks for a choice, then remembers it", async () => {
    server.use(...createMockBackend({ profiles: [A, B, C] }).handlers);
    renderApp();
    expect(await screen.findByText("Choose a profile")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 1, name: "Dashboard" })).not.toBeInTheDocument();
    expect(switcher()).toHaveValue("");
    expect(stored()).toBeNull();

    const user = userEvent.setup();
    const chooser = screen.getByRole("region", { name: "Choose a profile" });
    const use = within(chooser).getByRole("button", { name: "Use this profile" });
    expect(use).toBeDisabled();
    await user.selectOptions(within(chooser).getByRole("combobox"), "Sam Example");
    await user.click(use);

    expect(await screen.findByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
    expect(stored()).toBe("2");
    expect(switcher()).toHaveValue("2");
  });

  it("uses a remembered profile that still exists", async () => {
    window.localStorage.setItem(ACTIVE_PROFILE_STORAGE_KEY, "3");
    server.use(...createMockBackend({ profiles: [A, B, C] }).handlers);
    renderApp();
    expect(await screen.findByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
    expect(switcher()).toHaveValue("3");
  });

  it("a remembered profile that no longer exists requires a new choice", async () => {
    window.localStorage.setItem(ACTIVE_PROFILE_STORAGE_KEY, "42");
    server.use(...createMockBackend({ profiles: [A, B] }).handlers);
    renderApp();
    expect(await screen.findByText("The selected profile no longer exists")).toBeInTheDocument();
    expect(switcher()).toHaveValue("");
    expect(switcher()).toHaveAttribute("aria-invalid", "true");
    expect(stored()).toBe("42"); // unchanged until the user chooses
  });

  it("does not silently switch to the only remaining profile", async () => {
    window.localStorage.setItem(ACTIVE_PROFILE_STORAGE_KEY, "42");
    server.use(...createMockBackend({ profiles: [A] }).handlers);
    renderApp();
    expect(await screen.findByText("The selected profile no longer exists")).toBeInTheDocument();
    expect(stored()).toBe("42");

    const user = userEvent.setup();
    await user.selectOptions(switcher(), "Riya Fictional");
    expect(await screen.findByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
    expect(stored()).toBe("1");
  });

  it("ignores a malformed remembered id", async () => {
    window.localStorage.setItem(ACTIVE_PROFILE_STORAGE_KEY, "not-a-number");
    server.use(...createMockBackend({ profiles: [A] }).handlers);
    renderApp();
    expect(await screen.findByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
    await waitFor(() => expect(stored()).toBe("1"));
  });

  it("switching from the top bar takes effect, is remembered and confirmed", async () => {
    window.localStorage.setItem(ACTIVE_PROFILE_STORAGE_KEY, "1");
    server.use(...createMockBackend({ profiles: [A, B] }).handlers);
    renderApp();
    await screen.findByRole("heading", { level: 1, name: "Dashboard" });

    const user = userEvent.setup();
    await user.selectOptions(switcher(), "Sam Example");
    expect(switcher()).toHaveValue("2");
    expect(stored()).toBe("2");
    expect(await screen.findByText("Active profile: Sam Example")).toBeInTheDocument();
  });

  it("a profile-loading error is shown with a working retry", async () => {
    let calls = 0;
    server.use(...createMockBackend({ profiles: [A] }).handlers);
    server.use(
      http.get(`${API}/profiles/`, () => {
        calls += 1;
        return calls <= 2
          ? HttpResponse.json({ detail: "Database unavailable." }, { status: 503 })
          : HttpResponse.json([A]);
      }),
    );
    renderApp();
    expect(await screen.findByText("Could not load profiles", {}, { timeout: 4000 })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Database unavailable.");
    expect(switcher()).toBeDisabled();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
  });

  it("shows a loading state while profiles load", async () => {
    server.use(http.get(`${API}/profiles/`, () => new Promise(() => {})));
    renderApp();
    expect(await screen.findByRole("status", { name: "Loading profiles" })).toBeInTheDocument();
  });
});
