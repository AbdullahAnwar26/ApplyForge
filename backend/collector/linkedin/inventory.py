"""
Post-attribute inventory: WHERE does LinkedIn keep a post's identifier?

A diagnostic only. It does not change extraction and does not recover URLs.
It runs on the HTML the collector has already loaded (no extra page loads or
clicks), and only when explicitly enabled (``--attribute-inventory`` or
``COLLECTOR_ATTRIBUTE_INVENTORY``).

For each post container (and up to 3 ancestors) it records:

* attribute NAMES, with counts and maximum value length;
* public post URNs (``urn:li:activity|ugcPost|share:<15-20 digits>``), found in
  attribute values (also URL-encoded, and LinkedIn post links), with the
  attribute, tag and depth where they occur;
* for any other ``urn:li:<type>:`` value: the TYPE ONLY (e.g. ``fsd_profile``),
  never the value - profile/member URNs identify people;
* malformed post URNs as ``<type>(malformed)``, never their value;
* for everything else: the value LENGTH only.

Embedded ``<code>`` and JSON ``<script>`` blocks contribute only post-URN counts,
and per post whether that post's URNs appear in them.

Version 2 additions:

* every post-URN location records HOW it was found: ``plain``, ``url``
  (percent-decoded) or, for ``componentkey`` only, a bounded decoding chain
  such as ``base64``, ``base64url`` or ``url>base64`` (at most 3 steps). Only a
  strictly valid public post URN ever leaves the decoder; decoded content is
  never stored;
* ``data-view-name`` and ``role`` values are recorded only when they are short
  lowercase words joined by hyphens (e.g. ``feed-full-update``, ``listitem``);
  anything else is counted as ``<not-allowlisted>`` without its value.

Never recorded: text, names, headlines, e-mails, phone numbers, class values,
link targets, profile URNs, decoded content or any other attribute value.
"""

import base64
import binascii
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from lxml import html as lxml_html

from tracker.ingestion import normalize_source_url

from . import selectors as S
from .diagnostics import next_debug_path

REPORT_VERSION = 3
FILE_PREFIX = "linkedin_attr_inventory_"
MAX_ANCESTORS = 3
POST_URN_TYPES = {"activity": "activity", "ugcpost": "ugcPost", "share": "share"}

# Bounded decoding, for these attributes only.
DECODED_ATTRIBUTES = frozenset({"componentkey"})
MAX_DECODE_DEPTH = 3          # e.g. url>base64>base64
MAX_DECODE_INPUT = 4096       # characters considered per value / decoded step
MAX_DECODE_NODES = 64         # total decode attempts per value
MAX_BASE64_RUNS = 16          # candidate base64 substrings per step
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/_-]{16,}={0,2}")

# Structural values worth recording, and only when they look like identifiers.
STRUCTURAL_ATTRIBUTES = frozenset({"data-view-name", "role"})
MAX_STRUCTURAL_LENGTH = 40
_SAFE_STRUCTURAL_VALUE = re.compile(r"[a-z]+(?:-[a-z]+)*")
NOT_ALLOWLISTED = "<not-allowlisted>"

# The id part stops at "(" too, so a nested URN such as
# urn:li:fs_updateV2:(urn:li:activity:<id>,...) is still found, and at ":", "|",
# "/" and "\" so adjacent URNs ("<urn>|<urn>") are not swallowed by the first.
_ANY_URN = re.compile(r"urn:li:([A-Za-z][A-Za-z0-9_]{0,40}):([^\s\"'<>,;(){}\[\]&:|/\\]*)", re.I)
# A post id is 15-20 digits not followed by another digit; LinkedIn often glues
# text after it (e.g. componentkey "expandedurn:li:activity:<id>FeedType_MAIN",
# or "/" in a post link), so only the leading digits are taken.
_VALID_POST_ID = re.compile(r"(\d{15,20})(?!\d)")
_ATTRIBUTE_NAME = re.compile(r"[A-Za-z_][-A-Za-z0-9_:.]{0,60}")
_POST_LINK = re.compile(r"linkedin\.com/(?:feed/update|posts)/|^/(?:feed/update|posts)/", re.I)
_EMBEDDED_BLOCKS = "//code | //script[contains(@type, 'json')]"


# ---------------------------------------------------------------------------
# Value classification
# ---------------------------------------------------------------------------

def _post_urn(match):
    """Canonical public post URN for a ``_ANY_URN`` match, or None."""
    kind = POST_URN_TYPES.get(match.group(1).lower())
    ident = _VALID_POST_ID.match(match.group(2)) if kind else None
    return f"urn:li:{kind}:{ident.group(1)}" if ident else None


def classify_value(value):
    """``{"length", "post_urns", "other_urn_types"}`` - never the value itself."""
    value = value or ""
    decoded = unquote(value)
    post_urns, other_types = set(), Counter()
    for match in _ANY_URN.finditer(decoded):
        urn = _post_urn(match)
        if urn:
            post_urns.add(urn)
        elif match.group(1).lower() in POST_URN_TYPES:
            other_types[f"{POST_URN_TYPES[match.group(1).lower()]}(malformed)"] += 1
        else:
            other_types[match.group(1)] += 1
    if _POST_LINK.search(decoded):
        normalized = normalize_source_url(
            decoded if decoded.startswith("http") else f"https://www.linkedin.com{decoded}"
        )
        match = _ANY_URN.search(normalized or "")
        urn = _post_urn(match) if match else None
        if urn:
            post_urns.add(urn)
    return {"length": len(value), "post_urns": sorted(post_urns), "other_urn_types": dict(other_types)}


def _plain_post_urns(text):
    """Valid post URNs written literally in ``text`` (no decoding)."""
    found = (_post_urn(m) for m in _ANY_URN.finditer(text or ""))
    return {urn for urn in found if urn}


def _base64_decodings(run):
    """``[(method, decoded_text)]`` for one base64/base64url candidate run.

    The run is tried at each of the 4 alignments, so a segment glued to text
    (e.g. ``expanded<base64>FeedType``) still decodes. Decoded bytes are read as
    latin-1 so an ASCII URN inside binary data stays visible to the URN pattern.
    Only URNs found by the caller ever leave the decoder.
    """
    stripped = run.rstrip("=")
    urlsafe = "-" in stripped or "_" in stripped
    standard = "+" in stripped or "/" in stripped
    if urlsafe and standard:
        return []
    method = "base64url" if urlsafe else "base64"
    results = []
    for offset in range(4):
        body = stripped[offset:]
        if len(body) % 4 == 1:
            body = body[:-1]  # a trailing glued character cannot complete a quantum
        if len(body) < 16:
            continue
        padded = body + "=" * (-len(body) % 4)
        try:
            if urlsafe:
                raw = base64.b64decode(padded, altchars=b"-_", validate=True)
            else:
                raw = base64.b64decode(padded, validate=True)
        except (binascii.Error, ValueError):
            continue
        results.append((method, raw[:MAX_DECODE_INPUT].decode("latin-1")))
    return results


def decode_post_urns(value):
    """Post URNs hidden in ``value`` behind URL and/or base64 encoding.

    Returns ``{urn: [shortest decoding chains]}`` such as ``{"urn:li:activity:..":
    ["url>base64"]}``. Bounded: at most MAX_DECODE_DEPTH steps, MAX_DECODE_NODES
    attempts and MAX_DECODE_INPUT characters per step. Literal and plain
    URL-decoded URNs are left to ``classify_value``; decoded text never leaves.
    """
    found = defaultdict(set)
    queue, seen, attempts = [((), (value or "")[:MAX_DECODE_INPUT])], set(), 0
    while queue and attempts < MAX_DECODE_NODES:
        chain, text = queue.pop(0)
        if (chain, text) in seen:
            continue
        seen.add((chain, text))
        attempts += 1
        if chain and chain != ("url",):
            for urn in _plain_post_urns(text):
                found[urn].add(">".join(chain))
        if len(chain) >= MAX_DECODE_DEPTH:
            continue
        if "%" in text:
            url_decoded = unquote(text)
            if url_decoded != text:
                queue.append((chain + ("url",), url_decoded))
        for run in _BASE64_RUN.findall(text)[:MAX_BASE64_RUNS]:
            for method, decoded in _base64_decodings(run):
                queue.append((chain + (method,), decoded))
    shortest = {}
    for urn, chains in found.items():
        fewest = min(chain.count(">") for chain in chains)
        shortest[urn] = sorted(chain for chain in chains if chain.count(">") == fewest)
    return shortest


def structural_value(value):
    """The value if it is a short lowercase-hyphen identifier, else NOT_ALLOWLISTED."""
    value = value or ""
    if len(value) <= MAX_STRUCTURAL_LENGTH and _SAFE_STRUCTURAL_VALUE.fullmatch(value):
        return value
    return NOT_ALLOWLISTED


def _safe_name(name):
    return name if _ATTRIBUTE_NAME.fullmatch(name or "") else f"<unusual-name len {len(name or '')}>"


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

def _scan_element(element, scope, depth, stats, locations, structural):
    for raw_name, value in element.attrib.items():
        name = _safe_name(raw_name)
        info = classify_value(value)
        stat = stats[scope].setdefault(
            name, {"count": 0, "max_length": 0, "post_urn_values": 0, "other_urn_types": {}}
        )
        stat["count"] += 1
        stat["max_length"] = max(stat["max_length"], info["length"])
        for kind, count in info["other_urn_types"].items():
            stat["other_urn_types"][kind] = stat["other_urn_types"].get(kind, 0) + count

        # How each URN was found: literally, after percent-decoding, or (componentkey
        # only) behind a bounded base64/URL decoding chain.
        methods = defaultdict(set)
        literal = _plain_post_urns(value)
        percent_decoded = _plain_post_urns(unquote(value or ""))
        for urn in info["post_urns"]:
            if urn in literal:
                methods[urn].add("plain")
            elif urn in percent_decoded:
                methods[urn].add("url")
            else:
                methods[urn].add("post-link")  # derived from a /posts/...-activity-<id>-... link
        if raw_name.lower() in DECODED_ATTRIBUTES:
            for urn, chains in decode_post_urns(value).items():
                if urn not in methods:
                    methods[urn].update(chains)
        stat["post_urn_values"] += len(methods)
        for urn, found_by in methods.items():
            for method in sorted(found_by):
                locations.append({"urn": urn, "scope": scope, "depth": depth, "tag": str(element.tag),
                                  "attribute": name, "method": method})

        if raw_name.lower() in STRUCTURAL_ATTRIBUTES:
            values = structural[scope].setdefault(raw_name.lower(), {})
            safe = structural_value(value)
            values[safe] = values.get(safe, 0) + 1


def _post_inventory(index, container, embedded_urns):
    stats, locations, structural = defaultdict(dict), [], defaultdict(dict)
    ancestors = list(container.iterancestors())[:MAX_ANCESTORS]
    for level, ancestor in enumerate(ancestors, start=1):
        _scan_element(ancestor, f"ancestor-{level}", -level, stats, locations, structural)
    _scan_element(container, "container", 0, stats, locations, structural)
    base = len(list(container.iterancestors()))
    for element in container.iterdescendants():
        if isinstance(element.tag, str):
            depth = len(list(element.iterancestors())) - base
            _scan_element(element, "descendants", depth, stats, locations, structural)
    distinct = sorted({loc["urn"] for loc in locations})
    return {
        "index": index,
        "scopes": {scope: dict(sorted(names.items())) for scope, names in stats.items()},
        "post_urn_locations": sorted(
            locations, key=lambda loc: (loc["depth"], loc["urn"], loc["attribute"], loc["method"])
        ),
        "distinct_post_urns": distinct,
        "in_embedded_blocks": {urn: urn in embedded_urns for urn in distinct},
        "structural_values": {scope: dict(sorted(attrs.items())) for scope, attrs in structural.items()},
    }


def _post_urn_occurrences(text):
    """Every valid post URN occurrence in ``text`` (duplicates kept)."""
    found = (_post_urn(m) for m in _ANY_URN.finditer(unquote(text or "")))
    return [urn for urn in found if urn]


def _embedded_summary(root):
    """Counts only; the block text itself is never kept."""
    blocks = root.xpath(_EMBEDDED_BLOCKS)
    occurrences, urns, with_urns = 0, set(), 0
    for block in blocks:
        found = _post_urn_occurrences(block.text_content())
        if found:
            with_urns += 1
        occurrences += len(found)
        urns.update(found)
    return {
        "blocks_scanned": len(blocks),
        "blocks_with_post_urns": with_urns,
        "post_urn_occurrences": occurrences,
        "distinct_post_urn_count": len(urns),
    }, urns


# --- Header shapes (version 3) ------------------------------------------------------
# Where are the author's headline and the time label? Recorded as SHAPES only:
# each word -> "W", each number -> "9"; text made only of digits, bullets,
# separators and time-unit letters ("1m •", "--") is kept literally.
MAX_HEADER_ENTRIES = 25
# Literal only for a time token ("1m", "2mo") and/or digits and separators - never
# for words, even short ones made of unit letters (a name like "Mohd").
_LITERAL_SAFE = re.compile(r"^(?=.{1,24}$)[\s•·|\-–—:.,+]*(?:\d+\s*(?:mo|yr|s|m|h|d|w|y)\b)?[\s•·|\-–—:.,+\d]*$", re.I)
_PROFILE_LINK = "ancestor-or-self::a[contains(@href, 'linkedin.com/in/') or starts-with(@href, '/in/')]"
_COMPANY_LINK = "ancestor-or-self::a[contains(@href, 'linkedin.com/company/') or starts-with(@href, '/company/')]"


def text_shape(text):
    text = " ".join((text or "").split())
    if _LITERAL_SAFE.match(text):
        return text
    shaped = re.sub(r"\d+", "9", re.sub(r"[^\W\d_]+", "W", text))
    return shaped[:40]


def _nearest_view_name(element, container):
    for node in [element, *element.iterancestors()]:
        value = node.get("data-view-name")
        if value:
            return structural_value(value)
        if node is container:
            break
    return None


def _header_shapes(container):
    """Text-bearing elements before the post text, as privacy-safe shapes."""
    entries = []
    base = len(list(container.iterancestors()))
    for element in container.iter():
        if not isinstance(element.tag, str) or element is container:
            continue
        if element.get("data-view-name") == "feed-commentary":
            break
        own = " ".join((element.text or "").split())
        if not own:
            continue
        path = [element.tag]
        for ancestor in element.iterancestors():
            if ancestor is container or len(path) >= 4:
                break
            path.append(ancestor.tag)
        entries.append({
            "depth": len(list(element.iterancestors())) - base,
            "path": ">".join(reversed(path)),
            "in_profile_link": bool(element.xpath(_PROFILE_LINK)),
            "in_company_link": bool(element.xpath(_COMPANY_LINK)),
            "in_actor": bool(element.xpath("ancestor-or-self::*[@data-view-name='feed-actor']")),
            "view_name": _nearest_view_name(element, container),
            "shape": text_shape(own),
        })
        if len(entries) >= MAX_HEADER_ENTRIES:
            break
    return {
        "entries": entries,
        "time_elements": len(container.xpath(".//time")),
        "time_element_attributes": sorted({name for t in container.xpath(".//time") for name in t.attrib}),
    }


def build_inventory(html, keyword=None, now=None):
    """Inventory report (a dict) for one loaded search-results page."""
    root = lxml_html.fromstring(html or "<html></html>")
    legacy = root.xpath(" | ".join(S.POST_CONTAINER))
    containers = (legacy or root.xpath(" | ".join(S.SDUI_POST_CONTAINER))
                  or root.xpath(" | ".join(S.DVN_POST_CONTAINER)))
    embedded, embedded_urns = _embedded_summary(root)
    posts = [_post_inventory(i, c, embedded_urns) for i, c in enumerate(containers)]
    for post, container in zip(posts, containers):
        post["header_shapes"] = _header_shapes(container)
    attribute_names_with_urns = Counter(
        loc["attribute"] for post in posts for loc in post["post_urn_locations"]
    )
    methods = Counter(loc["method"] for post in posts for loc in post["post_urn_locations"])
    # In how many posts each allowlisted structural value occurs, per scope/attribute.
    structural = defaultdict(lambda: defaultdict(Counter))
    for post in posts:
        for scope, attrs in post["structural_values"].items():
            for attr, values in attrs.items():
                for value in values:
                    structural[scope][attr][value] += 1
    return {
        "report": "ApplyForge LinkedIn post-attribute inventory",
        "version": REPORT_VERSION,
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
        "keyword": keyword,
        "layout": "legacy" if legacy else ("current" if containers else "none"),
        "post_count": len(posts),
        "posts_with_post_urns": sum(1 for p in posts if p["distinct_post_urns"]),
        "attribute_names_carrying_post_urns": dict(attribute_names_with_urns.most_common()),
        "post_urn_methods": dict(methods.most_common()),
        "structural_values_by_post": {
            scope: {attr: dict(values.most_common()) for attr, values in sorted(attrs.items())}
            for scope, attrs in structural.items()
        },
        "embedded_blocks": embedded,
        "posts": posts,
    }


def save_inventory(report, debug_dir, keep=5):
    """Write the report as JSON; keep only the newest ``keep`` inventory files."""
    debug_dir = Path(debug_dir)
    debug_dir.mkdir(parents=True, exist_ok=True)
    path = next_debug_path(debug_dir, FILE_PREFIX, datetime.now(), ".json")
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    files = sorted(debug_dir.glob(f"{FILE_PREFIX}*.json"), key=lambda p: p.name)
    for old in files[:-keep] if keep > 0 else files:
        old.unlink(missing_ok=True)
    return path
