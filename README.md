# ApplyForge

ApplyForge is a personal job-discovery and job-application preparation platform designed to streamline and automate the process of finding relevant jobs, extracting key details, filtering opportunities, preventing duplicate applications, and generating personalized emails for job applications based on the user's resume.

*Note: The platform prepares applications but ensures the user is always in control and must manually click SEND.*

## Project Structure

- `/backend` - Django REST Framework API. Contains the virtual environment and backend logic.
- `/frontend` - React Application (Vite) for the Dashboard.
- `/docs` - Project documentation. `docs/PROJECT_SPEC.md` is the source of truth for the project's state, business rules and architecture. **Read it before making changes.** (The original `PROJECT_CONTEXT.md` was lost; the spec replaces it.)
- `/deploy` - Production configuration for a private/VPN Linux server (Gunicorn, systemd, nginx, environment example) and its runbook.

## Quick Start (Backend)

```bash
cd backend
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Mac/Linux:
source .venv/bin/activate

pip install -r requirements.txt
# DEBUG defaults to False (fail closed). For local development, the root .env
# (copied from .env.example) must contain DEBUG=True - see Configuration below.
python manage.py migrate
python manage.py runserver
```

## Quick Start (Frontend)

```bash
cd frontend
npm install
npm run dev
```

## Job Automation

Open **Job Automation** in the app: enter a search (e.g. *Python Backend Developer*), choose the
date range and how many jobs you want, and start collecting. Progress is live and the collection
can be cancelled or continued. Then review the jobs (contact details are shown with the line of the
post they came from), select the ones to apply to, generate a personalised email and WhatsApp
message for each with the local model, approve, and send. **Send Email** saves the email, with
your resume attached, as a draft in your own mailbox (Account -> Email sending: an app password for
Gmail/Outlook/Yahoo, IMAP enabled) and opens your Drafts; you check it and press Send there (or
download it as an `.eml` file for Outlook/Thunderbird). **Send WhatsApp** opens WhatsApp with the
message; attach the downloaded resume and send. An application becomes *Sent* only when ApplyForge
finds the email in your Sent folder or you confirm. For local testing set
`EMAIL_BACKEND=django.core.mail.backends.filebased.EmailBackend` so nothing leaves your machine
(drafts then go to a local test mailbox folder).

## Configuration

Copy `.env.example` to `.env` in the project root and fill in the values for your environment. Never commit the actual `.env` file to version control.

The root `.env` is for **local development only**. With `DEBUG` unset or false, the backend refuses to start without a real `SECRET_KEY` and `DATABASE_URL`.

**Accounts.** Open the app and choose *Create an account*. Each account sees only its own profiles, jobs and applications. The first account created receives any data that existed before accounts (or run `python manage.py claim_unowned_data <username>`). Set `SIGNUP_ENABLED=False` once everyone who needs an account has one.

## Deployment

ApplyForge's first production target is a **private server reachable only over a VPN**. It runs nginx (HTTPS, SPA, static files, `/api/` proxy) in front of Gunicorn on `127.0.0.1`, with PostgreSQL on the same host. The configuration and the step-by-step runbook are in [`deploy/README.md`](deploy/README.md).

- The production environment comes from a systemd environment file (example: [`deploy/env.production.example`](deploy/env.production.example), placeholders only). A development `.env` is never loaded there.
- The frontend is built on the workstation (`npm run build`), and `frontend/dist` is copied to the server.
- In this first configuration, `/admin` is blocked, `/media` is never served, and the LinkedIn collector and draft generation are disabled.
- **Backups** are encrypted with age to public keys only; the private key stays offline. They're written to mounted off-host storage and kept for 30 days by default, always keeping the 7 newest. Each archive's SHA-256 is recorded in a root-only local index, because age doesn't prove who created an archive.
- **Restores** need that hash from an independent record. They go only into a **new** database and media directory, and the dump is loaded by a dedicated, isolated non-superuser restore role, never the production owner. Switching the application to restored data is a separate manual step, unrelated to code rollback.
- See section 12 of the runbook.
- **Readiness (Milestone 8.4e):** validated locally on Ubuntu 26.04 (real nginx, Gunicorn, systemd, PostgreSQL, age, loopback NFS/SMB), but not yet deployed. [`deploy/PRODUCTION_READINESS.md`](deploy/PRODUCTION_READINESS.md) lists what is verified, what still needs the real host, and the go/no-go checklist. Run `deploy/host_check.py` (read-only) on the server before every deployment.
