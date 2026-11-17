/**
 * Job detail panel: the active profile's application for this job, or a form to
 * create one (POST applications/). Creating never generates a draft; that is a
 * separate, explicit step on the application page.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import {
  APPLICABLE_JOB_STATUSES,
  applicationKeys,
  createApplication,
  findApplicationForJob,
  type ApplicationCreateInput,
} from "../../api/applications";
import { isApiError } from "../../api/errors";
import { listResumes, resumeKeys } from "../../api/resources";
import { APPLICATION_CHANNELS, type ApplicationChannel, type Contact, type JobDetail, type Profile } from "../../api/types";
import { useApi } from "../../auth/TokenProvider";
import { StatusBadge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Field, FormErrors } from "../../components/Field";
import { Skeleton } from "../../components/States";
import { useToast } from "../../components/Toast";
import { APPLICATION_CHANNEL } from "../../lib/labels";

export function contactLabel(contact: Contact): string {
  const reach = [contact.email, contact.phone, contact.whatsapp && `WhatsApp ${contact.whatsapp}`].filter(Boolean).join(", ");
  return [contact.name || "Unnamed contact", reach].filter(Boolean).join(" - ");
}

/** Client-side mirror of validate_relationships for the channel/contact pair. */
export function channelProblem(channel: ApplicationChannel, contact: Contact | undefined): string | null {
  if (!contact) return null;
  if (channel === "EMAIL" && !contact.email) return "This contact has no email address. Choose another contact or channel.";
  if (channel === "WHATSAPP" && !contact.whatsapp && !contact.phone) return "This contact has no WhatsApp or phone number.";
  return null;
}

function CreateForm({ job, profile }: { job: JobDetail; profile: Profile }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { notify } = useToast();
  const resumes = useQuery({ queryKey: resumeKeys.forProfile(profile.id), queryFn: ({ signal }) => listResumes(api, profile.id, signal) });
  const firstEmail = job.contacts.find((c) => c.email);
  const [contactId, setContactId] = useState<string>(firstEmail ? String(firstEmail.id) : "");
  const [resumeId, setResumeId] = useState<string>(""); // "" = the profile's default
  const [channel, setChannel] = useState<ApplicationChannel>(firstEmail || job.contacts.length === 0 ? "EMAIL" : "OTHER");
  const [note, setNote] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const noteId = useId();

  const mutation = useMutation({
    mutationFn: (input: ApplicationCreateInput) => createApplication(api, input),
    onSuccess: (application) => {
      void queryClient.invalidateQueries({ queryKey: applicationKeys.all });
      queryClient.setQueryData(applicationKeys.detail(application.id), application);
      notify("Application created. Generate a draft when you are ready.", "success");
      navigate(`/applications/${application.id}`);
    },
    onError: (error) => {
      if (isApiError(error) && error.kind === "conflict") {
        void queryClient.invalidateQueries({ queryKey: applicationKeys.forJob(profile.id, job.id) });
      }
    },
  });

  const contact = job.contacts.find((c) => String(c.id) === contactId);
  const active = (resumes.data ?? []).filter((r) => r.is_active);
  const defaultResume = active.find((r) => r.is_default);

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (mutation.isPending) return;
    const issue = channelProblem(channel, contact);
    setProblem(issue);
    if (issue) return;
    const input: ApplicationCreateInput = { job: job.id, profile: profile.id, channel };
    if (contactId) input.contact = Number(contactId);
    if (resumeId) input.resume = Number(resumeId);
    if (note.trim()) input.note = note.trim();
    mutation.mutate(input);
  }

  const error = mutation.error;
  const fieldError = (field: string) => (isApiError(error) ? error.fieldMessage(field) : null);
  const conflictId = isApiError(error) && error.kind === "conflict" ? error.extra.application : null;

  return (
    <form className="form" onSubmit={onSubmit} noValidate>
      <fieldset className="form__fields" disabled={mutation.isPending}>
        <div className="form__grid">
          <Field label="Contact" hint={job.contacts.length ? undefined : "No contacts were found for this job."} error={fieldError("contact")}>
            {(props) => (
              <select {...props} className="input" value={contactId} onChange={(e) => setContactId(e.target.value)}>
                <option value="">No contact</option>
                {job.contacts.map((c) => (
                  <option key={c.id} value={c.id}>
                    {contactLabel(c)}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <Field label="Channel" error={problem ?? fieldError("channel")} hint="How you plan to send it yourself. ApplyForge never sends.">
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
          <Field
            label="Resume"
            error={fieldError("resume")}
            hint={
              resumes.data && !defaultResume && !resumeId ? (
                <>
                  {profile.name} has no active default resume; drafts need one. <Link to="/resumes">Manage resumes</Link>.
                </>
              ) : undefined
            }
          >
            {(props) => (
              <select {...props} className="input" value={resumeId} onChange={(e) => setResumeId(e.target.value)}>
                <option value="">{defaultResume ? `Default (${defaultResume.name})` : "Profile default (none set)"}</option>
                {active.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.name}
                  </option>
                ))}
              </select>
            )}
          </Field>
        </div>
        <div className="field">
          <label className="field__label" htmlFor={noteId}>
            Note <span className="muted">(optional, saved in the history)</span>
          </label>
          <input id={noteId} className="input" value={note} maxLength={2000} onChange={(e) => setNote(e.target.value)} />
        </div>
      </fieldset>
      {conflictId ? (
        <p className="notice notice--warning" role="alert">
          An application for this job already exists. <Link to={`/applications/${conflictId}`}>Open it</Link>.
        </p>
      ) : (
        <FormErrors error={error} shownFields={["contact", "channel", "resume"]} />
      )}
      <div className="form__actions">
        <Button type="submit" variant="primary" loading={mutation.isPending}>
          Create application
        </Button>
      </div>
    </form>
  );
}

export function StartApplication({ job, profile }: { job: JobDetail; profile: Profile }) {
  const api = useApi();
  const existing = useQuery({
    queryKey: applicationKeys.forJob(profile.id, job.id),
    queryFn: ({ signal }) => findApplicationForJob(api, profile.id, job.id, signal),
  });
  const applicable = (APPLICABLE_JOB_STATUSES as readonly string[]).includes(job.status);

  let body;
  if (existing.isPending) body = <Skeleton lines={2} label="Checking for an application" />;
  else if (existing.data) {
    body = (
      <div className="existing-application">
        <span>
          {profile.name} has an application for this job: <StatusBadge kind="application" value={existing.data.status} />
        </span>
        <Link className="btn btn--primary btn--sm" to={`/applications/${existing.data.id}`}>
          Open application
        </Link>
      </div>
    );
  } else if (!applicable) {
    body = (
      <p className="muted">
        Set your status to <strong>Shortlisted</strong> (above) to start an application for this job.
      </p>
    );
  } else {
    body = <CreateForm job={job} profile={profile} />;
  }

  return (
    <section className="panel" aria-labelledby="application-title">
      <div>
        <h2 id="application-title" className="panel__title">
          Application
        </h2>
        <p className="panel__subtitle">An application tracks this job for {profile.name}. Drafts are generated only when you ask.</p>
      </div>
      {existing.error ? <FormErrors error={existing.error} /> : body}
    </section>
  );
}

