/**
 * Jobs list: backend-supported filters only (no text search, no sorting - the
 * backend has neither; jobs are newest-discovered first), server pagination, and
 * an explicit, confirmed "re-screen all" action. Nothing is screened on load or
 * on a profile switch.
 */

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState, type ReactNode } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";

import { isApiError } from "../../api/errors";
import { jobKeys, JOBS_PAGE_SIZE, listCompanies, listJobs, companyKeys, screenAllJobs } from "../../api/jobs";
import { listSkills, skillKeys } from "../../api/resources";
import {
  JOB_SOURCES,
  JOB_STATUSES,
  SCREENING_OUTCOMES,
  SCREENING_REASONS,
  UNSCREENED,
  type JobSummary,
  type Profile,
  type ScreenAllResult,
} from "../../api/types";
import { useApi } from "../../auth/TokenProvider";
import { StatusBadge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { DataTable, type Column } from "../../components/DataTable";
import { ConfirmDialog } from "../../components/Dialog";
import { PageHeader } from "../../components/PageHeader";
import { Pagination } from "../../components/Pagination";
import { EmptyState, ErrorState } from "../../components/States";
import { useToast } from "../../components/Toast";
import { formatDate } from "../../lib/format";
import { JOB_SOURCE, JOB_STATUS, SCREENING_OUTCOME, SCREENING_REASON } from "../../lib/labels";
import { useActiveProfile } from "../../profile/ActiveProfileProvider";
import { RequireProfile } from "../../profile/ProfileControls";
import { AddJobDialog } from "./AddJobDialog";
import { hasFilters, readParams, toApiFilters, writeParams, type JobListParams } from "./filters";

const MAX_KEYWORDS_SHOWN = 3;

function FilterSelect({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string }[];
}) {
  const id = useId();
  return (
    <div className="filter">
      <label htmlFor={id} className="filter__label">
        {label}
      </label>
      <select id={id} className="input input--compact" value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="">All</option>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </div>
  );
}

function Filters({ params, profile, onChange }: { params: JobListParams; profile: Profile; onChange: (changes: Partial<JobListParams>) => void }) {
  const api = useApi();
  const companies = useQuery({ queryKey: companyKeys.all, queryFn: ({ signal }) => listCompanies(api, signal) });

  return (
    <section className="filters" aria-label="Filters">
      <FilterSelect
        label="Screening"
        value={params.outcome ?? ""}
        onChange={(v) => onChange({ outcome: (v || undefined) as JobListParams["outcome"] })}
        options={[...SCREENING_OUTCOMES, UNSCREENED].map((value) => ({ value, label: SCREENING_OUTCOME[value].label }))}
      />
      <FilterSelect
        label="Reason"
        value={params.reason ?? ""}
        onChange={(v) => onChange({ reason: (v || undefined) as JobListParams["reason"] })}
        options={SCREENING_REASONS.map((value) => ({ value, label: SCREENING_REASON[value] }))}
      />
      <FilterSelect
        label="Your status"
        value={params.status ?? ""}
        onChange={(v) => onChange({ status: (v || undefined) as JobListParams["status"] })}
        options={JOB_STATUSES.map((value) => ({ value, label: JOB_STATUS[value].label }))}
      />
      <FilterSelect
        label="Source"
        value={params.source ?? ""}
        onChange={(v) => onChange({ source: (v || undefined) as JobListParams["source"] })}
        options={JOB_SOURCES.map((value) => ({ value, label: JOB_SOURCE[value] }))}
      />
      <FilterSelect
        label="Company"
        value={params.company ? String(params.company) : ""}
        onChange={(v) => onChange({ company: v ? Number(v) : undefined })}
        options={(companies.data ?? []).map((company) => ({ value: String(company.id), label: company.name }))}
      />
      <label className="checkbox filter__toggle">
        <input type="checkbox" checked={params.mine} onChange={(event) => onChange({ mine: event.target.checked })} />
        <span>Only screened for {profile.name}</span>
      </label>
    </section>
  );
}

function ScreenAllAction({ profile }: { profile: Profile }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const { notify } = useToast();
  const [confirming, setConfirming] = useState(false);
  const [summary, setSummary] = useState<ScreenAllResult | null>(null);
  const skills = useQuery({
    queryKey: skillKeys.forProfile(profile.id),
    queryFn: ({ signal }) => listSkills(api, profile.id, signal),
  });

  const mutation = useMutation({
    mutationFn: () => screenAllJobs(api, profile.id),
    onSuccess: (result) => {
      setSummary(result);
      void queryClient.invalidateQueries({ queryKey: jobKeys.all });
      notify(`Screened ${result.screened} job${result.screened === 1 ? "" : "s"}`, "success");
    },
  });

  const skillCount = skills.data?.length ?? 0;
  return (
    <>
      <Button onClick={() => setConfirming(true)} disabled={mutation.isPending || !skills.data}>
        Re-screen all jobs
      </Button>
      <ConfirmDialog
        open={confirming}
        title={`Re-screen every job with ${profile.name}?`}
        message={
          skillCount === 0 ? (
            <>
              {profile.name} has no skills yet, so the backend will refuse to screen.{" "}
              <Link to="/profile">Add skills first</Link>.
            </>
          ) : (
            <>
              Every job is screened again against {profile.name}'s {skillCount} skill{skillCount === 1 ? "" : "s"} and
              experience limit ({profile.max_acceptable_experience_years ?? "—"} years). Each job's current outcome is
              replaced, whichever profile it was screened for before. Your job statuses do not change.
            </>
          )
        }
        confirmLabel="Re-screen all"
        onConfirm={async () => {
          await mutation.mutateAsync();
          setConfirming(false);
        }}
        onCancel={() => setConfirming(false)}
      />
      {summary && <ScreenSummary summary={summary} onDismiss={() => setSummary(null)} />}
    </>
  );
}

function ScreenSummary({ summary, onDismiss }: { summary: ScreenAllResult; onDismiss: () => void }) {
  return (
    <div className="notice notice--success screen-summary" role="status">
      <span>
        Screened {summary.screened} job{summary.screened === 1 ? "" : "s"}: {summary.outcomes.SHORTLISTED} shortlisted,{" "}
        {summary.outcomes.NEEDS_REVIEW} need review, {summary.outcomes.REJECTED} rejected.
      </span>
      <button type="button" className="toast__close" onClick={onDismiss} aria-label="Dismiss summary">
        ✕
      </button>
    </div>
  );
}

function Keywords({ keywords }: { keywords: string[] }) {
  if (keywords.length === 0) return <span className="muted">—</span>;
  const shown = keywords.slice(0, MAX_KEYWORDS_SHOWN);
  const more = keywords.length - shown.length;
  return (
    <span className="keywords" title={keywords.join(", ")}>
      {shown.join(", ")}
      {more > 0 && <span className="muted"> +{more}</span>}
    </span>
  );
}

function JobList({ profile }: { profile: Profile }) {
  const api = useApi();
  const location = useLocation();
  const [search, setSearch] = useSearchParams();
  const params = readParams(search);
  const filters = toApiFilters(params, profile.id);
  const [adding, setAdding] = useState(false);

  const jobs = useQuery({
    queryKey: jobKeys.list(filters, params.page),
    queryFn: ({ signal }) => listJobs(api, filters, params.page, signal),
    placeholderData: keepPreviousData,
  });

  const change = (changes: Partial<JobListParams>) => setSearch(writeParams(params, changes));
  const outOfRange = isApiError(jobs.error) && jobs.error.kind === "not_found" && params.page > 1;

  const columns: Column<JobSummary>[] = [
    {
      key: "title",
      header: "Job",
      primary: true,
      render: (job) => (
        <div className="job-cell">
          <Link className="job-cell__title" to={`/jobs/${job.id}`} state={{ from: location.search }}>
            {job.title}
          </Link>
          <span className="muted">{[job.company?.name, job.location, job.work_mode].filter(Boolean).join(" · ") || "Company not given"}</span>
        </div>
      ),
    },
    {
      key: "screening",
      header: "Screening",
      render: (job) => (
        <div className="stack-xs">
          <StatusBadge kind="screening" value={job.screening_outcome} />
          {job.screening_reason && <span className="muted small">{SCREENING_REASON[job.screening_reason]}</span>}
        </div>
      ),
    },
    { key: "status", header: "Your status", render: (job) => <StatusBadge kind="job" value={job.status} /> },
    { key: "keywords", header: "Matched skills", render: (job) => <Keywords keywords={job.matched_keywords} /> },
    { key: "experience", header: "Experience", render: (job) => job.required_experience_text || <span className="muted">—</span> },
    { key: "discovered", header: "Found", render: (job) => formatDate(job.discovered_date) },
  ];

  let body: ReactNode;
  if (outOfRange) {
    body = (
      <EmptyState title="This page no longer exists" action={<Button onClick={() => change({ page: 1 })}>Go to the first page</Button>}>
        The list has fewer pages now.
      </EmptyState>
    );
  } else if (jobs.error && !jobs.data) {
    body = <ErrorState title="Could not load jobs" error={jobs.error} onRetry={() => void jobs.refetch()} />;
  } else {
    body = (
      <>
        <DataTable
          caption="Jobs"
          columns={columns}
          rows={jobs.data?.items}
          rowKey={(job) => job.id}
          isLoading={jobs.isPending}
          emptyTitle={hasFilters(params) ? "No jobs match these filters" : "No jobs yet"}
          empty={
            hasFilters(params) ? (
              <Button size="sm" onClick={() => setSearch(new URLSearchParams())}>
                Clear filters
              </Button>
            ) : (
              "Jobs appear here after a collector run, or add one manually."
            )
          }
        />
        {jobs.data && (
          <Pagination
            page={params.page}
            pageSize={JOBS_PAGE_SIZE}
            count={jobs.data.count}
            onPageChange={(page) => change({ page })}
            disabled={jobs.isFetching}
          />
        )}
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Jobs"
        description="Collected and added jobs, newest first, with how each was screened."
        actions={
          <>
            <ScreenAllAction profile={profile} />
            <Button variant="primary" onClick={() => setAdding(true)}>
              Add job manually
            </Button>
          </>
        }
      />
      <Filters params={params} profile={profile} onChange={change} />
      <div className="list-meta" aria-live="polite">
        {jobs.data && (
          <span>
            {jobs.data.count} job{jobs.data.count === 1 ? "" : "s"}
            {hasFilters(params) ? " match" : ""}
          </span>
        )}
        {jobs.isFetching && jobs.data && <span className="muted">Updating…</span>}
        {hasFilters(params) && (
          <Button size="sm" variant="ghost" onClick={() => setSearch(new URLSearchParams())}>
            Clear filters
          </Button>
        )}
      </div>
      {body}
      <AddJobDialog open={adding} profile={profile} onClose={() => setAdding(false)} />
    </>
  );
}

export function JobsPage() {
  const { profile } = useActiveProfile();
  return <RequireProfile>{profile && <JobList profile={profile} />}</RequireProfile>;
}
