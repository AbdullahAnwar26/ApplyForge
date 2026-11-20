# ApplyForge — Project Specification

This is the tracked source of truth for ApplyForge's purpose, business rules,
architecture and rebuild plan. It replaces reliance on the gitignored
`PROJECT_CONTEXT.md`, which was lost along with the original development machine.

Every statement below is labelled as one of:

- **[Verified]** — present and checked in this checkout (`C:\Users\Abdul\OneDrive\Desktop\ApplyForge`,
  based on commit `96b205b` plus Milestone 1 changes).
- **[Recovered requirement]** — a requirement recovered from historical project
  context and the product owner. It is binding, but **not implemented** unless
  also marked [Verified].
- **[Proposed]** — a design intention for a future milestone. It may change.

---

## 1. Purpose

ApplyForge is a single-user, personal job-application assistant. It:

1. Discovers job postings (initially LinkedIn).
2. Extracts job and recruiter details (title, company, description, experience,
   contacts).
3. Screens each opportunity against the user's configured profile.
4. Prepares a personalised application for selected jobs using a local LLM.
5. Tracks each application until the user sends it manually.

**The user stays in control.** ApplyForge prepares; the user reviews and sends.

### Intended workflow [Recovered requirement]

```
Collect jobs ─► Ingest (dedupe) ─► Screen ─► Review shortlist
      ─► Generate application from the exact JD ─► User reviews/edits
      ─► User sends manually ─► Track status / follow-ups
```

---

## 2. Locked business rules [Recovered requirement]

These rules are fixed. Later milestones must not contradict them.

1. **Relevance:** a job is relevant when at least one configured profile
   skill or keyword matches its description. Experience eligibility is
   evaluated separately.
2. **Screening outcomes:** `shortlisted`, `rejected`, `needs_review`.
3. **Unknown experience:** if the experience requirement is unknown or cannot be
   parsed, the job must not be silently rejected. Use `needs_review` where
   appropriate.
4. **Screening order:** (1) keyword relevance, (2) experience eligibility,
   (3) duplicate detection. Profile-to-JD analysis may add supporting evidence,
   but a mandatory-skill gap must not automatically override a relevant
   keyword match.
5. **Idempotent ingestion:** re-ingesting the same job must not create a
   duplicate. Match in this order: exact source URL, then a stable
   job-description fingerprint, then normalised title/company/contact
   evidence as an extra duplicate check.
6. **Exact JD drives generation:** the description of the selected job is
   the input to personalised generation.
7. **Grounded content:** application content comes only from the actual
   profile and resume. Never invent qualifications, skills, experience or
   achievements.
8. **Single generation provider:** local Ollama/Qwen only, with no fallback
   provider. If generation fails, return a clear error and create **no**
   `Application` record.
9. **Manual sending:** every application is reviewed and sent by the user.
   ApplyForge never sends email (or any message) automatically.
10. **Historical Qwen configuration:** model `qwen3:1.7b`, base URL
    `http://localhost:11434/v1` (OpenAI-compatible chat completions),
    `temperature=0.3`, `max_tokens=768`, `think=False`, timeout 180 s.
11. **Stack:** Django + DRF, PostgreSQL, React + Vite, and Celery/Redis where
    justified.
12. **Integrated collector:** the LinkedIn collector lives inside this Django
    project. There is no separate collector server, and the collector makes
    no HTTP calls back to this same backend. It calls services in-process.
13. **Data preservation:** preserve existing models, migration history,
    uniqueness constraints and user data. Never delete or rewrite
    migrations to make checks pass.

---

## 3. Current state of this checkout [Verified]

### Repository and environment

- Git: `main` tracking `origin/main`
  (`https://github.com/AbdullahAnwar26/ApplyForge.git`).
- Python 3.12.10 virtual environment at the repository-root `.venv/` (ignored
  by `.gitignore`).
- Dependencies are pinned in `backend/requirements.txt`: Django 5.2.19 LTS,
  DRF 3.18.3, Celery 5.3.6, redis 5.0.3, gunicorn 27.0.1, psycopg2-binary,
  dj-database-url, django-cors-headers, python-dotenv, pypdf, python-docx,
  playwright, and others (upgraded in Milestone 8.2).

### Backend (`backend/`)

| Area | State |
|---|---|
| `config/settings.py` | Env-driven settings. SQLite by default; PostgreSQL via `DATABASE_URL`. CORS for Vite dev ports. `API_TOKEN`. Celery/Redis. Logging. Production hardening when `DEBUG=False`. |
| `config/celery.py` | Celery app with autodiscovery. No tasks exist yet. |
| `config/urls.py` | `admin/`, `api/v1/health/` (Milestone 8.3, unauthenticated), `api/v1/` → `tracker.api.urls`, and (Phase 6c) `api/v1/collector/` → `collector.urls`. |
| `tracker/models/` | `Profile`, `Skill`, `SkillCategory`, `Resume`, `Company`, `Contact`, `JobOpportunity`, `JobSource`, `JobStatus`, `ScreeningOutcome`, `ScreeningReason`, `Application`, `ApplicationStatus`, `ApplicationChannel`, `ApplicationEvent`. |
| `tracker/migrations/` | `0001`–`0008`. **Milestone 3** added `0008_jobopportunity_screening` (four additive fields; see below). The models and migrations are consistent (`makemigrations --check` reports no changes). |
| `tracker/admin.py` | Admin registrations for the models. |
| `tracker/api/` | **Milestone 1:** `auth.py`, `pagination.py`, `urls.py`. **Milestone 2:** `serializers.py`, `views.py`; routes for profiles, skills, skill categories and resumes (see §4). |
| `tracker/resume_text.py` | **Milestone 2:** PDF/DOCX text extraction (`pypdf`, `python-docx`). |
| `tracker/screening.py` | **Milestone 3:** profile selection, keyword matching, experience parsing, duplicate candidates, `screen_job`. |
| `tracker/ingestion.py` | **Milestone 3:** URL normalization, JD fingerprint, company/contact reuse, idempotent `ingest_job`. Called in-process by the collector. **Phase 6a** added LinkedIn post-URL normalization. |
| `tracker/applications.py` | **Milestone 4:** application creation, status-change rules and event recording. Reusable by generation (Milestone 5). |
| `tracker/generation.py` | **Milestone 5:** local Ollama/Qwen client (stdlib `urllib`, loopback URLs only), grounded prompt, output parsing and validation, `generate_for_application`. |
| `tracker/tests/` | `test_api_foundation.py` (M1), `test_profile_skill_api.py` and `test_resume_api.py` (M2), `test_screening.py` and `test_jobs_api.py` (M3), `test_applications_api.py` (M4), `test_generation.py` (M5), `test_linkedin_post_urls.py` (6a), `helpers.py`. 227 tests. |
| `collector/` | **Milestone 6.** Phase 6a: registered app, `posts.py` (post parsing), `sources.py` (source interface, offline JSON-file and static sources), `service.py` (`run_collection`), `collect_linkedin` command. Phase 6b: `linkedin/` (`selectors.py`, `extract.py`, `diagnostics.py`, `browser.py`, `source.py`) and the `linkedin_login` command. `tests/` (synthetic fixtures; real Chromium only with offline request interception). Phase 6c: `models.py` (`CollectorRun`), `migrations/0001_initial.py`, `runs.py` (`record_collection`), `api.py` + `urls.py` (`api/v1/collector/runs/`), `admin.py`. |

Existing data-integrity constraints:

- `JobOpportunity`: conditional unique constraints on non-empty `source_url`
  (`unique_job_source_url`) and non-empty `jd_fingerprint`
  (`unique_job_jd_fingerprint`).
- `Application`: `unique_together = (profile, job)`.
- `Skill`: `unique_together = (name, profile)`.

### Not present in this checkout

- **Verified live LinkedIn collection.** The Phase 6b browser source is
  implemented and tested offline only. Its selectors have not yet been checked
  against the real LinkedIn site.
- Batch generation (Celery). Profile, skill and resume endpoints exist as of
  Milestone 2; jobs, ingestion, screening, companies and contacts as of
  Milestone 3; application tracking as of Milestone 4; single-application
  generation as of Milestone 5.
- A live Ollama installation. Ollama was not installed on the development
  machine during Milestone 5, so generation is verified with a simulated
  Ollama only.
- `PROJECT_CONTEXT.md`. It is gitignored and was lost.
- A deployed server. Milestone 8.4c added the deployment configuration and
  runbook (`deploy/`), but nothing has been provisioned or deployed.
- A real backup or restore. Milestone 8.4d added the backup tool and its tests
  (fictional data and stand-in tools only); `age` is not installed on the
  development machine, and no PostgreSQL restore rehearsal has been run.

The React + Vite frontend (`frontend/`) was added in Milestone 7.

### Screening fields (migration 0008, Milestone 3)

| Field | Definition | Meaning |
|---|---|---|
| `screening_outcome` | `CharField(20)`, choices `SHORTLISTED`/`REJECTED`/`NEEDS_REVIEW`, nullable, indexed | Automatic verdict. `NULL` means the job hasn't been screened. |
| `screening_reason` | `CharField(40)`, choices `KEYWORD_MATCH`/`NO_KEYWORD_MATCH`/`EXPERIENCE_ABOVE_LIMIT`/`EXPERIENCE_UNKNOWN`/`POSSIBLE_DUPLICATE`, nullable | The primary (first triggered) reason. |
| `screening_details` | `JSONField(default=dict)` | `rules_version`, `profile_id`, `matched_keywords`, `experience` (`min`, `max`, `source`, `matches`, `max_acceptable_years`, `profile_experience_years`, `above_actual_experience`), `flags` (every triggered reason, in order), `duplicate_candidates` (`[{job_id, matched_on}]`). |
| `screened_at` | `DateTimeField`, nullable | Time of the last screening. |

Existing rows are unaffected: they keep `NULL` and `{}` until screened.
`JobStatus` is unchanged and is never modified by screening.

### Known model gaps against the rules (not yet resolved)

These gaps are recorded only. Milestones 1 and 2 did not change any models;
Milestone 3 only added the fields above.

- `SkillCategory` is a fixed `TextChoices` enum, not a table. Decision
  (Milestone 2): categories are read-only through the API, and adding one is a
  code change.
- `Resume` has no extracted-text field. Decision (Milestone 2): text is
  extracted on upload for validation and re-extracted on demand. It is not
  persisted.
- `Skill` uniqueness `(name, profile)` is case-sensitive, so `Python` and
  `python` can coexist.
- `Company.normalized_name` has no unique constraint. Ingestion reuses the
  oldest matching company, but two concurrent ingests could still create two
  companies with the same name. Contacts have the same limitation.
- Jobs are not linked to a profile. The profile used for screening is
  recorded in `screening_details.profile_id` only.

---

## 4. API

### Conventions [Verified — Milestone 1]

- **Base path:** `/api/v1/`.
- **Authentication:** `tracker.api.auth.StaticTokenAuthentication`, the
  project-wide default, together with `IsAuthenticated`.
  - Header: `Authorization: Bearer <API_TOKEN>`. The scheme name is
    case-insensitive.
  - A missing header gives `401` with
    `WWW-Authenticate: Bearer realm="api"`.
  - A wrong scheme, a malformed header or a wrong token gives `401`.
  - If `API_TOKEN` is unset, `None`, empty or whitespace-only, every request
    is rejected (fail closed).
  - Tokens are compared in constant time. Authentication does no database
    access and creates no users. A valid token yields an in-memory
    `StaticTokenUser` principal.
  - Note: `settings.py` defaults `API_TOKEN` to `local-dev-token` when the
    variable is unset and `DEBUG=True`. With `DEBUG=False`, startup refuses
    that default. Local `.env` files should set a real token.
    `.env.example` does not yet list `API_TOKEN`.
- **Pagination:** `tracker.api.pagination.OptionalPagination`, the
  project-wide default.
  - Without `page` and without `page_size`, the response is a plain JSON
    array of all results.
  - With either parameter, the response is a standard DRF page:
    `{count, next, previous, results}`.
  - Default page size is `REST_FRAMEWORK["PAGE_SIZE"]` (50). The maximum is 200;
    larger requests are capped.
  - An invalid or non-positive `page_size` falls back to 50. An invalid or
    out-of-range `page` returns `404`.
- **Throttling** [Verified settings]: `ScopedRateThrottle` with these scopes:
  - `application-generation` (20/min)
  - `application-batch-generation` (5/min)
  - `collector-run` (6/min)

  These scopes take effect once views declare them.

### Implemented endpoints [Verified — Milestone 2]

All endpoints below require the bearer token. List endpoints use
`OptionalPagination`.

| Path (under `/api/v1/`) | Methods | Notes |
|---|---|---|
| `profiles/`, `profiles/{id}/` | GET, POST, PUT, PATCH, DELETE | `name` and `target_roles` are required. `experience_years` must be ≥ 0. `max_acceptable_experience_years` must be an integer ≥ 0. DELETE returns `409` if the profile has applications; otherwise it cascades to skills and resumes and removes their stored files. |
| `skills/`, `skills/{id}/` | GET, POST, PUT, PATCH, DELETE | Fields: `name`, `category` (default `SKILL`), `profile`. A duplicate `(name, profile)` returns `400`. Filters: `?profile=<id>`, `?category=<value>`. Invalid filters return `400`. |
| `skill-categories/` | GET only | Returns the fixed `SkillCategory` choices as `[{value, label}]`. Not paginated. |
| `resumes/`, `resumes/{id}/` | GET, POST, PUT, PATCH, DELETE | Uploads are multipart with field `file`, required on create. `name` defaults to the file name. Other fields: `resume_type`, `is_active` (default true), `is_default` (one default per profile, enforced by the model). Responses expose `file_name` and `file_type`, but not the storage path or media URL. Replacing or deleting a file removes the old file after commit. Filters: `?profile=<id>`, `?is_active=true\|false`. |
| `resumes/{id}/text/` | GET | Re-extracts text from the stored file: `{id, text, characters}`. Returns `409` if the resume has no file or the file is missing, and `422` if the file can no longer be read. |

**Resume file rules:**
- Accepted types are `.pdf` and `.docx`; the extension is checked case-insensitively.
- The content must match the extension: a `%PDF-` header for PDFs, and a zip
  containing `word/document.xml` for DOCX.
- The maximum size is 10 MB. A DOCX whose uncompressed content exceeds 50 MB is
  rejected (zip-bomb guard).
- Password-protected PDFs are rejected.
- The file must yield non-empty text. Scanned, image-only resumes are
  rejected; there is no OCR.
- A rejected upload returns `400` with a message under `file`, and nothing is
  stored.

### Implemented endpoints [Verified — Milestone 3]

| Path (under `/api/v1/`) | Methods | Notes |
|---|---|---|
| `jobs/ingest/` | POST | Required: `title`, `full_jd`. Optional: `profile`, `source`, `source_url` (http/https), `company_name`, `company_website`, `contacts` (`[{name, email, phone, whatsapp}]`, each with at least one identifier; phones need ≥ 7 digits), `location`, `work_mode`, `posted_date`, `required_experience_text`. Returns `{created, matched_by, job}`: `201` when created and screened; `200` with `matched_by` = `source_url` or `jd_fingerprint` for an exact match, returned unchanged. |
| `jobs/` | GET | Summary (no `full_jd` or `screening_details`). Filters: `status`, `screening_outcome` (or `UNSCREENED`), `screening_reason`, `source`, `company`, `profile` (screened against). Invalid filter values return `400`. Ordered newest first. |
| `jobs/{id}/` | GET, PATCH | Detail adds `full_jd`, `screening_details` and `contacts`. PATCH accepts **only** `status`; any other field returns `400`. No PUT or DELETE. |
| `jobs/{id}/screen/` | POST | Re-screens one job. Optional `profile`. Never changes `status`. |
| `jobs/screen/` | POST | Re-screens all jobs, or `jobs: [ids]` (unknown IDs return `400`). Optional `profile`. Returns `{profile, screened, outcomes: {SHORTLISTED, REJECTED, NEEDS_REVIEW}}`. Runs synchronously. |
| `companies/`, `companies/{id}/` | GET | Read-only: `id, name, website, created_at, updated_at`. |
| `contacts/`, `contacts/{id}/` | GET | Read-only: `id, name, email, phone, whatsapp, company, created_at, updated_at`. Filter: `?company=<id>`. |

**Profile selection (ingest and screen):** an explicit `profile` is used as
given. If it's omitted, the only profile is used; zero profiles or several
profiles return `400` under `profile`. A profile with no skills returns `400`.

**Ingestion:**
- **Source URL.** The URL is normalized: lower-case scheme and host, no
  fragment, default ports or tracking parameters, and sorted query. LinkedIn
  links become `https://www.linkedin.com/jobs/view/<id>/`. Existing jobs are
  matched on either the normalized or the raw URL.
- **JD fingerprint.** If the URL doesn't match, the job is matched on the
  fingerprint: SHA-256 of the lower-cased, whitespace-collapsed description.
- **Exact match.** The existing job is returned without changes.
- **Companies and contacts.** Companies are reused by normalized name.
  Contacts are reused by normalized email, then phone digits, then WhatsApp
  digits. Existing companies and contacts are never overwritten.
- **Race conditions.** If a unique-constraint error occurs during creation, the
  whole creation is rolled back and the existing job is returned.

**Screening rules (rules version 1):**
- **Keywords.**
  - Matching is case-insensitive and aware of symbols: `C` doesn't match inside
    `C++`/`C#`, `Java` doesn't match inside `JavaScript`, and `.NET` doesn't
    match inside `ASP.NET`.
  - `React` matches `React.js`, and multi-word skills match across any
    whitespace.
  - All of the profile's skill categories are used, and matching runs on
    `full_jd`. One match is enough.
- **Experience source.** `required_experience_text` is used first, then the JD.
  - In the JD, a "N years" mention only counts when experience wording is in
    the same clause.
  - When there are several requirements, the highest minimum applies.
  - An upper bound alone ("up to 3 years") leaves the minimum unknown.
  - "Fresher" or "entry level" with no number means a minimum of 0.
  - Months, word numbers and values above 40 aren't parsed.
- **Experience thresholds.** A minimum above `max_acceptable_experience_years`
  is rejected. A minimum above `experience_years` but within the ceiling is
  allowed and recorded as `above_actual_experience`.
- **Duplicates.** A job is a possible duplicate when another job has the same
  normalized title plus the same normalized company or a shared contact.
  These are listed as candidates and never rejected.
- **Precedence.** `NO_KEYWORD_MATCH` (stops) → `EXPERIENCE_ABOVE_LIMIT` (stops)
  → `EXPERIENCE_UNKNOWN` → `POSSIBLE_DUPLICATE` → `KEYWORD_MATCH`.
- **Re-screening.** It re-parses experience and replaces the outcome, reason,
  details, `relevant_keywords` and `screened_at`.

### Implemented endpoints [Verified — Milestone 4]

No schema change was needed. All endpoints require the token. ApplyForge
never sends email or WhatsApp messages.

| Path (under `/api/v1/`) | Methods | Notes |
|---|---|---|
| `applications/` | GET, POST | **POST** body: `job` (required), plus optional `profile` (only-profile rule as for screening), `contact`, `resume` (defaults to the profile's active default resume), `channel` (default `EMAIL`) and `note`. Creates a `SHORTLISTED` application plus its first event. A duplicate `(profile, job)` returns `409` with the existing `application` id. **GET** filters: `profile`, `job`, `company`, `status` (comma-separated), `channel`, `awaiting_action=true\|false`. Ordered by `queue_position` (nulls last), then newest. |
| `applications/{id}/` | GET, PATCH | PATCH accepts only `status`, `channel`, `contact`, `resume`, `note` (the note is used for the status event) and, from Milestone 5, the drafts `generated_subject`, `generated_body` and `generated_whatsapp_message`. Drafts can only be edited before the application is sent, and editing them writes no event. Timestamps, `queue_position`, `last_error`, `job` and `profile` are read-only. No PUT or DELETE. |
| `applications/{id}/events/` | GET | Status history, newest first (`-timestamp`, then `-id`). Paginated on request. |
| `applications/summary/` | GET | `{total, by_status (all 9 statuses), awaiting_action: {count, statuses, oldest (10)}, recent (10 newest)}`. Optional `?profile=`. |

**Three separate layers:**
- `JobOpportunity.screening_outcome` is the automatic verdict.
- `JobOpportunity.status` is the user's decision about the job. An
  application can only be created for a job the user has set to `SHORTLISTED`
  or `APPLIED`, whatever the screening outcome.
- `Application.status` is progress after the user chooses to apply.

Application changes never touch the job's screening fields. The one effect on
the job's status (user decision, Milestone 5): an explicit change of an
application to `SENT` sets the job's status to `APPLIED`. It is never reversed
automatically, and an unchanged `SENT` does nothing.

**Validation:**
- The contact must be linked to the job.
- The resume must belong to the profile and be active.
- With `EMAIL`, the contact needs an email address; with `WHATSAPP`, it needs a
  WhatsApp or phone number.

**Status rules** (existing `ApplicationStatus` values only):
- `GENERATED` and `FAILED` are reserved for content generation (Milestone 5);
  PATCH rejects them.
- `OPENED` sets `opened_at`, and `SENT` sets `sent_at`; both are set the first
  time only. `SENT` means the user reports a manual send.
- `FOLLOW_UP` and `REJECTED` (employer rejection) require that the application
  was sent.
- Once sent, an application cannot return to `SHORTLISTED`, `OPENED`,
  `READY_TO_SEND` or `SKIPPED`.
- `SKIPPED` can be reopened to `SHORTLISTED`.
- "Awaiting user action" means `SHORTLISTED`, `GENERATED`, `OPENED`,
  `READY_TO_SEND`, `FAILED` or `FOLLOW_UP`.

**Events:**
- Creating an application writes one event: `SHORTLISTED`, with the given note
  or "Application created."
- Each actual status change writes one event, with the note or "Status changed
  from X to Y."
- An unchanged status, or a change to the channel, contact or resume only,
  writes no event.

### Implemented endpoint [Verified — Milestone 5]

| Path (under `/api/v1/`) | Methods | Notes |
|---|---|---|
| `applications/{id}/generate/` | POST (no body) | Generates drafts for an **existing** application with local Ollama/Qwen. Throttle scope `application-generation`. Returns `200` with `{application, model, truncated: {job_description, resume}}`. Never sends anything. |

No schema change was needed. `generated_*`, `generated_at` and `last_error`
already existed.

**Configuration** (`settings.py`, overridable from the environment):
- The defaults are the historical values: `OLLAMA_BASE_URL=http://localhost:11434/v1`,
  `OLLAMA_MODEL=qwen3:1.7b`, `OLLAMA_TEMPERATURE=0.3`, `OLLAMA_MAX_TOKENS=768`
  and `OLLAMA_TIMEOUT_SECONDS=180`. `think` is always `false`.
- The base URL must be a loopback address. Any other host is refused, and there
  is no fallback provider.
- `GENERATION_MAX_JD_CHARS` and `GENERATION_MAX_RESUME_CHARS` (6000 each) cap
  the prompt inputs. Longer text is cut with a `[...truncated]` marker and
  reported in the response, rather than silently cut by Ollama's context window.

**Prompt (rules 6 and 7):**
- The user message contains the profile (name, summary, target roles,
  experience, skills), the resume text and job metadata, plus the job's exact
  `full_jd` inside `<job_description>` and the recipient name and channel.
- The resume is the application's own, or the profile's active default; the
  default is then recorded on the application. Its text is extracted on demand.
- The system prompt forbids inventing anything, tells the model to treat tagged
  text as data, bans placeholders, and asks for one JSON object with `subject`,
  `body` and `whatsapp_message`.

**Output validation:**
- `<think>` blocks, code fences and surrounding prose are tolerated.
- The JSON must contain a non-empty subject and body.
- `whatsapp_message` falls back to the body if it's missing.
- Placeholders such as `[Your Name]` or `[Company]` cause a failure.
- The subject is collapsed to one line and capped at 500 characters.

**Outcomes:**

| Case | HTTP | Effect |
|---|---|---|
| Success | 200 | Drafts saved, `generated_at` set, `last_error` cleared, status → `GENERATED` (one event "Content generated with <model>." unless already `GENERATED`). |
| Status not in `SHORTLISTED`/`GENERATED`/`OPENED`/`READY_TO_SEND`/`FAILED`, no resume, resume without a file, or a request body | 400 | Nothing changed; Ollama is not called. |
| Resume file missing or unreadable | 422 | `last_error` set. |
| Ollama unreachable or remote URL configured | 503 | `last_error` set. |
| Ollama timeout | 504 | `last_error` set. |
| Ollama HTTP error, bad response, invalid or placeholder output | 502 | `last_error` set. |
| Application moved to another status (e.g. `SENT`) during the call | 409 | Result discarded; `last_error` set. |

On every failure (user decision, Milestone 5), the status and any earlier
drafts are unchanged, no event is written, and no application is created. No
database lock is held during the model call.

### Planned resources [Proposed]

The exact historical route list was not recovered. The resources below follow
from the models, the throttle scopes and the workflow. Confirm each one when
its milestone starts.

| Path (under `/api/v1/`) | Purpose | Milestone |
|---|---|---|
| `applications/generate-batch/` (scope `application-batch-generation`) | Batch generation through Celery, using `queue_position` | later |
| `collector/…` (scope `collector-run`) | Start and monitor LinkedIn collection runs | 6 |

---

## 5. LinkedIn collector (Milestone 6)

### History [Verified]

None of the original collector code survived. Milestone 1 removed the
`"collector"` app and the `api/v1/collector/` route so Django could start. It
kept the `collector` logger, the `collector-run` throttle scope and the
`.gitignore` rules (`backend/browser_profile/`, LinkedIn debug files). Those
traces indicate that the original used Playwright with a logged-in browser
profile to search LinkedIn and identify **post** authors; that it collected
posts is an inference.

### Decisions (approved)

1. Collect recruiter hiring **posts** first; LinkedIn Jobs listings are not
   implemented.
2. A `CollectorRun` model and migration are approved for Phase 6c.
3. The management command is the primary interface. A synchronous API with a
   strict limit may come later. There is no Celery or Redis.
4. Playwright Chromium installation is approved for Phase 6b; it is not
   installed yet.
5. The browser profile lives outside OneDrive, at
   `%LOCALAPPDATA%\ApplyForge\browser_profile`.

No anti-bot or detection-evasion techniques are used. Runs are started by the
user only, and are small.

### Phase 6a [Verified]

- **Registration.** `"collector"` is back in `INSTALLED_APPS`. The
  `api/v1/collector/` route stays deferred to Phase 6c, because there are no
  endpoints yet.
- **Settings.** `COLLECTOR_DEFAULT_POSTS_PER_RUN` (20) and
  `COLLECTOR_MAX_POSTS_PER_RUN` (50).
- **`collector/posts.py`.** It turns a `RawPost` (text, url, author name and
  headline, posted time) into a `JobIngestSerializer` payload.
  - Posts without a hiring cue are **skipped**. Text under 40 characters is
    **invalid**.
  - `full_jd` is the exact post text.
  - The title comes from a `Role:`/`Position:` label, then a hiring phrase
    ("hiring a X", "looking for an X", "opening for X"), otherwise the first
    sentence.
  - Company, location, work mode and experience are only filled when the post
    states them.
  - Contacts come only from emails and phone numbers in the post (WhatsApp when
    the line says so). One email plus at most one phone and one WhatsApp number
    make a single contact named after the author; otherwise each identifier is
    a separate unnamed contact. Numbers are deduplicated on their last 10 digits.
  - Nothing is invented.
- **`collector/sources.py`.** The `PostSource` interface
  (`iter_posts(keywords, limit)`), `SourceError`, `StaticPostSource` (tests) and
  `JsonFilePostSource` (offline JSON list).
- **`collector/service.py`.** `run_collection(source, keywords, profile, limit)`.
  - It validates the limit, then calls `resolve_profile` **before** touching the
    source. Keywords default to the profile's target roles.
  - Each post is parsed, validated with `JobIngestSerializer` and passed to
    `ingest_job` in-process.
  - Outcomes per post: `created`, `existing` (with `matched_by`), `skipped`,
    `invalid` or `failed`. One failing post never stops the run. A
    `SourceError` ends the run and keeps the partial results.
- **`collect_linkedin` command.**
  `--source file --file posts.json [--keywords …] [--limit N] [--profile ID] [--json] [-v 2]`.
  `--source linkedin` (the default) reports that the browser source arrives in
  Phase 6b. Output is safe on legacy Windows console encodings.
- **Post URLs.** `tracker.ingestion.normalize_source_url` collapses
  `/feed/update/urn:li:activity:<id>` (also URL-encoded) and
  `/posts/<slug>-activity-<id>-<suffix>` into
  `https://www.linkedin.com/feed/update/urn:li:activity:<id>/`; `share` and
  `ugcPost` ids keep their type. The existing LinkedIn job-link behavior is
  unchanged.

### Phase 6b — browser source [Verified offline; NOT verified against live LinkedIn]

- **Chromium.** Installed with `playwright install chromium` to
  `%LOCALAPPDATA%\ms-playwright` (build `chromium-1234`).
- **Settings,** all overridable from the environment:
  - `COLLECTOR_BROWSER_PROFILE_DIR` (default
    `%LOCALAPPDATA%\ApplyForge\browser_profile`) and `COLLECTOR_DEBUG_DIR`
    (default `%LOCALAPPDATA%\ApplyForge\debug`), both outside the repo and
    OneDrive.
  - `COLLECTOR_DEBUG_FILES_KEPT` (5) and `COLLECTOR_HEADLESS` (false: a visible
    window).
  - `COLLECTOR_ACTION_DELAY_SECONDS` (4, fixed and not randomized),
    `COLLECTOR_PAGE_TIMEOUT_SECONDS` (30), `COLLECTOR_MAX_SCROLLS_PER_KEYWORD` (8)
    and `COLLECTOR_LOGIN_TIMEOUT_SECONDS` (600).
- **`linkedin/selectors.py`.** Every URL, URL marker and selector, in XPath
  (`cssselect` isn't a dependency), with ordered fallbacks.
  - Search: `/search/results/content/?keywords=…&sortBy="date_posted"`.
  - LinkedIn's ordinary login form posts to `/checkpoint/lg/…`, which counts as
    a login. Only `/checkpoint/challenge…` counts as a security check.
  - The selectors are best-effort and must be confirmed live.
- **`linkedin/extract.py`.** Pure lxml functions.
  - `classify_page`: results, no results, login required, checkpoint, or
    unrecognized.
  - `extract_posts`: text with line breaks kept, without hidden duplicates,
    "…see more" or "hashtag" labels; author name and headline; canonical URL
    `https://www.linkedin.com/feed/update/<urn>/`.
  - The posted time is decoded from the post id (the top 41 bits are
    milliseconds), with LinkedIn's relative label ("3d") as a fallback.
- **`linkedin/diagnostics.py`.** Saves sanitized HTML only for unrecognized
  pages.
  - It strips script, style, code, meta, inputs, iframes, links and svg, keeps
    only a small set of attributes and strips query strings from hrefs, and
    redacts token-like text (JSESSIONID, li_at, ajax:…, bearer, long opaque
    strings).
  - Only the newest 5 files are kept, and cookies are never read.
- **`linkedin/browser.py`.** `LinkedInBrowser` wraps a persistent Chromium
  context in plain, unmodified Chromium: no stealth, user-agent, fingerprint or
  proxy changes, and no CAPTCHA handling.
  - It waits for a recognizable page state and pauses after each navigation
    and scroll.
  - After a scroll it waits up to 5 seconds for lazily loaded posts.
  - It clicks visible "see more" toggles.
  - Every Playwright failure becomes `BrowserError` (a `SourceError`), with a
    clear message when the profile is in use by another window.
  - Test guard: `LIVE_BROWSER_ALLOWED` is switched off by `collector/tests`, and
    then a browser only starts when a `route_handler` serves every request
    offline.
- **`linkedin/source.py`.** `LinkedInPostSource(browser_factory, …)`.
  - Per keyword: search, check the page state, expand, extract, deduplicate,
    then scroll until the limit, the scroll budget, or a scroll that adds
    nothing new.
  - **It collects first and closes the browser before yielding anything,**
    because Playwright's sync API runs an asyncio loop that makes Django refuse
    database access while the browser is open. On a mid-run failure, the posts
    already collected are yielded and then the error is raised.
  - Errors: `LoginRequired`, `CheckpointRequired` (never automated),
    `PageNotUnderstood` (with a diagnostic) and `BrowserError`. A keyword with
    no results is not an error.
- **Commands.**
  - `collect_linkedin` defaults to `--source linkedin` (`--headless` optional).
    It exits with an error when the source fails **or any post fails to
    ingest**; posts already ingested are kept.
  - `linkedin_login` opens a visible window at the LinkedIn login page and waits
    until you are logged in (or the window is closed, or it times out).
    `linkedin_login --check` checks the saved session headlessly.
  - The commands never prompt for, read or store a password or cookies.

### Phase 6b — live validation and hardening [Verified on live LinkedIn, scratch DB only]

All live runs used the saved session, a visible browser, one keyword and at
most 5 posts. They wrote only to the scratch database
`%LOCALAPPDATA%\ApplyForge\live-test.sqlite3`; the project database stayed at
0 bytes.

- **Login and session.**
  - Login detection uses the `/feed` address plus the absence of login forms,
    guest markers or security checks. `global-nav` is a hint only.
  - `linkedin_login --check` confirmed the session.
  - Closing the window gives a clean `BrowserClosed` error.
- **Current search layout.**
  - Classes are hashed. Posts are found by `data-view-name="feed-full-update"`
    (outermost only), then the `Feed post` heading, then the legacy selectors.
  - Text comes from `feed-commentary` and the author from `feed-actor` (person
    or company actors only).
  - Header layouts confirmed by the inventory's header shapes:
    - person: name + `• 2nd`, then headline, then time;
    - company page: name, then time;
    - group post: the group is the actor; the person's name is followed by a
      `•` degree marker; no time is shown;
    - name and degree in one paragraph.
  - The headline is never the author or a degree marker. Time labels are
    strict (`1m`, `3h • Edited`; not `3D Artist`).
- **"… more" expansion.**
  - The control is identified structurally: a button inside a post paragraph,
    `aria-hidden="true"` or starting with `…`, with no `data-view-name` or popup.
  - It's clicked by element handle, with a bounded wait (5 s at most) for
    asynchronously loaded text.
  - `-v 3` marks any text still ending in `…`.
- **Classification.**
  - Job seekers' posts ("I am actively looking for new opportunities", "Open to
    work", "Recruiters and hiring managers…") are skipped.
  - Titles prefer real roles over slogans like "Immediate Joiners Required".
  - Lowercase and ALL-CAPS titles are tidied.
  - Company only from explicit evidence, never the author.
- **Post URLs.**
  - None in normal search results; `componentkey` is opaque (inventory v2/v3).
  - **Group posts** carry their post URN percent-encoded in links (seen 1 of 9);
    recovered since 6c (see "Group-post URLs" below).
- **Diagnostics.**
  - `collect_linkedin --inventory-only` and `--attribute-inventory` (inventory
    v3: attribute names, public post URNs, allowlisted structural values,
    masked header shapes).
  - `-v 3` prints per-post author, headline, time and commentary length.
- **Scratch database contents.** The early runs saved two job-seeker posts as
  jobs (ids 3 and 5) and one truncated text (id 2), before the fixes. They are
  left as evidence and are not production data.

### Phase 6c — run history and API [Verified offline]

- **Model** `collector.CollectorRun` (migration `collector/0001_initial`, one
  additive table, depends on `tracker/0008`): `status` (RUNNING, SUCCEEDED,
  PARTIAL, FAILED), `trigger` (command, api), `source`, `profile` (SET_NULL),
  `keywords`, `limit`, `counts`, `job_ids` (created or matched), `error`,
  `started_at`, `finished_at`. Run metadata only: no post text, authors,
  headlines, URLs or contact details.
- **Recording** (`collector/runs.py`, `record_collection`): wraps
  `run_collection` unchanged. A RUNNING row is written before the source is
  used and finished afterwards; both writes happen outside the browser session.
  SUCCEEDED = no source error and no failed posts; PARTIAL = source failed after
  some posts, or some posts failed to ingest; FAILED = invalid input, source
  failed before any post, or an unexpected error (recorded, then re-raised).
- **Command.** `collect_linkedin` records every collection (trigger `command`)
  and prints the run id (`run_id`/`run_status` in `--json`). Exit behaviour is
  unchanged. `--inventory-only` records nothing and still has no database access.
- **API** (`/api/v1/collector/runs/`, token auth):
  - `GET runs/` newest first; filters `status`, `trigger`, `profile`; optional
    pagination. `GET runs/{id}/`. Runs are read-only (no PATCH/PUT/DELETE).
  - `POST runs/` starts a LinkedIn run and waits for it. Body (all optional):
    `keywords` (≤10 strings), `limit` (1..`COLLECTOR_API_MAX_POSTS_PER_RUN`, 10;
    default `COLLECTOR_API_DEFAULT_POSTS_PER_RUN`, 5), `profile`. Unknown fields are rejected, so
    there is no file source over HTTP. 201 SUCCEEDED; 502 PARTIAL/FAILED (with
    `detail`; saved jobs are kept); 400 invalid input (with `run` when the
    service recorded a FAILED run); 409 while another run is RUNNING; 500 for an
    unexpected error (recorded FAILED; details only in the server log).
    Throttle `collector-run` (6/min) on POST only.
  - One run at a time: a per-process lock plus the RUNNING-row check. RUNNING
    rows older than `COLLECTOR_STALE_RUN_MINUTES` (30) are marked FAILED
    ("Abandoned") and stop blocking.
- **Databases.** Applied to the development database `backend/db.sqlite3`
  only (its first migration; it was empty). The scratch evidence database
  (`%LOCALAPPDATA%\ApplyForge\live-test.sqlite3`) was **not** migrated and
  has no `collector_collectorrun` table, so a guarded live run against it now
  needs that migration (or another scratch database) first.
- **Limitations.** API runs are synchronous: the request waits for the browser
  (visible window unless `COLLECTOR_HEADLESS`). The cross-process guard is a
  check-then-insert on the RUNNING row, not a database lock; fine for a single
  local user. Live LinkedIn has not been exercised through the API.

### Group-post URLs [Verified offline; NOT yet verified on live LinkedIn]

- Current layout only, and only when no `data-urn`/permalink exists:
  `_encoded_link_urn` reads post URNs (`activity`, `ugcPost`, `share`) from
  percent-encoded (up to double-encoded) **LinkedIn-internal** `<a href>` links
  that belong to the post itself.
- Ignored: links inside the post text (`feed-commentary`), links inside a nested
  (reshared) `feed-full-update`, external or non-HTTPS links, other URN types.
- Accepted only when exactly **one** distinct URN is found and its embedded
  timestamp is plausible (2010 … now + 1 day); otherwise no URL (never a guess).
- The URL becomes `https://www.linkedin.com/feed/update/<urn>/`, so dedupe and
  ingestion match by URL; the URN also supplies `posted_at` for group posts,
  which show no time label.
- Limitation: the exact link shape is inferred from the inventory (attribute
  `href`, method `url`, one URN in 3 links); fixtures use fictional shapes.

### Next phases

- **Live check of group-post URLs** in the next authorised validation run
  (scratch database, which first needs the `collector/0001` migration).

---

## 6. Rebuild plan

| # | Milestone | Status |
|---|---|---|
| 1 | Backend foundation: `tracker.api` auth/pagination/urls, collector deferral, tests, this spec, `.gitignore` for root venvs | **Done (uncommitted in working tree)** |
| 2 | Profile, skills and resume APIs (CRUD and serializers, read-only skill categories, on-demand resume text extraction), plus tests | **Done (uncommitted in working tree)** |
| 3 | Ingestion and screening services: idempotent dedupe (rule 5), keyword relevance → experience eligibility → duplicates (rules 1–4), an additive migration for screening outcomes/reasons, and the jobs API | **Done (uncommitted in working tree)** |
| 4 | Application tracking API: statuses, events, the manual-send workflow (rule 9), dashboard summary | **Done (uncommitted in working tree)** |
| 5 | Generation via local Ollama/Qwen (rules 6–8, 10): grounded prompts, clear failure that changes only `last_error`, throttled endpoint, editable drafts, `SENT` → job `APPLIED` | **Done (uncommitted; verified with a simulated Ollama only)**. Batch generation through Celery is deferred. |
| 6 | Integrated LinkedIn collector (rule 12): 6a parsing, service, command and offline tests; 6b Playwright browser source; 6c `CollectorRun` and API | **6a, 6b and 6c done:** browser source validated live into the scratch database and hardened; 6c run history and API verified offline. |
| 7 | React + Vite frontend dashboard | **Done** (frontend `17420d2`, integration hardening `16c2ba0`). |
| 8 | Production readiness: settings hardening, dependency upgrades, health check, PostgreSQL, private deployment, backups | **8.1–8.3 done** (`ba57c62`). **8.4b** PostgreSQL rehearsal passed (PostgreSQL 18.6; all migrations, checks and 557 backend tests). **8.4c** private/VPN deployment configuration and runbook written; not yet used on a server. **8.4d** encrypted backup and restore tool, documentation and 140 tests written, including two security corrective passes; real `age` 1.2.1 integration validated (2026-11-19); real PostgreSQL 18.6 backup/restore rehearsal passed (2026-11-20, disposable objects, **tmpfs** destination - NFS/SMB and the systemd recovery path not yet validated). |

Order may be adjusted, but each milestone should leave `manage.py check`,
`makemigrations --check` and the test suite passing.

### Deployment: private/VPN server (Milestone 8.4c) [Configuration only; not yet deployed]

The configuration and the full runbook are in `deploy/`; `deploy/README.md` is
authoritative.

- **Topology.** A single Linux host reachable only over a VPN, with these layers:
  - **nginx** listens on the VPN address and allows only the VPN subnet. It
    terminates HTTPS and serves the React build from the release's
    `frontend/dist` (SPA fallback to `index.html`) and Django static files from
    `STATIC_ROOT`. It proxies `/api/` to Gunicorn.
  - **Gunicorn** listens on `127.0.0.1:8000`, with 1 `gthread` worker × 8
    threads and a 900-second timeout. It runs under the sandboxed systemd unit
    `applyforge`, as user `applyforge`.
  - **PostgreSQL** runs on localhost. The first deployment starts from an empty
    database.
- **One worker on purpose.** The throttles (`LocMemCache`) and the collector
  lock are per process.
- **Configuration.**
  - Settings come only from `/etc/applyforge/applyforge.env` (example:
    `deploy/env.production.example`, placeholders only).
  - `APPLYFORGE_DOTENV` points to a path that does not exist, so a development
    `.env` is never loaded.
  - `deploy/preflight.py` runs before the service starts and before every
    management command, and refuses to continue on an unsafe configuration
    (`DEBUG`, placeholder secrets, `.env` present, wrong proxy or HSTS
    settings). It never touches the database.
- **Edge rules.**
  - `X-Forwarded-Proto` is overwritten with `https`, never appended.
  - `/admin` and `/media` answer 404 at nginx.
  - Uploads are capped at 12 MB at nginx; the application's resume limit is
    10 MB.
  - HSTS is one year, without `includeSubDomains` and without `preload`.
- **Disabled features.** The collector (`POST /api/v1/collector/…`) and draft
  generation (`POST /api/v1/applications/<id>/generate`) are disabled: nginx
  answers 403 with a JSON `detail`, and the service may only reach localhost.
  The 900-second timeouts are already configured for when these features are
  deliberately enabled.
- **Releases.**
  - Each release goes in `/srv/applyforge/releases/<commit>` with its own
    `.venv`; an atomic `current` symlink selects the active one.
  - Media and static files live under `/var/lib/applyforge/`, outside the
    releases.
  - A code rollback switches the symlink back. It never restores or migrates
    the database; that is a separate, explicit decision (Milestone 8.4d).

### Backup and restore (Milestone 8.4d) [Tested with fictional data only; no real rehearsal yet]

`deploy/backup/afbackup.py` (installed as `applyforge-backup`) is described in
section 12 of `deploy/README.md`, which is authoritative.

- **Encryption and trust.**
  - Archives are encrypted with `age` to public X25519 recipients only.
  - The private identity stays offline.
  - The tool refuses a recipients file that contains a private key.
  - age does not prove who created an archive, and the share controls what it
    returns on reads. So the trusted SHA-256 is computed from age's output as
    it streams through the tool, never from a read-back of the share.
  - That hash is recorded in a root-only local index and the journal
    **before** the archive is published.
  - `restore` requires `--expected-sha256` and checks it against a private
    local copy before decryption. When a local index exists, the hash must be
    in it (`--allow-unindexed` is the documented exception).
- **Contents.**
  - The archive holds a manifest (timestamp, host, database name, release,
    and size and SHA-256 per file), a `pg_dump` custom-format dump and every
    regular file under `MEDIA_ROOT`.
  - No environment file, tokens, `SECRET_KEY`, credentials or browser
    profiles are included.
  - `pg_dump` uses peer authentication as `postgres`, so no password is
    involved.
- **Consistency.**
  - The service is stopped while the dump and media are captured, within a
    capture deadline and with `pg_dump --lock-wait-timeout`.
  - It is always started again if it was running, including on failure,
    timeout and SIGTERM; exit code 3 means it could not be.
  - A durable marker plus the unit's `ExecStopPost=… recover-service`
    restarts it even after SIGKILL.
  - Encryption happens after the restart.
  - Unreadable media fails the backup.
- **Concurrency and plaintext.**
  - One shared lock, `/run/lock/applyforge-backup.lock`, covers timer and
    manual runs.
  - Stale work and staging directories from crashed runs are removed under the
    lock, and a tmpfiles.d rule backs this up.
- **Destination.**
  - It must be a mount point containing a regular marker file, with an
    optional allow-list of filesystem types, otherwise the backup is refused.
  - The tool creates the `.partial` file itself
    (`O_CREAT|O_EXCL|O_NOFOLLOW`, `0600`). age writes into that descriptor,
    which is fsynced.
  - The destination is re-checked before publishing.
  - Publishing never overwrites an existing archive.
  - On failure, only the run's own partial file is removed.
- **Retention.**
  - Archives older than 30 days (configurable) are deleted, and only after a
    successful backup.
  - The 7 newest are always kept (`BACKUP_MIN_KEEP`).
  - Only exact archive names are eligible.
  - A retention failure is reported separately (exit code 4).
- **Restore.**
  - The expected hash is checked, then the archive is decrypted and fully
    validated: member types, sizes against the manifest, member count, free
    disk space and checksums.
  - Data goes into a new database and a new or empty media directory, and both
    names must be repeated as confirmation.
  - `createdb` runs as `postgres`. `pg_restore` runs as a dedicated, isolated
    restore role and OS account (`applyforge_restore`), never as `postgres`
    and never as the production owner.
  - Before the restore, the effective identity of that connection is
    verified: `current_user`/`session_user`, no superuser or role-creation
    rights, no bypass-RLS, no memberships, no server-program or server-file
    roles, and no ownership of or privileges on production objects.
  - Ownership transfer is a separately authorized switch step.
  - Existing data is never dropped, overwritten or switched to.
  - A failed restore leaves the newly created empty database in place and
    reports it.
- **Configuration.**
  - `backup.conf` and the recipients file must be root-owned and not group- or
    world-writable.
  - Unknown and duplicate keys are refused.
  - The destination, work, state and media directories must not overlap.
  - `MEDIA_ROOT` must not be under `/etc`, `/root`, `/home` or
    `/srv/applyforge`.
- **Media capture.** Media is walked only through directory descriptors with
  `O_NOFOLLOW` and checked by device and inode during the scan and again when
  archiving. Filesystem crossings and swaps are refused.
- **Service recovery.** Automatic recovery happens only for the marker of the
  same invocation, same boot, and an untouched service. Old or ambiguous
  markers never start ApplyForge.
- **Signals and environment.** SIGTERM, SIGHUP and SIGINT are handled, and
  logging can't block recovery. Subprocesses get a minimal explicit
  environment.
- **Tests.** 140 tests with fictional data and stand-in tools, including a
  real-`age` round trip.
- **Real `age` 1.2.1, validated in WSL on 2026-11-19:** streaming
  encryption, a trusted hash matching age's output, byte-for-byte decryption,
  and clean refusal of a wrong key, truncation, corruption and age failures.
- **Real PostgreSQL 18.6 restore rehearsal, passed on 2026-11-20:**
  - disposable source and target databases, roles and OS account;
  - real `create`, `verify` and `restore`;
  - schema, every row and sequences identical, and all media byte for byte;
  - the restore ran as the isolated restore role;
  - the protected rehearsal database was untouched.
- **Limits of that rehearsal:** the destination was **tmpfs**, not NFS/SMB,
  and the service stop/start window and `ExecStopPost` were not exercised.
- **Fixed afterwards:** the rehearsal found restored media subdirectories at
  `0700` instead of `0750`; this is fixed and covered by regression tests.
- **Production hardening:** revoke the default PUBLIC `CONNECT`/`TEMP` on the
  production database (`deploy/README.md` section 12.6).
- **Remaining real-host checks:** listed in `deploy/README.md` section 12.10.

---

## 7. Verification commands

From the repository root:

```powershell
.\.venv\Scripts\python.exe backend\manage.py check
.\.venv\Scripts\python.exe backend\manage.py makemigrations --check --dry-run
cd backend; ..\.venv\Scripts\python.exe manage.py test tracker collector
```
