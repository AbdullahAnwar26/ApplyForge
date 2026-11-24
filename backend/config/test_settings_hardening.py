"""Milestone 8.1: production settings hardening.

Settings are evaluated at import time, so each startup case imports
``config.settings`` in a fresh Python process with a controlled environment:
no inherited DEBUG/SECRET_KEY/... and APPLYFORGE_DOTENV pointing at a file that
does not exist, so a developer's local .env cannot influence the result. The
child processes only import settings - no database is opened.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, override_settings

BACKEND = Path(__file__).resolve().parent.parent
CONTROLLED = {
    "DEBUG", "SECRET_KEY", "API_TOKEN", "DATABASE_URL", "ALLOWED_HOSTS", "STATIC_ROOT", "MEDIA_ROOT",
    "SECURE_PROXY_SSL_HEADER", "SECURE_HSTS_PRELOAD", "SECURE_HSTS_SECONDS", "SECURE_HSTS_INCLUDE_SUBDOMAINS",
    "SECURE_SSL_REDIRECT", "SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE", "DJANGO_SETTINGS_MODULE", "APPLYFORGE_DOTENV",
}
PRODUCTION = {
    "SECRET_KEY": "test-only-secret-key-not-used-anywhere-0123456789abcdef",
    "API_TOKEN": "test-only-api-token-0123456789abcdef",
    # Parsed only; never connected to.
    "DATABASE_URL": "postgres://user:pw@127.0.0.1:1/never_connected",
}
REPORT = """
import json
from pathlib import Path
import config.settings as s
print(json.dumps({
    "DEBUG": s.DEBUG,
    "SECURE_PROXY_SSL_HEADER": s.SECURE_PROXY_SSL_HEADER,
    "STATIC_ROOT": str(s.STATIC_ROOT),
    "MEDIA_ROOT": str(s.MEDIA_ROOT),
    "SECURE_SSL_REDIRECT": getattr(s, "SECURE_SSL_REDIRECT", None),
    "SESSION_COOKIE_SECURE": getattr(s, "SESSION_COOKIE_SECURE", None),
    "CSRF_COOKIE_SECURE": getattr(s, "CSRF_COOKIE_SECURE", None),
    "SECURE_HSTS_SECONDS": getattr(s, "SECURE_HSTS_SECONDS", None),
    "SECURE_HSTS_INCLUDE_SUBDOMAINS": getattr(s, "SECURE_HSTS_INCLUDE_SUBDOMAINS", None),
    "SECURE_HSTS_PRELOAD": getattr(s, "SECURE_HSTS_PRELOAD", None),
    "SECURE_CONTENT_TYPE_NOSNIFF": getattr(s, "SECURE_CONTENT_TYPE_NOSNIFF", None),
    "X_FRAME_OPTIONS": getattr(s, "X_FRAME_OPTIONS", None),
    "DATABASE_ENGINE": s.DATABASES["default"]["ENGINE"],
}))
"""


def load_settings(**env):
    """Import config.settings in a clean child process. Returns (returncode, data or stderr)."""
    child_env = {k: v for k, v in os.environ.items() if k not in CONTROLLED}
    child_env["APPLYFORGE_DOTENV"] = str(BACKEND / "no-such-dotenv-file.env")
    child_env.update(env)
    proc = subprocess.run(
        [sys.executable, "-c", REPORT], cwd=BACKEND, env=child_env, capture_output=True, text=True, timeout=60
    )
    if proc.returncode == 0:
        return 0, json.loads(proc.stdout.strip().splitlines()[-1])
    return proc.returncode, proc.stderr


class DebugFailsClosedTests(SimpleTestCase):
    def test_missing_debug_fails_closed(self):
        # No DEBUG and no production secrets: DEBUG is False, so startup refuses.
        code, err = load_settings()
        self.assertNotEqual(code, 0)
        self.assertIn("SECRET_KEY must be configured when DEBUG=False", err)

    def test_missing_debug_with_production_settings_is_production(self):
        code, data = load_settings(**PRODUCTION)
        self.assertEqual(code, 0, data)
        self.assertFalse(data["DEBUG"])
        self.assertTrue(data["SECURE_SSL_REDIRECT"])
        self.assertTrue(data["SESSION_COOKIE_SECURE"])
        self.assertTrue(data["CSRF_COOKIE_SECURE"])
        self.assertEqual(data["SECURE_HSTS_SECONDS"], 31536000)
        self.assertTrue(data["SECURE_HSTS_INCLUDE_SUBDOMAINS"])
        self.assertTrue(data["SECURE_CONTENT_TYPE_NOSNIFF"])
        self.assertEqual(data["X_FRAME_OPTIONS"], "DENY")
        self.assertEqual(data["DATABASE_ENGINE"], "django.db.backends.postgresql")

    def test_unrecognised_debug_value_fails_closed(self):
        for value in ("", "maybe", "ture", "2"):
            with self.subTest(value=value):
                code, err = load_settings(DEBUG=value)
                self.assertNotEqual(code, 0)
                self.assertIn("SECRET_KEY must be configured", err)

    def test_production_still_requires_the_database(self):
        code, err = load_settings(SECRET_KEY=PRODUCTION["SECRET_KEY"])
        self.assertNotEqual(code, 0)
        self.assertIn("DATABASE_URL must be configured", err)

    def test_production_no_longer_needs_an_api_token(self):
        # Accounts milestone: users sign in with their own accounts; the shared API token is gone.
        code, data = load_settings(SECRET_KEY=PRODUCTION["SECRET_KEY"], DATABASE_URL=PRODUCTION["DATABASE_URL"])
        self.assertEqual(code, 0, data)

    def test_explicit_local_development(self):
        for value in ("True", "true", "1", "yes"):
            with self.subTest(value=value):
                code, data = load_settings(DEBUG=value)
                self.assertEqual(code, 0, data)
                self.assertTrue(data["DEBUG"])
                self.assertEqual(data["DATABASE_ENGINE"], "django.db.backends.sqlite3")
                self.assertIsNone(data["SECURE_SSL_REDIRECT"])  # HTTPS hardening is production-only

    def test_dotenv_can_enable_development(self):
        with tempfile.TemporaryDirectory() as tmp:
            dotenv = Path(tmp) / ".env"
            dotenv.write_text("DEBUG=True\n", encoding="utf-8")
            code, data = load_settings(APPLYFORGE_DOTENV=str(dotenv))
            self.assertEqual(code, 0, data)
            self.assertTrue(data["DEBUG"])
            # A real environment variable still wins over the .env file.
            code, err = load_settings(APPLYFORGE_DOTENV=str(dotenv), DEBUG="False")
            self.assertNotEqual(code, 0)


class ProxySslHeaderSettingTests(SimpleTestCase):
    def test_not_trusted_by_default(self):
        code, data = load_settings(**PRODUCTION)
        self.assertEqual(code, 0, data)
        self.assertIsNone(data["SECURE_PROXY_SSL_HEADER"])

    def test_configured_header(self):
        code, data = load_settings(**PRODUCTION, SECURE_PROXY_SSL_HEADER=" HTTP_X_FORWARDED_PROTO , https ")
        self.assertEqual(code, 0, data)
        self.assertEqual(data["SECURE_PROXY_SSL_HEADER"], ["HTTP_X_FORWARDED_PROTO", "https"])

    def test_malformed_values_stop_startup(self):
        for value in ("X-Forwarded-Proto", "X-Forwarded-Proto,https", "HTTP_X_FORWARDED_PROTO", "HTTP_,https",
                      "HTTP_x_forwarded_proto,https", "HTTP_X_FORWARDED_PROTO,", "HTTP_A,https,extra"):
            with self.subTest(value=value):
                code, err = load_settings(**PRODUCTION, SECURE_PROXY_SSL_HEADER=value)
                self.assertNotEqual(code, 0)
                self.assertIn("SECURE_PROXY_SSL_HEADER must look like", err)


@override_settings(SECURE_SSL_REDIRECT=True, ALLOWED_HOSTS=["testserver"])
class ProxySslRedirectBehaviourTests(SimpleTestCase):
    """Django's SecurityMiddleware with the setting: forwarded HTTPS is honoured only when configured."""

    URL = "/api/v1/skill-categories/"  # needs a token: 401 means "served", 301 means "redirected"

    @override_settings(SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"))
    def test_forwarded_https_is_served(self):
        response = self.client.get(self.URL, HTTP_X_FORWARDED_PROTO="https")
        self.assertEqual(response.status_code, 401)

    @override_settings(SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"))
    def test_forwarded_http_is_redirected(self):
        response = self.client.get(self.URL, HTTP_X_FORWARDED_PROTO="http")
        self.assertEqual(response.status_code, 301)
        self.assertTrue(response["Location"].startswith("https://"))

    @override_settings(SECURE_PROXY_SSL_HEADER=None)
    def test_forwarded_header_is_ignored_unless_configured(self):
        response = self.client.get(self.URL, HTTP_X_FORWARDED_PROTO="https")
        self.assertEqual(response.status_code, 301)


class StaticAndMediaRootTests(SimpleTestCase):
    def test_defaults(self):
        code, data = load_settings(DEBUG="True")
        self.assertEqual(code, 0, data)
        self.assertEqual(Path(data["STATIC_ROOT"]), BACKEND / "staticfiles")
        self.assertEqual(Path(data["MEDIA_ROOT"]), BACKEND / "media")  # unchanged local default

    def test_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            static, media = Path(tmp) / "static", Path(tmp) / "media"
            code, data = load_settings(DEBUG="True", STATIC_ROOT=str(static), MEDIA_ROOT=str(media))
            self.assertEqual(code, 0, data)
            self.assertEqual(Path(data["STATIC_ROOT"]), static)
            self.assertEqual(Path(data["MEDIA_ROOT"]), media)

    def test_relative_paths_are_refused(self):
        for name in ("STATIC_ROOT", "MEDIA_ROOT"):
            with self.subTest(name=name):
                code, err = load_settings(DEBUG="True", **{name: "relative/dir"})
                self.assertNotEqual(code, 0)
                self.assertIn(f"{name} must be an absolute path", err)

    def test_collectstatic_can_run(self):
        # --dry-run: proves STATIC_ROOT is usable without writing any file.
        with tempfile.TemporaryDirectory() as tmp:
            child_env = {k: v for k, v in os.environ.items() if k not in CONTROLLED}
            child_env.update(APPLYFORGE_DOTENV=str(BACKEND / "no-such-dotenv-file.env"), DEBUG="True", STATIC_ROOT=str(Path(tmp) / "static"))
            proc = subprocess.run(
                [sys.executable, "manage.py", "collectstatic", "--dry-run", "--noinput"],
                cwd=BACKEND, env=child_env, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertRegex(proc.stdout, r"\d+ static files? copied")
            self.assertFalse((Path(tmp) / "static").exists())  # dry run wrote nothing


class HstsPreloadTests(SimpleTestCase):
    def test_preload_defaults_off(self):
        code, data = load_settings(**PRODUCTION)
        self.assertEqual(code, 0, data)
        self.assertFalse(data["SECURE_HSTS_PRELOAD"])
        self.assertEqual(data["SECURE_HSTS_SECONDS"], 31536000)  # HSTS itself stays on

    def test_preload_is_opt_in(self):
        code, data = load_settings(**PRODUCTION, SECURE_HSTS_PRELOAD="True")
        self.assertEqual(code, 0, data)
        self.assertTrue(data["SECURE_HSTS_PRELOAD"])
