/**
 * Drafts: explicit generation (POST generate/, no body), review, copy and saving
 * edits (PATCH of the three draft fields - the backend supports it until the
 * application is sent). Copy only: nothing is ever sent from here.
 *
 * Generation failures leave the stored drafts and status unchanged (backend), and
 * this editor keeps whatever the user typed: it is only reset by a successful
 * generation or a confirmed save.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useState } from "react";

import {
  applicationKeys,
  canEditDrafts,
  GENERATABLE_STATUSES,
  generateDrafts,
  updateApplication,
  type ApplicationUpdate,
} from "../../api/applications";
import type { ApplicationDetail, GenerationResult } from "../../api/types";
import { useApi } from "../../auth/AuthProvider";
import { Button } from "../../components/Button";
import { CopyButton } from "../../components/CopyButton";
import { ConfirmDialog } from "../../components/Dialog";
import { FormErrors } from "../../components/Field";
import { TechnicalDetails } from "../../components/TechnicalDetails";
import { useToast } from "../../components/Toast";
import { formatDateTime } from "../../lib/format";
import { APPLICATION_STATUS } from "../../lib/labels";
import { describeGenerationError } from "./generationErrors";

type DraftField = "generated_subject" | "generated_body" | "generated_whatsapp_message";
const DRAFT_FIELDS: DraftField[] = ["generated_subject", "generated_body", "generated_whatsapp_message"];
const LABELS: Record<DraftField, string> = {
  generated_subject: "Subject",
  generated_body: "Message body",
  generated_whatsapp_message: "WhatsApp message",
};
type Drafts = Record<DraftField, string>;

const fromApplication = (application: ApplicationDetail): Drafts => ({
  generated_subject: application.generated_subject ?? "",
  generated_body: application.generated_body ?? "",
  generated_whatsapp_message: application.generated_whatsapp_message ?? "",
});

function useElapsedSeconds(running: boolean): number {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    if (!running) return;
    const start = Date.now();
    const timer = window.setInterval(() => setElapsed(Math.floor((Date.now() - start) / 1000)), 1000);
    return () => {
      window.clearInterval(timer);
      setElapsed(0);
    };
  }, [running]);
  return elapsed;
}

const clock = (seconds: number) => `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;

function GenerateControl({ application, hasDrafts, dirty }: { application: ApplicationDetail; hasDrafts: boolean; dirty: boolean }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const { notify } = useToast();
  const [confirming, setConfirming] = useState(false);
  const [result, setResult] = useState<GenerationResult | null>(null);

  const mutation = useMutation({
    mutationFn: () => generateDrafts(api, application.id),
    onMutate: () => setResult(null),
    onSuccess: (generated) => {
      setResult(generated);
      queryClient.setQueryData(applicationKeys.detail(application.id), generated.application);
      notify("Draft generated. Review it before you send anything.", "success");
    },
    onSettled: () => {
      // Success or failure: status, last_error, events and counts may have changed.
      void queryClient.invalidateQueries({ queryKey: applicationKeys.all });
    },
  });
  const elapsed = useElapsedSeconds(mutation.isPending);

  const allowed = GENERATABLE_STATUSES.includes(application.status);
  const start = () => {
    if (mutation.isPending) return; // one request at a time
    if (hasDrafts || dirty) setConfirming(true);
    else mutation.mutate();
  };
  const problem = mutation.error ? describeGenerationError(mutation.error) : null;

  return (
    <div className="generate">
      <div className="generate__row">
        <Button variant={hasDrafts ? "secondary" : "primary"} onClick={start} loading={mutation.isPending} disabled={!allowed}>
          {hasDrafts ? "Generate a new draft" : "Generate draft"}
        </Button>
        {mutation.isPending ? (
          <span className="generate__progress" role="status">
            Writing the draft with the local model… {clock(elapsed)} (can take up to 3 minutes)
          </span>
        ) : (
          <span className="muted small">
            {allowed
              ? "Written by the local model from this job's description, the profile and the resume only."
              : `Drafts cannot be generated for a ${APPLICATION_STATUS[application.status].label.toLowerCase()} application.`}
          </span>
        )}
      </div>

      {problem && (
        <div className={`notice ${problem.uncertain ? "notice--warning" : "notice--error"} generate__error`} role="alert">
          <strong>{problem.title}.</strong> {problem.advice}
          {problem.message && <span className="generate__detail">{problem.message}</span>}
          {/* Recorded failures only (400/429 already say nothing changed or ran). */}
          {problem.confirmedUnchanged && problem.technical !== null && (
            <span className="generate__detail">The backend kept your existing drafts and the status unchanged.</span>
          )}
          {problem.uncertain && (
            <span>
              <Button
                size="sm"
                onClick={() => void queryClient.invalidateQueries({ queryKey: applicationKeys.all })}
              >
                Refresh application
              </Button>
            </span>
          )}
          <TechnicalDetails text={problem.technical} />
        </div>
      )}
      {result && (result.truncated.job_description || result.truncated.resume) && (
        <p className="notice notice--warning" role="note">
          The {[result.truncated.job_description && "job description", result.truncated.resume && "resume"].filter(Boolean).join(" and ")} was
          too long and was shortened for the model. Check the draft carefully.
        </p>
      )}
      {result && <p className="muted small">Generated by the local model.</p>}

      <ConfirmDialog
        open={confirming}
        title="Replace the current draft?"
        message={
          dirty
            ? "A new draft replaces the saved one, and your unsaved edits are lost if generation succeeds."
            : "A new draft replaces the saved one if generation succeeds. If it fails, the current draft is kept."
        }
        confirmLabel="Generate a new draft"
        onConfirm={() => {
          setConfirming(false);
          mutation.mutate();
        }}
        onCancel={() => setConfirming(false)}
      />
    </div>
  );
}

function DraftEditor({ application, onDirtyChange }: { application: ApplicationDetail; onDirtyChange: (dirty: boolean) => void }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const { notify } = useToast();
  const saved = fromApplication(application);
  const [values, setValues] = useState<Drafts>(saved);
  const editable = canEditDrafts(application);
  const ids = { generated_subject: useId(), generated_body: useId(), generated_whatsapp_message: useId() };

  const changed = DRAFT_FIELDS.filter((field) => values[field] !== saved[field]);
  const dirty = changed.length > 0;
  const hasDrafts = DRAFT_FIELDS.some((field) => saved[field]);
  useEffect(() => onDirtyChange(dirty), [dirty, onDirtyChange]);

  const save = useMutation({
    mutationFn: (changes: ApplicationUpdate) => updateApplication(api, application.id, changes),
    onSuccess: (updated) => {
      queryClient.setQueryData(applicationKeys.detail(application.id), updated);
      void queryClient.invalidateQueries({ queryKey: applicationKeys.lists() });
      setValues(fromApplication(updated)); // what the backend stored
      notify("Draft changes saved", "success");
    },
  });

  return (
    <>
      {!hasDrafts && !dirty && editable && <p className="muted">No draft yet. Generate one, or write it yourself below.</p>}
      {!editable && (
        <p className="notice" role="note">
          This application was sent, so its drafts are read-only.
        </p>
      )}

      <div className="drafts">
        {DRAFT_FIELDS.map((field) => (
          <div key={field} className="draft">
            <div className="draft__header">
              <label htmlFor={ids[field]} className="field__label">
                {LABELS[field]}
                {values[field] !== saved[field] && <span className="draft__unsaved"> · unsaved</span>}
              </label>
              <CopyButton text={values[field]} label={`Copy ${LABELS[field].toLowerCase()}`} />
            </div>
            {field === "generated_subject" ? (
              <input
                id={ids[field]}
                className="input"
                value={values[field]}
                maxLength={500}
                readOnly={!editable}
                disabled={save.isPending}
                onChange={(e) => setValues((v) => ({ ...v, [field]: e.target.value }))}
              />
            ) : (
              <textarea
                id={ids[field]}
                className="input input--multiline draft__text"
                rows={field === "generated_body" ? 12 : 5}
                value={values[field]}
                readOnly={!editable}
                disabled={save.isPending}
                onChange={(e) => setValues((v) => ({ ...v, [field]: e.target.value }))}
              />
            )}
          </div>
        ))}
      </div>

      <FormErrors error={save.error} />
      {editable && (
        <div className="form__actions">
          {dirty && <span className="muted small draft__status">Unsaved changes - copying uses the text shown.</span>}
          <Button onClick={() => setValues(saved)} disabled={!dirty || save.isPending}>
            Discard changes
          </Button>
          <Button
            variant="primary"
            loading={save.isPending}
            disabled={!dirty}
            onClick={() => {
              if (save.isPending) return;
              save.mutate(Object.fromEntries(changed.map((field) => [field, values[field]])) as ApplicationUpdate);
            }}
          >
            Save draft changes
          </Button>
        </div>
      )}
    </>
  );
}

/** What the last generation could not claim, and terms to double-check (never blocking). */
function GenerationNotesView({ application }: { application: ApplicationDetail }) {
  const notes = application.generation_notes ?? {};
  const gaps = notes.gaps ?? [];
  const unsupported = notes.unsupported_terms ?? [];
  if (!application.generated_at || (!gaps.length && !unsupported.length && !notes.resume_name)) return null;
  return (
    <div className="generation-notes">
      {notes.resume_name && (
        <p className="muted small">
          Written from this job's description, your profile and the resume “{notes.resume_name}”.
        </p>
      )}
      {unsupported.length > 0 && (
        <p className="notice notice--warning" role="note">
          Check before sending: the draft mentions <strong>{unsupported.join(", ")}</strong>, which your profile and resume do
          not mention. Remove it unless it is true.
        </p>
      )}
      {gaps.length > 0 && (
        <p className="muted small">
          Not claimed (not in your profile or resume): {gaps.join(", ")}.
        </p>
      )}
    </div>
  );
}

export function DraftsPanel({ application }: { application: ApplicationDetail }) {
  const [dirty, setDirty] = useState(false);
  const hasDrafts = DRAFT_FIELDS.some((field) => fromApplication(application)[field]);
  return (
    <section className="panel" aria-labelledby="drafts-title">
      <div>
        <h2 id="drafts-title" className="panel__title">
          Draft
        </h2>
        <p className="panel__subtitle">
          Review every line and edit anything you want. Approve and send it below - nothing is sent without your
          confirmation.
          {application.generated_at && <> Last generated {formatDateTime(application.generated_at)}.</>}
        </p>
      </div>
      <GenerateControl application={application} hasDrafts={hasDrafts} dirty={dirty} />
      <GenerationNotesView application={application} />
      {/* A successful generation (new generated_at) resets the editor to the new draft. */}
      <DraftEditor
        key={`${application.id}:${application.generated_at ?? "none"}`}
        application={application}
        onDirtyChange={setDirty}
      />
    </section>
  );
}
