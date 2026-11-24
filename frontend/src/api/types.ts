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
  "INTERVIEW",
  "OFFER",
  "SEND_FAILED",
  "COMPOSE_OPENED",
] as const;
export type ApplicationStatus = (typeof APPLICATION_STATUSES)[number];

export const APPLICATION_CHANNELS = ["EMAIL", "WHATSAPP", "OTHER"] as const;
export type ApplicationChannel = (typeof APPLICATION_CHANNELS)[number];

export const COLLECTOR_RUN_STATUSES = ["RUNNING", "SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED", "INTERRUPTED"] as const;
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
  /** Digits only, e.g. "91": completes WhatsApp numbers written without a country code. */
  phone_country_code?: string;
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
  // Review fields (Job Automation).
  recruiter_name: string;
  /** false: the post was cut off; the full description must be pasted before generating. */
  jd_complete: boolean;
  contacts_verified_at: string | null;
  contact_summary: { emails: string[]; phones: string[]; whatsapp: string[] };
  application: { id: number; status: ApplicationStatus; sent_at: string | null } | null;
  eligibility: { eligible: boolean; issues: EligibilityIssue[] };
  matching_resume: { id: number; name: string; reason: string } | null;
}

export type EligibilityCode =
  | "already_applied"
  | "in_applications"
  | "incomplete_jd"
  | "no_contact"
  | "no_resume"
  | "screened_out"
  | "possible_duplicate"
  | "skipped";

export interface EligibilityIssue {
  code: EligibilityCode;
  message: string;
  blocking: boolean;
}

export interface ContactEvidence {
  kind: "email" | "phone" | "whatsapp";
  value: string;
  /** The line of the post the value was found on. */
  evidence: string;
}

export interface JobExtraction {
  title_source?: string;
  truncated?: boolean;
  contacts?: ContactEvidence[];
  edited_fields?: string[];
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
  recruiter_headline: string;
  /** The post exactly as collected (never edited). */
  source_post_text: string;
  extraction: JobExtraction;
}

export interface JobContactEdit extends JobContactInput {
  id?: number;
}

/** PATCH jobs/{id}/ - review decision and corrections. */
export interface JobReviewUpdate {
  status?: JobStatus;
  title?: string;
  company_name?: string;
  location?: string;
  work_mode?: string;
  recruiter_name?: string;
  required_experience_text?: string;
  full_jd?: string;
  contacts?: JobContactEdit[];
  contacts_verified?: boolean;
}

export interface SelectJobResult {
  application: ApplicationDetail;
  created: boolean;
  job: JobDetail;
}

export interface SelectJobsResult {
  results: { job: number; application: number | null; created: boolean; status?: ApplicationStatus; error?: string }[];
  created: number;
  already_selected: number;
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
  /** Follow-up reminder day (YYYY-MM-DD); due on and after it. */
  follow_up_on: string | null;
  updated_at: string;
  /** Where sending stands per channel (backend tracker.sending.channel_states). */
  send_state?: Record<SendChannel, ChannelSendState>;
  /** The recruiter's email and WhatsApp/phone from the application's contact ("" = none). */
  recipient_email?: string;
  recipient_phone?: string;
  /** What "Send WhatsApp" opens: the international number, how its country code was chosen, or why it cannot. */
  whatsapp?: { number: string; note: string; problem: string };
}

export type SendChannel = "EMAIL" | "WHATSAPP";

export interface ChannelSendState {
  /** none: not started; open: opened for you to send; sent; failed: the last attempt failed. */
  state: "none" | "open" | "sent" | "failed";
  record: number | null;
  /** The record status; "MARKED" = marked as sent by hand. */
  status: SendRecordStatus | "MARKED" | "";
  provider: string;
  recipient: string;
  at: string | null;
  open_url: string;
  /** The draft is in your mailbox: ApplyForge can look for it in your Sent folder. */
  can_verify: boolean;
  error: string;
}

export interface ApplicationDetail extends ApplicationSummary {
  generated_body: string | null;
  generated_whatsapp_message: string | null;
  last_error: string | null;
  generation_notes: GenerationNotes;
}

export interface GenerationNotes {
  /** Job requirements the profile/resume do not support (not claimed in the draft). */
  gaps?: string[];
  /** Technologies the draft mentions that appear nowhere in the profile or resume. */
  unsupported_terms?: string[];
  model?: string;
  resume?: number;
  resume_name?: string;
  whatsapp?: boolean;
}

// --- Sending ----------------------------------------------------------------------

export interface SendPreview {
  channel: ApplicationChannel;
  recipient: string;
  /** "post": found in the original post; "edited": added or changed by you. */
  recipient_source: "post" | "edited" | "";
  sender: string;
  subject: string;
  body: string;
  attachment: { resume_id: number | null; name: string; size: number | null; ok: boolean; problem: string };
  whatsapp_link: string;
  problems: string[];
  notes: string[];
  ready: boolean;
  version: string;
}

export interface SendRecord {
  id: number;
  channel: ApplicationChannel;
  provider: string;
  status: SendRecordStatus;
  recipient: string;
  sender: string;
  subject: string;
  body: string;
  resume: number | null;
  attachment_name: string;
  attachment_size: number | null;
  message_id: string;
  error: string;
  open_url: string;
  details: { method?: string; folder?: string; mailbox?: string; evidence?: { match: string; subject: string; date: string } };
  created_at: string;
  finished_at: string | null;
}

export type SendRecordStatus =
  | "SENDING" | "SENT" | "FAILED" | "HANDED_OFF" | "COMPOSE_OPENED" | "CONFIRMED" | "VERIFIED" | "ABANDONED";

/** mailbox: a draft in your own mailbox; eml: a draft file for a desktop email app; link: WhatsApp. */
export type HandoffMethod = "mailbox" | "eml" | "link";

export interface SendResult {
  record: SendRecord;
  application: ApplicationDetail;
  detail?: string;
  /** compose (eml): where to download the draft file, relative to the API. */
  download_url?: string;
  /** compose/verify: whether the email was found in your Sent folder. */
  verified?: boolean;
}

export type SmtpSecurity = "starttls" | "ssl";

export interface SendingAccount {
  from_name: string;
  from_email: string;
  smtp_host: string;
  smtp_port: number;
  security: SmtpSecurity;
  username: string;
  imap_host: string;
  imap_port: number;
  /** The IMAP server used for drafts (entered, or derived from the SMTP server). */
  drafts_server: string;
  /** Your provider's web page listing drafts ("" = unknown, or this server's test mailbox). */
  drafts_page: string;
  has_password: boolean;
  verified_at: string | null;
  last_error: string;
  updated_at: string;
}

export interface SendingAccountInput {
  from_name: string;
  from_email: string;
  smtp_host: string;
  smtp_port: number;
  security: SmtpSecurity;
  username: string;
  imap_host?: string;
  imap_port?: number;
  password?: string;
}

export interface SendingAccountState {
  account: SendingAccount | null;
  /** "smtp" = real delivery; anything else ("console", "filebased", "locmem") = nothing leaves the server. */
  mode: string;
  /** Where "Send email" saves drafts: "imap" = your real mailbox, "local" = a test folder on the server. */
  drafts_mode?: "imap" | "local";
  /** Account test only: whether drafts can be saved in the mailbox (IMAP). */
  drafts?: { ok: boolean; folders: { drafts?: string; sent?: string }; error: string };
}

export interface ApplicationEvent {
  id: number;
  /** STATUS = a status change; NOTE = a note added without changing the status. */
  kind: "STATUS" | "NOTE";
  status: ApplicationStatus;
  notes: string | null;
  timestamp: string;
}

export interface ApplicationsOverview {
  total: number;
  by_status: Record<ApplicationStatus, number>;
  awaiting_action: { count: number; statuses: ApplicationStatus[]; oldest: ApplicationSummary[] };
  /** Applications ever marked sent (stays counted after later statuses). */
  sent_total: number;
  follow_ups_due: { count: number; items: ApplicationSummary[] };
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
  out_of_range: number;
  /** New jobs you can apply to now (full description, a contact, matching): the target counts these. */
  ready: number;
}

export const DATE_RANGES = ["any", "past-24h", "past-week", "past-month", "custom"] as const;
export type DateRange = (typeof DATE_RANGES)[number];

export type StopReason =
  | ""
  | "target_reached"
  | "exhausted"
  | "cancelled"
  | "time_limit"
  | "examined_limit"
  | "source_error"
  | "interrupted"
  | "failed";

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
  search_query: string;
  date_range: DateRange;
  date_from: string | null;
  date_to: string | null;
  cancel_requested: boolean;
  /** What the run is doing right now (or how it ended). */
  stage: string;
  stop_reason: StopReason;
  heartbeat_at: string | null;
  /** New jobs still missing to reach the target (limit). */
  remaining: number | null;
}

export type CollectedPostStatus = "created" | "existing" | "skipped" | "invalid" | "failed" | "out_of_range";

export interface CollectedPost {
  id: number;
  position: number;
  status: CollectedPostStatus;
  post_url: string;
  title: string;
  job: number | null;
  matched_by: string;
  reason: string;
  posted_at: string | null;
  truncated: boolean;
  created_at: string;
}

/** POST collector/runs/ */
export interface CollectionInput {
  query: string;
  target: number;
  date_range: DateRange;
  date_from?: string | null;
  date_to?: string | null;
  profile?: number;
}

