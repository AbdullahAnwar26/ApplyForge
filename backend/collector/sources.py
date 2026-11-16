"""
Post sources for the collector.

A source yields ``RawPost`` objects for some keywords, up to a limit. Phase 6a
ships only offline sources; the Playwright LinkedIn browser source arrives in
Phase 6b and must implement the same ``iter_posts`` interface.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Protocol

from django.utils import timezone

from .posts import RawPost


class SourceError(Exception):
    """The source itself failed (file unreadable, browser crashed, ...)."""


class PostSource(Protocol):
    name: str

    def iter_posts(self, keywords: list[str], limit: int) -> Iterator[RawPost]:
        ...


class StaticPostSource:
    """Yields a fixed list of posts. Used by tests."""

    name = "static"

    def __init__(self, posts: Iterable[RawPost]):
        self.posts = list(posts)
        self.calls = []

    def iter_posts(self, keywords, limit):
        self.calls.append((list(keywords), limit))
        yield from self.posts[:limit]


class JsonFilePostSource:
    """Reads posts from a JSON file: a list of objects with ``text`` and optional
    ``url``, ``author_name``, ``author_headline`` and ``posted_at`` (ISO 8601).

    Offline stand-in for the LinkedIn browser until Phase 6b. Keywords are
    ignored. Entries are yielded in file order.
    """

    name = "file"

    def __init__(self, path):
        self.path = Path(path)

    def _load(self):
        try:
            with self.path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError as exc:
            raise SourceError(f"Posts file not found: {self.path}") from exc
        except (OSError, ValueError) as exc:
            raise SourceError(f"Posts file could not be read as JSON: {exc}") from exc
        if not isinstance(data, list):
            raise SourceError("Posts file must contain a JSON list of post objects.")
        return data

    def iter_posts(self, keywords, limit):
        for index, entry in enumerate(self._load()[:limit]):
            if not isinstance(entry, dict):
                raise SourceError(f"Entry {index} is not an object.")
            yield RawPost(
                text=str(entry.get("text") or ""),
                url=entry.get("url") or None,
                author_name=entry.get("author_name") or None,
                author_headline=entry.get("author_headline") or None,
                posted_at=_parse_datetime(entry.get("posted_at"), index),
            )


def _parse_datetime(value, index):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise SourceError(f"Entry {index} has an invalid posted_at: {value!r}") from exc
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.utc)
    return parsed
