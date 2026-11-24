"""Job ingestion, screening, company and contact API tests (Milestone 3)."""

from unittest import mock

from tracker.ingestion import find_existing_job, jd_fingerprint
from tracker.models import (
    Company,
    Contact,
    JobOpportunity,
    JobStatus,
    Profile,
    ScreeningOutcome,
    Skill,
)

from .helpers import APITestCase

INGEST_URL = "/api/v1/jobs/ingest/"


def payload(**overrides):
    data = {
        "title": "Backend Engineer",
        "full_jd": "We need a Python/Django developer with 2-4 years of experience.",
        "source": "LINKEDIN",
        "source_url": "https://www.linkedin.com/jobs/view/backend-engineer-at-acme-111/?trk=x",
        "company_name": "Acme Corp",
        "location": "Remote",
        "contacts": [{"name": "Hiring Manager", "email": "HR@Acme.com", "phone": "+1 (555) 123-4567"}],
    }
    data.update(overrides)
    return data


class JobAPITestCase(APITestCase):
    def setUp(self):
        super().setUp()
        self.profile = Profile.objects.create(
            name="Jane", target_roles="Backend", experience_years=3, max_acceptable_experience_years=6
        )
        Skill.objects.create(name="Python", profile=self.profile)
        Skill.objects.create(name="Django", profile=self.profile)

    def ingest(self, **overrides):
        return self.client.post(INGEST_URL, payload(**overrides), format="json")


class IngestTests(JobAPITestCase):
    def test_ingest_creates_and_screens(self):
        response = self.ingest()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["created"])
        self.assertIsNone(response.data["matched_by"])
        job = response.data["job"]
        self.assertEqual(job["source_url"], "https://www.linkedin.com/jobs/view/111/")
        self.assertEqual(job["company"]["name"], "Acme Corp")
        self.assertEqual(job["screening_outcome"], "SHORTLISTED")
        self.assertEqual(job["screening_reason"], "KEYWORD_MATCH")
        self.assertEqual(job["matched_keywords"], ["Django", "Python"])
        self.assertEqual(job["status"], "DISCOVERED")
        self.assertEqual(job["screening_details"]["profile_id"], self.profile.id)
        self.assertEqual(job["parsed_min_experience"], 2.0)
        self.assertEqual(len(job["contacts"]), 1)
        self.assertNotIn("jd_fingerprint", job)
        self.assertNotIn("normalized_title", job)

        stored = JobOpportunity.objects.get()
        self.assertEqual(stored.jd_fingerprint, jd_fingerprint(payload()["full_jd"]))

    def test_same_url_is_idempotent_and_does_not_overwrite(self):
        first = self.ingest().data["job"]
        response = self.ingest(
            source_url="https://linkedin.com/jobs/view/111?refId=abc",
            title="Changed title",
            full_jd="Completely different description with Python 9+ years experience.",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["created"])
        self.assertEqual(response.data["matched_by"], "source_url")
        self.assertEqual(response.data["job"]["id"], first["id"])
        self.assertEqual(response.data["job"]["title"], "Backend Engineer")
        self.assertEqual(JobOpportunity.objects.count(), 1)
        self.assertEqual(Company.objects.count(), 1)
        self.assertEqual(Contact.objects.count(), 1)

    def test_raw_legacy_url_matches(self):
        legacy = "https://example.com/jobs/1?utm_source=feed"
        JobOpportunity.objects.create(
            title="Legacy", full_jd="legacy jd", jd_fingerprint="legacy", source_url=legacy
        )
        response = self.ingest(source_url=legacy)
        self.assertEqual(response.data["matched_by"], "source_url")
        self.assertEqual(JobOpportunity.objects.count(), 1)

    def test_same_jd_different_url_matches_fingerprint(self):
        first = self.ingest().data["job"]
        response = self.ingest(
            source_url="https://jobs.example.com/acme/backend",
            full_jd="  WE NEED a python/django developer   with 2-4 years of experience. ",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["matched_by"], "jd_fingerprint")
        self.assertEqual(response.data["job"]["id"], first["id"])
        self.assertEqual(JobOpportunity.objects.count(), 1)

    def test_repeated_ingest_without_url(self):
        self.ingest(source_url=None)
        response = self.ingest(source_url="")
        self.assertEqual(response.data["matched_by"], "jd_fingerprint")
        self.assertEqual(JobOpportunity.objects.count(), 1)

    def test_company_and_contact_reuse(self):
        self.ingest()
        response = self.ingest(
            source_url="https://jobs.example.com/2",
            title="Senior Backend Engineer",
            full_jd="Another Python role, 3+ years of experience.",
            company_name="  acme   corp ",
            contacts=[{"email": "hr@acme.com"}, {"phone": "15551234567"}, {"whatsapp": "+44 7000 000000"}],
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Company.objects.count(), 1)
        # email and phone reuse the first contact; whatsapp creates a new one
        self.assertEqual(Contact.objects.count(), 2)
        self.assertEqual(len(response.data["job"]["contacts"]), 2)
        original = Contact.objects.order_by("id").first()
        self.assertEqual(original.name, "Hiring Manager")  # not overwritten

    def test_possible_duplicate_by_title_and_company(self):
        first = self.ingest().data["job"]
        response = self.ingest(
            source_url="https://jobs.example.com/acme-backend",
            title="backend  ENGINEER",
            full_jd="Python developer for the platform team, 3 years of experience.",
        )
        self.assertEqual(response.status_code, 201)
        job = response.data["job"]
        self.assertEqual(job["screening_outcome"], "NEEDS_REVIEW")
        self.assertEqual(job["screening_reason"], "POSSIBLE_DUPLICATE")
        self.assertEqual(
            job["screening_details"]["duplicate_candidates"][0],
            {"job_id": first["id"], "matched_on": ["title", "company", "contact"]},
        )
        self.assertEqual(JobOpportunity.objects.count(), 2)  # never blocked or rejected

    def test_screening_outcomes_through_ingest(self):
        cases = [
            ("Rust developer, 2 years of experience.", "REJECTED", "NO_KEYWORD_MATCH"),
            ("Python lead, 10+ years of experience.", "REJECTED", "EXPERIENCE_ABOVE_LIMIT"),
            ("Python developer. Competitive salary.", "NEEDS_REVIEW", "EXPERIENCE_UNKNOWN"),
            ("Python developer, 5 years of experience.", "SHORTLISTED", "KEYWORD_MATCH"),
        ]
        for i, (jd, outcome, reason) in enumerate(cases):
            with self.subTest(reason=reason):
                response = self.ingest(
                    title=f"Role {i}", full_jd=jd, source_url=None, company_name=f"Co {i}", contacts=[]
                )
                self.assertEqual(response.status_code, 201, response.data)
                self.assertEqual(response.data["job"]["screening_outcome"], outcome)
                self.assertEqual(response.data["job"]["screening_reason"], reason)

    def test_experience_field_used(self):
        response = self.ingest(required_experience_text="8+ years")
        self.assertEqual(response.data["job"]["screening_reason"], "EXPERIENCE_ABOVE_LIMIT")
        self.assertEqual(response.data["job"]["required_experience_text"], "8+ years")

    def test_validation_errors(self):
        cases = [
            ({"title": ""}, "title"),
            ({"title": "   "}, "title"),
            ({"full_jd": ""}, "full_jd"),
            ({"source": "MONSTER"}, "source"),
            ({"source_url": "not a url"}, "source_url"),
            ({"source_url": "ftp://example.com/job"}, "source_url"),
            ({"contacts": [{"name": "Only a name"}]}, "contacts"),
            ({"contacts": [{"email": "not-an-email"}]}, "contacts"),
            ({"contacts": [{"phone": "123"}]}, "contacts"),
            ({"posted_date": "yesterday"}, "posted_date"),
            ({"title": "x" * 256}, "title"),
        ]
        for overrides, field in cases:
            with self.subTest(field=field, overrides=overrides):
                response = self.ingest(**overrides)
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.data)
        missing = self.client.post(INGEST_URL, {}, format="json")
        self.assertEqual(missing.status_code, 400)
        self.assertIn("title", missing.data)
        self.assertIn("full_jd", missing.data)
        self.assertEqual(JobOpportunity.objects.count(), 0)

    def test_unique_conflict_race_returns_existing(self):
        existing = self.ingest().data["job"]
        real = find_existing_job
        calls = []

        def racing_lookup(urls, fingerprint, owner):
            calls.append(1)
            if len(calls) == 1:
                return None, None  # simulate: the other request committed after our check
            return real(urls, fingerprint, owner)

        with mock.patch("tracker.ingestion.find_existing_job", side_effect=racing_lookup):
            response = self.ingest()
        self.assertEqual(response.status_code, 200, response.data)
        # The second lookup only happens in the IntegrityError handler.
        self.assertEqual(len(calls), 2)
        self.assertFalse(response.data["created"])
        self.assertEqual(response.data["job"]["id"], existing["id"])
        self.assertEqual(JobOpportunity.objects.count(), 1)
        self.assertEqual(Company.objects.count(), 1)
        self.assertEqual(Contact.objects.count(), 1)


class ProfileSelectionTests(JobAPITestCase):
    def test_no_profile(self):
        Profile.objects.all().delete()
        response = self.ingest()
        self.assertEqual(response.status_code, 400)
        self.assertIn("No profile exists", str(response.data["profile"]))
        self.assertEqual(JobOpportunity.objects.count(), 0)

    def test_profile_without_skills(self):
        Skill.objects.all().delete()
        response = self.ingest()
        self.assertEqual(response.status_code, 400)
        self.assertIn("no skills", str(response.data["profile"]))
        self.assertEqual(JobOpportunity.objects.count(), 0)

    def test_multiple_profiles_require_explicit_id(self):
        other = Profile.objects.create(name="Other", target_roles="E")
        Skill.objects.create(name="Rust", profile=other)
        response = self.ingest()
        self.assertEqual(response.status_code, 400)
        self.assertIn("Multiple profiles", str(response.data["profile"]))

        response = self.ingest(profile=other.id)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["job"]["screening_details"]["profile_id"], other.id)
        self.assertEqual(response.data["job"]["screening_reason"], "NO_KEYWORD_MATCH")

    def test_unknown_profile_id(self):
        response = self.ingest(profile=999)
        self.assertEqual(response.status_code, 400)
        self.assertIn("profile", response.data)


class JobEndpointTests(JobAPITestCase):
    def make_jobs(self):
        ids = {}
        for key, jd in {
            "short": "Python developer, 3 years of experience.",
            "reject": "Rust developer, 3 years of experience.",
            "review": "Django developer. Great team.",
        }.items():
            response = self.ingest(title=key, full_jd=jd, source_url=None, company_name=key, contacts=[])
            ids[key] = response.data["job"]["id"]
        return ids

    def test_endpoints_require_token(self):
        job_id = self.make_jobs()["short"]
        for method, url in [
            ("get", "/api/v1/jobs/"),
            ("get", f"/api/v1/jobs/{job_id}/"),
            ("patch", f"/api/v1/jobs/{job_id}/"),
            ("post", INGEST_URL),
            ("post", f"/api/v1/jobs/{job_id}/screen/"),
            ("post", "/api/v1/jobs/screen/"),
            ("get", "/api/v1/companies/"),
            ("get", "/api/v1/contacts/"),
        ]:
            with self.subTest(method=method, url=url):
                response = getattr(self.anon, method)(url, {}, format="json")
                self.assertEqual(response.status_code, 401)

    def test_list_excludes_full_jd_and_filters(self):
        ids = self.make_jobs()
        response = self.client.get("/api/v1/jobs/")
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.data, list)
        self.assertEqual(len(response.data), 3)
        self.assertNotIn("full_jd", response.data[0])
        self.assertNotIn("screening_details", response.data[0])

        def ids_for(query):
            return {j["id"] for j in self.client.get(f"/api/v1/jobs/{query}").data}

        self.assertEqual(ids_for("?screening_outcome=SHORTLISTED"), {ids["short"]})
        self.assertEqual(ids_for("?screening_outcome=REJECTED"), {ids["reject"]})
        self.assertEqual(ids_for("?screening_reason=EXPERIENCE_UNKNOWN"), {ids["review"]})
        self.assertEqual(ids_for(f"?profile={self.profile.id}"), set(ids.values()))
        self.assertEqual(ids_for("?profile=999"), set())
        self.assertEqual(ids_for("?status=DISCOVERED"), set(ids.values()))
        self.assertEqual(ids_for("?source=LINKEDIN"), set(ids.values()))
        company_id = Company.objects.get(name="short").id
        self.assertEqual(ids_for(f"?company={company_id}"), {ids["short"]})

        unscreened = JobOpportunity.objects.create(title="u", full_jd="u", jd_fingerprint="u")
        self.assertEqual(ids_for("?screening_outcome=UNSCREENED"), {unscreened.id})

    def test_invalid_filters_400(self):
        for query in ("?status=NOPE", "?screening_outcome=MAYBE", "?screening_reason=X",
                      "?source=X", "?company=abc", "?profile=0"):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(f"/api/v1/jobs/{query}").status_code, 400)

    def test_pagination_and_query_count(self):
        for i in range(7):
            self.ingest(title=f"Job {i}", full_jd=f"Python job {i}, 2 years of experience.",
                        source_url=None, company_name=f"Co {i}", contacts=[])
        # session + user, jobs+company, prefetch keywords/contacts/applications, resumes, profiles.
        # Constant: does not grow with the number of jobs.
        with self.assertNumQueries(8):
            response = self.client.get("/api/v1/jobs/")
        self.assertEqual(len(response.data), 7)
        response = self.client.get("/api/v1/jobs/?page_size=5&page=2")
        self.assertEqual(response.data["count"], 7)
        self.assertEqual(len(response.data["results"]), 2)

    def test_detail(self):
        job_id = self.make_jobs()["short"]
        response = self.client.get(f"/api/v1/jobs/{job_id}/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("full_jd", response.data)
        self.assertIn("screening_details", response.data)
        self.assertEqual(self.client.get("/api/v1/jobs/999/").status_code, 404)

    def test_patch_status_and_review_fields_only(self):
        job_id = self.make_jobs()["review"]
        url = f"/api/v1/jobs/{job_id}/"
        response = self.client.patch(url, {"status": "SHORTLISTED"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "SHORTLISTED")
        self.assertEqual(response.data["screening_outcome"], "NEEDS_REVIEW")  # verdict untouched

        for body in ({"status": "APPLIED", "screening_outcome": "SHORTLISTED"}, {"jd_fingerprint": "x"},
                     {"source_url": "https://example.com/x"}, {"status": "BOGUS"}, {"title": ""}, {}):
            with self.subTest(body=body):
                self.assertEqual(self.client.patch(url, body, format="json").status_code, 400)
        job = JobOpportunity.objects.get(pk=job_id)
        self.assertEqual((job.title, job.status), ("review", "SHORTLISTED"))

    def test_put_and_delete_not_allowed(self):
        job_id = self.make_jobs()["short"]
        self.assertEqual(self.client.put(f"/api/v1/jobs/{job_id}/", {}, format="json").status_code, 405)
        self.assertEqual(self.client.delete(f"/api/v1/jobs/{job_id}/").status_code, 405)
        self.assertEqual(self.client.post("/api/v1/jobs/", {}, format="json").status_code, 405)


class ScreenEndpointTests(JobAPITestCase):
    def test_rescreen_one_after_skill_change_keeps_status(self):
        job_id = self.ingest(full_jd="Kotlin developer, 2 years of experience.").data["job"]["id"]
        JobOpportunity.objects.filter(pk=job_id).update(status=JobStatus.SKIPPED)
        self.assertEqual(
            JobOpportunity.objects.get(pk=job_id).screening_outcome, ScreeningOutcome.REJECTED
        )
        Skill.objects.create(name="Kotlin", profile=self.profile)

        response = self.client.post(f"/api/v1/jobs/{job_id}/screen/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["screening_outcome"], "SHORTLISTED")
        self.assertEqual(response.data["matched_keywords"], ["Kotlin"])
        self.assertEqual(response.data["status"], "SKIPPED")

    def test_rescreen_unknown_job_404(self):
        self.assertEqual(self.client.post("/api/v1/jobs/999/screen/", {}, format="json").status_code, 404)

    def test_rescreen_with_other_profile(self):
        job_id = self.ingest().data["job"]["id"]
        other = Profile.objects.create(name="Other", target_roles="E")
        Skill.objects.create(name="Rust", profile=other)
        response = self.client.post(
            f"/api/v1/jobs/{job_id}/screen/", {"profile": other.id}, format="json"
        )
        self.assertEqual(response.data["screening_reason"], "NO_KEYWORD_MATCH")
        self.assertEqual(response.data["screening_details"]["profile_id"], other.id)
        # two profiles now exist, so omitting profile is an error
        response = self.client.post(f"/api/v1/jobs/{job_id}/screen/", {}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_screen_all_and_subset(self):
        a = self.ingest(title="A", full_jd="Python, 2 years of experience.", source_url=None, contacts=[]).data["job"]["id"]
        b = self.ingest(title="B", full_jd="Rust, 2 years of experience.", source_url=None, contacts=[]).data["job"]["id"]
        unscreened = JobOpportunity.objects.create(title="C", full_jd="Django role", jd_fingerprint="c")

        response = self.client.post("/api/v1/jobs/screen/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data,
            {"profile": self.profile.id, "screened": 3,
             "outcomes": {"SHORTLISTED": 1, "REJECTED": 1, "NEEDS_REVIEW": 1}},
        )
        unscreened.refresh_from_db()
        self.assertEqual(unscreened.screening_reason, "EXPERIENCE_UNKNOWN")

        response = self.client.post("/api/v1/jobs/screen/", {"jobs": [a]}, format="json")
        self.assertEqual(response.data["screened"], 1)

        for body in ({"jobs": [a, 999]}, {"jobs": []}, {"jobs": ["x"]}):
            with self.subTest(body=body):
                self.assertEqual(self.client.post("/api/v1/jobs/screen/", body, format="json").status_code, 400)
        self.assertTrue(JobOpportunity.objects.filter(pk=b).exists())


class CompanyContactEndpointTests(JobAPITestCase):
    def test_read_only_lists(self):
        self.ingest()
        companies = self.client.get("/api/v1/companies/")
        self.assertEqual(companies.status_code, 200)
        self.assertEqual(companies.data[0]["name"], "Acme Corp")
        self.assertNotIn("normalized_name", companies.data[0])

        contacts = self.client.get("/api/v1/contacts/")
        self.assertEqual(contacts.data[0]["email"], "HR@Acme.com")
        self.assertNotIn("normalized_email", contacts.data[0])
        company_id = companies.data[0]["id"]
        self.assertEqual(len(self.client.get(f"/api/v1/contacts/?company={company_id}").data), 1)
        self.assertEqual(len(self.client.get("/api/v1/contacts/?company=999").data), 0)
        self.assertEqual(self.client.get("/api/v1/contacts/?company=abc").status_code, 400)

        for url in ("/api/v1/companies/", "/api/v1/contacts/",
                    f"/api/v1/companies/{company_id}/"):
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url, {"name": "x"}, format="json").status_code, 405)
                self.assertEqual(self.client.delete(url).status_code, 405)

    def test_company_pagination(self):
        for i in range(3):
            Company.objects.create(name=f"C{i}")
        response = self.client.get("/api/v1/companies/?page_size=2")
        self.assertEqual(response.data["count"], 3)
        self.assertEqual(len(response.data["results"]), 2)
