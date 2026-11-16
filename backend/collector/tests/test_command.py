"""collect_linkedin management command tests (offline file source only)."""

import io
import json
import tempfile
from pathlib import Path
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from tracker.models import JobOpportunity, Profile, Skill

FIXTURE = str(Path(__file__).parent / "fixtures" / "sample_posts.json")


class CollectCommandTests(TestCase):
    def setUp(self):
        self.profile = Profile.objects.create(name="Jane", target_roles="Python Developer",
                                              max_acceptable_experience_years=6)
        for name in ("Python", "Django", "React"):
            Skill.objects.create(name=name, profile=self.profile)

    def run_command(self, *args, stdout=None):
        out = stdout or io.StringIO()
        call_command("collect_linkedin", *args, stdout=out, stderr=io.StringIO())
        return out.getvalue() if hasattr(out, "getvalue") else out

    def test_file_source_summary(self):
        output = self.run_command("--source", "file", "--file", FIXTURE)
        self.assertIn(f"Collected 7 post(s) from 'file' for profile {self.profile.id}", output)
        self.assertIn("created 5, existing 0, skipped 1, invalid 1, failed 0", output)
        self.assertEqual(JobOpportunity.objects.count(), 5)

    def test_verbose_lists_each_post(self):
        output = self.run_command("--source", "file", "--file", FIXTURE, "--verbosity", "2")
        self.assertIn("[0] created job", output)
        self.assertIn("Senior Python Developer SHORTLISTED", output)
        self.assertIn("[2] skipped: - No hiring cue found in the post.", output)

    def test_json_output(self):
        output = self.run_command("--source", "file", "--file", FIXTURE, "--json",
                                  "--keywords", "django, python", "--keywords", "react")
        data = json.loads(output)
        self.assertEqual(data["counts"]["created"], 5)
        self.assertEqual(data["keywords"], ["django", "python", "react"])
        self.assertEqual(len(data["outcomes"]), 7)

    def test_rerun_reports_existing(self):
        self.run_command("--source", "file", "--file", FIXTURE)
        output = self.run_command("--source", "file", "--file", FIXTURE)
        self.assertIn("created 0, existing 5", output)
        self.assertEqual(JobOpportunity.objects.count(), 5)

    def test_limit_and_profile_options(self):
        output = self.run_command("--source", "file", "--file", FIXTURE, "--limit", "2",
                                  "--profile", str(self.profile.id))
        self.assertIn("Collected 2 post(s)", output)

    def test_errors(self):
        cases = [
            (("--source", "linkedin", "--file", FIXTURE), "--file is only used with --source file"),
            (("--source", "file"), "--file is required"),
            (("--source", "file", "--file", FIXTURE, "--profile", "999"), "Profile 999 does not exist"),
            (("--source", "file", "--file", FIXTURE, "--limit", "0"), "Limit must be"),
        ]
        for args, message in cases:
            with self.subTest(args=args):
                with self.assertRaisesMessage(CommandError, message):
                    self.run_command(*args)
        self.assertEqual(JobOpportunity.objects.count(), 0)

    def test_linkedin_source_cannot_reach_network_in_tests(self):
        # The default source is the real browser; the test-mode guard must stop it
        # before Chromium starts, and the command must report a failed run.
        with mock.patch("playwright.sync_api.sync_playwright") as start:
            with self.assertRaisesMessage(CommandError, "Live browser access is disabled"):
                self.run_command()
            start.assert_not_called()
        self.assertEqual(JobOpportunity.objects.count(), 0)

    def test_any_failed_post_makes_the_run_fail(self):
        with mock.patch("collector.service.ingest_job", side_effect=RuntimeError("db down")):
            with self.assertRaisesMessage(CommandError, "5 post(s) that FAILED to ingest"):
                self.run_command("--source", "file", "--file", FIXTURE)

    def test_profile_errors_become_command_errors(self):
        Profile.objects.create(name="Other", target_roles="x")
        with self.assertRaisesMessage(CommandError, "Multiple profiles"):
            self.run_command("--source", "file", "--file", FIXTURE)

    def test_unreadable_files(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        tmp = Path(tmp_dir.name) / "posts.json"
        for content, message in (("{not json", "could not be read as JSON"),
                                 ('{"text": "x"}', "must contain a JSON list"),
                                 ('[{"text": "x", "posted_at": "yesterday"}]', "invalid posted_at")):
            tmp.write_text(content, encoding="utf-8")
            with self.subTest(content=content), self.assertRaisesMessage(CommandError, message):
                self.run_command("--source", "file", "--file", str(tmp))
        with self.assertRaisesMessage(CommandError, "Posts file not found"):
            self.run_command("--source", "file", "--file", "does-not-exist.json")

    @override_settings(COLLECTOR_MAX_POSTS_PER_RUN=50)
    def test_emoji_output_on_legacy_console_encoding(self):
        raw = io.BytesIO()
        console = io.TextIOWrapper(raw, encoding="cp1252")
        self.run_command("--source", "file", "--file", FIXTURE, "--verbosity", "2", stdout=console)
        console.flush()
        self.assertIn(b"Senior Python Developer", raw.getvalue())
