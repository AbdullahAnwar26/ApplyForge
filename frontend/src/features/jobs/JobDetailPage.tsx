/**
 * One job: every field the backend returns, the screening breakdown, contacts and
 * the full description (plain text, never HTML). Screening runs only when the user
 * clicks "Screen"; the job status is the user's own decision.
 */

import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useId, useState, type ReactNode } from "react";
import { Link, useLocation, useParams } from "react-router-dom";

import { isApiError } from "../../api/errors";
import { applicationKeys } from "../../api/applications";
import { companyKeys, getCompany, getJob, jobKeys, screenJob, updateJobStatus } from "../../api/jobs";
import { listSkills, skillKeys } from "../../api/resources";
import { JOB_STATUSES, type JobDetail, type JobStatus, type Profile, type ScreeningDetails } from "../../api/types";
import { useApi } from "../../auth/TokenProvider";
import { StatusBadge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { FormErrors } from "../../components/Field";
import { PageHeader } from "../../components/PageHeader";
import { EmptyState, ErrorState, Skeleton } from "../../components/States";
import { useToast } from "../../components/Toast";
import { formatDateTime, formatDayUTC } from "../../lib/format";
import { JOB_SOURCE, JOB_STATUS, JOB_STATUS_HELP, SCREENING_OUTCOME, SCREENING_REASON } from "../../lib/labels";
import { useActiveProfile } from "../../profile/ActiveProfileProvider";
import { RequireProfile } from "../../profile/ProfileControls";
import { StartApplication } from "../applications/StartApplication";

const DESCRIPTION_PREVIEW_CHARS = 1500;

/**
 * After a job status change or screening: every job query (lists, counts, other jobs'
 * details whose duplicate candidates may change) and every application query
 * (applications show the job's status and screening outcome).
 */
export function invalidateAfterJobChange(queryClient: QueryClient): void {
  void queryClient.invalidateQueries({ queryKey: jobKeys.all });
  void queryClient.invalidateQueries({ queryKey: applicationKeys.all });
}

/** Only http(s) links are ever rendered as links. */
export function safeHttpUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch {
    return null;
  }
}

function Item({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="details__item">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

const missing = <span className="muted">Not given</span>;
const years = (value: number | null | undefined) => (value === null || value === undefined ? "?" : `${value}`);

function ExternalLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noopener noreferrer nofollow" referrerPolicy="no-referrer">
      {children}
      <span className="visually-hidden"> (opens in a new tab)</span>
    </a>
  );
}

function CompanyWebsite({ companyId }: { companyId: number }) {
  const api = useApi();
  const company = useQuery({ queryKey: companyKeys.detail(companyId), queryFn: ({ signal }) => getCompany(api, companyId, signal) });
  const href = safeHttpUrl(company.data?.website);
  if (!href) return null;
  return (
    <Item label="Company website">
      <ExternalLink href={href}>{company.data!.website}</ExternalLink>
    </Item>
  );
}

/** "2–4 years", "3+ years", or null when the minimum is unknown. */
export function formatRange(min: number | null | undefined, max: number | null | undefined): string | null {
  if (min === null || min === undefined) return null;
  return max === null || max === undefined ? `${min}+ years` : `${min}–${max} years`;
}

function experienceSource(source: string | undefined) {
  if (source === "field") return "the experience field";
  if (source === "jd") return "the job description";
  return "nowhere (no requirement found)";
}

function ScreeningBreakdown({ job, details }: { job: JobDetail; details: ScreeningDetails }) {
  const { profiles } = useActiveProfile();
  const screenedFor = profiles.find((p) => p.id === details.profile_id);
  const experience = details.experience;
  return (
    <>
      <dl className="details">
        <Item label="Outcome">
          <span className="badges">
            <StatusBadge kind="screening" value={job.screening_outcome} />
            {job.screening_reason && <span>{SCREENING_REASON[job.screening_reason]}</span>}
          </span>
        </Item>
        <Item label="Screened">
          {formatDateTime(job.screened_at)}
          {details.profile_id !== undefined && (
            <> for {screenedFor ? screenedFor.name : <span className="muted">a profile that no longer exists</span>}</>
          )}
        </Item>
        <Item label="Matched skills">
          {job.matched_keywords.length ? (
            <ul className="chips chips--static">
              {job.matched_keywords.map((keyword) => (
                <li key={keyword} className="chip">
                  {keyword}
                </li>
              ))}
            </ul>
          ) : (
            <span className="muted">None of the profile's skills appear in the description.</span>
          )}
        </Item>
        {experience && (
          <Item label="Experience check">
            <ul className="plain-list plain-list--tight">
              <li>
                Required: {formatRange(experience.min, experience.max) ?? "unknown"}, read from {experienceSource(experience.source)}
              </li>
              {experience.matches && experience.matches.length > 0 && (
                <li>
                  Found: {experience.matches.map((m) => `“${m.text}”`).join(", ")}
                </li>
              )}
              <li>
                Accepted up to {years(experience.max_acceptable_years)} years; profile experience {years(experience.profile_experience_years)}{" "}
                years
              </li>
              {experience.above_actual_experience && <li>Asks for more than the profile's actual experience (within the limit).</li>}
            </ul>
          </Item>
        )}
        {details.flags && details.flags.length > 0 && (
          <Item label="All reasons">{details.flags.map((flag) => SCREENING_REASON[flag] ?? flag).join(", ")}</Item>
        )}
        {details.duplicate_candidates && details.duplicate_candidates.length > 0 && (
          <Item label="Possible duplicates">
            <ul className="plain-list plain-list--tight">
              {details.duplicate_candidates.map((candidate) => (
                <li key={candidate.job_id}>
                  <Link to={`/jobs/${candidate.job_id}`}>Job #{candidate.job_id}</Link>{" "}
                  <span className="muted">(same {candidate.matched_on.join(" and ")})</span>
                </li>
              ))}
            </ul>
          </Item>
        )}
      </dl>
    </>
  );
}

function ScreeningPanel({ job, profile }: { job: JobDetail; profile: Profile }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const { notify } = useToast();
  const skills = useQuery({ queryKey: skillKeys.forProfile(profile.id), queryFn: ({ signal }) => listSkills(api, profile.id, signal) });

  const mutation = useMutation({
    mutationFn: () => screenJob(api, job.id, profile.id),
    onSuccess: (updated) => {
      queryClient.setQueryData(jobKeys.detail(job.id), updated);
      invalidateAfterJobChange(queryClient);
      const outcome = updated.screening_outcome ? SCREENING_OUTCOME[updated.screening_outcome].label : "Not screened";
      notify(`Screened: ${outcome}`, "success");
    },
  });

  const details = job.screening_details ?? {};
  const screened = job.screening_outcome !== null;
  const screenedForOther = screened && details.profile_id !== undefined && details.profile_id !== profile.id;
  const skillCount = skills.data?.length;

  return (
    <section className="panel" aria-labelledby="screening-title">
      <div className="panel__header">
        <div>
          <h2 id="screening-title" className="panel__title">
            Screening
          </h2>
          <p className="panel__subtitle">
            Screening checks {profile.name}'s skills
            {skillCount !== undefined ? ` (${skillCount})` : ""} against the description, then the experience requirement.
            It never changes your status.
          </p>
        </div>
        <Button
          variant={screened ? "secondary" : "primary"}
          loading={mutation.isPending}
          disabled={skillCount === 0}
          onClick={() => {
            if (!mutation.isPending) mutation.mutate();
          }}
        >
          {screened ? `Re-screen with ${profile.name}` : `Screen with ${profile.name}`}
        </Button>
      </div>

      {skillCount === 0 && (
        <p className="notice notice--warning">
          {profile.name} has no skills, so screening is not possible. <Link to="/profile">Add skills</Link>.
        </p>
      )}
      {screenedForOther && (
        <p className="notice" role="note">
          This result was produced for another profile. Re-screen to check it against {profile.name}.
        </p>
      )}
      <FormErrors error={mutation.error} />

      {screened ? (
        <ScreeningBreakdown job={job} details={details} />
      ) : (
        <p className="muted">Not screened yet.</p>
      )}
    </section>
  );
}

function StatusControl({ job }: { job: JobDetail }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const { notify } = useToast();
  const id = useId();
  const [value, setValue] = useState<JobStatus>(job.status);

  const mutation = useMutation({
    mutationFn: (status: JobStatus) => updateJobStatus(api, job.id, status),
    onSuccess: (updated) => {
      queryClient.setQueryData(jobKeys.detail(job.id), updated);
      invalidateAfterJobChange(queryClient);
      notify(`Status: ${JOB_STATUS[updated.status].label}`, "success");
    },
  });

  return (
    <div className="status-control">
      <label htmlFor={id} className="field__label">
        Your status
      </label>
      <div className="status-control__row">
        <select id={id} className="input input--compact" value={value} onChange={(e) => setValue(e.target.value as JobStatus)} disabled={mutation.isPending}>
          {JOB_STATUSES.map((status) => (
            <option key={status} value={status}>
              {JOB_STATUS[status].label}
            </option>
          ))}
        </select>
        <Button size="sm" loading={mutation.isPending} disabled={value === job.status} onClick={() => mutation.mutate(value)}>
          Save status
        </Button>
      </div>
      <p className="field__hint">{JOB_STATUS_HELP[value]} Applications can be started for Shortlisted or Applied jobs.</p>
      <FormErrors error={mutation.error} />
    </div>
  );
}

function Description({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  const long = text.length > DESCRIPTION_PREVIEW_CHARS;
  const shown = long && !expanded ? `${text.slice(0, DESCRIPTION_PREVIEW_CHARS).trimEnd()}…` : text;
  return (
    <section className="panel" aria-labelledby="description-title">
      <div className="panel__header">
        <h2 id="description-title" className="panel__title">
          Job description
        </h2>
        <span className="muted small">{text.length.toLocaleString()} characters</span>
      </div>
      {/* Plain text from the posting: never rendered as HTML. */}
      <div className="job-description">{shown}</div>
      {long && (
        <Button size="sm" onClick={() => setExpanded((v) => !v)} aria-expanded={expanded}>
          {expanded ? "Show less" : "Show the full description"}
        </Button>
      )}
    </section>
  );
}

function Contacts({ job }: { job: JobDetail }) {
  return (
    <section className="panel" aria-labelledby="contacts-title">
      <h2 id="contacts-title" className="panel__title">
        Contacts
      </h2>
      {job.contacts.length === 0 ? (
        <p className="muted">No contact details were found in this posting.</p>
      ) : (
        <ul className="contact-list">
          {job.contacts.map((contact) => (
            <li key={contact.id} className="contact-card">
              <strong>{contact.name || "Unnamed contact"}</strong>
              <dl className="contact-card__fields">
                {contact.email && (
                  <>
                    <dt>Email</dt>
                    <dd>{contact.email}</dd>
                  </>
                )}
                {contact.phone && (
                  <>
                    <dt>Phone</dt>
                    <dd>{contact.phone}</dd>
                  </>
                )}
                {contact.whatsapp && (
                  <>
                    <dt>WhatsApp</dt>
                    <dd>{contact.whatsapp}</dd>
                  </>
                )}
              </dl>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function JobView({ job, profile }: { job: JobDetail; profile: Profile }) {
  const sourceHref = safeHttpUrl(job.source_url);
  const experienceRange = formatRange(job.parsed_min_experience, job.parsed_max_experience);

  return (
    <>
      <section className="panel" aria-labelledby="overview-title">
        <div className="panel__header">
          <h2 id="overview-title" className="panel__title">
            Overview
          </h2>
          <span className="badges">
            <StatusBadge kind="screening" value={job.screening_outcome} />
            <StatusBadge kind="job" value={job.status} />
          </span>
        </div>
        <dl className="details">
          <Item label="Company">{job.company?.name ?? missing}</Item>
          {job.company && <CompanyWebsite companyId={job.company.id} />}
          <Item label="Location">{job.location || missing}</Item>
          <Item label="Work mode">{job.work_mode || missing}</Item>
          <Item label="Experience required">
            {job.required_experience_text || (experienceRange ? null : missing)}
            {experienceRange && (
              <span className={job.required_experience_text ? "muted" : undefined}>
                {job.required_experience_text ? ` (read as ${experienceRange})` : `${experienceRange} (from the description)`}
              </span>
            )}
          </Item>
          <Item label="Source">
            {JOB_SOURCE[job.source] ?? job.source}
            {sourceHref && (
              <>
                {" · "}
                <ExternalLink href={sourceHref}>Open the original posting</ExternalLink>
              </>
            )}
          </Item>
          <Item label="Posted">{job.posted_date ? formatDayUTC(job.posted_date) : missing}</Item>
          <Item label="Found by ApplyForge">{formatDateTime(job.discovered_date)}</Item>
        </dl>
        <StatusControl key={job.status} job={job} />
      </section>

      <ScreeningPanel job={job} profile={profile} />
      <StartApplication job={job} profile={profile} />
      <Contacts job={job} />
      <Description text={job.full_jd} />
    </>
  );
}

function JobDetailContent({ profile }: { profile: Profile }) {
  const api = useApi();
  const { id: rawId } = useParams();
  const location = useLocation();
  const id = /^[1-9]\d*$/.test(rawId ?? "") ? Number(rawId) : null;
  const backTo = `/jobs${(location.state as { from?: string } | null)?.from ?? ""}`;

  const job = useQuery({
    queryKey: jobKeys.detail(id ?? 0),
    queryFn: ({ signal }) => getJob(api, id!, signal),
    enabled: id !== null,
  });

  const back = (
    <Link className="back-link" to={backTo}>
      ← All jobs
    </Link>
  );

  if (id === null || (isApiError(job.error) && job.error.kind === "not_found")) {
    return (
      <>
        {back}
        <PageHeader title="Job not found" />
        <EmptyState title="There is no job with this id">It may have been removed, or the link is wrong.</EmptyState>
      </>
    );
  }
  if (job.error) {
    return (
      <>
        {back}
        <PageHeader title="Job" />
        <ErrorState title="Could not load this job" error={job.error} onRetry={() => void job.refetch()} />
      </>
    );
  }
  if (!job.data) {
    return (
      <>
        {back}
        <Skeleton lines={8} label="Loading job" />
      </>
    );
  }
  return (
    <>
      {back}
      <PageHeader
        title={job.data.title}
        description={[job.data.company?.name, job.data.location, job.data.work_mode].filter(Boolean).join(" · ") || undefined}
      />
      <JobView job={job.data} profile={profile} />
    </>
  );
}

export function JobDetailPage() {
  const { profile } = useActiveProfile();
  return <RequireProfile>{profile && <JobDetailContent profile={profile} />}</RequireProfile>;
}
