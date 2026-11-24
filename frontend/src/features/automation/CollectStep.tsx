/**
 * Step 1 - Search and collect. The user enters a search, a date range and how many
 * jobs they want; after a confirmation the backend collects in the background and this
 * panel shows live progress (polled), with Cancel and - for runs that stopped early -
 * Continue. Nothing here applies for a job or contacts anyone.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import {
  cancelRun,
  collectionInput,
  collectorKeys,
  continueRun,
  DEFAULT_TARGET,
  getRun,
  isActive,
  MAX_TARGET,
  RUN_POLL_MS,
  startCollection,
  validateCollection,
  type CollectionFormErrors,
  type CollectionFormValues,
} from "../../api/collector";
import { isApiError } from "../../api/errors";
import { companyKeys, jobKeys } from "../../api/jobs";
import type { CollectorRun, DateRange, Profile } from "../../api/types";
import { useApi } from "../../auth/AuthProvider";
import { StatusBadge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { TechnicalDetails } from "../../components/TechnicalDetails";
import { useToast } from "../../components/Toast";
import { isExceptionText } from "../../lib/diagnostics";
import { formatDateTime } from "../../lib/format";
import { DATE_RANGE_LABEL, STOP_REASON_TEXT } from "../../lib/labels";
import { shortError } from "../collector/runText";

const DATE_CHOICES: DateRange[] = ["past-24h", "past-week", "past-month", "any", "custom"];

export function localToday(now = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

function firstRole(profile: Profile): string {
  return (profile.target_roles || "").split(",").map((r) => r.trim()).find(Boolean) ?? "";
}

function describeStartProblem(error: unknown): string {
  if (!isApiError(error)) return "The collection could not be started.";
  if (error.kind === "conflict") return error.message || "A collection is already running. Wait for it to finish.";
  if (error.kind === "throttled") return "Too many collections were started recently. Wait a minute and try again.";
  if (error.kind === "validation") {
    return Object.values(error.fieldErrors).flat()[0] ?? error.message;
  }
  if (error.kind === "network") {
    return "No answer from the server. The collection may still have started; check the progress below before trying again.";
  }
  return error.message;
}

function ConfirmCollectDialog({
  values,
  profile,
  onCancel,
  onConfirm,
}: {
  values: CollectionFormValues | null;
  profile: Profile;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const [acknowledged, setAcknowledged] = useState(false);
  const checkId = useId();
  const close = () => {
    setAcknowledged(false);
    onCancel();
  };
  const range =
    values?.dateRange === "custom"
      ? `${values.dateFrom}${values.dateTo ? ` to ${values.dateTo}` : " to today"}`
      : values
        ? DATE_RANGE_LABEL[values.dateRange]
        : "";
  return (
    <Dialog
      open={values !== null}
      title="Start collecting from LinkedIn?"
      onClose={close}
      footer={
        <>
          <Button onClick={close} data-autofocus>
            Cancel
          </Button>
          <Button
            variant="primary"
            disabled={!acknowledged}
            onClick={() => {
              setAcknowledged(false);
              onConfirm();
            }}
          >
            Start collecting
          </Button>
        </>
      }
    >
      {values && (
        <dl className="run-summary">
          <dt>Search</dt>
          <dd>{values.query.trim()}</dd>
          <dt>Source</dt>
          <dd>LinkedIn · Posts</dd>
          <dt>Date posted</dt>
          <dd>{range}</dd>
          <dt>New jobs wanted</dt>
          <dd>{values.target}</dd>
          <dt>Profile</dt>
          <dd>{profile.name} (jobs are screened against it)</dd>
        </dl>
      )}
      <ul className="plain-list run-warnings">
        <li>
          LinkedIn is opened in a browser <strong>on the server machine</strong> with the LinkedIn session saved there. It reads
          search results like a person would: opening each post's “… more”, scrolling and loading more results.
        </li>
        <li>It stops at your number of new jobs, when there are no more results, or when you cancel. Duplicates are recognised.</li>
        <li>If LinkedIn asks for a login or a security check, the collection stops and tells you. Nothing is bypassed.</li>
        <li>Nothing is applied for and nobody is contacted.</li>
      </ul>
      <label className="checkbox" htmlFor={checkId}>
        <input id={checkId} type="checkbox" checked={acknowledged} onChange={(e) => setAcknowledged(e.target.checked)} />
        <span>I understand this uses the LinkedIn session saved on the server.</span>
      </label>
    </Dialog>
  );
}

function useElapsed(run: CollectorRun): string {
  const [now, setNow] = useState(() => Date.now());
  const running = isActive(run);
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);
  const end = running ? now : run.finished_at ? Date.parse(run.finished_at) : now;
  const seconds = Math.max(0, Math.floor((end - Date.parse(run.started_at)) / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

const PROGRESS_COUNTS: { key: keyof CollectorRun["counts"]; label: string; help: string }[] = [
  { key: "ready", label: "Ready to apply", help: "New jobs with the full description and the recruiter's email or phone, matching your skills." },
  { key: "created", label: "New jobs", help: "Job posts saved as new jobs (including ones without a contact)." },
  { key: "existing", label: "Duplicates", help: "Jobs you already had (same post or the same description)." },
  { key: "skipped", label: "Not job posts", help: "Posts that are not job openings (e.g. people looking for work)." },
  { key: "out_of_range", label: "Outside dates", help: "Posts outside the date range you chose." },
  { key: "invalid", label: "Unreadable", help: "Job posts that could not be read into a job." },
  { key: "failed", label: "Failed", help: "Posts that could not be saved because of an error." },
];

export function RunProgress({ run, onReview }: { run: CollectorRun; onReview?: (runId: number) => void }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const toast = useToast();
  const elapsed = useElapsed(run);
  const running = isActive(run);
  // The target counts jobs ready to apply (older runs: every new job).
  const created = run.counts.ready ?? run.counts.created ?? 0;
  const target = run.limit ?? 0;
  const percent = target ? Math.min(100, Math.round((created / target) * 100)) : 0;

  const cancel = useMutation({
    mutationFn: () => cancelRun(api, run.id),
    onSuccess: (updated) => queryClient.setQueryData(collectorKeys.run(run.id), updated),
    onError: (error) => toast.notify(describeStartProblem(error), "danger"),
  });
  const resume = useMutation({
    mutationFn: () => continueRun(api, run.id),
    onSuccess: ({ run: next }) => {
      queryClient.setQueryData(collectorKeys.run(next.id), next);
      void queryClient.invalidateQueries({ queryKey: collectorKeys.latest });
      toast.notify(`Continuing: collecting ${next.limit} more job(s).`, "success");
    },
    onError: (error) => toast.notify(describeStartProblem(error), "danger"),
  });
  const canContinue = !running && (run.remaining ?? 0) > 0 && Boolean(run.search_query) && run.stop_reason !== "exhausted";

  return (
    <div className={`collect-progress collect-progress--${run.status.toLowerCase()}`} aria-live="polite">
      <div className="collect-progress__head">
        <StatusBadge kind="collector" value={run.status} />
        <strong className="collect-progress__query">“{run.search_query || run.keywords.join(", ")}”</strong>
        <span className="muted small">
          {DATE_RANGE_LABEL[run.date_range] ?? run.date_range} · started {formatDateTime(run.started_at)} · {elapsed}
        </span>
      </div>
      <div
        className="progress"
        role="progressbar"
        aria-label="Jobs ready to apply"
        aria-valuemin={0}
        aria-valuemax={target}
        aria-valuenow={created}
      >
        <div className="progress__bar" style={{ width: `${percent}%` }} />
      </div>
      <p className="collect-progress__summary">
        <strong>
          {created} of {target} jobs ready to apply
        </strong>
        {run.remaining ? <span className="muted"> · {run.remaining} remaining</span> : null}
        <span className="muted"> · {run.counts.found ?? 0} posts checked</span>
      </p>
      <p className="collect-progress__stage" role="status">
        {running && <span className="btn__spinner" aria-hidden="true" />} {run.stage || (running ? "Starting…" : "")}
      </p>
      <dl className="run-counts run-counts--compact">
        {PROGRESS_COUNTS.map(({ key, label, help }) => (
          <div key={key} className={`run-counts__item run-counts__item--${key}`} title={help}>
            <dt>{label}</dt>
            <dd>{run.counts[key] ?? 0}</dd>
          </div>
        ))}
      </dl>
      {!running && run.stop_reason && <p className="muted">{STOP_REASON_TEXT[run.stop_reason]}</p>}
      {!running && run.stop_reason === "exhausted" && created < target && (run.counts.out_of_range ?? 0) > 0 && (
        <p className="notice notice--info" role="note">
          Only {created} of {target}: the other posts found were older than “{DATE_RANGE_LABEL[run.date_range]}”. Choose a longer date range
          to get more.
        </p>
      )}
      {!running && run.error && (
        <div className="notice notice--warning">
          {shortError(run.error)}
          {isExceptionText(run.error) && <TechnicalDetails text={run.error} />}
        </div>
      )}
      <div className="collect-progress__actions">
        {running ? (
          <Button onClick={() => cancel.mutate()} loading={cancel.isPending} disabled={run.cancel_requested}>
            {run.cancel_requested ? "Cancelling…" : "Cancel collection"}
          </Button>
        ) : (
          <>
            {created > 0 && onReview && (
              <Button variant="primary" onClick={() => onReview(run.id)}>
                Pick from {created} job{created === 1 ? "" : "s"} →
              </Button>
            )}
            {canContinue && (
              <Button onClick={() => resume.mutate()} loading={resume.isPending}>
                Continue collecting ({run.remaining} more)
              </Button>
            )}
          </>
        )}
        <Link className="small" to={`/collector/runs/${run.id}`}>
          Every post in this run
        </Link>
      </div>
    </div>
  );
}

/** Live view of one run: polls while it is running, refreshes job lists when it ends. */
export function LiveRun({ runId, onReview }: { runId: number; onReview?: (runId: number) => void }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const run = useQuery({
    queryKey: collectorKeys.run(runId),
    queryFn: ({ signal }) => getRun(api, runId, signal),
    refetchInterval: (query) => (isActive(query.state.data) ? RUN_POLL_MS : false),
  });
  const status = run.data?.status;
  const created = run.data?.counts.created ?? 0;
  useEffect(() => {
    // New jobs appear in the review list while the collection is still running.
    void queryClient.invalidateQueries({ queryKey: jobKeys.lists() });
  }, [created, queryClient]);
  useEffect(() => {
    if (status && status !== "RUNNING") {
      void queryClient.invalidateQueries({ queryKey: jobKeys.all });
      void queryClient.invalidateQueries({ queryKey: companyKeys.all });
      void queryClient.invalidateQueries({ queryKey: ["collector", "runs"] });
      void queryClient.invalidateQueries({ queryKey: collectorKeys.latest }); // unlocks the form
    }
  }, [status, queryClient]);
  if (!run.data) return <p className="muted">Loading collection #{runId}…</p>;
  return <RunProgress run={run.data} onReview={onReview} />;
}

export function CollectForm({
  profile,
  busy,
  onStarted,
}: {
  profile: Profile;
  busy: boolean;
  onStarted: (run: CollectorRun) => void;
}) {
  const api = useApi();
  const queryClient = useQueryClient();
  const [values, setValues] = useState<CollectionFormValues>(() => ({
    query: firstRole(profile),
    target: String(DEFAULT_TARGET),
    dateRange: "past-week",
    dateFrom: "",
    dateTo: "",
  }));
  const [errors, setErrors] = useState<CollectionFormErrors>({});
  const [pending, setPending] = useState<CollectionFormValues | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const today = localToday();
  const set = (changes: Partial<CollectionFormValues>) => setValues((current) => ({ ...current, ...changes }));

  const start = useMutation({
    mutationFn: (form: CollectionFormValues) => startCollection(api, collectionInput(form, profile.id)),
    onSuccess: ({ run }) => {
      setProblem(null);
      queryClient.setQueryData(collectorKeys.run(run.id), run);
      void queryClient.invalidateQueries({ queryKey: collectorKeys.latest });
      onStarted(run);
    },
    onError: (error) => {
      setProblem(describeStartProblem(error));
      if (isApiError(error) && error.kind === "conflict") void queryClient.invalidateQueries({ queryKey: collectorKeys.latest });
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || start.isPending) return;
    const found = validateCollection(values, today);
    setErrors(found);
    if (Object.keys(found).length === 0) setPending(values);
  }

  return (
    <>
      <form className="form collect-form" onSubmit={submit} noValidate>
        <fieldset className="form__fields" disabled={busy || start.isPending}>
          <div className="collect-form__grid">
            <Field label="Search for" required error={errors.query} hint="The job you want, as you would type it in LinkedIn.">
              {(props) => (
                <input
                  {...props}
                  className="input"
                  value={values.query}
                  placeholder="Python Backend Developer"
                  onChange={(e) => set({ query: e.target.value })}
                />
              )}
            </Field>
            <Field label="Date posted">
              {(props) => (
                <select {...props} className="input" value={values.dateRange} onChange={(e) => set({ dateRange: e.target.value as DateRange })}>
                  {DATE_CHOICES.map((choice) => (
                    <option key={choice} value={choice}>
                      {DATE_RANGE_LABEL[choice]}
                    </option>
                  ))}
                </select>
              )}
            </Field>
            <Field
              label="Number of jobs"
              required
              error={errors.target}
              hint={`New jobs to collect (up to ${MAX_TARGET}). Duplicates and non-job posts don't count.`}
            >
              {(props) => (
                <input
                  {...props}
                  className="input"
                  type="number"
                  min={1}
                  max={MAX_TARGET}
                  inputMode="numeric"
                  value={values.target}
                  onChange={(e) => set({ target: e.target.value })}
                />
              )}
            </Field>
          </div>
          {values.dateRange === "custom" && (
            <div className="collect-form__grid collect-form__grid--dates">
              <Field label="From" required error={errors.dateFrom}>
                {(props) => (
                  <input {...props} className="input" type="date" max={today} value={values.dateFrom} onChange={(e) => set({ dateFrom: e.target.value })} />
                )}
              </Field>
              <Field label="To" error={errors.dateTo} hint="Leave empty for today.">
                {(props) => (
                  <input {...props} className="input" type="date" max={today} value={values.dateTo} onChange={(e) => set({ dateTo: e.target.value })} />
                )}
              </Field>
            </div>
          )}
          {values.dateRange === "custom" && (
            <p className="muted small">
              LinkedIn only filters by 24 hours, week or month. ApplyForge uses the closest LinkedIn filter and then keeps only
              posts dated inside your range.
            </p>
          )}
          <p className="collect-form__source">
            <span className="chip chip--static">LinkedIn</span>
            <span className="chip chip--static">Posts</span>
            <span className="muted small">Sorted by newest first</span>
          </p>
        </fieldset>
        <div className="form__actions">
          <Button type="submit" variant="primary" disabled={busy} loading={start.isPending}>
            Start collecting
          </Button>
          {busy && <span className="muted small">A collection is running. Wait for it to finish or cancel it.</span>}
        </div>
        {problem && (
          <p className="notice notice--error" role="alert">
            {problem}
          </p>
        )}
      </form>
      <ConfirmCollectDialog
        values={pending}
        profile={profile}
        onCancel={() => setPending(null)}
        onConfirm={() => {
          const form = pending;
          setPending(null);
          if (form && !start.isPending) start.mutate(form);
        }}
      />
    </>
  );
}
