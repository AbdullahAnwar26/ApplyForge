"""
Pure extraction from LinkedIn page HTML (no browser, no network).

``classify_page`` decides what a loaded page is (results, no results, login
wall, security checkpoint, or unrecognized). ``extract_posts`` turns a results
page into ``RawPost`` objects. Both take plain HTML, so they are tested with
synthetic pages.
"""

import re
from datetime import datetime, timedelta, timezone
from enum import Enum
from urllib.parse import urlsplit

from lxml import html as lxml_html

from collector.posts import RawPost
from tracker.ingestion import normalize_source_url

from . import selectors as S

_URN = re.compile(r"urn:li:(activity|ugcPost|share):(\d+)", re.I)
_CANONICAL_TYPES = {"activity": "activity", "ugcpost": "ugcPost", "share": "share"}
_RELATIVE_TIME = re.compile(r"^\s*(\d+)\s*(s|m|h|d|w|mo|yr|y)\b", re.I)
# A whole paragraph that IS a time label: "1m", "3h •", "2w • Edited". Not a
# headline that merely starts with a number ("3D Artist", "5m followers").
_TIME_LABEL = re.compile(r"^\s*\d+\s*(?:s|m|h|d|w|mo|yr|y)\s*(?:[•·].*)?$", re.I)
_DEGREE_MARKER = re.compile(r"^\s*[•·]")  # "• 2nd", "• 3rd+", "• Following"
# Name and degree in ONE paragraph ("Fictional Person • 3rd+"), seen in the live run.
_NAME_WITH_DEGREE = re.compile(r"^(?P<name>[^•·]+?)\s*[•·]\s*\d+[^\s•·]{0,4}\+?\s*$")
_RELATIVE_UNITS = {
    "s": timedelta(seconds=1), "m": timedelta(minutes=1), "h": timedelta(hours=1),
    "d": timedelta(days=1), "w": timedelta(weeks=1), "mo": timedelta(days=30),
    "yr": timedelta(days=365), "y": timedelta(days=365),
}
_SEE_MORE_TAIL = re.compile(r"\s*(?:…|\.\.\.)?\s*(?:see|show)\s+(?:more|less)\s*$", re.I)
_HASHTAG_LABEL = re.compile(r"\bhashtag\s*#", re.I)
_EARLIEST_POST = datetime(2010, 1, 1, tzinfo=timezone.utc)


class PageState(Enum):
    RESULTS = "results"
    NO_RESULTS = "no_results"
    LOGIN_REQUIRED = "login_required"
    CHECKPOINT = "checkpoint"
    UNRECOGNIZED = "unrecognized"


def _parse(html):
    return lxml_html.fromstring(html or "<html></html>")


def _first(node, xpaths):
    for xpath in xpaths:
        found = node.xpath(xpath)
        if found:
            return found
    return []


def _url_has(url, markers):
    path = (url or "").lower()
    return any(marker in path for marker in markers)


def classify_page(url, html):
    """What kind of page was loaded. URL markers take priority over HTML."""
    if _url_has(url, S.CHECKPOINT_URL_MARKERS):
        return PageState.CHECKPOINT
    if _url_has(url, S.LOGIN_URL_MARKERS):
        return PageState.LOGIN_REQUIRED
    root = _parse(html)
    if _first(root, S.CHECKPOINT_PAGE):
        return PageState.CHECKPOINT
    if _first(root, S.LOGIN_FORM) and not _first(root, S.LOGGED_IN_MARKER):
        return PageState.LOGIN_REQUIRED
    if _first(root, S.ANY_POST_CONTAINER):
        return PageState.RESULTS
    if _first(root, S.NO_RESULTS):
        return PageState.NO_RESULTS
    return PageState.UNRECOGNIZED


def _is_linkedin_https(url):
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(
        host == domain or host.endswith("." + domain) for domain in S.LINKEDIN_HOSTS
    )


def _is_logged_in_path(url):
    path = urlsplit(url or "").path.lower().rstrip("/") or "/"
    return any(path == p or path.startswith(p + "/") for p in S.LOGGED_IN_PATHS)


def is_logged_in(url, html):
    """True only with positive evidence of a logged-in LinkedIn session.

    Fails closed: anything that is not clearly logged in returns False.

    * not an https linkedin.com page                         -> False
    * login / sign-in wall / security check (URL or HTML)    -> False
    * a login form or guest markers anywhere on the page     -> False
    * empty page                                             -> False
    * feed URL (/feed, /feed/...)                            -> True
    * other LinkedIn page with the global-nav marker         -> True
    * anything else (ambiguous)                              -> False
    """
    if not _is_linkedin_https(url):
        return False
    if classify_page(url, html) in (PageState.LOGIN_REQUIRED, PageState.CHECKPOINT):
        return False
    root = _parse(html)
    if not " ".join(root.text_content().split()):
        return False
    if _first(root, S.LOGIN_FORM) or _first(root, S.GUEST_MARKERS):
        return False
    if _is_logged_in_path(url):
        return True
    return bool(_first(root, S.LOGGED_IN_MARKER))


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------

def element_text(element, drop_buttons=False):
    """Visible text with line breaks kept; LinkedIn's hidden duplicates removed.

    ``drop_buttons`` removes inline controls such as the "… more" toggle that the
    current layout places inside the post text.
    """
    element = lxml_html.fromstring(lxml_html.tostring(element))  # work on a copy
    for hidden in element.xpath(".//*[contains(concat(' ', normalize-space(@class), ' '), ' visually-hidden ')]"):
        hidden.drop_tree()
    if drop_buttons:
        for button in element.xpath(".//button"):
            button.drop_tree()  # keeps the text that follows the button
    for br in element.iter("br"):
        br.tail = "\n" + (br.tail or "")
    for block in element.xpath(".//p | .//li | .//div"):
        block.tail = "\n" + (block.tail or "")
    text = _HASHTAG_LABEL.sub("#", element.text_content())
    lines = [" ".join(line.split()) for line in text.replace("\xa0", " ").splitlines()]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return _SEE_MORE_TAIL.sub("", text).strip()


def _field(container, xpaths):
    nodes = _first(container, xpaths)
    return element_text(nodes[0]) if nodes else None


def canonical_urn(value):
    match = _URN.search(value or "")
    if not match:
        return None
    return f"urn:li:{_CANONICAL_TYPES[match.group(1).lower()]}:{match.group(2)}"


def posted_at_from_urn(urn, now=None):
    """LinkedIn post ids carry their creation time in the top 41 bits (milliseconds)."""
    match = _URN.search(urn or "")
    if not match:
        return None
    try:
        posted = datetime.fromtimestamp((int(match.group(2)) >> 22) / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    now = now or datetime.now(timezone.utc)
    if not _EARLIEST_POST <= posted <= now + timedelta(days=1):
        return None
    return posted


def posted_at_from_relative(text, now=None):
    """Approximate time from LinkedIn's relative label ("5m", "2h", "3d", "1w", "2mo", "1yr")."""
    match = _RELATIVE_TIME.match(text or "")
    if not match:
        return None
    now = now or datetime.now(timezone.utc)
    return now - int(match.group(1)) * _RELATIVE_UNITS[match.group(2).lower()]


def _container_urn(container):
    for attribute in S.POST_URN_ATTRIBUTES:
        urn = canonical_urn(container.get(attribute))
        if urn:
            return urn
    for link in _first(container, S.PERMALINK):
        urn = canonical_urn(link.get("href"))
        if urn:
            return urn
    return None


def _permalink(container):
    for link in _first(container, S.PERMALINK):
        href = (link.get("href") or "").strip()
        if href:
            return href if href.startswith("http") else f"{S.BASE_URL}{href}"
    return None


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------

def _post_url(container):
    urn = _container_urn(container)
    return urn, (f"{S.BASE_URL}/feed/update/{urn}/" if urn else _permalink(container))


def _legacy_post(container, now):
    urn, url = _post_url(container)
    label = _field(container, S.POST_TIME)
    posted_at = posted_at_from_urn(urn, now) if urn else None
    if posted_at is None:
        posted_at = posted_at_from_relative(label, now)
    return RawPost(
        text=_field(container, S.POST_TEXT) or "",
        url=url,
        author_name=_field(container, S.AUTHOR_NAME),
        author_headline=_field(container, S.AUTHOR_HEADLINE),
        posted_at=posted_at,
        posted_label=label,
    )


# Paragraphs that belong to the author/header block, not the post text: inside a
# person (/in/) or organisation (/company/) link, or inside data-view-name="feed-actor".
_IN_AUTHOR_BLOCK = (
    "ancestor::a[contains(@href, 'linkedin.com/in/') or starts-with(@href, '/in/')"
    " or contains(@href, 'linkedin.com/company/') or starts-with(@href, '/company/')]"
    " or ancestor::*[@data-view-name='feed-actor']"
)


_PERSON_OR_COMPANY_LINK = (
    "ancestor-or-self::a[contains(@href, 'linkedin.com/in/') or starts-with(@href, '/in/')"
    " or contains(@href, 'linkedin.com/company/') or starts-with(@href, '/company/')]"
    " | .//a[contains(@href, 'linkedin.com/in/') or starts-with(@href, '/in/')"
    " or contains(@href, 'linkedin.com/company/') or starts-with(@href, '/company/')]"
)


def _structural_author(container):
    # The first author link is the (empty) avatar link; use the first one with text.
    for link in container.xpath(S.SDUI_AUTHOR_LINK):
        inner = [p for p in link.xpath(".//p") if element_text(p)]
        name = element_text(inner[0] if inner else link).split("\n")[0].strip()
        if name:
            return name
    return None


def _author_before_degree_marker(paragraphs):
    """The paragraph right before a "• 2nd"/"• 3rd+" marker is the person's name.
    Used when the actor block is not a person/company (e.g. a group post)."""
    for _, text, _a, _b in paragraphs:          # "Name • 3rd+" in one paragraph
        joined = _NAME_WITH_DEGREE.match(text)
        if joined:
            return joined.group("name").strip()
    for (_, text, _a, _b), (_, following, _c, _d) in zip(paragraphs, paragraphs[1:]):
        if text and _DEGREE_MARKER.match(following) and not _DEGREE_MARKER.match(text) \
                and not _TIME_LABEL.match(text):
            return text
    return None


def _sdui_post(container, now, author_name=None):
    """Current layout: structure only (see selectors.SDUI_* for the observed shapes).

    Observed header layouts (live inventory, 9 posts):
      person   - [actor: name, "• 2nd"] headline, time
      company  - [actor: name] time
      group    - [actor: GROUP name] person's name, "• 3rd+", headline   (no time shown)

    ``author_name`` (from a person/company feed-actor) overrides the structural guess
    and is never accepted as the headline.
    """
    paragraphs = [
        (p, element_text(p, drop_buttons=True), bool(p.xpath(_IN_AUTHOR_BLOCK)),
         bool(p.xpath("ancestor::*[@data-view-name='feed-actor']")))
        for p in container.xpath(".//p")
    ]
    author_name = (author_name or _structural_author(container)
                   or _author_before_degree_marker(paragraphs))
    # The time label may sit inside or outside the author block; group posts have none.
    time_index = next((i for i, (_, t, _a, _b) in enumerate(paragraphs) if _TIME_LABEL.match(t)), None)

    # Post text: largest paragraph after the time label (or anywhere, if there is no
    # time label), outside the author block.
    candidates = [(i, t) for i, (_, t, in_author, _b) in enumerate(paragraphs)
                  if not in_author and (time_index is None or i > time_index)]
    body_index, body = max(candidates, key=lambda c: len(c[1]), default=(None, ""))

    # Headline: a header paragraph (before the time label, or before the post text
    # when there is none) that is not the author's name, not a "• 2nd" marker and not a
    # time. Paragraphs outside the actor block are preferred (in a group post the
    # actor holds the GROUP name). Never equal to the author.
    header_end = time_index if time_index is not None else (body_index if body_index is not None else 0)
    author_key = (author_name or "").casefold()
    header = [(t, in_actor) for _, t, _a, in_actor in paragraphs[:header_end]
              if t and t.casefold() != author_key and not _DEGREE_MARKER.match(t)
              and not _TIME_LABEL.match(t) and not _NAME_WITH_DEGREE.match(t)]
    headline = next((t for t, in_actor in header if not in_actor), None) \
        or next((t for t, _in_actor in header), None)

    urn, url = _post_url(container)  # normally none in this layout
    label = paragraphs[time_index][1] if time_index is not None else None
    posted_at = posted_at_from_urn(urn, now) if urn else None
    if posted_at is None and label is not None:
        posted_at = posted_at_from_relative(label, now)
    return RawPost(text=body, url=url, author_name=author_name, author_headline=headline,
                   posted_at=posted_at, posted_label=label)


def _own_hooks(container, xpath):
    """Hook elements belonging to THIS post, not to a post nested inside it (a reshare)."""
    tree = container.getroottree()
    own_path = tree.getpath(container)
    owned = []
    for element in container.xpath(xpath):
        nearest_post = next(
            (a for a in element.iterancestors() if a.get("data-view-name") == S.DVN_POST), None
        )
        if nearest_post is not None and tree.getpath(nearest_post) == own_path:
            owned.append(element)
    return owned


def _dvn_post(container, now):
    """Current layout via data-view-name hooks; each missing or empty hook falls
    back to the structural rules (which also supply the headline and time)."""
    author = None
    for actor in _own_hooks(container, S.DVN_ACTOR):
        # A group post's actor is the GROUP (no person/company link): not the author.
        if not actor.xpath(_PERSON_OR_COMPANY_LINK):
            continue
        names = [element_text(p) for p in actor.xpath(".//p")]
        names = [n for n in names if n] or [element_text(actor).split("\n")[0]]
        author = names[0] or None
        if author:
            break
    # The actor's name is passed in, so the structural headline can never equal it.
    fallback = _sdui_post(container, now, author_name=author)
    text = next(
        (t for t in (element_text(e, drop_buttons=True) for e in _own_hooks(container, S.DVN_COMMENTARY)) if t),
        None,
    )
    return RawPost(
        text=text or fallback.text,
        url=fallback.url,  # normally None: this layout exposes no post URL
        author_name=fallback.author_name,
        author_headline=fallback.author_headline,
        posted_at=fallback.posted_at,
        posted_label=fallback.posted_label,
    )


def extract_posts(html, now=None):
    """All posts on a results page, in page order, de-duplicated.

    Layouts, in order: legacy selectors; the current layout via its
    data-view-name hooks; the current layout via the "Feed post" heading.
    Current-layout posts have no URL.
    """
    root = _parse(html)
    containers, build = _first(root, S.POST_CONTAINER), _legacy_post
    if not containers:
        containers, build = _first(root, S.DVN_POST_CONTAINER), _dvn_post
    if not containers:
        containers, build = _first(root, S.SDUI_POST_CONTAINER), _sdui_post
    posts, seen = [], set()
    for container in containers:
        post = build(container, now)
        key = post_key(post)
        if key in seen:
            continue
        seen.add(key)
        posts.append(post)
    return posts


def post_key(post):
    """Stable identity of a RawPost for de-duplication across pages/keywords.

    With a URL: the tracker's URL normalization, so feed and /posts/ share links
    of the same post share a key. Without one (current layout): author and text.
    """
    normalized = normalize_source_url(post.url)
    if normalized:
        return canonical_urn(normalized) or normalized
    return f"{post.author_name or ''}\x00{post.text}"
