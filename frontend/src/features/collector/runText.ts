/** Plain-language explanations of collector run states, counts and start errors. */

import { isApiError } from "../../api/errors";
import { publicMessage } from "../../lib/diagnostics";
import type { CollectorCounts, CollectorRunStatus } from "../../api/types";

export const STATUS_EXPLANATION: Record<CollectorRunStatus, string> = {
  SUCCEEDED: "Every post found was processed.",
  PARTIAL:
    "The run stopped early or some posts could not be saved. Jobs that were saved are kept; the error below says what went wrong.",
  FAILED:
    "Nothing was collected: the input was rejected, the LinkedIn session or browser failed before any post, or an unexpected error occurred.",
  RUNNING:
    "This run is still in progress - or it was interrupted. The backend marks runs still running after 30 minutes as failed when the next run starts.",
};

export const COUNT_LABELS: { key: keyof CollectorCounts; label: string; help: string }[] = [
  { key: "found", label: "Posts read", help: "Posts the collector read from the search results." },
  { key: "created", label: "New jobs", help: "Hiring posts saved as new jobs (and screened)." },
  { key: "existing", label: "Already known", help: "Matched a job that already existed (same post or description)." },
  { key: "skipped", label: "Skipped", help: "Not hiring posts, e.g. job seekers' posts." },
  { key: "invalid", label: "Invalid", help: "Hiring posts that could not be turned into a job." },
  { key: "failed", label: "Failed", help: "Posts that could not be saved because of an error." },
];

export const INTERNAL_ERROR_TEXT = "An internal error occurred; the details are in the backend log.";

/**
 * A run error or post reason fit for the UI: the collector's own messages (e.g. "Not
 * logged in to LinkedIn…", "1 post(s) failed to ingest.") are shown cleaned (no URLs or
 * OS error codes); raw exception text ("OperationalError: …") is replaced by a plain
 * explanation. Use <TechnicalDetails> next to it when the original is worth keeping.
 */
export function shortError(text: string | null | undefined): string | null {
  if (!text || !text.trim()) return null;
  return publicMessage(text) ?? INTERNAL_ERROR_TEXT;
}

export interface StartProblem {
  title: string;
  message: string;
  /** A run id the user can open (recorded failed run, or the run in progress). */
  runId: number | null;
  /** True when the request may have reached the backend and a run may exist. */
  uncertain: boolean;
}

/** For errors other than a completed 502 run (handled separately with its body). */
export function describeStartError(error: unknown): StartProblem {
  if (!isApiError(error)) {
    return { title: "The run could not be started", message: "Something went wrong.", runId: null, uncertain: true };
  }
  const runId = typeof error.extra.run === "number" ? error.extra.run : null;
  switch (error.kind) {
    case "validation": {
      const fields = Object.entries(error.fieldErrors).map(([field, messages]) =>
        field === "non_field_errors" ? messages[0] : `${field}: ${messages[0]}`,
      );
      return {
        title: "The backend did not accept this run",
        message: fields.join(" ") || error.message,
        runId,
        uncertain: false,
      };
    }
    case "conflict":
      return {
        title: "Another collector run is in progress",
        message: "Only one run can happen at a time. Wait for it to finish, then refresh the history.",
        runId,
        uncertain: false,
      };
    case "throttled":
      return {
        title: "Too many runs started recently",
        message: error.retryAfterSeconds
          ? `The backend allows a few runs per minute. Wait ${error.retryAfterSeconds} seconds before starting another.`
          : "The backend allows a few runs per minute. Wait a moment before starting another.",
        runId: null,
        uncertain: false,
      };
    case "network":
      return {
        title: "No answer from the backend",
        message:
          "The connection failed before a result arrived. The run may still have started on the backend and may still be running. It was NOT retried. Refresh the history to see what happened before starting another run.",
        runId: null,
        uncertain: true,
      };
    case "server":
      if (runId === null) {
        // e.g. a gateway/proxy timeout: no result, and no proof that nothing ran.
        return {
          title: "No result from the backend",
          message:
            "The request ended without a run result (for example a timeout). The run may have started and may still be running. It was NOT retried. Refresh the history before starting another run.",
          runId: null,
          uncertain: true,
        };
      }
      return {
        title: "The run failed unexpectedly",
        message: "The backend recorded the failure; details are only in the server log.",
        runId,
        uncertain: false,
      };
    default:
      return { title: "The run could not be started", message: error.message, runId, uncertain: false };
  }
}
