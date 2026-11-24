"""
Collection service: posts -> existing tracker ingestion (Milestone 6).

Everything after parsing is existing tracker code, called in-process (rule 12):

* ``tracker.screening.resolve_profile`` - profile selection (zero / one /
  several profiles, profile without skills). Runs BEFORE the source is
  touched, so a bad profile setup never starts browsing.
* ``tracker.api.serializers.JobIngestSerializer`` - payload validation.
* ``tracker.ingestion.ingest_job`` - URL/fingerprint deduplication,
  company/contact reuse, experience parsing and screening.

One bad post never stops a run: it is counted as skipped, invalid or failed.
A failure of the source itself ends the run early with the results so far.
"""

import logging
from dataclasses import asdict, dataclass, field
from itertools import islice

from django.conf import settings

from tracker.api.serializers import JobIngestSerializer
from tracker.ingestion import ingest_job
from tracker.screening import resolve_profile

from .posts import PostParseError, PostSkipped, build_payload
from .sources import SourceError

logger = logging.getLogger("collector")

CREATED, EXISTING, SKIPPED, INVALID, FAILED = "created", "existing", "skipped", "invalid", "failed"


class CollectionInputError(Exception):
    def __init__(self, field_name, message):
        super().__init__(message)
        self.field = field_name
        self.message = message


@dataclass
class PostOutcome:
    index: int
    status: str
    url: str | None = None
    title: str | None = None
    job_id: int | None = None
    matched_by: str | None = None
    screening_outcome: str | None = None
    reason: str | None = None
    # Extraction summary for `collect_linkedin -v 3` only: in memory, never stored,
    # logged or included in as_dict()/--json. See extraction_summary().
    extraction: dict | None = field(default=None, repr=False, compare=False)


@dataclass
class CollectionResult:
    source: str
    profile_id: int
    keywords: list
    limit: int
    outcomes: list = field(default_factory=list)
    source_error: str | None = None

    def count(self, status):
        return sum(1 for o in self.outcomes if o.status == status)

    @property
    def counts(self):
        counts = {s: self.count(s) for s in (CREATED, EXISTING, SKIPPED, INVALID, FAILED)}
        counts["found"] = len(self.outcomes)
        return counts

    @property
    def job_ids(self):
        return [o.job_id for o in self.outcomes if o.status == CREATED]

    def as_dict(self):
        return {
            "source": self.source,
            "profile_id": self.profile_id,
            "keywords": self.keywords,
            "limit": self.limit,
            "counts": self.counts,
            "source_error": self.source_error,
            "outcomes": [
                {k: v for k, v in asdict(o).items() if k != "extraction"} for o in self.outcomes
            ],
        }


def extraction_summary(post):
    """What extraction produced for one post, for -v 3 console diagnostics only.

    Commentary is represented by its length, never its text.
    """
    return {
        "author": post.author_name,
        "headline": post.author_headline,
        "relative_time": post.posted_label,
        "commentary_chars": len(post.text or ""),
        # A bare trailing "…" usually means the "… more" expansion did not complete.
        "commentary_truncated": (post.text or "").rstrip().endswith("…"),
    }


def default_keywords(profile):
    """Search keywords from the profile's comma-separated target roles."""
    return [role.strip() for role in (profile.target_roles or "").split(",") if role.strip()]


def _validate_limit(limit):
    maximum = settings.COLLECTOR_MAX_POSTS_PER_RUN
    if limit is None:
        return min(settings.COLLECTOR_DEFAULT_POSTS_PER_RUN, maximum)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= maximum:
        raise CollectionInputError("limit", f"Limit must be an integer from 1 to {maximum}.")
    return limit


def _format_errors(errors):
    parts = []
    for key, value in errors.items():
        messages = value if isinstance(value, list) else [value]
        parts.append(f"{key}: {'; '.join(str(m) for m in messages)}")
    return " | ".join(parts)


def _process_post(index, post, profile, skills):
    # The summary is taken before hiring classification, so skipped and invalid
    # posts can be diagnosed too.
    outcome = PostOutcome(index=index, status=FAILED, url=post.url, extraction=extraction_summary(post))
    try:
        payload = build_payload(post)
    except PostSkipped as exc:
        outcome.status, outcome.reason = SKIPPED, str(exc)
        return outcome
    except PostParseError as exc:
        outcome.status, outcome.reason = INVALID, str(exc)
        return outcome
    outcome.title = payload["title"]

    serializer = JobIngestSerializer(data=payload)
    if not serializer.is_valid():
        outcome.status, outcome.reason = INVALID, _format_errors(serializer.errors)
        return outcome

    try:
        result = ingest_job(serializer.validated_data, profile, skills)
    except Exception as exc:  # isolate one post's failure; ingest_job is atomic
        logger.exception("Ingesting collected post %s failed", index)
        outcome.reason = f"{type(exc).__name__}: {exc}"
        return outcome

    outcome.status = CREATED if result.created else EXISTING
    outcome.job_id = result.job.pk
    outcome.matched_by = result.matched_by
    outcome.screening_outcome = result.job.screening_outcome
    return outcome


def run_collection(source, keywords=None, profile=None, limit=None, *, owner=None):
    """Collect posts from ``source`` and ingest them. Returns CollectionResult.

    Raises ``tracker.screening.ScreeningInputError`` for profile problems and
    CollectionInputError for bad keywords/limit, in both cases before the
    source is used.
    """
    limit = _validate_limit(limit)
    profile, skills = resolve_profile(profile, owner=owner)
    if keywords is None:
        keywords = default_keywords(profile)
    keywords = [k.strip() for k in keywords if isinstance(k, str) and k.strip()]
    if not keywords:
        raise CollectionInputError(
            "keywords", "No search keywords. Pass keywords or set the profile's target roles."
        )

    result = CollectionResult(
        source=getattr(source, "name", type(source).__name__),
        profile_id=profile.pk,
        keywords=keywords,
        limit=limit,
    )
    logger.info(
        "Collection started: source=%s profile=%s keywords=%s limit=%s",
        result.source, profile.pk, keywords, limit,
    )
    posts = source.iter_posts(keywords, limit)
    try:
        for index, post in enumerate(islice(posts, limit)):
            result.outcomes.append(_process_post(index, post, profile, skills))
    except SourceError as exc:
        result.source_error = str(exc)
        logger.warning("Collection source failed: %s", exc)
    finally:
        # Stop the source deterministically (e.g. close the browser) even when
        # the run ends early at the limit.
        close = getattr(posts, "close", None)
        if close is not None:
            close()
    logger.info("Collection finished: %s", result.counts)
    return result
