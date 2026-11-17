/**
 * One collector run (GET collector/runs/{id}/ - read-only). Shows what the backend
 * stores: status, trigger, profile, keywords, limit, times, counts, error and the ids
 * of jobs created or matched. Per-post details are only returned when a run is
 * started from this app, so they are not available here.
 */

import { useQueries, useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { collectorKeys, getRun } from "../../api/collector";
import { isApiError } from "../../api/errors";
import { getJob, jobKeys } from "../../api/jobs";
import { useApi } from "../../auth/TokenProvider";
import { StatusBadge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { PageHeader } from "../../components/PageHeader";
import { EmptyState, ErrorState, Skeleton } from "../../components/States";
import { formatDateTime } from "../../lib/format";
import { useActiveProfile } from "../../profile/ActiveProfileProvider";
import { RequireProfile } from "../../profile/ProfileControls";
import { formatDuration } from "./CollectorPage";
import { RunCounts } from "./RunParts";
import { shortError, STATUS_EXPLANATION } from "./runText";

const MAX_JOB_TITLES = 50;

function RunJobs({ jobIds }: { jobIds: number[] }) {
  const api = useApi();
  const shown = jobIds.slice(0, MAX_JOB_TITLES);
  const jobs = useQueries({
    queries: shown.map((id) => ({ queryKey: jobKeys.detail(id), queryFn: ({ signal }: { signal: AbortSignal }) => getJob(api, id, signal) })),
  });
  if (jobIds.length === 0) return <p className="muted">No jobs were created or matched by this run.</p>;
  return (
    <ul className="run-jobs">
      {shown.map((id, index) => {
        const job = jobs[index];
        return (
          <li key={id}>
            <Link to={`/jobs/${id}`}>{job?.data?.title ?? `Job #${id}`}</Link>
            {job?.data && (
              <>
                {job.data.company && <span className="muted"> · {job.data.company.name}</span>} <StatusBadge kind="screening" value={job.data.screening_outcome} />
              </>
            )}
            {isApiError(job?.error) && job.error.kind === "not_found" && <span className="muted"> (no longer exists)</span>}
          </li>
        );
      })}
      {jobIds.length > shown.length && <li className="muted">and {jobIds.length - shown.length} more</li>}
    </ul>
  );
}

function RunContent() {
  const api = useApi();
  const { profiles } = useActiveProfile();
  const { id: rawId } = useParams();
  const id = /^[1-9]\d*$/.test(rawId ?? "") ? Number(rawId) : null;
  const run = useQuery({
    queryKey: collectorKeys.run(id ?? 0),
    queryFn: ({ signal }) => getRun(api, id!, signal),
    enabled: id !== null,
  });
  const back = (
    <Link className="back-link" to="/collector">
      ← Collector
    </Link>
  );

  if (id === null || (isApiError(run.error) && run.error.kind === "not_found")) {
    return (
      <>
        {back}
        <PageHeader title="Run not found" />
        <EmptyState title="There is no collector run with this id" />
      </>
    );
  }
  if (run.error && !run.data) {
    return (
      <>
        {back}
        <PageHeader title="Collector run" />
        <ErrorState title="Could not load this run" error={run.error} onRetry={() => void run.refetch()} />
      </>
    );
  }
  if (!run.data) {
    return (
      <>
        {back}
        <Skeleton lines={6} label="Loading run" />
      </>
    );
  }
  const data = run.data;
  const profile = profiles.find((p) => p.id === data.profile);
  return (
    <>
      {back}
      <PageHeader
        title={`Collector run #${data.id}`}
        description={formatDateTime(data.started_at)}
        actions={
          <Button size="sm" onClick={() => void run.refetch()} loading={run.isFetching}>
            Refresh
          </Button>
        }
      />
      <section className="panel" aria-labelledby="run-status-title">
        <div className="panel__header">
          <h2 id="run-status-title" className="panel__title">
            Result
          </h2>
          <StatusBadge kind="collector" value={data.status} />
        </div>
        <p>{STATUS_EXPLANATION[data.status]}</p>
        {data.error && (
          <p className="notice notice--warning">
            <strong>Reported problem:</strong> {shortError(data.error)}
          </p>
        )}
        <RunCounts counts={data.counts} />
      </section>
      <section className="panel" aria-labelledby="run-settings-title">
        <h2 id="run-settings-title" className="panel__title">
          Run settings
        </h2>
        <dl className="details">
          <div className="details__item">
            <dt>Keywords</dt>
            <dd>{data.keywords.length ? data.keywords.join(", ") : <span className="muted">None recorded</span>}</dd>
          </div>
          <div className="details__item">
            <dt>Maximum posts</dt>
            <dd>{data.limit ?? "—"}</dd>
          </div>
          <div className="details__item">
            <dt>Profile</dt>
            <dd>{data.profile === null ? <span className="muted">None (deleted or not set)</span> : (profile?.name ?? `Profile #${data.profile}`)}</dd>
          </div>
          <div className="details__item">
            <dt>Started from</dt>
            <dd>
              {data.trigger === "api" ? "This app" : "Command line"} · source {data.source}
            </dd>
          </div>
          <div className="details__item">
            <dt>Started</dt>
            <dd>{formatDateTime(data.started_at)}</dd>
          </div>
          <div className="details__item">
            <dt>Finished</dt>
            <dd>
              {data.finished_at ? formatDateTime(data.finished_at) : "Not finished"}
              {data.duration_seconds !== null && <span className="muted"> ({formatDuration(data.duration_seconds)})</span>}
            </dd>
          </div>
        </dl>
      </section>
      <section className="panel" aria-labelledby="run-jobs-title">
        <h2 id="run-jobs-title" className="panel__title">
          Jobs from this run
        </h2>
        <RunJobs jobIds={data.job_ids} />
      </section>
    </>
  );
}

export function RunDetailPage() {
  return (
    <RequireProfile>
      <RunContent />
    </RequireProfile>
  );
}
