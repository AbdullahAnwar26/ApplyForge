/**
 * Application shell: sidebar navigation (a drawer below 768px), top bar with the
 * active-profile switcher, theme, connection indicator and Disconnect.
 */

import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { NavLink } from "react-router-dom";

import { isApiError } from "../api/errors";
import { useToken } from "../auth/TokenProvider";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/Dialog";
import { THEMES, useTheme, type Theme } from "../lib/theme";
import { useActiveProfile } from "../profile/ActiveProfileProvider";
import { ProfileSwitcher } from "../profile/ProfileControls";
import { NAV_ITEMS } from "./navigation";

const THEME_LABELS: Record<Theme, string> = { system: "System", light: "Light", dark: "Dark" };

function ThemeSwitcher() {
  const { theme, setTheme } = useTheme();
  const id = useId();
  return (
    <div className="theme-switcher">
      <label htmlFor={id} className="theme-switcher__label">
        Theme
      </label>
      <select id={id} className="theme-switcher__select" value={theme} onChange={(e) => setTheme(e.target.value as Theme)}>
        {THEMES.map((value) => (
          <option key={value} value={value}>
            {THEME_LABELS[value]}
          </option>
        ))}
      </select>
    </div>
  );
}

function ConnectionIndicator() {
  const { mode } = useToken();
  const { state } = useActiveProfile();
  const unreachable = state.status === "error" && isApiError(state.error) && state.error.kind === "network";
  const label = unreachable ? "Backend unreachable" : "Connected";
  const detail = unreachable
    ? "The Django server did not respond."
    : mode === "memory"
      ? "Token kept in memory only; it is cleared on reload."
      : "Token kept for this browser session.";
  return (
    <p className={`connection ${unreachable ? "connection--down" : "connection--up"}`} title={detail}>
      <span className="connection__dot" aria-hidden="true" />
      <span>{label}</span>
      <span className="visually-hidden">. {detail}</span>
    </p>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const { disconnect } = useToken();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  const navRef = useRef<HTMLElement>(null);
  const navId = useId();

  // Drawer: focus the first link when opened; Escape closes it and returns focus.
  useEffect(() => {
    if (!drawerOpen) return;
    navRef.current?.querySelector<HTMLElement>("a")?.focus();
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setDrawerOpen(false);
        menuButtonRef.current?.focus();
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [drawerOpen]);

  return (
    <div className="shell" data-drawer={drawerOpen ? "open" : "closed"}>
      <a className="skip-link" href="#main">
        Skip to content
      </a>

      <aside className="shell__sidebar">
        <div className="brand">
          <span className="brand__mark" aria-hidden="true">
            AF
          </span>
          <span className="brand__name">ApplyForge</span>
        </div>
        <nav id={navId} ref={navRef} className="nav" aria-label="Main">
          <ul className="nav__list">
            {NAV_ITEMS.map((item) => (
              <li key={item.to}>
                <NavLink
                  to={item.to}
                  end={item.to === "/"}
                  className={({ isActive }) => `nav__link${isActive ? " is-active" : ""}`}
                  onClick={() => setDrawerOpen(false)}
                >
                  {item.icon}
                  <span>{item.label}</span>
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>
        <div className="shell__sidebar-footer">
          <ThemeSwitcher />
          <p className="shell__note">Prepares applications; you review and send them yourself.</p>
        </div>
      </aside>

      {drawerOpen && <div className="shell__backdrop" onClick={() => setDrawerOpen(false)} aria-hidden="true" />}

      <div className="shell__main">
        <header className="topbar">
          <button
            ref={menuButtonRef}
            type="button"
            className="topbar__menu btn btn--ghost"
            aria-controls={navId}
            aria-expanded={drawerOpen}
            onClick={() => setDrawerOpen((open) => !open)}
          >
            <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false">
              <path d="M4 7h16M4 12h16M4 17h16" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
            </svg>
            <span className="visually-hidden">Menu</span>
          </button>
          <span className="topbar__brand" aria-hidden="true">
            ApplyForge
          </span>
          <div className="topbar__controls">
            <ProfileSwitcher />
            <ConnectionIndicator />
            <Button size="sm" onClick={() => setConfirmDisconnect(true)}>
              Disconnect
            </Button>
          </div>
        </header>
        <main id="main" className="content" tabIndex={-1}>
          {children}
        </main>
      </div>

      <ConfirmDialog
        open={confirmDisconnect}
        title="Disconnect from the backend?"
        message="The API token is removed from this browser. You will need to enter it again to continue."
        confirmLabel="Disconnect"
        onConfirm={() => {
          setConfirmDisconnect(false);
          disconnect("user");
        }}
        onCancel={() => setConfirmDisconnect(false)}
      />
    </div>
  );
}
