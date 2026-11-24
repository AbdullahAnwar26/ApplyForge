/**
 * Collection history (GET collector/runs/). Collections are started from Job
 * Automation; this page only reads. Refreshed by the "Refresh" button or after a run
 * finishes - no polling here.
 */

import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { collectorKeys, listRuns, RUNS_PAGE_SIZE, type RunFilters } from "../../api/collector";
import { isApiError } from "../../api/errors";
import {
  COLLECTOR_RUN_STATUSES,
  COLLECTOR_RUN_TRIGGERS,
  type CollectorRun,
  type CollectorRunStatus,
  type CollectorRunTrigger,
  type Profile,
} from "../../api/types";
import { useApi } from "../../auth/AuthProvider";
import { StatusBadge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { DataTable, type Column } from "../../components/DataTable";
import { PageHeader } from "../../components/PageHeader";
import { Pagination } from "../../components/Pagination";
import { EmptyState, ErrorState } from "../../components/States";
import { formatDateTime } from "../../lib/format";
import { COLLECTOR_RUN_STATUS } from "../../lib/labels";
import { useActiveProfile } from "../../profile/ActiveProfileProvider";
import { RequireProfile } from "../../profile/ProfileControls";

const TRIGGER_LABEL: Record<CollectorRunTrigger, string> = { command: "Command line", api: "This app" };

export interface RunListParams {
  status?: CollectorRunStatus;
  trigger?: CollectorRunTrigger;
  mine: boolean;
  page: number;
}

function oneOf<T extends string>(value: string | null, allowed: readonly T[]): T | undefined {
  return value !== null && (allowed as readonly string[]).includes(value) ? (value as T) : undefined;
}

export function readRunParams(search: URLSearchParams): RunListParams {
  const page = search.get("page");
  return {
    status: oneOf(search.get("status"), COLLECTOR_RUN_STATUSES),
    trigger: oneOf(search.get("trigger"), COLLECTOR_RUN_TRIGGERS),
    mine: search.get("mine") === "1",
    page: page && /^[1-9]\d{0,9}$/.test(page) ? Number(page) : 1,
  };
}

function writeRunParams(current: RunListParams, changes: Partial<RunListParams>): URLSearchParams {
  const next = { ...current, ...changes };
  if (!("page" in changes)) next.page = 1;
  const search = new URLSearchParams();
  if (next.status) search.set("status", next.status);
  if (next.trigger) search.set("trigger", next.trigger);
  if (next.mine) search.set("mine", "1");
  if (next.page > 1) search.set("page", String(next.page));
  return search;
}

export function formatDuration(seconds: number | null): string {
  if (seconds === null) return "—";
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} min ${Math.round(seconds % 60)} s`;
}

function Select({ label, value, onChange, children }: { label: string; value: string; onChange: (v: string) => void; children: ReactNode }) {
  const id = useId();
  return (
    <div className="filter">
      <label htmlFor={id} className="filter__label">
        {label}
      </label>
      <select id={id} className="input input--compact" value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">All</option>
        {children}
      </select>
    </div>
  );
}

function RunHistory({ profile }: { profile: Profile }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const [search, setSearch] = useSearchParams();
  const params = readRunParams(search);
  const filters: RunFilters = {};
  if (params.status) filters.status = params.status;
  if (params.trigger) filters.trigger = params.trigger;
  if (params.mine) filters.profile = profile.id;
  const change = (changes: Partial<RunListParams>) => setSearch(writeRunParams(params, changes));

  const runs = useQuery({
    queryKey: collectorKeys.runs(filters, params.page),
    queryFn: ({ signal }) => listRuns(api, filters, params.page, signal),
    placeholderData: keepPreviousData,
    staleTime: Infinity, // refreshed only on request or after a run finishes
    refetchOnMount: false,
  });
  const refresh = () => void queryClient.invalidateQueries({ queryKey: collectorKeys.all });
  const runningRun = runs.data?.items.find((run) => run.status === "RUNNING") ?? null;
  const outOfRange = isApiError(runs.error) && runs.error.kind === "not_found" && params.page > 1;

  const columns: Column<CollectorRun>[] = [
    {
      key: "run",
      header: "Run",
      primary: true,
      render: (run) => (
        <div className="job-cell">
          <Link className="job-cell__title" to={`/collector/runs/${run.id}`}>
            Run #{run.id}
          </Link>
          <span className="muted">{formatDateTime(run.started_at)}</span>
        </div>
      ),
    },
    { key: "status", header: "Status", render: (run) => <StatusBadge kind="collector" value={run.status} /> },
    {
      key: "keywords",
      header: "Search",
      render: (run) => {
        const text = run.search_query || run.keywords.join(", ");
        return text ? <span className="clamp">{text}</span> : <span className="muted">—</span>;
      },
    },
    { key: "limit", header: "Jobs wanted", numeric: true, render: (run) => run.limit ?? "—" },
    {
      key: "result",
      header: "Result",
      render: (run) =>
        typeof run.counts.found === "number" ? (
          <span>
            {run.counts.created ?? 0} new · {run.counts.existing ?? 0} duplicates · {run.counts.found} checked
          </span>
        ) : (
          <span className="muted">—</span>
        ),
    },
    { key: "trigger", header: "Started from", render: (run) => TRIGGER_LABEL[run.trigger] ?? run.trigger },
  ];

  let body: ReactNode;
  if (outOfRange) {
    body = (
      <EmptyState title="This page no longer exists" action={<Button onClick={() => change({ page: 1 })}>Go to the first page</Button>}>
        The history has fewer pages now.
      </EmptyState>
    );
  } else if (runs.error && !runs.data) {
    body = <ErrorState title="Could not load the run history" error={runs.error} onRetry={() => void runs.refetch()} />;
  } else {
    body = (
      <>
        <DataTable
          caption="Collector runs"
          columns={columns}
          rows={runs.data?.items}
          rowKey={(run) => run.id}
          isLoading={runs.isPending}
          emptyTitle={params.status || params.trigger || params.mine ? "No runs match these filters" : "No collector runs yet"}
          empty={params.status || params.trigger || params.mine ? undefined : "Runs started here or with the collect_linkedin command appear here."}
        />
        {runs.data && (
          <Pagination page={params.page} pageSize={RUNS_PAGE_SIZE} count={runs.data.count} onPageChange={(page) => change({ page })} disabled={runs.isFetching} />
        )}
      </>
    );
  }

  return (
    <>
      <section className="panel" aria-labelledby="start-title">
        <h2 id="start-title" className="panel__title">
          Collect jobs
        </h2>
        <p className="panel__subtitle">
          Collections are started from <Link to="/automation">Job Automation</Link>, where you choose the search, the date range
          and how many jobs you want, and follow the progress.
        </p>
        {runningRun && (
          <p className="notice notice--info" role="note">
            Collection <Link to={`/collector/runs/${runningRun.id}`}>#{runningRun.id}</Link> is running now.
          </p>
        )}
      </section>
      <section className="panel" aria-labelledby="history-title">
        <div className="panel__header">
          <div>
            <h2 id="history-title" className="panel__title">
              Run history
            </h2>
            <p className="panel__subtitle">Newest first. Refreshes after a run finishes, or when you ask.</p>
          </div>
          <Button size="sm" onClick={refresh} loading={runs.isFetching && !runs.isPending}>
            Refresh
          </Button>
        </div>
        <div className="filters filters--flat" role="group" aria-label="Run filters">
          <Select label="Status" value={params.status ?? ""} onChange={(v) => change({ status: (v || undefined) as CollectorRunStatus | undefined })}>
            {COLLECTOR_RUN_STATUSES.map((status) => (
              <option key={status} value={status}>
                {COLLECTOR_RUN_STATUS[status].label}
              </option>
            ))}
          </Select>
          <Select label="Started from" value={params.trigger ?? ""} onChange={(v) => change({ trigger: (v || undefined) as CollectorRunTrigger | undefined })}>
            {COLLECTOR_RUN_TRIGGERS.map((trigger) => (
              <option key={trigger} value={trigger}>
                {TRIGGER_LABEL[trigger]}
              </option>
            ))}
          </Select>
          <label className="checkbox filter__toggle">
            <input type="checkbox" checked={params.mine} onChange={(e) => change({ mine: e.target.checked })} />
            <span>Only runs for {profile.name}</span>
          </label>
        </div>
        {body}
      </section>
    </>
  );
}

export function CollectorPage() {
  const { profile } = useActiveProfile();
  return (
    <>
      <PageHeader
        title="Collections"
        description="Every LinkedIn collection: what it searched for, how far it got, and each post it checked."
      />
      <RequireProfile>{profile && <RunHistory key={profile.id} profile={profile} />}</RequireProfile>
    </>
  );
}
