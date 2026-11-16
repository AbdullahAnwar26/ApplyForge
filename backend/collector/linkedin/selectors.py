"""
Every LinkedIn URL, URL marker and page selector used by the collector.

LinkedIn changes its markup often and the historical selectors were lost, so
these are best-effort and MUST be confirmed during the live Phase 6b check.
Each element has an ordered list of fallbacks; the first match wins. Change
selectors here only.

Selectors are XPath (lxml has no CSS support without the optional
``cssselect`` package, which is not a project dependency). Playwright accepts
the same XPath strings with an ``xpath=`` prefix.
"""

from urllib.parse import urlencode

BASE_URL = "https://www.linkedin.com"
LOGIN_URL = f"{BASE_URL}/login"
FEED_URL = f"{BASE_URL}/feed/"
CONTENT_SEARCH_URL = f"{BASE_URL}/search/results/content/"


def content_search_url(keyword):
    """Post search for ``keyword``, newest first (the normal LinkedIn UI search)."""
    query = urlencode(
        {"keywords": keyword, "origin": "GLOBAL_SEARCH_HEADER", "sortBy": '"date_posted"'}
    )
    return f"{CONTENT_SEARCH_URL}?{query}"


# --- Page state, from the URL after navigation --------------------------------
# Note: LinkedIn's ordinary login form submits to /checkpoint/lg/login-submit, so
# only /checkpoint/challenge... means a security check.
LOGIN_URL_MARKERS = ("/login", "/uas/login", "/authwall", "/signup", "/start/join", "/checkpoint/lg/")
CHECKPOINT_URL_MARKERS = ("/checkpoint/challenge",)  # security verification / CAPTCHA - never automated

# Login detection (linkedin_login / --check). LinkedIn redirects logged-out
# visitors of the feed to /login or /authwall, so an https LinkedIn page at one
# of these paths, with no login form or guest markers, means "logged in".
# The global-nav marker below is an extra positive signal, not a requirement.
LINKEDIN_HOSTS = ("linkedin.com",)  # and any subdomain, e.g. www., in.
LOGGED_IN_PATHS = ("/feed",)  # exactly /feed or /feed/...


def _cls(name):
    """XPath predicate: element has CSS class ``name``."""
    return f"contains(concat(' ', normalize-space(@class), ' '), ' {name} ')"


# --- Page state, from the HTML --------------------------------------------------
LOGIN_FORM = [
    "//form[contains(@class, 'login__form')]",
    "//input[@id='username' or @name='session_key']",
]
CHECKPOINT_PAGE = [
    "//form[contains(@action, '/checkpoint/challenge')]",
    "//*[@id='captcha-internal' or @id='captchaInternalPath']",
]
NO_RESULTS = [
    f"//*[{_cls('search-reusable-search-no-results')}]",
    f"//*[{_cls('search-no-results')}]",
    f"//*[{_cls('artdeco-empty-state__headline')}]",
]
# Historically present on logged-in pages; NOT confirmed on the current live
# feed (the Phase 6b live login did not find it). Positive signal only.
LOGGED_IN_MARKER = [
    "//*[@id='global-nav']",
    f"//*[{_cls('global-nav')}]",
]
# Guest / sign-in-wall content. If any is present, login detection fails closed
# even on a feed URL (LinkedIn can serve a sign-in wall without redirecting).
# Only LinkedIn's own sign-in/join links count (relative, or on linkedin.com), so a
# feed post linking to some other site's /login page does not trigger this.
_LI_LINK = "starts-with(@href, '{path}') or contains(@href, 'linkedin.com{path}')"
GUEST_MARKERS = [
    f"//*[{_cls('authwall')} or contains(@class, 'authwall-')]",
    "//a[" + " or ".join(_LI_LINK.format(path=p) for p in ("/login", "/uas/login", "/signup", "/start/join")) + "]",
    "//form[contains(@action, 'login-submit') or contains(@action, '/uas/login')]",
]

# --- Posts ----------------------------------------------------------------------
POST_CONTAINER = [
    f"//div[{_cls('feed-shared-update-v2')} and @data-urn]",
    "//div[starts-with(@data-urn, 'urn:li:activity:') or starts-with(@data-urn, 'urn:li:ugcPost:')"
    " or starts-with(@data-urn, 'urn:li:share:')]",
    "//div[starts-with(@data-id, 'urn:li:activity:')]",
]
POST_URN_ATTRIBUTES = ("data-urn", "data-id")
# Relative to a post container (leading '.').
POST_TEXT = [
    f".//*[{_cls('update-components-text')}]",
    f".//*[{_cls('feed-shared-inline-show-more-text')}]",
    f".//*[{_cls('feed-shared-update-v2__description')}]",
    f".//*[{_cls('feed-shared-text')}]",
]
AUTHOR_NAME = [
    f".//*[{_cls('update-components-actor__title')}]//span[@aria-hidden='true']",
    f".//*[{_cls('update-components-actor__name')}]//span[@aria-hidden='true']",
    f".//*[{_cls('update-components-actor__title')}]",
    f".//*[{_cls('update-components-actor__name')}]",
]
AUTHOR_HEADLINE = [
    f".//*[{_cls('update-components-actor__description')}]//span[@aria-hidden='true']",
    f".//*[{_cls('update-components-actor__description')}]",
]
POST_TIME = [
    f".//*[{_cls('update-components-actor__sub-description')}]//span[@aria-hidden='true']",
    f".//*[{_cls('update-components-actor__sub-description')}]",
]
PERMALINK = [
    ".//a[contains(@href, '/feed/update/urn:li:')]",
    ".//a[contains(@href, '/posts/')]",
]
# --- Posts, current layout (observed in the Phase 6b live diagnostic) -------------
# LinkedIn's current search results use generated, hashed class names (e.g.
# "k4dl59") that change between builds, and expose no data-urn/data-id and no
# post permalink. Only structure is used here, never class names:
#   * each post is the parent of an accessible heading <h2>Feed post</h2>
#     (English UI label; observed 6/6 posts, and on no other element);
#   * the author is the first link to a profile (/in/<slug>) that has text (the
#     very first profile link is the empty avatar link); the name is the first
#     non-empty <p> inside it;
#   * the relative time is the first <p> whose text starts with "<n><unit>";
#   * the headline is the first other <p> in the header (before the time);
#   * the post text is the largest <p>, with <br> line breaks and an inline
#     "… more" <button> that must be ignored.
# No canonical post URL is available in this layout, so these posts have no URL
# and are de-duplicated by the JD fingerprint during ingestion.
SDUI_POST_HEADING = "//h2[normalize-space(.)='Feed post']"
SDUI_POST_CONTAINER = [f"{SDUI_POST_HEADING}/.."]
# Author links: people (/in/) and organisations (/company/).
SDUI_AUTHOR_LINK = (
    ".//a[contains(@href, 'linkedin.com/in/') or starts-with(@href, '/in/')"
    " or contains(@href, 'linkedin.com/company/') or starts-with(@href, '/company/')]"
)

# --- Posts, current layout: data-view-name hooks (preferred) ---------------------
# Confirmed by the live attribute inventory (6/6 posts, both runs): language-
# neutral identifiers, unlike the English "Feed post" heading.
#   * data-view-name="feed-full-update" - the post card (parent of the heading's
#     container); only OUTERMOST ones are posts (a reshare may nest another);
#   * data-view-name="feed-commentary"  - the post text;
#   * data-view-name="feed-actor"       - the author (name is its first non-empty <p>).
# Other observed values (feed-actor-image, feed-control-menu, reaction-button,
# feed-comment-button, feed-share-button, feed-send-as-message-button,
# relationship-building-button, edge-creation-follow-action,
# feed-actor-sub-description-cta, feed-update-image, view-likers) are not text.
# Missing hooks fall back, field by field, to the structural rules above.
DVN_POST = "feed-full-update"
DVN_POST_CONTAINER = [
    f"//*[@data-view-name='{DVN_POST}'][not(ancestor::*[@data-view-name='{DVN_POST}'])]"
]
DVN_COMMENTARY = ".//*[@data-view-name='feed-commentary']"
DVN_ACTOR = ".//*[@data-view-name='feed-actor']"

# Every post container, in any layout (used for page-ready and scroll waits).
ANY_POST_CONTAINER = POST_CONTAINER + DVN_POST_CONTAINER + SDUI_POST_CONTAINER

# The commentary-expansion ("… more") control, a normal reader click.
# Evidence (live diagnostic, 6/6 posts): the ONLY <button> inside any <p> of a
# post is this control; it sits in the post-text paragraph, has
# aria-hidden="true", and its label starts with "…". Action controls (menu,
# reactions, comment, share, send, follow) are never inside a <p> and carry a
# data-view-name. So: a button inside a paragraph of a post, structurally and
# language-neutrally - never by its English word.
_CURRENT_POST_SCOPES = f"(//*[@data-view-name='{DVN_POST}'] | {SDUI_POST_HEADING}/..)"
SEE_MORE_BUTTON = (
    f"//button[{_cls('feed-shared-inline-show-more-text__see-more-less-toggle')}"
    " and not(@aria-expanded='true')]"
    f" | {_CURRENT_POST_SCOPES}//p//button"
    "[not(@data-view-name) and not(@aria-haspopup) and not(@aria-expanded='true')"
    " and (@aria-hidden='true' or starts-with(normalize-space(.), '…'))]"
)
# After clicking, LinkedIn may load the full text asynchronously; a post
# paragraph that still ends with "…" (outside any button) is not settled yet.
PENDING_EXPANSION = (
    f"{_CURRENT_POST_SCOPES}//p"
    "[not(.//button) and substring(normalize-space(.), string-length(normalize-space(.))) = '…']"
)
