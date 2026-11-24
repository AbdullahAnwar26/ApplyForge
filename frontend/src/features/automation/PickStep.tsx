/**
 * Step 2 - Pick jobs. The jobs found (by default: the latest collection) that match the
 * profile - at least one of the user's skills in the description (screening), a complete
 * description and a recruiter email or phone in the post - best matches first and all
 * ticked. "Write applications" shortlists the ticked jobs (one application per job,
 * with the matching resume) and writes the email (and, when the post has a number, the
 * WhatsApp message) for each with the local model, one after another.
 */

import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { applicationKeys, generateDrafts } from "../../api/applications";
import { isApiError } from "../../api/errors";
import { jobKeys, selectJobs } from "../../api/jobs";
import type { JobSummary, Profile } from "../../api/types";
import { useApi } from "../../auth/AuthProvider";
import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { EmptyState, ErrorState, errorMessage, Skeleton } from "../../components/States";
import { formatDate } from "../../lib/format";

const PAGE = 200; // the API's largest page
const MAX_LIMIT_WAITS = 5;

export type PickReason = "match" | "no_match" | "incomplete" | "no_contact" | "applied";

export function pickReason(job: JobSummary): PickReason {
  if (job.application) return "applied";
  if (!job.jd_complete) return "incomplete";
  const { emails, phones, whatsapp } = job.contact_summary;
  if (!emails.length && !phones.length && !whatsapp.length) return "no_contact";
  if (job.screening_outcome === "REJECTED") return "no_match";
  return "match";
}

const OTHER_LABEL: Record<Exclude<PickReason, "match">, string> = {
  no_match: "Does not match your skills or experience",
  incomplete: "Post was cut off",
  no_contact: "No email or phone in the post",
  applied: "Already applied",
};

function JobRow({ job, checked, onToggle, disabled }: { job: JobSummary; checked: boolean; onToggle: () => void; disabled?: boolean }) {
  const reason = pickReason(job);
  const { emails, phones, whatsapp } = job.contact_summary;
  const phone = whatsapp[0] ?? phones[0];
  return (
    <li className={`pick-row${reason === "match" ? "" : " pick-row--other"}`}>
      <label className="pick-row__check">
        <input type="checkbox" checked={checked} disabled={disabled || reason !== "match"} onChange={onToggle} aria-label={`Pick ${job.title}`} />
      </label>
      <div className="pick-row__main">
        <div className="pick-row__title">
          <Link to={`/jobs/${job.id}`}>{job.title}</Link>
          {reason !== "match" && <Badge tone="neutral">{OTHER_LABEL[reason]}</Badge>}
        </div>
        <span className="muted small">
          {[job.company?.name, job.location, job.posted_date ? `posted ${formatDate(job.posted_date)}` : null].filter(Boolean).join(" · ")}
        </span>
        {job.matched_keywords.length > 0 && (
          <span className="small">
            Matches your skills: <strong>{job.matched_keywords.join(", ")}</strong>
          </span>
        )}
        <span className="small pick-row__contacts">
          {emails[0] && <span>✉ {emails[0]}</span>}
          {phone && <span>WhatsApp {phone}</span>}
          {job.matching_resume && <span className="muted">Resume: {job.matching_resume.name}</span>}
          {job.source_url && (
            <a href={job.source_url} target="_blank" rel="noopener noreferrer">
              Open post ↗
            </a>
          )}
        </span>
      </div>
    </li>
  );
}

interface Progress {
  total: number;
  done: number;
  failed: { title: string; error: string }[];
  running: boolean;
  /** Waiting this many seconds for the server's per-minute limit. */
  waiting?: number;
}

export function PickStep({ profile, runId, onClearRun, onDone }: { profile: Profile; runId: number | null; onClearRun: () => void; onDone: () => void }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const filters = { status: "DISCOVERED" as const, ...(runId ? { run: runId } : {}) };
  const jobs = useQuery({
    queryKey: [...jobKeys.lists(), "pick", filters],
    queryFn: ({ signal }) => api.list<JobSummary>("jobs/", { query: { ...filters }, page: { page: 1, pageSize: PAGE }, signal }),
    placeholderData: keepPreviousData,
  });
  const [unticked, setUnticked] = useState<Set<number>>(new Set());
  const [showOthers, setShowOthers] = useState(false);
  const [progress, setProgress] = useState<Progress | null>(null);

  const { matching, others } = useMemo(() => {
    const items = jobs.data?.items ?? [];
    const match = items
      .filter((j) => pickReason(j) === "match")
      .sort((a, b) => b.matched_keywords.length - a.matched_keywords.length || (b.posted_date ?? "").localeCompare(a.posted_date ?? ""));
    return { matching: match, others: items.filter((j) => pickReason(j) !== "match") };
  }, [jobs.data]);
  const picked = matching.filter((j) => !unticked.has(j.id));
  const toggle = (id: number) =>
    setUnticked((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  /** The server limits how many drafts are written per minute: wait as it asks, then go on. */
  async function generateWaitingForLimit(id: number, onWait: (seconds: number) => void) {
    for (let attempt = 0; ; attempt += 1) {
      try {
        return await generateDrafts(api, id);
      } catch (error) {
        if (!isApiError(error) || error.status !== 429 || attempt >= MAX_LIMIT_WAITS) throw error;
        const seconds = Math.min(90, Math.max(1, error.retryAfterSeconds ?? 20));
        onWait(seconds);
        await new Promise((resolve) => setTimeout(resolve, seconds * 1000));
      }
    }
  }

  async function write() {
    const chosen = picked;
    setProgress({ total: chosen.length, done: 0, failed: [], running: true });
    try {
      const selected = await selectJobs(api, chosen.map((j) => j.id), profile.id);
      const titles = new Map(chosen.map((j) => [j.id, j.title]));
      const failed: Progress["failed"] = [];
      let done = 0;
      for (const row of selected.results) {
        if (row.application && (row.created || ["SHORTLISTED", "FAILED"].includes(row.status ?? ""))) {
          try {
            await generateWaitingForLimit(row.application, (seconds) =>
              setProgress({ total: chosen.length, done, failed: [...failed], running: true, waiting: seconds }),
            );
          } catch (error) {
            failed.push({ title: titles.get(row.job) ?? `Job ${row.job}`, error: errorMessage(error) });
          }
        } else if (!row.application) {
          failed.push({ title: titles.get(row.job) ?? `Job ${row.job}`, error: row.error ?? "Could not be selected." });
        }
        done += 1;
        setProgress({ total: chosen.length, done, failed: [...failed], running: true });
      }
      setProgress({ total: chosen.length, done, failed, running: false });
      void queryClient.invalidateQueries({ queryKey: jobKeys.all });
      void queryClient.invalidateQueries({ queryKey: applicationKeys.all });
      if (!failed.length) onDone();
    } catch (error) {
      setProgress((p) => ({ total: chosen.length, done: p?.done ?? 0, failed: [{ title: "Selection", error: errorMessage(error) }], running: false }));
    }
  }

  if (jobs.error && !jobs.data) return <ErrorState title="Could not load the jobs" error={jobs.error} onRetry={() => void jobs.refetch()} />;
  if (!jobs.data) return <Skeleton lines={6} label="Loading jobs" />;
  const running = progress?.running ?? false;

  return (
    <section className="panel" aria-labelledby="pick-title">
      <div>
        <h2 id="pick-title" className="panel__title">
          Pick jobs
        </h2>
        <p className="panel__subtitle">
          Jobs that match your skills and have the recruiter's email or phone, best matches first. Untick any you don't want, then
          write the applications.
        </p>
      </div>
      {runId && (
        <p className="small">
          From your latest search.{" "}
          <button type="button" className="link-button" onClick={onClearRun}>
            Show all new jobs instead
          </button>
        </p>
      )}

      {matching.length === 0 ? (
        <EmptyState title="No matching jobs here">
          Search again in step 1, or check that your profile lists your skills (jobs must mention at least one of them).
        </EmptyState>
      ) : (
        <ul className="pick-list" aria-label="Matching jobs">
          {matching.map((job) => (
            <JobRow key={job.id} job={job} checked={!unticked.has(job.id)} onToggle={() => toggle(job.id)} disabled={running} />
          ))}
        </ul>
      )}

      {others.length > 0 && (
        <div>
          <button type="button" className="link-button small" onClick={() => setShowOthers((v) => !v)} aria-expanded={showOthers}>
            {showOthers ? "Hide" : "Show"} {others.length} other post{others.length === 1 ? "" : "s"} (no match, cut off or no contact)
          </button>
          {showOthers && (
            <ul className="pick-list" aria-label="Other jobs">
              {others.map((job) => (
                <JobRow key={job.id} job={job} checked={false} onToggle={() => undefined} />
              ))}
            </ul>
          )}
        </div>
      )}

      {progress && (
        <div className={`notice ${progress.failed.length ? "notice--warning" : "notice--info"}`} role="status">
          {progress.running
            ? `Writing applications… ${progress.done} of ${progress.total}${progress.waiting ? ` (waiting ${progress.waiting} s for the server's per-minute limit)` : ""}`
            : `Wrote ${progress.total - progress.failed.length} of ${progress.total} applications.`}
          {progress.failed.length > 0 && (
            <ul className="small">
              {progress.failed.map((f) => (
                <li key={f.title}>
                  {f.title}: {f.error}
                </li>
              ))}
            </ul>
          )}
          {!progress.running && progress.failed.length > 0 && (
            <Button size="sm" onClick={onDone}>
              Go to Apply →
            </Button>
          )}
        </div>
      )}

      {matching.length > 0 && (
        <div className="form__actions pick-actions">
          <Button variant="primary" disabled={!picked.length || running} loading={running} onClick={() => void write()}>
            Write applications for {picked.length} job{picked.length === 1 ? "" : "s"}
          </Button>
        </div>
      )}
    </section>
  );
}
