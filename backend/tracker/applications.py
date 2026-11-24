"""
Application tracking workflow (Milestone 4).

Three separate layers (docs/PROJECT_SPEC.md):

* ``JobOpportunity.screening_outcome`` - the automatic screening verdict.
* ``JobOpportunity.status`` - the user's decision about the job. An
  application can only be created for a job the user has set to
  ``SHORTLISTED`` (or ``APPLIED``).
* ``Application.status`` - progress after the user chooses to apply.

Nothing here touches the job's screening fields, and nothing here sends
email or WhatsApp messages. ``SENT`` records that the user sent it manually
(locked rule 9). The one job-status effect (user decision, Milestone 5): an
explicit change to ``SENT`` sets the job's status to ``APPLIED``. That is
never reversed automatically.

Status rules (existing ``ApplicationStatus`` choices only):

* ``GENERATED`` and ``FAILED`` are set by content generation (Milestone 5),
  never directly by the user.
* ``SENT`` stamps ``sent_at`` and ``OPENED`` stamps ``opened_at`` (first
  time only).
* ``FOLLOW_UP``, ``INTERVIEW``, ``OFFER`` and ``REJECTED`` (employer
  rejection) require the application to have been sent.
* Once sent, an application cannot move back to a pre-send status
  (``SHORTLISTED``, ``OPENED``, ``READY_TO_SEND``, ``SKIPPED``).
* Every status change writes one ``ApplicationEvent``. An unchanged status
  writes none. Notes without a status change are ``NOTE`` events.

Follow-up reminders (``follow_up_on``): marking an application ``SENT`` sets the
reminder ``FOLLOW_UP_DEFAULT_DAYS`` ahead unless one is already set; marking it
``REJECTED`` clears it. A reminder is *due* on and after its day while the
application is still open (``FOLLOW_UP_OPEN_STATUSES``). Reminders are shown in
the app only; nothing is ever sent.
"""

from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from tracker.models import (
    Application,
    ApplicationEvent,
    ApplicationEventKind,
    ApplicationStatus,
    JobOpportunity,
    JobStatus,
    SendRecord,
    SendRecordStatus,
)

S = ApplicationStatus

# SEND_FAILED and COMPOSE_OPENED are set by the sending service (tracker.sending): a
# provider failed / the message was opened in the user's email app or WhatsApp.
SYSTEM_ONLY_STATUSES = frozenset({S.GENERATED, S.FAILED, S.SEND_FAILED, S.COMPOSE_OPENED})
PRE_SEND_STATUSES = frozenset({S.SHORTLISTED, S.GENERATED, S.OPENED, S.READY_TO_SEND, S.SKIPPED, S.FAILED,
                               S.SEND_FAILED, S.COMPOSE_OPENED})
POST_SEND_STATUSES = frozenset({S.SENT, S.FOLLOW_UP, S.INTERVIEW, S.OFFER, S.REJECTED})
REQUIRES_SENT = frozenset({S.FOLLOW_UP, S.INTERVIEW, S.OFFER, S.REJECTED})
# Statuses where the next step is the user's (dashboard "awaiting action").
AWAITING_USER_ACTION = (
    S.SHORTLISTED, S.GENERATED, S.OPENED, S.READY_TO_SEND, S.FAILED, S.SEND_FAILED, S.COMPOSE_OPENED,
    S.FOLLOW_UP,
    S.INTERVIEW, S.OFFER,
)
# A follow-up reminder counts as due only while the application is still open.
FOLLOW_UP_OPEN_STATUSES = (S.SENT, S.FOLLOW_UP, S.INTERVIEW, S.OFFER)
APPLICABLE_JOB_STATUSES = frozenset({JobStatus.SHORTLISTED, JobStatus.APPLIED})


class ApplicationWorkflowError(Exception):
    def __init__(self, field_name, message):
        super().__init__(message)
        self.field = field_name
        self.message = message


class DuplicateApplicationError(Exception):
    def __init__(self, existing):
        super().__init__("An application for this profile and job already exists.")
        self.existing = existing


def validate_relationships(profile, job, contact=None, resume=None, channel=None):
    """Check contact/resume/channel consistency. Raises ApplicationWorkflowError."""
    if contact is not None and not job.contacts.filter(pk=contact.pk).exists():
        raise ApplicationWorkflowError("contact", "This contact is not linked to the job.")
    if resume is not None:
        if resume.profile_id != profile.pk:
            raise ApplicationWorkflowError("resume", "This resume belongs to a different profile.")
        if not resume.is_active:
            raise ApplicationWorkflowError("resume", "This resume is inactive.")
    if contact is not None and channel == "EMAIL" and not contact.email:
        raise ApplicationWorkflowError("channel", "The selected contact has no email address.")
    if contact is not None and channel == "WHATSAPP" and not (contact.whatsapp or contact.phone):
        raise ApplicationWorkflowError("channel", "The selected contact has no WhatsApp or phone number.")


def default_resume(profile):
    return profile.resumes.filter(is_active=True, is_default=True).order_by("id").first()


def create_application(profile, job, contact=None, resume=None, channel=None, note=None):
    """Create a SHORTLISTED application for an explicitly chosen job."""
    if job.status not in APPLICABLE_JOB_STATUSES:
        raise ApplicationWorkflowError(
            "job",
            f"Job {job.pk} has status {job.status}. Set the job to SHORTLISTED "
            "before creating an application.",
        )
    existing = Application.objects.filter(profile=profile, job=job).first()
    if existing:
        raise DuplicateApplicationError(existing)

    channel = channel or Application._meta.get_field("channel").default
    if resume is None:
        resume = default_resume(profile)
    validate_relationships(profile, job, contact, resume, channel)

    try:
        with transaction.atomic():
            application = Application.objects.create(
                profile=profile, job=job, contact=contact, resume=resume, channel=channel,
                status=S.SHORTLISTED,
            )
            ApplicationEvent.objects.create(
                application=application,
                status=application.status,
                notes=note or "Application created.",
            )
    except IntegrityError:
        existing = Application.objects.filter(profile=profile, job=job).first()
        if existing:
            raise DuplicateApplicationError(existing)
        raise
    return application


def check_status_change(application, new_status):
    """Raise ApplicationWorkflowError if ``new_status`` is not allowed."""
    if new_status == application.status:
        return
    if new_status in SYSTEM_ONLY_STATUSES:
        raise ApplicationWorkflowError(
            "status", f"{new_status} is set by ApplyForge (generation or sending), not manually."
        )
    sent = application.sent_at is not None or application.status in POST_SEND_STATUSES
    if new_status in REQUIRES_SENT and not sent:
        raise ApplicationWorkflowError(
            "status", f"{new_status} is only allowed after the application was sent."
        )
    if sent and new_status in PRE_SEND_STATUSES:
        raise ApplicationWorkflowError(
            "status", f"The application was already sent; it cannot move back to {new_status}."
        )


def change_status(application, new_status, note=None):
    """Apply a status change and record one event. Returns True if it changed.

    Caller must have validated the change with ``check_status_change``.
    Saves the application.
    """
    old_status = application.status
    if new_status == old_status:
        return False
    now = timezone.now()
    application.status = new_status
    if new_status == S.OPENED and application.opened_at is None:
        application.opened_at = now
    if new_status == S.SENT and application.sent_at is None:
        application.sent_at = now
    if new_status == S.SENT and application.follow_up_on is None:
        application.follow_up_on = timezone.localdate(now) + timedelta(days=settings.FOLLOW_UP_DEFAULT_DAYS)
    if new_status == S.REJECTED:
        application.follow_up_on = None
    application.save()
    if old_status == S.COMPOSE_OPENED:
        _close_open_handoffs(application, new_status, now)
    ApplicationEvent.objects.create(
        application=application,
        status=new_status,
        notes=note or f"Status changed from {old_status} to {new_status}.",
    )
    if new_status == S.SENT:
        # Only the job's workflow status; screening fields are never touched.
        JobOpportunity.objects.filter(pk=application.job_id).exclude(
            status=JobStatus.APPLIED
        ).update(status=JobStatus.APPLIED, updated_at=now)
    return True


def _close_open_handoffs(application, new_status, now):
    """Leaving COMPOSE_OPENED another way (e.g. the user set the status by hand): the open
    hand-off ends - confirmed by the user if they marked it SENT, otherwise not sent.
    (The sending service closes its own records before changing the status.)"""
    outcome = SendRecordStatus.CONFIRMED if new_status == S.SENT else SendRecordStatus.ABANDONED
    SendRecord.objects.filter(application=application, status=SendRecordStatus.COMPOSE_OPENED).update(
        status=outcome, finished_at=now
    )


def add_note(application, text):
    """Record a note without changing the status. Returns the event."""
    return ApplicationEvent.objects.create(
        application=application,
        status=application.status,
        kind=ApplicationEventKind.NOTE,
        notes=text,
    )


def follow_ups_due(queryset, today=None):
    """Open applications whose follow-up day has come, earliest first."""
    today = today or timezone.localdate()
    return queryset.filter(
        follow_up_on__isnull=False, follow_up_on__lte=today, status__in=FOLLOW_UP_OPEN_STATUSES
    ).order_by("follow_up_on", "id")
