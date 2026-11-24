"""Hand-off sending: the user sends from their own email app or WhatsApp.

Fictional people and addresses only. The "mailbox" is a temporary local folder or a
fake IMAP server (``FakeImap``); nothing touches a network and nothing is ever sent.
"""

import email
import imaplib
import shutil
import tempfile
from datetime import timedelta
from email.utils import format_datetime
from pathlib import Path
from unittest import mock

from django.core import mail
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone

from tracker.credentials import decrypt
from tracker.models import Application, ApplicationEvent, JobStatus, SendingAccount, SendRecord
from tracker.mailbox import _parse_list, drafts_page_url, imap_host_for

from .helpers import make_user
from .test_job_automation import SendingTestCase


class FakeImap:
    """Records what ApplyForge does to the mailbox. ``sent`` = header blocks in Sent."""

    instances = []
    sent = []
    fail_login = False
    login_error = "[AUTHENTICATIONFAILED] Invalid credentials (Failure)"

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.appended, self.searches, self.selected, self.user, self.password = [], [], None, None, None
        self.readonly = None
        self.uid_fetches = []
        FakeImap.instances.append(self)

    def login(self, user, password):
        if FakeImap.fail_login:
            raise imaplib.IMAP4.error(FakeImap.login_error)
        self.user, self.password = user, password
        return "OK", [b"logged in"]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Drafts) "/" "[Gmail]/Drafts"',
                      b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"']

    def append(self, mailbox, flags, date_time, message):
        self.appended.append((mailbox, flags, message))
        return "OK", [b"[APPENDUID 7 4242] (Success)"]

    def select(self, mailbox, readonly=False):
        self.selected, self.readonly = mailbox, readonly
        return "OK", [str(len(FakeImap.sent)).encode()]

    def search(self, charset, *criteria):
        self.searches.append(criteria)
        if "Message-ID" in criteria:
            wanted = criteria[-1].strip('"')
            ids = [str(i + 1) for i, h in enumerate(FakeImap.sent) if wanted.encode() in h]
        else:
            ids = [str(i + 1) for i in range(len(FakeImap.sent))]
        return "OK", [" ".join(ids).encode()]

    def fetch(self, number, spec):
        headers = FakeImap.sent[int(number) - 1]
        return "OK", [(f"{int(number)} (BODY[HEADER.FIELDS] {{{len(headers)}}}".encode(), headers), b")"]

    capabilities = ("IMAP4REV1", "UIDPLUS", "X-GM-EXT-1")

    def uid(self, command, uid, spec):
        self.uid_fetches.append((command, uid, spec))
        return "OK", [f"1 (X-GM-MSGID 1795513326041234567 UID {uid})".encode()]

    def logout(self):
        return "BYE", [b""]


class HandoffTestCase(SendingTestCase):
    drafts_backend = "local"

    def setUp(self):
        super().setUp()
        self.mailroot = tempfile.mkdtemp(prefix="af-mailbox-")
        self.addCleanup(shutil.rmtree, self.mailroot, ignore_errors=True)
        override = override_settings(EMAIL_DRAFTS_BACKEND=self.drafts_backend,
                                     EMAIL_DRAFTS_LOCAL_PATH=self.mailroot)
        override.enable()
        self.addCleanup(override.disable)
        self.connect_email()
        self.base = f"/api/v1/applications/{self.app.pk}"

    def compose(self, channel="EMAIL", method="mailbox", version=None):
        version = version or self.preview(channel)["version"]
        return self.client.post(f"{self.base}/compose/", {"channel": channel, "method": method, "version": version},
                                format="json")

    def confirm(self, record_id, confirm=True):
        return self.client.post(f"{self.base}/compose/confirm/", {"record": record_id, "confirm": confirm},
                                format="json")

    def verify(self, record_id):
        return self.client.post(f"{self.base}/compose/verify/", {"record": record_id}, format="json")

    def abandon(self, record_id):
        return self.client.post(f"{self.base}/compose/abandon/", {"record": record_id}, format="json")

    def drafts(self):
        return sorted((Path(self.mailroot) / str(self.user.pk) / "Drafts").glob("*.eml"))

    def events(self):
        return list(ApplicationEvent.objects.filter(application=self.app).order_by("timestamp", "id")
                    .values_list("status", "kind"))


class EmailDraftTests(HandoffTestCase):
    def test_draft_saved_with_recipient_subject_body_and_resume_without_sending(self):
        response = self.compose()
        self.assertEqual(response.status_code, 200, response.data)
        record = response.data["record"]
        self.assertEqual((record["status"], record["provider"], record["channel"]),
                         ("COMPOSE_OPENED", "mailbox_draft", "EMAIL"))
        self.assertEqual(record["details"]["folder"], "Drafts")
        self.assertEqual(record["open_url"], "")  # local test mailbox: no web page to open
        # The draft in the mailbox: exactly the previewed message, resume attached.
        [draft_path] = self.drafts()
        draft = email.message_from_bytes(draft_path.read_bytes())
        self.assertEqual(draft["To"], "riya@fictional-labs.example")
        self.assertEqual(draft["Subject"], "Python Backend Developer application")
        self.assertEqual(draft["From"], "Maya Example <maya@example.org>")
        self.assertEqual(draft["Message-ID"], record["message_id"])
        parts = list(draft.walk())
        body = next(p for p in parts if p.get_content_type() == "text/plain" and not p.get_filename())
        self.assertIn("I am applying for the Python Backend Developer role.", body.get_payload(decode=True).decode())
        attachment = next(p for p in parts if p.get_filename())
        self.assertEqual(attachment.get_filename(), "Maya_Example_Resume.pdf")
        self.assertEqual(attachment.get_content_type(), "application/pdf")
        self.assertEqual(attachment.get_payload(decode=True)[:4], b"%PDF")
        # Opened is not sent.
        self.app.refresh_from_db()
        self.assertEqual(self.app.status, "COMPOSE_OPENED")
        self.assertIsNone(self.app.sent_at)
        self.assertEqual(self.app.job.status, JobStatus.SHORTLISTED)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(response.data["application"]["send_state"]["EMAIL"]["state"], "open")
        self.assertTrue(response.data["application"]["send_state"]["EMAIL"]["can_verify"])
        self.assertTrue(ApplicationEvent.objects.filter(application=self.app, status="COMPOSE_OPENED",
                                                        notes__contains="Not sent yet").exists())

    def test_explicit_confirmation_marks_sent_once(self):
        record_id = self.compose().data["record"]["id"]
        self.assertEqual(self.confirm(record_id, confirm=False).status_code, 400)
        self.app.refresh_from_db()
        self.assertEqual(self.app.status, "COMPOSE_OPENED")

        response = self.confirm(record_id)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["record"]["status"], "CONFIRMED")
        self.assertIsNotNone(response.data["record"]["finished_at"])
        self.app.refresh_from_db()
        self.assertEqual((self.app.status, self.app.channel), ("SENT", "EMAIL"))
        self.assertIsNotNone(self.app.sent_at)
        self.assertIsNotNone(self.app.follow_up_on)
        self.assertEqual(self.app.job.status, JobStatus.APPLIED)
        state = response.data["application"]["send_state"]["EMAIL"]
        self.assertEqual((state["state"], state["status"]), ("sent", "CONFIRMED"))
        sent_at = self.app.sent_at

        # A second (double-click, retried request) confirmation changes nothing.
        again = self.confirm(record_id)
        self.assertEqual(again.status_code, 409)
        self.app.refresh_from_db()
        self.assertEqual(self.app.sent_at, sent_at)
        self.assertEqual(ApplicationEvent.objects.filter(application=self.app, status="SENT").count(), 1)
        self.assertEqual(len(mail.outbox), 0)
        # History kept in order: approved -> opened -> sent.
        self.assertEqual([s for s, _ in self.events()][-2:], ["COMPOSE_OPENED", "SENT"])

    def test_verification_finds_the_email_in_the_sent_folder(self):
        record_id = self.compose().data["record"]["id"]
        response = self.verify(record_id)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["verified"])
        self.assertIn("No email to riya@fictional-labs.example", response.data["detail"])
        self.app.refresh_from_db()
        self.assertEqual(self.app.status, "COMPOSE_OPENED")  # not found: nothing changes

        # The user sends the draft from their email app (here: the local mailbox).
        [draft] = self.drafts()
        sent_dir = Path(self.mailroot) / str(self.user.pk) / "Sent"
        message = email.message_from_bytes(draft.read_bytes())
        message.replace_header("Message-ID", "<rewritten-by-provider@example.org>")  # providers may do this
        message["Date"] = format_datetime(timezone.now())
        (sent_dir / draft.name).write_bytes(message.as_bytes())
        draft.unlink()

        response = self.verify(record_id)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["verified"])
        self.assertEqual(response.data["record"]["status"], "VERIFIED")
        self.assertEqual(response.data["record"]["details"]["evidence"]["match"], "recipient-and-time")
        self.app.refresh_from_db()
        self.assertEqual(self.app.status, "SENT")
        self.assertTrue(ApplicationEvent.objects.filter(application=self.app, status="SENT",
                                                        notes__contains="found in your Sent folder").exists())
        self.assertEqual(self.verify(record_id).status_code, 409)  # already recorded

    def test_an_older_email_to_the_same_recruiter_is_not_taken_as_proof(self):
        record_id = self.compose().data["record"]["id"]
        sent_dir = Path(self.mailroot) / str(self.user.pk) / "Sent"
        old = email.message.EmailMessage()
        old["To"] = "riya@fictional-labs.example"
        old["Subject"] = "An earlier email"
        old["Date"] = format_datetime(timezone.now() - timedelta(days=3))
        (sent_dir / "old.eml").write_bytes(old.as_bytes())
        self.assertFalse(self.verify(record_id).data["verified"])

    def test_abandoned_draft_can_be_opened_again(self):
        first = self.compose().data["record"]["id"]
        self.assertIn(self.compose().status_code, (400, 409))  # one open hand-off at a time
        response = self.abandon(first)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["record"]["status"], "ABANDONED")
        self.app.refresh_from_db()
        self.assertEqual(self.app.status, "READY_TO_SEND")
        self.assertIsNone(self.app.sent_at)
        self.assertEqual(self.abandon(first).status_code, 409)
        self.assertEqual(self.confirm(first).status_code, 409)  # an abandoned draft cannot be confirmed

        second = self.compose()
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(list(SendRecord.objects.order_by("id").values_list("status", flat=True)),
                         ["ABANDONED", "COMPOSE_OPENED"])
        self.assertEqual([s for s, _ in self.events()][-3:], ["COMPOSE_OPENED", "READY_TO_SEND", "COMPOSE_OPENED"])

    def test_open_draft_blocks_direct_sending_and_regeneration(self):
        self.compose()
        self.assertIn(self.send().status_code, (400, 409))
        self.assertEqual(len(mail.outbox), 0)
        self.assertTrue(any("Confirm that you sent it" in p for p in self.preview()["problems"]))
        response = self.client.post(f"{self.base}/generate/", {}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_requires_drafts_and_refuses_a_changed_preview(self):
        version = self.preview()["version"]
        Application.objects.filter(pk=self.app.pk).update(status="SHORTLISTED")  # nothing generated yet
        self.assertEqual(self.compose(version=version).status_code, 400)
        Application.objects.filter(pk=self.app.pk).update(status="READY_TO_SEND")
        self.client.patch(f"{self.base}/", {"generated_subject": "Changed after review"}, format="json")
        self.assertEqual(self.compose(version=version).status_code, 409)
        self.assertEqual(self.compose(method="link").status_code, 400)  # not an email method
        self.assertFalse(SendRecord.objects.exists())
        self.assertEqual(self.drafts(), [])

    def test_one_click_from_a_generated_draft_without_approval_step(self):
        # The click on "Send email" is the approval: nothing is sent until the user presses
        # Send in their own email app. No preview version is needed.
        Application.objects.filter(pk=self.app.pk).update(status="GENERATED")
        response = self.client.post(f"{self.base}/compose/", {"channel": "EMAIL", "method": "mailbox"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.app.refresh_from_db()
        self.assertEqual(self.app.status, "COMPOSE_OPENED")
        self.assertEqual(len(self.drafts()), 1)
        self.assertEqual(len(mail.outbox), 0)
        # Direct sending from ApplyForge still needs the approval and the confirmation.
        Application.objects.filter(pk=self.app.pk).update(status="GENERATED")
        SendRecord.objects.update(status="ABANDONED")
        self.assertIn("Approve the drafts first.", self.preview()["problems"])
        self.assertEqual(self.send().status_code, 400)
        self.assertEqual(len(mail.outbox), 0)

    def test_list_carries_the_recipient_for_the_cards(self):
        [row] = self.client.get("/api/v1/applications/").data
        self.assertEqual((row["recipient_email"], row["recipient_phone"]),
                         ("riya@fictional-labs.example", "+91 98765 43210"))
        self.assertEqual(row["send_state"]["WHATSAPP"]["state"], "none")

    def test_users_cannot_set_compose_opened_and_manual_sent_closes_the_hand_off(self):
        response = self.client.patch(f"{self.base}/", {"status": "COMPOSE_OPENED"}, format="json")
        self.assertEqual(response.status_code, 400)
        record_id = self.compose().data["record"]["id"]
        response = self.client.patch(f"{self.base}/", {"status": "SENT", "note": "Sent it."}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(SendRecord.objects.get(pk=record_id).status, "CONFIRMED")

    def test_database_allows_one_open_hand_off_and_one_sent_record_per_channel(self):
        common = {"application": self.app, "provider": "mailbox_draft", "recipient": "riya@fictional-labs.example"}
        SendRecord.objects.create(channel="EMAIL", status="COMPOSE_OPENED", **common)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SendRecord.objects.create(channel="WHATSAPP", status="COMPOSE_OPENED", **common)
        SendRecord.objects.create(channel="EMAIL", status="CONFIRMED", **common)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SendRecord.objects.create(channel="EMAIL", status="VERIFIED", **common)


class EmlDraftTests(HandoffTestCase):
    def test_draft_file_for_desktop_email_apps(self):
        response = self.compose(method="eml")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["record"]["provider"], "eml_file")
        url = response.data["download_url"]
        self.assertTrue(url.startswith(f"applications/{self.app.pk}/compose/draft/?record="))
        download = self.client.get(f"/api/v1/{url}")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download["Content-Type"], "message/rfc822")
        self.assertIn("attachment", download["Content-Disposition"])
        self.assertIn(".eml", download["Content-Disposition"])
        self.assertEqual(download["Cache-Control"], "private, no-store")
        message = email.message_from_bytes(download.content)
        self.assertEqual(message["X-Unsent"], "1")  # opens as a new message, ready to send
        self.assertEqual(message["To"], "riya@fictional-labs.example")
        self.assertEqual([p.get_filename() for p in message.walk() if p.get_filename()], ["Maya_Example_Resume.pdf"])
        self.assertEqual(self.drafts(), [])  # nothing saved in the mailbox
        self.app.refresh_from_db()
        self.assertEqual(self.app.status, "COMPOSE_OPENED")
        self.assertFalse(response.data["application"]["send_state"]["EMAIL"]["can_verify"])
        # No Sent-folder check for a downloaded file: the user confirms.
        self.assertEqual(self.verify(response.data["record"]["id"]).status_code, 400)
        self.assertEqual(self.confirm(response.data["record"]["id"]).status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/{url}").status_code, 409)  # closed

    def test_draft_file_needs_the_record_parameter(self):
        self.assertEqual(self.client.get(f"{self.base}/compose/draft/").status_code, 400)
        self.assertEqual(self.client.get(f"{self.base}/compose/draft/?record=999").status_code, 404)


class ImapMailboxTests(HandoffTestCase):
    drafts_backend = "imap"
    def setUp(self):
        super().setUp()
        FakeImap.instances, FakeImap.sent, FakeImap.fail_login = [], [], False
        FakeImap.login_error = "[AUTHENTICATIONFAILED] Invalid credentials (Failure)"
        patcher = mock.patch("tracker.mailbox.imaplib.IMAP4_SSL", FakeImap)
        patcher.start()
        self.addCleanup(patcher.stop)
        SendingAccount.objects.filter(owner=self.user).update(smtp_host="smtp.gmail.com")

    def test_draft_is_appended_to_the_drafts_folder_and_gmail_drafts_open(self):
        response = self.compose()
        self.assertEqual(response.status_code, 200, response.data)
        [imap] = FakeImap.instances
        self.assertEqual((imap.host, imap.port), ("imap.gmail.com", 993))
        self.assertEqual((imap.user, imap.password), ("maya@example.org", "fictional-app-password"))
        [(folder, flags, raw)] = imap.appended
        self.assertEqual(folder, '"[Gmail]/Drafts"')
        self.assertIn("\\Draft", flags)
        message = email.message_from_bytes(raw)
        self.assertEqual(message["To"], "riya@fictional-labs.example")
        self.assertEqual([p.get_filename() for p in message.walk() if p.get_filename()], ["Maya_Example_Resume.pdf"])
        self.assertEqual(response.data["record"]["open_url"],
                         "https://mail.google.com/mail/?authuser=maya@example.org#drafts?compose=18eaf1db642afc87")
        self.assertEqual(imap.uid_fetches, [("FETCH", "4242", "(X-GM-MSGID)")])  # opens that very draft
        self.assertEqual(response.data["record"]["details"]["folder"], "[Gmail]/Drafts")
        self.assertNotIn("fictional-app-password", str(response.data))
        self.assertEqual(len(mail.outbox), 0)

    def test_sent_folder_match_by_message_id(self):
        record = self.compose().data["record"]
        FakeImap.sent = [
            b"To: someone@elsewhere.example\r\nSubject: Other\r\nMessage-ID: <other@x>\r\n"
            b"Date: " + format_datetime(timezone.now()).encode() + b"\r\n\r\n",
            f"To: Riya <riya@fictional-labs.example>\r\nSubject: Python Backend Developer application\r\n"
            f"Message-ID: {record['message_id']}\r\nDate: {format_datetime(timezone.now())}\r\n\r\n".encode(),
        ]
        response = self.verify(record["id"])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["verified"])
        self.assertEqual(response.data["record"]["details"]["evidence"]["match"], "message-id")
        imap = FakeImap.instances[-1]
        self.assertEqual(imap.selected, '"[Gmail]/Sent Mail"')
        self.assertTrue(imap.readonly)  # the Sent folder is only read
        self.app.refresh_from_db()
        self.assertEqual(self.app.status, "SENT")

    def test_sent_folder_match_by_recipient_and_time(self):
        record = self.compose().data["record"]
        FakeImap.sent = [f"To: riya@fictional-labs.example\r\nSubject: Edited subject\r\nMessage-ID: <new@gmail>\r\n"
                         f"Date: {format_datetime(timezone.now())}\r\n\r\n".encode()]
        response = self.verify(record["id"])
        self.assertTrue(response.data["verified"], response.data)
        criteria = FakeImap.instances[-1].searches[-1]
        self.assertEqual((criteria[0], criteria[2], criteria[3]), ("SINCE", "TO", '"riya@fictional-labs.example"'))

    def test_login_failure_is_recorded_and_retry_works(self):
        FakeImap.fail_login = True
        response = self.compose()
        self.assertEqual(response.status_code, 502)
        self.assertIn("app password", response.data["detail"])
        self.assertEqual(response.data["record"]["status"], "FAILED")
        self.app.refresh_from_db()
        self.assertEqual(self.app.status, "SEND_FAILED")
        self.assertIn("IMAP", self.app.last_error)
        self.assertEqual(response.data["application"]["send_state"]["EMAIL"]["state"], "failed")
        FakeImap.fail_login = False
        retry = self.compose()
        self.assertEqual(retry.status_code, 200, retry.data)
        self.app.refresh_from_db()
        self.assertEqual((self.app.status, self.app.last_error), ("COMPOSE_OPENED", None))

    def test_app_password_copied_with_spaces_works(self):
        # Google shows "abcd efgh ijkl mnop"; the login needs the 16 letters.
        self.client.put(self.account_url, {"from_email": "maya@example.org", "smtp_host": "smtp.gmail.com",
                                           "smtp_port": 587, "security": "starttls", "username": " maya@example.org ",
                                           "password": "abcd efgh ijkl mnop"}, format="json")
        self.assertEqual(self.compose().status_code, 200)
        self.assertEqual((FakeImap.instances[-1].user, FakeImap.instances[-1].password),
                         ("maya@example.org", "abcdefghijklmnop"))

    def test_gmail_refuses_a_normal_password_when_saving(self):
        body = {"from_email": "maya@example.org", "smtp_host": "smtp.gmail.com", "smtp_port": 587,
                "security": "starttls", "username": "maya@example.org", "password": "Normal#Pw9"}
        response = self.client.put(self.account_url, body, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("16 letters", response.data["password"][0])
        stored = SendingAccount.objects.get(owner=self.user).password_encrypted
        self.assertEqual(decrypt(stored), "fictional-app-password")  # nothing saved
        body["password"] = "ABCD EFGH IJKL MNOP"
        self.assertEqual(self.client.put(self.account_url, body, format="json").status_code, 200)

    def test_login_refusal_says_what_to_do(self):
        FakeImap.fail_login = True
        cases = {
            "[ALERT] Application-specific password required: https://support.google.com/accounts/answer/185833":
                "myaccount.google.com/apppasswords",
            "[ALERT] Please log in via your web browser": "sign in on its website",
            "[AUTHENTICATIONFAILED] Invalid credentials (Failure)": "Invalid credentials",
        }
        for server_text, expected in cases.items():
            with self.subTest(server_text=server_text):
                FakeImap.login_error = server_text
                Application.objects.filter(pk=self.app.pk).update(status="READY_TO_SEND")
                response = self.compose()
                self.assertEqual(response.status_code, 502)
                self.assertIn(expected, response.data["detail"])
                self.assertNotIn("fictional-app-password", response.data["detail"])

    def test_verification_errors_change_nothing(self):
        record_id = self.compose().data["record"]["id"]
        FakeImap.fail_login = True
        response = self.verify(record_id)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(SendRecord.objects.get(pk=record_id).status, "COMPOSE_OPENED")

    def test_account_test_checks_the_drafts_folder(self):
        response = self.client.post(f"{self.account_url}test/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["drafts"],
                         {"ok": True, "folders": {"drafts": "[Gmail]/Drafts", "sent": "[Gmail]/Sent Mail"},
                          "error": ""})
        self.assertEqual(response.data["account"]["drafts_server"], "imap.gmail.com")


class MailboxHelperTests(HandoffTestCase):
    def test_imap_server_and_drafts_page(self):
        account = SendingAccount(from_email="maya@example.org", smtp_host="smtp-mail.outlook.com")
        self.assertEqual(imap_host_for(account), "outlook.office365.com")
        self.assertEqual(drafts_page_url(account), "https://outlook.live.com/mail/0/drafts")
        account.smtp_host = "smtp.fictional-mail.example"
        self.assertEqual(imap_host_for(account), "imap.fictional-mail.example")
        self.assertEqual(drafts_page_url(account), "")  # unknown provider: open Drafts in your app
        account.imap_host = "mail.fictional-mail.example"
        self.assertEqual(imap_host_for(account), "mail.fictional-mail.example")

    def test_list_parsing(self):
        folders = _parse_list([b'(\\HasNoChildren \\Sent) "." "INBOX.Sent \\"Items\\""', b'(\\Noselect) NIL Archive',
                               (b"literal", b"skipped")])
        self.assertEqual(folders, [({"\\hasnochildren", "\\sent"}, 'INBOX.Sent "Items"'), ({"\\noselect"}, "Archive")])


class CountryCodeTests(HandoffTestCase):
    """Numbers written without a country code (profile code empty)."""

    def setUp(self):
        super().setUp()
        self.profile.phone_country_code = ""
        self.profile.save()
        self.contact = self.app.contact

    def number(self, written, *, post="We're hiring a Python developer. Remote.", location=""):
        self.contact.whatsapp = written
        self.contact.save()
        self.job.source_post_text = post
        self.job.full_jd = post
        self.job.location = location
        self.job.save()
        return self.client.get(f"{self.base}/").data["whatsapp"]

    def test_written_with_a_code_is_used_as_written(self):
        self.assertEqual(self.number("+44 7700 900123")["number"], "+447700900123")
        self.assertEqual(self.number("0091 98765 43210")["number"], "+919876543210")
        self.assertEqual(self.number("919876543210")["number"], "+919876543210")

    def test_job_in_india_means_plus_91(self):
        found = self.number("98765 43210", location="Pune")
        self.assertEqual(found["number"], "+919876543210")
        self.assertIn("+91 (India)", found["note"])
        self.assertEqual(self.number("098765 43210", post="Hiring in Bengaluru, 12 LPA")["number"], "+919876543210")
        preview = self.preview("WHATSAPP")
        self.assertTrue(preview["whatsapp_link"].startswith("https://wa.me/919876543210?text="))
        self.assertTrue(any("+91 (India)" in n for n in preview["notes"]))

    def test_code_of_another_number_in_the_post(self):
        post = "Hiring! Call +44 7700 900111 or WhatsApp 07700 900222. C++17 a plus."
        found = self.number("07700 900222", post=post)
        self.assertEqual(found["number"], "+447700900222")
        self.assertIn("+44", found["note"])

    def test_unknown_country_is_asked_not_guessed(self):
        found = self.number("98765 43210", post="Hiring a C++17 developer, 5+ years.")
        self.assertEqual(found["number"], "")
        self.assertIn("country code", found["problem"])
        # A leading 0 is never sent as part of the number.
        found = self.number("098765 43210")
        self.assertEqual(found["number"], "")
        self.assertIn("country code", found["problem"])
        self.assertEqual(self.compose("WHATSAPP", "link").status_code, 400)

    def test_profile_code_wins(self):
        self.profile.phone_country_code = "971"
        self.profile.save()
        found = self.number("050 123 4567", location="Pune")
        self.assertEqual((found["number"], found["note"]), ("+971501234567", ""))


class WhatsAppHandoffTests(HandoffTestCase):
    def test_link_opens_whatsapp_and_confirmation_marks_sent(self):
        response = self.compose("WHATSAPP", "link")
        self.assertEqual(response.status_code, 200, response.data)
        record = response.data["record"]
        self.assertTrue(record["open_url"].startswith("https://wa.me/919876543210?text=Hi%20Riya"))
        self.assertEqual((record["status"], record["provider"]), ("COMPOSE_OPENED", "whatsapp_link"))
        self.app.refresh_from_db()
        self.assertEqual(self.app.status, "COMPOSE_OPENED")
        self.assertIsNone(self.app.sent_at)
        self.assertEqual(self.verify(record["id"]).status_code, 400)  # WhatsApp cannot be checked
        response = self.confirm(record["id"])
        self.assertEqual(response.status_code, 200, response.data)
        self.app.refresh_from_db()
        self.assertEqual((self.app.status, self.app.channel), ("SENT", "WHATSAPP"))
        self.assertEqual(self.confirm(record["id"]).status_code, 409)
        self.assertEqual(len(mail.outbox), 0)

    def test_older_whatsapp_sent_endpoint_confirms_the_open_hand_off(self):
        record_id = self.compose("WHATSAPP", "link").data["record"]["id"]
        response = self.client.post(f"{self.base}/whatsapp-sent/", {"version": "ignored", "confirm": True},
                                    format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(SendRecord.objects.get(pk=record_id).status, "CONFIRMED")
        self.assertEqual(SendRecord.objects.count(), 1)

    def test_both_channels_can_be_sent_once_each(self):
        email_record = self.compose().data["record"]["id"]
        self.confirm(email_record)
        self.app.refresh_from_db()
        sent_at = self.app.sent_at
        self.assertIn("Already sent by email.", self.preview()["problems"])
        self.assertEqual(self.compose().status_code, 400)

        # WhatsApp is still possible; the application stays SENT (sent once, first channel).
        response = self.compose("WHATSAPP", "link")
        self.assertEqual(response.status_code, 200, response.data)
        self.app.refresh_from_db()
        self.assertEqual(self.app.status, "SENT")
        self.confirm(response.data["record"]["id"])
        self.app.refresh_from_db()
        self.assertEqual((self.app.status, self.app.channel, self.app.sent_at), ("SENT", "EMAIL", sent_at))
        self.assertEqual(ApplicationEvent.objects.filter(application=self.app, status="SENT", kind="STATUS").count(), 1)
        self.assertTrue(ApplicationEvent.objects.filter(application=self.app, kind="NOTE",
                                                        notes__contains="WhatsApp to").exists())
        states = self.client.get(f"{self.base}/").data["send_state"]
        self.assertEqual((states["EMAIL"]["state"], states["WHATSAPP"]["state"]), ("sent", "sent"))
        self.assertEqual(self.compose("WHATSAPP", "link").status_code, 400)


class HandoffIsolationTests(HandoffTestCase):
    def test_another_user_cannot_open_confirm_check_or_download(self):
        record = self.compose(method="eml").data["record"]
        sam = make_user("sam-example")
        self.client.force_login(sam)
        version = "x" * 32
        for url, body in ((f"{self.base}/compose/", {"channel": "EMAIL", "method": "eml", "version": version}),
                          (f"{self.base}/compose/confirm/", {"record": record["id"], "confirm": True}),
                          (f"{self.base}/compose/verify/", {"record": record["id"]}),
                          (f"{self.base}/compose/abandon/", {"record": record["id"]})):
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url, body, format="json").status_code, 404)
        self.assertEqual(self.client.get(f"{self.base}/compose/draft/?record={record['id']}").status_code, 404)
        self.assertEqual(SendRecord.objects.get(pk=record["id"]).status, "COMPOSE_OPENED")

    def test_a_record_of_another_application_is_refused(self):
        other_job = self.make_job(2)
        self.select(other_job)
        other = Application.objects.get(job=other_job)
        foreign = SendRecord.objects.create(application=other, channel="EMAIL", provider="eml_file",
                                            status="COMPOSE_OPENED", recipient="riya@fictional-labs.example")
        self.assertEqual(self.confirm(foreign.pk).status_code, 404)
        self.assertEqual(self.client.get(f"{self.base}/compose/draft/?record={foreign.pk}").status_code, 404)
        self.assertEqual(SendRecord.objects.get(pk=foreign.pk).status, "COMPOSE_OPENED")
