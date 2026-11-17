/**
 * Theme: "system" (follows the OS), "light" or "dark", remembered per browser.
 * Applied as `data-theme` on <html>; tokens.css defines both palettes.
 */

import { createContext, useContext, useEffect, useMemo, useSyncExternalStore, type ReactNode } from "react";

import { createPreference } from "./preferences";

export const THEMES = ["system", "light", "dark"] as const;
export type Theme = (typeof THEMES)[number];

export const THEME_STORAGE_KEY = "applyforge.theme";

export const themePreference = createPreference<Theme>(THEME_STORAGE_KEY, "system", (raw) =>
  (THEMES as readonly string[]).includes(raw) ? (raw as Theme) : undefined,
);

export function applyTheme(theme: Theme, root: HTMLElement = document.documentElement): void {
  if (theme === "system") delete root.dataset.theme;
  else root.dataset.theme = theme;
}

interface ThemeContextValue {
  theme: Theme;
  setTheme(theme: Theme): void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const theme = useSyncExternalStore(themePreference.subscribe, themePreference.get);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  const value = useMemo(() => ({ theme, setTheme: (next: Theme) => themePreference.set(next) }), [theme]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const value = useContext(ThemeContext);
  if (!value) throw new Error("useTheme must be used inside <ThemeProvider>.");
  return value;
}
