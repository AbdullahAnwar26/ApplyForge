"""Application tracking API tests (Milestone 4)."""

from unittest import mock

from django.core import mail

from tracker.models import (
    Application,
    ApplicationEvent,
    Company,
    Contact,
    JobOpportunity,
    JobStatus,
    Profile,
    Resume,
    ScreeningOutcome,
)

from .helpers import APITestCase

URL = "/api/v1/applications/"


def make_job(n=1, status=JobStatus.SHORTLISTED, company=None, outcome=ScreeningOutcome.NEEDS_REVIEW):
    job = JobOpportunity.objects.create(
        title=f"Job {n}", full_jd=f"jd {n}", jd_fingerprint=f"fp{n}", status=status,
        company=company, screening_outcome=outcome,
    )
    return job


class ApplicationAPITestCase(APITestCase):
    def setUp(self):
        super().setUp()
        self.profile = Profile.objects.create(name="Jane", target_roles="Backend")
        self.company = Company.objects.create(name="Acme")
        self.job = make_job(company=self.company)
        self.contact = Contact.objects.create(name="HR", email="hr@acme.com", phone="5551234567")
        self.job.contacts.add(self.contact)

    def create(self, **data):
        body = {"job": self.job.id, **data}
        return self.client.post(URL, body, format="json")

    def patch(self, app_id, **data):
        return self.client.patch(f"{URL}{app_id}/", data, format="json")

    def events(self, app_id):
        return self.client.get(f"{URL}{app_id}/events/").data


class AuthTests(ApplicationAPITestCase):
    def test_all_endpoints_require_token(self):
        app = Application.objects.create(profile=self.profile, job=self.job)
        for method, url in [
            ("get", URL), ("post", URL), ("get", f"{URL}{app.id}/"),
            ("patch", f"{URL}{app.id}/"), ("get", f"{URL}{app.id}/events/"),
            ("get", f"{URL}summary/"),
        ]:
            with self.subTest(method=method, url=url):
                self.assertEqual(getattr(self.anon, method)(url, {}, format="json").status_code, 401)
        self.assertEqual(Application.objects.count(), 1)


class CreateTests(ApplicationAPITestCase):
    def test_create_and_retrieve(self):
        response = self.create(contact=self.contact.id, note="Applying via email")
        self.assertEqual(response.status_code, 201, response.data)
        data = response.data
        self.assertEqual(data["status"], "SHORTLISTED")
        self.assertEqual(data["channel"], "EMAIL")
        self.assertEqual(data["profile"], self.profile.id)
        self.assertEqual(data["contact"], self.contact.id)
        self.assertEqual(data["job"]["company"]["name"], "Acme")
        for field in ("generated_subject", "generated_body", "generated_whatsapp_message", "last_error"):
            self.assertIn(field, data)
            self.assertIsNone(data[field])

        detail = self.client.get(f"{URL}{data['id']}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data["id"], data["id"])
        events = self.events(data["id"])
        self.assertEqual([(e["status"], e["notes"]) for e in events], [("SHORTLISTED", "Applying via email")])

    def test_create_does_not_touch_job(self):
        self.create()
        job = JobOpportunity.objects.get(pk=self.job.pk)
        self.assertEqual(job.status, JobStatus.SHORTLISTED)
        self.assertEqual(job.screening_outcome, ScreeningOutcome.NEEDS_REVIEW)

    def test_requires_user_shortlisted_job(self):
        for status in (JobStatus.DISCOVERED, JobStatus.SKIPPED):
            with self.subTest(status=status):
                job = make_job(n=status, status=status, outcome=ScreeningOutcome.SHORTLISTED)
                response = self.client.post(URL, {"job": job.id}, format="json")
                self.assertEqual(response.status_code, 400)
                self.assertIn("SHORTLISTED", str(response.data["job"]))
        applied = make_job(n="applied", status=JobStatus.APPLIED)
        self.assertEqual(self.client.post(URL, {"job": applied.id}, format="json").status_code, 201)

    def test_screening_rejected_job_allowed_when_user_shortlists(self):
        job = make_job(n=9, outcome=ScreeningOutcome.REJECTED)
        self.assertEqual(self.client.post(URL, {"job": job.id}, format="json").status_code, 201)

    def test_duplicate_prevented(self):
        first = self.create().data
        response = self.create()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["application"], first["id"])
        self.assertEqual(Application.objects.count(), 1)
        self.assertEqual(ApplicationEvent.objects.count(), 1)

    def test_duplicate_race_uses_db_constraint(self):
        first = self.create().data
        with mock.patch("tracker.applications.Application.objects.filter") as filt:
            # First pre-check misses; the unique constraint catches the duplicate.
            filt.return_value.first.side_effect = [None, Application.objects.get(pk=first["id"])]
            response = self.create()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Application.objects.count(), 1)

    def test_same_job_other_profile_allowed(self):
        self.create(profile=self.profile.id)
        other = Profile.objects.create(name="Other", target_roles="E")
        self.assertEqual(self.create(profile=other.id).status_code, 201)

    def test_profile_selection(self):
        Profile.objects.create(name="Other", target_roles="E")
        response = self.create()
        self.assertEqual(response.status_code, 400)
        self.assertIn("Multiple profiles", str(response.data["profile"]))
        self.assertEqual(self.create(profile=999).status_code, 400)

    def test_invalid_relationships(self):
        other_profile = Profile.objects.create(name="Other", target_roles="E")
        foreign_resume = Resume.objects.create(name="r", profile=other_profile)
        inactive = Resume.objects.create(name="old", profile=self.profile, is_active=False)
        unlinked = Contact.objects.create(email="x@other.com")
        no_email = Contact.objects.create(phone="5550000000")
        self.job.contacts.add(no_email)
        cases = [
            ({"job": 999}, "job"),
            ({"job": None}, "job"),
            ({"contact": 999}, "contact"),
            ({"contact": unlinked.id}, "contact"),
            ({"resume": 999}, "resume"),
            ({"resume": foreign_resume.id}, "resume"),
            ({"resume": inactive.id}, "resume"),
            ({"contact": no_email.id, "channel": "EMAIL"}, "channel"),
            ({"channel": "FAX"}, "channel"),
        ]
        for overrides, field in cases:
            with self.subTest(overrides=overrides):
                response = self.client.post(
                    URL, {"job": self.job.id, "profile": self.profile.id, **overrides}, format="json"
                )
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn(field, response.data)
        self.assertEqual(Application.objects.count(), 0)
        ok = self.create(profile=self.profile.id, contact=no_email.id, channel="WHATSAPP")
        self.assertEqual(ok.status_code, 201, ok.data)

    def test_default_resume_used(self):
        default = Resume.objects.create(name="main", profile=self.profile, is_default=True)
        self.assertEqual(self.create().data["resume"], default.id)

    def test_generated_fields_not_writable_on_create(self):
        response = self.create(generated_body="Injected", status="SENT")
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.data["generated_body"])
        self.assertEqual(response.data["status"], "SHORTLISTED")


class StatusWorkflowTests(ApplicationAPITestCase):
    def setUp(self):
        super().setUp()
        self.app_id = self.create(contact=self.contact.id).data["id"]

    def test_manual_send_flow_and_timestamps(self):
        response = self.patch(self.app_id, status="OPENED")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNotNone(response.data["opened_at"])
        self.assertIsNone(response.data["sent_at"])

        self.patch(self.app_id, status="READY_TO_SEND")
        response = self.patch(self.app_id, status="SENT", note="Sent from Gmail")
        self.assertEqual(response.data["status"], "SENT")
        sent_at = response.data["sent_at"]
        self.assertIsNotNone(sent_at)

        self.patch(self.app_id, status="FOLLOW_UP")
        response = self.patch(self.app_id, status="SENT")
        self.assertEqual(response.data["sent_at"], sent_at)  # first send time kept
        self.assertEqual(self.patch(self.app_id, status="REJECTED").status_code, 200)

        statuses = [e["status"] for e in self.events(self.app_id)]
        self.assertEqual(
            statuses,
            ["REJECTED", "SENT", "FOLLOW_UP", "SENT", "READY_TO_SEND", "OPENED", "SHORTLISTED"],
        )
        sent_event = [e for e in self.events(self.app_id) if e["notes"] == "Sent from Gmail"]
        self.assertEqual(len(sent_event), 1)

    def test_pre_send_status_changes_do_not_change_job(self):
        for status in ("OPENED", "READY_TO_SEND", "SKIPPED", "SHORTLISTED"):
            self.patch(self.app_id, status=status)
        job = JobOpportunity.objects.get(pk=self.job.pk)
        self.assertEqual(job.status, JobStatus.SHORTLISTED)
        self.assertEqual(job.screening_outcome, ScreeningOutcome.NEEDS_REVIEW)

    def test_sent_marks_job_applied_without_touching_screening(self):
        self.patch(self.app_id, status="SENT")
        job = JobOpportunity.objects.get(pk=self.job.pk)
        self.assertEqual(job.status, JobStatus.APPLIED)
        self.assertEqual(job.screening_outcome, ScreeningOutcome.NEEDS_REVIEW)

    def test_job_applied_is_not_reversed_automatically(self):
        self.patch(self.app_id, status="SENT")
        self.patch(self.app_id, status="REJECTED")
        self.assertEqual(JobOpportunity.objects.get(pk=self.job.pk).status, JobStatus.APPLIED)
        # A later manual job decision is respected by follow-up/rejection changes.
        JobOpportunity.objects.filter(pk=self.job.pk).update(status=JobStatus.SKIPPED)
        self.patch(self.app_id, status="FOLLOW_UP")
        self.assertEqual(JobOpportunity.objects.get(pk=self.job.pk).status, JobStatus.SKIPPED)

    def test_unchanged_sent_does_not_touch_job(self):
        self.patch(self.app_id, status="SENT")
        JobOpportunity.objects.filter(pk=self.job.pk).update(status=JobStatus.SKIPPED)
        self.patch(self.app_id, status="SENT")  # no change -> no event, no job update
        self.assertEqual(JobOpportunity.objects.get(pk=self.job.pk).status, JobStatus.SKIPPED)

    def test_unchanged_status_creates_no_event(self):
        before = ApplicationEvent.objects.count()
        response = self.patch(self.app_id, status="SHORTLISTED", note="noop")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ApplicationEvent.objects.count(), before)

    def test_non_status_change_creates_no_event(self):
        before = ApplicationEvent.objects.count()
        response = self.patch(self.app_id, channel="OTHER")
        self.assertEqual(response.data["channel"], "OTHER")
        self.assertEqual(ApplicationEvent.objects.count(), before)

    def test_system_only_statuses_rejected(self):
        for status in ("GENERATED", "FAILED"):
            with self.subTest(status=status):
                response = self.patch(self.app_id, status=status)
                self.assertEqual(response.status_code, 400)
                self.assertIn("generation", str(response.data["status"]))

    def test_post_send_statuses_require_sent(self):
        for status in ("FOLLOW_UP", "REJECTED"):
            with self.subTest(status=status):
                self.assertEqual(self.patch(self.app_id, status=status).status_code, 400)

    def test_cannot_unsend(self):
        self.patch(self.app_id, status="SENT")
        for status in ("SHORTLISTED", "OPENED", "READY_TO_SEND", "SKIPPED"):
            with self.subTest(status=status):
                self.assertEqual(self.patch(self.app_id, status=status).status_code, 400)
        self.assertEqual(Application.objects.get(pk=self.app_id).status, "SENT")

    def test_skip_and_reopen(self):
        self.assertEqual(self.patch(self.app_id, status="SKIPPED").status_code, 200)
        self.assertEqual(self.patch(self.app_id, status="SHORTLISTED").status_code, 200)
        self.assertEqual(len(self.events(self.app_id)), 3)

    def test_generated_application_can_progress(self):
        # GENERATED is set by generation (Milestone 5); simulate it at model level.
        Application.objects.filter(pk=self.app_id).update(status="GENERATED")
        self.assertEqual(self.patch(self.app_id, status="OPENED").status_code, 200)

    def test_read_only_and_invalid_fields(self):
        for body in ({"generated_at": "2020-01-01T00:00:00Z"}, {"job": 1}, {"profile": 1},
                     {"sent_at": "2020-01-01T00:00:00Z"}, {"last_error": "x"},
                     {"status": "BOGUS"}, {}, {"note": "only a note"}, {"queue_position": 1}):
            with self.subTest(body=body):
                self.assertEqual(self.client.patch(f"{URL}{self.app_id}/", body, format="json").status_code, 400)

    def test_patch_relationship_validation(self):
        unlinked = Contact.objects.create(email="z@z.com")
        self.assertEqual(self.patch(self.app_id, contact=unlinked.id).status_code, 400)
        foreign = Resume.objects.create(name="r", profile=Profile.objects.create(name="O", target_roles="E"))
        self.assertEqual(self.patch(self.app_id, resume=foreign.id).status_code, 400)
        phone_only = Contact.objects.create(phone="5559999999")
        self.job.contacts.add(phone_only)
        self.assertEqual(self.patch(self.app_id, contact=phone_only.id).status_code, 400)  # channel EMAIL
        response = self.patch(self.app_id, contact=phone_only.id, channel="WHATSAPP")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["contact"], phone_only.id)
        # invalid change in the same request leaves everything untouched
        response = self.patch(self.app_id, status="SENT", contact=unlinked.id)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Application.objects.get(pk=self.app_id).status, "SHORTLISTED")

    def test_put_and_delete_not_allowed(self):
        self.assertEqual(self.client.put(f"{URL}{self.app_id}/", {}, format="json").status_code, 405)
        self.assertEqual(self.client.delete(f"{URL}{self.app_id}/").status_code, 405)

    def test_unknown_application_404(self):
        self.assertEqual(self.client.get(f"{URL}999/").status_code, 404)
        self.assertEqual(self.patch(999, status="SENT").status_code, 404)
        self.assertEqual(self.client.get(f"{URL}999/events/").status_code, 404)


class NoExternalMessagesTests(ApplicationAPITestCase):
    def test_full_workflow_sends_nothing(self):
        with mock.patch("smtplib.SMTP") as smtp, mock.patch("smtplib.SMTP_SSL") as smtp_ssl, \
                mock.patch("urllib.request.urlopen") as urlopen:
            app_id = self.create(contact=self.contact.id).data["id"]
            for status in ("OPENED", "READY_TO_SEND", "SENT", "FOLLOW_UP"):
                self.assertEqual(self.patch(app_id, status=status).status_code, 200)
            self.client.get(f"{URL}summary/")
        self.assertEqual(mail.outbox, [])
        smtp.assert_not_called()
        smtp_ssl.assert_not_called()
        urlopen.assert_not_called()


class EventHistoryTests(ApplicationAPITestCase):
    def test_events_newest_first_and_paginated(self):
        app_id = self.create().data["id"]
        for status in ("SKIPPED", "SHORTLISTED", "SKIPPED", "SHORTLISTED"):
            self.patch(app_id, status=status)
        events = self.events(app_id)
        self.assertEqual(len(events), 5)
        ids = [e["id"] for e in events]
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertEqual(events[-1]["notes"], "Application created.")
        self.assertEqual(events[0]["notes"], "Status changed from SKIPPED to SHORTLISTED.")
        self.assertEqual(set(events[0]), {"id", "kind", "status", "notes", "timestamp"})

        page = self.client.get(f"{URL}{app_id}/events/?page_size=2").data
        self.assertEqual(page["count"], 5)
        self.assertEqual([e["id"] for e in page["results"]], ids[:2])

    def test_events_scoped_to_application(self):
        a = self.create().data["id"]
        other_job = make_job(n=2)
        b = self.client.post(URL, {"job": other_job.id}, format="json").data["id"]
        self.patch(b, status="SKIPPED")
        self.assertEqual(len(self.events(a)), 1)
        self.assertEqual(len(self.events(b)), 2)


class ListFilterTests(ApplicationAPITestCase):
    def setUp(self):
        super().setUp()
        other_company = Company.objects.create(name="Globex")
        self.other_profile = Profile.objects.create(name="Other", target_roles="E")
        self.apps = {}
        for n, (company, channel, status) in enumerate([
            (self.company, "EMAIL", "SHORTLISTED"),
            (self.company, "WHATSAPP", "SENT"),
            (other_company, "OTHER", "SKIPPED"),
        ], start=10):
            job = make_job(n=n, company=company)
            app = Application.objects.create(profile=self.profile, job=job, channel=channel, status=status)
            self.apps[status] = app
        self.other_app = Application.objects.create(
            profile=self.other_profile, job=make_job(n=20, company=other_company), status="FOLLOW_UP"
        )

    def ids(self, query=""):
        response = self.client.get(f"{URL}{query}")
        self.assertEqual(response.status_code, 200, response.data)
        return {a["id"] for a in response.data}

    def test_filters(self):
        a = self.apps
        self.assertEqual(len(self.ids()), 4)
        self.assertEqual(self.ids(f"?profile={self.profile.id}"), {x.id for x in a.values()})
        self.assertEqual(self.ids(f"?job={a['SENT'].job_id}"), {a["SENT"].id})
        self.assertEqual(self.ids(f"?company={self.company.id}"), {a["SHORTLISTED"].id, a["SENT"].id})
        self.assertEqual(self.ids("?status=SENT"), {a["SENT"].id})
        self.assertEqual(self.ids("?status=SENT,SKIPPED"), {a["SENT"].id, a["SKIPPED"].id})
        self.assertEqual(self.ids("?channel=WHATSAPP"), {a["SENT"].id})
        self.assertEqual(self.ids("?awaiting_action=true"), {a["SHORTLISTED"].id, self.other_app.id})
        self.assertEqual(self.ids("?awaiting_action=false"), {a["SENT"].id, a["SKIPPED"].id})

    def test_invalid_filters(self):
        for query in ("?status=NOPE", "?status=SENT,NOPE", "?channel=FAX", "?profile=x",
                      "?company=0", "?awaiting_action=maybe"):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(f"{URL}{query}").status_code, 400)

    def test_list_omits_long_text_and_paginates(self):
        response = self.client.get(URL)
        self.assertNotIn("generated_body", response.data[0])
        self.assertIn("generated_subject", response.data[0])
        page = self.client.get(f"{URL}?page_size=3").data
        self.assertEqual(page["count"], 4)
        self.assertEqual(len(page["results"]), 3)

    def test_ordering_queue_position_nulls_last(self):
        Application.objects.filter(pk=self.apps["SKIPPED"].pk).update(queue_position=1)
        Application.objects.filter(pk=self.apps["SENT"].pk).update(queue_position=2)
        ids = [a["id"] for a in self.client.get(URL).data]
        self.assertEqual(ids[:2], [self.apps["SKIPPED"].id, self.apps["SENT"].id])

    def test_list_query_count(self):
        with self.assertNumQueries(4):  # + the send records (send_state)
            self.client.get(URL)


class SummaryTests(ApplicationAPITestCase):
    def test_summary_counts(self):
        statuses = ["SHORTLISTED", "SHORTLISTED", "READY_TO_SEND", "SENT", "SKIPPED", "FOLLOW_UP"]
        for n, status in enumerate(statuses, start=30):
            Application.objects.create(profile=self.profile, job=make_job(n=n, company=self.company), status=status)
        other = Profile.objects.create(name="O", target_roles="E")
        Application.objects.create(profile=other, job=make_job(n=99), status="FAILED")

        data = self.client.get(f"{URL}summary/").data
        self.assertEqual(data["total"], 7)
        self.assertEqual(data["by_status"]["SHORTLISTED"], 2)
        self.assertEqual(data["by_status"]["FAILED"], 1)
        self.assertEqual(data["by_status"]["GENERATED"], 0)
        self.assertEqual(len(data["by_status"]), 13)
        self.assertEqual(data["awaiting_action"]["count"], 5)  # 2 shortlisted, ready, follow-up, failed
        self.assertEqual(len(data["awaiting_action"]["oldest"]), 5)
        self.assertEqual(len(data["recent"]), 7)
        recent_ids = [a["id"] for a in data["recent"]]
        self.assertEqual(recent_ids, sorted(recent_ids, reverse=True))

        mine = self.client.get(f"{URL}summary/?profile={self.profile.id}").data
        self.assertEqual(mine["total"], 6)
        self.assertEqual(mine["awaiting_action"]["count"], 4)
        self.assertEqual(self.client.get(f"{URL}summary/?profile=x").status_code, 400)

    def test_summary_empty(self):
        data = self.client.get(f"{URL}summary/").data
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["recent"], [])
        self.assertEqual(data["awaiting_action"]["count"], 0)

    def test_recent_limited_to_ten(self):
        for n in range(12):
            Application.objects.create(profile=self.profile, job=make_job(n=100 + n))
        data = self.client.get(f"{URL}summary/").data
        self.assertEqual(data["total"], 12)
        self.assertEqual(len(data["recent"]), 10)
