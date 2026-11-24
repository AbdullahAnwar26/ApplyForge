"""collect_linkedin -v 3 extraction diagnostics. Offline; fake browser; fictional content.

-v 3 prints, per post: author, headline, relative-time label and commentary
LENGTH. Nothing else, nowhere else (no DB, files, logs or --json).
"""

import io
import json
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.db import transaction
from django.test import SimpleTestCase, TestCase, override_settings

from collector.linkedin.extract import extract_posts
from collector.management.commands.collect_linkedin import format_extraction
from collector.service import extraction_summary
from tracker.models import Application, ApplicationEvent, JobOpportunity, Profile, Skill

from .linkedin_pages import (
    HIRING_TEXT,
    dvn_post_html,
    post_html,
    sdui_post_html,
    sdui_search_page,
    search_page,
    urn_for,
)
from .test_linkedin_source import FakeBrowser

KEYWORD = "python backend developer"
MARKER = "ZEBRAMARKER"  # appears only inside commentary
HIRING = dvn_post_html(
    author="Riya Fictional", relative="3h",
    headline="Talent Partner | mail head@fictional.test | +91 90000 00000 | https://www.linkedin.com/in/riya-x",
    text=f"We're hiring a Python Backend Developer at Example Labs! {MARKER} 1\n"
         "Experience: 1-3 years\nSend your CV to jobs@examplelabs.test or WhatsApp +91 98765 43210",
    slug="riya-0001", seed="a",
)
SECOND = dvn_post_html(
    author="Omar Placeholder", headline="Engineering Manager", relative="1d",
    text=f"Looking for a Django Developer in Pune. {MARKER} 2\nShare your resume: hr@placeholder.test",
    slug="omar-0002", seed="b",
)
NOT_HIRING = dvn_post_html(
    author="Sam Lee", headline="Software Engineer at Initech", relative="2w",
    text=f"Thrilled to share that I started a new position at Initech today! {MARKER} 3",
    slug="sam-0003", seed="c",
)
TOO_SHORT = dvn_post_html(author="Dana White", headline="Recruiter", relative="5m",
                          text="Hiring! DM me.", slug="dana-0004", seed="d")
SENSITIVE = ("head@fictional.test", "jobs@examplelabs.test", "hr@placeholder.test", "90000 00000",
             "98765 43210", "linkedin.com/in/", "riya-x", "riya-0001", MARKER, "Send your CV",
             "Thrilled to share")


class FormatTests(SimpleTestCase):
    def test_fields_and_masking(self):
        line = format_extraction({
            "author": "Riya\nFictional", "relative_time": "3h •", "commentary_chars": 594,
            "headline": "HR | a@b.test | +1 (415) 555-0132 | 9876543210 | https://x.test/y | www.z.test | "
                        "linkedin.com/in/someone",
        })
        self.assertEqual(line, "extracted: author='Riya Fictional' | "
                               "headline='HR | [email] | [phone] | [phone] | [link] | [link] | [link]' | "
                               "time='3h •' | commentary=594 chars")

    def test_missing_values_and_truncation(self):
        self.assertEqual(format_extraction({"author": None, "headline": "", "relative_time": None,
                                            "commentary_chars": 0}),
                         "extracted: author='-' | headline='-' | time='-' | commentary=0 chars")
        long_line = format_extraction({"author": "A" * 300, "commentary_chars": 1})
        self.assertIn("A" * 120 + "…", long_line)
        self.assertNotIn("A" * 121, long_line)

    def test_summary_never_holds_commentary_text(self):
        post = extract_posts(sdui_search_page(HIRING))[0]
        summary = extraction_summary(post)
        self.assertEqual(set(summary), {"author", "headline", "relative_time", "commentary_chars",
                                        "commentary_truncated"})
        self.assertEqual(summary["commentary_chars"], len(post.text))
        self.assertFalse(summary["commentary_truncated"])
        self.assertNotIn(MARKER, json.dumps(summary))

    def test_truncation_flag(self):
        self.assertTrue(extraction_summary(type("P", (), {"author_name": "A", "author_headline": None,
                                                           "posted_label": "1m", "text": "Cut short…  "})())
                        ["commentary_truncated"])
        self.assertIn("commentary=5 chars (ends with '…')",
                      format_extraction({"commentary_chars": 5, "commentary_truncated": True}))
        self.assertTrue(format_extraction({"commentary_chars": 5}).endswith("commentary=5 chars"))


class PostedLabelTests(SimpleTestCase):
    def test_label_from_every_layout(self):
        self.assertEqual(extract_posts(sdui_search_page(HIRING))[0].posted_label, "3h •")
        self.assertEqual(extract_posts(sdui_search_page(sdui_post_html(relative="4d")))[0].posted_label, "4d •")
        self.assertEqual(extract_posts(search_page(post_html(urn_for(seq=1), HIRING_TEXT, relative="2d")))[0]
                         .posted_label, "2d •")
        self.assertIsNone(extract_posts(sdui_search_page(sdui_post_html(relative="")))[0].posted_label)


class CommandDiagnosticsTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        profile = Profile.objects.create(name="Test User", target_roles=KEYWORD,
                                         experience_years=1.3, max_acceptable_experience_years=9)
        for name in ("Python", "Django"):
            Skill.objects.create(name=name, profile=profile)

    def call(self, *args, posts=(HIRING, SECOND, NOT_HIRING, TOO_SHORT)):
        out = io.StringIO()
        browser = FakeBrowser({KEYWORD: [sdui_search_page(*posts)]})
        with override_settings(COLLECTOR_DEBUG_DIR=Path(self.tmp) / "debug"), \
                mock.patch("collector.management.commands.collect_linkedin.default_browser", return_value=browser):
            call_command("collect_linkedin", "--keywords", KEYWORD, *args, stdout=out, stderr=io.StringIO())
        return out.getvalue()

    def diagnostic_lines(self, output):
        return [line.strip() for line in output.splitlines() if line.strip().startswith("extracted:")]

    def test_verbosity_3_prints_one_line_per_post_with_all_fields(self):
        output = self.call("-v", "3")
        lines = self.diagnostic_lines(output)
        self.assertEqual(lines, [
            "extracted: author='Riya Fictional' | "
            "headline='Talent Partner | mail [email] | [phone] | [link]' | time='3h •' | "
            f"commentary={len(extract_posts(sdui_search_page(HIRING))[0].text)} chars",
            "extracted: author='Omar Placeholder' | headline='Engineering Manager' | time='1d •' | "
            f"commentary={len(extract_posts(sdui_search_page(SECOND))[0].text)} chars",
            "extracted: author='Sam Lee' | headline='Software Engineer at Initech' | time='2w •' | "
            f"commentary={len(extract_posts(sdui_search_page(NOT_HIRING))[0].text)} chars",
            "extracted: author='Dana White' | headline='Recruiter' | time='5m •' | commentary=14 chars",
        ])
        # Each diagnostic follows its own post line, including skipped and invalid posts.
        block = output.split("\n")
        self.assertIn("[2] skipped", block[block.index(next(l for l in block if "author='Sam Lee'" in l)) - 1])
        self.assertIn("[3] invalid", block[block.index(next(l for l in block if "author='Dana White'" in l)) - 1])

    def test_lower_verbosity_prints_no_diagnostics(self):
        for level in ("0", "1", "2"):
            with self.subTest(level=level), transaction.atomic():
                output = self.call("-v", level)
                self.assertEqual(self.diagnostic_lines(output), [])
                for field in ("author=", "headline=", "time=", " chars"):
                    self.assertNotIn(field, output)
                transaction.set_rollback(True)

    def test_sensitive_values_and_commentary_never_printed(self):
        output = self.call("-v", "3")
        for secret in SENSITIVE:
            with self.subTest(secret=secret):
                self.assertNotIn(secret, output)

    def test_not_in_json_logs_or_database(self):
        with self.assertLogs("collector", level="DEBUG") as logs:
            output = self.call("-v", "3", "--json")
        data = json.loads(output)
        self.assertTrue(all("extraction" not in outcome for outcome in data["outcomes"]))
        self.assertNotIn("extracted:", output)
        log_text = "\n".join(logs.output)
        for value in ("Riya Fictional", "Talent Partner", "3h", "commentary"):
            self.assertNotIn(value, log_text)
        self.assertFalse((Path(self.tmp) / "debug").exists())
        # The headline is kept only on the job it belongs to (shown to the user for review).
        self.assertTrue(JobOpportunity.objects.filter(recruiter_headline__contains="Talent Partner").exists())

    def snapshot(self):
        return sorted(
            (j.title, j.company.name if j.company else None, j.source_url, j.screening_outcome,
             j.screening_reason, j.required_experience_text, j.full_jd,
             tuple(sorted((c.name, c.email, c.phone, c.whatsapp) for c in j.contacts.all())))
            for j in JobOpportunity.objects.all()
        )

    def test_counts_payloads_and_ingestion_unchanged(self):
        with transaction.atomic():
            quiet = self.call("-v", "1")
            quiet_snapshot = self.snapshot()
            transaction.set_rollback(True)
        verbose = self.call("-v", "3")
        self.assertIn("created 2, existing 0, skipped 1, invalid 1, failed 0", quiet)
        self.assertIn("created 2, existing 0, skipped 1, invalid 1, failed 0", verbose)
        self.assertEqual(self.snapshot(), quiet_snapshot)
        self.assertEqual(len(quiet_snapshot), 2)
        self.assertTrue(all(row[2] is None for row in quiet_snapshot))   # no invented source URLs
        self.assertEqual((Application.objects.count(), ApplicationEvent.objects.count()), (0, 0))

    def test_file_source_has_no_layout_fields(self):
        posts_file = Path(self.tmp) / "posts.json"
        posts_file.write_text(json.dumps([{"text": "We're hiring a Django Developer! Mail a@b.test today please.",
                                           "author_name": "File Author"}]), encoding="utf-8")
        out = io.StringIO()
        call_command("collect_linkedin", "--source", "file", "--file", str(posts_file), "-v", "3",
                     stdout=out, stderr=io.StringIO())
        self.assertIn("extracted: author='File Author' | headline='-' | time='-' |", out.getvalue())
