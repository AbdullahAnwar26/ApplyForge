/**
 * Display labels and colour tones for backend enum values. One place, so every
 * screen shows the same words and colours for the same status.
 */

import type {
  ApplicationChannel,
  ApplicationStatus,
  CollectorRunStatus,
  JobSource,
  JobStatus,
  ScreeningOutcome,
  ScreeningReason,
  SkillCategory,
} from "../api/types";

export type Tone = "neutral" | "info" | "success" | "warning" | "danger" | "accent";

export interface LabelTone {
  label: string;
  tone: Tone;
}

export const SCREENING_OUTCOME: Record<ScreeningOutcome | "UNSCREENED", LabelTone> = {
  SHORTLISTED: { label: "Shortlisted", tone: "success" },
  NEEDS_REVIEW: { label: "Needs review", tone: "warning" },
  REJECTED: { label: "Rejected", tone: "danger" },
  UNSCREENED: { label: "Unscreened", tone: "neutral" },
};

export const SCREENING_REASON: Record<ScreeningReason, string> = {
  KEYWORD_MATCH: "Keyword match",
  NO_KEYWORD_MATCH: "No keyword match",
  EXPERIENCE_ABOVE_LIMIT: "Experience above limit",
  EXPERIENCE_UNKNOWN: "Experience unknown",
  POSSIBLE_DUPLICATE: "Possible duplicate",
};

export const JOB_SOURCE: Record<JobSource, string> = {
  LINKEDIN: "LinkedIn",
  NAUKRI: "Naukri",
  INDEED: "Indeed",
  COMPANY_WEBSITE: "Company website",
  MANUAL: "Added manually",
  OTHER: "Other",
};

/** What each job status means to the user (the backend never changes it during screening). */
export const JOB_STATUS_HELP: Record<JobStatus, string> = {
  DISCOVERED: "Not reviewed yet.",
  SHORTLISTED: "You want to apply.",
  SKIPPED: "You decided not to apply.",
  APPLIED: "You have applied.",
};

export const JOB_STATUS: Record<JobStatus, LabelTone> = {
  DISCOVERED: { label: "Discovered", tone: "neutral" },
  SHORTLISTED: { label: "Shortlisted", tone: "success" },
  SKIPPED: { label: "Skipped", tone: "neutral" },
  APPLIED: { label: "Applied", tone: "info" },
};

export const APPLICATION_STATUS: Record<ApplicationStatus, LabelTone> = {
  SHORTLISTED: { label: "Shortlisted", tone: "neutral" },
  GENERATED: { label: "Draft ready", tone: "accent" },
  OPENED: { label: "Opened", tone: "accent" },
  READY_TO_SEND: { label: "Ready to send", tone: "warning" },
  SENT: { label: "Sent", tone: "info" },
  FOLLOW_UP: { label: "Follow-up", tone: "warning" },
  SKIPPED: { label: "Skipped", tone: "neutral" },
  REJECTED: { label: "Rejected", tone: "danger" },
  FAILED: { label: "Generation failed", tone: "danger" },
};

export const APPLICATION_CHANNEL: Record<ApplicationChannel, string> = {
  EMAIL: "Email",
  WHATSAPP: "WhatsApp",
  OTHER: "Other",
};

export const COLLECTOR_RUN_STATUS: Record<CollectorRunStatus, LabelTone> = {
  RUNNING: { label: "Running", tone: "accent" },
  SUCCEEDED: { label: "Succeeded", tone: "success" },
  PARTIAL: { label: "Partial", tone: "warning" },
  FAILED: { label: "Failed", tone: "danger" },
};

export const SKILL_CATEGORY: Record<SkillCategory, string> = {
  SKILL: "Skill",
  ROLE: "Role",
  TECHNOLOGY: "Technology",
  FRAMEWORK: "Framework",
  OTHER: "Other",
};

export type StatusKind = "screening" | "job" | "application" | "collector";

/** Label and tone for a status value of the given kind; unknown values stay visible as-is. */
export function statusLabel(kind: StatusKind, value: string | null | undefined): LabelTone {
  const tables: Record<StatusKind, Record<string, LabelTone>> = {
    screening: SCREENING_OUTCOME,
    job: JOB_STATUS,
    application: APPLICATION_STATUS,
    collector: COLLECTOR_RUN_STATUS,
  };
  if (kind === "screening" && !value) return SCREENING_OUTCOME.UNSCREENED;
  if (!value) return { label: "—", tone: "neutral" };
  return tables[kind][value] ?? { label: value, tone: "neutral" };
}
