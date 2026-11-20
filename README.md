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

## Configuration

Copy `.env.example` to `.env` in the project root and fill in the values for your environment. Never commit the actual `.env` file to version control.

The root `.env` is for **local development only**. With `DEBUG` unset or false, the backend refuses to start without a real `SECRET_KEY`, `API_TOKEN` and `DATABASE_URL`.

## Deployment

ApplyForge's first production target is a **private server reachable only over a VPN**. It runs nginx (HTTPS, SPA, static files, `/api/` proxy) in front of Gunicorn on `127.0.0.1`, with PostgreSQL on the same host. The configuration and the step-by-step runbook are in [`deploy/README.md`](deploy/README.md).

- The production environment comes from a systemd environment file (example: [`deploy/env.production.example`](deploy/env.production.example), placeholders only). A development `.env` is never loaded there.
- The frontend is built on the workstation (`npm run build`), and `frontend/dist` is copied to the server.
- In this first configuration, `/admin` is blocked, `/media` is never served, and the LinkedIn collector and draft generation are disabled.
- **Backups** are encrypted with age to public keys only; the private key stays offline. They're written to mounted off-host storage and kept for 30 days by default, always keeping the 7 newest. Each archive's SHA-256 is recorded in a root-only local index, because age doesn't prove who created an archive.
- **Restores** need that hash from an independent record. They go only into a **new** database and media directory, and the dump is loaded by a dedicated, isolated non-superuser restore role, never the production owner. Switching the application to restored data is a separate manual step, unrelated to code rollback.
- See section 12 of the runbook.
