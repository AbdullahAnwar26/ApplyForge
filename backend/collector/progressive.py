"""
Progressive background collection (Job Automation, phase 1).

``start_collection`` records a RUNNING ``CollectorRun`` and launches
``execute_run`` in a background thread; the API returns at once and the UI
polls the run for progress.

Inside ``execute_run`` there are two threads:

* a producer, which drives the post source (for LinkedIn: the Playwright
  browser) and puts each post on a bounded queue. It never touches the
  database, so Playwright's event loop never overlaps Django database access;
* the consumer (the run's own thread), which ingests each post through the
  existing service (``collector.service._process_post`` ->
  ``tracker.ingestion.ingest_job``: validation, URL/fingerprint de-duplication,
  company/contact reuse, screening), records a ``CollectedPost`` row, and
  saves the run's counts, stage and heartbeat as it goes.

The run ends when the target number of NEW jobs is reached, the source is
exhausted, the user cancels, the time or post budget is used up, or the source
fails. Everything collected so far is kept in every case. A run whose
heartbeat stops (server restart, crash) is marked INTERRUPTED by
``reconcile_interrupted`` and can be continued with a new run: duplicates
are recognised, so only the remaining jobs are collected.

Nothing here applies for a job or contacts anyone. No platform protection is
bypassed: a login wall or security check ends the run with a clear error.
"""

import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.db import connection
from django.db.models import Q
from django.utils import timezone

from tracker.models import JobOpportunity, ScreeningOutcome
from tracker.screening import ScreeningInputError, resolve_profile

from .models import CollectedPost, CollectorRun, CollectorRunStatus, CollectorRunTrigger, DateRange, StopReason
from .service import CREATED, EXISTING, FAILED, INVALID, SKIPPED, _process_post
from .sources import SourceError

logger = logging.getLogger("collector")

OUT_OF_RANGE = "out_of_range"
# READY: new jobs the user can apply to straight away - a complete description, a
# recruiter email or phone, and not rejected by screening against the profile. The
# target counts these, so "30 jobs" means 30 applications can be written.
READY = "ready"
COUNT_KEYS = ("found", CREATED, EXISTING, SKIPPED, INVALID, FAILED, OUT_OF_RANGE, READY)
# Results are sorted newest first: this many dated posts in a row older than the
# requested range means the range is exhausted. Generous, because reposts and promoted
# posts can appear out of date order.
OLDER_IN_A_ROW_TO_STOP = 25
HEARTBEAT_SECONDS = 1.0
QUEUE_SIZE = 100


class CollectionRequestError(Exception):
    def __init__(self, field_name, message):
        super().__init__(message)
        self.field = field_name
        self.message = message


@dataclass(frozen=True)
class DateWindow:
    """What to ask LinkedIn for, and the exact window applied to each post."""

    linkedin_filter: str | None
    start: datetime | None
    end: datetime | None

    def contains(self, posted_at):
        if posted_at is None:
            return True  # undated posts are kept (the date is shown as unknown)
        if self.start and posted_at < self.start:
            return False
        return not (self.end and posted_at > self.end)

    def is_older(self, posted_at):
        return bool(posted_at and self.start and posted_at < self.start)


def date_window(date_range, date_from=None, date_to=None, now=None):
    """Map the UI's date choice to LinkedIn's "Date posted" filter plus an exact window.

    LinkedIn only offers 24 hours / week / month. A custom range uses the narrowest
    LinkedIn filter that covers it and then keeps only posts dated inside the range.
    """
    now = now or timezone.now()
    if date_range in (None, "", DateRange.ANY):
        return DateWindow(None, None, None)
    fixed = {DateRange.PAST_24H: 1, DateRange.PAST_WEEK: 7, DateRange.PAST_MONTH: 30}
    if date_range in fixed:
        return DateWindow(date_range, now - timedelta(days=fixed[date_range]), None)
    if date_range != DateRange.CUSTOM:
        raise CollectionRequestError("date_range", f"Unknown date range: {date_range}.")
    today = timezone.localdate(now)
    if date_from is None:
        raise CollectionRequestError("date_from", "Choose the first day of the range.")
    if date_from > today:
        raise CollectionRequestError("date_from", "The range cannot start in the future.")
    if date_to is not None and date_to < date_from:
        raise CollectionRequestError("date_to", "The last day must be on or after the first day.")
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(date_from, datetime.min.time()), tz)
    end = None
    if date_to is not None and date_to < today:
        end = timezone.make_aware(datetime.combine(date_to, datetime.max.time()), tz)
    age_days = (today - date_from).days + 1
    linkedin = next((value for value, days in fixed.items() if age_days <= days), None)
    return DateWindow(linkedin, start, end)


def max_target():
    return min(settings.COLLECTOR_API_MAX_POSTS_PER_RUN, settings.COLLECTOR_MAX_POSTS_PER_RUN)


def _launch_thread(target):
    thread = threading.Thread(target=target, name="collector-run", daemon=True)
    thread.start()
    return thread


# Tests replace this with a synchronous call.
launch = _launch_thread


def start_collection(source, *, owner, profile, query, target, date_range=DateRange.ANY,
                     date_from=None, date_to=None, trigger=CollectorRunTrigger.API):
    """Validate, record a RUNNING run and start it in the background. Returns the run.

    Raises ScreeningInputError / CollectionRequestError before anything is recorded.
    """
    query = " ".join((query or "").split())
    if not query:
        raise CollectionRequestError("query", "Enter what to search for, e.g. Python Backend Developer.")
    if not isinstance(target, int) or isinstance(target, bool) or not 1 <= target <= max_target():
        raise CollectionRequestError("target", f"Choose a number of jobs from 1 to {max_target()}.")
    date_window(date_range, date_from, date_to)  # validates
    profile, _skills = resolve_profile(profile, owner=owner)
    now = timezone.now()
    run = CollectorRun.objects.create(
        owner=owner, trigger=trigger, source=getattr(source, "name", type(source).__name__),
        profile=profile, keywords=[query], limit=target, search_query=query,
        date_range=date_range or DateRange.ANY, date_from=date_from, date_to=date_to,
        counts={key: 0 for key in COUNT_KEYS}, heartbeat_at=now, stage="Starting",
    )
    run_id = run.pk
    launch(lambda: execute_run(run_id, source))
    return run


def _cancel_requested(run_id):
    return CollectorRun.objects.filter(pk=run_id, cancel_requested=True).exists()


def request_cancel(run):
    """Ask a running collection to stop. It stops after the current post/page and
    keeps everything collected so far."""
    CollectorRun.objects.filter(pk=run.pk, status=CollectorRunStatus.RUNNING).update(
        cancel_requested=True, stage="Cancelling…"
    )


def reconcile_interrupted(now=None):
    """Mark background runs whose heartbeat stopped as INTERRUPTED (server restart, crash)."""
    now = now or timezone.now()
    cutoff = now - timedelta(seconds=settings.COLLECTOR_HEARTBEAT_STALE_SECONDS)
    for run in CollectorRun.objects.filter(status=CollectorRunStatus.RUNNING, heartbeat_at__lt=cutoff):
        run.status = CollectorRunStatus.INTERRUPTED
        run.stop_reason = StopReason.INTERRUPTED
        run.stage = "Interrupted"
        run.error = ("The collection stopped unexpectedly (the server was restarted or stopped). "
                     "Everything collected before that is kept; you can continue the collection.")
        run.finished_at = run.heartbeat_at
        run.save(update_fields=["status", "stop_reason", "stage", "error", "finished_at"])


# ---------------------------------------------------------------------------
# Producer (source thread: no database access)
# ---------------------------------------------------------------------------

def _stream(source, keywords, window, stop, on_stage, max_scrolls):
    stream = getattr(source, "stream_posts", None)
    if stream is not None:
        return stream(keywords, date_posted=window.linkedin_filter, stop=stop, on_stage=on_stage,
                      max_scrolls=max_scrolls)
    return source.iter_posts(keywords, settings.COLLECTOR_MAX_POSTS_PER_RUN * 10)


def _put(q, item, stop, force=False):
    while True:
        try:
            q.put(item, timeout=0.25)
            return True
        except queue.Full:
            if stop.is_set() and not force:
                return False


def _produce(source, keywords, window, stop, stage, q, max_scrolls):
    def on_stage(text):
        stage[0] = str(text)[:200]

    posts = None
    try:
        posts = _stream(source, keywords, window, stop, on_stage, max_scrolls)
        for post in posts:
            if stop.is_set() or not _put(q, ("post", post), stop):
                break
    except SourceError as exc:
        _put(q, ("error", exc), stop, force=True)
    except Exception as exc:  # never leave the consumer waiting
        logger.exception("Collector source crashed")
        _put(q, ("crash", exc), stop, force=True)
    finally:
        close = getattr(posts, "close", None)
        if close is not None:
            try:
                close()  # closes the browser deterministically
            except Exception:
                logger.debug("Ignoring error while closing the source", exc_info=True)
        _put(q, ("done", None), stop, force=True)


# ---------------------------------------------------------------------------
# Consumer (the run's thread)
# ---------------------------------------------------------------------------

class _Run:
    def __init__(self, run):
        self.run = run
        self.counts = {key: int((run.counts or {}).get(key, 0)) for key in COUNT_KEYS}
        self.job_ids = list(run.job_ids or [])
        self.position = 0
        self.older_in_a_row = 0
        self.stop_reason = ""
        self.error = ""

    def save(self, stage=None, **extra):
        run = self.run
        run.counts = dict(self.counts)
        run.job_ids = self.job_ids
        run.heartbeat_at = timezone.now()
        if stage is not None:
            run.stage = stage[:200]
        fields = ["counts", "job_ids", "heartbeat_at", "stage"]
        for name, value in extra.items():
            setattr(run, name, value)
            fields.append(name)
        run.save(update_fields=fields)

    def record(self, post, status, *, title="", job_id=None, matched_by="", reason=""):
        self.counts["found"] += 1
        self.counts[status] += 1
        CollectedPost.objects.create(
            run=self.run, position=self.position, status=status, post_url=(post.url or "")[:1024],
            title=(title or "")[:255], job_id=job_id, matched_by=matched_by or "",
            reason=(reason or "")[:500], posted_at=post.posted_at, truncated=bool(post.truncated),
        )
        self.position += 1
        if job_id and job_id not in self.job_ids:
            self.job_ids.append(job_id)


def _finish_status(state, cancelled):
    counts = state.counts
    if cancelled:
        return CollectorRunStatus.CANCELLED
    if state.stop_reason == StopReason.SOURCE_ERROR:
        return CollectorRunStatus.PARTIAL if counts["found"] else CollectorRunStatus.FAILED
    if counts[FAILED]:
        return CollectorRunStatus.PARTIAL
    return CollectorRunStatus.SUCCEEDED


def execute_run(run_id, source, *, clock=time.monotonic):
    """Run a recorded collection to the end. Never raises; the outcome is on the run."""
    try:
        run = CollectorRun.objects.select_related("profile", "owner").get(pk=run_id)
        try:
            profile, skills = resolve_profile(run.profile, owner=run.owner)
            window = date_window(run.date_range, run.date_from, run.date_to)
        except (ScreeningInputError, CollectionRequestError) as exc:
            _fail(run, exc.message)
            return
        _consume(run, source, profile, skills, window, clock)
    except Exception as exc:  # recorded, never propagated out of the thread
        logger.exception("Collector run %s failed unexpectedly", run_id)
        CollectorRun.objects.filter(pk=run_id, status=CollectorRunStatus.RUNNING).update(
            status=CollectorRunStatus.FAILED, stop_reason=StopReason.FAILED, stage="Failed",
            error=f"{type(exc).__name__}: {exc}", finished_at=timezone.now(),
        )
    finally:
        if threading.current_thread() is not threading.main_thread():
            connection.close()


def _fail(run, message):
    run.status = CollectorRunStatus.FAILED
    run.stop_reason = StopReason.FAILED
    run.error = message
    run.stage = "Failed"
    run.finished_at = timezone.now()
    run.save(update_fields=["status", "stop_reason", "error", "stage", "finished_at"])


def _consume(run, source, profile, skills, window, clock):
    target = run.limit or 1
    state = _Run(run)
    max_examined = max(settings.COLLECTOR_MIN_POSTS_EXAMINED, target * settings.COLLECTOR_POSTS_EXAMINED_PER_JOB)
    max_scrolls = min(settings.COLLECTOR_MAX_SCROLLS_CAP, max(settings.COLLECTOR_MAX_SCROLLS_PER_KEYWORD, target + 10))
    deadline = clock() + settings.COLLECTOR_MAX_RUN_MINUTES * 60

    stop = threading.Event()
    stage = ["Opening the source"]
    q = queue.Queue(maxsize=QUEUE_SIZE)
    producer = threading.Thread(
        target=_produce, args=(source, list(run.keywords), window, stop, stage, q, max_scrolls),
        name=f"collector-source-{run.pk}", daemon=True,
    )
    producer.start()
    state.save(stage=stage[0])

    cancelled, last_beat, done = False, clock(), False
    try:
        while not done:
            try:
                kind, item = q.get(timeout=HEARTBEAT_SECONDS)
            except queue.Empty:
                kind, item = None, None

            if kind == "post" and not stop.is_set():
                _handle_post(state, item, profile, skills, window)
                if state.counts[READY] >= target:
                    state.stop_reason = StopReason.TARGET_REACHED
                elif state.older_in_a_row >= OLDER_IN_A_ROW_TO_STOP:
                    state.stop_reason = StopReason.EXHAUSTED
                elif state.counts["found"] >= max_examined:
                    state.stop_reason = StopReason.EXAMINED_LIMIT
            elif kind in ("error", "crash"):
                state.stop_reason = StopReason.SOURCE_ERROR
                state.error = str(item) if kind == "error" else (
                    f"The source failed unexpectedly ({type(item).__name__}); see the server log.")
            elif kind == "done":
                done = True
                if not state.stop_reason:
                    state.stop_reason = StopReason.EXHAUSTED

            now = clock()
            if not state.stop_reason and now >= deadline:
                state.stop_reason = StopReason.TIME_LIMIT
            if now - last_beat >= HEARTBEAT_SECONDS or kind == "post":
                last_beat = now
                if not cancelled and _cancel_requested(run.pk):
                    cancelled = True
                    state.stop_reason = StopReason.CANCELLED
                state.save(stage="Cancelling…" if cancelled else _progress_stage(state, target, stage[0]))
            if state.stop_reason and not stop.is_set():
                stop.set()  # the producer stops after its current page/post
    finally:
        stop.set()
        producer.join(timeout=settings.COLLECTOR_PAGE_TIMEOUT_SECONDS + 15)

    status = _finish_status(state, cancelled)
    final_stage = {
        StopReason.TARGET_REACHED: f"Done: {state.counts[READY]} job(s) ready to apply",
        StopReason.EXHAUSTED: "Done: no more matching posts",
        StopReason.CANCELLED: "Cancelled",
        StopReason.TIME_LIMIT: "Stopped: time limit reached",
        StopReason.EXAMINED_LIMIT: "Stopped: post limit for this run reached",
        StopReason.SOURCE_ERROR: "Stopped: the source failed",
    }.get(state.stop_reason, "Finished")
    state.save(stage=final_stage, status=status, stop_reason=state.stop_reason,
               error=state.error, finished_at=timezone.now())
    logger.info("Collector run %s finished: %s (%s) %s", run.pk, status, state.stop_reason, state.counts)


def _progress_stage(state, target, source_stage):
    return f"{source_stage} · {state.counts[READY]} of {target} jobs ready to apply"


def _handle_post(state, post, profile, skills, window):
    if not window.contains(post.posted_at):
        state.older_in_a_row = state.older_in_a_row + 1 if window.is_older(post.posted_at) else 0
        state.record(post, OUT_OF_RANGE, reason="Posted outside the chosen date range.")
        return
    if post.posted_at is not None:
        state.older_in_a_row = 0
    outcome = _process_post(state.position, post, profile, skills)
    state.record(post, outcome.status, title=outcome.title or "", job_id=outcome.job_id,
                 matched_by=outcome.matched_by or "", reason=outcome.reason or "")
    if outcome.status == CREATED and outcome.job_id and ready_to_apply(outcome.job_id):
        state.counts[READY] += 1


def ready_to_apply(job_id):
    """A new job the user can apply to now (see READY)."""
    job = JobOpportunity.objects.filter(pk=job_id).only("jd_complete", "screening_outcome").first()
    if job is None or not job.jd_complete or job.screening_outcome == ScreeningOutcome.REJECTED:
        return False
    return job.contacts.filter(~Q(email__isnull=True) & ~Q(email="") | ~Q(phone__isnull=True) & ~Q(phone="")
                               | ~Q(whatsapp__isnull=True) & ~Q(whatsapp="")).exists()
