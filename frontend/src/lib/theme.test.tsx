import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { createMockBackend } from "../test/mockBackend";
import { makeProfile, renderApp } from "../test/renderApp";
import { server } from "../test/server";
import { applyTheme, THEME_STORAGE_KEY } from "./theme";

afterEach(() => {
  delete document.documentElement.dataset.theme;
});

describe("theme", () => {
  it("defaults to the system theme (no data-theme attribute)", async () => {
    server.use(...createMockBackend({ profiles: [makeProfile(1, "Riya Fictional")] }).handlers);
    renderApp();
    expect(await screen.findByLabelText("Theme")).toHaveValue("system");
    expect(document.documentElement.dataset.theme).toBeUndefined();
  });

  it("switches between dark, light and system, and remembers the choice", async () => {
    server.use(...createMockBackend({ profiles: [makeProfile(1, "Riya Fictional")] }).handlers);
    renderApp();
    const user = userEvent.setup();
    const select = await screen.findByLabelText("Theme");

    await user.selectOptions(select, "dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");

    await user.selectOptions(select, "light");
    expect(document.documentElement.dataset.theme).toBe("light");

    await user.selectOptions(select, "system");
    expect(document.documentElement.dataset.theme).toBeUndefined();
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("system");
  });

  it("applies a remembered theme on load, including on the Connect screen", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "dark");
    renderApp({ connected: false });
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("ignores an unknown stored value", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "neon");
    renderApp({ connected: false });
    expect(document.documentElement.dataset.theme).toBeUndefined();
  });

  it("applyTheme sets and clears the attribute", () => {
    const root = document.createElement("html");
    applyTheme("dark", root);
    expect(root.dataset.theme).toBe("dark");
    applyTheme("system", root);
    expect(root.dataset.theme).toBeUndefined();
  });
});
