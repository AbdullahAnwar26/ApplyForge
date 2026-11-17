/** Empty, error and loading placeholders shared by every screen. */

import type { ReactNode } from "react";

import { isApiError } from "../api/errors";
import { Button } from "./Button";

export function EmptyState({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="state state--empty">
      <p className="state__title">{title}</p>
      {children && <div className="state__body">{children}</div>}
      {action && <div className="state__action">{action}</div>}
    </div>
  );
}

/** A readable message for any error; never includes request details. */
export function errorMessage(error: unknown): string {
  if (isApiError(error)) {
    if (error.kind === "network") return "Cannot reach the ApplyForge backend. Is the Django server running?";
    return error.message;
  }
  return "Something went wrong.";
}

export function ErrorState({
  error,
  title = "Could not load this",
  onRetry,
}: {
  error: unknown;
  title?: string;
  onRetry?: () => void;
}) {
  return (
    <div className="state state--error" role="alert">
      <p className="state__title">{title}</p>
      <p className="state__body">{errorMessage(error)}</p>
      {onRetry && (
        <div className="state__action">
          <Button size="sm" onClick={onRetry}>
            Try again
          </Button>
        </div>
      )}
    </div>
  );
}

export function Skeleton({ lines = 3, label = "Loading" }: { lines?: number; label?: string }) {
  return (
    <div className="skeleton" role="status" aria-label={label}>
      {Array.from({ length: lines }, (_, i) => (
        <span key={i} className="skeleton__line" style={{ width: `${92 - ((i * 17) % 40)}%` }} />
      ))}
    </div>
  );
}
