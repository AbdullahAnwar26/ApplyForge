"""User accounts (sign-up, login, logout, password change) and per-user data isolation.

Isolation is tested from the second user's side against every resource: lists are empty,
another account's ids behave exactly like ids that do not exist (404 / "does not exist"),
and nothing can be created that points at another account's rows.
"""

from io import StringIO

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from collector.models import CollectorRun
from tracker.accounts import claim_unowned_data
from tracker.models import (
    Application,
    ApplicationEvent,
    Company,
    Contact,
    JobOpportunity,
    JobStatus,
    Profile,
    Resume,
    Skill,
)

from .helpers import TEST_PASSWORD, make_docx_bytes, upload

User = get_user_model()
AUTH = "/api/v1/auth/"
STRONG = "Correct-Horse-Battery-42"


class AccountTestCase(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()  # throttle counters
        self.client = APIClient()

    def signup(self, client=None, **data):
        body = {"username": "maya", "password": STRONG, **data}
        return (client or self.client).post(f"{AUTH}signup/", body, format="json")

    def login(self, client=None, **data):
        body = {"username": "maya", "password": STRONG, **data}
        return (client or self.client).post(f"{AUTH}login/", body, format="json")


class SignupTests(AccountTestCase):
    def test_signup_creates_the_account_and_signs_in(self):
        response = self.signup(email="maya@example.com")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["user"]["username"], "maya")
        self.assertEqual(response.data["user"]["email"], "maya@example.com")
        self.assertNotIn("password", response.data["user"])
        me = self.client.get(f"{AUTH}me/")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.data["user"]["username"], "maya")
        self.assertTrue(User.objects.get(username="maya").check_password(STRONG))

    def test_weak_passwords_are_rejected_with_reasons(self):
        for password, reason in (("short1!", "too short"), ("password123", "too common"), ("12345678901", "entirely numeric")):
            with self.subTest(password=password):
                response = self.signup(password=password)
                self.assertEqual(response.status_code, 400)
                self.assertIn(reason, " ".join(response.data["password"]))
        response = self.signup(username="mayafictional", password="mayafictional1")
        self.assertEqual(response.status_code, 400)
        self.assertIn("too similar", " ".join(response.data["password"]))
        self.assertFalse(User.objects.exists())

    def test_usernames_are_unique_ignoring_case_and_validated(self):
        self.assertEqual(self.signup().status_code, 201)
        other = APIClient()
        response = self.signup(other, username="MAYA")
        self.assertEqual(response.status_code, 400)
        self.assertIn("already taken", str(response.data["username"]))
        for bad in ("ab", "has space", "semi;colon", "x" * 151):
            with self.subTest(username=bad):
                self.assertEqual(self.signup(APIClient(), username=bad).status_code, 400)

    @override_settings(SIGNUP_ENABLED=False)
    def test_signup_can_be_disabled(self):
        response = self.signup()
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.exists())

    def test_first_account_claims_existing_data_and_later_accounts_do_not(self):
        legacy = Profile.objects.create(name="Legacy", target_roles="Dev")
        job = JobOpportunity.objects.create(title="Old job", full_jd="jd", jd_fingerprint="old")
        response = self.signup()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["claimed"]["Profile"], 1)
        maya = User.objects.get(username="maya")
        legacy.refresh_from_db(); job.refresh_from_db()
        self.assertEqual((legacy.owner, job.owner), (maya, maya))
        self.assertEqual([p["name"] for p in self.client.get("/api/v1/profiles/").data], ["Legacy"])

        Profile.objects.create(name="Created later without owner", target_roles="Dev")
        second = self.signup(APIClient(), username="sam")
        self.assertEqual(second.status_code, 201)
        self.assertNotIn("claimed", second.data)
        self.assertIsNone(Profile.objects.get(name="Created later without owner").owner)


class LoginLogoutTests(AccountTestCase):
    def setUp(self):
        super().setUp()
        User.objects.create_user(username="maya", password=STRONG)

    def test_login_logout_and_me(self):
        self.assertEqual(self.client.get(f"{AUTH}me/").status_code, 401)
        response = self.login()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["user"]["username"], "maya")
        self.assertEqual(self.client.get("/api/v1/profiles/").status_code, 200)
        self.assertEqual(self.client.post(f"{AUTH}logout/").status_code, 204)
        self.assertEqual(self.client.get(f"{AUTH}me/").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/profiles/").status_code, 401)

    def test_username_is_case_insensitive(self):
        self.assertEqual(self.login(username="  MAYA ").status_code, 200)

    def test_wrong_password_and_unknown_user_get_the_same_answer(self):
        wrong = self.login(password="not-it")
        unknown = self.login(username="nobody")
        self.assertEqual((wrong.status_code, unknown.status_code), (400, 400))
        self.assertEqual(wrong.data, unknown.data)
        self.assertEqual(wrong.data["detail"], "Incorrect username or password.")
        self.assertEqual(self.client.get(f"{AUTH}me/").status_code, 401)

    def test_inactive_account_cannot_sign_in(self):
        User.objects.filter(username="maya").update(is_active=False)
        self.assertEqual(self.login().status_code, 400)

    def test_remember_me_controls_the_session_lifetime(self):
        self.login(remember=True)
        self.assertFalse(self.client.session.get_expire_at_browser_close())
        other = APIClient()
        self.login(other, remember=False)
        self.assertTrue(other.session.get_expire_at_browser_close())

    def test_login_is_throttled(self):
        codes = [self.login(password=f"guess-{i}").status_code for i in range(11)]
        self.assertEqual(codes[:10], [400] * 10)
        self.assertEqual(codes[10], 429)


class CsrfTests(AccountTestCase):
    """A browser-like client: CSRF is enforced for login and for every write when signed in."""

    def setUp(self):
        super().setUp()
        User.objects.create_user(username="maya", password=STRONG)
        self.client = APIClient(enforce_csrf_checks=True)

    def csrf(self):
        self.assertEqual(self.client.get(f"{AUTH}csrf/").status_code, 200)
        return self.client.cookies["csrftoken"].value

    def test_login_without_csrf_token_is_refused(self):
        response = self.login()
        self.assertEqual(response.status_code, 403)
        self.assertIn("CSRF", str(response.data))
        self.assertEqual(self.client.get(f"{AUTH}me/").status_code, 401)

    def test_login_and_writes_with_csrf_token(self):
        token = self.csrf()
        response = self.client.post(f"{AUTH}login/", {"username": "maya", "password": STRONG}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        token = self.client.cookies["csrftoken"].value  # rotated at login
        no_header = self.client.post("/api/v1/profiles/", {"name": "P", "target_roles": "R"}, format="json")
        self.assertEqual(no_header.status_code, 403)
        ok = self.client.post("/api/v1/profiles/", {"name": "P", "target_roles": "R"}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(ok.status_code, 201)


class PasswordChangeTests(AccountTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="maya", password=STRONG)
        self.client.force_login(self.user)

    def change(self, current, new):
        return self.client.post(f"{AUTH}password/", {"current_password": current, "new_password": new}, format="json")

    def test_wrong_current_password(self):
        response = self.change("wrong", "Another-Strong-Pass-77")
        self.assertEqual(response.status_code, 400)
        self.assertIn("current_password", response.data)

    def test_weak_new_password(self):
        response = self.change(STRONG, "123")
        self.assertEqual(response.status_code, 400)
        self.assertIn("new_password", response.data)

    def test_change_keeps_this_session_and_the_new_password_works(self):
        self.assertEqual(self.change(STRONG, "Another-Strong-Pass-77").status_code, 204)
        self.assertEqual(self.client.get(f"{AUTH}me/").status_code, 200)  # still signed in here
        fresh = APIClient()
        self.assertEqual(self.login(fresh, password=STRONG).status_code, 400)
        self.assertEqual(self.login(fresh, password="Another-Strong-Pass-77").status_code, 200)

    def test_requires_sign_in(self):
        self.assertEqual(APIClient().post(f"{AUTH}password/", {}, format="json").status_code, 401)


class IsolationTests(TestCase):
    """Maya's data, seen from Sam's account."""

    def setUp(self):
        cache.clear()
        self.maya = User.objects.create_user(username="maya", password=TEST_PASSWORD)
        self.sam = User.objects.create_user(username="sam", password=TEST_PASSWORD)
        self.maya_client = APIClient(); self.maya_client.force_login(self.maya)
        self.sam_client = APIClient(); self.sam_client.force_login(self.sam)

        self.profile = Profile.objects.create(owner=self.maya, name="Maya", target_roles="Python Developer")
        self.skill = Skill.objects.create(profile=self.profile, name="Python")
        self.company = Company.objects.create(owner=self.maya, name="Acme Fictional Labs")
        self.contact = Contact.objects.create(owner=self.maya, name="Recruiter", email="hr@acme.example", company=self.company)
        self.job = JobOpportunity.objects.create(
            owner=self.maya, title="Python Developer", full_jd="Python and Django", jd_fingerprint="fp-maya",
            source_url="https://jobs.example/1", company=self.company, status=JobStatus.SHORTLISTED,
        )
        self.job.contacts.add(self.contact)
        self.resume = Resume.objects.create(profile=self.profile, name="CV", is_default=True)
        self.application = Application.objects.create(profile=self.profile, job=self.job, contact=self.contact)
        ApplicationEvent.objects.create(application=self.application, status="SHORTLISTED", notes="created")
        self.run = CollectorRun.objects.create(owner=self.maya, trigger="api", source="file", profile=self.profile)

    def test_lists_are_empty_for_another_account(self):
        for url in ("/api/v1/profiles/", "/api/v1/skills/", "/api/v1/resumes/", "/api/v1/jobs/", "/api/v1/companies/",
                    "/api/v1/contacts/", "/api/v1/applications/", "/api/v1/collector/runs/"):
            with self.subTest(url=url):
                response = self.sam_client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, [])
                self.assertEqual(len(self.maya_client.get(url).data), 1)
        summary = self.sam_client.get("/api/v1/applications/summary/").data
        self.assertEqual(summary["total"], 0)
        self.assertEqual(self.sam_client.get("/api/v1/jobs/", {"search": "python"}).data, [])

    def test_another_accounts_ids_are_not_found(self):
        a, p, j = self.application.id, self.profile.id, self.job.id
        cases = [
            ("get", f"/api/v1/profiles/{p}/", None), ("patch", f"/api/v1/profiles/{p}/", {"name": "x"}),
            ("delete", f"/api/v1/profiles/{p}/", None), ("get", f"/api/v1/skills/{self.skill.id}/", None),
            ("delete", f"/api/v1/skills/{self.skill.id}/", None), ("get", f"/api/v1/resumes/{self.resume.id}/", None),
            ("get", f"/api/v1/resumes/{self.resume.id}/text/", None), ("delete", f"/api/v1/resumes/{self.resume.id}/", None),
            ("get", f"/api/v1/jobs/{j}/", None), ("patch", f"/api/v1/jobs/{j}/", {"status": "SKIPPED"}),
            ("post", f"/api/v1/jobs/{j}/screen/", {}), ("get", f"/api/v1/companies/{self.company.id}/", None),
            ("get", f"/api/v1/contacts/{self.contact.id}/", None), ("get", f"/api/v1/applications/{a}/", None),
            ("patch", f"/api/v1/applications/{a}/", {"status": "SKIPPED"}), ("get", f"/api/v1/applications/{a}/events/", None),
            ("post", f"/api/v1/applications/{a}/notes/", {"note": "x"}), ("post", f"/api/v1/applications/{a}/generate/", {}),
            ("get", f"/api/v1/collector/runs/{self.run.id}/", None),
        ]
        for method, url, body in cases:
            with self.subTest(method=method, url=url):
                response = getattr(self.sam_client, method)(url, body, format="json") if body is not None else getattr(self.sam_client, method)(url)
                self.assertEqual(response.status_code, 404, getattr(response, "data", None))
        # Nothing of Maya's changed.
        self.job.refresh_from_db(); self.application.refresh_from_db(); self.profile.refresh_from_db()
        self.assertEqual((self.job.status, self.application.status, self.profile.name), ("SHORTLISTED", "SHORTLISTED", "Maya"))
        self.assertEqual(self.application.history.count(), 1)

    def test_cannot_create_anything_that_points_at_another_account(self):
        sam_profile = self.sam_client.post("/api/v1/profiles/", {"name": "Sam", "target_roles": "Dev"}, format="json").data["id"]
        self.assertEqual(Profile.objects.get(pk=sam_profile).owner, self.sam)
        attempts = [
            ("/api/v1/skills/", {"name": "Go", "profile": self.profile.id}, "profile"),
            ("/api/v1/applications/", {"job": self.job.id, "profile": sam_profile}, "job"),
            ("/api/v1/applications/", {"job": self.job.id, "profile": self.profile.id}, "profile"),
            ("/api/v1/jobs/ingest/", {"title": "T", "full_jd": "Go", "profile": self.profile.id}, "profile"),
            ("/api/v1/jobs/screen/", {"profile": self.profile.id}, "profile"),
            ("/api/v1/collector/runs/", {"profile": self.profile.id}, "profile"),
        ]
        for url, body, field in attempts:
            with self.subTest(url=url, field=field):
                response = self.sam_client.post(url, body, format="json")
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn(field, response.data)
        upload_response = self.sam_client.post(
            "/api/v1/resumes/", {"profile": self.profile.id, "file": upload("cv.docx", make_docx_bytes())}, format="multipart"
        )
        self.assertEqual(upload_response.status_code, 400)
        self.assertIn("profile", upload_response.data)
        self.assertEqual(Skill.objects.filter(profile=self.profile).count(), 1)
        self.assertEqual(Application.objects.count(), 1)

    def test_cannot_attach_another_accounts_contact_or_resume(self):
        Skill.objects.create(profile=Profile.objects.create(owner=self.sam, name="Sam", target_roles="Dev"), name="Python")
        sam_job = JobOpportunity.objects.create(owner=self.sam, title="J", full_jd="Python", jd_fingerprint="fp-sam", status=JobStatus.SHORTLISTED)
        app = self.sam_client.post("/api/v1/applications/", {"job": sam_job.id}, format="json")
        self.assertEqual(app.status_code, 201, app.data)
        for field, value in (("contact", self.contact.id), ("resume", self.resume.id)):
            with self.subTest(field=field):
                response = self.sam_client.patch(f"/api/v1/applications/{app.data['id']}/", {field: value}, format="json")
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.data)

    def test_the_same_posting_can_be_saved_by_both_accounts(self):
        Skill.objects.create(profile=Profile.objects.create(owner=self.sam, name="Sam", target_roles="Dev"), name="Python")
        body = {"title": "Python Developer", "full_jd": "Python and Django", "source_url": "https://jobs.example/1",
                "company_name": "Acme Fictional Labs", "contacts": [{"email": "hr@acme.example"}]}
        first = self.sam_client.post("/api/v1/jobs/ingest/", body, format="json")
        self.assertEqual(first.status_code, 201, first.data)  # not matched to Maya's identical job
        again = self.sam_client.post("/api/v1/jobs/ingest/", body, format="json")
        self.assertEqual((again.status_code, again.data["matched_by"]), (200, "source_url"))
        sam_job = JobOpportunity.objects.get(pk=first.data["job"]["id"])
        self.assertEqual(sam_job.owner, self.sam)
        self.assertNotEqual(sam_job.company_id, self.company.id)  # Sam's own company and contact rows
        self.assertEqual(sam_job.contacts.get().owner, self.sam)
        self.assertEqual(sam_job.screening_details["duplicate_candidates"], [])  # Maya's job is not a candidate
        self.assertEqual(JobOpportunity.objects.filter(owner=self.maya).count(), 1)


class ClaimCommandTests(TestCase):
    def test_dry_run_then_claim(self):
        user = User.objects.create_user(username="maya", password=TEST_PASSWORD)
        Profile.objects.create(name="Legacy", target_roles="Dev")
        out = StringIO()
        call_command("claim_unowned_data", "MAYA", "--dry-run", stdout=out)
        self.assertIn("Would claim for maya: 1 Profile", out.getvalue())
        self.assertIsNone(Profile.objects.get().owner)
        call_command("claim_unowned_data", "maya", stdout=StringIO())
        self.assertEqual(Profile.objects.get().owner, user)
        self.assertEqual(claim_unowned_data(user)["Profile"], 0)  # nothing left
