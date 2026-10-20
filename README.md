# ApplyForge

ApplyForge is a personal job-discovery and job-application preparation platform designed to streamline and automate the process of finding relevant jobs, extracting key details, filtering opportunities, preventing duplicate applications, and generating personalized emails for job applications based on the user's resume.

*Note: The platform prepares applications but ensures the user is always in control and must manually click SEND.*

## Project Structure

- `/backend` - Django REST Framework API. Contains the virtual environment and backend logic.
- `/frontend` - React Application (Vite) for the Dashboard.
- `/docs` - Project documentation.
- `PROJECT_CONTEXT.md` - Primary source of truth for the project's state, business rules, and architecture. **Always read this first before making changes.**

## Quick Start (Backend)

```bash
cd backend
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Mac/Linux:
source .venv/bin/activate

pip install -r requirements.txt
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
