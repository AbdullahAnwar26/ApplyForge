"""LinkedInPostSource with a scripted fake browser (no Playwright, no network)."""

import io
import tempfile
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from collector.linkedin.browser import BrowserError, PageSnapshot
from collector.linkedin.source import CheckpointRequired, LinkedInPostSource, LoginRequired, PageNotUnderstood
from collector.service import run_collection
from tracker.models import JobOpportunity, Profile, Skill

from .linkedin_pages import (
    CHECKPOINT_PAGE,
    HIRING_TEXT,
    LOGIN_PAGE,
    NO_RESULTS_PAGE,
    NOT_HIRING_TEXT,
    REACT_TEXT,
    UNRECOGNIZED_PAGE,
    post_html,
    search_page,
    urn_for,
)

SEARCH = "https://www.linkedin.com/search/results/content/"


def hiring(n, role="Python Developer"):
    return post_html(urn_for(seq=n), f"We're hiring a {role} at Acme {n}!\nExperience: 2-4 years\n"
                                     f"Python and Django. Send your CV to jobs{n}@acme.example")


class FakeBrowser:
    """Scripted pages per keyword: pages[keyword] = [snapshot after 0 scrolls, after 1, ...]."""

    def __init__(self, pages, fail_on=None):
        self.pages = pages
        self.fail_on = fail_on or {}
        self.events = []
        self.keyword = None
        self.scrolls = 0

    def __enter__(self):
        self.events.append("enter")
        return self

    def __exit__(self, *exc):
        self.events.append("exit")
        return False

    def _current(self):
        value = self.pages[self.keyword][min(self.scrolls, len(self.pages[self.keyword]) - 1)]
        return value if isinstance(value, PageSnapshot) else PageSnapshot(url=self.url, html=value)

    def goto(self, url):
        self.events.append(("goto", url))
        self.url = url
        self.keyword = parse_qs(urlsplit(url).query)["keywords"][0]
        self.scrolls = 0
        if ("goto", self.keyword) in self.fail_on:
            raise BrowserError(self.fail_on[("goto", self.keyword)])
        return self._current()

    def snapshot(self):
        return self._current()

    def scroll(self):
        self.events.append("scroll")
        self.scrolls += 1

    def expand_see_more(self, max_clicks=15):
        self.events.append("see_more")
        return 0


def source_for(browser, tmp, max_scrolls=3):
    return LinkedInPostSource(browser_factory=lambda: browser, max_scrolls=max_scrolls,
                              debug_dir=Path(tmp) / "debug", debug_files_kept=5)


class SourceTestCase(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.profile = Profile.objects.create(name="Jane", target_roles="python developer",
                                              max_acceptable_experience_years=6)
        for name in ("Python", "Django", "React"):
            Skill.objects.create(name=name, profile=self.profile)


class CollectTests(SourceTestCase):
    def test_collects_across_scrolls_and_keywords(self):
        browser = FakeBrowser({
            "python developer": [search_page(hiring(1), hiring(2)),
                                 search_page(hiring(1), hiring(2), hiring(3))],
            "django": [search_page(hiring(2), hiring(4))],  # post 2 already seen
        })
        posts = list(source_for(browser, self.tmp).iter_posts(["python developer", "django"], 10))
        self.assertEqual([p.url.rsplit(":", 1)[1].rstrip("/") for p in posts],
                         [urn_for(seq=n).rsplit(":", 1)[1] for n in (1, 2, 3, 4)])
        self.assertEqual(browser.events[0], "enter")
        self.assertEqual(browser.events[-1], "exit")
        self.assertIn(("goto", SEARCH + "?keywords=python+developer&origin=GLOBAL_SEARCH_HEADER"
                               "&sortBy=%22date_posted%22"), browser.events)

    def test_stops_scrolling_when_nothing_new_and_at_max(self):
        browser = FakeBrowser({"k": [search_page(hiring(1))]})
        list(source_for(browser, self.tmp, max_scrolls=5).iter_posts(["k"], 10))
        self.assertEqual(browser.events.count("scroll"), 1)  # one scroll added nothing -> stop

        endless = FakeBrowser({"k": [search_page(*(hiring(n) for n in range(i + 1))) for i in range(20)]})
        list(source_for(endless, self.tmp, max_scrolls=3).iter_posts(["k"], 50))
        self.assertEqual(endless.events.count("scroll"), 3)

    def test_limit_stops_early_and_closes_browser(self):
        browser = FakeBrowser({"k": [search_page(*(hiring(n) for n in range(10)))]})
        iterator = source_for(browser, self.tmp).iter_posts(["k", "other"], 3)
        self.assertEqual(len(list(iterator)), 3)
        self.assertNotIn("scroll", browser.events)
        self.assertEqual(browser.events[-1], "exit")

    def test_no_results_keyword_is_not_an_error(self):
        browser = FakeBrowser({"none": [NO_RESULTS_PAGE], "k": [search_page(hiring(1))]})
        posts = list(source_for(browser, self.tmp).iter_posts(["none", "k"], 10))
        self.assertEqual(len(posts), 1)


class FailureTests(SourceTestCase):
    def run_source(self, browser):
        return list(source_for(browser, self.tmp).iter_posts(["k"], 10))

    def test_login_required(self):
        for page in (LOGIN_PAGE, PageSnapshot(url="https://www.linkedin.com/authwall?x", html="<html></html>")):
            browser = FakeBrowser({"k": [page]})
            with self.subTest(page=str(page)[:30]):
                with self.assertRaisesMessage(LoginRequired, "linkedin_login"):
                    self.run_source(browser)
                self.assertEqual(browser.events[-1], "exit")

    def test_session_expires_mid_run(self):
        browser = FakeBrowser({"k": [search_page(hiring(1)), LOGIN_PAGE]})
        iterator = source_for(browser, self.tmp).iter_posts(["k"], 10)
        self.assertEqual(len([next(iterator)]), 1)
        with self.assertRaises(LoginRequired):
            next(iterator)

    def test_checkpoint_is_never_automated(self):
        browser = FakeBrowser({"k": [CHECKPOINT_PAGE]})
        with self.assertRaisesMessage(CheckpointRequired, "does not automate"):
            self.run_source(browser)

    def test_unrecognized_page_saves_sanitized_diagnostic(self):
        browser = FakeBrowser({"k": [UNRECOGNIZED_PAGE]})
        source = source_for(browser, self.tmp)
        with self.assertRaises(PageNotUnderstood) as ctx:
            list(source.iter_posts(["k"], 10))
        self.assertEqual(len(source.diagnostics), 1)
        path = source.diagnostics[0]
        self.assertIn(str(path), str(ctx.exception))
        content = path.read_text(encoding="utf-8")
        self.assertIn("brand-new-layout", content)
        for secret in ("ajax:1234567890123456789", "li_at", "JSESSIONID", "AQEDAR"):
            self.assertNotIn(secret, content)

    def test_no_diagnostic_for_login_or_no_results(self):
        for page in (LOGIN_PAGE, NO_RESULTS_PAGE):
            source = source_for(FakeBrowser({"k": [page]}), self.tmp)
            try:
                list(source.iter_posts(["k"], 10))
            except LoginRequired:
                pass
            self.assertEqual(source.diagnostics, [])
        self.assertFalse((Path(self.tmp) / "debug").exists())

    def test_browser_error_propagates_as_source_error(self):
        browser = FakeBrowser({"k": [search_page()]}, fail_on={("goto", "k"): "Timed out loading x"})
        with self.assertRaisesMessage(BrowserError, "Timed out"):
            self.run_source(browser)
        self.assertEqual(browser.events[-1], "exit")


class ServiceIntegrationTests(SourceTestCase):
    def test_run_collection_ingests_posts(self):
        browser = FakeBrowser({"python developer": [search_page(
            post_html(urn_for(seq=1), HIRING_TEXT), post_html(urn_for(seq=2), REACT_TEXT),
            post_html(urn_for(seq=3), NOT_HIRING_TEXT),
        )]})
        result = run_collection(source_for(browser, self.tmp))
        self.assertEqual(result.counts["created"], 2)
        self.assertEqual(result.counts["skipped"], 1)
        self.assertIsNone(result.source_error)
        job = JobOpportunity.objects.get(title="Senior Python Developer")
        self.assertEqual(job.source, "LINKEDIN")
        self.assertEqual(job.source_url, f"https://www.linkedin.com/feed/update/{urn_for(seq=1)}/")
        self.assertIsNotNone(job.posted_date)
        self.assertEqual(job.contacts.get().name, "Priya Sharma")
        self.assertEqual(browser.events[-1], "exit")

    def test_failure_is_reported_with_partial_results(self):
        browser = FakeBrowser({"python developer": [search_page(hiring(1)), LOGIN_PAGE]})
        result = run_collection(source_for(browser, self.tmp))
        self.assertEqual(result.counts["created"], 1)
        self.assertIn("linkedin_login", result.source_error)
        self.assertEqual(browser.events[-1], "exit")

    def test_limit_closes_browser_via_service(self):
        browser = FakeBrowser({"python developer": [search_page(*(hiring(n) for n in range(10)))]})
        run_collection(source_for(browser, self.tmp), limit=2)
        self.assertEqual(browser.events[-1], "exit")


class CommandTests(SourceTestCase):
    def setUp(self):
        super().setUp()
        override = override_settings(COLLECTOR_DEBUG_DIR=Path(self.tmp) / "debug")
        override.enable()
        self.addCleanup(override.disable)

    def call(self, browser, *args):
        out = io.StringIO()
        with mock.patch("collector.management.commands.collect_linkedin.default_browser",
                        return_value=browser) as factory:
            call_command("collect_linkedin", *args, stdout=out, stderr=io.StringIO())
        return out.getvalue(), factory

    def test_success(self):
        browser = FakeBrowser({"python developer": [search_page(hiring(1), hiring(2))]})
        output, factory = self.call(browser)
        self.assertIn("created 2", output)
        factory.assert_called_once_with(headless=None)

    def test_headless_flag(self):
        browser = FakeBrowser({"python developer": [search_page(hiring(1))]})
        _, factory = self.call(browser, "--headless")
        factory.assert_called_once_with(headless=True)

    def test_failed_run_exits_with_error_not_success(self):
        browser = FakeBrowser({"python developer": [search_page(hiring(1)), LOGIN_PAGE]})
        with self.assertRaisesMessage(CommandError, "Collection FAILED after 1 post(s)"):
            self.call(browser)
        self.assertEqual(JobOpportunity.objects.count(), 1)  # what was collected is kept
