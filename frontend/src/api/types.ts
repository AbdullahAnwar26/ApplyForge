/**
 * Response and request shapes of the ApplyForge API, written from the backend
 * serializers (backend/tracker/api/serializers.py, backend/collector/api.py).
 * Dates are ISO 8601 strings. Keep in sync when the backend changes.
 */

// --- Enums (backend TextChoices) -------------------------------------------------

export const JOB_STATUSES = ["DISCOVERED", "SHORTLISTED", "SKIPPED", "APPLIED"] as const;
export type JobStatus = (typeof JOB_STATUSES)[number];

export const JOB_SOURCES = ["LINKEDIN", "NAUKRI", "INDEED", "COMPANY_WEBSITE", "MANUAL", "OTHER"] as const;
export type JobSource = (typeof JOB_SOURCES)[number];

export const SCREENING_OUTCOMES = ["SHORTLISTED", "REJECTED", "NEEDS_REVIEW"] as const;
export type ScreeningOutcome = (typeof SCREENING_OUTCOMES)[number];
/** Filter-only value of `screening_outcome` for jobs never screened. */
export const UNSCREENED = "UNSCREENED" as const;

export const SCREENING_REASONS = [
  "KEYWORD_MATCH",
  "NO_KEYWORD_MATCH",
  "EXPERIENCE_ABOVE_LIMIT",
  "EXPERIENCE_UNKNOWN",
  "POSSIBLE_DUPLICATE",
] as const;
export type ScreeningReason = (typeof SCREENING_REASONS)[number];

export const SKILL_CATEGORIES = ["SKILL", "ROLE", "TECHNOLOGY", "FRAMEWORK", "OTHER"] as const;
export type SkillCategory = (typeof SKILL_CATEGORIES)[number];

export const APPLICATION_STATUSES = [
  "SHORTLISTED",
  "GENERATED",
  "OPENED",
  "READY_TO_SEND",
  "SENT",
  "SKIPPED",
  "REJECTED",
  "FAILED",
  "FOLLOW_UP",
] as const;
export type ApplicationStatus = (typeof APPLICATION_STATUSES)[number];

export const APPLICATION_CHANNELS = ["EMAIL", "WHATSAPP", "OTHER"] as const;
export type ApplicationChannel = (typeof APPLICATION_CHANNELS)[number];

export const COLLECTOR_RUN_STATUSES = ["RUNNING", "SUCCEEDED", "PARTIAL", "FAILED"] as const;
export type CollectorRunStatus = (typeof COLLECTOR_RUN_STATUSES)[number];

export const COLLECTOR_RUN_TRIGGERS = ["command", "api"] as const;
export type CollectorRunTrigger = (typeof COLLECTOR_RUN_TRIGGERS)[number];

// --- Profiles, skills, resumes ----------------------------------------------------

export interface Profile {
  id: number;
  name: string;
  professional_summary: string | null;
  target_roles: string;
  experience_info: string | null;
  preferred_locations: string | null;
  preferred_work_modes: string | null;
  experience_years: number | null;
  max_acceptable_experience_years: number | null;
  created_at: string;
  updated_at: string;
}

export type ProfileInput = Partial<Omit<Profile, "id" | "created_at" | "updated_at">> &
  Pick<Profile, "name" | "target_roles">;

export interface Skill {
  id: number;
  name: string;
  category: SkillCategory;
  profile: number;
}

export interface SkillCategoryOption {
  value: SkillCategory;
  label: string;
}

export interface Resume {
  id: number;
  profile: number;
  name: string;
  file_name: string | null;
  file_type: string | null;
  resume_type: string | null;
  is_active: boolean;
  is_default: boolean;
  created_at: string;
  updated_at: string;
}

export interface ResumeText {
  id: number;
  text: string;
  characters: number;
}

// --- Companies, contacts, jobs ------------------------------------------------------

export interface CompanySummary {
  id: number;
  name: string;
}

export interface Company extends CompanySummary {
  website: string | null;
  created_at: string;
  updated_at: string;
}

export interface Contact {
  id: number;
  name: string | null;
  email: string | null;
  phone: string | null;
  whatsapp: string | null;
  company: number | null;
  created_at: string;
  updated_at: string;
}

export interface JobSummary {
  id: number;
  title: string;
  company: CompanySummary | null;
  source: JobSource;
  source_url: string | null;
  location: string | null;
  work_mode: string | null;
  posted_date: string | null;
  discovered_date: string;
  required_experience_text: string | null;
  parsed_min_experience: number | null;
  parsed_max_experience: number | null;
  status: JobStatus;
  screening_outcome: ScreeningOutcome | null;
  screening_reason: ScreeningReason | null;
  screened_at: string | null;
  matched_keywords: string[];
  created_at: string;
  updated_at: string;
}

export interface ScreeningDetails {
  rules_version?: number;
  profile_id?: number;
  matched_keywords?: string[];
  experience?: {
    min?: number | null;
    max?: number | null;
    /** Where the requirement was found: the experience field, the description, or nowhere. */
    source?: "field" | "jd" | "none" | string;
    matches?: { text: string; min: number | null; max: number | null }[];
    max_acceptable_years?: number | null;
    profile_experience_years?: number | null;
    above_actual_experience?: boolean;
  };
  flags?: ScreeningReason[];
  duplicate_candidates?: { job_id: number; matched_on: string[] }[];
}

export interface JobContactInput {
  name?: string;
  email?: string;
  phone?: string;
  whatsapp?: string;
}

/** POST jobs/ingest/ (JobIngestSerializer). */
export interface JobIngestInput {
  title: string;
  full_jd: string;
  profile?: number;
  source?: JobSource;
  source_url?: string;
  company_name?: string;
  company_website?: string;
  contacts?: JobContactInput[];
  location?: string;
  work_mode?: string;
  posted_date?: string;
  required_experience_text?: string;
}

export interface JobIngestResult {
  created: boolean;
  /** null when created; "source_url" or "jd_fingerprint" for an existing match. */
  matched_by: "source_url" | "jd_fingerprint" | null;
  job: JobDetail;
}

export interface JobDetail extends JobSummary {
  full_jd: string;
  screening_details: ScreeningDetails;
  contacts: Contact[];
}

export interface ScreenAllResult {
  profile: number;
  screened: number;
  outcomes: Record<ScreeningOutcome, number>;
}

// --- Applications -------------------------------------------------------------------

export interface ApplicationJobSummary {
  id: number;
  title: string;
  company: CompanySummary | null;
  status: JobStatus;
  screening_outcome: ScreeningOutcome | null;
}

export interface ApplicationSummary {
  id: number;
  profile: number;
  job: ApplicationJobSummary;
  contact: number | null;
  resume: number | null;
  status: ApplicationStatus;
  channel: ApplicationChannel;
  generated_subject: string | null;
  queue_position: number | null;
  created_at: string;
  generated_at: string | null;
  opened_at: string | null;
  sent_at: string | null;
  updated_at: string;
}

export interface ApplicationDetail extends ApplicationSummary {
  generated_body: string | null;
  generated_whatsapp_message: string | null;
  last_error: string | null;
}

export interface ApplicationEvent {
  id: number;
  status: ApplicationStatus;
  notes: string | null;
  timestamp: string;
}

export interface ApplicationsOverview {
  total: number;
  by_status: Record<ApplicationStatus, number>;
  awaiting_action: { count: number; statuses: ApplicationStatus[]; oldest: ApplicationSummary[] };
  recent: ApplicationSummary[];
}

export interface GenerationResult {
  application: ApplicationDetail;
  model: string;
  truncated: { job_description: boolean; resume: boolean };
}

// --- Collector ------------------------------------------------------------------------

export interface CollectorCounts {
  found: number;
  created: number;
  existing: number;
  skipped: number;
  invalid: number;
  failed: number;
}

export interface CollectorRun {
  id: number;
  status: CollectorRunStatus;
  trigger: CollectorRunTrigger;
  source: string;
  profile: number | null;
  keywords: string[];
  limit: number | null;
  counts: Partial<CollectorCounts>;
  job_ids: number[];
  error: string;
  started_at: string;
  finished_at: string | null;
  duration_seconds: number | null;
}

export interface CollectorPostOutcome {
  index: number;
  status: "created" | "existing" | "skipped" | "invalid" | "failed";
  url: string | null;
  title: string | null;
  job_id: number | null;
  matched_by: string | null;
  screening_outcome: ScreeningOutcome | null;
  reason: string | null;
}

/** Body of 201 (SUCCEEDED) and 502 (PARTIAL/FAILED) responses to POST collector/runs/. */
export interface CollectorRunResult {
  run: CollectorRun;
  outcomes: CollectorPostOutcome[];
  detail?: string;
}

export interface CollectorRunInput {
  keywords?: string[];
  limit?: number;
  profile?: number;
}
