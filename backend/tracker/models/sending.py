"""
Sending applications (Job Automation, phase 5).

* ``SendingAccount`` - one per user: the SMTP mailbox that user's application
  emails are sent from. The password (normally an app password) is stored
  encrypted (``tracker.credentials``) and is never returned by the API.
* ``SendRecord`` - one row per send attempt (email) or WhatsApp hand-off, with
  exactly what was sent, to whom, and the result. Nothing is ever sent without
  the user's explicit confirmation (``tracker.sending``).
"""

from django.conf import settings
from django.db import models


class SmtpSecurity(models.TextChoices):
    STARTTLS = "starttls", "STARTTLS (port 587)"
    SSL = "ssl", "SSL/TLS (port 465)"


class SendingAccount(models.Model):
    owner = models.OneToOneField(
        settings.AUTH_USER_MODEL, related_name="sending_account", on_delete=models.CASCADE
    )
    from_name = models.CharField(max_length=255, blank=True, default="")
    from_email = models.EmailField(max_length=254)
    smtp_host = models.CharField(max_length=255)
    smtp_port = models.PositiveIntegerField(default=587)
    security = models.CharField(max_length=10, choices=SmtpSecurity.choices, default=SmtpSecurity.STARTTLS)
    username = models.CharField(max_length=254)
    # Drafts (email hand-off) are saved to this mailbox over IMAP with the same login.
    # Empty = derived from the SMTP server (smtp.gmail.com -> imap.gmail.com).
    imap_host = models.CharField(max_length=255, blank=True, default="")
    imap_port = models.PositiveIntegerField(default=993)
    password_encrypted = models.TextField(blank=True, default="")
    # Set when a test email to the user's own address succeeded with these settings.
    verified_at = models.DateTimeField(blank=True, null=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Sending account {self.from_email}"


class SendRecordStatus(models.TextChoices):
    SENDING = "SENDING", "Sending"
    SENT = "SENT", "Sent"
    FAILED = "FAILED", "Failed"
    # WhatsApp: the message was opened in WhatsApp for the user to send there, and the
    # user then confirmed that they sent it.
    HANDED_OFF = "HANDED_OFF", "Sent from WhatsApp"
    # Hand-off: the message was opened for the user to send in their own email app or
    # WhatsApp. It ends as CONFIRMED (the user said they sent it), VERIFIED (found in the
    # mailbox's Sent folder) or ABANDONED (the user did not send it).
    COMPOSE_OPENED = "COMPOSE_OPENED", "Opened for you to send"
    CONFIRMED = "CONFIRMED", "Sent (confirmed by you)"
    VERIFIED = "VERIFIED", "Sent (found in your Sent folder)"
    ABANDONED = "ABANDONED", "Not sent"


# A channel counts as sent once one of its records reaches one of these.
SENT_RECORD_STATUSES = (SendRecordStatus.SENT, SendRecordStatus.HANDED_OFF, SendRecordStatus.CONFIRMED,
                        SendRecordStatus.VERIFIED)


class SendRecord(models.Model):
    application = models.ForeignKey(
        "tracker.Application", related_name="send_records", on_delete=models.CASCADE
    )
    channel = models.CharField(max_length=20)
    # "smtp" (sent by ApplyForge), "mailbox_draft" (saved to the user's Drafts folder),
    # "eml_file" (downloaded draft for a desktop email app), "whatsapp_link" (wa.me).
    provider = models.CharField(max_length=30)
    status = models.CharField(max_length=20, choices=SendRecordStatus.choices, db_index=True)
    recipient = models.CharField(max_length=254)
    sender = models.CharField(max_length=254, blank=True, default="")
    subject = models.CharField(max_length=500, blank=True, default="")
    body = models.TextField(blank=True, default="")
    resume = models.ForeignKey(
        "tracker.Resume", related_name="send_records", on_delete=models.SET_NULL, null=True, blank=True
    )
    attachment_name = models.CharField(max_length=255, blank=True, default="")
    attachment_size = models.PositiveIntegerField(null=True, blank=True)
    message_id = models.CharField(max_length=255, blank=True, default="")
    error = models.TextField(blank=True, default="")
    # Hand-off: the link opened for the user (webmail Drafts, wa.me) and where the draft
    # was saved / how it was verified ({"folder": ..., "evidence": ...}).
    open_url = models.TextField(blank=True, default="")
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            # One open hand-off per application, and a channel is sent at most once.
            models.UniqueConstraint(
                fields=["application"], condition=models.Q(status="COMPOSE_OPENED"),
                name="sendrecord_one_open_handoff",
            ),
            models.UniqueConstraint(
                fields=["application", "channel"],
                condition=models.Q(status__in=["SENT", "HANDED_OFF", "CONFIRMED", "VERIFIED"]),
                name="sendrecord_sent_once_per_channel",
            ),
        ]

    def __str__(self):
        return f"{self.channel} to {self.recipient} ({self.status})"
