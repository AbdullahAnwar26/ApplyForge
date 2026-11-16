"""
collect_linkedin --inventory-only: one results page, one inventory report, exit.

Offline only (fake browser, or real Chromium with every request served locally).
Command tests are SimpleTestCase, so ANY database query raises - that is how
"no database access" is enforced, not just asserted. Post parsing, payload
building, validation, run recording, run_collection and ingest_job are additionally replaced
by tripwires that fail the test if called.
"""

import io
import json
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, override_settings

from collector.linkedin import selectors as S
from collector.linkedin.browser import BrowserError, LinkedInBrowser, PageSnapshot
from collector.linkedin.inventory import FILE_PREFIX
from collector.linkedin.source import (
    CheckpointRequired,
    LinkedInPostSource,
    LoginRequired,
    NothingToInventory,
    PageNotUnderstood,
)

from .linkedin_pages import (
    AUTHWALL_PAGE,
    CHECKPOINT_PAGE,
    LOGIN_PAGE,
    NO_RESULTS_PAGE,
    UNRECOGNIZED_PAGE,
    sdui_post_html,
    sdui_search_page,
    urn_for,
)
from .test_linkedin_browser import CHROMIUM, QUIET_ARGS, SEARCH_PREFIX
from .test_linkedin_inventory import SECRET_STRINGS, page
from .test_linkedin_source import FakeBrowser

KEYWORD = "Python Backend Developer"
POST = urn_for(seq=44)
RESULTS = page(container_attrs=f'componentkey="expanded{POST}FeedType_MAIN"')
TRIPWIRES = (
    "collector.management.commands.collect_linkedin.record_collection",
    "collector.runs.record_collection",
    "collector.runs.run_collection",
    "collector.service.run_collection",
    "collector.service.ingest_job",
    "tracker.ingestion.ingest_job",
    "collector.linkedin.source.extract_posts",
    "collector.linkedin.extract.extract_posts",
    "collector.service.build_payload",
    "collector.posts.build_payload",
    "collector.service.JobIngestSerializer",
)


def tripwires(stack):
    for target in TRIPWIRES:
        stack.enter_context(mock.patch(target, side_effect=AssertionError(f"{target} must not be called")))


class InventoryOnlySourceTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.debug = Path(self.tmp) / "debug"

    def source(self, browser):
        return LinkedInPostSource(browser_factory=lambda: browser, debug_dir=self.debug, attribute_inventory=True)

    def files(self):
        return sorted(self.debug.iterdir()) if self.debug.exists() else []

    def test_one_navigation_one_report_then_exit(self):
        browser = FakeBrowser({KEYWORD: [RESULTS]})
        with ExitStack() as stack:
            tripwires(stack)
            path = self.source(browser).capture_inventory_only(KEYWORD)
        self.assertEqual(browser.events, ["enter", ("goto", S.content_search_url(KEYWORD)), "exit"])
        self.assertEqual(self.files(), [path])                   # exactly one file, the report
        self.assertTrue(path.name.startswith(FILE_PREFIX) and path.suffix == ".json")
        report = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((report["keyword"], report["post_count"]), (KEYWORD, 1))
        self.assertEqual(report["posts"][0]["distinct_post_urns"], [POST])
        text = path.read_text(encoding="utf-8")
        for secret in SECRET_STRINGS + ("linkedin.com/in/",):
            self.assertNotIn(secret, text)

    def test_failures_write_nothing(self):
        cases = [
            (LOGIN_PAGE, LoginRequired),
            (PageSnapshot(url="https://www.linkedin.com/authwall?x", html=AUTHWALL_PAGE), LoginRequired),
            (CHECKPOINT_PAGE, CheckpointRequired),
            (PageSnapshot(url="https://www.linkedin.com/checkpoint/challenge/x", html=RESULTS), CheckpointRequired),
            (UNRECOGNIZED_PAGE, PageNotUnderstood),
            (NO_RESULTS_PAGE, NothingToInventory),
        ]
        for snapshot, error in cases:
            with self.subTest(error=error.__name__, page=str(snapshot)[:40]):
                browser = FakeBrowser({KEYWORD: [snapshot]})
                with self.assertRaises(error):
                    self.source(browser).capture_inventory_only(KEYWORD)
                self.assertEqual(browser.events[-1], "exit")
                self.assertEqual(browser.events.count("scroll") + browser.events.count("see_more"), 0)
                self.assertEqual(self.files(), [])           # no report and no HTML diagnostic

    def test_browser_error_is_not_retried(self):
        browser = FakeBrowser({KEYWORD: [RESULTS]}, fail_on={("goto", KEYWORD): "Timed out loading x"})
        with self.assertRaisesMessage(BrowserError, "Timed out"):
            self.source(browser).capture_inventory_only(KEYWORD)
        self.assertEqual([e for e in browser.events if isinstance(e, tuple)], [("goto", S.content_search_url(KEYWORD))])
        self.assertEqual(self.files(), [])


@override_settings(COLLECTOR_ATTRIBUTE_INVENTORY=False)
class InventoryOnlyCommandTests(SimpleTestCase):
    """SimpleTestCase: any database query in these tests raises."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.debug = Path(self.tmp) / "debug"

    def call(self, browser, *args):
        out = io.StringIO()
        with ExitStack() as stack:
            tripwires(stack)
            stack.enter_context(override_settings(COLLECTOR_DEBUG_DIR=self.debug))
            factory = stack.enter_context(mock.patch(
                "collector.management.commands.collect_linkedin.default_browser", return_value=browser))
            call_command("collect_linkedin", *args, stdout=out, stderr=io.StringIO())
        return out.getvalue(), factory

    def files(self):
        return sorted(self.debug.iterdir()) if self.debug.exists() else []

    def test_success(self):
        browser = FakeBrowser({KEYWORD: [RESULTS]})
        output, factory = self.call(browser, "--inventory-only", "--keywords", KEYWORD)
        factory.assert_called_once_with(headless=None)           # visible by default
        self.assertEqual(browser.events, ["enter", ("goto", S.content_search_url(KEYWORD)), "exit"])
        self.assertEqual(len(self.files()), 1)
        self.assertIn(f"Inventory-only report saved: {self.files()[0]}", output)
        self.assertIn("layout current, 1 post(s), 1 with public post URNs; attributes carrying post URNs: componentkey",
                      output)
        self.assertIn("No posts were parsed or ingested; no database access.", output)
        for secret in SECRET_STRINGS:
            self.assertNotIn(secret, output)

    def test_headless_and_redundant_attribute_inventory_flags(self):
        browser = FakeBrowser({KEYWORD: [RESULTS]})
        _, factory = self.call(browser, "--inventory-only", "--attribute-inventory", "--headless", "--keywords", KEYWORD)
        factory.assert_called_once_with(headless=True)
        self.assertEqual(len(self.files()), 1)

    def test_safe_failures(self):
        cases = [
            (LOGIN_PAGE, "linkedin_login"),
            (CHECKPOINT_PAGE, "does not automate"),
            (UNRECOGNIZED_PAGE, "No diagnostic was saved"),
            (NO_RESULTS_PAGE, "has no posts; no inventory was written"),
        ]
        for snapshot, message in cases:
            with self.subTest(message=message):
                browser = FakeBrowser({KEYWORD: [snapshot]})
                with self.assertRaisesMessage(CommandError, f"Inventory-only FAILED: "):
                    try:
                        self.call(browser, "--inventory-only", "--keywords", KEYWORD)
                    except CommandError as exc:
                        self.assertIn(message, str(exc))
                        raise
                self.assertEqual(browser.events[-1], "exit")
                self.assertEqual(self.files(), [])

    def test_browser_error(self):
        browser = FakeBrowser({KEYWORD: [RESULTS]}, fail_on={("goto", KEYWORD): "Could not load x"})
        with self.assertRaisesMessage(CommandError, "Inventory-only FAILED: Could not load x"):
            self.call(browser, "--inventory-only", "--keywords", KEYWORD)
        self.assertEqual(self.files(), [])

    def test_argument_validation_happens_before_any_browser(self):
        cases = [
            (("--inventory-only", "--source", "file", "--keywords", KEYWORD), "only used with --source linkedin"),
            (("--inventory-only", "--file", "x.json", "--keywords", KEYWORD), "only used with --source linkedin"),
            (("--inventory-only",), "exactly one keyword"),
            (("--inventory-only", "--keywords", "a", "--keywords", "b"), "exactly one keyword"),
            (("--inventory-only", "--keywords", "a, b"), "exactly one keyword"),
            (("--inventory-only", "--keywords", " , "), "exactly one keyword"),
            (("--inventory-only", "--keywords", KEYWORD, "--profile", "1"), "--profile is not used"),
            (("--inventory-only", "--keywords", KEYWORD, "--limit", "5"), "--limit is not used"),
            (("--inventory-only", "--keywords", KEYWORD, "--json"), "--json is not used"),
        ]
        for args, message in cases:
            with self.subTest(args=args):
                browser = FakeBrowser({KEYWORD: [RESULTS]})
                with self.assertRaisesMessage(CommandError, message):
                    self.call(browser, *args)
                self.assertEqual(browser.events, [])          # browser never started
                self.assertEqual(self.files(), [])


@unittest.skipUnless(CHROMIUM, "Playwright Chromium is not installed")
class RealChromiumInventoryOnlyTests(SimpleTestCase):
    """Real Chromium, offline: requests are served locally or aborted; temp profile only."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.requests = []
        self.body = sdui_search_page(
            sdui_post_html(seed="a").replace('id="[REDACTED]"', f'componentkey="expanded{POST}FeedType_MAIN"'),
            extra_body="<script>window.addEventListener('scroll', function () {"
                       " document.body.insertAdjacentHTML('beforeend', '<p id=scrolled>x</p>'); });</script>",
        )

    def route(self, route):
        url = route.request.url
        self.requests.append(url)
        if url.startswith(SEARCH_PREFIX):
            route.fulfill(status=200, content_type="text/html", body=self.body)
        elif "/authwall" in url or url.startswith(S.LOGIN_URL):
            route.fulfill(status=200, content_type="text/html", body=LOGIN_PAGE)
        else:
            route.abort()

    def source(self):
        browser = LinkedInBrowser(profile_dir=Path(self.tmp) / "profile", headless=True, timeout_seconds=10,
                                  delay_seconds=0, launch_args=QUIET_ARGS, route_handler=self.route)
        return LinkedInPostSource(browser_factory=lambda: browser, debug_dir=Path(self.tmp) / "debug",
                                  attribute_inventory=True)

    def test_single_page_report(self):
        with ExitStack() as stack:
            tripwires(stack)
            path = self.source().capture_inventory_only(KEYWORD)
        report = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(report["posts"][0]["distinct_post_urns"], [POST])
        self.assertEqual(report["attribute_names_carrying_post_urns"], {"componentkey": 1})
        self.assertEqual(self.requests, [S.content_search_url(KEYWORD)])   # one navigation, nothing else
        self.assertEqual(sorted(p.name for p in (Path(self.tmp) / "debug").iterdir()), [path.name])

    def test_redirect_to_login_wall(self):
        self.body = ("<html><body><script>location.replace('https://www.linkedin.com/authwall?trk=x');"
                     "</script></body></html>")
        with self.assertRaises(LoginRequired):
            self.source().capture_inventory_only(KEYWORD)
        self.assertFalse((Path(self.tmp) / "debug").exists())
