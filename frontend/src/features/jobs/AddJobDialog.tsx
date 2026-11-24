/**
 * Manual job entry through POST jobs/ingest/ - the same in-process ingestion the
 * collector uses, with no LinkedIn or collector involvement. The backend matches
 * an existing job by source URL, then by description; a match is returned
 * unchanged (200) instead of creating a duplicate. New jobs are screened with the
 * given profile (the active one).
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";

import { isApiError } from "../../api/errors";
import { companyKeys, ingestJob, jobKeys } from "../../api/jobs";
import { JOB_SOURCES, type JobContactInput, type JobIngestInput, type JobSource, type Profile } from "../../api/types";
import { useApi } from "../../auth/AuthProvider";
import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { Field, FormErrors } from "../../components/Field";
import { useToast } from "../../components/Toast";
import { JOB_SOURCE, SCREENING_OUTCOME } from "../../lib/labels";

interface Values {
  title: string;
  company_name: string;
  company_website: string;
  location: string;
  work_mode: string;
  source: JobSource;
  source_url: string;
  posted_date: string;
  required_experience_text: string;
  full_jd: string;
}

const EMPTY: Values = {
  title: "",
  company_name: "",
  company_website: "",
  location: "",
  work_mode: "",
  source: "MANUAL",
  source_url: "",
  posted_date: "",
  required_experience_text: "",
  full_jd: "",
};

const EMPTY_CONTACT: Required<JobContactInput> = { name: "", email: "", phone: "", whatsapp: "" };
type ContactErrors = Partial<Record<keyof JobContactInput | "contact", string>>;

const isHttpUrl = (value: string) => /^https?:\/\/[^\s/$.?#][^\s]*$/i.test(value);
const digits = (value: string) => value.replace(/\D/g, "");

export function validateJob(values: Values, contacts: Required<JobContactInput>[]) {
  const errors: Partial<Record<keyof Values, string>> = {};
  const contactErrors: ContactErrors[] = contacts.map(() => ({}));
  if (!values.title.trim()) errors.title = "Enter the job title.";
  else if (values.title.trim().length > 255) errors.title = "Use at most 255 characters.";
  if (!values.full_jd.trim()) errors.full_jd = "Paste the full job description - screening and drafts depend on it.";
  if (values.source_url.trim() && !isHttpUrl(values.source_url.trim())) errors.source_url = "Enter an http(s) link.";
  if (values.company_website.trim() && !isHttpUrl(values.company_website.trim()))
    errors.company_website = "Enter an http(s) link.";
  if (values.required_experience_text.trim().length > 255) errors.required_experience_text = "Use at most 255 characters.";

  contacts.forEach((contact, index) => {
    const e = contactErrors[index]!;
    if (contact.email.trim() && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(contact.email.trim())) e.email = "Enter a valid email address.";
    for (const key of ["phone", "whatsapp"] as const) {
      if (contact[key].trim() && digits(contact[key]).length < 7) e[key] = "Must contain at least 7 digits.";
    }
    const filled = Object.values(contact).some((v) => v.trim());
    if (filled && !contact.email.trim() && !contact.phone.trim() && !contact.whatsapp.trim()) {
      e.contact = "A contact needs an email, phone or WhatsApp number.";
    }
  });
  const valid = Object.keys(errors).length === 0 && contactErrors.every((e) => Object.keys(e).length === 0);
  return { errors, contactErrors, valid };
}

export function buildIngestPayload(values: Values, contacts: Required<JobContactInput>[], profileId: number): JobIngestInput {
  const payload: JobIngestInput = { title: values.title.trim(), full_jd: values.full_jd.trim(), profile: profileId, source: values.source };
  const optional = ["company_name", "company_website", "location", "work_mode", "source_url", "required_experience_text"] as const;
  for (const key of optional) {
    const value = values[key].trim();
    if (value) payload[key] = value;
  }
  if (values.posted_date) payload.posted_date = `${values.posted_date}T00:00:00Z`; // a day, stored as midnight UTC
  const filled = contacts
    .map((c) => Object.fromEntries(Object.entries(c).map(([k, v]) => [k, v.trim()]).filter(([, v]) => v)) as JobContactInput)
    .filter((c) => Object.keys(c).length > 0);
  if (filled.length) payload.contacts = filled;
  return payload;
}

export function AddJobDialog({ open, profile, onClose }: { open: boolean; profile: Profile; onClose: () => void }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { notify } = useToast();
  const [values, setValues] = useState<Values>(EMPTY);
  const [contacts, setContacts] = useState<Required<JobContactInput>[]>([]);
  const [errors, setErrors] = useState<ReturnType<typeof validateJob>>({ errors: {}, contactErrors: [], valid: true });

  const mutation = useMutation({
    mutationFn: (payload: JobIngestInput) => ingestJob(api, payload),
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: jobKeys.all });
      void queryClient.invalidateQueries({ queryKey: companyKeys.all });
      queryClient.setQueryData(jobKeys.detail(result.job.id), result.job);
      if (result.created) {
        const outcome = result.job.screening_outcome ? SCREENING_OUTCOME[result.job.screening_outcome].label : "not screened";
        notify(`Added “${result.job.title}” - ${outcome}`, "success");
      } else {
        const how = result.matched_by === "source_url" ? "same link" : "same description";
        notify(`This job already exists (${how}); showing the existing job.`, "warning");
      }
      setValues(EMPTY);
      setContacts([]);
      onClose();
      navigate(`/jobs/${result.job.id}`);
    },
  });

  const set = (key: keyof Values, value: string) => setValues((current) => ({ ...current, [key]: value }));
  const serverError = mutation.error;
  const errorFor = (key: keyof Values) =>
    errors.errors[key] ?? (isApiError(serverError) ? serverError.fieldMessage(key) : null);

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (mutation.isPending) return;
    const result = validateJob(values, contacts);
    setErrors(result);
    if (!result.valid) return;
    mutation.mutate(buildIngestPayload(values, contacts, profile.id));
  }

  const text = (key: keyof Values, label: string, options: { required?: boolean; hint?: string; type?: string } = {}) => (
    <Field label={label} hint={options.hint} error={errorFor(key)} required={options.required}>
      {(props) => (
        <input {...props} className="input" type={options.type ?? "text"} value={values[key]} onChange={(e) => set(key, e.target.value)} />
      )}
    </Field>
  );

  return (
    <Dialog open={open} title="Add a job manually" onClose={onClose} dismissible={!mutation.isPending} wide>
      <form className="form" onSubmit={onSubmit} noValidate>
        <p className="muted">
          The job is saved and screened against {profile.name}. If the same link or description already exists, the existing
          job is shown instead. Nothing is sent and LinkedIn is not contacted.
        </p>
        <fieldset className="form__fields" disabled={mutation.isPending}>
          <div className="form__grid">
            {text("title", "Job title", { required: true })}
            {text("company_name", "Company")}
            {text("location", "Location")}
            {text("work_mode", "Work mode", { hint: "e.g. Remote, Hybrid, On-site." })}
            <Field label="Source" error={errorFor("source")}>
              {(props) => (
                <select {...props} className="input" value={values.source} onChange={(e) => set("source", e.target.value)}>
                  {JOB_SOURCES.map((source) => (
                    <option key={source} value={source}>
                      {JOB_SOURCE[source]}
                    </option>
                  ))}
                </select>
              )}
            </Field>
            {text("source_url", "Link to the posting", { type: "url", hint: "Optional. Used to avoid duplicates." })}
            {text("company_website", "Company website", { type: "url" })}
            {text("posted_date", "Posted on", { type: "date" })}
          </div>
          {text("required_experience_text", "Experience required", { hint: 'As written, e.g. "2-4 years". Optional.' })}
          <Field label="Full job description" required error={errorFor("full_jd")} hint="Plain text; screening reads this.">
            {(props) => (
              <textarea {...props} className="input input--multiline" rows={8} value={values.full_jd} onChange={(e) => set("full_jd", e.target.value)} />
            )}
          </Field>

          <div className="contacts-editor">
            <h3 className="contacts-editor__title">Contacts</h3>
            {contacts.length === 0 && <p className="muted small">Optional. Add the recruiter if the posting names one.</p>}
            {contacts.map((contact, index) => {
              const e = errors.contactErrors[index] ?? {};
              return (
                <fieldset key={index} className="contact-row">
                  <legend className="visually-hidden">Contact {index + 1}</legend>
                  {(["name", "email", "phone", "whatsapp"] as const).map((key) => (
                    <Field key={key} label={`${key === "whatsapp" ? "WhatsApp" : key[0]!.toUpperCase() + key.slice(1)} (contact ${index + 1})`} error={e[key]}>
                      {(props) => (
                        <input
                          {...props}
                          className="input"
                          type={key === "email" ? "email" : "text"}
                          value={contact[key]}
                          onChange={(event) =>
                            setContacts((current) => current.map((c, i) => (i === index ? { ...c, [key]: event.target.value } : c)))
                          }
                        />
                      )}
                    </Field>
                  ))}
                  {e.contact && <p className="field__error contact-row__error">{e.contact}</p>}
                  <Button size="sm" variant="ghost" onClick={() => setContacts((current) => current.filter((_, i) => i !== index))}>
                    Remove contact {index + 1}
                  </Button>
                </fieldset>
              );
            })}
            {contacts.length < 5 && (
              <Button size="sm" onClick={() => setContacts((current) => [...current, { ...EMPTY_CONTACT }])}>
                Add contact
              </Button>
            )}
          </div>
        </fieldset>

        <FormErrors error={serverError} shownFields={Object.keys(EMPTY)} />

        <div className="form__actions">
          <Button onClick={onClose} disabled={mutation.isPending}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={mutation.isPending}>
            Add job
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
