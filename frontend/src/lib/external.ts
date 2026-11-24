/**
 * Opening the user's email app / WhatsApp from a click whose target URL is only known
 * after a request (the hand-off). Browsers block pop-ups opened after an `await`, so the
 * tab is reserved synchronously in the click handler and pointed at the URL afterwards.
 */

export interface ReservedWindow {
  go(url: string): boolean;
  cancel(): void;
}

export function reserveWindow(): ReservedWindow {
  let win: Window | null = null;
  try {
    win = window.open("about:blank", "_blank");
    // A same-origin blank page: say what it is while the message is prepared.
    if (win?.document?.body) {
      win.document.title = "Preparing…";
      win.document.body.style.font = "16px system-ui, sans-serif";
      win.document.body.style.padding = "2rem";
      win.document.body.textContent = "Preparing your message in ApplyForge… this tab opens it in a moment.";
    }
  } catch {
    win = null;
  }
  return {
    go(url: string) {
      if (!win || win.closed) return false;
      try {
        win.opener = null; // the opened page cannot reach back into ApplyForge
        win.location.href = url;
        return true;
      } catch {
        return false;
      }
    },
    cancel() {
      try {
        win?.close();
      } catch {
        /* already closed */
      }
    },
  };
}

/** Start a download of a same-origin URL (the session cookie authenticates it). */
export function downloadFile(url: string): void {
  const link = document.createElement("a");
  link.href = url;
  link.download = "";
  link.rel = "noopener";
  document.body.appendChild(link);
  link.click();
  link.remove();
}
