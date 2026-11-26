"""
Sending API (Job Automation, phase 5). Always limited to the signed-in user.

* ``GET/PUT/DELETE sending/email-account/``   the user's own SMTP mailbox. The password
                                              is write-only (never returned).
* ``POST sending/email-account/test/``        send a test email to the user's own address.
* ``GET  applications/{id}/send-preview/?channel=EMAIL|WHATSAPP``
                                              exactly what would be sent, with its ``version``.
* ``POST applications/{id}/send/``            ``{"channel": "EMAIL", "version", "confirm": true}``
* ``POST applications/{id}/whatsapp-sent/``   ``{"version", "confirm": true}``: the user sent it from WhatsApp.
* ``GET  applications/{id}/send-records/``    every attempt, newest first.

Hand-off (the user sends from their own email app or WhatsApp; ``tracker.sending``):

* ``POST applications/{id}/compose/``          ``{"channel", "method", "version"}``: open it (never sends)
* ``POST applications/{id}/compose/confirm/``  ``{"record", "confirm": true}``: the user sent it
* ``POST applications/{id}/compose/verify/``   ``{"record"}``: look for it in the Sent folder
* ``POST applications/{id}/compose/abandon/``  ``{"record"}``: not sent; can be opened again
* ``GET  applications/{id}/compose/draft/?record=``  the .eml draft (resume attached)

The application actions live on ``ApplicationViewSet`` (views.py) and call into
``tracker.sending``, which enforces approval, confirmation and the preview version.
"""

import re

from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from tracker.credentials import CredentialError, decrypt, encrypt
from tracker.mailbox import app_password, drafts_page_url, imap_host_for, local_mode
from tracker.models import SendingAccount, SendRecord, SmtpSecurity
from tracker.sending import SendingError, check_mailbox, email_sending_mode, send_test_email

from .ownership import OwnedModelSerializer, OwnedSerializer


def is_gmail(host):
    return (host or "").strip().lower().endswith(("gmail.com", "googlemail.com"))


def looks_like_app_password(password):
    """A Google app password: exactly 16 letters (shown as four groups of four)."""
    return bool(re.fullmatch(r"[a-z]{16}", app_password(password or "").strip().lower()))


def drafts_mode():
    """Where "Send email" saves drafts: "imap" (the user's own mailbox) or "local" (a test folder)."""
    return "local" if local_mode() else "imap"


class SendingAccountSerializer(OwnedModelSerializer):
    password = serializers.CharField(write_only=True, required=False, allow_blank=False, max_length=500,
                                     trim_whitespace=False, style={"input_type": "password"})
    has_password = serializers.SerializerMethodField()
    smtp_host = serializers.CharField(max_length=255)
    smtp_port = serializers.IntegerField(min_value=1, max_value=65535)
    security = serializers.ChoiceField(choices=SmtpSecurity.choices)
    username = serializers.CharField(max_length=254)
    imap_host = serializers.CharField(max_length=255, required=False, allow_blank=True)
    imap_port = serializers.IntegerField(min_value=1, max_value=65535, required=False)
    # The IMAP server actually used for drafts (entered, or derived from the SMTP server).
    drafts_server = serializers.SerializerMethodField()
    # The provider's web page listing drafts ("" = unknown provider, or the local test mailbox).
    drafts_page = serializers.SerializerMethodField()
    # Gmail only: False when the saved password cannot be a Google app password (16 letters),
    # so the login will be refused. None for other providers.
    app_password_ok = serializers.SerializerMethodField()

    class Meta:
        model = SendingAccount
        fields = ["from_name", "from_email", "smtp_host", "smtp_port", "security", "username", "password",
                  "imap_host", "imap_port", "drafts_server", "drafts_page", "app_password_ok", "has_password", "verified_at", "last_error",
                  "updated_at"]
        read_only_fields = ["has_password", "drafts_server", "drafts_page", "app_password_ok", "verified_at", "last_error", "updated_at"]

    def get_has_password(self, account):
        return bool(account.password_encrypted)

    def get_drafts_server(self, account):
        return imap_host_for(account)

    def get_app_password_ok(self, account):
        if not is_gmail(account.smtp_host) or not account.password_encrypted:
            return None
        try:
            password = decrypt(account.password_encrypted)
        except CredentialError:
            return False
        return looks_like_app_password(password)

    def get_drafts_page(self, account):
        return "" if local_mode() else drafts_page_url(account)

    def validate_smtp_host(self, value):
        value = value.strip()
        if not value or any(c in value for c in " /\\:@"):
            raise serializers.ValidationError("Enter the server name only, e.g. smtp.gmail.com.")
        return value

    def validate_imap_host(self, value):
        value = value.strip()
        if any(c in value for c in " /\\:@"):
            raise serializers.ValidationError("Enter the server name only, e.g. imap.gmail.com.")
        return value


class SendRequestSerializer(OwnedSerializer):
    channel = serializers.ChoiceField(choices=["EMAIL"], required=False, default="EMAIL")
    version = serializers.CharField(max_length=64)
    confirm = serializers.BooleanField()


class ComposeRequestSerializer(OwnedSerializer):
    channel = serializers.ChoiceField(choices=["EMAIL", "WHATSAPP"])
    method = serializers.ChoiceField(choices=["mailbox", "eml", "link"])
    # Optional: when given (the preview the user saw), any change since is refused.
    version = serializers.CharField(max_length=64, required=False, allow_blank=True)


class HandoffRecordSerializer(OwnedSerializer):
    record = serializers.IntegerField(min_value=1)


class HandoffConfirmSerializer(HandoffRecordSerializer):
    confirm = serializers.BooleanField()


class WhatsAppSentSerializer(OwnedSerializer):
    version = serializers.CharField(max_length=64)
    confirm = serializers.BooleanField()


class SendRecordSerializer(OwnedModelSerializer):
    class Meta:
        model = SendRecord
        fields = ["id", "channel", "provider", "status", "recipient", "sender", "subject", "body", "resume",
                  "attachment_name", "attachment_size", "message_id", "error", "open_url", "details",
                  "created_at", "finished_at"]
        read_only_fields = fields


def sending_error_response(exc):
    return Response({exc.field: [exc.message]}, status=exc.http_status)


class EmailAccountView(APIView):
    def get(self, request):
        account = SendingAccount.objects.filter(owner=request.user).first()
        data = SendingAccountSerializer(account).data if account else None
        return Response({"account": data, "mode": email_sending_mode(), "drafts_mode": drafts_mode()})

    def put(self, request):
        account = SendingAccount.objects.filter(owner=request.user).first()
        serializer = SendingAccountSerializer(account, data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        password = data.pop("password", None)
        if account is None and not password:
            return Response({"password": ["Enter the password (an app password for Gmail/Outlook)."]},
                            status=status.HTTP_400_BAD_REQUEST)
        host = str(data.get("smtp_host") or (account.smtp_host if account else ""))
        if password is not None and is_gmail(host) and not looks_like_app_password(password):
            # Gmail refuses normal passwords for app logins; an app password is always 16 letters.
            return Response({"password": [
                "This is not a Google app password (those are exactly 16 letters, e.g. “abcd efgh ijkl mnop”). "
                "Your normal Google password does not work here. Create an app password at "
                "myaccount.google.com/apppasswords (2-Step Verification must be on) and paste it here."]},
                status=status.HTTP_400_BAD_REQUEST)
        changed = account is None or password is not None or any(
            getattr(account, k) != v for k, v in data.items() if k != "from_name")
        account = account or SendingAccount(owner=request.user)
        for key, value in data.items():
            setattr(account, key, value)
        if password is not None:
            account.password_encrypted = encrypt(password)
        if changed:
            account.verified_at = None  # settings changed: test again
            account.last_error = ""
        account.save()
        return Response({"account": SendingAccountSerializer(account).data, "mode": email_sending_mode(), "drafts_mode": drafts_mode()})

    def delete(self, request):
        SendingAccount.objects.filter(owner=request.user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class EmailAccountTestView(APIView):
    throttle_scope = "application-send"

    def post(self, request):
        account = SendingAccount.objects.filter(owner=request.user).first()
        if account is None:
            return Response({"detail": "Save your email account first."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            ok = send_test_email(account)
        except SendingError as exc:
            return sending_error_response(exc)
        account.refresh_from_db()
        body = {"ok": ok, "account": SendingAccountSerializer(account).data, "mode": email_sending_mode(), "drafts_mode": drafts_mode()}
        if ok:
            # Drafts (hand-off) use the same login over IMAP: check that too. Read only.
            body["drafts"] = check_mailbox(account)
        return Response(body, status=status.HTTP_200_OK if ok else status.HTTP_502_BAD_GATEWAY)
