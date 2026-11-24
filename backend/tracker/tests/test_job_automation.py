"""Job Automation: review (phase 2), lifecycle (phase 4), sending (phase 5), isolation.

Fictional people and addresses only. Email goes to Django's in-memory test outbox;
nothing is ever delivered. WhatsApp is only ever a link.
"""

import smtplib
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.utils import timezone

from collector.models import CollectedPost, CollectorRun
from tracker.credentials import decrypt
from tracker.models import (
    Application,
    ApplicationEvent,
    Company,
    Contact,
    JobOpportunity,
    JobStatus,
    Profile,
    Resume,
    SendingAccount,
    SendRecord,
    Skill,
)
from tracker.review import match_resume
from tracker.sending import whatsapp_digits

from .helpers import APITestCase, make_pdf_bytes, make_user, upload

POST_TEXT = ("We're hiring a Python Backend Developer at Fictional Labs!\nLocation: Pune\n"
             "Python, Django, PostgreSQL. 2-4 years.\nSend your CV to riya@fictional-labs.example "
             "or WhatsApp +91 98765 43210")


class AutomationTestCase(APITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.profile = Profile.objects.create(name="Maya Example", target_roles="Python Developer",
                                              experience_years=3, phone_country_code="91")
        Skill.objects.create(name="Python", profile=self.profile)
        self.resume = Resume.objects.create(name="General CV", profile=self.profile, is_default=True,
                                            file_path=upload("maya-cv.pdf", make_pdf_bytes("Maya Python")))
        self.job = self.make_job()

    def make_job(self, n=1, owner=None, text=POST_TEXT, complete=True, title="Python Backend Developer"):
        owner = owner or self.user
        company = Company.objects.create(owner=owner, name=f"Fictional Labs {n}")
        job = JobOpportunity.objects.create(
            owner=owner, title=title, full_jd=text, source_post_text=text, jd_fingerprint=f"fp-{owner.pk}-{n}",
            company=company, source="LINKEDIN", recruiter_name="Riya Fictional", jd_complete=complete,
            source_url=f"https://www.linkedin.com/feed/update/urn:li:activity:{7300000000000000000 + n}/",
            extraction={"contacts": [
                {"kind": "email", "value": "riya@fictional-labs.example", "evidence": "Send your CV to riya@…"}]},
        )
        contact = Contact.objects.create(owner=owner, name="Riya Fictional", email="riya@fictional-labs.example",
                                         whatsapp="+91 98765 43210")
        job.contacts.add(contact)
        return job

    def select(self, job=None):
        return self.client.post(f"/api/v1/jobs/{(job or self.job).pk}/select/", {}, format="json")


class ReviewTests(AutomationTestCase):
    def test_list_has_review_fields(self):
        data = self.client.get("/api/v1/jobs/").data[0]
        self.assertEqual(data["recruiter_name"], "Riya Fictional")
        self.assertTrue(data["jd_complete"])
        self.assertEqual(data["contact_summary"], {"emails": ["riya@fictional-labs.example"], "phones": [],
                                                   "whatsapp": ["+91 98765 43210"]})
        self.assertTrue(data["eligibility"]["eligible"])
        self.assertEqual(data["matching_resume"]["id"], self.resume.pk)
        self.assertIsNone(data["application"])
        detail = self.client.get(f"/api/v1/jobs/{self.job.pk}/").data
        self.assertEqual(detail["source_post_text"], POST_TEXT)
        self.assertEqual(detail["extraction"]["contacts"][0]["value"], "riya@fictional-labs.example")

    def test_eligibility_issues(self):
        incomplete = self.make_job(2, complete=False)
        no_contact = self.make_job(3)
        no_contact.contacts.clear()
        by_id = {j["id"]: j["eligibility"] for j in self.client.get("/api/v1/jobs/").data}
        self.assertFalse(by_id[incomplete.pk]["eligible"])
        self.assertIn("incomplete_jd", [i["code"] for i in by_id[incomplete.pk]["issues"]])
        self.assertTrue(by_id[no_contact.pk]["eligible"])  # a warning only: the user may add one
        self.assertIn("no_contact", [i["code"] for i in by_id[no_contact.pk]["issues"]])
        Resume.objects.all().update(is_active=False)
        data = self.client.get(f"/api/v1/jobs/{self.job.pk}/").data
        self.assertFalse(data["eligibility"]["eligible"])
        self.assertIn("no_resume", [i["code"] for i in data["eligibility"]["issues"]])

    def test_run_filter(self):
        run = CollectorRun.objects.create(owner=self.user, trigger="api", source="file")
        CollectedPost.objects.create(run=run, position=0, status="created", job=self.job)
        CollectedPost.objects.create(run=run, position=1, status="skipped")
        self.make_job(2)
        self.assertEqual([j["id"] for j in self.client.get(f"/api/v1/jobs/?run={run.pk}").data], [self.job.pk])
        # Another account's run id shows nothing.
        other = make_user("sam-example")
        theirs = CollectorRun.objects.create(owner=other, trigger="api", source="file")
        CollectedPost.objects.create(run=theirs, position=0, status="created", job=self.job)
        self.assertEqual(self.client.get(f"/api/v1/jobs/?run={theirs.pk}").data, [])

    def test_edit_extracted_details(self):
        url = f"/api/v1/jobs/{self.job.pk}/"
        response = self.client.patch(url, {
            "title": "Senior Python Developer", "company_name": "Fictional Labs Pvt", "location": "Remote",
            "recruiter_name": "Riya F.",
            "contacts": [{"email": "jobs@fictional-labs.example", "name": "Riya F."}],
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["title"], "Senior Python Developer")
        self.assertEqual(response.data["company"]["name"], "Fictional Labs Pvt")
        self.assertEqual([c["email"] for c in response.data["contacts"]], ["jobs@fictional-labs.example"])
        self.assertIsNotNone(response.data["contacts_verified_at"])
        self.assertEqual(response.data["extraction"]["edited_fields"],
                         ["company", "contacts", "location", "recruiter_name", "title"])
        self.assertEqual(response.data["source_post_text"], POST_TEXT)  # the original is never edited

    def test_edit_an_existing_contact_and_validation(self):
        contact = self.job.contacts.get()
        url = f"/api/v1/jobs/{self.job.pk}/"
        response = self.client.patch(url, {"contacts": [{"id": contact.pk, "whatsapp": ""}]}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        contact.refresh_from_db()
        self.assertIsNone(contact.whatsapp)
        self.assertIsNone(contact.normalized_whatsapp)
        for body in ({"contacts": [{"phone": "12"}]}, {"contacts": [{"name": "Only a name"}]},
                     {"full_jd": "too short"}, {"title": "  "}):
            with self.subTest(body=body):
                self.assertEqual(self.client.patch(url, body, format="json").status_code, 400)
        other = make_user("sam-example")
        theirs = Contact.objects.create(owner=other, email="sam@example.org")
        response = self.client.patch(url, {"contacts": [{"id": theirs.pk, "email": "x@example.org"}]}, format="json")
        self.assertEqual(response.status_code, 400)
        theirs.refresh_from_db()
        self.assertEqual(theirs.email, "sam@example.org")

    def test_pasting_the_full_description_completes_the_job(self):
        job = self.make_job(2, complete=False)
        response = self.client.patch(f"/api/v1/jobs/{job.pk}/", {"full_jd": POST_TEXT + "\nBenefits: health."},
                                     format="json")
        self.assertTrue(response.data["jd_complete"])
        self.assertIn("full_jd", response.data["extraction"]["edited_fields"])

    def test_skip_and_bulk_status(self):
        other = self.make_job(2)
        response = self.client.post("/api/v1/jobs/bulk-status/", {"jobs": [self.job.pk, other.pk],
                                                                    "status": "SKIPPED"}, format="json")
        self.assertEqual(response.data, {"updated": 2})
        self.assertEqual(set(JobOpportunity.objects.values_list("status", flat=True)), {"SKIPPED"})
        theirs = self.make_job(3, owner=make_user("sam-example"))
        response = self.client.post("/api/v1/jobs/bulk-status/", {"jobs": [theirs.pk], "status": "SKIPPED"},
                                    format="json")
        self.assertEqual(response.status_code, 400)
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, "DISCOVERED")

    def test_matching_resume_prefers_the_title(self):
        backend = Resume.objects.create(name="Backend resume", profile=self.profile,
                                        file_path=upload("backend.pdf", make_pdf_bytes("x")))
        Resume.objects.create(name="Frontend resume", profile=self.profile,
                              file_path=upload("front.pdf", make_pdf_bytes("x")))
        match = match_resume(self.profile, self.job)
        self.assertEqual(match.resume, backend)
        self.assertIn("backend", match.reason)
        unrelated = self.make_job(2, title="Data Analyst")
        self.assertEqual(match_resume(self.profile, unrelated).resume, self.resume)  # the default
        Resume.objects.create(name="No file", profile=self.profile)
        self.assertNotEqual(match_resume(self.profile, unrelated).resume.name, "No file")


class SelectionTests(AutomationTestCase):
    def test_select_creates_one_prefilled_application(self):
        response = self.select()
        self.assertEqual(response.status_code, 201, response.data)
        application = Application.objects.get()
        self.assertEqual(application.resume, self.resume)
        self.assertEqual(application.contact.email, "riya@fictional-labs.example")
        self.assertEqual(application.channel, "EMAIL")
        self.assertEqual(application.status, "SHORTLISTED")
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, JobStatus.SHORTLISTED)
        self.assertEqual(response.data["job"]["application"]["id"], application.pk)
        # Selecting again never makes a second application.
        again = self.select()
        self.assertEqual(again.status_code, 200)
        self.assertFalse(again.data["created"])
        self.assertEqual(Application.objects.count(), 1)

    def test_contact_added_later_is_used_by_the_application(self):
        self.job.contacts.clear()
        self.select()
        application = Application.objects.get()
        self.assertIsNone(application.contact)
        self.client.patch(f"/api/v1/jobs/{self.job.pk}/", {"contacts": [{"email": "hr@fictional-labs.example"}]},
                          format="json")
        application.refresh_from_db()
        self.assertEqual(application.contact.email, "hr@fictional-labs.example")
        self.assertEqual(application.channel, "EMAIL")

    def test_whatsapp_channel_when_the_post_has_only_a_number(self):
        self.job.contacts.update(email=None, normalized_email=None)
        self.select()
        self.assertEqual(Application.objects.get().channel, "WHATSAPP")

    def test_select_many(self):
        jobs = [self.make_job(n) for n in range(2, 5)]
        self.select(jobs[0])
        response = self.client.post("/api/v1/jobs/select/", {"jobs": [j.pk for j in jobs] + [jobs[1].pk]},
                                    format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["created"], 2)
        self.assertEqual(response.data["already_selected"], 1)
        self.assertEqual(Application.objects.count(), 3)

    def test_other_accounts_jobs_cannot_be_selected(self):
        theirs = self.make_job(9, owner=make_user("sam-example"))
        self.assertEqual(self.select(theirs).status_code, 404)
        response = self.client.post("/api/v1/jobs/select/", {"jobs": [theirs.pk]}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Application.objects.exists())

    def test_two_users_apply_to_the_same_posting_independently(self):
        sam = make_user("sam-example")
        sam_profile = Profile.objects.create(owner=sam, name="Sam", target_roles="Developer")
        Resume.objects.create(name="Sam CV", profile=sam_profile, is_default=True,
                              file_path=upload("sam.pdf", make_pdf_bytes("Sam")))
        sams_copy = self.make_job(1, owner=sam)  # same URL, saved by Sam
        self.assertEqual(self.select().status_code, 201)
        self.client.force_login(sam)
        self.assertEqual(self.select(sams_copy).status_code, 201)
        self.assertEqual(Application.objects.count(), 2)
        self.assertEqual(Application.objects.get(profile=sam_profile).resume.name, "Sam CV")


class SendingTestCase(AutomationTestCase):
    def setUp(self):
        super().setUp()
        self.select()
        self.app = Application.objects.get()
        Application.objects.filter(pk=self.app.pk).update(
            status="READY_TO_SEND", generated_subject="Python Backend Developer application",
            generated_body="Dear Riya,\nI am applying for the Python Backend Developer role.\nMaya",
            generated_whatsapp_message="Hi Riya, I'm Maya - applying for the Python Backend Developer role.",
        )
        self.account_url = "/api/v1/sending/email-account/"

    def connect_email(self, user_client=None):
        client = user_client or self.client
        return client.put(self.account_url, {
            "from_name": "Maya Example", "from_email": "maya@example.org", "smtp_host": "smtp.example.org",
            "smtp_port": 587, "security": "starttls", "username": "maya@example.org",
            "password": "fictional-app-password"}, format="json")

    def preview(self, channel="EMAIL"):
        return self.client.get(f"/api/v1/applications/{self.app.pk}/send-preview/?channel={channel}").data

    def send(self, version=None, confirm=True):
        version = version or self.preview()["version"]
        return self.client.post(f"/api/v1/applications/{self.app.pk}/send/",
                                {"channel": "EMAIL", "version": version, "confirm": confirm}, format="json")


class EmailAccountTests(SendingTestCase):
    def test_password_is_write_only_and_encrypted(self):
        response = self.connect_email()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("password", response.data["account"])
        self.assertTrue(response.data["account"]["has_password"])
        account = SendingAccount.objects.get(owner=self.user)
        self.assertNotIn("fictional-app-password", account.password_encrypted)
        self.assertEqual(decrypt(account.password_encrypted), "fictional-app-password")
        self.assertEqual(self.client.get(self.account_url).data["account"]["from_email"], "maya@example.org")
        self.assertEqual(self.client.get(self.account_url).data["mode"], "locmem")
        # Updating without a password keeps the saved one.
        self.client.put(self.account_url, {"from_email": "maya@example.org", "smtp_host": "smtp.example.org",
                                           "smtp_port": 465, "security": "ssl", "username": "maya@example.org"},
                        format="json")
        account.refresh_from_db()
        self.assertEqual(decrypt(account.password_encrypted), "fictional-app-password")
        self.assertEqual(account.smtp_port, 465)

    def test_validation(self):
        first = self.client.put(self.account_url, {"from_email": "maya@example.org", "smtp_host": "smtp.example.org",
                                                   "smtp_port": 587, "security": "starttls",
                                                   "username": "maya"}, format="json")
        self.assertIn("password", first.data)
        bad = self.client.put(self.account_url, {"from_email": "nope", "smtp_host": "https://smtp",
                                                 "smtp_port": 0, "security": "tls", "username": "m",
                                                 "password": "x"}, format="json")
        self.assertEqual(set(bad.data), {"from_email", "smtp_host", "smtp_port", "security"})

    def test_test_email_goes_to_the_users_own_address(self):
        self.connect_email()
        response = self.client.post(f"{self.account_url}test/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["maya@example.org"])
        self.assertIsNotNone(response.data["account"]["verified_at"])

    def test_accounts_are_per_user(self):
        self.connect_email()
        sam = make_user("sam-example")
        self.client.force_login(sam)
        self.assertIsNone(self.client.get(self.account_url).data["account"])
        self.assertEqual(self.client.post(f"{self.account_url}test/", {}, format="json").status_code, 400)
        self.assertEqual(len(mail.outbox), 0)


class EmailSendingTests(SendingTestCase):
    def setUp(self):
        super().setUp()
        self.connect_email()

    def test_preview_shows_everything_that_will_be_sent(self):
        preview = self.preview()
        self.assertTrue(preview["ready"], preview["problems"])
        self.assertEqual(preview["recipient"], "riya@fictional-labs.example")
        self.assertEqual(preview["recipient_source"], "post")
        self.assertEqual(preview["sender"], "Maya Example <maya@example.org>")
        self.assertEqual(preview["subject"], "Python Backend Developer application")
        self.assertEqual(preview["attachment"]["name"], "Maya_Example_Resume.pdf")
        self.assertTrue(preview["attachment"]["ok"])
        self.assertGreater(preview["attachment"]["size"], 0)
        self.assertEqual(len(mail.outbox), 0)  # previewing sends nothing

    def test_send_with_confirmation(self):
        response = self.send()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["riya@fictional-labs.example"])
        self.assertEqual(message.from_email, "Maya Example <maya@example.org>")
        self.assertEqual(message.subject, "Python Backend Developer application")
        self.assertEqual(message.attachments[0][0], "Maya_Example_Resume.pdf")
        self.assertEqual(message.attachments[0][2], "application/pdf")
        self.app.refresh_from_db()
        self.assertEqual(self.app.status, "SENT")
        self.assertIsNotNone(self.app.sent_at)
        self.assertEqual(self.app.job.status, JobStatus.APPLIED)
        record = SendRecord.objects.get()
        self.assertEqual((record.status, record.recipient, record.attachment_name),
                         ("SENT", "riya@fictional-labs.example", "Maya_Example_Resume.pdf"))
        self.assertTrue(record.message_id)
        self.assertTrue(ApplicationEvent.objects.filter(application=self.app, status="SENT",
                                                        notes__contains="Email sent to").exists())
        # Never twice.
        again = self.send(version=response.data["record"]["id"] and self.preview()["version"])
        self.assertIn(again.status_code, (400, 409))
        self.assertEqual(len(mail.outbox), 1)

    def test_no_send_without_explicit_confirmation(self):
        for confirm in (False, None):
            with self.subTest(confirm=confirm):
                body = {"channel": "EMAIL", "version": self.preview()["version"]}
                if confirm is not None:
                    body["confirm"] = confirm
                response = self.client.post(f"/api/v1/applications/{self.app.pk}/send/", body, format="json")
                self.assertEqual(response.status_code, 400)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(SendRecord.objects.exists())

    def test_drafts_must_be_approved_first(self):
        Application.objects.filter(pk=self.app.pk).update(status="GENERATED")
        self.assertIn("Approve the drafts first.", self.preview()["problems"])
        response = self.send()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(mail.outbox), 0)

    def test_changed_draft_after_preview_is_refused(self):
        version = self.preview()["version"]
        self.client.patch(f"/api/v1/applications/{self.app.pk}/", {"generated_body": "Edited after review"},
                          format="json")
        response = self.send(version=version)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(mail.outbox), 0)

    def test_attachment_problems_block_sending(self):
        self.resume.file_path.delete(save=False)
        Resume.objects.filter(pk=self.resume.pk).update(file_path="resumes/missing.pdf")
        preview = self.preview()
        self.assertFalse(preview["ready"])
        self.assertIn("missing", preview["attachment"]["problem"])
        self.assertEqual(self.send().status_code, 400)
        self.assertEqual(len(mail.outbox), 0)

    def test_no_recipient_or_no_account(self):
        self.app.contact.email = None
        self.app.contact.save()
        self.assertTrue(any("recipient" in p for p in self.preview()["problems"]))
        SendingAccount.objects.all().delete()
        self.assertTrue(any("Connect your email" in p for p in self.preview()["problems"]))

    def test_failure_is_recorded_and_can_be_retried(self):
        with mock.patch("django.core.mail.EmailMessage.send",
                        side_effect=smtplib.SMTPAuthenticationError(535, b"bad credentials")):
            response = self.send()
        self.assertEqual(response.status_code, 502)
        self.assertIn("app password", response.data["detail"])
        self.app.refresh_from_db()
        self.assertEqual(self.app.status, "SEND_FAILED")
        self.assertIsNone(self.app.sent_at)
        self.assertIn("rejected", self.app.last_error)
        self.assertEqual(SendRecord.objects.get().status, "FAILED")
        self.assertEqual(self.app.job.status, JobStatus.SHORTLISTED)  # not applied yet
        # Retry (still with explicit confirmation) succeeds.
        retry = self.send()
        self.assertEqual(retry.status_code, 200, retry.data)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(list(SendRecord.objects.order_by("id").values_list("status", flat=True)),
                         ["FAILED", "SENT"])

    def test_users_cannot_set_send_failed_themselves(self):
        response = self.client.patch(f"/api/v1/applications/{self.app.pk}/", {"status": "SEND_FAILED"},
                                     format="json")
        self.assertEqual(response.status_code, 400)

    def test_concurrent_send_is_refused(self):
        SendRecord.objects.create(application=self.app, channel="EMAIL", provider="smtp", status="SENDING",
                                  recipient="riya@fictional-labs.example")
        self.assertEqual(self.send().status_code, 409)
        self.assertEqual(len(mail.outbox), 0)

    def test_send_records_listed(self):
        self.send()
        records = self.client.get(f"/api/v1/applications/{self.app.pk}/send-records/").data
        self.assertEqual([r["status"] for r in records], ["SENT"])


class WhatsAppTests(SendingTestCase):
    def test_click_to_chat_link_and_confirmation(self):
        Application.objects.filter(pk=self.app.pk).update(channel="WHATSAPP")
        preview = self.preview("WHATSAPP")
        self.assertTrue(preview["ready"], preview["problems"])
        self.assertTrue(preview["whatsapp_link"].startswith("https://wa.me/919876543210?text=Hi%20Riya"))
        self.assertTrue(any("attach your resume" in n for n in preview["notes"]))
        response = self.client.post(f"/api/v1/applications/{self.app.pk}/whatsapp-sent/",
                                    {"version": preview["version"], "confirm": True}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.app.refresh_from_db()
        self.assertEqual((self.app.status, self.app.channel), ("SENT", "WHATSAPP"))
        self.assertEqual(SendRecord.objects.get().status, "HANDED_OFF")
        self.assertEqual(len(mail.outbox), 0)  # ApplyForge itself sent nothing

    def test_number_normalisation(self):
        self.assertEqual(whatsapp_digits("+91 98765 43210"), ("919876543210", ""))
        self.assertEqual(whatsapp_digits("0091-98765-43210"), ("919876543210", ""))
        self.assertEqual(whatsapp_digits("98765 43210", "91"), ("919876543210", ""))
        self.assertEqual(whatsapp_digits("098765 43210", "91"), ("919876543210", ""))
        self.assertEqual(whatsapp_digits("98765 43210")[0], "")
        self.assertIn("country code", whatsapp_digits("98765 43210")[1])
        self.assertEqual(whatsapp_digits("12345")[0], "")

    def test_requires_confirmation(self):
        preview = self.preview("WHATSAPP")
        response = self.client.post(f"/api/v1/applications/{self.app.pk}/whatsapp-sent/",
                                    {"version": preview["version"], "confirm": False}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(SendRecord.objects.exists())


class IsolationTests(SendingTestCase):
    def setUp(self):
        super().setUp()
        self.connect_email()
        self.sam = make_user("sam-example")
        self.client.force_login(self.sam)

    def test_another_user_cannot_see_or_send_my_application(self):
        base = f"/api/v1/applications/{self.app.pk}"
        self.assertEqual(self.client.get(f"{base}/").status_code, 404)
        self.assertEqual(self.client.get(f"{base}/send-preview/").status_code, 404)
        self.assertEqual(self.client.get(f"{base}/send-records/").status_code, 404)
        response = self.client.post(f"{base}/send/", {"version": "x" * 32, "confirm": True}, format="json")
        self.assertEqual(response.status_code, 404)
        response = self.client.post(f"{base}/whatsapp-sent/", {"version": "x" * 32, "confirm": True}, format="json")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.post(f"{base}/generate/", {}, format="json").status_code, 404)
        self.assertEqual(self.client.patch(f"{base}/", {"generated_body": "x"}, format="json").status_code, 404)
        self.assertEqual(len(mail.outbox), 0)

    def test_another_user_cannot_download_my_resume(self):
        self.assertEqual(self.client.get(f"/api/v1/resumes/{self.resume.pk}/download/").status_code, 404)
        self.assertEqual(self.client.get(f"/media/{self.resume.file_path.name}").status_code, 404)
        self.client.force_login(self.user)
        response = self.client.get(f"/api/v1/resumes/{self.resume.pk}/download/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(b"".join(response.streaming_content)[:4], b"%PDF")

    def test_another_user_cannot_see_my_jobs_drafts_or_runs(self):
        run = CollectorRun.objects.create(owner=self.user, trigger="api", source="file")
        self.assertEqual(self.client.get("/api/v1/jobs/").data, [])
        self.assertEqual(self.client.get(f"/api/v1/jobs/{self.job.pk}/").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/applications/").data, [])
        self.assertEqual(self.client.get("/api/v1/collector/runs/").data, [])
        self.assertEqual(self.client.get(f"/api/v1/collector/runs/{run.pk}/").status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/collector/runs/{run.pk}/items/").status_code, 404)

    def test_sending_always_uses_the_application_owners_mailbox(self):
        # Sam connects his own mailbox; it can never be used for Maya's application and vice versa.
        self.connect_email()
        SendingAccount.objects.filter(owner=self.sam).update(from_email="sam@example.org")
        self.client.force_login(self.user)
        preview = self.preview()
        self.assertEqual(preview["sender"], "Maya Example <maya@example.org>")
        self.send()
        self.assertEqual(mail.outbox[0].from_email, "Maya Example <maya@example.org>")


class LifecycleTests(AutomationTestCase):
    def test_send_failed_counts_as_awaiting_action(self):
        self.select()
        Application.objects.update(status="SEND_FAILED")
        summary = self.client.get("/api/v1/applications/summary/").data
        self.assertEqual(summary["by_status"]["SEND_FAILED"], 1)
        self.assertEqual(self.client.get("/api/v1/applications/?awaiting_action=true").data[0]["status"],
                         "SEND_FAILED")

    def test_already_applied_is_shown_on_the_job(self):
        self.select()
        Application.objects.update(status="SENT", sent_at=timezone.now())
        data = self.client.get(f"/api/v1/jobs/{self.job.pk}/").data
        self.assertIn("already_applied", [i["code"] for i in data["eligibility"]["issues"]])
        self.assertFalse(data["eligibility"]["eligible"])
