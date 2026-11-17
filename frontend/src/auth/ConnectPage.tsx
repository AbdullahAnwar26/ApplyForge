/**
 * Connect screen: the user pastes the backend's API_TOKEN. It is checked with one
 * authenticated request before it is kept. The token is never echoed back, logged,
 * or put in a URL; error messages come from the response body only.
 */

import { useId, useState, type FormEvent } from "react";

import { createApiClient } from "../api/client";
import { Button } from "../components/Button";
import { useDocumentTitle } from "../lib/useDocumentTitle";
import { isApiError } from "../api/errors";
import { API_BASE_URL, useToken } from "./TokenProvider";
import { normalizeToken, type TokenMode } from "./tokenStorage";

function connectErrorMessage(error: unknown): string {
  if (isApiError(error)) {
    switch (error.kind) {
      case "unauthorized":
        return "The backend rejected this token. Check API_TOKEN in the backend's .env.";
      case "network":
        return "Cannot reach the ApplyForge backend. Is the Django server running (python manage.py runserver)?";
      case "server":
        return "The backend reported an error. Check the Django server log.";
      default:
        return error.message;
    }
  }
  return "Could not connect. Please try again.";
}

export function ConnectPage({ baseUrl = API_BASE_URL }: { baseUrl?: string }) {
  const { connect, lastDisconnect } = useToken();
  const [value, setValue] = useState("");
  const [mode, setMode] = useState<TokenMode>("session");
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const tokenId = useId();
  const errorId = useId();
  useDocumentTitle("Connect");

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    const token = normalizeToken(value);
    if (!token) {
      setError("Enter the API token (a single value without spaces).");
      return;
    }
    setChecking(true);
    setError(null);
    try {
      // A cheap authenticated read; it only proves the token is accepted.
      await createApiClient({ baseUrl, getToken: () => token }).list("profiles/", { page: { pageSize: 1 } });
      setValue("");
      connect(token, mode);
    } catch (caught) {
      setError(connectErrorMessage(caught));
    } finally {
      setChecking(false);
    }
  }

  return (
    <main className="connect">
      <form className="connect__card" onSubmit={onSubmit} noValidate aria-describedby={error ? errorId : undefined}>
        <div className="connect__brand">
          <span className="brand__mark" aria-hidden="true">
            AF
          </span>
          <h1 className="connect__title">ApplyForge</h1>
        </div>
        <p className="connect__lead">Connect to your local ApplyForge backend.</p>

        {lastDisconnect === "unauthorized" && (
          <p className="notice notice--warning" role="status">
            The backend no longer accepts the saved token. Enter it again to continue.
          </p>
        )}
        {lastDisconnect === "user" && (
          <p className="notice" role="status">
            Disconnected. The token was removed from this browser.
          </p>
        )}

        <label className="field__label" htmlFor={tokenId}>
          API token
        </label>
        <input
          id={tokenId}
          className="field__input"
          type="password"
          name="api-token"
          autoComplete="off"
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
          value={value}
          onChange={(event) => setValue(event.target.value)}
          aria-invalid={error ? true : undefined}
          disabled={checking}
          required
        />
        <p className="field__hint">The value of API_TOKEN in the backend's .env file.</p>

        <fieldset className="connect__modes" disabled={checking}>
          <legend className="field__label">Keep the token</legend>
          <label className="radio">
            <input type="radio" name="token-mode" value="session" checked={mode === "session"} onChange={() => setMode("session")} />
            <span>
              For this browser session <small>(survives a reload; cleared when the browser closes)</small>
            </span>
          </label>
          <label className="radio">
            <input type="radio" name="token-mode" value="memory" checked={mode === "memory"} onChange={() => setMode("memory")} />
            <span>
              In memory only <small>(cleared on reload)</small>
            </span>
          </label>
        </fieldset>

        {error && (
          <p id={errorId} className="notice notice--error" role="alert">
            {error}
          </p>
        )}

        <Button variant="primary" type="submit" loading={checking}>
          {checking ? "Checking…" : "Connect"}
        </Button>
      </form>
    </main>
  );
}
