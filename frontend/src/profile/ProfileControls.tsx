/** Top-bar profile switcher and the gate for pages that need an active profile. */

import { useId, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { Button } from "../components/Button";
import { PageHeader } from "../components/PageHeader";
import { EmptyState, ErrorState, Skeleton } from "../components/States";
import { useToast } from "../components/Toast";
import { useActiveProfile } from "./ActiveProfileProvider";

export function ProfileSwitcher() {
  const { state, profiles, select } = useActiveProfile();
  const { notify } = useToast();
  const id = useId();

  const selectable = state.status === "ready" || state.status === "needs-selection";
  const value = state.status === "ready" ? String(state.profile.id) : "";

  return (
    <div className="profile-switcher">
      <label htmlFor={id} className="profile-switcher__label">
        Profile
      </label>
      <select
        id={id}
        className="profile-switcher__select"
        value={value}
        disabled={!selectable}
        aria-invalid={state.status === "needs-selection" ? true : undefined}
        onChange={(event) => {
          const next = Number(event.target.value);
          const profile = profiles.find((p) => p.id === next);
          if (profile && select(next)) notify(`Active profile: ${profile.name}`, "success");
        }}
      >
        {state.status === "loading" && <option value="">Loading…</option>}
        {state.status === "error" && <option value="">Unavailable</option>}
        {state.status === "empty" && <option value="">No profiles</option>}
        {state.status === "needs-selection" && (
          <option value="" disabled>
            Select a profile…
          </option>
        )}
        {profiles.map((profile) => (
          <option key={profile.id} value={profile.id}>
            {profile.name}
          </option>
        ))}
      </select>
    </div>
  );
}

/** Inline chooser shown when a valid profile must be picked before continuing. */
function ProfileChooser({ reason }: { reason: "unselected" | "missing" }) {
  const { profiles, select } = useActiveProfile();
  const [choice, setChoice] = useState<string>("");
  const id = useId();

  return (
    <div className="state state--attention" role="region" aria-labelledby={`${id}-title`}>
      <p id={`${id}-title`} className="state__title">
        {reason === "missing" ? "The selected profile no longer exists" : "Choose a profile"}
      </p>
      <p className="state__body">
        {reason === "missing"
          ? "The profile chosen earlier in this browser was not found. Select the profile to work with."
          : "There are several profiles. Select the one to work with; you can change it later from the top bar."}
      </p>
      <form
        className="profile-chooser"
        onSubmit={(event) => {
          event.preventDefault();
          if (choice) select(Number(choice));
        }}
      >
        <label htmlFor={id} className="visually-hidden">
          Profile
        </label>
        <select id={id} className="input" value={choice} onChange={(event) => setChoice(event.target.value)}>
          <option value="" disabled>
            Select a profile…
          </option>
          {profiles.map((profile) => (
            <option key={profile.id} value={profile.id}>
              {profile.name}
            </option>
          ))}
        </select>
        <Button type="submit" variant="primary" disabled={!choice}>
          Use this profile
        </Button>
      </form>
    </div>
  );
}

/**
 * Renders `children` only when an active profile is set; otherwise explains what
 * is needed (loading, load error with retry, no profiles yet, or a choice).
 */
export function RequireProfile({ children }: { children: ReactNode }) {
  const { state, refetch } = useActiveProfile();
  switch (state.status) {
    case "loading":
      return <Skeleton lines={4} label="Loading profiles" />;
    case "error":
      return <ErrorState title="Could not load profiles" error={state.error} onRetry={refetch} />;
    case "empty":
      // A new account lands here first: a real page heading, then the one next step.
      return (
        <>
          <PageHeader title="Welcome to ApplyForge" description="Set up your profile to start tracking jobs and applications." />
          <EmptyState
            title="No profile yet"
            action={
              <Link className="btn btn--primary" to="/profile">
                Go to Profile &amp; Skills
              </Link>
            }
          >
            ApplyForge screens jobs and prepares applications for a profile. Create one to get started.
          </EmptyState>
        </>
      );
    case "needs-selection":
      return <ProfileChooser reason={state.reason} />;
    case "ready":
      return <>{children}</>;
  }
}
