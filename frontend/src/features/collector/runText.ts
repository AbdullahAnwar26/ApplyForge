/** Plain-language explanations of collector run states, counts and start errors. */

import { publicMessage } from "../../lib/diagnostics";
import type { CollectorCounts, CollectorRunStatus } from "../../api/types";

export const STATUS_EXPLANATION: Record<CollectorRunStatus, string> = {
  SUCCEEDED: "Every post found was processed.",
  PARTIAL:
    "The run stopped early or some posts could not be saved. Jobs that were saved are kept; the error below says what went wrong.",
  FAILED:
    "Nothing was collected: the input was rejected, the LinkedIn session or browser failed before any post, or an unexpected error occurred.",
  RUNNING: "This collection is in progress.",
  CANCELLED: "You cancelled this collection. Everything collected before that is kept.",
  INTERRUPTED: "The server stopped during this collection. Everything collected before that is kept; you can continue it.",
};

export const COUNT_LABELS: { key: keyof CollectorCounts; label: string; help: string }[] = [
  { key: "found", label: "Posts read", help: "Posts the collector read from the search results." },
  { key: "created", label: "New jobs", help: "Hiring posts saved as new jobs (and screened)." },
  { key: "existing", label: "Already known", help: "Matched a job that already existed (same post or description)." },
  { key: "skipped", label: "Skipped", help: "Not hiring posts, e.g. job seekers' posts." },
  { key: "invalid", label: "Invalid", help: "Hiring posts that could not be turned into a job." },
  { key: "failed", label: "Failed", help: "Posts that could not be saved because of an error." },
  { key: "out_of_range", label: "Outside dates", help: "Posts outside the chosen date range." },
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
