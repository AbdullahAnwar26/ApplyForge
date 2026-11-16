"""Profile, Skill and SkillCategory API tests (Milestone 2)."""

import os

from tracker.models import (
    Application,
    JobOpportunity,
    Profile,
    Resume,
    Skill,
    SkillCategory,
)

from .helpers import APITestCase, make_pdf_bytes, upload

PROFILE_FIELDS = {
    "id",
    "name",
    "professional_summary",
    "target_roles",
    "experience_info",
    "preferred_locations",
    "preferred_work_modes",
    "experience_years",
    "max_acceptable_experience_years",
    "created_at",
    "updated_at",
}


def make_profile(name="Jane", **extra):
    return Profile.objects.create(name=name, target_roles="Backend Engineer", **extra)


class AuthenticationRequiredTests(APITestCase):
    def test_all_endpoints_require_token(self):
        profile = make_profile()
        urls = [
            "/api/v1/profiles/",
            f"/api/v1/profiles/{profile.id}/",
            "/api/v1/skills/",
            "/api/v1/skill-categories/",
            "/api/v1/resumes/",
        ]
        for url in urls:
            with self.subTest(url=url):
                response = self.anon.get(url)
                self.assertEqual(response.status_code, 401)
                self.assertTrue(response["WWW-Authenticate"].startswith("Bearer"))

    def test_writes_require_token(self):
        response = self.anon.post(
            "/api/v1/profiles/", {"name": "X", "target_roles": "Y"}, format="json"
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(Profile.objects.count(), 0)

    def test_wrong_token_rejected(self):
        self.anon.credentials(HTTP_AUTHORIZATION="Bearer wrong")
        self.assertEqual(self.anon.get("/api/v1/profiles/").status_code, 401)


class ProfileAPITests(APITestCase):
    def test_create_with_all_fields(self):
        payload = {
            "name": "Jane Doe",
            "professional_summary": "Backend developer.",
            "target_roles": "Backend Engineer, Django Developer",
            "experience_info": "3 years building APIs",
            "preferred_locations": "Remote, Berlin",
            "preferred_work_modes": "Remote, Hybrid",
            "experience_years": 3.5,
            "max_acceptable_experience_years": 6,
        }
        response = self.client.post("/api/v1/profiles/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(set(response.data), PROFILE_FIELDS)
        for key, value in payload.items():
            self.assertEqual(response.data[key], value)
        profile = Profile.objects.get(pk=response.data["id"])
        self.assertEqual(profile.experience_years, 3.5)

    def test_create_minimal_uses_model_defaults(self):
        response = self.client.post(
            "/api/v1/profiles/",
            {"name": "Jane", "target_roles": "Engineer"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["experience_years"], 1.0)
        self.assertEqual(response.data["max_acceptable_experience_years"], 9)
        self.assertIsNone(response.data["professional_summary"])

    def test_required_fields(self):
        response = self.client.post("/api/v1/profiles/", {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("name", response.data)
        self.assertIn("target_roles", response.data)

    def test_blank_required_fields_rejected(self):
        response = self.client.post(
            "/api/v1/profiles/", {"name": "  ", "target_roles": ""}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("name", response.data)
        self.assertIn("target_roles", response.data)

    def test_negative_and_non_numeric_experience_rejected(self):
        for field, value in [
            ("experience_years", -1),
            ("experience_years", "abc"),
            ("max_acceptable_experience_years", -2),
            ("max_acceptable_experience_years", 2.5),
        ]:
            with self.subTest(field=field, value=value):
                response = self.client.post(
                    "/api/v1/profiles/",
                    {"name": "J", "target_roles": "E", field: value},
                    format="json",
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.data)

    def test_read_only_fields_ignored(self):
        response = self.client.post(
            "/api/v1/profiles/",
            {"id": 999, "name": "J", "target_roles": "E", "created_at": "2000-01-01T00:00:00Z"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertNotEqual(response.data["id"], 999)
        self.assertFalse(response.data["created_at"].startswith("2000"))

    def test_retrieve_update_partial_update(self):
        profile = make_profile()
        url = f"/api/v1/profiles/{profile.id}/"
        self.assertEqual(self.client.get(url).data["name"], "Jane")

        response = self.client.patch(url, {"experience_years": 4}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["experience_years"], 4.0)
        self.assertEqual(response.data["name"], "Jane")

        response = self.client.put(
            url, {"name": "Janet", "target_roles": "SRE"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        profile.refresh_from_db()
        self.assertEqual((profile.name, profile.target_roles), ("Janet", "SRE"))

        response = self.client.put(url, {"name": "Only name"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("target_roles", response.data)

    def test_unknown_profile_returns_404(self):
        self.assertEqual(self.client.get("/api/v1/profiles/999/").status_code, 404)
        self.assertEqual(self.client.delete("/api/v1/profiles/999/").status_code, 404)

    def test_list_plain_and_paginated(self):
        for i in range(3):
            make_profile(name=f"P{i}")
        response = self.client.get("/api/v1/profiles/")
        self.assertIsInstance(response.data, list)
        self.assertEqual([p["name"] for p in response.data], ["P0", "P1", "P2"])

        response = self.client.get("/api/v1/profiles/?page_size=2")
        self.assertEqual(response.data["count"], 3)
        self.assertEqual(len(response.data["results"]), 2)
        self.assertIsNotNone(response.data["next"])

    def test_list_query_count(self):
        for i in range(5):
            make_profile(name=f"P{i}")
        with self.assertNumQueries(1):
            self.client.get("/api/v1/profiles/")

    def test_delete_cascades_skills_and_resume_files(self):
        profile = make_profile()
        Skill.objects.create(name="Python", profile=profile)
        response = self.client.post(
            "/api/v1/resumes/",
            {"profile": profile.id, "file": upload("cv.pdf", make_pdf_bytes())},
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.data)
        stored_path = Resume.objects.get().file_path.path
        self.assertTrue(os.path.exists(stored_path))

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.delete(f"/api/v1/profiles/{profile.id}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Profile.objects.exists())
        self.assertFalse(Skill.objects.exists())
        self.assertFalse(Resume.objects.exists())
        self.assertFalse(os.path.exists(stored_path))

    def test_delete_refused_when_profile_has_applications(self):
        profile = make_profile()
        job = JobOpportunity.objects.create(
            title="Backend Engineer", full_jd="Python role", jd_fingerprint="abc123"
        )
        Application.objects.create(profile=profile, job=job)

        response = self.client.delete(f"/api/v1/profiles/{profile.id}/")
        self.assertEqual(response.status_code, 409)
        self.assertIn("applications", response.data["detail"])
        self.assertTrue(Profile.objects.filter(pk=profile.id).exists())
        self.assertEqual(Application.objects.count(), 1)


class SkillAPITests(APITestCase):
    def setUp(self):
        super().setUp()
        self.profile = make_profile()

    def test_create_with_default_category(self):
        response = self.client.post(
            "/api/v1/skills/", {"name": " Python ", "profile": self.profile.id}, format="json"
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            response.data,
            {
                "id": response.data["id"],
                "name": "Python",
                "category": "SKILL",
                "profile": self.profile.id,
            },
        )

    def test_create_with_each_category(self):
        for value in SkillCategory.values:
            response = self.client.post(
                "/api/v1/skills/",
                {"name": f"k-{value}", "category": value, "profile": self.profile.id},
                format="json",
            )
            self.assertEqual(response.status_code, 201, response.data)
            self.assertEqual(response.data["category"], value)

    def test_required_fields(self):
        response = self.client.post("/api/v1/skills/", {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("name", response.data)
        self.assertIn("profile", response.data)

    def test_invalid_category(self):
        response = self.client.post(
            "/api/v1/skills/",
            {"name": "Python", "category": "LANGUAGE", "profile": self.profile.id},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("category", response.data)

    def test_invalid_profile_relationship(self):
        for value in (999, "abc", None):
            with self.subTest(profile=value):
                response = self.client.post(
                    "/api/v1/skills/", {"name": "Python", "profile": value}, format="json"
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("profile", response.data)
        self.assertFalse(Skill.objects.exists())

    def test_unique_name_per_profile(self):
        Skill.objects.create(name="Python", profile=self.profile)
        response = self.client.post(
            "/api/v1/skills/", {"name": "Python", "profile": self.profile.id}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("non_field_errors", response.data)
        self.assertEqual(Skill.objects.count(), 1)

        other = make_profile(name="Other")
        response = self.client.post(
            "/api/v1/skills/", {"name": "Python", "profile": other.id}, format="json"
        )
        self.assertEqual(response.status_code, 201)

    def test_update_into_duplicate_rejected(self):
        Skill.objects.create(name="Python", profile=self.profile)
        django_skill = Skill.objects.create(name="Django", profile=self.profile)
        response = self.client.patch(
            f"/api/v1/skills/{django_skill.id}/", {"name": "Python"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        django_skill.refresh_from_db()
        self.assertEqual(django_skill.name, "Django")

    def test_update_and_delete(self):
        skill = Skill.objects.create(name="Pythn", profile=self.profile)
        url = f"/api/v1/skills/{skill.id}/"
        response = self.client.patch(
            url, {"name": "Python", "category": "TECHNOLOGY"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        skill.refresh_from_db()
        self.assertEqual((skill.name, skill.category), ("Python", "TECHNOLOGY"))

        self.assertEqual(self.client.delete(url).status_code, 204)
        self.assertFalse(Skill.objects.exists())
        self.assertTrue(Profile.objects.filter(pk=self.profile.id).exists())

    def test_filters(self):
        other = make_profile(name="Other")
        Skill.objects.create(name="Python", profile=self.profile, category="TECHNOLOGY")
        Skill.objects.create(name="Django", profile=self.profile, category="FRAMEWORK")
        Skill.objects.create(name="Go", profile=other, category="TECHNOLOGY")

        names = lambda r: sorted(s["name"] for s in r.data)  # noqa: E731
        self.assertEqual(
            names(self.client.get(f"/api/v1/skills/?profile={self.profile.id}")),
            ["Django", "Python"],
        )
        self.assertEqual(
            names(self.client.get("/api/v1/skills/?category=TECHNOLOGY")), ["Go", "Python"]
        )
        self.assertEqual(
            names(
                self.client.get(
                    f"/api/v1/skills/?profile={self.profile.id}&category=FRAMEWORK"
                )
            ),
            ["Django"],
        )

    def test_invalid_filters_return_400(self):
        for query in ("?profile=abc", "?profile=0", "?category=NOPE"):
            with self.subTest(query=query):
                self.assertEqual(
                    self.client.get(f"/api/v1/skills/{query}").status_code, 400
                )

    def test_pagination_and_query_count(self):
        for i in range(60):
            Skill.objects.create(name=f"s{i:02d}", profile=self.profile)
        with self.assertNumQueries(1):
            response = self.client.get("/api/v1/skills/")
        self.assertEqual(len(response.data), 60)

        with self.assertNumQueries(2):  # COUNT + page
            response = self.client.get("/api/v1/skills/?page=2")
        self.assertEqual(response.data["count"], 60)
        self.assertEqual(len(response.data["results"]), 10)


class SkillCategoryAPITests(APITestCase):
    def test_lists_fixed_choices(self):
        response = self.client.get("/api/v1/skill-categories/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data,
            [{"value": v, "label": l} for v, l in SkillCategory.choices],
        )
        self.assertEqual(
            [c["value"] for c in response.data],
            ["SKILL", "ROLE", "TECHNOLOGY", "FRAMEWORK", "OTHER"],
        )

    def test_read_only(self):
        for method in ("post", "put", "patch", "delete"):
            with self.subTest(method=method):
                response = getattr(self.client, method)(
                    "/api/v1/skill-categories/", {"value": "X"}, format="json"
                )
                self.assertEqual(response.status_code, 405)
