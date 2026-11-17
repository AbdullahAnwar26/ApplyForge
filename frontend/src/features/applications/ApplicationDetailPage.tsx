/**
 * One application: overview, status changes (backend rules), contact/channel/resume,
 * drafts (explicit generation, review, copy, saving edits) and the status history.
 * Nothing on this page sends a message; "Sent" only records that the user sent it.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";

import {
  allowedStatusChanges,
  applicationKeys,
  getApplication,
  getApplicationEvents,
  isSent,
  updateApplication,
  type ApplicationUpdate,
} from "../../api/applications";
import { isApiError } from "../../api/errors";
import { getJob, jobKeys } from "../../api/jobs";
import { listResumes, resumeKeys } from "../../api/resources";
import { APPLICATION_CHANNELS, type ApplicationChannel, type ApplicationDetail, type ApplicationStatus, type JobDetail } from "../../api/types";
import { useApi } from "../../auth/TokenProvider";
import { StatusBadge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/Dialog";
import { Field, FormErrors } from "../../components/Field";
import { PageHeader } from "../../components/PageHeader";
import { EmptyState, ErrorState, Skeleton } from "../../components/States";
import { useToast } from "../../components/Toast";
import { formatDateTime } from "../../lib/format";
import { APPLICATION_CHANNEL, APPLICATION_STATUS } from "../../lib/labels";
import { useActiveProfile } from "../../profile/ActiveProfileProvider";
import { RequireProfile } from "../../profile/ProfileControls";
import { DraftsPanel } from "./DraftsPanel";
import { channelProblem, contactLabel } from "./StartApplication";

function Item({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="details__item">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function useApplicationUpdate(application: ApplicationDetail) {
  const api = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (changes: ApplicationUpdate) => updateApplication(api, application.id, changes),
    onSuccess: (updated, changes) => {
      queryClient.setQueryData(applicationKeys.detail(application.id), updated);
      void queryClient.invalidateQueries({ queryKey: applicationKeys.all });
      // "Sent" sets the job's status to Applied on the backend.
      if (changes.status === "SENT") void queryClient.invalidateQueries({ queryKey: jobKeys.all });
    },
  });
}

function StatusControl({ application }: { application: ApplicationDetail }) {
  const { notify } = useToast();
  const options = allowedStatusChanges(application);
  const [target, setTarget] = useState<ApplicationStatus | "">("");
  const [note, setNote] = useState("");
  const [confirmSent, setConfirmSent] = useState(false);
  const mutation = useApplicationUpdate(application);
  const selectId = useId();
  const noteId = useId();

  const apply = () =>
    mutation.mutateAsync({ status: target as ApplicationStatus, ...(note.trim() ? { note: note.trim() } : {}) }).then((updated) => {
      notify(`Status: ${APPLICATION_STATUS[updated.status].label}`, "success");
      setTarget("");
      setNote("");
    });

  return (
    <div className="status-control">
      <div className="status-control__row">
        <div className="field">
          <label htmlFor={selectId} className="field__label">
            Change status
          </label>
          <select
            id={selectId}
            className="input input--compact"
            value={target}
            disabled={mutation.isPending || options.length === 0}
            onChange={(e) => setTarget(e.target.value as ApplicationStatus)}
          >
            <option value="">Choose…</option>
            {options.map((status) => (
              <option key={status} value={status}>
                {status === "SENT" ? "Sent (I sent it myself)" : APPLICATION_STATUS[status].label}
              </option>
            ))}
          </select>
        </div>
        <div className="field status-control__note">
          <label htmlFor={noteId} className="field__label">
            Note <span className="muted">(optional)</span>
          </label>
          <input id={noteId} className="input input--compact" value={note} maxLength={2000} disabled={mutation.isPending} onChange={(e) => setNote(e.target.value)} />
        </div>
        <Button
          size="sm"
          className="status-control__save"
          loading={mutation.isPending}
          disabled={!target}
          onClick={() => {
            if (mutation.isPending || !target) return;
            if (target === "SENT") setConfirmSent(true);
            else void apply().catch(() => undefined);
          }}
        >
          Update status
        </Button>
      </div>
      <p className="field__hint">
        {isSent(application)
          ? "Sent applications can move to Follow-up or Rejected."
          : "Generated and Failed are set by draft generation. Follow-up and Rejected become available after sending."}
      </p>
      <FormErrors error={mutation.error} />

      <ConfirmDialog
        open={confirmSent}
        title="Mark as sent?"
        message="Only do this after you have sent the application yourself - ApplyForge does not send it. The drafts become read-only, the job's status becomes Applied, and the application cannot move back to an unsent status."
        confirmLabel="Yes, I sent it"
        onConfirm={async () => {
          await apply();
          setConfirmSent(false);
        }}
        onCancel={() => setConfirmSent(false)}
      />
    </div>
  );
}

function SettingsForm({ application, job }: { application: ApplicationDetail; job: JobDetail | undefined }) {
  const api = useApi();
  const { notify } = useToast();
  const resumes = useQuery({ queryKey: resumeKeys.forProfile(application.profile), queryFn: ({ signal }) => listResumes(api, application.profile, signal) });
  const [channel, setChannel] = useState<ApplicationChannel>(application.channel);
  const [contact, setContact] = useState(application.contact ? String(application.contact) : "");
  const [resume, setResume] = useState(application.resume ? String(application.resume) : "");
  const [problem, setProblem] = useState<string | null>(null);
  const mutation = useApplicationUpdate(application);

  const contacts = job?.contacts ?? [];
  const changes: ApplicationUpdate = {};
  if (channel !== application.channel) changes.channel = channel;
  if ((contact ? Number(contact) : null) !== application.contact) changes.contact = contact ? Number(contact) : null;
  if ((resume ? Number(resume) : null) !== application.resume) changes.resume = resume ? Number(resume) : null;
  const dirty = Object.keys(changes).length > 0;
  const fieldError = (field: string) => (isApiError(mutation.error) ? mutation.error.fieldMessage(field) : null);
  const resumeOptions = (resumes.data ?? []).filter((r) => r.is_active || r.id === application.resume);

  return (
    <form
      className="form"
      onSubmit={(event) => {
        event.preventDefault();
        if (!dirty || mutation.isPending) return;
        const issue = channelProblem(channel, contacts.find((c) => String(c.id) === contact));
        setProblem(issue);
        if (issue) return;
        mutation.mutate(changes, { onSuccess: () => notify("Application details saved", "success") });
      }}
      noValidate
    >
      <fieldset className="form__fields" disabled={mutation.isPending}>
        <div className="form__grid form__grid--3">
          <Field label="Contact" error={fieldError("contact")}>
            {(props) => (
              <select {...props} className="input" value={contact} onChange={(e) => setContact(e.target.value)}>
                <option value="">No contact</option>
                {contacts.map((c) => (
                  <option key={c.id} value={c.id}>
                    {contactLabel(c)}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <Field label="Channel" error={problem ?? fieldError("channel")}>
            {(props) => (
              <select {...props} className="input" value={channel} onChange={(e) => setChannel(e.target.value as ApplicationChannel)}>
                {APPLICATION_CHANNELS.map((value) => (
                  <option key={value} value={value}>
                    {APPLICATION_CHANNEL[value]}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <Field label="Resume" error={fieldError("resume")} hint="Drafts are written from this resume.">
            {(props) => (
              <select {...props} className="input" value={resume} onChange={(e) => setResume(e.target.value)}>
                <option value="">Profile default</option>
                {resumeOptions.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.name}
                    {!r.is_active ? " (inactive)" : ""}
                  </option>
                ))}
              </select>
            )}
          </Field>
        </div>
      </fieldset>
      <FormErrors error={mutation.error} shownFields={["contact", "channel", "resume"]} />
      <div className="form__actions">
        <Button
          onClick={() => {
            setChannel(application.channel);
            setContact(application.contact ? String(application.contact) : "");
            setResume(application.resume ? String(application.resume) : "");
            setProblem(null);
          }}
          disabled={!dirty || mutation.isPending}
        >
          Discard changes
        </Button>
        <Button type="submit" variant="primary" loading={mutation.isPending} disabled={!dirty}>
          Save details
        </Button>
      </div>
    </form>
  );
}

function Timeline({ applicationId }: { applicationId: number }) {
  const api = useApi();
  const events = useQuery({ queryKey: applicationKeys.events(applicationId), queryFn: ({ signal }) => getApplicationEvents(api, applicationId, signal) });
  return (
    <section className="panel" aria-labelledby="history-title">
      <h2 id="history-title" className="panel__title">
        History
      </h2>
      {events.error ? (
        <ErrorState title="Could not load the history" error={events.error} onRetry={() => void events.refetch()} />
      ) : !events.data ? (
        <Skeleton lines={3} label="Loading history" />
      ) : events.data.length === 0 ? (
        <p className="muted">No history yet.</p>
      ) : (
        <ol className="timeline">
          {events.data.map((event) => (
            <li key={event.id} className="timeline__item">
              <div className="timeline__head">
                <StatusBadge kind="application" value={event.status} />
                <time dateTime={event.timestamp} className="muted small">
                  {formatDateTime(event.timestamp)}
                </time>
              </div>
              {event.notes && <p className="timeline__note">{event.notes}</p>}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function Overview({ application, job }: { application: ApplicationDetail; job: JobDetail | undefined }) {
  const api = useApi();
  const { profiles } = useActiveProfile();
  const resumes = useQuery({ queryKey: resumeKeys.forProfile(application.profile), queryFn: ({ signal }) => listResumes(api, application.profile, signal) });
  const contact = job?.contacts.find((c) => c.id === application.contact);
  const resume = resumes.data?.find((r) => r.id === application.resume);
  const profile = profiles.find((p) => p.id === application.profile);

  return (
    <section className="panel" aria-labelledby="overview-title">
      <div className="panel__header">
        <h2 id="overview-title" className="panel__title">
          Overview
        </h2>
        <StatusBadge kind="application" value={application.status} />
      </div>
      {application.last_error && (
        <div className="notice notice--error" role="note">
          <strong>Last generation error:</strong> {application.last_error}
        </div>
      )}
      <dl className="details">
        <Item label="Job">
          <Link to={`/jobs/${application.job.id}`}>{application.job.title}</Link>
          <span className="badges job-badges">
            <span className="muted small">Job status</span>
            <StatusBadge kind="job" value={application.job.status} />
            <span className="muted small">Screening</span>
            <StatusBadge kind="screening" value={application.job.screening_outcome} />
          </span>
        </Item>
        <Item label="Company">{application.job.company?.name ?? <span className="muted">Not given</span>}</Item>
        <Item label="Profile">{profile?.name ?? <span className="muted">Profile #{application.profile}</span>}</Item>
        <Item label="Channel">{APPLICATION_CHANNEL[application.channel]}</Item>
        <Item label="Contact">
          {application.contact === null ? <span className="muted">None</span> : contact ? contactLabel(contact) : <span className="muted">Contact #{application.contact}</span>}
        </Item>
        <Item label="Resume">
          {application.resume === null ? (
            <span className="muted">Profile default (recorded when a draft is generated)</span>
          ) : (
            resume?.name ?? <span className="muted">Resume #{application.resume}</span>
          )}
        </Item>
        <Item label="Created">{formatDateTime(application.created_at)}</Item>
        <Item label="Draft generated">{formatDateTime(application.generated_at)}</Item>
        <Item label="Opened">{formatDateTime(application.opened_at)}</Item>
        <Item label="Sent">{formatDateTime(application.sent_at)}</Item>
      </dl>
      <StatusControl key={`${application.status}:${application.sent_at}`} application={application} />
    </section>
  );
}

function ApplicationView({ application }: { application: ApplicationDetail }) {
  const api = useApi();
  const { profile } = useActiveProfile();
  const job = useQuery({ queryKey: jobKeys.detail(application.job.id), queryFn: ({ signal }) => getJob(api, application.job.id, signal) });

  return (
    <>
      {profile && profile.id !== application.profile && (
        <p className="notice notice--warning" role="note">
          This application belongs to another profile than the active one ({profile.name}).
        </p>
      )}
      <Overview application={application} job={job.data} />
      <DraftsPanel application={application} />
      <section className="panel" aria-labelledby="settings-title">
        <h2 id="settings-title" className="panel__title">
          Contact, channel and resume
        </h2>
        <SettingsForm key={`${application.contact}:${application.channel}:${application.resume}`} application={application} job={job.data} />
      </section>
      <Timeline applicationId={application.id} />
    </>
  );
}

function ApplicationContent() {
  const api = useApi();
  const { id: rawId } = useParams();
  const id = /^[1-9]\d*$/.test(rawId ?? "") ? Number(rawId) : null;
  const application = useQuery({
    queryKey: applicationKeys.detail(id ?? 0),
    queryFn: ({ signal }) => getApplication(api, id!, signal),
    enabled: id !== null,
  });
  const back = (
    <Link className="back-link" to="/applications">
      ← All applications
    </Link>
  );

  if (id === null || (isApiError(application.error) && application.error.kind === "not_found")) {
    return (
      <>
        {back}
        <PageHeader title="Application not found" />
        <EmptyState title="There is no application with this id">It may belong to a removed profile, or the link is wrong.</EmptyState>
      </>
    );
  }
  if (application.error && !application.data) {
    return (
      <>
        {back}
        <PageHeader title="Application" />
        <ErrorState title="Could not load this application" error={application.error} onRetry={() => void application.refetch()} />
      </>
    );
  }
  if (!application.data) {
    return (
      <>
        {back}
        <Skeleton lines={8} label="Loading application" />
      </>
    );
  }
  const data = application.data;
  return (
    <>
      {back}
      <PageHeader title={data.job.title} description={data.job.company?.name ?? undefined} />
      <ApplicationView application={data} />
    </>
  );
}

export function ApplicationDetailPage() {
  return (
    <RequireProfile>
      <ApplicationContent />
    </RequireProfile>
  );
}
