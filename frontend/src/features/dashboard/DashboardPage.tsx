/**
 * Dashboard: a read-only overview built only from existing endpoints, reusing the
 * other sections' API functions and query keys (so their actions refresh it):
 *
 *   profile     active profile (provider), GET skills/?profile=, GET resumes/?profile=
 *   jobs        GET jobs/?page_size=1 (+ screening_outcome=…) -> exact counts; GET jobs/?page_size=5
 *   apps        GET applications/summary/?profile=
 *   collector   GET collector/runs/ (page 1) -> latest run
 *
 * Every section loads and fails independently. A failed count shows "unavailable",
 * never 0. Each section says when its data was fetched. Data loads when the page
 * opens (if not fresh) or on "Refresh"; nothing polls. Nothing here screens,
 * generates, collects or sends anything - only GET requests.
 */

import { useIsFetching, useQueries, useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { applicationKeys, getApplicationsOverview } from "../../api/applications";
import { collectorKeys, listRuns } from "../../api/collector";
import { countJobs, jobKeys, recentJobs, type JobFilters } from "../../api/jobs";
import { listResumes, listSkills, resumeKeys, skillKeys } from "../../api/resources";
import { APPLICATION_STATUSES, type ApplicationsOverview, type ApplicationSummary, type Profile } from "../../api/types";
import { useApi } from "../../auth/AuthProvider";
import { StatusBadge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { PageHeader } from "../../components/PageHeader";
import { EmptyState, ErrorState, Skeleton } from "../../components/States";
import { formatDate, formatDateTime, formatDayUTC, splitList } from "../../lib/format";
import { SCREENING_OUTCOME, SCREENING_REASON } from "../../lib/labels";
import { useActiveProfile } from "../../profile/ActiveProfileProvider";
import { RequireProfile } from "../../profile/ProfileControls";
import { shortError } from "../collector/runText";

const RECENT_JOBS = 5;
const LIST_ITEMS = 5;
const OUTCOME_FILTERS: { key: string; filters: JobFilters; outcome: keyof typeof SCREENING_OUTCOME }[] = [
  { key: "SHORTLISTED", filters: { screening_outcome: "SHORTLISTED" }, outcome: "SHORTLISTED" },
  { key: "NEEDS_REVIEW", filters: { screening_outcome: "NEEDS_REVIEW" }, outcome: "NEEDS_REVIEW" },
  { key: "REJECTED", filters: { screening_outcome: "REJECTED" }, outcome: "REJECTED" },
  { key: "UNSCREENED", filters: { screening_outcome: "UNSCREENED" }, outcome: "UNSCREENED" },
];

const timeFormat = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });

/** When this section's data was fetched (or that it is being refreshed). */
function Freshness({ queries }: { queries: UseQueryResult[] }) {
  if (queries.some((q) => q.isFetching)) return <span className="freshness">Updating…</span>;
  const times = queries.map((q) => q.dataUpdatedAt).filter((t) => t > 0);
  if (times.length === 0) return null;
  return <span className="freshness">As of {timeFormat.format(new Date(Math.min(...times)))}</span>;
}

function Card({
  title,
  id,
  freshness,
  links,
  children,
}: {
  title: string;
  id: string;
  freshness?: ReactNode;
  links?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="panel dash-card" aria-labelledby={id}>
      <div className="panel__header">
        <h2 id={id} className="panel__title">
          {title}
        </h2>
        {freshness}
      </div>
      {children}
      {links && <div className="dash-card__links">{links}</div>}
    </section>
  );
}

/** `value`: a number; undefined while loading; null when it could not be loaded (never shown as 0). */
function Metric({ label, value, to, tone }: { label: string; value: number | null | undefined; to?: string; tone?: string }) {
  const shown = value === undefined ? "…" : value === null ? "—" : value;
  const content = (
    <>
      <span className={`metric__value${value === null ? " metric__value--na" : ""}`}>{shown}</span>
      <span className="metric__label">
        {label}
        {value === null && <span className="metric__na"> · unavailable</span>}
        {value === undefined && <span className="visually-hidden"> (loading)</span>}
      </span>
    </>
  );
  const className = `metric${tone ? ` metric--${tone}` : ""}`;
  return to && typeof value === "number" ? (
    <Link className={`${className} metric--link`} to={to}>
      {content}
    </Link>
  ) : (
    <div className={className} title={value === null ? "Unavailable: this count could not be loaded." : undefined}>
      {content}
    </div>
  );
}

function valueOf<T>(query: UseQueryResult<T>, pick: (data: T) => number): number | null | undefined {
  if (query.data !== undefined) return pick(query.data);
  return query.error ? null : undefined;
}

// --- Profile ---------------------------------------------------------------------------

function ProfileCard({ profile }: { profile: Profile }) {
  const api = useApi();
  const skills = useQuery({ queryKey: skillKeys.forProfile(profile.id), queryFn: ({ signal }) => listSkills(api, profile.id, signal) });
  const resumes = useQuery({ queryKey: resumeKeys.forProfile(profile.id), queryFn: ({ signal }) => listResumes(api, profile.id, signal) });
  const roles = splitList(profile.target_roles);
  const activeDefault = resumes.data?.find((r) => r.is_default && r.is_active);

  return (
    <Card
      title="Profile"
      id="dash-profile"
      freshness={<Freshness queries={[skills, resumes]} />}
      links={
        <>
          <Link to="/profile">Manage profile &amp; skills</Link>
          <Link to="/resumes">Manage resumes</Link>
        </>
      }
    >
      <p className="dash-profile__name">{profile.name}</p>
      {roles.length > 0 && (
        <ul className="chips chips--static" aria-label="Target roles">
          {roles.map((role) => (
            <li key={role} className="chip">
              {role}
            </li>
          ))}
        </ul>
      )}
      <p className="muted small">
        {profile.experience_years ?? "—"} years of experience · accepts requirements up to {profile.max_acceptable_experience_years ?? "—"} years
      </p>
      <div className="metrics">
        <Metric label="Skills" value={valueOf(skills, (d) => d.length)} to="/profile" />
        <Metric label="Resumes" value={valueOf(resumes, (d) => d.length)} to="/resumes" />
      </div>
      {skills.error && <ErrorState title="Could not load skills" error={skills.error} onRetry={() => void skills.refetch()} />}
      {resumes.error && <ErrorState title="Could not load resumes" error={resumes.error} onRetry={() => void resumes.refetch()} />}
      {skills.data && skills.data.length === 0 && (
        <p className="notice notice--warning">No skills yet - jobs cannot be screened. <Link to="/profile">Add skills</Link>.</p>
      )}
      {resumes.data && !activeDefault && (
        <p className="notice notice--warning">
          No active default resume - drafts need one. <Link to="/resumes">Choose a resume</Link>.
        </p>
      )}
      {activeDefault && <p className="muted small">Default resume: {activeDefault.name}</p>}
    </Card>
  );
}

// --- Jobs --------------------------------------------------------------------------------

function JobsCard() {
  const api = useApi();
  const total = useQuery({ queryKey: jobKeys.count({}), queryFn: ({ signal }) => countJobs(api, {}, signal) });
  const outcomes = useQueries({
    queries: OUTCOME_FILTERS.map(({ filters }) => ({
      queryKey: jobKeys.count(filters),
      queryFn: ({ signal }: { signal: AbortSignal }) => countJobs(api, filters, signal),
    })),
  });
  const recent = useQuery({ queryKey: jobKeys.recent(RECENT_JOBS), queryFn: ({ signal }) => recentJobs(api, RECENT_JOBS, signal) });
  const countQueries = [total, ...outcomes];
  const failedCounts = countQueries.filter((q) => q.error);

  return (
    <Card
      title="Jobs"
      id="dash-jobs"
      freshness={<Freshness queries={[...countQueries, recent]} />}
      links={<Link to="/jobs">View all jobs</Link>}
    >
      <div className="metrics">
        <Metric label="Jobs in total" value={valueOf(total, (d) => d)} to="/jobs" />
        {OUTCOME_FILTERS.map(({ key, outcome }, index) => (
          <Metric
            key={key}
            label={outcome === "UNSCREENED" ? "Not screened yet" : `Screened: ${SCREENING_OUTCOME[outcome].label.toLowerCase()}`}
            value={outcomes[index] ? valueOf(outcomes[index], (d) => d) : undefined}
            to={`/jobs?outcome=${key}`}
            tone={SCREENING_OUTCOME[outcome].tone}
          />
        ))}
      </div>
      <p className="muted small">
        Automatic screening results for all jobs (each job's latest screening). Your own decision per job is its status on
        the <Link to="/jobs">Jobs</Link> page.
      </p>
      {failedCounts.length > 0 && (
        <ErrorState
          title="Some job counts could not be loaded"
          error={failedCounts[0]!.error}
          onRetry={() => failedCounts.forEach((q) => void q.refetch())}
        />
      )}

      <h3 className="dash-subtitle">Recently found</h3>
      {recent.error ? (
        <ErrorState title="Could not load recent jobs" error={recent.error} onRetry={() => void recent.refetch()} />
      ) : !recent.data ? (
        <Skeleton lines={3} label="Loading recent jobs" />
      ) : recent.data.length === 0 ? (
        <EmptyState title="No jobs yet">
          Run the <Link to="/collector">collector</Link> or add a job on the <Link to="/jobs">Jobs</Link> page.
        </EmptyState>
      ) : (
        <ul className="dash-list" aria-label="Recently found jobs">
          {recent.data.map((job) => (
            <li key={job.id} className="dash-list__item">
              <div className="dash-list__main">
                <Link to={`/jobs/${job.id}`}>{job.title}</Link>
                <span className="muted small">
                  {[job.company?.name, formatDate(job.discovered_date)].filter(Boolean).join(" · ")}
                </span>
              </div>
              <span className="badges">
                <StatusBadge kind="screening" value={job.screening_outcome} />
                {job.screening_reason && <span className="muted small">{SCREENING_REASON[job.screening_reason]}</span>}
              </span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

// --- Applications ------------------------------------------------------------------------

function ApplicationList({ items, label, showFollowUp = false }: { items: ApplicationSummary[]; label: string; showFollowUp?: boolean }) {
  return (
    <ul className="dash-list" aria-label={label}>
      {items.slice(0, LIST_ITEMS).map((a) => (
        <li key={a.id} className="dash-list__item">
          <div className="dash-list__main">
            <Link to={`/applications/${a.id}`}>{a.job.title}</Link>
            <span className="muted small">
              {[
                a.job.company?.name,
                showFollowUp && a.follow_up_on ? `follow up since ${formatDayUTC(a.follow_up_on)}` : `updated ${formatDate(a.updated_at)}`,
              ]
                .filter(Boolean)
                .join(" · ")}
            </span>
          </div>
          <StatusBadge kind="application" value={a.status} />
        </li>
      ))}
    </ul>
  );
}

function ApplicationsCard({ profile }: { profile: Profile }) {
  const api = useApi();
  const overview = useQuery({
    queryKey: applicationKeys.summary(profile.id),
    queryFn: ({ signal }) => getApplicationsOverview(api, profile.id, signal),
  });
  const data = overview.data;
  const statuses = data ? APPLICATION_STATUSES.filter((s) => (data.by_status[s] ?? 0) > 0) : [];
  // Older backends do not report these; show them as unavailable rather than crash or claim 0.
  const due = data?.follow_ups_due as ApplicationsOverview["follow_ups_due"] | undefined;
  const count = (n: number | undefined) => (typeof n === "number" ? n : null);

  return (
    <Card
      title="Applications"
      id="dash-applications"
      freshness={<Freshness queries={[overview]} />}
      links={
        <>
          <Link to="/applications">View applications</Link>
          <Link to="/applications?awaiting=1">Review those waiting for you</Link>
        </>
      }
    >
      {overview.error ? (
        <ErrorState title="Could not load applications" error={overview.error} onRetry={() => void overview.refetch()} />
      ) : !data ? (
        <Skeleton lines={4} label="Loading applications" />
      ) : data.total === 0 ? (
        <EmptyState title={`No applications for ${profile.name} yet`}>
          Shortlist a job, then start an application from its page. <Link to="/jobs?status=SHORTLISTED">Shortlisted jobs</Link>
        </EmptyState>
      ) : (
        <>
          <div className="metrics">
            <Metric label="Applications" value={data.total} to="/applications" />
            <Metric label="Waiting for you" value={data.awaiting_action.count} to="/applications?awaiting=1" tone="accent" />
            <Metric
              label="Follow-ups due"
              value={count(due?.count)}
              to="/applications?due=1"
              tone={due?.count ? "warning" : undefined}
            />
          </div>
          <div className="metrics" aria-label="Outcomes">
            <Metric label="Sent" value={count(data.sent_total)} to="/applications?status=SENT" tone="info" />
            <Metric label="Interviewing" value={count(data.by_status.INTERVIEW)} to="/applications?status=INTERVIEW" tone="accent" />
            <Metric label="Offers" value={count(data.by_status.OFFER)} to="/applications?status=OFFER" tone="success" />
            <Metric label="Rejected" value={count(data.by_status.REJECTED)} to="/applications?status=REJECTED" />
          </div>
          {due && due.count > 0 && (
            <>
              <h3 className="dash-subtitle">Follow-ups due</h3>
              <ApplicationList items={due.items} label="Follow-ups due" showFollowUp />
            </>
          )}
          <ul className="status-counts" aria-label="Applications by status">
            {statuses.map((status) => (
              <li key={status}>
                <Link className="status-count" to={`/applications?status=${status}`}>
                  <StatusBadge kind="application" value={status} />
                  <span className="status-count__n">{data.by_status[status]}</span>
                </Link>
              </li>
            ))}
          </ul>
          <h3 className="dash-subtitle">Waiting for you (oldest first)</h3>
          {data.awaiting_action.oldest.length ? (
            <ApplicationList items={data.awaiting_action.oldest} label="Applications waiting for you" />
          ) : (
            <p className="muted">Nothing is waiting for you.</p>
          )}
          <h3 className="dash-subtitle">Recently created</h3>
          <ApplicationList items={data.recent} label="Recently created applications" />
        </>
      )}
    </Card>
  );
}

// --- Collector -----------------------------------------------------------------------------

function CollectorCard() {
  const api = useApi();
  // Same query as the Collector page's first history page: shared cache, no extra request.
  const runs = useQuery({ queryKey: collectorKeys.runs({}, 1), queryFn: ({ signal }) => listRuns(api, {}, 1, signal) });
  const latest = runs.data?.items[0];

  return (
    <Card
      title="Latest collector run"
      id="dash-collector"
      freshness={<Freshness queries={[runs]} />}
      links={
        <>
          <Link to="/collector">Collector &amp; run history</Link>
          {latest && <Link to={`/collector/runs/${latest.id}`}>Run #{latest.id} details</Link>}
        </>
      }
    >
      {runs.error ? (
        <ErrorState title="Could not load collector runs" error={runs.error} onRetry={() => void runs.refetch()} />
      ) : !runs.data ? (
        <Skeleton lines={3} label="Loading collector runs" />
      ) : !latest ? (
        <EmptyState title="No collector runs yet">
          Runs are started, after confirmation, on the <Link to="/collector">Collector</Link> page.
        </EmptyState>
      ) : (
        <>
          <p className="dash-run">
            <StatusBadge kind="collector" value={latest.status} />
            <span>
              Run #{latest.id} · {formatDateTime(latest.started_at)}
            </span>
          </p>
          <p className="muted small">Keywords: {latest.keywords.length ? latest.keywords.join(", ") : "—"}</p>
          {typeof latest.counts.found === "number" ? (
            <div className="metrics">
              <Metric label="Posts read" value={latest.counts.found} />
              <Metric label="New jobs" value={latest.counts.created ?? 0} tone="success" />
              <Metric label="Already known" value={latest.counts.existing ?? 0} />
              <Metric label="Failed" value={latest.counts.failed ?? 0} tone={latest.counts.failed ? "danger" : undefined} />
            </div>
          ) : (
            <p className="muted">No posts were processed in this run.</p>
          )}
          {latest.error && <p className="notice notice--warning">{shortError(latest.error)}</p>}
          <p className="muted small">
            {runs.data.count} run{runs.data.count === 1 ? "" : "s"} in total.
          </p>
        </>
      )}
    </Card>
  );
}

// --- Page ----------------------------------------------------------------------------------

function DashboardContent({ profile }: { profile: Profile }) {
  const queryClient = useQueryClient();
  const refreshing = useIsFetching() > 0;
  const refresh = () => {
    // Read-only refetches of everything the dashboard shows.
    for (const key of [jobKeys.lists(), applicationKeys.summary(profile.id), collectorKeys.all, skillKeys.forProfile(profile.id), resumeKeys.forProfile(profile.id)]) {
      void queryClient.invalidateQueries({ queryKey: key });
    }
  };

  return (
    <>
      <PageHeader
        title="Dashboard"
        description="Where things stand across jobs, applications and collection runs."
        actions={
          <Button onClick={refresh} loading={refreshing}>
            Refresh
          </Button>
        }
      />
      <div className="dashboard">
        <div className="dashboard__col">
          <ApplicationsCard profile={profile} />
          <JobsCard />
        </div>
        <div className="dashboard__col">
          <ProfileCard profile={profile} />
          <CollectorCard />
        </div>
      </div>
    </>
  );
}

export function DashboardPage() {
  const { profile } = useActiveProfile();
  return (
    <RequireProfile>
      {profile ? <DashboardContent key={profile.id} profile={profile} /> : null}
    </RequireProfile>
  );
}
