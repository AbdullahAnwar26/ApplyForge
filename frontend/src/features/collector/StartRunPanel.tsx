/**
 * Starting a collector run - the only place in the app that can make the backend
 * open LinkedIn. Safeguards:
 *   - never automatic: only after the form is submitted AND the confirmation dialog's
 *     acknowledgement is ticked AND "Start collection run" is clicked;
 *   - the confirmation shows the exact keywords, limit and profile that will be sent;
 *   - one request at a time (pending state + a ref guard), never retried (mutations do
 *     not retry; a failed or interrupted POST may already have reached the backend);
 *   - history is refreshed only when a run has finished (or on the user's request).
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { collectorKeys, DEFAULT_RUN_LIMIT, MAX_KEYWORDS, MAX_RUN_LIMIT, parseKeywords, startRun, validateRunInput } from "../../api/collector";
import { isApiError } from "../../api/errors";
import { jobKeys } from "../../api/jobs";
import type { CollectorRun, CollectorRunInput, CollectorRunResult, Profile } from "../../api/types";
import { useApi } from "../../auth/TokenProvider";
import { StatusBadge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { splitList } from "../../lib/format";
import { RunCounts, RunOutcomes } from "./RunParts";
import { describeStartError, shortError, STATUS_EXPLANATION } from "./runText";

type Finished = { kind: "finished"; result: CollectorRunResult } | { kind: "error"; error: unknown };

function isCompletedRun(error: unknown): error is { body: CollectorRunResult } {
  if (!isApiError(error) || error.status !== 502) return false;
  const body = error.body as Partial<CollectorRunResult> | undefined;
  return Boolean(body && typeof body === "object" && body.run && typeof body.run.id === "number");
}

function useElapsed(running: boolean): number {
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

const clock = (s: number) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

function ConfirmRunDialog({
  open,
  input,
  profile,
  onCancel,
  onConfirm,
}: {
  open: boolean;
  input: CollectorRunInput;
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
  return (
    <Dialog
      open={open}
      title="Start a LinkedIn collection run?"
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
            Start collection run
          </Button>
        </>
      }
    >
      <dl className="run-summary">
        <dt>Keywords</dt>
        <dd>
          <ul className="chips chips--static">
            {(input.keywords ?? []).map((keyword) => (
              <li key={keyword} className="chip">
                {keyword}
              </li>
            ))}
          </ul>
        </dd>
        <dt>Maximum posts</dt>
        <dd>{input.limit}</dd>
        <dt>Profile</dt>
        <dd>{profile.name} (new jobs are screened against it)</dd>
      </dl>
      <ul className="plain-list run-warnings">
        <li>
          The backend opens LinkedIn in a browser <strong>on the backend machine</strong>, using the LinkedIn session already
          saved there. A browser window may appear on that machine.
        </li>
        <li>It reads search results and creates or updates job records in ApplyForge's local database.</li>
        <li>It does not apply for any job and does not contact anyone.</li>
        <li>This page waits until the run finishes, which can take several minutes.</li>
      </ul>
      <label className="checkbox" htmlFor={checkId}>
        <input id={checkId} type="checkbox" checked={acknowledged} onChange={(e) => setAcknowledged(e.target.checked)} />
        <span>I understand this run accesses LinkedIn through the backend's saved session.</span>
      </label>
    </Dialog>
  );
}

function ResultView({ outcome, onRefresh }: { outcome: Finished; onRefresh: () => void }) {
  if (outcome.kind === "finished") {
    const { run, outcomes, detail } = outcome.result;
    return (
      <div className={`run-result run-result--${run.status.toLowerCase()}`} role="status" aria-live="polite">
        <div className="run-result__head">
          <StatusBadge kind="collector" value={run.status} />
          <strong>Run #{run.id} finished.</strong>
          <Link to={`/collector/runs/${run.id}`}>View run details</Link>
        </div>
        <p className="muted">{STATUS_EXPLANATION[run.status]}</p>
        {(detail || run.error) && <p className="notice notice--warning">{shortError(detail || run.error)}</p>}
        <RunCounts counts={run.counts} />
        <RunOutcomes outcomes={outcomes} />
      </div>
    );
  }
  const problem = describeStartError(outcome.error);
  return (
    <div className="notice notice--error run-result" role="alert">
      <strong>{problem.title}.</strong> <span>{problem.message}</span>
      {problem.runId !== null && (
        <span>
          {" "}
          <Link to={`/collector/runs/${problem.runId}`}>Open run #{problem.runId}</Link>
        </span>
      )}
      {problem.uncertain && (
        <div className="run-result__actions">
          <Button size="sm" onClick={onRefresh}>
            Refresh history
          </Button>
        </div>
      )}
    </div>
  );
}

export function StartRunPanel({ profile, runningRun, onRefresh }: { profile: Profile; runningRun: CollectorRun | null; onRefresh: () => void }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const [keywordsText, setKeywordsText] = useState(() => splitList(profile.target_roles).join(", "));
  const [limit, setLimit] = useState(String(DEFAULT_RUN_LIMIT));
  const [errors, setErrors] = useState<{ keywords?: string; limit?: string }>({});
  const [pending, setPending] = useState<CollectorRunInput | null>(null); // awaiting confirmation
  const [outcome, setOutcome] = useState<Finished | null>(null);
  const inFlight = useRef(false);

  const finished = () => {
    void queryClient.invalidateQueries({ queryKey: collectorKeys.all });
    void queryClient.invalidateQueries({ queryKey: jobKeys.all });
  };

  const mutation = useMutation({
    mutationFn: (input: CollectorRunInput) => startRun(api, input),
    onSuccess: (result) => {
      setOutcome({ kind: "finished", result });
      finished();
    },
    onError: (error) => {
      if (isCompletedRun(error)) {
        setOutcome({ kind: "finished", result: error.body });
        finished();
        return;
      }
      setOutcome({ kind: "error", error });
      // A run was recorded (400/500 with a run id): history changed. Otherwise
      // (network, 409, 429) nothing is refreshed automatically.
      if (isApiError(error) && typeof error.extra.run === "number" && error.kind !== "conflict") finished();
    },
    onSettled: () => {
      inFlight.current = false;
    },
  });
  const elapsed = useElapsed(mutation.isPending);
  const keywords = parseKeywords(keywordsText);

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (mutation.isPending || inFlight.current) return;
    const problems = validateRunInput(keywords, limit);
    setErrors(problems);
    if (Object.keys(problems).length) return;
    setPending({ keywords, limit: Number(limit), profile: profile.id });
  }

  function confirmed() {
    const input = pending;
    setPending(null);
    if (!input || inFlight.current || mutation.isPending) return; // one request, ever, per confirmation
    inFlight.current = true;
    setOutcome(null);
    mutation.mutate(input);
  }

  const busy = mutation.isPending;
  return (
    <section className="panel" aria-labelledby="start-run-title">
      <div>
        <h2 id="start-run-title" className="panel__title">
          Start a collection run
        </h2>
        <p className="panel__subtitle">
          Collects recruiter hiring posts from LinkedIn into ApplyForge as jobs. This is a local collection run, not a job
          application - nothing is applied for or sent.
        </p>
      </div>

      {runningRun && !busy && (
        <p className="notice notice--warning" role="note">
          Run <Link to={`/collector/runs/${runningRun.id}`}>#{runningRun.id}</Link> is shown as running. The backend refuses a
          new run while another is active.
        </p>
      )}

      <form className="form" onSubmit={onSubmit} noValidate>
        <fieldset className="form__fields" disabled={busy}>
          <div className="form__grid form__grid--wide-first">
            <Field
              label="Search keywords"
              required
              error={errors.keywords}
              hint={`Comma-separated, up to ${MAX_KEYWORDS}. Defaults to ${profile.name}'s target roles.`}
            >
              {(props) => (
                <input
                  {...props}
                  className="input"
                  value={keywordsText}
                  onChange={(e) => setKeywordsText(e.target.value)}
                />
              )}
            </Field>
            <Field label="Maximum posts" required error={errors.limit} hint={`1 to ${MAX_RUN_LIMIT}.`}>
              {(props) => (
                <input {...props} className="input" inputMode="numeric" value={limit} onChange={(e) => setLimit(e.target.value)} />
              )}
            </Field>
          </div>
        </fieldset>
        <div className="form__actions">
          <Button type="submit" variant="primary" loading={busy} disabled={busy}>
            Review and start…
          </Button>
        </div>
      </form>

      {busy && (
        <div className="run-progress" role="status" aria-live="polite">
          <span className="btn__spinner" aria-hidden="true" />
          <span>
            Collecting on the backend machine… {clock(elapsed)}. Stay on this page to see the result; the run continues on the
            backend either way.
          </span>
        </div>
      )}
      {outcome && !busy && <ResultView outcome={outcome} onRefresh={onRefresh} />}

      <ConfirmRunDialog
        open={pending !== null}
        input={pending ?? {}}
        profile={profile}
        onCancel={() => setPending(null)}
        onConfirm={confirmed}
      />
    </section>
  );
}
