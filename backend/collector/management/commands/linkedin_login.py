"""
python manage.py linkedin_login          # open a visible browser and log in yourself
python manage.py linkedin_login --check  # report whether the saved session works

Opens Chromium with the persistent profile at COLLECTOR_BROWSER_PROFILE_DIR
(outside OneDrive by default). You type your credentials into LinkedIn's own
page. ApplyForge never asks for, reads, stores or logs your password, and never
reads cookies; the session stays in the browser profile.

Security checks (CAPTCHA, verification codes) are completed by you in the
window; nothing is automated.
"""

import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from collector.linkedin import selectors as S
from collector.linkedin.browser import BrowserClosed, BrowserError, default_browser
from collector.linkedin.extract import PageState, classify_page, is_logged_in

POLL_SECONDS = 2
WINDOW_CLOSED = "The browser window was closed before logging in."


class Command(BaseCommand):
    help = "Log in to LinkedIn yourself in a visible browser window, or --check the saved session."

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true", help="Only check whether the saved session is logged in.")
        parser.add_argument(
            "--timeout", type=int, default=None,
            help="Seconds to wait for you to log in (default COLLECTOR_LOGIN_TIMEOUT_SECONDS).",
        )

    # Indirection for tests.
    def make_browser(self, headless):
        return default_browser(headless=headless)

    def clock(self):
        return time.monotonic()

    def sleep(self, seconds):
        time.sleep(seconds)

    def handle(self, *args, **options):
        profile_dir = settings.COLLECTOR_BROWSER_PROFILE_DIR
        try:
            if options["check"]:
                self._check(profile_dir)
            else:
                timeout = options["timeout"] or settings.COLLECTOR_LOGIN_TIMEOUT_SECONDS
                self._login(profile_dir, timeout)
        except BrowserClosed:
            raise CommandError(WINDOW_CLOSED if not options["check"] else "The browser was closed during the check.")
        except BrowserError as exc:
            raise CommandError(str(exc))

    def _check(self, profile_dir):
        with self.make_browser(headless=True) as browser:
            snapshot = browser.goto(S.FEED_URL)
        if is_logged_in(snapshot.url, snapshot.html):
            self.stdout.write(f"LinkedIn session OK (profile: {profile_dir}).")
            return
        if classify_page(snapshot.url, snapshot.html) is PageState.CHECKPOINT:
            raise CommandError("LinkedIn is asking for a security check. Run linkedin_login and complete it yourself.")
        raise CommandError("Not logged in to LinkedIn. Run `python manage.py linkedin_login`.")

    def _login(self, profile_dir, timeout):
        self.stdout.write(
            "Opening a browser window. Log in to LinkedIn there yourself.\n"
            "ApplyForge never sees or stores your password; the session is kept in:\n"
            f"  {profile_dir}\n"
            f"Waiting up to {timeout} seconds. Close the window to cancel."
        )
        with self.make_browser(headless=False) as browser:
            snapshot = browser.goto(S.LOGIN_URL)
            deadline = self.clock() + timeout
            while True:
                if is_logged_in(snapshot.url, snapshot.html):
                    self.stdout.write(self.style.SUCCESS(
                        "Logged in. The session is saved; you can run collect_linkedin now."
                    ))
                    self.sleep(POLL_SECONDS)  # let LinkedIn finish writing the session
                    return
                if browser.is_closed():
                    raise CommandError(WINDOW_CLOSED)
                if self.clock() >= deadline:
                    raise CommandError(f"Not logged in after {timeout} seconds. Run linkedin_login again.")
                self.sleep(POLL_SECONDS)
                try:
                    snapshot = browser.snapshot()
                except BrowserClosed:
                    raise CommandError(WINDOW_CLOSED)
                except BrowserError:
                    if browser.is_closed():
                        raise CommandError(WINDOW_CLOSED)
                    raise
