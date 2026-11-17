import type { ReactNode } from "react";

import { statusLabel, type StatusKind, type Tone } from "../lib/labels";

export function Badge({ tone = "neutral", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span className={`badge badge--${tone}`} title={title}>
      {children}
    </span>
  );
}

/** A backend status value shown with its shared label and colour (see lib/labels.ts). */
export function StatusBadge({ kind, value }: { kind: StatusKind; value: string | null | undefined }) {
  const { label, tone } = statusLabel(kind, value);
  return (
    <span className={`badge badge--${tone} badge--dot`} data-status={value ?? "none"}>
      {label}
    </span>
  );
}
