"""
Thin Playwright wrapper around a persistent Chromium profile.

* The profile directory holds the user's LinkedIn session after
  ``linkedin_login``. ApplyForge never sees or stores the password, and never
  reads or writes cookies itself.
* Plain, unmodified Chromium: no stealth plugins, user-agent or fingerprint
  changes, proxies or CAPTCHA handling. A security checkpoint stops the run.
* Pacing: a fixed pause after every navigation and scroll.
* Every Playwright failure is raised as ``BrowserError`` (a ``SourceError``), so
  a failed run is always reported as failed.
"""

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from collector.sources import SourceError

from . import selectors as S

logger = logging.getLogger("collector")

_READY_XPATH = "xpath=" + " | ".join(
    S.ANY_POST_CONTAINER + S.NO_RESULTS + S.LOGIN_FORM + S.CHECKPOINT_PAGE
)
_POSTS_XPATH = "xpath=" + " | ".join(S.ANY_POST_CONTAINER)
_PROFILE_IN_USE = ("already in use", "processsingleton", "user data directory is already")


# Safety switch. collector/tests/__init__.py sets this to False so that no test
# can ever reach the real network: with it off, a browser may only start when
# a ``route_handler`` intercepts every request (offline fixtures).
LIVE_BROWSER_ALLOWED = True


class BrowserError(SourceError):
    """The browser could not be started or a page could not be loaded."""


class BrowserClosed(BrowserError):
    """The browser window, page or context was closed (for example by the user)."""

    def __init__(self, message="The browser window was closed."):
        super().__init__(message)


@dataclass
class PageSnapshot:
    url: str
    html: str


class LinkedInBrowser:
    """Context manager: ``with LinkedInBrowser(...) as browser: browser.goto(url)``."""

    def __init__(self, profile_dir, headless=False, timeout_seconds=30.0, delay_seconds=4.0,
                 launch_args=None, sleep=time.sleep, route_handler=None):
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.timeout_ms = int(timeout_seconds * 1000)
        self.delay_seconds = delay_seconds
        self.launch_args = list(launch_args or [])
        self._sleep = sleep
        self.route_handler = route_handler  # offline tests: serves every request
        self._playwright = None
        self.context = None
        self.page = None

    # -- lifecycle ---------------------------------------------------------------
    def __enter__(self):
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        if not LIVE_BROWSER_ALLOWED and self.route_handler is None:
            raise BrowserError(
                "Live browser access is disabled (test mode). Use a fake browser or a route_handler."
            )
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._playwright = sync_playwright().start()
            self.context = self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir), headless=self.headless, args=self.launch_args
            )
            if self.route_handler is not None:
                # Installed before any page navigates, so nothing reaches the network.
                self.context.route("**/*", self.route_handler)
            self.context.set_default_timeout(self.timeout_ms)
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        except PlaywrightError as exc:
            self.__exit__(None, None, None)
            message = str(exc)
            if any(marker in message.lower() for marker in _PROFILE_IN_USE):
                raise BrowserError(
                    f"The browser profile at {self.profile_dir} is in use by another browser window. "
                    "Close it (for example a running linkedin_login) and try again."
                ) from exc
            raise BrowserError(f"Could not start Chromium: {message.splitlines()[0]}") from exc
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.context is not None:
                self.context.close()
        except Exception:  # closing must never mask the original error
            logger.debug("Ignoring error while closing the browser context", exc_info=True)
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            logger.debug("Ignoring error while stopping Playwright", exc_info=True)
        self.context = self._playwright = self.page = None
        return False

    # -- actions -----------------------------------------------------------------
    def pause(self):
        if self.delay_seconds > 0:
            self._sleep(self.delay_seconds)

    def goto(self, url):
        """Navigate, wait for a recognisable page state, pause, and return a snapshot."""
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        try:
            response = self.page.goto(url, wait_until="domcontentloaded")
        except PlaywrightTimeout as exc:
            raise BrowserError(f"Timed out loading {url}.") from exc
        except PlaywrightError as exc:
            raise BrowserError(f"Could not load {url}: {str(exc).splitlines()[0]}") from exc
        if response is not None and response.status >= 500:
            raise BrowserError(f"LinkedIn returned HTTP {response.status} for {url}.")
        try:
            self.page.wait_for_selector(_READY_XPATH, timeout=self.timeout_ms)
        except PlaywrightTimeout:
            pass  # classification of the snapshot decides what this page is
        except PlaywrightError as exc:
            raise BrowserError(f"Page failed while loading {url}: {str(exc).splitlines()[0]}") from exc
        self.pause()
        return self.snapshot()

    def snapshot(self):
        """Current URL and HTML. A closed window raises BrowserClosed, never a raw
        Playwright error; any other Playwright failure raises BrowserError."""
        from playwright.sync_api import Error as PlaywrightError

        for attempt in range(2):  # content() can race with a client-side redirect
            if self.is_closed():
                raise BrowserClosed()
            try:
                return PageSnapshot(url=self.page.url, html=self.page.content())
            except PlaywrightError as exc:
                if self.is_closed():
                    raise BrowserClosed() from exc
                if attempt == 1:
                    raise BrowserError(f"Could not read the page: {str(exc).splitlines()[0]}") from exc
            try:
                self.page.wait_for_timeout(500)
            except PlaywrightError as exc:  # closed while waiting to retry
                if self.is_closed():
                    raise BrowserClosed() from exc
                raise BrowserError(f"Could not read the page: {str(exc).splitlines()[0]}") from exc

    def scroll(self, settle_seconds=5.0):
        """Scroll down, then wait (briefly) for lazily loaded posts before pausing."""
        from playwright.sync_api import Error as PlaywrightError

        posts = self.page.locator(_POSTS_XPATH)
        try:
            before = posts.count()
            self.page.mouse.wheel(0, 2500)
            waited = 0.0
            while waited < min(settle_seconds, self.timeout_ms / 1000):
                self.page.wait_for_timeout(250)
                waited += 0.25
                if posts.count() > before:
                    break
        except PlaywrightError as exc:
            raise BrowserError(f"Scrolling failed: {str(exc).splitlines()[0]}") from exc
        self.pause()

    def expand_see_more(self, max_clicks=15):
        """Click visible "…see more" toggles (a normal reader action). Returns clicks made."""
        from playwright.sync_api import Error as PlaywrightError

        clicks = 0
        try:
            # Element handles stay valid when a click removes that post's control;
            # positional nth() would shift and skip the next post's control.
            handles = self.page.locator(f"xpath={S.SEE_MORE_BUTTON}").element_handles()
            for handle in handles[:max_clicks]:
                try:
                    if handle.is_visible():
                        handle.click(timeout=3000)
                        clicks += 1
                except PlaywrightError:
                    continue  # a toggle that cannot be clicked is not fatal
            if clicks:
                self._wait_for_expansion()
        except PlaywrightError as exc:
            raise BrowserError(f"Reading the page failed: {str(exc).splitlines()[0]}") from exc
        return clicks

    def _wait_for_expansion(self, settle_seconds=5.0):
        """Bounded wait for clicked posts to finish expanding: their "…" controls are
        gone and no post paragraph still ends with a bare "…" (text still loading)."""
        controls = self.page.locator(f"xpath={S.SEE_MORE_BUTTON}")
        pending = self.page.locator(f"xpath={S.PENDING_EXPANSION}")
        waited, limit = 0.0, min(settle_seconds, self.timeout_ms / 1000)
        while waited < limit:
            if controls.count() == 0 and pending.count() == 0:
                return
            self.page.wait_for_timeout(250)
            waited += 0.25

    def is_closed(self):
        try:
            return self.page is None or self.page.is_closed()
        except Exception:
            return True


def default_browser(headless=None):
    """A LinkedInBrowser configured from settings."""
    from django.conf import settings

    return LinkedInBrowser(
        profile_dir=settings.COLLECTOR_BROWSER_PROFILE_DIR,
        headless=settings.COLLECTOR_HEADLESS if headless is None else headless,
        timeout_seconds=settings.COLLECTOR_PAGE_TIMEOUT_SECONDS,
        delay_seconds=settings.COLLECTOR_ACTION_DELAY_SECONDS,
    )
