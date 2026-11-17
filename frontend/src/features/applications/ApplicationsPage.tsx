/**
 * Applications of the active profile: summary counts (GET applications/summary/),
 * backend-supported filters (status, channel, company, awaiting_action) kept in the
 * URL, and a paginated list. Nothing is generated or sent from this page.
 */

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useId, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";

import {
  APPLICATIONS_PAGE_SIZE,
  applicationKeys,
  getApplicationsOverview,
  listApplications,
  type ApplicationFilters,
} from "../../api/applications";
import { isApiError } from "../../api/errors";
import { companyKeys, listCompanies } from "../../api/jobs";
import {
  APPLICATION_CHANNELS,
  APPLICATION_STATUSES,
  type ApplicationChannel,
  type ApplicationStatus,
  type ApplicationSummary,
  type Profile,
} from "../../api/types";
import { useApi } from "../../auth/TokenProvider";
import { StatusBadge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { DataTable, type Column } from "../../components/DataTable";
import { PageHeader } from "../../components/PageHeader";
import { Pagination } from "../../components/Pagination";
import { EmptyState, ErrorState, Skeleton } from "../../components/States";
import { formatDate } from "../../lib/format";
import { APPLICATION_CHANNEL, APPLICATION_STATUS } from "../../lib/labels";
import { useActiveProfile } from "../../profile/ActiveProfileProvider";
import { RequireProfile } from "../../profile/ProfileControls";

export interface ApplicationListParams {
  status?: ApplicationStatus;
  channel?: ApplicationChannel;
  company?: number;
  awaiting: boolean;
  page: number;
}

function oneOf<T extends string>(value: string | null, allowed: readonly T[]): T | undefined {
  return value !== null && (allowed as readonly string[]).includes(value) ? (value as T) : undefined;
}
const positiveInt = (value: string | null) => (value !== null && /^[1-9]\d{0,9}$/.test(value) ? Number(value) : undefined);

export function readApplicationParams(search: URLSearchParams): ApplicationListParams {
  return {
    status: oneOf(search.get("status"), APPLICATION_STATUSES),
    channel: oneOf(search.get("channel"), APPLICATION_CHANNELS),
    company: positiveInt(search.get("company")),
    awaiting: search.get("awaiting") === "1",
    page: positiveInt(search.get("page")) ?? 1,
  };
}

export function writeApplicationParams(current: ApplicationListParams, changes: Partial<ApplicationListParams>): URLSearchParams {
  const next = { ...current, ...changes };
  if (!("page" in changes)) next.page = 1;
  const search = new URLSearchParams();
  if (next.status) search.set("status", next.status);
  if (next.channel) search.set("channel", next.channel);
  if (next.company) search.set("company", String(next.company));
  if (next.awaiting) search.set("awaiting", "1");
  if (next.page > 1) search.set("page", String(next.page));
  return search;
}

function toFilters(params: ApplicationListParams, profile: number): ApplicationFilters {
  const filters: ApplicationFilters = { profile };
  if (params.status) filters.status = params.status;
  if (params.channel) filters.channel = params.channel;
  if (params.company) filters.company = params.company;
  if (params.awaiting) filters.awaiting_action = true;
  return filters;
}

const hasFilters = (p: ApplicationListParams) => Boolean(p.status || p.channel || p.company || p.awaiting);

function Summary({ profile, onPick }: { profile: Profile; onPick: (changes: Partial<ApplicationListParams>) => void }) {
  const api = useApi();
  const overview = useQuery({
    queryKey: applicationKeys.summary(profile.id),
    queryFn: ({ signal }) => getApplicationsOverview(api, profile.id, signal),
  });
  if (overview.error) {
    return <ErrorState title="Could not load the summary" error={overview.error} onRetry={() => void overview.refetch()} />;
  }
  if (!overview.data) return <Skeleton lines={2} label="Loading summary" />;
  const { total, by_status: byStatus, awaiting_action: awaiting } = overview.data;
  const shown = APPLICATION_STATUSES.filter((status) => byStatus[status] > 0);

  return (
    <section className="summary" aria-label="Summary">
      <button type="button" className="summary__card" onClick={() => onPick({ status: undefined, awaiting: false })}>
        <span className="summary__value">{total}</span>
        <span className="summary__label">Applications</span>
      </button>
      <button type="button" className="summary__card summary__card--accent" onClick={() => onPick({ status: undefined, awaiting: true })}>
        <span className="summary__value">{awaiting.count}</span>
        <span className="summary__label">Waiting for you</span>
      </button>
      {shown.length > 0 && (
        <ul className="summary__statuses" aria-label="By status">
          {shown.map((status) => (
            <li key={status}>
              <button type="button" className="summary__status" onClick={() => onPick({ status, awaiting: false })}>
                <StatusBadge kind="application" value={status} />
                <span className="summary__count">{byStatus[status]}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
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

function ApplicationList({ profile }: { profile: Profile }) {
  const api = useApi();
  const [search, setSearch] = useSearchParams();
  const params = readApplicationParams(search);
  const filters = toFilters(params, profile.id);
  const change = (changes: Partial<ApplicationListParams>) => setSearch(writeApplicationParams(params, changes));
  const companies = useQuery({ queryKey: companyKeys.all, queryFn: ({ signal }) => listCompanies(api, signal) });

  const applications = useQuery({
    queryKey: applicationKeys.list(filters, params.page),
    queryFn: ({ signal }) => listApplications(api, filters, params.page, signal),
    placeholderData: keepPreviousData,
  });
  const outOfRange = isApiError(applications.error) && applications.error.kind === "not_found" && params.page > 1;

  const columns: Column<ApplicationSummary>[] = [
    {
      key: "job",
      header: "Job",
      primary: true,
      render: (a) => (
        <div className="job-cell">
          <Link className="job-cell__title" to={`/applications/${a.id}`}>
            {a.job.title}
          </Link>
          <span className="muted">{a.job.company?.name ?? "Company not given"}</span>
        </div>
      ),
    },
    { key: "status", header: "Status", render: (a) => <StatusBadge kind="application" value={a.status} /> },
    { key: "channel", header: "Channel", render: (a) => APPLICATION_CHANNEL[a.channel] },
    {
      key: "draft",
      header: "Draft subject",
      render: (a) => (a.generated_subject ? <span className="clamp">{a.generated_subject}</span> : <span className="muted">No draft yet</span>),
    },
    { key: "created", header: "Created", render: (a) => formatDate(a.created_at) },
    { key: "sent", header: "Sent", render: (a) => (a.sent_at ? formatDate(a.sent_at) : <span className="muted">—</span>) },
  ];

  let body: ReactNode;
  if (outOfRange) {
    body = (
      <EmptyState title="This page no longer exists" action={<Button onClick={() => change({ page: 1 })}>Go to the first page</Button>}>
        The list has fewer pages now.
      </EmptyState>
    );
  } else if (applications.error && !applications.data) {
    body = <ErrorState title="Could not load applications" error={applications.error} onRetry={() => void applications.refetch()} />;
  } else {
    body = (
      <>
        <DataTable
          caption="Applications"
          columns={columns}
          rows={applications.data?.items}
          rowKey={(a) => a.id}
          isLoading={applications.isPending}
          emptyTitle={hasFilters(params) ? "No applications match these filters" : "No applications yet"}
          empty={
            hasFilters(params) ? (
              <Button size="sm" onClick={() => setSearch(new URLSearchParams())}>
                Clear filters
              </Button>
            ) : (
              <>
                Start one from a shortlisted job on the <Link to="/jobs?status=SHORTLISTED">Jobs</Link> page.
              </>
            )
          }
        />
        {applications.data && (
          <Pagination
            page={params.page}
            pageSize={APPLICATIONS_PAGE_SIZE}
            count={applications.data.count}
            onPageChange={(page) => change({ page })}
            disabled={applications.isFetching}
          />
        )}
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Applications"
        description={`${profile.name}'s applications. Drafts are prepared here; you send everything yourself.`}
      />
      <Summary profile={profile} onPick={change} />
      <section className="filters" aria-label="Filters">
        <Select label="Status" value={params.status ?? ""} onChange={(v) => change({ status: (v || undefined) as ApplicationStatus | undefined })}>
          {APPLICATION_STATUSES.map((status) => (
            <option key={status} value={status}>
              {APPLICATION_STATUS[status].label}
            </option>
          ))}
        </Select>
        <Select label="Channel" value={params.channel ?? ""} onChange={(v) => change({ channel: (v || undefined) as ApplicationChannel | undefined })}>
          {APPLICATION_CHANNELS.map((channel) => (
            <option key={channel} value={channel}>
              {APPLICATION_CHANNEL[channel]}
            </option>
          ))}
        </Select>
        <Select label="Company" value={params.company ? String(params.company) : ""} onChange={(v) => change({ company: v ? Number(v) : undefined })}>
          {(companies.data ?? []).map((company) => (
            <option key={company.id} value={company.id}>
              {company.name}
            </option>
          ))}
        </Select>
        <label className="checkbox filter__toggle">
          <input type="checkbox" checked={params.awaiting} onChange={(e) => change({ awaiting: e.target.checked })} />
          <span>Only those waiting for me</span>
        </label>
      </section>
      <div className="list-meta" aria-live="polite">
        {applications.data && (
          <span>
            {applications.data.count} application{applications.data.count === 1 ? "" : "s"}
            {hasFilters(params) ? " match" : ""}
          </span>
        )}
        {applications.isFetching && applications.data && <span className="muted">Updating…</span>}
        {hasFilters(params) && (
          <Button size="sm" variant="ghost" onClick={() => setSearch(new URLSearchParams())}>
            Clear filters
          </Button>
        )}
      </div>
      {body}
    </>
  );
}

export function ApplicationsPage() {
  const { profile } = useActiveProfile();
  return <RequireProfile>{profile && <ApplicationList key={profile.id} profile={profile} />}</RequireProfile>;
}
