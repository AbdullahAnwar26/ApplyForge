from django.db import models

from .profile import Profile, Resume
from .company import Contact
from .job import JobOpportunity


class ApplicationStatus(models.TextChoices):
    SHORTLISTED = 'SHORTLISTED', 'Shortlisted'
    GENERATED = 'GENERATED', 'Generated'
    OPENED = 'OPENED', 'Opened'
    READY_TO_SEND = 'READY_TO_SEND', 'Ready to Send'
    SENT = 'SENT', 'Sent'
    SKIPPED = 'SKIPPED', 'Skipped'
    REJECTED = 'REJECTED', 'Rejected'
    FAILED = 'FAILED', 'Failed'
    FOLLOW_UP = 'FOLLOW_UP', 'Follow-up'


class ApplicationChannel(models.TextChoices):
    EMAIL = 'EMAIL', 'Email'
    WHATSAPP = 'WHATSAPP', 'WhatsApp'
    OTHER = 'OTHER', 'Other'


class Application(models.Model):
    profile = models.ForeignKey(
        Profile,
        related_name='applications',
        on_delete=models.CASCADE,
    )

    job = models.ForeignKey(
        JobOpportunity,
        related_name='applications',
        on_delete=models.CASCADE,
    )

    contact = models.ForeignKey(
        Contact,
        related_name='applications',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    resume = models.ForeignKey(
        Resume,
        related_name='applications',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    status = models.CharField(
        max_length=50,
        choices=ApplicationStatus.choices,
        default=ApplicationStatus.SHORTLISTED,
        db_index=True,
    )

    channel = models.CharField(
        max_length=50,
        choices=ApplicationChannel.choices,
        default=ApplicationChannel.EMAIL,
    )

    generated_subject = models.CharField(
        max_length=500,
        blank=True,
        null=True,
    )

    generated_body = models.TextField(
        blank=True,
        null=True,
    )

    # A distinct, shorter draft for the WhatsApp handoff - WhatsApp reads
    # as a mismatch when it just gets the full formal email copy/pasted
    # in, so generation produces this separately. Falls back to
    # generated_body when a provider response doesn't include it.
    generated_whatsapp_message = models.TextField(
        blank=True,
        null=True,
    )

    # Application queue
    queue_position = models.PositiveIntegerField(
        blank=True,
        null=True,
        db_index=True,
    )

    last_error = models.TextField(
        blank=True,
        null=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    generated_at = models.DateTimeField(blank=True, null=True)
    opened_at = models.DateTimeField(blank=True, null=True)
    sent_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('profile', 'job')
        ordering = ['queue_position', '-created_at']

    def __str__(self):
        return f"Application for {self.job.title} ({self.status})"


class ApplicationEvent(models.Model):
    application = models.ForeignKey(
        Application,
        related_name='history',
        on_delete=models.CASCADE,
    )

    status = models.CharField(
        max_length=50,
        choices=ApplicationStatus.choices,
    )

    notes = models.TextField(
        blank=True,
        null=True,
    )

    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.application} -> {self.status} at {self.timestamp}"