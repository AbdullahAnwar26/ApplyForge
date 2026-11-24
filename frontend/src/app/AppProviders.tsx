import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { AuthProvider } from "../auth/AuthProvider";
import { ToastProvider } from "../components/Toast";
import { ThemeProvider } from "../lib/theme";

/** Everything the app needs above the router. Used by main.tsx and by tests. */
export function AppProviders({
  queryClient,
  baseUrl,
  children,
}: {
  queryClient: QueryClient;
  baseUrl?: string;
  children: ReactNode;
}) {
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <ToastProvider>
          <AuthProvider baseUrl={baseUrl}>{children}</AuthProvider>
        </ToastProvider>
      </ThemeProvider>
    </QueryClientProvider>
  );
}
