"""
Sanitized diagnostic HTML for selector/page problems.

Saved only when a page cannot be understood (unrecognized layout or missing
selectors), never for normal runs or login walls. Before writing, the page is
stripped of everything that could carry authentication data:

* removed elements: script, style, noscript, iframe, link, meta, template,
  code (LinkedIn embeds JSON state with tokens there), form inputs, svg;
* kept attributes only: class, id, data-urn, data-id, aria-hidden,
  aria-expanded, and href without its query string or fragment;
* any remaining text that looks like a session value (JSESSIONID, li_at,
  ajax:…, bearer tokens, long opaque strings) is redacted.

Cookies are never read, so they cannot be written. Files go to
``COLLECTOR_DEBUG_DIR`` (outside the repository and OneDrive by default),
and only the newest ``COLLECTOR_DEBUG_FILES_KEPT`` are kept.
"""

import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from lxml import etree
from lxml import html as lxml_html

REMOVED_TAGS = ("script", "style", "noscript", "iframe", "link", "meta", "template",
                "code", "input", "textarea", "select", "svg", "object", "embed")
KEPT_ATTRIBUTES = {"class", "id", "data-urn", "data-id", "aria-hidden", "aria-expanded", "href"}
_SECRETS = [
    re.compile(r"JSESSIONID\s*[=:]\s*\"?[^\s\";&<]+", re.I),
    re.compile(r"\bli_at\s*[=:]\s*\"?[^\s\";&<]+", re.I),
    re.compile(r"\bajax:\d{6,}", re.I),
    re.compile(r"\b(?:bearer|token|csrf[-_]?token)\s*[=:]\s*\"?[^\s\";&<]+", re.I),
    re.compile(r"\b[A-Za-z0-9_\-+/=]{60,}\b"),
]
REDACTED = "[REDACTED]"
FILE_PREFIX = "linkedin_debug_"


def _clean_href(href):
    parts = urlsplit(href or "")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def sanitize_html(html):
    root = lxml_html.fromstring(html or "<html></html>")
    etree.strip_elements(root, *REMOVED_TAGS, with_tail=False)
    etree.strip_elements(root, etree.Comment, with_tail=False)
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        for name in list(element.attrib):
            if name not in KEPT_ATTRIBUTES:
                del element.attrib[name]
        if "href" in element.attrib:
            element.attrib["href"] = _clean_href(element.attrib["href"])
    text = lxml_html.tostring(root, encoding="unicode")
    for pattern in _SECRETS:
        text = pattern.sub(REDACTED, text)
    return text


def next_debug_path(debug_dir, prefix, now, suffix):
    """``<prefix><timestamp>-<NNN><suffix>``, never reusing a name.

    Windows' clock is coarse, so several files can share a timestamp. The counter
    is one above the highest existing counter for that timestamp, so name order
    always equals creation order (pruning relies on it) even after pruning.
    """
    stamp = now.strftime("%Y%m%d-%H%M%S-%f")
    taken = re.compile(rf"^{re.escape(prefix + stamp)}-(\d{{3,}})")
    used = [int(m.group(1)) for p in Path(debug_dir).glob(f"{prefix}{stamp}-*")
            if (m := taken.match(p.name))]
    return Path(debug_dir) / f"{prefix}{stamp}-{max(used, default=-1) + 1:03d}{suffix}"


def save_diagnostic(html, reason, url, debug_dir, keep=5):
    """Write sanitized HTML; return its path. Keeps only the newest ``keep`` files."""
    debug_dir = Path(debug_dir)
    debug_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", (reason or "page").lower()).strip("-")[:40] or "page"
    path = next_debug_path(debug_dir, FILE_PREFIX, datetime.now(), f"_{slug}.html")
    header = (
        f"<!-- ApplyForge collector diagnostic. Reason: {reason}. URL: {_clean_href(url)}. "
        "Sanitized: scripts, code blocks, inputs, metadata and token-like values removed. -->\n"
    )
    path.write_text(header + sanitize_html(html), encoding="utf-8")
    files = sorted(debug_dir.glob(f"{FILE_PREFIX}*.html"), key=lambda p: p.name)
    for old in files[:-keep] if keep > 0 else files:
        old.unlink(missing_ok=True)
    return path
