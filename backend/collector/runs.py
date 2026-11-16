"""
Run history around the existing collection service (Phase 6c).

``record_collection`` wraps ``collector.service.run_collection`` unchanged: it
creates a RUNNING row before the source is touched and finishes it afterwards.
Both database writes happen outside the browser session (the LinkedIn source
closes its browser before yielding posts), so Playwright's event loop never
overlaps database access.

Status rules:
* SUCCEEDED - no source error and no failed posts;
* PARTIAL   - the source failed after yielding posts, or some posts failed to ingest;
* FAILED    - invalid input, the source failed before any post, or an unexpected error.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from tracker.screening import ScreeningInputError

from .models import CollectorRun, CollectorRunStatus
from .service import CollectionInputError, run_collection

logger = logging.getLogger("collector")


def _finish(run, status, error=""):
    run.status = status
    run.error = error or ""
    run.finished_at = timezone.now()
    run.save(update_fields=["status", "error", "finished_at", "profile", "keywords", "limit",
                            "counts", "job_ids"])
    return run


def status_for(result):
    if result.source_error:
        return CollectorRunStatus.PARTIAL if result.outcomes else CollectorRunStatus.FAILED
    if result.counts["failed"]:
        return CollectorRunStatus.PARTIAL
    return CollectorRunStatus.SUCCEEDED


def record_collection(source, *, trigger, keywords=None, profile=None, limit=None):
    """Run a collection and record it. Returns ``(run, result)``.

    Input and unexpected errors are recorded as FAILED and then re-raised (with
    the run attached as ``exc.run``), so callers keep their existing error handling.
    """
    run = CollectorRun.objects.create(
        trigger=trigger,
        source=getattr(source, "name", type(source).__name__),
        profile=profile,
        keywords=[k for k in (keywords or []) if isinstance(k, str)],
        limit=limit,
    )
    try:
        result = run_collection(source, keywords=keywords, profile=profile, limit=limit)
    except (ScreeningInputError, CollectionInputError) as exc:
        _finish(run, CollectorRunStatus.FAILED, exc.message)
        exc.run = run
        raise
    except Exception as exc:
        logger.exception("Collector run %s failed unexpectedly", run.pk)
        _finish(run, CollectorRunStatus.FAILED, f"{type(exc).__name__}: {exc}")
        exc.run = run
        raise

    run.profile_id = result.profile_id
    run.keywords = list(result.keywords)
    run.limit = result.limit
    run.counts = result.counts
    run.job_ids = [o.job_id for o in result.outcomes if o.job_id]
    _finish(run, status_for(result), result.source_error or "")
    logger.info("Collector run %s finished: %s %s", run.pk, run.status, run.counts)
    return run, result


def active_run():
    """The RUNNING run that blocks a new one, if any. Stale RUNNING rows (older than
    COLLECTOR_STALE_RUN_MINUTES, e.g. left by a crash) are marked FAILED first."""
    cutoff = timezone.now() - timedelta(minutes=settings.COLLECTOR_STALE_RUN_MINUTES)
    for stale in CollectorRun.objects.filter(status=CollectorRunStatus.RUNNING, started_at__lt=cutoff):
        _finish(stale, CollectorRunStatus.FAILED,
                f"Abandoned: still RUNNING after {settings.COLLECTOR_STALE_RUN_MINUTES} minutes.")
    return CollectorRun.objects.filter(status=CollectorRunStatus.RUNNING).order_by("started_at").first()
