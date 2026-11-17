import { QueryClient } from "@tanstack/react-query";

import { isApiError } from "./errors";

/** Reads are retried once, and only for network errors or 5xx/429 responses. */
export function shouldRetryQuery(failureCount: number, error: unknown): boolean {
  if (failureCount >= 1) return false;
  if (!isApiError(error)) return false;
  return error.kind === "network" || error.kind === "server";
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: shouldRetryQuery,
        staleTime: 30_000,
        refetchOnWindowFocus: false,
      },
      // Writes are never retried automatically (collection runs, generation, status changes).
      mutations: { retry: false },
    },
  });
}
