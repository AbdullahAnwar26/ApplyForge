/** Shared pieces of the collector screens: result counts and per-post outcomes. */

import { Link } from "react-router-dom";

import type { CollectorCounts, CollectorPostOutcome } from "../../api/types";
import { Badge, StatusBadge } from "../../components/Badge";
import type { Tone } from "../../lib/labels";
import { COUNT_LABELS, shortError } from "./runText";

export function RunCounts({ counts }: { counts: Partial<CollectorCounts> }) {
  const recorded = COUNT_LABELS.some(({ key }) => typeof counts[key] === "number");
  if (!recorded) return <p className="muted">No posts were processed, so there are no counts.</p>;
  return (
    <dl className="run-counts">
      {COUNT_LABELS.map(({ key, label, help }) => (
        <div key={key} className={`run-counts__item run-counts__item--${key}`} title={help}>
          <dt>{label}</dt>
          <dd>{counts[key] ?? 0}</dd>
        </div>
      ))}
    </dl>
  );
}

const OUTCOME: Record<CollectorPostOutcome["status"], { label: string; tone: Tone }> = {
  created: { label: "New job", tone: "success" },
  existing: { label: "Already known", tone: "info" },
  skipped: { label: "Skipped", tone: "neutral" },
  invalid: { label: "Invalid", tone: "warning" },
  failed: { label: "Failed", tone: "danger" },
};

/**
 * Per-post results returned by POST collector/runs/. Only the job title, a link to
 * the saved job, the screening outcome and the reason are shown - not the post's
 * text, author or LinkedIn link.
 */
export function RunOutcomes({ outcomes }: { outcomes: CollectorPostOutcome[] }) {
  if (outcomes.length === 0) return null;
  return (
    <div className="run-outcomes">
      <h3 className="run-outcomes__title">Posts in this run</h3>
      <ol className="run-outcomes__list">
        {outcomes.map((outcome) => {
          const tone = OUTCOME[outcome.status] ?? { label: outcome.status, tone: "neutral" as Tone };
          return (
            <li key={outcome.index} className="run-outcome">
              <Badge tone={tone.tone}>{tone.label}</Badge>
              {outcome.job_id ? (
                <Link to={`/jobs/${outcome.job_id}`}>{outcome.title || `Job #${outcome.job_id}`}</Link>
              ) : (
                <span>{outcome.title || `Post ${outcome.index + 1}`}</span>
              )}
              {outcome.screening_outcome && <StatusBadge kind="screening" value={outcome.screening_outcome} />}
              {outcome.reason && <span className="muted small run-outcome__reason">{shortError(outcome.reason)}</span>}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
