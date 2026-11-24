"""
Collector run history (Milestone 6, Phase 6c).

One row per collection run, started by the ``collect_linkedin`` command or by
``POST /api/v1/collector/runs/``. Only run metadata is stored - never post
text, author names, headlines or contact details (those live, when hiring
posts are ingested, in the tracker's own models).
"""

from django.conf import settings
from django.db import models
from django.utils import timezone


class CollectorRunStatus(models.TextChoices):
    RUNNING = "RUNNING", "Running"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    PARTIAL = "PARTIAL", "Partially succeeded"   # source failed after some posts, or posts failed
    FAILED = "FAILED", "Failed"
    CANCELLED = "CANCELLED", "Cancelled"         # the user stopped it; what was collected is kept
    INTERRUPTED = "INTERRUPTED", "Interrupted"   # the server stopped mid-run (restart, crash)


class DateRange(models.TextChoices):
    ANY = "any", "Any time"
    PAST_24H = "past-24h", "Last 24 hours"
    PAST_WEEK = "past-week", "Last 7 days"
    PAST_MONTH = "past-month", "Last 30 days"
    CUSTOM = "custom", "Custom range"


class StopReason(models.TextChoices):
    TARGET_REACHED = "target_reached", "Target reached"
    EXHAUSTED = "exhausted", "No more results"
    CANCELLED = "cancelled", "Cancelled"
    TIME_LIMIT = "time_limit", "Time limit reached"
    EXAMINED_LIMIT = "examined_limit", "Post limit reached"
    SOURCE_ERROR = "source_error", "Source error"
    INTERRUPTED = "interrupted", "Interrupted"
    FAILED = "failed", "Failed"


class CollectorRunTrigger(models.TextChoices):
    COMMAND = "command", "Management command"
    API = "api", "API"


class CollectorRun(models.Model):
    # The account this belongs to (accounts milestone). Every API query is limited to the
    # signed-in user's rows. NULL only for data created before accounts existed; such rows
    # are invisible until claimed (first account created, or `manage.py claim_unowned_data`).
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="collector_runs", on_delete=models.CASCADE,
        null=True, blank=True, db_index=True,
    )
    status = models.CharField(
        max_length=20, choices=CollectorRunStatus.choices, default=CollectorRunStatus.RUNNING, db_index=True
    )
    trigger = models.CharField(max_length=10, choices=CollectorRunTrigger.choices)
    source = models.CharField(max_length=20, help_text="Post source, e.g. 'linkedin' or 'file'.")
    profile = models.ForeignKey(
        "tracker.Profile", related_name="collector_runs", on_delete=models.SET_NULL, null=True, blank=True
    )
    keywords = models.JSONField(default=list, blank=True)
    limit = models.PositiveIntegerField(null=True, blank=True)
    # {"found", "created", "existing", "skipped", "invalid", "failed"}
    counts = models.JSONField(default=dict, blank=True)
    # Jobs created or matched by this run (ids only).
    job_ids = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True, default="")
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    # Progressive collection (Job Automation). ``limit`` is the target number of NEW jobs.
    search_query = models.CharField(max_length=200, blank=True, default="")
    date_range = models.CharField(max_length=20, choices=DateRange.choices, default=DateRange.ANY)
    date_from = models.DateField(null=True, blank=True)
    date_to = models.DateField(null=True, blank=True)
    cancel_requested = models.BooleanField(default=False)
    # Updated while a background run is alive; a stale heartbeat means it was interrupted.
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    stage = models.CharField(max_length=200, blank=True, default="")
    stop_reason = models.CharField(max_length=30, choices=StopReason.choices, blank=True, default="")

    class Meta:
        ordering = ["-started_at", "-id"]

    def __str__(self):
        return f"Collector run {self.pk} ({self.source}, {self.status})"

    @property
    def duration_seconds(self):
        if self.finished_at is None:
            return None
        return round((self.finished_at - self.started_at).total_seconds(), 3)


class CollectedPostStatus(models.TextChoices):
    CREATED = "created", "New job"
    EXISTING = "existing", "Duplicate (already saved)"
    SKIPPED = "skipped", "Not a job post"
    INVALID = "invalid", "Could not be read"
    FAILED = "failed", "Failed"
    OUT_OF_RANGE = "out_of_range", "Outside the date range"


class CollectedPost(models.Model):
    """One post examined by a run: what happened to it. Post text is not stored here
    (a hiring post's full text is kept on its job)."""

    run = models.ForeignKey(CollectorRun, related_name="items", on_delete=models.CASCADE)
    position = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=CollectedPostStatus.choices, db_index=True)
    post_url = models.URLField(max_length=1024, blank=True, default="")
    title = models.CharField(max_length=255, blank=True, default="")
    job = models.ForeignKey(
        "tracker.JobOpportunity", related_name="collected_posts", on_delete=models.SET_NULL,
        null=True, blank=True,
    )
    matched_by = models.CharField(max_length=30, blank=True, default="")
    reason = models.CharField(max_length=500, blank=True, default="")
    posted_at = models.DateTimeField(null=True, blank=True)
    truncated = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["run", "position"]
        constraints = [
            models.UniqueConstraint(fields=["run", "position"], name="unique_collected_post_position"),
        ]
