import { useEffect, useState } from "react";

import { copyText } from "../lib/clipboard";
import { Button } from "./Button";

type CopyState = "idle" | "copied" | "failed";

const capitalize = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

/** Copies `text` and says so (visibly and to screen readers) for a few seconds. */
export function CopyButton({ text, label, disabled = false }: { text: string; label: string; disabled?: boolean }) {
  const [state, setState] = useState<CopyState>("idle");

  useEffect(() => {
    if (state === "idle") return;
    const timer = window.setTimeout(() => setState("idle"), 2500);
    return () => window.clearTimeout(timer);
  }, [state]);

  return (
    <span className="copy-button">
      <Button size="sm" disabled={disabled || !text} onClick={async () => setState((await copyText(text)) ? "copied" : "failed")}>
        {state === "copied" ? "Copied" : label}
      </Button>
      <span className={`copy-button__status copy-button__status--${state}`} role="status" aria-live="polite">
        {state === "copied" && `${capitalize(label.replace(/^Copy /, ""))} copied to the clipboard.`}
        {state === "failed" && "Could not copy. Select the text and copy it manually."}
      </span>
    </span>
  );
}
