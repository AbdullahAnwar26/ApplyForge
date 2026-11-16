"""linkedin_login command with a fake browser. It must never ask for or store a password."""

import io
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase

from collector.linkedin.browser import BrowserError, PageSnapshot
from collector.management.commands import linkedin_login

from .linkedin_pages import AUTHWALL_PAGE, CHECKPOINT_PAGE, FEED_PAGE, FEED_PAGE_NO_NAV, LOGIN_PAGE

LOGIN = PageSnapshot(url="https://www.linkedin.com/login", html=LOGIN_PAGE)
FEED = PageSnapshot(url="https://www.linkedin.com/feed/", html=FEED_PAGE)
FEED_NO_NAV = PageSnapshot(url="https://www.linkedin.com/feed/", html=FEED_PAGE_NO_NAV)
FEED_AUTHWALL = PageSnapshot(url="https://www.linkedin.com/feed/", html=AUTHWALL_PAGE)
CHECKPOINT = PageSnapshot(url="https://www.linkedin.com/checkpoint/challenge/x", html=CHECKPOINT_PAGE)


class FakeBrowser:
    def __init__(self, first, later=(), closed_after=None):
        self.first, self.later = first, list(later)
        self.closed_after = closed_after
        self.snapshots = 0
        self.gotos = []
        self.exited = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.exited = True
        return False

    def goto(self, url):
        self.gotos.append(url)
        return self.first

    def snapshot(self):
        self.snapshots += 1
        return self.later.pop(0) if self.later else self.first

    def is_closed(self):
        return self.closed_after is not None and self.snapshots >= self.closed_after


class LoginCommandTests(SimpleTestCase):
    def setUp(self):
        # Fail loudly if anything tries to prompt for credentials.
        for target in ("builtins.input", "getpass.getpass"):
            patcher = mock.patch(target, side_effect=AssertionError(f"{target} must never be called"))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.clock_value = 0.0

        def advance(seconds):
            self.clock_value += seconds

        for name, value in (("sleep", lambda self_, s: advance(s)),
                            ("clock", lambda self_: self.clock_value)):
            patcher = mock.patch.object(linkedin_login.Command, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_login(self, browser, *args):
        out = io.StringIO()
        with mock.patch.object(linkedin_login.Command, "make_browser",
                               lambda self_, headless: self.record(browser, headless)):
            call_command("linkedin_login", *args, stdout=out, stderr=io.StringIO())
        return out.getvalue()

    def record(self, browser, headless):
        browser.headless = headless
        return browser

    def test_user_logs_in(self):
        browser = FakeBrowser(LOGIN, later=[LOGIN, LOGIN, FEED])
        output = self.run_login(browser)
        self.assertFalse(browser.headless)  # visible window
        self.assertEqual(browser.gotos, ["https://www.linkedin.com/login"])
        self.assertIn("never sees or stores your password", output)
        self.assertIn("Logged in.", output)
        self.assertTrue(browser.exited)

    def test_login_detected_on_feed_without_global_nav(self):
        # Regression for the live Phase 6b run: home page reached, no global-nav.
        browser = FakeBrowser(LOGIN, later=[LOGIN, FEED_NO_NAV])
        output = self.run_login(browser)
        self.assertIn("Logged in.", output)
        self.assertEqual(browser.snapshots, 2)

    def test_sign_in_wall_at_feed_url_is_not_login(self):
        with self.assertRaisesMessage(CommandError, "Not logged in after 10 seconds"):
            self.run_login(FakeBrowser(FEED_AUTHWALL), "--timeout", "10")

    def test_check_ok_without_global_nav(self):
        self.assertIn("LinkedIn session OK", self.run_login(FakeBrowser(FEED_NO_NAV), "--check"))

    def test_check_sign_in_wall_at_feed_url(self):
        with self.assertRaisesMessage(CommandError, "Not logged in"):
            self.run_login(FakeBrowser(FEED_AUTHWALL), "--check")

    def test_already_logged_in(self):
        output = self.run_login(FakeBrowser(FEED))
        self.assertIn("Logged in.", output)

    def test_window_closed_before_login(self):
        with self.assertRaisesMessage(CommandError, "closed before logging in"):
            self.run_login(FakeBrowser(LOGIN, closed_after=1))

    def test_snapshot_fails_because_window_closed(self):
        class Closing(FakeBrowser):
            def snapshot(self):
                self.snapshots += 1
                raise BrowserError("Target closed")

            def is_closed(self):
                return self.snapshots > 0

        with self.assertRaisesMessage(CommandError, "closed before logging in"):
            self.run_login(Closing(LOGIN))

    def test_timeout(self):
        with self.assertRaisesMessage(CommandError, "Not logged in after 10 seconds"):
            self.run_login(FakeBrowser(LOGIN), "--timeout", "10")

    def test_check_ok(self):
        browser = FakeBrowser(FEED)
        output = self.run_login(browser, "--check")
        self.assertTrue(browser.headless)
        self.assertEqual(browser.gotos, ["https://www.linkedin.com/feed/"])
        self.assertIn("LinkedIn session OK", output)

    def test_check_not_logged_in_or_checkpoint(self):
        with self.assertRaisesMessage(CommandError, "Not logged in"):
            self.run_login(FakeBrowser(LOGIN), "--check")
        with self.assertRaisesMessage(CommandError, "security check"):
            self.run_login(FakeBrowser(CHECKPOINT), "--check")

    def test_browser_start_failure(self):
        class Broken(FakeBrowser):
            def __enter__(self):
                raise BrowserError("The browser profile is in use by another browser window.")

        with self.assertRaisesMessage(CommandError, "in use"):
            self.run_login(Broken(LOGIN))
