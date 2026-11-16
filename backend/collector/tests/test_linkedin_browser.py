"""
Real Chromium, fully offline: every request is answered by a route handler from
synthetic pages and anything else is aborted, so nothing reaches LinkedIn or
the internet. Exercises the actual Playwright wrapper: persistent profile,
navigation, "see more" clicks, scrolling with lazy-loaded posts, client-side
redirects to a login wall, and network errors.

Skipped when the Playwright Chromium build is not installed.
"""

import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from django.test import TestCase

from collector.linkedin.browser import BrowserClosed, BrowserError, LinkedInBrowser
from collector.linkedin.source import LinkedInPostSource, LoginRequired
from collector.service import run_collection
from tracker.models import JobOpportunity, Profile, Skill

from .linkedin_pages import (
    LOGIN_PAGE,
    NO_RESULTS_PAGE,
    post_html,
    sdui_post_html,
    sdui_search_page,
    search_page,
    urn_for,
)

SEARCH_PREFIX = "https://www.linkedin.com/search/results/content/"
LOGIN_PREFIX = "https://www.linkedin.com/login"
# Test hygiene only: keep Chromium's own background services quiet.
QUIET_ARGS = ["--disable-background-networking", "--disable-component-update", "--no-first-run",
              "--disable-sync", "--disable-default-apps"]


def chromium_installed():
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            return os.path.exists(playwright.chromium.executable_path)
    except Exception:
        return False


CHROMIUM = chromium_installed()

# "See more" reveals the rest of the text; scrolling lazily appends a second post.
INTERACTIVE_JS = """
<script>
document.addEventListener('click', function (e) {
  var b = e.target.closest('.feed-shared-inline-show-more-text__see-more-less-toggle');
  if (!b) return;
  b.setAttribute('aria-expanded', 'true');
  var span = b.parentElement.querySelector('.update-components-text span');
  span.insertAdjacentHTML('beforeend', '<br>Full details: Python and Django, 3+ years of experience.');
});
var loaded = false;
window.addEventListener('scroll', function () {
  if (loaded) return; loaded = true;
  document.querySelector('ul').insertAdjacentHTML('beforeend', window.__LAZY_POST__);
});
</script>
<style>body { min-height: 5000px; }</style>
"""


def interactive_search_page():
    first = post_html(urn_for(seq=1), "We're hiring a Senior Python Developer at Acme Labs!\n"
                                      "Email careers@acme.example")
    lazy = post_html(urn_for(seq=2), "Hiring: Django Engineer at Globex. 2-4 years of experience. "
                                     "Send your CV to hr@globex.example").replace("`", "")
    script = f"<script>window.__LAZY_POST__ = `{lazy}`;</script>"
    return search_page(first, extra_head=script, extra_body=INTERACTIVE_JS)


AUTHWALL_REDIRECT = ("<html><body><script>location.replace('https://www.linkedin.com/authwall?trk=x');"
                     "</script></body></html>")


@unittest.skipUnless(CHROMIUM, "Playwright Chromium is not installed")
class RealChromiumOfflineTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.requests = []
        self.search_body = interactive_search_page()
        profile = Profile.objects.create(name="Jane", target_roles="python developer",
                                         max_acceptable_experience_years=6)
        for name in ("Python", "Django"):
            Skill.objects.create(name=name, profile=profile)

    def route(self, route):
        url = route.request.url
        self.requests.append(url)
        if url.startswith(SEARCH_PREFIX):
            route.fulfill(status=200, content_type="text/html", body=self.search_body)
        elif url.startswith(LOGIN_PREFIX) or "/authwall" in url:
            route.fulfill(status=200, content_type="text/html", body=LOGIN_PAGE)
        else:
            route.abort()  # nothing else is ever allowed out

    def browser(self):
        return LinkedInBrowser(
            profile_dir=Path(self.tmp) / "profile", headless=True, timeout_seconds=10,
            delay_seconds=0, launch_args=QUIET_ARGS, route_handler=self.route,
        )

    def source(self, **kwargs):
        return LinkedInPostSource(browser_factory=self.browser, debug_dir=Path(self.tmp) / "debug", **kwargs)

    def assert_offline(self):
        self.assertTrue(self.requests)
        for url in self.requests:
            self.assertTrue(url.startswith(("https://www.linkedin.com/", "data:")), url)

    def test_collects_with_see_more_and_scroll(self):
        posts = list(self.source(max_scrolls=2).iter_posts(["python developer"], 10))
        self.assertEqual(len(posts), 2)  # second post appeared only after scrolling
        self.assertIn("Full details: Python and Django, 3+ years of experience.", posts[0].text)
        self.assertEqual(posts[0].url, f"https://www.linkedin.com/feed/update/{urn_for(seq=1)}/")
        self.assertEqual(posts[1].author_name, "Priya Sharma")
        self.assert_offline()
        self.assertTrue((Path(self.tmp) / "profile").is_dir())  # persistent profile used

    def test_end_to_end_ingestion(self):
        result = run_collection(self.source(max_scrolls=2), limit=5)
        self.assertIsNone(result.source_error)
        self.assertEqual(result.counts["created"], 2)
        self.assertEqual(JobOpportunity.objects.filter(source="LINKEDIN").count(), 2)
        self.assert_offline()

    def test_client_side_redirect_to_login(self):
        self.search_body = AUTHWALL_REDIRECT
        with self.assertRaises(LoginRequired):
            list(self.source().iter_posts(["python developer"], 5))
        self.assertTrue(any("/authwall" in url for url in self.requests))

    def test_no_results(self):
        self.search_body = NO_RESULTS_PAGE
        self.assertEqual(list(self.source().iter_posts(["python developer"], 5)), [])

    def test_network_failure_is_a_browser_error(self):
        def refuse(route):
            self.requests.append(route.request.url)
            route.abort("internetdisconnected")

        browser = LinkedInBrowser(profile_dir=Path(self.tmp) / "profile", headless=True, timeout_seconds=10,
                                  delay_seconds=0, launch_args=QUIET_ARGS, route_handler=refuse)
        source = LinkedInPostSource(browser_factory=lambda: browser, debug_dir=Path(self.tmp) / "debug")
        result = run_collection(source, limit=5)
        self.assertIn("Could not load", result.source_error)
        self.assertEqual(result.counts["found"], 0)

    def test_pacing_uses_configured_delay(self):
        pauses = []
        browser = LinkedInBrowser(profile_dir=Path(self.tmp) / "profile", headless=True, timeout_seconds=10,
                                  delay_seconds=4, launch_args=QUIET_ARGS, route_handler=self.route,
                                  sleep=pauses.append)
        list(LinkedInPostSource(browser_factory=lambda: browser, max_scrolls=2,
                                debug_dir=Path(self.tmp) / "debug").iter_posts(["python developer"], 10))
        self.assertGreaterEqual(len(pauses), 2)  # after navigation and after each scroll
        self.assertTrue(all(p == 4 for p in pauses))



class StartupErrorTests(TestCase):
    """Startup failures are mapped to clear BrowserErrors (Playwright mocked; no browser)."""

    def start_with_error(self, message):
        from playwright.sync_api import Error as PlaywrightError

        playwright = mock.MagicMock()
        playwright.chromium.launch_persistent_context.side_effect = PlaywrightError(message)
        manager = mock.MagicMock()
        manager.start.return_value = playwright
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        browser = LinkedInBrowser(profile_dir=Path(tmp) / "p", route_handler=lambda route: route.abort())
        with mock.patch("playwright.sync_api.sync_playwright", return_value=manager):
            with self.assertRaises(BrowserError) as ctx:
                browser.__enter__()
        playwright.stop.assert_called_once()  # cleaned up after the failure
        return str(ctx.exception)

    def test_profile_in_use(self):
        message = self.start_with_error("Target closed... ProcessSingleton: profile is already in use")
        self.assertIn("in use by another browser window", message)
        self.assertIn("linkedin_login", message)

    def test_other_start_failure(self):
        self.assertIn("Could not start Chromium: Executable doesn't exist",
                      self.start_with_error("Executable doesn't exist at C:\\x\nmore details"))

    def test_live_access_blocked_in_tests(self):
        with mock.patch("playwright.sync_api.sync_playwright") as start:
            with self.assertRaisesMessage(BrowserError, "Live browser access is disabled"):
                LinkedInBrowser(profile_dir=Path(tempfile.gettempdir()) / "never-created").__enter__()
        start.assert_not_called()


class SnapshotClosedPageUnitTests(TestCase):
    """The exact sequence from the failed live login (Playwright mocked, no browser):
    content() fails, then the retry wait fails because the page has closed."""

    def browser_with_page(self, page):
        browser = LinkedInBrowser(profile_dir=Path(tempfile.gettempdir()) / "unused", route_handler=lambda r: None)
        browser.page = page
        return browser

    def test_retry_wait_on_closed_page_raises_browser_closed(self):
        from playwright.sync_api import Error as PlaywrightError

        state = {"closed": False}
        page = mock.MagicMock()
        page.is_closed.side_effect = lambda: state["closed"]
        page.content.side_effect = PlaywrightError("Target crashed")

        def close_then_fail(ms):
            state["closed"] = True
            raise PlaywrightError("Page.wait_for_timeout: Target page, context or browser has been closed")

        page.wait_for_timeout.side_effect = close_then_fail
        with self.assertRaises(BrowserClosed):
            self.browser_with_page(page).snapshot()

    def test_retry_wait_failure_on_open_page_is_browser_error(self):
        from playwright.sync_api import Error as PlaywrightError

        page = mock.MagicMock()
        page.is_closed.return_value = False
        page.content.side_effect = PlaywrightError("content failed")
        page.wait_for_timeout.side_effect = PlaywrightError("wait failed")
        with self.assertRaises(BrowserError) as ctx:
            self.browser_with_page(page).snapshot()
        self.assertNotIsInstance(ctx.exception, BrowserClosed)

    def test_second_content_failure_is_browser_error(self):
        from playwright.sync_api import Error as PlaywrightError

        page = mock.MagicMock()
        page.is_closed.return_value = False
        page.content.side_effect = PlaywrightError("still navigating")
        with self.assertRaisesMessage(BrowserError, "Could not read the page: still navigating"):
            self.browser_with_page(page).snapshot()
        self.assertEqual(page.content.call_count, 2)


@unittest.skipUnless(CHROMIUM, "Playwright Chromium is not installed")
class RealChromiumWindowClosedTests(TestCase):
    """Closing the window while linkedin_login polls must give a clean message.
    Real Chromium, offline: every request is served or aborted locally."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.requests = []

    def route(self, route):
        url = route.request.url
        self.requests.append(url)
        if url.startswith(LOGIN_PREFIX):
            route.fulfill(status=200, content_type="text/html", body=LOGIN_PAGE)
        else:
            route.abort()

    def browser(self):
        return LinkedInBrowser(
            profile_dir=Path(self.tmp) / "profile", headless=True, timeout_seconds=5,
            delay_seconds=0, launch_args=QUIET_ARGS, route_handler=self.route,
        )

    def test_snapshot_after_page_or_context_closed(self):
        for closer in ("page", "context"):
            with self.subTest(closed=closer), self.browser() as browser:
                browser.goto(LOGIN_PREFIX)
                getattr(browser, closer).close()
                with self.assertRaises(BrowserClosed):
                    browser.snapshot()

    def run_login_and_close(self, closer):
        from django.core.management import CommandError, call_command

        from collector.management.commands import linkedin_login

        holder = {}

        def make_browser(command, headless):
            holder["browser"] = self.browser()
            return holder["browser"]

        def close_window(command, seconds):
            getattr(holder["browser"], closer).close()  # the user closes the window while we poll

        with mock.patch.object(linkedin_login.Command, "make_browser", make_browser), \
                mock.patch.object(linkedin_login.Command, "sleep", close_window):
            with self.assertRaises(CommandError) as ctx:
                call_command("linkedin_login", "--timeout", "30", stdout=io.StringIO(), stderr=io.StringIO())
        return str(ctx.exception)

    def test_login_command_reports_window_closed(self):
        for closer in ("page", "context"):
            with self.subTest(closed=closer):
                self.assertEqual(self.run_login_and_close(closer),
                                 "The browser window was closed before logging in.")
        self.assertTrue(self.requests)
        self.assertTrue(all(url.startswith(LOGIN_PREFIX) for url in self.requests), self.requests)


# Current layout: the inline "… more" button inside the text paragraph expands
# the text; scrolling lazily appends another post. Fictional content only.
SDUI_JS = """
<script>
document.addEventListener('click', function (e) {
  var b = e.target.closest('p button');
  if (!b) return;
  b.insertAdjacentHTML('beforebegin', '<br>Stack: Python, Django, PostgreSQL. 1-3 years of experience.');
  b.remove();
});
var loaded = false;
window.addEventListener('scroll', function () {
  if (loaded) return; loaded = true;
  document.querySelector('#workspace > div:nth-of-type(2)').insertAdjacentHTML('beforeend', window.__LAZY__);
});
</script>
<style>body { min-height: 5000px; }</style>
"""


def sdui_interactive_page():
    first = sdui_post_html(author="Riya Fictional", text="We're hiring a Python Backend Developer!\n"
                                                         "Email jobs@examplelabs.test", seed="a")
    lazy = sdui_post_html(author="Omar Placeholder", relative="2d", slug="omar-0002", seed="b",
                          text="Hiring: Django Developer at Placeholder Inc. Send CV to hr@placeholder.test")
    return sdui_search_page(first, extra_head=f"<script>window.__LAZY__ = `{lazy}`;</script>",
                            extra_body=SDUI_JS)


@unittest.skipUnless(CHROMIUM, "Playwright Chromium is not installed")
class RealChromiumSduiLayoutTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.requests = []
        profile = Profile.objects.create(name="Test User", target_roles="python backend developer",
                                         max_acceptable_experience_years=9)
        for name in ("Python", "Django"):
            Skill.objects.create(name=name, profile=profile)

    def route(self, route):
        url = route.request.url
        self.requests.append(url)
        if url.startswith(SEARCH_PREFIX):
            route.fulfill(status=200, content_type="text/html", body=sdui_interactive_page())
        else:
            route.abort()

    def source(self):
        browser = LinkedInBrowser(profile_dir=Path(self.tmp) / "profile", headless=True, timeout_seconds=10,
                                  delay_seconds=0, launch_args=QUIET_ARGS, route_handler=self.route)
        return LinkedInPostSource(browser_factory=lambda: browser, max_scrolls=2,
                                  debug_dir=Path(self.tmp) / "debug")

    def test_see_more_scroll_and_ingestion(self):
        result = run_collection(self.source(), limit=5)
        self.assertIsNone(result.source_error)
        self.assertEqual(result.counts["found"], 2)          # second post appeared only after scrolling
        self.assertEqual(result.counts["created"], 2)
        first = JobOpportunity.objects.order_by("id").first()
        self.assertIn("Stack: Python, Django, PostgreSQL. 1-3 years of experience.", first.full_jd)  # "… more" clicked
        self.assertNotIn("… more", first.full_jd)
        self.assertIsNone(first.source_url)
        self.assertEqual(first.contacts.get().name, "Riya Fictional")
        self.assertFalse((Path(self.tmp) / "debug").exists())  # page understood: no diagnostic
        self.assertTrue(all(url.startswith(SEARCH_PREFIX) for url in self.requests), self.requests)
