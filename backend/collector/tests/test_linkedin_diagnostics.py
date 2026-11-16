"""Diagnostic HTML must keep layout clues and never contain session or token data."""

import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from collector.linkedin.diagnostics import FILE_PREFIX, sanitize_html, save_diagnostic

from .linkedin_pages import UNRECOGNIZED_PAGE

SECRETS = (
    "ajax:1234567890123456789", "ajax:9876543210987654321", "JSESSIONID", "li_at",
    "AQEDAR-secret-session-token-value-that-must-not-leak", "csrf-token", "csrfToken", "window.__SESSION__",
)


class SanitizeTests(SimpleTestCase):
    def test_removes_scripts_code_meta_inputs_and_tokens(self):
        cleaned = sanitize_html(UNRECOGNIZED_PAGE)
        for secret in SECRETS:
            with self.subTest(secret=secret):
                self.assertNotIn(secret, cleaned)
        for tag in ("<script", "<code", "<meta", "<input"):
            self.assertNotIn(tag, cleaned)

    def test_keeps_layout_clues(self):
        cleaned = sanitize_html(UNRECOGNIZED_PAGE)
        self.assertIn('class="brand-new-layout"', cleaned)
        self.assertIn("We are hiring Python developers", cleaned)
        self.assertIn('id="global-nav"', cleaned)

    def test_attributes_and_hrefs(self):
        html = ('<div class="a" data-urn="urn:li:activity:1" data-tracking="x" style="y" onclick="z">'
                '<a href="https://www.linkedin.com/feed/update/urn:li:activity:1/?trk=abc&token=SECRET#c">p</a></div>')
        cleaned = sanitize_html(html)
        self.assertIn('data-urn="urn:li:activity:1"', cleaned)
        self.assertIn('href="https://www.linkedin.com/feed/update/urn:li:activity:1/"', cleaned)
        for dropped in ("data-tracking", "style=", "onclick", "SECRET", "trk="):
            self.assertNotIn(dropped, cleaned)

    def test_redacts_token_like_text(self):
        cleaned = sanitize_html(
            "<p>JSESSIONID=ajax:123456789 li_at=AQED-xyz Bearer abc.def token: zzz "
            + "Q" * 80 + "</p>"
        )
        self.assertNotIn("ajax:123456789", cleaned)
        self.assertNotIn("AQED-xyz", cleaned)
        self.assertNotIn("Q" * 80, cleaned)
        self.assertIn("[REDACTED]", cleaned)


class SaveTests(SimpleTestCase):
    def test_save_and_prune(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = [
                save_diagnostic(UNRECOGNIZED_PAGE, f"Unrecognized page {i}!", "https://www.linkedin.com/x?token=t",
                                Path(tmp) / "debug", keep=3)
                for i in range(5)
            ]
            remaining = sorted(p.name for p in (Path(tmp) / "debug").glob(f"{FILE_PREFIX}*.html"))
            self.assertEqual(remaining, sorted(p.name for p in paths[-3:]))
            content = paths[-1].read_text(encoding="utf-8")
            self.assertIn("Reason: Unrecognized page 4!", content)
            self.assertIn("URL: https://www.linkedin.com/x.", content)  # query string dropped
            self.assertNotIn("token=t", content)
            for secret in SECRETS:
                self.assertNotIn(secret, content)
            self.assertIn("unrecognized-page-4", paths[-1].name)


class SameTickNamingTests(SimpleTestCase):
    def test_same_tick_diagnostics_never_overwrite_and_prune_keeps_newest(self):
        from datetime import datetime as real_datetime
        from unittest import mock

        frozen = real_datetime(2026, 11, 16, 12, 0, 0, 123456)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("collector.linkedin.diagnostics.datetime") as clock:
            clock.now.return_value = frozen
            paths = [save_diagnostic(f"<p>page {i}</p>", "same reason", "https://www.linkedin.com/x",
                                     Path(tmp), keep=2) for i in range(4)]
            self.assertEqual(len({p.name for p in paths}), 4)
            remaining = sorted(p.name for p in Path(tmp).glob(f"{FILE_PREFIX}*.html"))
            self.assertEqual(remaining, [paths[2].name, paths[3].name])
            self.assertIn("page 3", paths[3].read_text(encoding="utf-8"))
