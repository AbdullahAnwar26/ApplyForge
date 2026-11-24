/**
 * One collection run: live progress while it runs (with Cancel), how it ended (with
 * Continue for runs that stopped early), its settings, and every post it examined
 * with what happened to it (new job, duplicate, not a job post, outside the dates...).
 */

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { collectorKeys, getRun, ITEMS_PAGE_SIZE, listRunItems } from "../../api/collector";
import { isApiError } from "../../api/errors";
import type { CollectedPostStatus, CollectorRun } from "../../api/types";
import { useApi } from "../../auth/AuthProvider";
import { Badge } from "../../components/Badge";
import { PageHeader } from "../../components/PageHeader";
import { Pagination } from "../../components/Pagination";
import { EmptyState, ErrorState, Skeleton } from "../../components/States";
import { formatDate, formatDateTime } from "../../lib/format";
import { DATE_RANGE_LABEL, type Tone } from "../../lib/labels";
import { useActiveProfile } from "../../profile/ActiveProfileProvider";
import { RequireProfile } from "../../profile/ProfileControls";
import { LiveRun } from "../automation/CollectStep";
import { formatDuration } from "./CollectorPage";
import { shortError } from "./runText";

export const ITEM_STATUS: Record<CollectedPostStatus, { label: string; tone: Tone }> = {
  created: { label: "New job", tone: "success" },
  existing: { label: "Duplicate", tone: "info" },
  skipped: { label: "Not a job post", tone: "neutral" },
  out_of_range: { label: "Outside dates", tone: "neutral" },
  invalid: { label: "Unreadable", tone: "warning" },
  failed: { label: "Failed", tone: "danger" },
};

function RunItems({ run }: { run: CollectorRun }) {
  const api = useApi();
  const [status, setStatus] = useState<CollectedPostStatus | "">("");
  const [page, setPage] = useState(1);
  const items = useQuery({
    queryKey: [...collectorKeys.items(run.id, status, page), run.counts.found ?? 0],
    queryFn: ({ signal }) => listRunItems(api, run.id, status, page, signal),
    placeholderData: keepPreviousData,
  });
  return (
    <section className="panel" aria-labelledby="run-items-title">
      <div className="panel__header">
        <h2 id="run-items-title" className="panel__title">
          Posts examined
        </h2>
        <label className="filter">
          <span className="filter__label">Show</span>
          <select
            className="input input--compact"
            value={status}
            onChange={(e) => {
              setPage(1);
              setStatus(e.target.value as CollectedPostStatus | "");
            }}
          >
            <option value="">All</option>
            {(Object.keys(ITEM_STATUS) as CollectedPostStatus[]).map((value) => (
              <option key={value} value={value}>
                {ITEM_STATUS[value].label}
              </option>
            ))}
          </select>
        </label>
      </div>
      {items.error && !items.data ? (
        <ErrorState error={items.error} title="Could not load the posts" onRetry={() => void items.refetch()} />
      ) : !items.data ? (
        <Skeleton lines={4} label="Loading posts" />
      ) : items.data.items.length === 0 ? (
        <EmptyState title={status ? "No posts with this result" : "No posts examined (yet)"} />
      ) : (
        <>
          <ol className="run-outcomes__list">
            {items.data.items.map((item) => {
              const tone = ITEM_STATUS[item.status] ?? { label: item.status, tone: "neutral" as Tone };
              return (
                <li key={item.id} className="run-outcome">
                  <Badge tone={tone.tone}>{tone.label}</Badge>
                  {item.job ? <Link to={`/jobs/${item.job}`}>{item.title || `Job #${item.job}`}</Link> : <span>{item.title || `Post ${item.position + 1}`}</span>}
                  {item.truncated && <Badge tone="warning">cut off</Badge>}
                  {item.posted_at && <span className="muted small">posted {formatDate(item.posted_at)}</span>}
                  {item.post_url && (
                    <a className="small" href={item.post_url} target="_blank" rel="noopener noreferrer">
                      post ↗
                    </a>
                  )}
                  {item.reason && <span className="muted small run-outcome__reason">{shortError(item.reason)}</span>}
                </li>
              );
            })}
          </ol>
          <Pagination page={page} pageSize={ITEMS_PAGE_SIZE} count={items.data.count} onPageChange={setPage} disabled={items.isFetching} />
        </>
      )}
    </section>
  );
}

function RunContent() {
  const api = useApi();
  const navigate = useNavigate();
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
      ← Collection history
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
      <PageHeader title={`Collection #${data.id}`} description={formatDateTime(data.started_at)} />
      <section className="panel" aria-labelledby="run-status-title">
        <h2 id="run-status-title" className="panel__title">
          Progress
        </h2>
        <LiveRun runId={data.id} onReview={(runId) => navigate(`/automation?step=pick&run=${runId}`)} />
      </section>
      <section className="panel" aria-labelledby="run-settings-title">
        <h2 id="run-settings-title" className="panel__title">
          Settings
        </h2>
        <dl className="details">
          <div className="details__item">
            <dt>Search</dt>
            <dd>{data.search_query || data.keywords.join(", ") || <span className="muted">None recorded</span>}</dd>
          </div>
          <div className="details__item">
            <dt>Date posted</dt>
            <dd>
              {DATE_RANGE_LABEL[data.date_range] ?? data.date_range}
              {data.date_range === "custom" && data.date_from && ` (${data.date_from} to ${data.date_to ?? "today"})`}
            </dd>
          </div>
          <div className="details__item">
            <dt>New jobs wanted</dt>
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
            <dt>Finished</dt>
            <dd>
              {data.finished_at ? formatDateTime(data.finished_at) : "Not finished"}
              {data.duration_seconds !== null && <span className="muted"> ({formatDuration(data.duration_seconds)})</span>}
            </dd>
          </div>
        </dl>
      </section>
      <RunItems run={data} />
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
