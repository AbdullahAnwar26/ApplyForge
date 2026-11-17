/**
 * Profile & Skills. Reachable without a profile: with none, it is where the first
 * profile is created. A created profile goes straight into the active-profile
 * provider (no reload); it becomes active because the user just created it.
 */

import { useState, type ReactNode } from "react";

import type { Profile } from "../../api/types";
import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { PageHeader } from "../../components/PageHeader";
import { ErrorState, Skeleton } from "../../components/States";
import { useToast } from "../../components/Toast";
import { formatDateTime, splitList } from "../../lib/format";
import { useActiveProfile } from "../../profile/ActiveProfileProvider";
import { RequireProfile } from "../../profile/ProfileControls";
import { ProfileForm } from "./ProfileForm";
import { SkillsPanel } from "./SkillsPanel";

function Detail({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="details__item">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function orDash(value: string | null | undefined) {
  return value && value.trim() ? value : <span className="muted">Not set</span>;
}

function ProfileDetails({ profile, onEdit }: { profile: Profile; onEdit: () => void }) {
  const roles = splitList(profile.target_roles);
  return (
    <section className="panel" aria-labelledby="profile-details-title">
      <div className="panel__header">
        <div>
          <h2 id="profile-details-title" className="panel__title">
            {profile.name}
          </h2>
          <p className="panel__subtitle">Last updated {formatDateTime(profile.updated_at)}</p>
        </div>
        <Button onClick={onEdit}>Edit profile</Button>
      </div>
      <dl className="details">
        <Detail label="Target roles">
          {roles.length ? (
            <ul className="chips chips--static">
              {roles.map((role) => (
                <li key={role} className="chip">
                  {role}
                </li>
              ))}
            </ul>
          ) : (
            orDash(null)
          )}
        </Detail>
        <Detail label="Experience">{profile.experience_years ?? "—"} years</Detail>
        <Detail label="Accepts requirements up to">{profile.max_acceptable_experience_years ?? "—"} years</Detail>
        <Detail label="Preferred locations">{orDash(profile.preferred_locations)}</Detail>
        <Detail label="Preferred work modes">{orDash(profile.preferred_work_modes)}</Detail>
        <Detail label="Professional summary">
          <span className="prewrap">{orDash(profile.professional_summary)}</span>
        </Detail>
        <Detail label="Experience details">
          <span className="prewrap">{orDash(profile.experience_info)}</span>
        </Detail>
      </dl>
    </section>
  );
}

function ActiveProfileView({ profile }: { profile: Profile }) {
  const { storeProfile } = useActiveProfile();
  const { notify } = useToast();
  const [editing, setEditing] = useState(false);

  return (
    <>
      {editing ? (
        <section className="panel" aria-labelledby="profile-edit-title">
          <h2 id="profile-edit-title" className="panel__title">
            Edit profile
          </h2>
          <ProfileForm
            key={profile.id}
            profile={profile}
            submitLabel="Save changes"
            onCancel={() => setEditing(false)}
            onSaved={(saved) => {
              storeProfile(saved);
              setEditing(false);
              notify("Profile saved", "success");
            }}
          />
        </section>
      ) : (
        <ProfileDetails profile={profile} onEdit={() => setEditing(true)} />
      )}

      <section className="panel" aria-labelledby="skills-title">
        <div className="panel__header">
          <div>
            <h2 id="skills-title" className="panel__title">
              Skills &amp; keywords
            </h2>
            <p className="panel__subtitle">
              A job is relevant when at least one of these appears in its description.
            </p>
          </div>
        </div>
        <SkillsPanel key={profile.id} profile={profile} />
      </section>
    </>
  );
}

export function ProfilePage() {
  const { state, storeProfile, refetch } = useActiveProfile();
  const { notify } = useToast();
  const [creating, setCreating] = useState(false);

  const created = (profile: Profile) => {
    storeProfile(profile, { select: true });
    setCreating(false);
    notify(`Created ${profile.name}`, "success");
  };

  const canCreateMore = state.status === "ready" || state.status === "needs-selection";

  return (
    <>
      <PageHeader
        title="Profile & Skills"
        description="The profile jobs are screened against, and the skills that make a job relevant."
        actions={
          canCreateMore ? (
            <Button onClick={() => setCreating(true)} disabled={creating}>
              New profile
            </Button>
          ) : undefined
        }
      />

      {state.status === "loading" && <Skeleton lines={5} label="Loading profiles" />}
      {state.status === "error" && <ErrorState title="Could not load profiles" error={state.error} onRetry={refetch} />}
      {state.status === "empty" && (
        <section className="panel" aria-labelledby="first-profile-title">
          <h2 id="first-profile-title" className="panel__title">
            Create your first profile
          </h2>
          <p className="panel__subtitle">
            ApplyForge screens jobs and prepares applications for this profile. Only include information that is true.
          </p>
          <ProfileForm submitLabel="Create profile" onSaved={created} />
        </section>
      )}
      {(state.status === "ready" || state.status === "needs-selection") && (
        <RequireProfile>
          {state.status === "ready" && <ActiveProfileView profile={state.profile} />}
        </RequireProfile>
      )}

      <Dialog open={creating} title="New profile" onClose={() => setCreating(false)} wide>
        <p className="muted">The new profile becomes the active profile. Switch back at any time from the top bar.</p>
        <ProfileForm submitLabel="Create profile" onSaved={created} onCancel={() => setCreating(false)} />
      </Dialog>
    </>
  );
}
