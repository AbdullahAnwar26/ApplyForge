import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { createMockBackend } from "../test/mockBackend";
import { API, server } from "../test/server";
import { mockSession, TEST_PASSWORD } from "../test/mockAuth";
import { makeProfile, renderApp } from "../test/renderApp";

const ONE = [makeProfile(1, "Riya Fictional")];

const SECTIONS: [string, string][] = [
  ["/", "Dashboard"],
  ["/automation", "Job Automation"],
  ["/jobs", "Jobs"],
  ["/applications", "Applications"],
  ["/collector", "Collections"],
  ["/profile", "Profile & Skills"],
  ["/resumes", "Resumes"],
];

describe("navigation and routes", () => {
  it("lists all sections in the main navigation", async () => {
    server.use(...createMockBackend({ profiles: ONE }).handlers);
    renderApp();
    const nav = await screen.findByRole("navigation", { name: "Main" }); // after the session check
    expect(within(nav).getAllByRole("link").map((link) => link.textContent)).toEqual(SECTIONS.map(([, label]) => label));
    expect(await screen.findByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
  });

  it.each(SECTIONS)("renders %s and marks its link as current", async (path, label) => {
    server.use(...createMockBackend({ profiles: ONE }).handlers);
    renderApp({ path });
    expect(await screen.findByRole("heading", { level: 1, name: label })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("status", { name: /^Loading/ })).not.toBeInTheDocument());
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: label })).toHaveAttribute("aria-current", "page");
    expect(document.title).toBe(`${label} · ApplyForge`);
  });

  it("navigates between sections", async () => {
    server.use(...createMockBackend({ profiles: ONE }).handlers);
    renderApp();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("link", { name: "Collections" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Collections" })).toBeInTheDocument();
  });

  it("shows a useful 404 page", async () => {
    server.use(...createMockBackend({ profiles: ONE }).handlers);
    renderApp({ path: "/no/such/page" });
    expect(await screen.findByRole("heading", { level: 1, name: "Page not found" })).toBeInTheDocument();
    expect(screen.getByText("/no/such/page")).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByRole("link", { name: "Go to the dashboard" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
  });

});

describe("mobile navigation drawer", () => {
  it("opens from the menu button, closes after choosing a link", async () => {
    server.use(...createMockBackend({ profiles: ONE }).handlers);
    const { container } = renderApp();
    const menu = await screen.findByRole("button", { name: "Menu" });
    const shell = container.querySelector(".shell")!;
    const user = userEvent.setup();

    expect(menu).toHaveAttribute("aria-expanded", "false");
    expect(shell).toHaveAttribute("data-drawer", "closed");
    expect(menu).toHaveAttribute("aria-controls", screen.getByRole("navigation", { name: "Main" }).id);

    await user.click(menu);
    expect(menu).toHaveAttribute("aria-expanded", "true");
    expect(shell).toHaveAttribute("data-drawer", "open");
    expect(screen.getByRole("link", { name: "Dashboard" })).toHaveFocus();

    await user.click(within(screen.getByRole("navigation", { name: "Main" })).getByRole("link", { name: "Jobs" }));
    expect(shell).toHaveAttribute("data-drawer", "closed");
    expect(await screen.findByRole("heading", { level: 1, name: "Jobs" })).toBeInTheDocument();
  });

  it("closes with Escape and returns focus to the menu button", async () => {
    server.use(...createMockBackend({ profiles: ONE }).handlers);
    const { container } = renderApp();
    const user = userEvent.setup();
    const menu = await screen.findByRole("button", { name: "Menu" });
    await user.click(menu);
    await user.keyboard("{Escape}");
    expect(container.querySelector(".shell")).toHaveAttribute("data-drawer", "closed");
    expect(menu).toHaveFocus();
  });

  it("closes when the backdrop is clicked", async () => {
    server.use(...createMockBackend({ profiles: ONE }).handlers);
    const { container } = renderApp();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Menu" }));
    await user.click(container.querySelector(".shell__backdrop")!);
    expect(container.querySelector(".shell")).toHaveAttribute("data-drawer", "closed");
  });
});

describe("sign-in gate, sign-up and sign-out", () => {
  const nav = () => screen.queryByRole("navigation", { name: "Main" });

  it("shows only the sign-in page when signed out, and opens the requested page after signing in", async () => {
    const backend = createMockBackend({ profiles: ONE });
    server.use(...backend.handlers);
    renderApp({ path: "/resumes", signedIn: false });
    expect(await screen.findByRole("heading", { level: 1, name: "Sign in" })).toBeInTheDocument();
    expect(nav()).not.toBeInTheDocument();
    expect(backend.calls).toHaveLength(0); // no data is requested while signed out

    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Username"), "RIYA");
    await user.type(screen.getByLabelText("Password"), TEST_PASSWORD);
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("heading", { level: 1, name: "Resumes" })).toBeInTheDocument();
    expect(nav()).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /riya/ })).toHaveAttribute("href", "/account");
    expect(mockSession.remember).toBe(true);
  });

  it("checks the form, and shows one message for a wrong username or password", async () => {
    server.use(...createMockBackend({ profiles: ONE }).handlers);
    renderApp({ signedIn: false });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Sign in" }));
    expect(screen.getByText("Enter your username.")).toBeInTheDocument();
    expect(screen.getByText("Enter your password.")).toBeInTheDocument();

    await user.type(screen.getByLabelText("Username"), "riya");
    await user.type(screen.getByLabelText("Password"), "not-the-password");
    await user.click(screen.getByRole("checkbox", { name: /Keep me signed in/ }));
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Incorrect username or password.")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toHaveValue(""); // the wrong password is not kept
    expect(nav()).not.toBeInTheDocument();
  });

  it("creates an account from the sign-in page", async () => {
    server.use(...createMockBackend({ profiles: [] }).handlers);
    renderApp({ signedIn: false });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("link", { name: "Create an account" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Create an account" })).toBeInTheDocument();

    await user.type(screen.getByLabelText("Username"), "sam.example");
    await user.type(screen.getByLabelText("Password"), "My-new-password-9");
    await user.type(screen.getByLabelText("Confirm password"), "different-9");
    await user.click(screen.getByRole("button", { name: "Create account" }));
    expect(screen.getByText("The passwords do not match.")).toBeInTheDocument();

    // The backend's password rules are shown next to the field.
    await user.clear(screen.getByLabelText("Confirm password"));
    await user.type(screen.getByLabelText("Confirm password"), "My-new-password-9");
    await user.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByText("This password is too common.")).toBeInTheDocument();

    for (const label of ["Password", "Confirm password"]) {
      await user.clear(screen.getByLabelText(label));
      await user.type(screen.getByLabelText(label), "Sam-Fictional-42!");
    }
    await user.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByRole("link", { name: /sam\.example/ })).toBeInTheDocument();
    expect(mockSession.user?.username).toBe("sam.example");
  });

  it("refuses a username that is taken", async () => {
    server.use(...createMockBackend({ profiles: [] }).handlers);
    renderApp({ path: "/signup", signedIn: false });
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Username"), "Riya");
    await user.type(screen.getByLabelText("Password"), "Another-Fictional-7");
    await user.type(screen.getByLabelText("Confirm password"), "Another-Fictional-7");
    await user.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByText("This username is already taken.")).toBeInTheDocument();
  });

  it("signs out, clears the data and shows the sign-in page", async () => {
    const backend = createMockBackend({ profiles: ONE });
    server.use(...backend.handlers);
    const { queryClient } = renderApp();
    const user = userEvent.setup();
    await screen.findByRole("heading", { level: 1, name: "Dashboard" });
    expect(queryClient.getQueryCache().getAll().length).toBeGreaterThan(0);

    await user.click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByText("You are signed out.")).toBeInTheDocument();
    expect(nav()).not.toBeInTheDocument();
    expect(queryClient.getQueryCache().getAll()).toHaveLength(0); // nothing of this account stays cached
    expect(mockSession.user).toBeNull();
  });

  it("returns to sign-in when the session ends", async () => {
    server.use(...createMockBackend({ profiles: ONE }).handlers);
    renderApp();
    await screen.findByRole("heading", { level: 1, name: "Dashboard" });
    mockSession.user = null; // e.g. expired, or the password was changed on another device
    const user = userEvent.setup();
    await user.click(within(screen.getByRole("navigation", { name: "Main" })).getByRole("link", { name: "Jobs" }));
    expect(await screen.findByText("Your session ended. Sign in again to continue.")).toBeInTheDocument();
    expect(nav()).not.toBeInTheDocument();
  });

  it("changes the password on the account page", async () => {
    server.use(...createMockBackend({ profiles: ONE }).handlers);
    renderApp({ path: "/account" });
    const user = userEvent.setup();
    expect(await screen.findByRole("heading", { level: 1, name: "Account" })).toBeInTheDocument();
    expect(screen.getByText("riya@example.com")).toBeInTheDocument();

    await user.type(screen.getByLabelText("Current password"), "wrong-current");
    await user.type(screen.getByLabelText("New password"), "Brand-New-Pass-5");
    await user.type(screen.getByLabelText("Confirm new password"), "Brand-New-Pass-5");
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByText("The current password is not correct.")).toBeInTheDocument();

    await user.clear(screen.getByLabelText("Current password"));
    await user.type(screen.getByLabelText("Current password"), TEST_PASSWORD);
    await user.click(screen.getByRole("button", { name: "Change password" }));
    await waitFor(() => expect(mockSession.accounts[0]!.password).toBe("Brand-New-Pass-5"));
    expect(screen.getByLabelText("Current password")).toHaveValue("");
    expect(mockSession.user).not.toBeNull(); // still signed in here
  });

  it("explains an unreachable server before sign-in, and retries", async () => {
    let reachable = false;
    renderApp({ signedIn: false });
    server.use(http.get(`${API}/auth/me/`, () => (reachable ? undefined : HttpResponse.error())));
    expect(await screen.findByRole("heading", { level: 1, name: "Cannot reach the server" })).toBeInTheDocument();
    reachable = true;
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Sign in" })).toBeInTheDocument();
  });

  it("reports an unreachable server in the top bar", async () => {
    server.use(http.get(`${API}/profiles/`, () => HttpResponse.error()));
    renderApp();
    expect(await screen.findByText("Server unreachable", {}, { timeout: 4000 })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Cannot reach the ApplyForge backend"));
  });
});
