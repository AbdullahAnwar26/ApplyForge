"""
Production preflight for the private/VPN ApplyForge deployment (Milestone 8.4c).

Run by the systemd unit (ExecStartPre) and by ``deploy/bin/applyforge-manage``
before every management command. It fails closed: any problem stops the service
from starting. It checks the process environment (from
/etc/applyforge/applyforge.env) and the settings Django actually resolves.

It never connects to the database, never prints configuration values (only the
names of failing variables) and writes nothing.

Exit status: 0 = all checks passed, 1 = at least one check failed.
"""

import os
import sys
from pathlib import Path

RELEASE_ROOT = Path(__file__).resolve().parent.parent  # .../releases/<commit>
BACKEND_DIR = RELEASE_ROOT / "backend"
DEPLOY_PREFIX = Path("/srv/applyforge")

TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}
DEV_DEFAULTS = {"django-insecure-local-development-key", "local-dev-token"}
EXPECTED_PROXY_HEADER = "HTTP_X_FORWARDED_PROTO,https"

failures = []


def fail(message):
    failures.append(message)


def env(name):
    value = os.environ.get(name)
    return None if value is None else value.strip()


def env_list(name):
    value = env(name)
    return None if value is None else [item.strip() for item in value.split(",") if item.strip()]


def looks_like_placeholder(value):
    lowered = value.lower()
    return "<" in value or ">" in value or any(
        word in lowered for word in ("change-me", "changeme", "replace", "placeholder", "generate-a")
    )


def is_within(path, parent):
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def check_environment():
    # DEBUG: present and explicitly false (an unrecognised value is not accepted).
    debug = env("DEBUG")
    if debug is None:
        fail("DEBUG is not set; it must be set explicitly to False.")
    elif debug.lower() not in FALSE_VALUES:
        fail("DEBUG must be False in production.")

    # Development .env must never be loadable.
    dotenv = env("APPLYFORGE_DOTENV")
    if not dotenv:
        fail("APPLYFORGE_DOTENV is not set; it must name a file that does not exist.")
    else:
        dotenv_path = Path(dotenv)
        if not dotenv_path.is_absolute():
            fail("APPLYFORGE_DOTENV must be an absolute path.")
        elif dotenv_path.exists():
            fail("APPLYFORGE_DOTENV names an existing file; it must not exist.")
    for candidate in (RELEASE_ROOT / ".env", BACKEND_DIR / ".env", Path.cwd() / ".env"):
        if candidate.exists():
            fail(f"A .env file exists in the release ({candidate}); remove it - production "
                 "settings come only from the systemd EnvironmentFile.")

    # Secrets: real, distinct, long enough.
    secret_key, api_token = env("SECRET_KEY"), env("API_TOKEN")
    for name, value, minimum in (("SECRET_KEY", secret_key, 50), ("API_TOKEN", api_token, 32)):
        if not value:
            fail(f"{name} is not set.")
        elif value in DEV_DEFAULTS or value.startswith("django-insecure"):
            fail(f"{name} is a development default.")
        elif looks_like_placeholder(value):
            fail(f"{name} still contains a placeholder.")
        elif len(value) < minimum:
            fail(f"{name} must be at least {minimum} characters.")
    if secret_key and api_token and secret_key == api_token:
        fail("SECRET_KEY and API_TOKEN must be different.")

    # Database: PostgreSQL, filled in.
    database_url = env("DATABASE_URL")
    if not database_url:
        fail("DATABASE_URL is not set.")
    elif not database_url.startswith(("postgres://", "postgresql://")):
        fail("DATABASE_URL must be a PostgreSQL URL (postgres://...).")
    elif looks_like_placeholder(database_url):
        fail("DATABASE_URL still contains a placeholder.")

    # Hosts and origins.
    hosts = env_list("ALLOWED_HOSTS")
    if not hosts:
        fail("ALLOWED_HOSTS must list the private host name.")
    elif any(h == "*" or h.startswith(".") or looks_like_placeholder(h) for h in hosts):
        fail("ALLOWED_HOSTS must not contain wildcards or placeholders.")

    csrf_origins = env_list("CSRF_TRUSTED_ORIGINS")
    if not csrf_origins:
        fail("CSRF_TRUSTED_ORIGINS must list the https:// origin of the site.")
    elif any(not o.startswith("https://") or "*" in o or looks_like_placeholder(o) for o in csrf_origins):
        fail("CSRF_TRUSTED_ORIGINS must contain only real https:// origins without wildcards.")

    cors_origins = env_list("CORS_ALLOWED_ORIGINS")
    if cors_origins is None:
        fail("CORS_ALLOWED_ORIGINS must be set (normally empty); unset falls back to "
             "the localhost development origins.")
    elif any(not o.startswith("https://") or "*" in o for o in cors_origins):
        fail("CORS_ALLOWED_ORIGINS may contain only https:// origins without wildcards.")

    # HTTPS behind nginx.
    proxy_header = (env("SECURE_PROXY_SSL_HEADER") or "").replace(" ", "")
    if proxy_header != EXPECTED_PROXY_HEADER:
        fail(f"SECURE_PROXY_SSL_HEADER must be {EXPECTED_PROXY_HEADER} for this nginx setup.")
    for name in ("SECURE_SSL_REDIRECT", "SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE"):
        value = env(name)
        if value is not None and value.lower() not in TRUE_VALUES:
            fail(f"{name} must not be disabled.")
    # includeSubDomains defaults to True in settings.py, so it must be turned off explicitly.
    if (env("SECURE_HSTS_INCLUDE_SUBDOMAINS") or "").lower() not in FALSE_VALUES:
        fail("SECURE_HSTS_INCLUDE_SUBDOMAINS must be set to False for this deployment.")
    if (env("SECURE_HSTS_PRELOAD") or "false").lower() not in FALSE_VALUES:
        fail("SECURE_HSTS_PRELOAD must be False for this deployment.")

    # Storage: absolute, outside the release tree, separate.
    roots = {}
    for name in ("STATIC_ROOT", "MEDIA_ROOT"):
        value = env(name)
        if not value:
            fail(f"{name} is not set; the default lies inside the release directory.")
            continue
        path = Path(value)
        if not path.is_absolute():
            fail(f"{name} must be an absolute path.")
            continue
        if is_within(path, DEPLOY_PREFIX) or is_within(path, RELEASE_ROOT):
            fail(f"{name} must be outside the release tree (/srv/applyforge).")
        if not path.is_dir():
            fail(f"{name} does not exist or is not a directory.")
        roots[name] = path
    if len(roots) == 2:
        static, media = roots["STATIC_ROOT"], roots["MEDIA_ROOT"]
        if is_within(static, media) or is_within(media, static):
            fail("STATIC_ROOT and MEDIA_ROOT must be separate, non-nested directories.")
        elif media.is_dir() and not os.access(media, os.W_OK | os.X_OK):
            fail("MEDIA_ROOT is not writable by this user.")


def check_resolved_settings():
    """Import the real settings module (no django.setup(), no database access)."""
    sys.path.insert(0, str(BACKEND_DIR))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    try:
        from config import settings
    except Exception as exc:  # report the type only: messages may contain values
        fail(f"Django settings failed to load ({type(exc).__name__}).")
        return
    if settings.DEBUG is not False:
        fail("Resolved settings.DEBUG is not False.")
    if getattr(settings, "SECURE_PROXY_SSL_HEADER", None) != ("HTTP_X_FORWARDED_PROTO", "https"):
        fail("Resolved SECURE_PROXY_SSL_HEADER is not HTTP_X_FORWARDED_PROTO=https.")
    if not getattr(settings, "SECURE_SSL_REDIRECT", False):
        fail("Resolved SECURE_SSL_REDIRECT is not enabled.")
    if settings.DATABASES["default"].get("ENGINE") != "django.db.backends.postgresql":
        fail("Resolved database engine is not PostgreSQL.")


def main():
    check_environment()
    if not failures:  # only import settings once the environment itself is sane
        check_resolved_settings()
    if failures:
        for message in failures:
            print(f"preflight FAIL: {message}", file=sys.stderr)
        print(f"preflight: {len(failures)} problem(s); refusing to continue.", file=sys.stderr)
        return 1
    print("preflight: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
