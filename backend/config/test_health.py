"""Milestone 8.3: GET /api/v1/health/."""

import json
from unittest import mock

from django.conf import settings
from django.db import OperationalError, connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext

from tracker.models import Profile

URL = "/api/v1/health/"
SECRET_LOOKING_ERROR = (
    'connection to server at "db.internal.example" (10.1.2.3), port 5432 failed: '
    'FATAL: password authentication failed for user "applyforge_admin" password=hunter2'
)


class HealthyTests(TestCase):
    def test_healthy_response(self):
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json(), {"status": "ok", "database": "ok"})
        self.assertIn("no-store", response["Cache-Control"])  # never cached by proxies

    def test_no_authentication_needed_or_used(self):
        self.assertEqual(self.client.get(URL).status_code, 200)
        # A wrong token is simply ignored (other API routes would answer 401).
        response = self.client.get(URL, HTTP_AUTHORIZATION="Bearer wrong-token")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/v1/profiles/").status_code, 401)  # unchanged elsewhere

    def test_head_is_supported_and_writes_are_not(self):
        # enforce_csrf_checks: behave like a real client (the default test client skips CSRF).
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.head(URL).status_code, 200)
        for method in ("post", "put", "patch", "delete"):
            with self.subTest(method=method):
                response = getattr(client, method)(URL)
                self.assertEqual(response.status_code, 405)
                self.assertEqual(response["Allow"], "GET, HEAD")

    def test_only_one_read_only_query_and_no_writes(self):
        Profile.objects.create(name="Fictional Person", target_roles="Developer")
        before = Profile.objects.count()
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        statements = [q["sql"].strip().upper() for q in queries.captured_queries]
        self.assertEqual(statements, ["SELECT 1"])
        self.assertEqual(Profile.objects.count(), before)

    def test_no_user_data_in_response(self):
        Profile.objects.create(name="Fictional Person", target_roles="Developer")
        body = self.client.get(URL).content.decode()
        self.assertNotIn("Fictional", body)
        self.assertEqual(set(json.loads(body)), {"status", "database"})


class DatabaseUnavailableTests(TestCase):
    def broken_cursor(self):
        return mock.patch.object(connection, "cursor", side_effect=OperationalError(SECRET_LOOKING_ERROR))

    def test_unavailable_response(self):
        with self.broken_cursor(), self.assertLogs("django", "WARNING") as logs:
            response = self.client.get(URL)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable", "database": "unavailable"})
        self.assertIn("no-store", response["Cache-Control"])
        # The server log names the exception type only - not the driver message.
        joined = "\n".join(logs.output)
        self.assertIn("OperationalError", joined)
        self.assertNotIn("hunter2", joined)
        self.assertNotIn("db.internal.example", joined)

    def test_failure_details_never_reach_the_client(self):
        with self.broken_cursor(), self.assertLogs("django", "WARNING"):
            body = self.client.get(URL).content.decode()
        for secret in ("hunter2", "password", "db.internal.example", "10.1.2.3", "5432", "applyforge_admin",
                       "OperationalError", "Traceback", "FATAL"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, body)

    def test_os_level_failure_is_also_unavailable(self):
        with mock.patch.object(connection, "cursor", side_effect=OSError("[Errno 111] Connection refused")), \
                self.assertLogs("django", "WARNING"):
            response = self.client.get(URL)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("Errno", response.content.decode())


class NoInternalInformationTests(TestCase):
    def test_response_reveals_no_configuration(self):
        body = self.client.get(URL).content.decode()
        db = settings.DATABASES["default"]
        forbidden = [
            str(settings.BASE_DIR), str(db.get("NAME")), db["ENGINE"], "sqlite", "postgres",
            settings.SECRET_KEY, settings.API_TOKEN, "DATABASE_URL", "DEBUG", "version", "django",
        ]
        for value in forbidden:
            with self.subTest(value=value):
                self.assertNotIn(str(value).lower(), body.lower())

    def test_independent_of_optional_services(self):
        # LinkedIn, Ollama, Redis and Celery are never contacted.
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("no HTTP")), \
                mock.patch("socket.create_connection", side_effect=AssertionError("no sockets")):
            response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
