/**
 * Backend diagnostics shown in the UI.
 *
 * The backend's failure texts (generation, collector) are meant for the server log:
 * they can contain internal URLs (e.g. the local Ollama address), OS error codes,
 * model names and Python exception names. The UI shows its own plain explanation
 * and, where useful, the backend text only inside a collapsed "Technical details"
 * element - always with URLs and OS error codes removed.
 */

const MAX_CHARS = 300;
const URL = /\b(?:https?|wss?|file):\/\/[^\s'"<>)]+/gi;
const OS_ERROR = /\s*\[(?:WinError|Errno)\s*-?\d+\]\s*/gi;
const EXCEPTION = /^\s*(?:[A-Za-z_][\w.]*\.)?[A-Z][A-Za-z0-9]*(?:Error|Exception|Exit|Interrupt|Warning)\s*:/;

function clip(text: string): string {
  const clean = text.replace(/\s+/g, " ").trim();
  return clean.length > MAX_CHARS ? `${clean.slice(0, MAX_CHARS)}…` : clean;
}

/** True for raw exception text ("OperationalError: ...", tracebacks). */
export function isExceptionText(text: string | null | undefined): boolean {
  return Boolean(text && (EXCEPTION.test(text) || /Traceback \(most recent call last\)/.test(text)));
}

/** Backend text with internal URLs and OS error codes removed, shortened. */
export function technicalDetail(text: string | null | undefined): string | null {
  if (!text || !text.trim()) return null;
  return clip(text.replace(URL, "[address]").replace(OS_ERROR, " "));
}

/**
 * Backend text fit to show directly: cleaned like technicalDetail, but raw exception
 * text is not shown at all (null) - callers show their own explanation instead.
 */
export function publicMessage(text: string | null | undefined): string | null {
  if (!text || isExceptionText(text)) return null;
  return technicalDetail(text);
}
