"""
``LinkedInPostSource``: searches LinkedIn posts for each keyword and yields
``RawPost`` objects through the existing ``PostSource`` interface.

Per keyword: open the post search (newest first), check the page state,
expand "see more" toggles, read the posts, and scroll (paced) until the limit
is reached, the scroll budget is used up, or a scroll adds nothing new.

Failures are never silent; each raises a ``SourceError`` subclass, which the
collection service records and the command reports as a failed run:

* login wall or expired session -> ``LoginRequired`` (run ``linkedin_login``);
* security checkpoint / CAPTCHA -> ``CheckpointRequired`` (resolve it
  manually; it is never automated);
* logged-in page whose posts cannot be found -> ``PageNotUnderstood``, with a
  sanitized diagnostic file;
* browser or network problems -> ``BrowserError``.

A keyword with genuinely no results is not an error.
"""

import logging

from django.conf import settings

from collector.sources import SourceError

from . import selectors as S
from .browser import default_browser
from .diagnostics import save_diagnostic
from .extract import PageState, classify_page, extract_posts, post_key
from .inventory import build_inventory, save_inventory

logger = logging.getLogger("collector")


class LoginRequired(SourceError):
    pass


class CheckpointRequired(SourceError):
    pass


class PageNotUnderstood(SourceError):
    pass


class NothingToInventory(SourceError):
    """Inventory-only: the search page had no posts, so no report was written."""


class _PartialCollection(Exception):
    """Internal: carries posts collected before a SourceError out of the browser block."""

    def __init__(self, posts, error):
        super().__init__(str(error))
        self.posts = posts
        self.error = error


LOGIN_HELP = "The LinkedIn session is missing or expired. Run `python manage.py linkedin_login` and log in."
CHECKPOINT_HELP = (
    "LinkedIn is showing a security check. Run `python manage.py linkedin_login`, complete the "
    "check yourself in the browser, then try again later. ApplyForge does not automate it."
)


class LinkedInPostSource:
    name = "linkedin"

    def __init__(self, browser_factory=None, max_scrolls=None, debug_dir=None, debug_files_kept=None,
                 attribute_inventory=None):
        self.browser_factory = browser_factory or default_browser
        self.max_scrolls = (
            settings.COLLECTOR_MAX_SCROLLS_PER_KEYWORD if max_scrolls is None else max_scrolls
        )
        self.debug_dir = debug_dir or settings.COLLECTOR_DEBUG_DIR
        self.debug_files_kept = (
            settings.COLLECTOR_DEBUG_FILES_KEPT if debug_files_kept is None else debug_files_kept
        )
        # Diagnostic only, off unless explicitly enabled: see linkedin/inventory.py.
        self.attribute_inventory = (
            settings.COLLECTOR_ATTRIBUTE_INVENTORY if attribute_inventory is None else attribute_inventory
        )
        self.diagnostics = []  # paths saved during the last run
        self.inventory_paths = []  # at most one inventory report per run
        self.inventory_errors = []

    def iter_posts(self, keywords, limit):
        """Collect up to ``limit`` posts, close the browser, then yield them.

        Playwright's sync API runs an asyncio event loop in this thread while the
        browser is open, and Django refuses database access while a loop is
        running. So nothing is yielded (and nothing is ingested) until the browser
        is closed. If the browser fails part-way, the posts already collected are
        yielded first and the error is raised afterwards, so partial results are
        kept and the run is still reported as failed.
        """
        collected, error = [], None
        try:
            with self.browser_factory() as browser:
                collected = self._collect(browser, keywords, limit)
        except _PartialCollection as partial:
            collected, error = partial.posts, partial.error
        yield from collected
        if error is not None:
            raise error

    def capture_inventory_only(self, keyword):
        """Inventory-only mode: ONE navigation to the first results page for ``keyword``,
        one attribute-inventory report, then close the browser. Returns the report path.

        No post parsing (no extract_posts), no scrolling, no "see more" clicks, no
        database access, and no sanitized HTML diagnostic (which would hold post text).
        Login walls, security checks, unrecognized layouts, pages without posts and
        browser errors raise a SourceError subclass; nothing is retried.
        """
        with self.browser_factory() as browser:
            snapshot = browser.goto(S.content_search_url(keyword))
        state = self._check_state(snapshot, keyword, save_unrecognized=False)
        if state is not PageState.RESULTS:
            raise NothingToInventory(
                f"The search page for '{keyword}' has no posts; no inventory was written."
            )
        report = build_inventory(snapshot.html, keyword=keyword)
        if not report["post_count"]:  # classified as results but no post containers
            raise NothingToInventory(
                f"No post containers were found for '{keyword}'; no inventory was written."
            )
        path = save_inventory(report, self.debug_dir, self.debug_files_kept)
        self.inventory_paths.append(path)
        logger.info("Inventory-only report saved: %s", path)
        return path

    # -- internals ----------------------------------------------------------------
    def _maybe_capture_inventory(self, html, keyword):
        """Write one attribute inventory per run, only when enabled. Never fatal."""
        if not self.attribute_inventory or self.inventory_paths or self.inventory_errors:
            return
        try:
            report = build_inventory(html, keyword=keyword)
            path = save_inventory(report, self.debug_dir, self.debug_files_kept)
        except Exception as exc:  # a diagnostic must never break collection
            logger.warning("Attribute inventory failed: %s", exc)
            self.inventory_errors.append(f"{type(exc).__name__}: {exc}")
            return
        logger.info("Attribute inventory saved: %s", path)
        self.inventory_paths.append(path)

    def _collect(self, browser, keywords, limit):
        seen, posts = set(), []
        try:
            for keyword in keywords:
                if len(posts) >= limit:
                    break
                for post in self._collect_keyword(browser, keyword, limit - len(posts), seen):
                    posts.append(post)
                    if len(posts) >= limit:
                        break
        except SourceError as exc:
            raise _PartialCollection(posts, exc) from exc
        return posts

    def _check_state(self, snapshot, keyword, save_unrecognized=True):
        state = classify_page(snapshot.url, snapshot.html)
        if state is PageState.LOGIN_REQUIRED:
            raise LoginRequired(LOGIN_HELP)
        if state is PageState.CHECKPOINT:
            raise CheckpointRequired(CHECKPOINT_HELP)
        if state is PageState.UNRECOGNIZED and not save_unrecognized:
            raise PageNotUnderstood(
                f"The search page for '{keyword}' loaded but no posts or 'no results' message "
                "could be found; the page layout may have changed. No diagnostic was saved "
                "in inventory-only mode."
            )
        if state is PageState.UNRECOGNIZED:
            path = save_diagnostic(
                snapshot.html, f"unrecognized search page for {keyword}", snapshot.url,
                self.debug_dir, self.debug_files_kept,
            )
            self.diagnostics.append(path)
            raise PageNotUnderstood(
                f"The search page for '{keyword}' loaded but no posts or 'no results' message "
                f"could be found; the page layout may have changed. Sanitized diagnostic: {path}"
            )
        return state

    def _collect_keyword(self, browser, keyword, remaining, seen):
        snapshot = browser.goto(S.content_search_url(keyword))
        if self._check_state(snapshot, keyword) is PageState.NO_RESULTS:
            logger.info("No posts found for keyword %r", keyword)
            return

        yielded, scrolls = 0, 0
        while True:
            browser.expand_see_more()
            snapshot = browser.snapshot()
            state = self._check_state(snapshot, keyword)  # session can expire mid-run
            new_posts = []
            if state is PageState.RESULTS:
                self._maybe_capture_inventory(snapshot.html, keyword)
                for post in extract_posts(snapshot.html):
                    key = post_key(post)
                    if key not in seen:
                        seen.add(key)
                        new_posts.append(post)
            for post in new_posts:
                yield post
                yielded += 1
                if yielded >= remaining:
                    return
            if scrolls >= self.max_scrolls or (scrolls > 0 and not new_posts):
                logger.info("Keyword %r: %s post(s) after %s scroll(s)", keyword, yielded, scrolls)
                return
            browser.scroll()
            scrolls += 1
