/**
 * What a failed generation means, by the status code tracker/generation.py uses.
 * The backend's own message (`detail`) is shown underneath as the specific reason.
 */

import { isApiError } from "../../api/errors";

export interface GenerationProblem {
  title: string;
  advice: string;
  detail: string | null;
  /** True when the backend recorded the failure in `last_error` (status and drafts kept). */
  recorded: boolean;
}

export function describeGenerationError(error: unknown): GenerationProblem {
  if (!isApiError(error)) {
    return { title: "Generation failed", advice: "Please try again.", detail: null, recorded: false };
  }
  const detail = error.detail ?? error.fieldMessage("status") ?? error.fieldMessage("resume") ?? error.fieldMessage();
  switch (error.status) {
    case 503:
      return {
        title: "The local model is not available",
        advice: "Start Ollama on the backend machine (with the configured model pulled), then try again.",
        detail,
        recorded: true,
      };
    case 504:
      return {
        title: "The local model took too long",
        advice: "Ollama did not answer within the backend's time limit. Try again, or check the machine's load.",
        detail,
        recorded: true,
      };
    case 502:
      return {
        title: "The model's answer could not be used",
        advice: "The draft was rejected (for example invalid output or placeholder text). Try generating again.",
        detail,
        recorded: true,
      };
    case 422:
      return {
        title: "The resume could not be read",
        advice: "Check the resume on the Resumes page, or choose another resume for this application.",
        detail,
        recorded: true,
      };
    case 409:
      return {
        title: "The application changed during generation",
        advice: "Its status changed while the draft was being written, so the new draft was discarded.",
        detail,
        recorded: true,
      };
    case 400:
      return { title: "Generation is not possible yet", advice: "Nothing was changed.", detail, recorded: false };
    case 429:
      return {
        title: "Too many generation requests",
        advice: error.retryAfterSeconds ? `Wait ${error.retryAfterSeconds} seconds, then try again.` : "Wait a moment, then try again.",
        detail: null,
        recorded: false,
      };
    case 0:
      return { title: "The backend could not be reached", advice: "Check that the Django server is running.", detail: null, recorded: false };
    default:
      return { title: "Generation failed", advice: "Please try again.", detail, recorded: error.status >= 500 };
  }
}
