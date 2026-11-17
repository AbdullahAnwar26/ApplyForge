/**
 * What a failed generation means.
 *
 * Only responses that provably come from ApplyForge's generation code are treated as
 * definite. tracker/api/views.py answers every recorded generation failure (422, 502,
 * 503, 504, 409) with JSON `{detail, application: <id>}`; input problems are a 400
 * with field errors and change nothing. Anything else - a 5xx without that body (for
 * example a reverse proxy or gateway timeout), or a connection that dropped - is an
 * UNKNOWN outcome: generation may still finish and save a draft on the backend, so
 * the UI must not claim that drafts or status are unchanged.
 *
 * Backend texts can contain internal details (local Ollama URL, model name, OS error
 * codes); they are only shown in "Technical details" (see lib/diagnostics.ts), except
 * for user-actionable input/resume/conflict messages, which are shown cleaned.
 */

import { isApiError, type ApiError } from "../../api/errors";
import { publicMessage } from "../../lib/diagnostics";

export interface GenerationProblem {
  title: string;
  advice: string;
  /** A cleaned backend message worth showing directly (input, resume and conflict problems). */
  message: string | null;
  /** The raw backend text, for the collapsed "Technical details" only. */
  technical: string | null;
  /** True only when the backend confirmed it kept the drafts and status (recorded failure). */
  confirmedUnchanged: boolean;
  /** True when the outcome is unknown: the request may have completed on the backend. */
  uncertain: boolean;
}

/** A failure answered by the generation endpoint itself (JSON with the application id). */
function fromGenerationCode(error: ApiError): boolean {
  return typeof error.extra.application === "number";
}

function backendText(error: ApiError): string | null {
  return error.detail ?? error.fieldMessage("status") ?? error.fieldMessage("resume") ?? error.fieldMessage();
}

const UNKNOWN: Omit<GenerationProblem, "technical"> = {
  title: "No confirmed result from the backend",
  advice:
    "The request ended without an answer from ApplyForge (for example a timeout on the way). Generation may still finish on the backend and save a new draft. Refresh this application in a minute before trying again.",
  message: null,
  confirmedUnchanged: false,
  uncertain: true,
};

export function describeGenerationError(error: unknown): GenerationProblem {
  if (!isApiError(error)) return { ...UNKNOWN, technical: null };
  const text = backendText(error);

  if (error.status === 400) {
    // Input problem: rejected before anything ran.
    return {
      title: "Generation is not possible yet",
      advice: "Nothing was changed.",
      message: publicMessage(text),
      technical: null,
      confirmedUnchanged: true,
      uncertain: false,
    };
  }
  if (error.status === 429) {
    return {
      title: "Too many generation requests",
      advice: error.retryAfterSeconds ? `Wait ${error.retryAfterSeconds} seconds, then try again.` : "Wait a moment, then try again.",
      message: null,
      technical: null,
      confirmedUnchanged: true,
      uncertain: false,
    };
  }
  if (error.kind === "network" || !fromGenerationCode(error)) {
    // No answer, or an answer that did not come from the generation code.
    return { ...UNKNOWN, technical: error.kind === "network" ? null : text };
  }

  const recorded = { technical: text, confirmedUnchanged: true, uncertain: false };
  switch (error.status) {
    case 503:
      return {
        title: "The local model is not available",
        advice: "Start Ollama on the backend machine (with the configured model pulled), then try again.",
        message: null,
        ...recorded,
      };
    case 504:
      return {
        title: "The local model took too long",
        advice: "The model did not answer within the backend's time limit. Try again, or check the machine's load.",
        message: null,
        ...recorded,
      };
    case 502:
      return {
        title: "The model's answer could not be used",
        advice: "The draft was rejected (for example invalid output or placeholder text). Try generating again.",
        message: null,
        ...recorded,
      };
    case 422:
      return {
        title: "The resume could not be read",
        advice: "Check the resume on the Resumes page, or choose another resume for this application.",
        message: publicMessage(text),
        ...recorded,
      };
    case 409:
      return {
        title: "The application changed during generation",
        advice: "Its status changed while the draft was being written, so the new draft was discarded.",
        message: null,
        ...recorded,
      };
    default:
      return { title: "Generation failed", advice: "Please try again.", message: null, ...recorded };
  }
}
