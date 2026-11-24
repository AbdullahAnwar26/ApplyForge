"""Post-send lifecycle (Interviewing, Offer), follow-up reminders, notes and job search.

Added after the local end-to-end review (2026-11-21): applications ended at "Sent" with no way
to record an interview or offer, follow-ups had no date, notes needed a status change, and the
jobs list had no text search.
"""

from datetime import date, timedelta
from unittest import mock

from django.core import mail
from django.test import override_settings
from django.utils import timezone

from tracker.models import Application, ApplicationEvent, Company, JobOpportunity, JobStatus

from .test_applications_api import URL, ApplicationAPITestCase, make_job


class LifecycleTestCase(ApplicationAPITestCase):
    def sent_app(self):
        app_id = self.create().data["id"]
        self.assertEqual(self.patch(app_id, status="SENT").status_code, 200)
        return app_id


class InterviewOfferTests(LifecycleTestCase):
    def test_interview_and_offer_require_sending(self):
        app_id = self.create().data["id"]
        for status in ("INTERVIEW", "OFFER"):
            with self.subTest(status=status):
                response = self.patch(app_id, status=status)
                self.assertEqual(response.status_code, 400)
                self.assertIn("only allowed after the application was sent", str(response.data))
        self.assertEqual(Application.objects.get(pk=app_id).status, "SHORTLISTED")

    def test_full_post_send_path_records_history(self):
        app_id = self.sent_app()
        for status, note in (("FOLLOW_UP", "Chased"), ("INTERVIEW", "Phone screen Tuesday"), ("OFFER", "Offer received")):
            response = self.patch(app_id, status=status, note=note)
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data["status"], status)
        statuses = [e["status"] for e in self.events(app_id)]
        self.assertEqual(statuses, ["OFFER", "INTERVIEW", "FOLLOW_UP", "SENT", "SHORTLISTED"])
        self.assertEqual(self.events(app_id)[0]["notes"], "Offer received")

    def test_rejection_after_interview_allowed(self):
        app_id = self.sent_app()
        self.patch(app_id, status="INTERVIEW")
        self.assertEqual(self.patch(app_id, status="REJECTED").status_code, 200)

    def test_cannot_return_to_pre_send_status_after_interview(self):
        app_id = self.sent_app()
        self.patch(app_id, status="INTERVIEW")
        self.assertEqual(self.patch(app_id, status="READY_TO_SEND").status_code, 400)

    def test_interview_and_offer_wait_for_the_user(self):
        app_id = self.sent_app()
        self.patch(app_id, status="INTERVIEW")
        waiting = self.client.get(URL, {"awaiting_action": "true"}).data
        self.assertEqual([a["id"] for a in waiting], [app_id])
        summary = self.client.get(f"{URL}summary/").data
        self.assertIn("INTERVIEW", summary["awaiting_action"]["statuses"])
        self.assertEqual(summary["by_status"]["INTERVIEW"], 1)

    def test_drafts_stay_locked_after_interview(self):
        app_id = self.sent_app()
        self.patch(app_id, status="INTERVIEW")
        self.assertEqual(self.patch(app_id, generated_subject="x").status_code, 400)

    def test_nothing_is_sent(self):
        app_id = self.sent_app()
        with mock.patch("urllib.request.urlopen") as urlopen:
            self.patch(app_id, status="INTERVIEW")
            self.patch(app_id, status="OFFER")
        urlopen.assert_not_called()
        self.assertEqual(len(mail.outbox), 0)


class FollowUpReminderTests(LifecycleTestCase):
    @override_settings(FOLLOW_UP_DEFAULT_DAYS=7)
    def test_marking_sent_sets_the_default_reminder(self):
        app_id = self.sent_app()
        expected = timezone.localdate() + timedelta(days=7)
        self.assertEqual(Application.objects.get(pk=app_id).follow_up_on, expected)
        self.assertEqual(self.client.get(f"{URL}{app_id}/").data["follow_up_on"], expected.isoformat())

    def test_existing_reminder_is_kept_when_sent(self):
        app_id = self.create().data["id"]
        self.assertEqual(self.patch(app_id, follow_up_on="2030-01-15").status_code, 200)
        self.patch(app_id, status="SENT")
        self.assertEqual(Application.objects.get(pk=app_id).follow_up_on, date(2030, 1, 15))

    def test_user_can_change_and_clear_the_reminder_without_an_event(self):
        app_id = self.sent_app()
        before = ApplicationEvent.objects.filter(application_id=app_id).count()
        response = self.patch(app_id, follow_up_on="2031-03-02")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["follow_up_on"], "2031-03-02")
        response = self.patch(app_id, follow_up_on=None)
        self.assertIsNone(response.data["follow_up_on"])
        self.assertEqual(ApplicationEvent.objects.filter(application_id=app_id).count(), before)

    def test_invalid_reminder_date_rejected(self):
        app_id = self.create().data["id"]
        response = self.patch(app_id, follow_up_on="next week")
        self.assertEqual(response.status_code, 400)
        self.assertIn("follow_up_on", response.data)

    def test_rejection_clears_the_reminder(self):
        app_id = self.sent_app()
        self.patch(app_id, status="REJECTED")
        self.assertIsNone(Application.objects.get(pk=app_id).follow_up_on)

    def test_due_reminders_in_list_filter_and_summary(self):
        today = timezone.localdate()
        due_id = self.sent_app()
        Application.objects.filter(pk=due_id).update(follow_up_on=today - timedelta(days=1))
        later_job = make_job(2, company=self.company)
        later_id = self.client.post(URL, {"job": later_job.id}, format="json").data["id"]
        self.patch(later_id, status="SENT")
        Application.objects.filter(pk=later_id).update(follow_up_on=today + timedelta(days=3))
        # A due date on an application that is no longer open does not count.
        closed_job = make_job(3, company=self.company)
        closed_id = self.client.post(URL, {"job": closed_job.id}, format="json").data["id"]
        self.patch(closed_id, status="SKIPPED")
        Application.objects.filter(pk=closed_id).update(follow_up_on=today - timedelta(days=5))

        due = self.client.get(URL, {"follow_up_due": "true"}).data
        self.assertEqual([a["id"] for a in due], [due_id])
        summary = self.client.get(f"{URL}summary/").data
        self.assertEqual(summary["follow_ups_due"]["count"], 1)
        self.assertEqual([a["id"] for a in summary["follow_ups_due"]["items"]], [due_id])
        self.assertEqual(summary["sent_total"], 2)

    def test_client_day_decides_what_is_due(self):
        # Regression (local E2E, 2026-11-22 23:49 PST = 2026-11-23 UTC): the server's UTC day differed
        # from the user's day. The browser passes its own calendar day.
        app_id = self.sent_app()
        Application.objects.filter(pk=app_id).update(follow_up_on=date(2026, 11, 23))
        for today, expected in (("2026-11-22", []), ("2026-11-23", [app_id]), ("2026-11-24", [app_id])):
            with self.subTest(today=today):
                due = self.client.get(URL, {"follow_up_due": "true", "today": today}).data
                self.assertEqual([a["id"] for a in due], expected)
                summary = self.client.get(f"{URL}summary/", {"today": today}).data
                self.assertEqual(summary["follow_ups_due"]["count"], len(expected))

    def test_invalid_client_day_rejected(self):
        for path in (URL, f"{URL}summary/"):
            with self.subTest(path=path):
                response = self.client.get(path, {"follow_up_due": "true", "today": "23/11/2026"})
                self.assertEqual(response.status_code, 400)
                self.assertIn("today", response.data)

    def test_reminder_sent_with_the_status_change_is_kept(self):
        # The UI sends the user's own "today + 7" together with SENT.
        app_id = self.create().data["id"]
        response = self.patch(app_id, status="SENT", follow_up_on="2026-11-29")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["follow_up_on"], "2026-11-29")

    def test_sent_total_keeps_counting_after_later_statuses(self):
        app_id = self.sent_app()
        self.patch(app_id, status="INTERVIEW")
        self.assertEqual(self.client.get(f"{URL}summary/").data["sent_total"], 1)


class NoteTests(LifecycleTestCase):
    def test_note_is_recorded_without_changing_status(self):
        app_id = self.create().data["id"]
        response = self.client.post(f"{URL}{app_id}/notes/", {"note": "Recruiter said the team is hiring two people."}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["kind"], "NOTE")
        self.assertEqual(response.data["status"], "SHORTLISTED")
        self.assertEqual(Application.objects.get(pk=app_id).status, "SHORTLISTED")
        history = self.events(app_id)
        self.assertEqual([(e["kind"], e["notes"]) for e in history][0], ("NOTE", "Recruiter said the team is hiring two people."))
        self.assertEqual(history[-1]["kind"], "STATUS")

    def test_empty_or_missing_note_rejected(self):
        app_id = self.create().data["id"]
        for body in ({}, {"note": ""}, {"note": "   "}):
            with self.subTest(body=body):
                self.assertEqual(self.client.post(f"{URL}{app_id}/notes/", body, format="json").status_code, 400)
        self.assertEqual(ApplicationEvent.objects.filter(application_id=app_id, kind="NOTE").count(), 0)

    def test_note_requires_token_and_existing_application(self):
        app_id = self.create().data["id"]
        self.assertEqual(self.anon.post(f"{URL}{app_id}/notes/", {"note": "x"}, format="json").status_code, 401)
        self.assertEqual(self.client.post(f"{URL}99999/notes/", {"note": "x"}, format="json").status_code, 404)

    def test_notes_allowed_after_sending(self):
        app_id = self.sent_app()
        self.assertEqual(self.client.post(f"{URL}{app_id}/notes/", {"note": "Interview prep: system design"}, format="json").status_code, 201)


class JobSearchTests(ApplicationAPITestCase):
    def setUp(self):
        super().setUp()
        globex = Company.objects.create(name="Globex Example")
        JobOpportunity.objects.create(title="Data Engineer", full_jd="Spark and Airflow pipelines", jd_fingerprint="s1",
                                      company=globex, location="Pune")
        JobOpportunity.objects.create(title="Frontend Developer", full_jd="React and TypeScript", jd_fingerprint="s2",
                                      location="Remote")

    def titles(self, search):
        response = self.client.get("/api/v1/jobs/", {"search": search})
        self.assertEqual(response.status_code, 200, response.data)
        return sorted(j["title"] for j in response.data)

    def test_search_matches_title_company_location_and_description(self):
        self.assertEqual(self.titles("data"), ["Data Engineer"])
        self.assertEqual(self.titles("globex"), ["Data Engineer"])
        self.assertEqual(self.titles("remote"), ["Frontend Developer"])
        self.assertEqual(self.titles("typescript"), ["Frontend Developer"])

    def test_every_word_must_match(self):
        self.assertEqual(self.titles("react pune"), [])
        self.assertEqual(self.titles("spark pune"), ["Data Engineer"])

    def test_blank_search_returns_everything_and_long_search_rejected(self):
        self.assertEqual(len(self.titles("  ")), JobOpportunity.objects.count())
        self.assertEqual(self.client.get("/api/v1/jobs/", {"search": "x" * 201}).status_code, 400)

    def test_search_combines_with_filters(self):
        JobOpportunity.objects.filter(title="Data Engineer").update(status=JobStatus.SKIPPED)
        response = self.client.get("/api/v1/jobs/", {"search": "engineer", "status": "SKIPPED"})
        self.assertEqual([j["title"] for j in response.data], ["Data Engineer"])
