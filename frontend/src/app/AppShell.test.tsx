import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { TOKEN_STORAGE_KEY } from "../auth/tokenStorage";
import { createMockBackend } from "../test/mockBackend";
import { API, server } from "../test/server";
import { makeProfile, renderApp, TEST_TOKEN } from "../test/renderApp";

const ONE = [makeProfile(1, "Riya Fictional")];

const SECTIONS: [string, string][] = [
  ["/", "Dashboard"],
  ["/jobs", "Jobs"],
  ["/applications", "Applications"],
  ["/collector", "Collector"],
  ["/profile", "Profile & Skills"],
  ["/resumes", "Resumes"],
];

describe("navigation and routes", () => {
  it("lists all six sections in the main navigation", async () => {
    server.use(...createMockBackend({ profiles: ONE }).handlers);
    renderApp();
    const nav = screen.getByRole("navigation", { name: "Main" });
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
    await user.click(screen.getByRole("link", { name: "Collector" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Collector" })).toBeInTheDocument();
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
    const shell = container.querySelector(".shell")!;
    const menu = screen.getByRole("button", { name: "Menu" });
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
    const menu = screen.getByRole("button", { name: "Menu" });
    await user.click(menu);
    await user.keyboard("{Escape}");
    expect(container.querySelector(".shell")).toHaveAttribute("data-drawer", "closed");
    expect(menu).toHaveFocus();
  });

  it("closes when the backdrop is clicked", async () => {
    server.use(...createMockBackend({ profiles: ONE }).handlers);
    const { container } = renderApp();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Menu" }));
    await user.click(container.querySelector(".shell__backdrop")!);
    expect(container.querySelector(".shell")).toHaveAttribute("data-drawer", "closed");
  });
});

describe("Connect gate and disconnect", () => {
  it("shows only the Connect screen without a token, then the app after connecting", async () => {
    server.use(...createMockBackend({ profiles: ONE }).handlers);
    renderApp({ connected: false });
    expect(screen.queryByRole("navigation", { name: "Main" })).not.toBeInTheDocument();

    const user = userEvent.setup();
    await user.type(screen.getByLabelText("API token"), TEST_TOKEN);
    await user.click(screen.getByRole("button", { name: "Connect" }));

    expect(await screen.findByRole("navigation", { name: "Main" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
  });

  it("disconnects only after confirmation and clears the token", async () => {
    server.use(...createMockBackend({ profiles: ONE }).handlers);
    renderApp();
    const user = userEvent.setup();
    await screen.findByRole("heading", { level: 1, name: "Dashboard" });

    await user.click(screen.getByRole("button", { name: "Disconnect" }));
    const dialog = screen.getByRole("dialog", { name: "Disconnect from the backend?" });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(window.sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBe(TEST_TOKEN);

    await user.click(screen.getByRole("button", { name: "Disconnect" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Disconnect" }));

    expect(await screen.findByLabelText("API token")).toBeInTheDocument();
    expect(screen.getByText(/Disconnected. The token was removed/)).toBeInTheDocument();
    expect(window.sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
    expect(screen.queryByRole("navigation", { name: "Main" })).not.toBeInTheDocument();
  });

  it("returns to the Connect screen when the backend rejects the token", async () => {
    server.use(http.get(`${API}/profiles/`, () => HttpResponse.json({ detail: "Invalid token." }, { status: 401 })));
    renderApp();
    expect(await screen.findByText(/no longer accepts the saved token/)).toBeInTheDocument();
    expect(window.sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
  });

  it("shows the connection state", async () => {
    server.use(...createMockBackend({ profiles: ONE }).handlers);
    renderApp();
    expect(await screen.findByText("Connected")).toBeInTheDocument();
  });

  it("reports an unreachable backend in the top bar", async () => {
    server.use(http.get(`${API}/profiles/`, () => HttpResponse.error()));
    renderApp();
    expect(await screen.findByText("Backend unreachable", {}, { timeout: 4000 })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Cannot reach the ApplyForge backend"));
  });
});
