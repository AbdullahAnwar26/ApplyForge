/**
 * Form field: label, control, hint and error wired together for assistive tech.
 * The control is rendered by `children`, which receives the ids/attributes to spread.
 */

import { useId, type ReactNode } from "react";

import { isApiError } from "../api/errors";

export interface FieldControlProps {
  id: string;
  "aria-describedby": string | undefined;
  "aria-invalid": true | undefined;
}

export function Field({
  label,
  hint,
  error,
  required = false,
  children,
}: {
  label: string;
  hint?: ReactNode;
  error?: string | null;
  required?: boolean;
  children: (control: FieldControlProps) => ReactNode;
}) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const describedBy = [hint ? hintId : null, error ? errorId : null].filter(Boolean).join(" ") || undefined;

  return (
    <div className="field">
      <label className="field__label" htmlFor={id}>
        {label}
        {required && (
          <span className="field__required" aria-hidden="true">
            {" "}
            *
          </span>
        )}
      </label>
      {children({ id, "aria-describedby": describedBy, "aria-invalid": error ? true : undefined })}
      {hint && (
        <p id={hintId} className="field__hint">
          {hint}
        </p>
      )}
      {error && (
        <p id={errorId} className="field__error">
          {error}
        </p>
      )}
    </div>
  );
}

/**
 * Errors from a failed request that are not shown next to a field: the general
 * message, non-field errors, and errors for fields the form does not display.
 */
export function FormErrors({ error, shownFields = [] }: { error: unknown; shownFields?: string[] }) {
  if (!error) return null;
  if (!isApiError(error)) {
    return (
      <div className="notice notice--error" role="alert">
        Something went wrong. Please try again.
      </div>
    );
  }
  const messages: string[] = [];
  if (error.detail) messages.push(error.detail);
  for (const [field, list] of Object.entries(error.fieldErrors)) {
    if (shownFields.includes(field)) continue;
    for (const message of list) messages.push(field === "non_field_errors" ? message : `${field}: ${message}`);
  }
  if (messages.length === 0) {
    if (Object.keys(error.fieldErrors).length > 0) return null; // all shown next to their fields
    messages.push(error.message);
  }
  return (
    <div className="notice notice--error" role="alert">
      {messages.length === 1 ? (
        messages[0]
      ) : (
        <ul className="form-errors__list">
          {messages.map((message) => (
            <li key={message}>{message}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
