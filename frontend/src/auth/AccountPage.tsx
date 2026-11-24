/** Account page: who is signed in, password change and sign-out. */

import { useId, useState, type FormEvent } from "react";

import { changePassword } from "../api/auth";
import { isApiError } from "../api/errors";
import { Button } from "../components/Button";
import { PageHeader } from "../components/PageHeader";
import { useToast } from "../components/Toast";
import { formatDate } from "../lib/format";
import { useDocumentTitle } from "../lib/useDocumentTitle";
import { useAuth } from "./AuthProvider";
import { EmailSendingSection } from "./EmailSendingSection";

type Errors = Partial<Record<"current" | "next" | "confirm" | "form", string>>;

function PasswordForm() {
  const { api } = useAuth();
  const { notify } = useToast();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [errors, setErrors] = useState<Errors>({});
  const [busy, setBusy] = useState(false);
  const ids = { current: useId(), next: useId(), confirm: useId() };

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    const problems: Errors = {};
    if (!current) problems.current = "Enter your current password.";
    if (next.length < 8) problems.next = "Use at least 8 characters.";
    else if (next === current) problems.next = "Choose a password different from the current one.";
    if (confirm !== next) problems.confirm = "The new passwords do not match.";
    setErrors(problems);
    if (Object.keys(problems).length) return;
    setBusy(true);
    try {
      await changePassword(api, current, next);
      setCurrent("");
      setNext("");
      setConfirm("");
      notify("Password changed. Other devices are signed out.", "success");
    } catch (error) {
      if (isApiError(error)) {
        setErrors({
          current: error.fieldMessage("current_password") ?? undefined,
          next: error.fieldMessage("new_password") ?? undefined,
          form:
            error.fieldMessage("current_password") || error.fieldMessage("new_password")
              ? undefined
              : error.kind === "throttled"
                ? "Too many attempts. Wait a minute and try again."
                : error.message,
        });
      } else {
        setErrors({ form: "Something went wrong. Please try again." });
      }
    } finally {
      setBusy(false);
    }
  }

  const field = (key: "current" | "next" | "confirm", label: string, value: string, set: (v: string) => void, autoComplete: string) => (
    <div className="field">
      <label className="field__label" htmlFor={ids[key]}>
        {label}
      </label>
      <input
        id={ids[key]}
        className="field__input"
        type="password"
        value={value}
        autoComplete={autoComplete}
        disabled={busy}
        aria-invalid={errors[key] ? true : undefined}
        onChange={(event) => set(event.target.value)}
      />
      {errors[key] && (
        <p className="field__error" role="alert">
          {errors[key]}
        </p>
      )}
    </div>
  );

  return (
    <form className="form account__password" onSubmit={onSubmit} noValidate>
      {field("current", "Current password", current, setCurrent, "current-password")}
      {field("next", "New password", next, setNext, "new-password")}
      {field("confirm", "Confirm new password", confirm, setConfirm, "new-password")}
      {errors.form && (
        <p className="notice notice--error" role="alert">
          {errors.form}
        </p>
      )}
      <div className="form__actions">
        <Button type="submit" variant="primary" loading={busy}>
          Change password
        </Button>
      </div>
    </form>
  );
}

export function AccountPage() {
  const { user, signOut } = useAuth();
  const [signingOut, setSigningOut] = useState(false);
  useDocumentTitle("Account");
  if (!user) return null;

  return (
    <>
      <PageHeader title="Account" description="Your sign-in details. Your profiles, jobs and applications are visible only to you." />
      <section className="panel" aria-labelledby="account-title">
        <h2 id="account-title" className="panel__title">
          Signed in as {user.username}
        </h2>
        <dl className="details">
          <div className="details__item">
            <dt>Username</dt>
            <dd>{user.username}</dd>
          </div>
          <div className="details__item">
            <dt>Email</dt>
            <dd>{user.email || <span className="muted">Not set</span>}</dd>
          </div>
          <div className="details__item">
            <dt>Member since</dt>
            <dd>{formatDate(user.date_joined)}</dd>
          </div>
        </dl>
        <div className="form__actions">
          <Button
            loading={signingOut}
            onClick={() => {
              setSigningOut(true);
              void signOut().finally(() => setSigningOut(false));
            }}
          >
            Sign out
          </Button>
        </div>
      </section>
      <EmailSendingSection />
      <section className="panel" aria-labelledby="password-title">
        <h2 id="password-title" className="panel__title">
          Change password
        </h2>
        <PasswordForm />
      </section>
    </>
  );
}
