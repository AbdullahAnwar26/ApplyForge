"""Phase 6c: collector run history, the collector API, error handling and database isolation.

All sources are offline (StaticPostSource / the fixture file). The one test that
uses the real API source relies on the LIVE_BROWSER_ALLOWED=False guard, which
stops the browser before Chromium starts.
"""

import io
import json
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone

from collector import api as collector_api
from collector import progressive
from collector.models import CollectorRun, CollectorRunStatus, CollectorRunTrigger
from collector.posts import RawPost
from collector.runs import active_run, record_collection, status_for
from collector.service import CollectionInputError
from collector.sources import JsonFilePostSource, SourceError, StaticPostSource
from tracker.models import JobOpportunity, Profile, Skill
from tracker.screening import ScreeningInputError
from tracker.tests.helpers import APITestCase, make_user

FIXTURE = Path(__file__).parent / "fixtures" / "sample_posts.json"
POST_URL = "https://www.linkedin.com/feed/update/urn:li:activity:{}/"
RUNS_URL = "/api/v1/collector/runs/"
FICTIONAL_AUTHOR = "Priya Sharma"


def hiring_post(n, title="Python Developer"):
    return RawPost(
        text=f"We're hiring a {title} at Acme Labs!\nExperience: 2-4 years\nPython and Django. "
             f"Send your CV to jobs{n}@acme.example",
        url=POST_URL.format(7300000000000000000 + n),
        author_name=FICTIONAL_AUTHOR,
        author_headline="Talent Partner at Acme Labs",
    )


class FailingSource:
    """Yields ``posts`` and then fails like a crashed browser."""

    name = "linkedin"

    def __init__(self, posts=(), error="browser crashed"):
        self.posts, self.error = list(posts), error

    def iter_posts(self, keywords, limit):
        yield from self.posts
        raise SourceError(self.error)


def make_profile(name="Jane", target_roles="Python Developer"):
    profile = Profile.objects.create(name=name, target_roles=target_roles, experience_years=3,
                                     max_acceptable_experience_years=6)
    for skill in ("Python", "Django"):
        Skill.objects.create(name=skill, profile=profile)
    return profile


class RecordCollectionTests(TestCase):
    def setUp(self):
        self.profile = make_profile()

    def test_success_is_recorded(self):
        run, result = record_collection(JsonFilePostSource(FIXTURE), trigger=CollectorRunTrigger.COMMAND)
        run.refresh_from_db()
        self.assertEqual(run.status, CollectorRunStatus.SUCCEEDED)
        self.assertEqual(run.trigger, "command")
        self.assertEqual(run.source, "file")
        self.assertEqual(run.profile, self.profile)
        self.assertEqual(run.keywords, ["Python Developer"])
        self.assertEqual(run.limit, settings.COLLECTOR_DEFAULT_POSTS_PER_RUN)
        self.assertEqual(run.counts, result.counts)
        self.assertEqual(sorted(run.job_ids), sorted(JobOpportunity.objects.values_list("id", flat=True)))
        self.assertEqual(run.error, "")
        self.assertIsNotNone(run.finished_at)
        self.assertGreaterEqual(run.duration_seconds, 0)

    def test_existing_jobs_are_listed_on_a_repeat_run(self):
        source = StaticPostSource([hiring_post(1)])
        first, _ = record_collection(source, trigger="command")
        second, result = record_collection(source, trigger="command")
        self.assertEqual(result.counts["existing"], 1)
        self.assertEqual(second.job_ids, first.job_ids)
        self.assertEqual(second.status, CollectorRunStatus.SUCCEEDED)

    def test_failed_posts_make_the_run_partial(self):
        with mock.patch("collector.service.ingest_job", side_effect=RuntimeError("db down")):
            run, _ = record_collection(StaticPostSource([hiring_post(1)]), trigger="command")
        self.assertEqual(run.status, CollectorRunStatus.PARTIAL)
        self.assertEqual(run.counts["failed"], 1)
        self.assertEqual(run.job_ids, [])

    def test_source_error_after_posts_is_partial_and_keeps_jobs(self):
        run, _ = record_collection(FailingSource([hiring_post(1)]), trigger="command")
        self.assertEqual(run.status, CollectorRunStatus.PARTIAL)
        self.assertEqual(run.error, "browser crashed")
        self.assertEqual(len(run.job_ids), 1)
        self.assertEqual(JobOpportunity.objects.count(), 1)

    def test_source_error_before_any_post_is_failed(self):
        run, _ = record_collection(FailingSource(), trigger="command")
        self.assertEqual(run.status, CollectorRunStatus.FAILED)
        self.assertEqual(run.error, "browser crashed")
        self.assertEqual(run.counts["found"], 0)

    def test_input_errors_are_recorded_and_reraised(self):
        cases = [
            (dict(limit=0), CollectionInputError, "Limit must be"),
            (dict(keywords=["  "]), CollectionInputError, "No search keywords"),
        ]
        for kwargs, error, message in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesMessage(error, message) as ctx:
                    record_collection(StaticPostSource([hiring_post(1)]), trigger="command", **kwargs)
                run = ctx.exception.run
                run.refresh_from_db()
                self.assertEqual(run.status, CollectorRunStatus.FAILED)
                self.assertIn(message, run.error)
                self.assertIsNotNone(run.finished_at)

    def test_profile_error_is_recorded(self):
        make_profile(name="Other")
        with self.assertRaises(ScreeningInputError) as ctx:
            record_collection(StaticPostSource([]), trigger="api")
        self.assertEqual(ctx.exception.run.status, CollectorRunStatus.FAILED)
        self.assertIsNone(ctx.exception.run.profile)

    def test_unexpected_error_is_recorded_and_reraised(self):
        with mock.patch("collector.runs.run_collection", side_effect=KeyError("boom")),                 self.assertLogs("collector", "ERROR"):
            with self.assertRaises(KeyError) as ctx:
                record_collection(StaticPostSource([]), trigger="command")
        run = CollectorRun.objects.get()
        self.assertEqual(ctx.exception.run, run)
        self.assertEqual(run.status, CollectorRunStatus.FAILED)
        self.assertIn("KeyError", run.error)

    def test_run_is_running_while_the_source_is_used(self):
        seen = []

        class Watching(StaticPostSource):
            def iter_posts(self, keywords, limit):
                seen.append(CollectorRun.objects.get().status)
                yield from super().iter_posts(keywords, limit)

        run, _ = record_collection(Watching([hiring_post(1)]), trigger="command")
        self.assertEqual(seen, [CollectorRunStatus.RUNNING])
        self.assertEqual(run.status, CollectorRunStatus.SUCCEEDED)

    def test_no_post_content_is_stored_on_the_run(self):
        run, _ = record_collection(StaticPostSource([hiring_post(1)]), trigger="command")
        stored = json.dumps({f.attname: str(getattr(run, f.attname)) for f in CollectorRun._meta.fields})
        for fragment in (FICTIONAL_AUTHOR, "Talent Partner", "acme.example", "We're hiring", "linkedin.com"):
            self.assertNotIn(fragment, stored)

    def test_profile_deletion_keeps_history(self):
        run, _ = record_collection(StaticPostSource([]), trigger="command")
        self.profile.delete()
        run.refresh_from_db()
        self.assertIsNone(run.profile)

    def test_status_for(self):
        result = mock.Mock(source_error=None, outcomes=[], counts={"failed": 0})
        self.assertEqual(status_for(result), CollectorRunStatus.SUCCEEDED)

    @override_settings(COLLECTOR_STALE_RUN_MINUTES=30)
    def test_active_run_expires_stale_runs(self):
        stale = CollectorRun.objects.create(trigger="api", source="linkedin",
                                            started_at=timezone.now() - timedelta(minutes=31))
        self.assertIsNone(active_run())
        stale.refresh_from_db()
        self.assertEqual(stale.status, CollectorRunStatus.FAILED)
        self.assertIn("Abandoned", stale.error)

        fresh = CollectorRun.objects.create(trigger="api", source="linkedin")
        self.assertEqual(active_run(), fresh)


class CommandRunHistoryTests(TestCase):
    def setUp(self):
        self.profile = make_profile()

    def run_command(self, *args):
        out = io.StringIO()
        call_command("collect_linkedin", *args, stdout=out, stderr=io.StringIO())
        return out.getvalue()

    def test_command_records_a_run(self):
        output = self.run_command("--source", "file", "--file", str(FIXTURE))
        run = CollectorRun.objects.get()
        self.assertEqual((run.trigger, run.source, run.status), ("command", "file", "SUCCEEDED"))
        self.assertIn(f"Collector run {run.pk}: SUCCEEDED.", output)

    def test_json_output_includes_the_run(self):
        output = self.run_command("--source", "file", "--file", str(FIXTURE), "--json")
        data = json.loads(output)
        run = CollectorRun.objects.get()
        self.assertEqual((data["run_id"], data["run_status"]), (run.pk, "SUCCEEDED"))

    def test_input_error_is_recorded_as_failed(self):
        with self.assertRaisesMessage(CommandError, "recorded as FAILED"):
            self.run_command("--source", "file", "--file", str(FIXTURE), "--limit", "0")
        self.assertEqual(CollectorRun.objects.get().status, CollectorRunStatus.FAILED)

    def test_failed_source_is_recorded_and_still_fails_the_command(self):
        with self.assertRaisesMessage(CommandError, "Posts file not found"):
            self.run_command("--source", "file", "--file", "missing-posts.json")
        run = CollectorRun.objects.get()
        self.assertEqual(run.status, CollectorRunStatus.FAILED)
        self.assertIn("Posts file not found", run.error)

    def test_argument_errors_record_nothing(self):
        for args in (("--source", "file"), ("--source", "file", "--file", str(FIXTURE), "--profile", "999")):
            with self.subTest(args=args), self.assertRaises(CommandError):
                self.run_command(*args)
        self.assertFalse(CollectorRun.objects.exists())


class CollectorApiTests(APITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()  # throttle history
        self.addCleanup(cache.clear)
        self.profile = make_profile()
        self.other_user = make_user("other-account")
        self.source = StaticPostSource([hiring_post(1), hiring_post(2, "Backend Engineer")])
        self.source_patch = mock.patch.object(collector_api, "make_api_source", side_effect=lambda: self.source)
        self.make_source = self.source_patch.start()
        self.addCleanup(mock.patch.stopall)
        # Background runs execute inline in tests (same thread, same test transaction).
        self.launched = []

        def inline(run):
            self.launched.append(CollectorRun.objects.order_by("-id").first().pk)
            run()

        launch_patch = mock.patch.object(progressive, "launch", side_effect=inline)
        launch_patch.start()

    # --- authentication -------------------------------------------------
    def test_requires_token(self):
        self.assertEqual(self.anon.get(RUNS_URL).status_code, 401)
        self.assertEqual(self.anon.post(RUNS_URL, {}, format="json").status_code, 401)
        self.make_source.assert_not_called()

    # --- start a run -----------------------------------------------------
    def test_post_starts_a_background_collection(self):
        response = self.client.post(RUNS_URL, {"query": "Python Developer", "target": 2}, format="json")
        self.assertEqual(response.status_code, 202, response.data)
        run = CollectorRun.objects.get()
        self.assertEqual(response.data["run"]["id"], run.pk)
        self.assertEqual(self.launched, [run.pk])
        # Launched inline in tests: the run has already finished.
        self.assertEqual(response.data["run"]["status"], "SUCCEEDED")
        self.assertEqual(response.data["run"]["trigger"], "api")
        self.assertEqual(response.data["run"]["search_query"], "Python Developer")
        self.assertEqual(response.data["run"]["counts"]["created"], 2)
        self.assertEqual(response.data["run"]["remaining"], 0)
        self.assertEqual(response.data["run"]["stop_reason"], "target_reached")
        self.assertEqual(self.source.calls[0][0], ["Python Developer"])
        self.assertEqual(run.owner, self.user)

    def test_defaults(self):
        response = self.client.post(RUNS_URL, {"query": "Python Developer"}, format="json")
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(response.data["run"]["limit"], settings.COLLECTOR_API_DEFAULT_POSTS_PER_RUN)
        self.assertEqual(response.data["run"]["date_range"], "any")
        self.assertEqual(response.data["run"]["profile"], self.profile.pk)

    def test_targets_above_ten_are_accepted(self):
        for target in (11, 50, 100, 200, 500):
            with self.subTest(target=target):
                cache.clear()
                response = self.client.post(RUNS_URL, {"query": "Python", "target": target}, format="json")
                self.assertEqual(response.status_code, 202, response.data)
                self.assertEqual(response.data["run"]["limit"], target)

    @override_settings(COLLECTOR_API_MAX_POSTS_PER_RUN=300)
    def test_invalid_input_is_rejected_before_any_run(self):
        cases = [
            ({"target": 5}, "query"),
            ({"query": "   ", "target": 5}, "query"),
            ({"query": "x" * 201}, "query"),
            ({"query": "Python", "target": 301}, "target"),
            ({"query": "Python", "target": 0}, "target"),
            ({"query": "Python", "target": "x"}, "target"),
            ({"query": "Python", "date_range": "past-year"}, "date_range"),
            ({"query": "Python", "date_range": "custom"}, "date_from"),
            ({"query": "Python", "date_range": "custom", "date_from": "2999-01-01"}, "date_from"),
            ({"query": "Python", "date_range": "custom", "date_from": "2026-11-10",
              "date_to": "2026-11-01"}, "date_to"),
            ({"query": "Python", "profile": 999}, "profile"),
            ({"query": "Python", "source": "file"}, "source"),
            ({"query": "Python", "limit": 5}, "limit"),
        ]
        for body, field in cases:
            with self.subTest(body=body):
                cache.clear()  # the throttle counts rejected requests too
                response = self.client.post(RUNS_URL, body, format="json")
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.data)
        self.assertFalse(CollectorRun.objects.exists())
        self.assertEqual(self.source.calls, [])

    def test_non_object_body(self):
        response = self.client.post(RUNS_URL, ["Python"], format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(CollectorRun.objects.exists())

    def test_profile_problem_is_400_and_records_nothing(self):
        make_profile(name="Other")  # two profiles and none chosen
        response = self.client.post(RUNS_URL, {"query": "Python"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("profile", response.data)
        self.assertFalse(CollectorRun.objects.exists())
        self.assertEqual(self.source.calls, [])

    def test_explicit_profile(self):
        other = make_profile(name="Other", target_roles="Backend Engineer")
        response = self.client.post(RUNS_URL, {"query": "Backend", "profile": other.pk}, format="json")
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(response.data["run"]["profile"], other.pk)

    def test_another_accounts_profile_is_not_found(self):
        theirs = Profile.objects.create(owner=self.other_user, name="Theirs", target_roles="Dev")
        response = self.client.post(RUNS_URL, {"query": "Python", "profile": theirs.pk}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("profile", response.data)

    def test_source_failure_is_recorded_on_the_run(self):
        self.source = FailingSource([hiring_post(1)])
        response = self.client.post(RUNS_URL, {"query": "Python"}, format="json")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["run"]["status"], "PARTIAL")
        self.assertEqual(response.data["run"]["error"], "browser crashed")
        self.assertEqual(response.data["run"]["stop_reason"], "source_error")
        self.assertEqual(JobOpportunity.objects.count(), 1)  # what succeeded is kept

        cache.clear()
        self.source = FailingSource([], error="Not logged in")
        response = self.client.post(RUNS_URL, {"query": "Python"}, format="json")
        self.assertEqual(response.data["run"]["status"], "FAILED")
        self.assertEqual(response.data["run"]["error"], "Not logged in")

    def test_failed_posts_make_the_run_partial(self):
        with mock.patch("collector.service.ingest_job", side_effect=RuntimeError("db down")), \
                self.assertLogs("collector", "ERROR"):
            response = self.client.post(RUNS_URL, {"query": "Python"}, format="json")
        self.assertEqual(response.data["run"]["status"], "PARTIAL")
        self.assertEqual(response.data["run"]["counts"]["failed"], 2)

    def test_unexpected_error_is_recorded_as_failed(self):
        with mock.patch("collector.progressive._consume", side_effect=RuntimeError("secret detail")), \
                self.assertLogs("collector", "ERROR"):
            response = self.client.post(RUNS_URL, {"query": "Python"}, format="json")
        self.assertEqual(response.status_code, 202)
        run = CollectorRun.objects.get()
        self.assertEqual(run.status, CollectorRunStatus.FAILED)
        self.assertEqual(response.data["run"]["status"], "FAILED")

    def test_active_run_blocks_a_new_one(self):
        running = CollectorRun.objects.create(trigger="command", source="linkedin")
        response = self.client.post(RUNS_URL, {"query": "Python"}, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["run"], running.pk)
        self.assertEqual(self.launched, [])

    def test_another_accounts_run_blocks_without_revealing_it(self):
        CollectorRun.objects.create(trigger="api", source="linkedin", owner=self.other_user,
                                    heartbeat_at=timezone.now())
        response = self.client.post(RUNS_URL, {"query": "Python"}, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(response.data["run"])
        self.assertIn("Another collection", response.data["detail"])

    @override_settings(COLLECTOR_STALE_RUN_MINUTES=30)
    def test_stale_run_does_not_block(self):
        CollectorRun.objects.create(trigger="command", source="linkedin",
                                    started_at=timezone.now() - timedelta(hours=2))
        response = self.client.post(RUNS_URL, {"query": "Python"}, format="json")
        self.assertEqual(response.status_code, 202, response.data)

    @override_settings(COLLECTOR_HEARTBEAT_STALE_SECONDS=90)
    def test_run_without_heartbeat_is_interrupted_and_does_not_block(self):
        dead = CollectorRun.objects.create(trigger="api", source="linkedin", limit=50, search_query="Python",
                                           heartbeat_at=timezone.now() - timedelta(minutes=5),
                                           counts={"created": 12})
        response = self.client.get(f"{RUNS_URL}{dead.pk}/")
        self.assertEqual(response.data["status"], "INTERRUPTED")
        self.assertEqual(response.data["stop_reason"], "interrupted")
        self.assertEqual(response.data["remaining"], 38)
        self.assertEqual(self.client.post(RUNS_URL, {"query": "Python"}, format="json").status_code, 202)

    def test_concurrent_request_in_the_same_process_is_409(self):
        self.assertTrue(collector_api._run_lock.acquire(blocking=False))
        try:
            response = self.client.post(RUNS_URL, {"query": "Python"}, format="json")
        finally:
            collector_api._run_lock.release()
        self.assertEqual(response.status_code, 409)
        self.assertFalse(CollectorRun.objects.exists())

    def test_starting_runs_is_throttled(self):
        rates = {**settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], "collector-run": "2/min"}
        with mock.patch("rest_framework.throttling.ScopedRateThrottle.THROTTLE_RATES", rates):
            codes = [self.client.post(RUNS_URL, {"query": "Python"}, format="json").status_code
                     for _ in range(3)]
            self.assertEqual(codes, [202, 202, 429])
            # Reading history is not throttled.
            self.assertEqual([self.client.get(RUNS_URL).status_code for _ in range(3)], [200] * 3)

    def test_nothing_is_sent(self):
        from django.core import mail
        self.client.post(RUNS_URL, {"query": "Python"}, format="json")
        self.assertEqual(mail.outbox, [])

    def test_real_api_source_cannot_reach_network_in_tests(self):
        # Without the patch the API builds the real LinkedIn source; the test-mode
        # guard stops it before Chromium starts and the run is recorded as FAILED.
        self.source_patch.stop()  # restore the real make_api_source
        with mock.patch("playwright.sync_api.sync_playwright") as start:
            response = self.client.post(RUNS_URL, {"query": "Python"}, format="json")
            start.assert_not_called()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data["run"]["status"], "FAILED")
        self.assertIn("Live browser access is disabled", response.data["run"]["error"])

    # --- cancel / continue / items ----------------------------------------
    def test_cancel_a_running_run(self):
        run = CollectorRun.objects.create(trigger="api", source="linkedin", heartbeat_at=timezone.now(),
                                          limit=50, search_query="Python")
        response = self.client.post(f"{RUNS_URL}{run.pk}/cancel/", {}, format="json")
        self.assertEqual(response.status_code, 202)
        run.refresh_from_db()
        self.assertTrue(run.cancel_requested)
        # A finished run cannot be cancelled; another account's run is not found.
        run.status = CollectorRunStatus.SUCCEEDED
        run.save()
        self.assertEqual(self.client.post(f"{RUNS_URL}{run.pk}/cancel/", {}, format="json").status_code, 400)
        theirs = CollectorRun.objects.create(trigger="api", source="linkedin", owner=self.other_user,
                                             heartbeat_at=timezone.now())
        self.assertEqual(self.client.post(f"{RUNS_URL}{theirs.pk}/cancel/", {}, format="json").status_code, 404)
        theirs.refresh_from_db()
        self.assertFalse(theirs.cancel_requested)

    def test_continue_collects_only_what_is_missing(self):
        self.source = StaticPostSource([hiring_post(n) for n in range(1, 6)])
        self.client.post(RUNS_URL, {"query": "Python", "target": 2}, format="json")
        first = CollectorRun.objects.get()
        first.limit = 5  # as if it had been interrupted after 2 of 5
        first.status = CollectorRunStatus.INTERRUPTED
        first.save()
        cache.clear()
        response = self.client.post(f"{RUNS_URL}{first.pk}/continue/", {}, format="json")
        self.assertEqual(response.status_code, 202, response.data)
        second = response.data["run"]
        self.assertEqual(second["limit"], 3)
        self.assertEqual(second["search_query"], "Python")
        self.assertEqual(second["counts"]["existing"], 2)   # already collected: recognised
        self.assertEqual(second["counts"]["created"], 3)
        self.assertEqual(JobOpportunity.objects.count(), 5)
        # Nothing left: refused.
        cache.clear()
        done = CollectorRun.objects.get(pk=second["id"])
        self.assertEqual(self.client.post(f"{RUNS_URL}{done.pk}/continue/", {}, format="json").status_code, 400)

    def test_items_list_every_examined_post(self):
        self.source = StaticPostSource([hiring_post(1), RawPost(text="Lovely weather today. " * 5)])
        self.client.post(RUNS_URL, {"query": "Python", "target": 5}, format="json")
        run = CollectorRun.objects.get()
        response = self.client.get(f"{RUNS_URL}{run.pk}/items/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([i["status"] for i in response.data], ["created", "skipped"])
        self.assertEqual(response.data[0]["job"], JobOpportunity.objects.get().pk)
        filtered = self.client.get(f"{RUNS_URL}{run.pk}/items/?status=skipped")
        self.assertEqual(len(filtered.data), 1)
        self.client.force_login(self.other_user)
        self.assertEqual(self.client.get(f"{RUNS_URL}{run.pk}/items/").status_code, 404)
        self.assertEqual(self.client.get(f"{RUNS_URL}{run.pk}/").status_code, 404)
        self.assertEqual(self.client.get(RUNS_URL).data, [])

    # --- history ---------------------------------------------------------
    def _runs(self):
        older = CollectorRun.objects.create(
            trigger="command", source="file", profile=self.profile, status="SUCCEEDED",
            started_at=timezone.now() - timedelta(hours=1), finished_at=timezone.now())
        newer = CollectorRun.objects.create(trigger="api", source="linkedin", status="FAILED",
                                            error="Not logged in", finished_at=timezone.now())
        return older, newer

    def test_list_newest_first(self):
        older, newer = self._runs()
        response = self.client.get(RUNS_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r["id"] for r in response.data], [newer.pk, older.pk])
        self.assertEqual(response.data[0]["error"], "Not logged in")

    def test_list_filters(self):
        older, newer = self._runs()
        for query, expected in [
            ("status=FAILED", [newer.pk]),
            ("trigger=command", [older.pk]),
            (f"profile={self.profile.pk}", [older.pk]),
            ("status=SUCCEEDED&trigger=api", []),
        ]:
            with self.subTest(query=query):
                response = self.client.get(f"{RUNS_URL}?{query}")
                self.assertEqual([r["id"] for r in response.data], expected)
        for query in ("status=nope", "trigger=cron", "profile=0", "profile=x"):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(f"{RUNS_URL}?{query}").status_code, 400)

    def test_list_pagination(self):
        self._runs()
        response = self.client.get(f"{RUNS_URL}?page_size=1")
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(len(response.data["results"]), 1)

    def test_detail(self):
        older, _ = self._runs()
        response = self.client.get(f"{RUNS_URL}{older.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["source"], "file")
        self.assertIsNotNone(response.data["duration_seconds"])
        self.assertEqual(self.client.get(f"{RUNS_URL}999/").status_code, 404)

    def test_runs_are_read_only(self):
        older, _ = self._runs()
        url = f"{RUNS_URL}{older.pk}/"
        self.assertEqual(self.client.patch(url, {"status": "FAILED"}, format="json").status_code, 405)
        self.assertEqual(self.client.put(url, {}, format="json").status_code, 405)
        self.assertEqual(self.client.delete(url).status_code, 405)
        self.assertEqual(self.client.post(url, {}, format="json").status_code, 405)


class DatabaseIsolationTests(TestCase):
    """Tests must never run against the development or the scratch evidence database."""

    def test_tests_use_a_throwaway_database(self):
        name = str(connection.settings_dict["NAME"])
        self.assertNotEqual(Path(name).name, "db.sqlite3")
        self.assertNotIn("live-test", name)
        if connection.vendor == "sqlite":
            self.assertTrue(connection.is_in_memory_db(), name)
        else:
            self.assertTrue(Path(name).name.startswith("test_"), name)

    def test_collector_table_exists_in_the_test_database(self):
        self.assertIn(CollectorRun._meta.db_table, connection.introspection.table_names())
