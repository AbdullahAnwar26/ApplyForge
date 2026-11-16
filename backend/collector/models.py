"""
Collector run history (Milestone 6, Phase 6c).

One row per collection run, started by the ``collect_linkedin`` command or by
``POST /api/v1/collector/runs/``. Only run metadata is stored - never post
text, author names, headlines or contact details (those live, when hiring
posts are ingested, in the tracker's own models).
"""

from django.db import models
from django.utils import timezone


class CollectorRunStatus(models.TextChoices):
    RUNNING = "RUNNING", "Running"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    PARTIAL = "PARTIAL", "Partially succeeded"   # source failed after some posts, or posts failed
    FAILED = "FAILED", "Failed"


class CollectorRunTrigger(models.TextChoices):
    COMMAND = "command", "Management command"
    API = "api", "API"


class CollectorRun(models.Model):
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

    class Meta:
        ordering = ["-started_at", "-id"]

    def __str__(self):
        return f"Collector run {self.pk} ({self.source}, {self.status})"

    @property
    def duration_seconds(self):
        if self.finished_at is None:
            return None
        return round((self.finished_at - self.started_at).total_seconds(), 3)
