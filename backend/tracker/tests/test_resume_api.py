"""Resume upload, validation and text-extraction API tests (Milestone 2)."""

import io
import os
import zipfile
from unittest import mock

from tracker.models import Application, JobOpportunity, Profile, Resume

from .helpers import (
    APITestCase,
    make_docx_bytes,
    make_encrypted_pdf_bytes,
    make_pdf_bytes,
    upload,
)

RESUME_FIELDS = {
    "id",
    "profile",
    "name",
    "file_name",
    "file_type",
    "resume_type",
    "is_active",
    "is_default",
    "created_at",
    "updated_at",
}


class ResumeAPITestCase(APITestCase):
    def setUp(self):
        super().setUp()
        self.profile = Profile.objects.create(name="Jane", target_roles="Engineer")

    def post_resume(self, file, **data):
        payload = {"profile": self.profile.id, **data}
        if file is not None:
            payload["file"] = file
        return self.client.post("/api/v1/resumes/", payload, format="multipart")

    def stored_files(self):
        found = []
        for root, _dirs, files in os.walk(self.media_root):
            found.extend(os.path.join(root, f) for f in files)
        return found


class ResumeUploadTests(ResumeAPITestCase):
    def test_upload_pdf(self):
        response = self.post_resume(upload("Jane CV.pdf", make_pdf_bytes()))
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(set(response.data), RESUME_FIELDS)
        self.assertEqual(response.data["name"], "Jane CV")
        self.assertEqual(response.data["file_type"], "pdf")
        self.assertTrue(response.data["file_name"].endswith(".pdf"))
        self.assertTrue(response.data["is_active"])
        self.assertFalse(response.data["is_default"])

        resume = Resume.objects.get()
        self.assertTrue(resume.file_path.name.startswith("resumes/"))
        self.assertTrue(os.path.exists(resume.file_path.path))
        self.assertTrue(resume.file_path.path.startswith(self.media_root))

    def test_upload_docx_with_metadata(self):
        response = self.post_resume(
            upload("cv.DOCX", make_docx_bytes()),
            name="Backend CV",
            resume_type="backend",
            is_default="true",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["name"], "Backend CV")
        self.assertEqual(response.data["file_type"], "docx")
        self.assertEqual(response.data["resume_type"], "backend")
        self.assertTrue(response.data["is_default"])

    def test_response_does_not_expose_storage_path_or_url(self):
        response = self.post_resume(upload("cv.pdf", make_pdf_bytes()))
        body = str(response.data)
        self.assertNotIn("file_path", response.data)
        self.assertNotIn("file", response.data)
        self.assertNotIn("/media/", body)
        self.assertNotIn(self.media_root, body)

    def test_file_required_on_create(self):
        response = self.post_resume(None, name="No file")
        self.assertEqual(response.status_code, 400)
        self.assertIn("file", response.data)
        self.assertFalse(Resume.objects.exists())

    def test_profile_required_and_must_exist(self):
        for profile in ("", 999, "abc"):
            with self.subTest(profile=profile):
                response = self.client.post(
                    "/api/v1/resumes/",
                    {"profile": profile, "file": upload("cv.pdf", make_pdf_bytes())},
                    format="multipart",
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("profile", response.data)
        self.assertFalse(Resume.objects.exists())
        self.assertEqual(self.stored_files(), [])

    def test_unsupported_and_invalid_files_rejected(self):
        corrupt_pdf = b"%PDF-1.4\n" + os.urandom(512)
        zip_without_document = io.BytesIO()
        with zipfile.ZipFile(zip_without_document, "w") as archive:
            archive.writestr("hello.txt", "not a docx")

        cases = {
            "text file": ("cv.txt", b"plain text resume"),
            "legacy doc": ("cv.doc", b"\xd0\xcf\x11\xe0 legacy"),
            "no extension": ("cv", make_pdf_bytes()),
            "pdf extension, not a pdf": ("cv.pdf", b"hello world"),
            "docx extension, not a zip": ("cv.docx", b"hello world"),
            "pdf content renamed to docx": ("cv.docx", make_pdf_bytes()),
            "corrupt pdf": ("cv.pdf", corrupt_pdf),
            "zip that is not a docx": ("cv.docx", zip_without_document.getvalue()),
            "pdf without text": ("cv.pdf", make_pdf_bytes(text="")),
            "docx without text": ("cv.docx", make_docx_bytes(paragraphs=("", "   "))),
            "password-protected pdf": ("cv.pdf", make_encrypted_pdf_bytes()),
            "empty file": ("cv.pdf", b""),
        }
        for label, (name, data) in cases.items():
            with self.subTest(label):
                response = self.post_resume(upload(name, data))
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn("file", response.data)
        self.assertFalse(Resume.objects.exists())
        self.assertEqual(self.stored_files(), [])

    def test_oversized_file_rejected(self):
        with mock.patch("tracker.resume_text.MAX_RESUME_BYTES", 100):
            response = self.post_resume(upload("cv.pdf", make_pdf_bytes()))
        self.assertEqual(response.status_code, 400)
        self.assertIn("too large", str(response.data["file"]))
        self.assertFalse(Resume.objects.exists())

    def test_blank_name_rejected(self):
        response = self.post_resume(upload("cv.pdf", make_pdf_bytes()), name="   ")
        self.assertEqual(response.status_code, 400)
        self.assertIn("name", response.data)
        self.assertFalse(Resume.objects.exists())

        # Omitting the name falls back to the uploaded file name.
        response = self.post_resume(upload("cv.pdf", make_pdf_bytes()))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["name"], "cv")

        response = self.client.patch(
            f"/api/v1/resumes/{response.data['id']}/", {"name": ""}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("name", response.data)


class ResumeTextTests(ResumeAPITestCase):
    def test_pdf_text(self):
        resume_id = self.post_resume(
            upload("cv.pdf", make_pdf_bytes("Jane Doe Python Django"))
        ).data["id"]
        response = self.client.get(f"/api/v1/resumes/{resume_id}/text/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["id"], resume_id)
        self.assertIn("Jane Doe Python Django", response.data["text"])
        self.assertEqual(response.data["characters"], len(response.data["text"]))

    def test_docx_text_includes_paragraphs_and_tables(self):
        data = make_docx_bytes(
            paragraphs=("Jane Doe", "Django developer"),
            table=[["Skill", "Years"], ["Python", "4"]],
        )
        resume_id = self.post_resume(upload("cv.docx", data)).data["id"]
        text = self.client.get(f"/api/v1/resumes/{resume_id}/text/").data["text"]
        self.assertIn("Jane Doe", text)
        self.assertIn("Django developer", text)
        self.assertIn("Python | 4", text)

    def test_text_requires_token(self):
        resume_id = self.post_resume(upload("cv.pdf", make_pdf_bytes())).data["id"]
        self.assertEqual(
            self.anon.get(f"/api/v1/resumes/{resume_id}/text/").status_code, 401
        )

    def test_text_for_unknown_resume_404(self):
        self.assertEqual(self.client.get("/api/v1/resumes/999/text/").status_code, 404)

    def test_text_without_file_returns_409(self):
        resume = Resume.objects.create(name="Legacy", profile=self.profile)
        response = self.client.get(f"/api/v1/resumes/{resume.id}/text/")
        self.assertEqual(response.status_code, 409)
        self.assertIn("no uploaded file", response.data["detail"])

    def test_text_with_missing_stored_file_returns_409(self):
        resume_id = self.post_resume(upload("cv.pdf", make_pdf_bytes())).data["id"]
        os.remove(Resume.objects.get(pk=resume_id).file_path.path)
        response = self.client.get(f"/api/v1/resumes/{resume_id}/text/")
        self.assertEqual(response.status_code, 409)
        self.assertIn("missing", response.data["detail"])

    def test_text_with_corrupted_stored_file_returns_422(self):
        resume_id = self.post_resume(upload("cv.pdf", make_pdf_bytes())).data["id"]
        with open(Resume.objects.get(pk=resume_id).file_path.path, "wb") as fh:
            fh.write(b"%PDF-1.4\ngarbage")
        response = self.client.get(f"/api/v1/resumes/{resume_id}/text/")
        self.assertEqual(response.status_code, 422)
        self.assertIn("could not be read", response.data["detail"])


class ResumeCrudTests(ResumeAPITestCase):
    def create(self, name="cv.pdf", **data):
        response = self.post_resume(upload(name, make_pdf_bytes()), **data)
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def test_metadata_update_keeps_file(self):
        created = self.create()
        before = Resume.objects.get().file_path.name
        response = self.client.patch(
            f"/api/v1/resumes/{created['id']}/",
            {"name": "Renamed", "is_active": False, "resume_type": "general"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        resume = Resume.objects.get()
        self.assertEqual(resume.name, "Renamed")
        self.assertFalse(resume.is_active)
        self.assertEqual(resume.file_path.name, before)

    def test_replace_file_deletes_old_file(self):
        created = self.create()
        old_path = Resume.objects.get().file_path.path
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(
                f"/api/v1/resumes/{created['id']}/",
                {"file": upload("new.docx", make_docx_bytes())},
                format="multipart",
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["file_type"], "docx")
        self.assertFalse(os.path.exists(old_path))
        self.assertTrue(os.path.exists(Resume.objects.get().file_path.path))

    def test_replace_with_invalid_file_keeps_original(self):
        created = self.create()
        resume = Resume.objects.get()
        response = self.client.patch(
            f"/api/v1/resumes/{created['id']}/",
            {"file": upload("bad.txt", b"x")},
            format="multipart",
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(os.path.exists(resume.file_path.path))
        self.assertEqual(Resume.objects.get().file_path.name, resume.file_path.name)

    def test_delete_removes_file_and_unlinks_applications(self):
        created = self.create()
        resume = Resume.objects.get()
        job = JobOpportunity.objects.create(
            title="Engineer", full_jd="Python", jd_fingerprint="fp1"
        )
        application = Application.objects.create(
            profile=self.profile, job=job, resume=resume
        )
        path = resume.file_path.path

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.delete(f"/api/v1/resumes/{created['id']}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Resume.objects.exists())
        self.assertFalse(os.path.exists(path))
        application.refresh_from_db()
        self.assertIsNone(application.resume)

    def test_only_one_default_per_profile(self):
        first = self.create(is_default="true")
        second = self.create(is_default="true")
        self.assertFalse(Resume.objects.get(pk=first["id"]).is_default)
        self.assertTrue(Resume.objects.get(pk=second["id"]).is_default)

        other = Profile.objects.create(name="Other", target_roles="E")
        response = self.client.post(
            "/api/v1/resumes/",
            {"profile": other.id, "file": upload("o.pdf", make_pdf_bytes()), "is_default": "true"},
            format="multipart",
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(Resume.objects.get(pk=second["id"]).is_default)

    def test_filters_and_ordering(self):
        other = Profile.objects.create(name="Other", target_roles="E")
        a = self.create(name="a.pdf")
        b = self.create(name="b.pdf", is_default="true", is_active="false")
        Resume.objects.create(name="other", profile=other)

        ids = lambda r: [x["id"] for x in r.data]  # noqa: E731
        self.assertEqual(
            ids(self.client.get(f"/api/v1/resumes/?profile={self.profile.id}")),
            [b["id"], a["id"]],  # default first
        )
        self.assertEqual(
            ids(self.client.get(f"/api/v1/resumes/?profile={self.profile.id}&is_active=true")),
            [a["id"]],
        )
        for query in ("?is_active=maybe", "?profile=x"):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(f"/api/v1/resumes/{query}").status_code, 400)

    def test_list_paginated_and_query_count(self):
        for i in range(3):
            Resume.objects.create(name=f"r{i}", profile=self.profile)
        with self.assertNumQueries(1):
            response = self.client.get("/api/v1/resumes/")
        self.assertEqual(len(response.data), 3)
        response = self.client.get("/api/v1/resumes/?page_size=2")
        self.assertEqual(response.data["count"], 3)
        self.assertEqual(len(response.data["results"]), 2)
        self.assertIsNone(response.data["results"][0]["file_name"])
