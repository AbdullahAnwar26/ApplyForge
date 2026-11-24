/** Shared pieces of the collector screens: result counts. */

import type { CollectorCounts } from "../../api/types";
import { COUNT_LABELS } from "./runText";

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
