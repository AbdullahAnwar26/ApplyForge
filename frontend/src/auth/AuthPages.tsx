/**
 * Sign-in and sign-up pages (shown whenever nobody is signed in). After success the app
 * opens at the address the user asked for. Passwords are only sent to the backend; they
 * are never stored, logged or shown in an error.
 */

import { useId, useState, type FormEvent, type ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";

import { isApiError } from "../api/errors";
import { Button } from "../components/Button";
import { useDocumentTitle } from "../lib/useDocumentTitle";
import { useAuth } from "./AuthProvider";

type Errors = Partial<Record<"username" | "email" | "password" | "confirm" | "form", string>>;

function messageFor(error: unknown): Errors {
  if (isApiError(error)) {
    if (error.kind === "network") return { form: "Cannot reach the ApplyForge server. Check that it is running and try again." };
    if (error.kind === "throttled") return { form: "Too many attempts. Wait a minute and try again." };
    const fields: Errors = {};
    for (const key of ["username", "email", "password"] as const) {
      const message = error.fieldMessage(key);
      if (message) fields[key] = message;
    }
    if (Object.keys(fields).length) return fields;
    return { form: error.message };
  }
  return { form: "Something went wrong. Please try again." };
}

function AuthCard({ title, lead, children }: { title: string; lead: string; children: ReactNode }) {
  return (
    <main className="connect">
      <div className="connect__card">
        <div className="connect__brand">
          <span className="brand__mark" aria-hidden="true">
            AF
          </span>
          <span className="connect__product">ApplyForge</span>
        </div>
        <h1 className="connect__title">{title}</h1>
        <p className="connect__lead">{lead}</p>
        {children}
      </div>
    </main>
  );
}

function TextField({
  label,
  value,
  onChange,
  error,
  hint,
  type = "text",
  autoComplete,
  disabled,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  error?: string;
  hint?: string;
  type?: string;
  autoComplete: string;
  disabled: boolean;
}) {
  const id = useId();
  return (
    <div className="field">
      <label className="field__label" htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        className="field__input"
        type={type}
        value={value}
        autoComplete={autoComplete}
        autoCapitalize="off"
        autoCorrect="off"
        spellCheck={false}
        disabled={disabled}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? `${id}-error` : hint ? `${id}-hint` : undefined}
        onChange={(event) => onChange(event.target.value)}
      />
      {hint && !error && (
        <p id={`${id}-hint`} className="field__hint">
          {hint}
        </p>
      )}
      {error && (
        <p id={`${id}-error`} className="field__error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

function ShowPassword({ shown, onChange }: { shown: boolean; onChange: (shown: boolean) => void }) {
  return (
    <label className="checkbox auth__show">
      <input type="checkbox" checked={shown} onChange={(event) => onChange(event.target.checked)} />
      <span>Show password</span>
    </label>
  );
}

function RememberMe({ value, onChange }: { value: boolean; onChange: (value: boolean) => void }) {
  return (
    <label className="checkbox">
      <input type="checkbox" checked={value} onChange={(event) => onChange(event.target.checked)} />
      <span>
        Keep me signed in <small className="muted">(for two weeks on this device)</small>
      </span>
    </label>
  );
}

export function LoginPage() {
  const { signIn, signOutReason } = useAuth();
  const { search } = useLocation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(true);
  const [shown, setShown] = useState(false);
  const [errors, setErrors] = useState<Errors>({});
  const [busy, setBusy] = useState(false);
  useDocumentTitle("Sign in");

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    const missing: Errors = {};
    if (!username.trim()) missing.username = "Enter your username.";
    if (!password) missing.password = "Enter your password.";
    setErrors(missing);
    if (Object.keys(missing).length) return;
    setBusy(true);
    try {
      await signIn({ username: username.trim(), password, remember });
    } catch (error) {
      setPassword("");
      setErrors(messageFor(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthCard title="Sign in" lead="Sign in to your ApplyForge account.">
      {signOutReason === "expired" && (
        <p className="notice notice--warning" role="status">
          Your session ended. Sign in again to continue.
        </p>
      )}
      {signOutReason === "user" && (
        <p className="notice" role="status">
          You are signed out.
        </p>
      )}
      <form className="form auth__form" onSubmit={onSubmit} noValidate>
        <TextField label="Username" value={username} onChange={setUsername} error={errors.username} autoComplete="username" disabled={busy} />
        <TextField
          label="Password"
          type={shown ? "text" : "password"}
          value={password}
          onChange={setPassword}
          error={errors.password}
          autoComplete="current-password"
          disabled={busy}
        />
        <ShowPassword shown={shown} onChange={setShown} />
        <RememberMe value={remember} onChange={setRemember} />
        {errors.form && (
          <p className="notice notice--error" role="alert">
            {errors.form}
          </p>
        )}
        <Button variant="primary" type="submit" loading={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </Button>
      </form>
      <p className="auth__switch">
        New to ApplyForge? <Link to={{ pathname: "/signup", search }}>Create an account</Link>
      </p>
    </AuthCard>
  );
}

export function SignupPage() {
  const { signUp } = useAuth();
  const { search } = useLocation();
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [shown, setShown] = useState(false);
  const [errors, setErrors] = useState<Errors>({});
  const [busy, setBusy] = useState(false);
  useDocumentTitle("Create an account");

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    const problems: Errors = {};
    const name = username.trim();
    if (!name) problems.username = "Choose a username.";
    else if (!/^[A-Za-z0-9_.@+-]{3,150}$/.test(name)) problems.username = "Use 3-150 characters: letters, digits and . _ @ + - (no spaces).";
    if (email.trim() && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim())) problems.email = "Enter a valid email address, or leave it empty.";
    if (password.length < 8) problems.password = "Use at least 8 characters.";
    if (!shown && confirm !== password) problems.confirm = "The passwords do not match.";
    setErrors(problems);
    if (Object.keys(problems).length) return;
    setBusy(true);
    try {
      await signUp({ username: name, email: email.trim(), password, remember: true });
    } catch (error) {
      setErrors(messageFor(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthCard title="Create an account" lead="Your profiles, jobs and applications are private to your account.">
      <form className="form auth__form" onSubmit={onSubmit} noValidate>
        <TextField
          label="Username"
          value={username}
          onChange={setUsername}
          error={errors.username}
          hint="Letters, digits and . _ @ + - ; used to sign in."
          autoComplete="username"
          disabled={busy}
        />
        <TextField
          label="Email (optional)"
          type="email"
          value={email}
          onChange={setEmail}
          error={errors.email}
          autoComplete="email"
          disabled={busy}
        />
        <TextField
          label="Password"
          type={shown ? "text" : "password"}
          value={password}
          onChange={setPassword}
          error={errors.password}
          hint="At least 8 characters; not a common password and not only digits."
          autoComplete="new-password"
          disabled={busy}
        />
        {!shown && (
          <TextField
            label="Confirm password"
            type="password"
            value={confirm}
            onChange={setConfirm}
            error={errors.confirm}
            autoComplete="new-password"
            disabled={busy}
          />
        )}
        <ShowPassword shown={shown} onChange={setShown} />
        {errors.form && (
          <p className="notice notice--error" role="alert">
            {errors.form}
          </p>
        )}
        <Button variant="primary" type="submit" loading={busy}>
          {busy ? "Creating your account…" : "Create account"}
        </Button>
      </form>
      <p className="auth__switch">
        Already have an account? <Link to={{ pathname: "/login", search }}>Sign in</Link>
      </p>
    </AuthCard>
  );
}

/** Shown while checking the session, or when the server cannot be reached. */
export function AuthStatusPage({ unreachable, onRetry }: { unreachable: boolean; onRetry: () => void }) {
  useDocumentTitle(unreachable ? "Server unreachable" : "Loading");
  if (!unreachable) {
    return (
      <main className="connect" aria-busy="true">
        <p className="muted">Loading ApplyForge…</p>
      </main>
    );
  }
  return (
    <AuthCard title="Cannot reach the server" lead="The ApplyForge server did not respond.">
      <p className="muted">Check that the server is running, then try again.</p>
      <Button variant="primary" onClick={onRetry}>
        Try again
      </Button>
    </AuthCard>
  );
}
