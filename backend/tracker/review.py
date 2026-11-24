"""
Reviewing collected jobs (Job Automation, phase 2).

* ``match_resume`` - which of the profile's resumes fits a job best.
* ``job_eligibility`` - what stands between a job and an application
  (already applied, cut-off description, no contact, no resume...).
* ``select_job`` - the "Select for application" action: the job becomes
  SHORTLISTED and gets one application (never two for the same profile),
  with the matching resume and the best contact/channel already chosen.
* ``update_job_details`` - the user's corrections to extracted details.

Nothing here generates or sends anything.
"""

import re
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from tracker.applications import (
    POST_SEND_STATUSES,
    ApplicationWorkflowError,
    DuplicateApplicationError,
    create_application,
)
from tracker.ingestion import get_or_create_company, get_or_create_contact, normalize_whitespace
from tracker.models import Application, ApplicationChannel, Contact, JobStatus, ScreeningOutcome, ScreeningReason

_WORD = re.compile(r"[a-z][a-z0-9+#.]*")
_IGNORED_WORDS = {"resume", "cv", "the", "and", "for", "of", "a", "an", "my", "new", "final", "v1", "v2",
                  "updated", "latest", "pdf", "docx", "developer", "engineer"}


def _words(text):
    return {w.strip(".") for w in _WORD.findall((text or "").lower())} - _IGNORED_WORDS


@dataclass
class ResumeMatch:
    resume: object
    reason: str


def usable_resumes(profile, resumes=None):
    resumes = resumes if resumes is not None else profile.resumes.all()
    return [r for r in resumes if r.is_active and r.file_path]


def match_resume(profile, job, resumes=None):
    """The resume to use for ``job``: the one whose name/type shares the most words with
    the job title (e.g. "Backend resume" for "Python Backend Developer"), otherwise the
    default resume, otherwise the only one. None when the profile has no usable resume."""
    candidates = usable_resumes(profile, resumes)
    if not candidates:
        return None
    title = _words(job.title)
    scored = []
    for resume in candidates:
        overlap = title & _words(f"{resume.name} {resume.resume_type or ''}")
        scored.append((len(overlap), resume.is_default, -resume.pk, resume, overlap))
    scored.sort(key=lambda item: item[:3], reverse=True)
    best_score, _default, _pk, best, overlap = scored[0]
    if best_score:
        return ResumeMatch(best, f"Matches “{', '.join(sorted(overlap))}” in the job title")
    default = next((r for r in candidates if r.is_default), None)
    if default:
        return ResumeMatch(default, "Your default resume")
    if len(candidates) == 1:
        return ResumeMatch(candidates[0], "Your only resume")
    return ResumeMatch(sorted(candidates, key=lambda r: r.pk)[0], "First of your resumes (none is set as default)")


def best_contact(job, contacts=None):
    """``(contact, channel)``: a contact with an email (email channel) is preferred,
    then one with WhatsApp/phone. ``(None, None)`` when the post gave no contact."""
    contacts = list(contacts if contacts is not None else job.contacts.all())
    for contact in sorted(contacts, key=lambda c: c.pk):
        if contact.email:
            return contact, ApplicationChannel.EMAIL
    for contact in sorted(contacts, key=lambda c: c.pk):
        if contact.whatsapp or contact.phone:
            return contact, ApplicationChannel.WHATSAPP
    return None, None


ISSUE_MESSAGES = {
    "already_applied": "You already applied for this job.",
    "in_applications": "This job is already in your applications.",
    "incomplete_jd": "The post was cut off. Paste the full description before generating.",
    "no_contact": "No email or phone number was found in the post.",
    "no_resume": "Upload a resume first.",
    "screened_out": "Screening suggests it is not a match.",
    "possible_duplicate": "Looks like a job you already saved.",
    "skipped": "You skipped this job.",
}
BLOCKING = {"already_applied", "incomplete_jd", "no_resume"}


def job_eligibility(job, *, applications, contacts, has_resume):
    """``{"eligible": bool, "issues": [{"code", "message", "blocking"}]}`` for the review UI.

    ``applications``/``contacts`` are the job's (prefetched) rows; ``has_resume`` whether the
    user has a usable resume. Only blocking issues make a job ineligible.
    """
    codes = []
    if any(a.sent_at or a.status in POST_SEND_STATUSES for a in applications):
        codes.append("already_applied")
    elif applications:
        codes.append("in_applications")
    if not job.jd_complete:
        codes.append("incomplete_jd")
    if not any(c.email or c.phone or c.whatsapp for c in contacts):
        codes.append("no_contact")
    if not has_resume:
        codes.append("no_resume")
    if job.screening_reason == ScreeningReason.POSSIBLE_DUPLICATE:
        codes.append("possible_duplicate")
    elif job.screening_outcome == ScreeningOutcome.REJECTED:
        codes.append("screened_out")
    if job.status == JobStatus.SKIPPED:
        codes.append("skipped")
    issues = [{"code": c, "message": ISSUE_MESSAGES[c], "blocking": c in BLOCKING} for c in codes]
    return {"eligible": not any(i["blocking"] for i in issues), "issues": issues}


@dataclass
class SelectResult:
    application: Application
    created: bool


def select_job(job, profile):
    """Select a job for application. Idempotent: the existing application is returned
    when there is one (``created=False``); a second one is never made."""
    existing = Application.objects.filter(profile=profile, job=job).first()
    if existing:
        return SelectResult(existing, created=False)
    with transaction.atomic():
        if job.status not in (JobStatus.SHORTLISTED, JobStatus.APPLIED):
            job.status = JobStatus.SHORTLISTED
            job.save(update_fields=["status", "updated_at"])
        match = match_resume(profile, job)
        contact, channel = best_contact(job)
        try:
            application = create_application(
                profile, job, contact=contact, resume=match.resume if match else None,
                channel=channel or ApplicationChannel.EMAIL,
                note="Selected for application in Job Automation"
                     + (f" (resume: {match.resume.name} - {match.reason.lower()})." if match else "."),
            )
        except DuplicateApplicationError as exc:
            return SelectResult(exc.existing, created=False)
    return SelectResult(application, created=True)


EDITABLE_TEXT = ("title", "location", "work_mode", "recruiter_name", "required_experience_text")


def update_job_details(job, data, owner):
    """Apply the user's corrections. ``data`` is validated (JobReviewUpdateSerializer).

    Edited fields are recorded in ``extraction["edited_fields"]`` so later collections
    never overwrite them. A pasted full description makes the job complete.
    """
    extraction = dict(job.extraction or {})
    edited = set(extraction.get("edited_fields") or [])
    fields = set()
    with transaction.atomic():
        for name in EDITABLE_TEXT:
            if name in data:
                value = normalize_whitespace(data[name] or "")
                if name == "title" and not value:
                    raise ApplicationWorkflowError("title", "A job needs a title.")
                setattr(job, name, value if name in ("title", "recruiter_name") else (value or None))
                fields.add(name)
        if "company_name" in data:
            job.company = get_or_create_company(data["company_name"] or "", owner=owner)
            fields.add("company")
        if "full_jd" in data:
            text = (data["full_jd"] or "").strip()
            if len(text) < 40:
                raise ApplicationWorkflowError("full_jd", "Paste the complete job description (at least 40 characters).")
            job.full_jd = text
            job.jd_complete = True
            fields.update({"full_jd", "jd_complete"})
        if "contacts" in data:
            job.contacts.set(_contacts_from(data["contacts"], job, owner))
            _refresh_application_contacts(job)
            edited.add("contacts")
            job.contacts_verified_at = timezone.now()
            fields.add("contacts_verified_at")
        if data.get("contacts_verified") is True:
            job.contacts_verified_at = timezone.now()
            fields.add("contacts_verified_at")
        elif data.get("contacts_verified") is False:
            job.contacts_verified_at = None
            fields.add("contacts_verified_at")
        if "status" in data:
            job.status = data["status"]
            fields.add("status")
        edited |= {f for f in fields if f in EDITABLE_TEXT or f in ("company", "full_jd")}
        extraction["edited_fields"] = sorted(edited)
        job.extraction = extraction
        job.save()
    return job


def _refresh_application_contacts(job):
    """Unsent applications of this job whose contact is missing or no longer on the job get
    the job's best contact (and its channel), so a recipient the user just added is used."""
    linked = set(job.contacts.values_list("pk", flat=True))
    for application in job.applications.filter(sent_at__isnull=True).exclude(status__in=POST_SEND_STATUSES):
        if application.contact_id in linked:
            continue
        contact, channel = best_contact(job)
        application.contact = contact
        if channel:
            application.channel = channel
        application.save(update_fields=["contact", "channel", "updated_at"])


def _contacts_from(items, job, owner):
    contacts = []
    for item in items:
        contact_id = item.get("id")
        if contact_id:
            contact = Contact.objects.filter(owner=owner, pk=contact_id).first()
            if contact is None:
                raise ApplicationWorkflowError("contacts", f"Contact {contact_id} not found.")
            for name in ("name", "email", "phone", "whatsapp"):
                if name in item:
                    setattr(contact, name, (item[name] or "").strip() or None)
            if not (contact.email or contact.phone or contact.whatsapp):
                raise ApplicationWorkflowError("contacts", "A contact needs an email, phone or WhatsApp number.")
            contact.save()
        else:
            contact = get_or_create_contact(item, job.company, owner=owner)
        if contact not in contacts:
            contacts.append(contact)
    return contacts
