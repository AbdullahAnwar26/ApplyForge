"""
Sending applications (Job Automation, phase 5).

Email: through the user's own mailbox over SMTP (``SendingAccount``; for Gmail
or Outlook an app password). Django's mail framework sends it, so the
``EMAIL_BACKEND`` setting decides the transport: SMTP in production, the
in-memory outbox in tests, a file or console backend for local development -
nothing leaves the machine unless SMTP is configured.

WhatsApp: a click-to-chat link (``https://wa.me/<number>?text=...``) opens
WhatsApp with the drafted message; the user attaches the resume and presses
send there, then confirms in ApplyForge. ApplyForge never automates WhatsApp
(Web) itself. The official WhatsApp Business Cloud API is not used: it can
only start conversations with pre-approved templates from a business number,
which does not fit a job seeker writing to a recruiter.

Approval is mandatory and enforced here, not only in the UI:

* the application must be approved (``READY_TO_SEND``), or have failed to send
  (``SEND_FAILED``, a retry);
* the request must carry ``confirm=true`` and the ``version`` of the exact
  preview the user saw (recipient, sender, subject, body, resume). Any change
  after the preview - an edited draft, another resume - is refused;
* one send at a time per application, and never a second email once sent.

Every attempt is recorded as a ``SendRecord``; the application history gets
an event for each outcome.

Hand-off (the user sends from their own email app or WhatsApp)
--------------------------------------------------------------
``open_compose`` prepares the approved message for the user to send themselves:

* email, ``mailbox``: the email (recipient, subject, body, resume attached) is saved as a
  draft in the user's own mailbox over IMAP (``tracker.mailbox``) and the provider's
  Drafts page is opened. A ``mailto:`` link is not used: it cannot carry an attachment.
* email, ``eml``: a draft file (``X-Unsent: 1``) that desktop email apps (Outlook,
  Thunderbird, Apple Mail) open as a new message, attachment included.
* WhatsApp: the ``wa.me`` click-to-chat link with the message (no attachment possible).

The record is ``COMPOSE_OPENED`` and so is the application (if it was not sent yet).
Opening never means sent. The hand-off ends with ``confirm_handoff`` (the user says
they sent it: CONFIRMED), ``verify_handoff`` (a matching message is found in the
mailbox's Sent folder: VERIFIED) or ``abandon_handoff`` (not sent: ABANDONED, back to
READY_TO_SEND so it can be opened again). One open hand-off per application and one
sent record per channel (database constraints); a confirmation is accepted once.

An application can be sent on both channels (e.g. an email and a WhatsApp message to the
same recruiter): the first one makes it SENT, the second is added to its history.
"""

import hashlib
import logging
import os
import re
import smtplib
import socket
from dataclasses import dataclass, field
from datetime import timedelta
from email.utils import formataddr, make_msgid
from urllib.parse import quote

from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.core.validators import validate_email
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone

from tracker.applications import add_note, change_status
from tracker.credentials import CredentialError, decrypt
from tracker.mailbox import MailboxError, app_password, drafts_page_url, local_mode, mailbox_for
from tracker.models import (
    SENT_RECORD_STATUSES,
    Application,
    ApplicationChannel,
    ApplicationStatus,
    SendingAccount,
    SendRecord,
    SendRecordStatus,
    SmtpSecurity,
)

logger = logging.getLogger("tracker.sending")

S = ApplicationStatus
R = SendRecordStatus
# Approved (or a failed attempt to retry). After the first channel was sent (SENT /
# FOLLOW_UP), the other channel can still be sent once.
SENDABLE_STATUSES = frozenset({S.READY_TO_SEND, S.SEND_FAILED})
SECOND_CHANNEL_STATUSES = frozenset({S.SENT, S.FOLLOW_UP})
# The hand-off sends nothing (the user presses Send in their own app), so the click on
# "Send" is itself the approval: a generated draft can be opened directly.
HANDOFF_STATUSES = SENDABLE_STATUSES | {S.GENERATED, S.OPENED}
POST_SEND = frozenset({S.SENT, S.FOLLOW_UP, S.INTERVIEW, S.OFFER, S.REJECTED})
CHANNEL_NAME = {ApplicationChannel.EMAIL: "email", ApplicationChannel.WHATSAPP: "WhatsApp"}
HANDOFF_METHODS = {ApplicationChannel.EMAIL: ("mailbox", "eml"), ApplicationChannel.WHATSAPP: ("link",)}
PROVIDER_FOR_METHOD = {"mailbox": "mailbox_draft", "eml": "eml_file", "link": "whatsapp_link"}
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
ALLOWED_ATTACHMENT_EXTENSIONS = (".pdf", ".docx", ".doc")
SENDING_STALE_MINUTES = 10


class SendingError(Exception):
    """The send was refused before anything left. ``field`` names the reason."""

    def __init__(self, field_name, message, http_status=400):
        super().__init__(message)
        self.field = field_name
        self.message = message
        self.http_status = http_status


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

@dataclass
class Attachment:
    resume_id: int | None = None
    name: str = ""
    size: int | None = None
    ok: bool = False
    problem: str = ""


@dataclass
class Preview:
    channel: str
    recipient: str = ""
    recipient_source: str = ""  # "post" (found in the post), "edited" (added/changed by the user)
    sender: str = ""
    subject: str = ""
    body: str = ""
    attachment: Attachment = field(default_factory=Attachment)
    whatsapp_link: str = ""
    problems: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def ready(self):
        return not self.problems

    @property
    def version(self):
        parts = [self.channel, self.recipient, self.sender, self.subject, self.body,
                 str(self.attachment.resume_id), self.attachment.name, str(self.attachment.size)]
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]

    def as_dict(self):
        return {
            "channel": self.channel, "recipient": self.recipient, "recipient_source": self.recipient_source,
            "sender": self.sender, "subject": self.subject, "body": self.body,
            "attachment": self.attachment.__dict__, "whatsapp_link": self.whatsapp_link,
            "problems": self.problems, "notes": self.notes, "ready": self.ready, "version": self.version,
        }


def _attachment(application):
    resume = application.resume
    if resume is None:
        return Attachment(problem="No resume is selected for this application.")
    stored = os.path.basename(resume.file_path.name) if resume.file_path else ""
    info = Attachment(resume_id=resume.pk, name=attachment_name(application.profile.name, stored))
    if not resume.is_active:
        info.problem = "The selected resume is inactive."
    elif not resume.file_path:
        info.problem = "The selected resume has no uploaded file."
    elif not stored.lower().endswith(ALLOWED_ATTACHMENT_EXTENSIONS):
        info.problem = "Only PDF or Word resumes can be attached."
    else:
        try:
            info.size = resume.file_path.size
        except (FileNotFoundError, OSError):
            info.problem = "The resume file is missing from storage. Upload it again."
        else:
            if info.size == 0:
                info.problem = "The resume file is empty."
            elif info.size > MAX_ATTACHMENT_BYTES:
                info.problem = "The resume file is larger than 10 MB."
    info.ok = not info.problem
    return info


def attachment_name(candidate_name, stored_name):
    """The file name the recruiter sees: "<Candidate_Name>_Resume.<ext>" (the stored name
    carries a storage suffix such as "_htgUsSm")."""
    ext = os.path.splitext(stored_name)[1].lower()
    words = re.findall(r"[A-Za-z0-9]+", candidate_name or "")
    return f"{'_'.join(words) or 'Resume'}{'_Resume' if words else ''}{ext}" if stored_name else ""


def _in_post(job, value):
    """True when ``value`` (an email or a number) appears in the original post or in the
    description the user corrected."""
    text = "\n".join((job.source_post_text or "", job.full_jd or "")).lower()
    if "@" in value:
        return value.lower() in text
    digits = re.sub(r"\D", "", value)
    return bool(digits) and digits[-10:] in re.sub(r"\D", "", text)


def whatsapp_digits(number, country_code=""):
    """International digits for wa.me, or ``("", problem)``."""
    raw = (number or "").strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("00"):
        digits = digits[2:]
    elif raw.lstrip("(").startswith("+"):
        pass
    elif digits.startswith("0") or len(digits) == 10:
        # A national number: a leading "0" is the trunk prefix, never part of the international number.
        if not country_code:
            return "", ("The number has no country code. Add it to the number (e.g. +91…) or set your "
                        "phone country code on your profile.")
        digits = country_code + (digits[1:] if digits.startswith("0") else digits)
    if not 11 <= len(digits) <= 15:
        return "", "This does not look like a full international phone number."
    return digits, ""


# Places that mean the job is in India (+91), for numbers written without a country code.
_INDIA = re.compile(
    r"\b(india|bengaluru|bangalore|mumbai|pune|hyderabad|chennai|delhi|new delhi|noida|gurgaon|gurugram|"
    r"kolkata|ahmedabad|jaipur|kochi|cochin|indore|chandigarh|coimbatore|thiruvananthapuram|trivandrum|"
    r"bhubaneswar|nagpur|lucknow|vadodara|mysuru|mysore|visakhapatnam|lpa|lakhs?)\b|₹", re.IGNORECASE)
# A whole international number in the text: "+" or "00", then 11-15 digits.
_CODED_NUMBER = re.compile(r"(?:\+|\b00)\s?(\d[\d\s().-]{8,20}\d)")
KNOWN_COUNTRY_CODES = ("880", "971", "966", "974", "977", "353", "91", "44", "65", "61", "49", "60", "31", "33", "92",
                       "94", "1")


def country_code_for(application, number):
    """``(code, how)`` for a number written without a country code: the user's profile
    setting; otherwise another number in the same post written with a code; otherwise +91
    when the job is in India. ``("", "")`` when it cannot be told."""
    if application.profile.phone_country_code:
        return application.profile.phone_country_code, "profile"
    job = application.job
    text = "\n".join(filter(None, (job.source_post_text, job.full_jd)))
    own = re.sub(r"\D", "", number)
    for match in _CODED_NUMBER.finditer(text):
        found = re.sub(r"\D", "", match.group(1))
        if 11 <= len(found) <= 15:
            code = next((c for c in KNOWN_COUNTRY_CODES if found.startswith(c)), "")  # longest codes first
            if code:
                return code, "post"
    if _INDIA.search(" ".join(filter(None, (job.location, text)))) and len(own.lstrip("0")) == 10:
        return "91", "india"
    return "", ""


def resolve_whatsapp(application, number):
    """``(digits, problem, note)``: the wa.me number for ``number`` and, when its country
    code was not written in the number, how the code was chosen (shown to the user)."""
    raw = (number or "").strip()
    has_code = raw.startswith("00") or raw.lstrip("(").startswith("+") or len(re.sub(r"\D", "", raw)) > 11
    code, how = ("", "") if has_code else country_code_for(application, raw)
    digits, problem = whatsapp_digits(raw, code)
    note = ""
    if digits and how == "post":
        note = f"The number has no country code: +{code} was used, as in the other number in the post."
    elif digits and how == "india":
        note = "The number has no country code: +91 (India) was used because the job is in India."
    return digits, problem, note


def build_preview(application, channel, handoff=False):
    channel = channel or application.channel
    job, contact = application.job, application.contact
    preview = Preview(channel=channel)
    preview.attachment = _attachment(application)
    if not preview.attachment.ok:
        preview.problems.append(preview.attachment.problem)
    blocked = send_block(application, channel, list(application.send_records.all()), handoff=handoff)
    if blocked:
        preview.problems.append(blocked)

    if channel == ApplicationChannel.EMAIL:
        preview.subject = application.generated_subject or ""
        preview.body = application.generated_body or ""
        recipient = (contact.email or "") if contact else ""
        account = SendingAccount.objects.filter(owner=application.profile.owner).first()
        if account is None:
            preview.problems.append("Connect your email account (Account → Email sending) to send from ApplyForge.")
        else:
            preview.sender = formataddr((account.from_name, account.from_email)) if account.from_name \
                else account.from_email
            if not account.password_encrypted:
                preview.problems.append("Your email account has no saved password.")
        if not preview.subject or not preview.body:
            preview.problems.append("Generate or write the email subject and body first.")
        if not recipient:
            preview.problems.append("No recipient email address. Add the recruiter's email to the job.")
        else:
            try:
                validate_email(recipient)
            except DjangoValidationError:
                preview.problems.append(f"“{recipient}” is not a valid email address.")
        preview.recipient = recipient
    elif channel == ApplicationChannel.WHATSAPP:
        preview.body = application.generated_whatsapp_message or ""
        number = ((contact.whatsapp or contact.phone) or "") if contact else ""
        preview.recipient = number
        if not number:
            preview.problems.append("No WhatsApp or phone number. Add the recruiter's number to the job.")
        else:
            digits, problem, note = resolve_whatsapp(application, number)
            if problem:
                preview.problems.append(problem)
            if note:
                preview.notes.append(note)
            if digits and preview.body:
                preview.whatsapp_link = f"https://wa.me/{digits}?text={quote(preview.body)}"
        if not preview.body:
            preview.problems.append("Generate or write the WhatsApp message first.")
        preview.notes.append("WhatsApp links cannot attach files: attach your resume in WhatsApp before sending.")
    else:
        preview.problems.append("Choose email or WhatsApp.")

    if preview.recipient:
        preview.recipient_source = "post" if _in_post(job, preview.recipient) else "edited"
        if preview.recipient_source == "edited":
            preview.notes.append("The recipient was not found in the original post; check it before sending.")
    return preview


def send_block(application, channel, records, handoff=False):
    """Why ``channel`` cannot be sent (or handed off) now, or "". ``records``: the
    application's SendRecords."""
    name = CHANNEL_NAME.get(channel, "this channel")
    sent_channels = {r.channel for r in records if r.status in SENT_RECORD_STATUSES}
    if channel in sent_channels:
        return f"Already sent by {name}."
    if any(r.status == R.COMPOSE_OPENED for r in records):
        return ("You opened this application to send it yourself. Confirm that you sent it, or mark it "
                "as not sent, first.")
    sent = application.sent_at is not None or application.status in POST_SEND
    if sent:
        if application.status not in SECOND_CHANNEL_STATUSES:
            return "This application was already sent."
        if not sent_channels and application.channel == channel:
            return "This application was already sent."  # marked sent by hand, before send records
        return ""
    if application.status not in (HANDOFF_STATUSES if handoff else SENDABLE_STATUSES):
        return "Approve the drafts first." if not handoff else "Generate the drafts first."
    return ""


def channel_states(application):
    """Per channel, where sending stands (for the application cards):
    ``{"EMAIL": {"state": "none|open|sent|failed", ...}, "WHATSAPP": {...}}``. Uses the
    prefetched ``send_records``."""
    records = list(application.send_records.all())  # newest first
    states = {}
    for channel in (ApplicationChannel.EMAIL, ApplicationChannel.WHATSAPP):
        mine = [r for r in records if r.channel == channel]
        done = next((r for r in mine if r.status in SENT_RECORD_STATUSES), None)
        open_ = next((r for r in mine if r.status == R.COMPOSE_OPENED), None)
        state = {"state": "none", "record": None, "status": "", "provider": "", "recipient": "", "at": None,
                 "open_url": "", "can_verify": False, "error": ""}
        current = done or open_ or (mine[0] if mine else None)
        if current is not None:
            state.update(record=current.pk, status=current.status, provider=current.provider,
                         recipient=current.recipient,
                         at=current.finished_at if done is not None else current.created_at)
        if done is not None:
            state["state"] = "sent"
        elif open_ is not None:
            state.update(state="open", open_url=open_.open_url, can_verify=open_.provider == "mailbox_draft")
        elif mine and mine[0].status == R.FAILED:
            state.update(state="failed", error=mine[0].error)
        elif not records and application.channel == channel and (
                application.sent_at or application.status in POST_SEND):
            state.update(state="sent", status="MARKED", at=application.sent_at)  # marked sent by hand
        states[channel] = state
    return states


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def _connection(account, timeout=30):
    try:
        password = decrypt(account.password_encrypted)
    except CredentialError as exc:
        raise SendingError("account", str(exc)) from exc
    return get_connection(
        host=account.smtp_host, port=account.smtp_port, username=account.username.strip(),
        password=app_password(password),
        use_tls=account.security == SmtpSecurity.STARTTLS, use_ssl=account.security == SmtpSecurity.SSL,
        timeout=timeout, fail_silently=False,
    )


def _describe_smtp_error(exc):
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "The email server rejected the username or password (for Gmail/Outlook use an app password)."
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "The email server refused the recipient address."
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return "The email server refused the sender address."
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "The email server did not respond in time."
    if isinstance(exc, (ConnectionError, socket.gaierror, OSError)) and not isinstance(exc, smtplib.SMTPException):
        return f"Could not connect to the email server ({type(exc).__name__})."
    return f"Sending failed: {exc}"[:500]


def _check_request(application, channel, version, confirm, preview):
    if confirm is not True:
        raise SendingError("confirm", "Sending needs your explicit confirmation.")
    _check_preview(version, preview)


def _check_preview(version, preview):
    if not preview.ready:
        raise SendingError("non_field_errors", " ".join(preview.problems))
    if version != preview.version:
        raise SendingError(
            "version", "The message, recipient or resume changed since you reviewed it. Review it again.",
            http_status=409,
        )


def _claim(application, preview, provider=None, handoff=False):
    """Lock the application and create the SENDING record (one send at a time)."""
    with transaction.atomic():
        current = Application.objects.select_for_update().get(pk=application.pk)
        stale = timezone.now() - timedelta(minutes=SENDING_STALE_MINUTES)
        records = list(SendRecord.objects.filter(application=current))
        blocked = send_block(current, preview.channel, records, handoff=handoff)
        if blocked:
            raise SendingError("status", blocked, 409)
        if any(r.status == R.SENDING and r.created_at >= stale for r in records):
            raise SendingError("status", "This application is being sent right now.", 409)
        record = SendRecord.objects.create(
            application=current, channel=preview.channel,
            provider=provider or ("smtp" if preview.channel == ApplicationChannel.EMAIL else "whatsapp_link"),
            status=SendRecordStatus.SENDING, recipient=preview.recipient, sender=preview.sender,
            subject=preview.subject, body=preview.body, resume_id=preview.attachment.resume_id,
            attachment_name=preview.attachment.name, attachment_size=preview.attachment.size,
        )
    return current, record


def _record_sent(application_pk, channel, note):
    """The channel was sent: SENT for the first channel, a history note for the second."""
    with transaction.atomic():
        locked = Application.objects.select_for_update().get(pk=application_pk)
        locked.last_error = None
        if locked.sent_at is None and locked.status not in POST_SEND:
            locked.channel = channel
            change_status(locked, S.SENT, note=note)
        else:
            locked.save(update_fields=["last_error", "updated_at"])
            add_note(locked, note)
    return locked


def _finish(record, status, *, error="", message_id=""):
    record.status = status
    record.error = error
    record.message_id = message_id
    record.finished_at = timezone.now()
    record.save(update_fields=["status", "error", "message_id", "finished_at"])


def send_email(application, *, version, confirm):
    """Send the approved email with the resume attached. Returns the SendRecord.

    Raises SendingError when refused (nothing sent). A delivery failure is recorded
    (record FAILED, application SEND_FAILED) and returned, not raised.
    """
    preview = build_preview(application, ApplicationChannel.EMAIL)
    _check_request(application, ApplicationChannel.EMAIL, version, confirm, preview)
    account = SendingAccount.objects.get(owner=application.profile.owner)
    connection = _connection(account)
    current, record = _claim(application, preview)

    message_id = _message_id(account)
    try:
        email = _email_message(
            to=preview.recipient, sender=preview.sender, reply_to=account.from_email, subject=preview.subject,
            body=preview.body, resume=current.resume, attachment=preview.attachment.name, message_id=message_id,
        )
        email.connection = connection
        email.send(fail_silently=False)
    except Exception as exc:  # recorded and shown; the drafts stay as they are
        error = _describe_smtp_error(exc)
        logger.warning("Sending application %s failed: %s", current.pk, type(exc).__name__)
        _attempt_failed(current, record, error, f"Email to {preview.recipient} failed: {error}")
        return record

    _finish(record, SendRecordStatus.SENT, message_id=message_id)
    _record_sent(current.pk, ApplicationChannel.EMAIL,
                 f"Email sent to {preview.recipient} from {account.from_email} with {preview.attachment.name}.")
    return record


def confirm_whatsapp_sent(application, *, version, confirm):
    """The user sent the message from WhatsApp themselves: record it and mark the
    application SENT. Nothing is sent by ApplyForge."""
    open_record = application.send_records.filter(channel=ApplicationChannel.WHATSAPP,
                                                   status=R.COMPOSE_OPENED).first()
    if open_record is not None:  # opened through the hand-off: confirm that record
        return confirm_handoff(application, record_id=open_record.pk, confirm=confirm)
    preview = build_preview(application, ApplicationChannel.WHATSAPP)
    _check_request(application, ApplicationChannel.WHATSAPP, version, confirm, preview)
    current, record = _claim(application, preview)
    _finish(record, SendRecordStatus.HANDED_OFF)
    _record_sent(current.pk, ApplicationChannel.WHATSAPP,
                 f"Sent from WhatsApp to {preview.recipient} (confirmed by you).")
    return record


# ---------------------------------------------------------------------------
# Hand-off: the user sends from their own email app or WhatsApp
# ---------------------------------------------------------------------------

def _message_id(account):
    return make_msgid(domain=account.from_email.rsplit("@", 1)[-1])


def _email_message(*, to, sender, reply_to, subject, body, resume, attachment, message_id, headers=None):
    email = EmailMessage(subject=subject, body=body, from_email=sender, to=[to], reply_to=[reply_to],
                         headers={"Message-ID": message_id, **(headers or {})})
    with resume.file_path.open("rb") as handle:
        email.attach(attachment, handle.read(), _content_type(attachment))
    return email


def _draft_bytes(record, account, *, unsent_header=False):
    """The record's email as RFC 822 bytes (resume attached). ``X-Unsent: 1`` makes
    desktop email apps open the file as a new message ready to send."""
    if record.resume is None or not record.resume.file_path:
        raise SendingError("resume", "The resume of this draft is no longer available.", 409)
    email = _email_message(
        to=record.recipient, sender=record.sender, reply_to=account.from_email, subject=record.subject,
        body=record.body, resume=record.resume, attachment=record.attachment_name,
        message_id=record.message_id, headers={"X-Unsent": "1"} if unsent_header else None,
    )
    return email.message().as_bytes(linesep="\r\n")


def _handoff_failed(current, record, error, channel):
    _attempt_failed(current, record, error, f"Could not prepare the {CHANNEL_NAME[channel]} for you to send: {error}")


def _attempt_failed(current, record, error, note):
    """Record a failed attempt: SEND_FAILED (retry allowed), or only a note when the
    application was already sent on the other channel."""
    _finish(record, R.FAILED, error=error)
    with transaction.atomic():
        locked = Application.objects.select_for_update().get(pk=current.pk)
        locked.last_error = error
        if locked.status in POST_SEND:
            locked.save(update_fields=["last_error", "updated_at"])
            add_note(locked, note)
        elif not change_status(locked, S.SEND_FAILED, note=note):
            locked.save()


def open_compose(application, *, channel, method, version=None):
    """Prepare the approved message for the user to send themselves (see module doc).
    Sends nothing. Returns the SendRecord: COMPOSE_OPENED, or FAILED when the draft could
    not be saved (recorded; the application is SEND_FAILED and can be retried).
    Raises SendingError when refused (nothing changed)."""
    if method not in HANDOFF_METHODS.get(channel, ()):
        raise SendingError("method", "Choose how to open this message.")
    preview = build_preview(application, channel, handoff=True)
    if version:
        _check_preview(version, preview)
    elif not preview.ready:
        raise SendingError("non_field_errors", " ".join(preview.problems))
    account = None
    if channel == ApplicationChannel.EMAIL:
        account = SendingAccount.objects.get(owner=application.profile.owner)
    current, record = _claim(application, preview, provider=PROVIDER_FOR_METHOD[method], handoff=True)

    details, open_url, where = {"method": method}, "", ""
    if channel == ApplicationChannel.EMAIL:
        record.message_id = _message_id(account)
        if method == "mailbox":
            try:
                mailbox = mailbox_for(account)
                with mailbox:
                    folder = mailbox.save_draft(_draft_bytes(record, account))
            except MailboxError as exc:
                _handoff_failed(current, record, exc.message, channel)
                return record
            except Exception as exc:  # unexpected: recorded, never shown raw
                logger.warning("Saving a draft for application %s failed: %s", current.pk, type(exc).__name__)
                _handoff_failed(current, record, "Could not save the draft in your mailbox.", channel)
                return record
            details.update(folder=folder, mailbox="local" if local_mode() else "imap")
            open_url = "" if local_mode() else drafts_page_url(account, getattr(mailbox, "draft_gmail_id", ""))
            where = f"saved in the {folder} folder of {mailbox.description}"
        else:
            where = "downloaded as a draft file for your email app"
    else:
        open_url = preview.whatsapp_link
        where = "opened in WhatsApp"

    record.status = R.COMPOSE_OPENED
    record.open_url = open_url
    record.details = details
    record.save(update_fields=["status", "open_url", "details", "message_id"])
    note = (f"{'Email' if channel == ApplicationChannel.EMAIL else 'WhatsApp message'} to {preview.recipient} "
            f"{where}, for you to check and send. Not sent yet.")
    with transaction.atomic():
        locked = Application.objects.select_for_update().get(pk=current.pk)
        locked.last_error = None
        if locked.status in POST_SEND:
            locked.save(update_fields=["last_error", "updated_at"])
            add_note(locked, note)
        else:
            change_status(locked, S.COMPOSE_OPENED, note=note)
    return record


def _open_record(application, record_id):
    """The application's hand-off record ``record_id``, locked, with the application locked."""
    current = Application.objects.select_for_update().get(pk=application.pk)
    record = SendRecord.objects.select_for_update().filter(application=current, pk=record_id).first()
    if record is None:
        raise SendingError("record", "This hand-off does not belong to this application.", 404)
    if record.status in SENT_RECORD_STATUSES:
        raise SendingError("record", "This message is already recorded as sent.", 409)
    if record.status != R.COMPOSE_OPENED:
        raise SendingError("record", "This message is no longer open. Open it again to send it.", 409)
    return current, record


def confirm_handoff(application, *, record_id, confirm):
    """The user sent the opened message themselves: CONFIRMED, application SENT."""
    if confirm is not True:
        raise SendingError("confirm", "Confirm that you sent the message.")
    with transaction.atomic():
        current, record = _open_record(application, record_id)
        record.status = R.CONFIRMED
        record.finished_at = timezone.now()
        record.save(update_fields=["status", "finished_at"])
        how = "from your email app" if record.channel == ApplicationChannel.EMAIL else "from WhatsApp"
        _record_sent(current.pk, record.channel, f"{CHANNEL_NAME[record.channel].capitalize()} to "
                                                 f"{record.recipient} sent {how} (confirmed by you).")
    return record


def verify_handoff(application, *, record_id):
    """Look for the drafted email in the mailbox's Sent folder. Found: VERIFIED and the
    application SENT. Returns ``(record, found)``; the mailbox is only read."""
    with transaction.atomic():
        _, record = _open_record(application, record_id)
    if record.provider != "mailbox_draft":
        raise SendingError("record", "Only a draft saved in your mailbox can be checked. Confirm it yourself.")
    account = SendingAccount.objects.filter(owner=application.profile.owner).first()
    if account is None:
        raise SendingError("account", "Connect your email account to check your Sent folder.")
    try:
        with mailbox_for(account) as mailbox:
            found = mailbox.find_sent(recipient=record.recipient, since=record.created_at,
                                      message_id=record.message_id)
    except MailboxError as exc:
        raise SendingError("detail", exc.message, 502) from exc
    except Exception as exc:
        logger.warning("Checking the Sent folder for application %s failed: %s", application.pk,
                       type(exc).__name__)
        raise SendingError("detail", "Could not check your Sent folder.", 502) from exc
    if not found:
        return record, False
    with transaction.atomic():
        current, record = _open_record(application, record_id)
        record.status = R.VERIFIED
        record.finished_at = timezone.now()
        record.details = {**record.details, "evidence": found}
        record.save(update_fields=["status", "finished_at", "details"])
        _record_sent(current.pk, record.channel,
                     f"Email to {record.recipient} found in your Sent folder "
                     f"(“{found.get('subject', '')}”, {found.get('date', '')}).")
    return record, True


def abandon_handoff(application, *, record_id):
    """The user did not send the opened message: ABANDONED; the application goes back to
    approved so it can be opened (or sent) again."""
    with transaction.atomic():
        current, record = _open_record(application, record_id)
        record.status = R.ABANDONED
        record.finished_at = timezone.now()
        record.save(update_fields=["status", "finished_at"])
        note = f"Not sent: the {CHANNEL_NAME[record.channel]} to {record.recipient} was not sent (you said so)."
        if current.status == S.COMPOSE_OPENED:
            change_status(current, S.READY_TO_SEND, note=note)
        else:
            add_note(current, note)
    return record


def draft_file(application, record_id):
    """``(filename, bytes)`` of an open ``eml`` hand-off, for the user's email app."""
    record = application.send_records.filter(pk=record_id, provider="eml_file").first()
    if record is None:
        raise SendingError("record", "No draft file for this application.", 404)
    if record.status != R.COMPOSE_OPENED:
        raise SendingError("record", "This draft is no longer open. Open it again to send it.", 409)
    account = SendingAccount.objects.filter(owner=application.profile.owner).first()
    if account is None:
        raise SendingError("account", "Connect your email account first.", 409)
    words = re.findall(r"[A-Za-z0-9]+", application.job.title or "")[:6]
    return f"Application_{'_'.join(words) or application.pk}.eml", _draft_bytes(record, account, unsent_header=True)


def check_mailbox(account):
    """For the account test: can drafts be saved / Sent be read? ``{"ok", "folders", "error"}``."""
    try:
        with mailbox_for(account) as mailbox:
            return {"ok": True, "folders": mailbox.check(), "error": ""}
    except MailboxError as exc:
        return {"ok": False, "folders": {}, "error": exc.message}
    except Exception as exc:
        logger.warning("Mailbox check failed: %s", type(exc).__name__)
        return {"ok": False, "folders": {}, "error": "Could not open your mailbox (IMAP)."}


def _content_type(name):
    lower = name.lower()
    if lower.endswith(".pdf"):
        return "application/pdf"
    if lower.endswith(".docx"):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return "application/msword"


def send_test_email(account):
    """Send a short test email to the account's own address; records the result on the account."""
    connection = _connection(account)
    email = EmailMessage(
        subject="ApplyForge test email",
        body="This test confirms that ApplyForge can send application emails from this mailbox.\n"
             "No application was sent.",
        from_email=formataddr((account.from_name, account.from_email)) if account.from_name else account.from_email,
        to=[account.from_email], connection=connection,
    )
    try:
        email.send(fail_silently=False)
    except Exception as exc:
        account.last_error = _describe_smtp_error(exc)
        account.verified_at = None
        account.save(update_fields=["last_error", "verified_at", "updated_at"])
        return False
    account.last_error = ""
    account.verified_at = timezone.now()
    account.save(update_fields=["last_error", "verified_at", "updated_at"])
    return True


def email_sending_mode():
    """What "send" means on this server, for the UI: 'smtp' or a local-only backend."""
    backend = settings.EMAIL_BACKEND.rsplit(".", 2)[-2] if "." in settings.EMAIL_BACKEND else settings.EMAIL_BACKEND
    return "smtp" if backend == "smtp" else backend
