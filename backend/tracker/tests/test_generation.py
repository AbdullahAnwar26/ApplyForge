"""Application generation tests (Milestone 5). Ollama is simulated; nothing leaves the machine."""

import io
import json
import socket
import urllib.error
from unittest import mock

from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.test import SimpleTestCase

from tracker.generation import (
    SYSTEM_PROMPT,
    GenerationConfig,
    GenerationFailed,
    build_messages,
    chat_completion,
    parse_generated_content,
)
from tracker.models import (
    SendingAccount,
    Application,
    ApplicationEvent,
    Company,
    Contact,
    JobOpportunity,
    JobStatus,
    Profile,
    Resume,
    ScreeningOutcome,
    Skill,
)

from .helpers import APITestCase, make_pdf_bytes, upload

URLOPEN = "tracker.generation.urllib.request.urlopen"
# Neutral sentences (no job words, no technologies) that bring a test email over the minimum
# length (generation.MIN_EMAIL_WORDS) without changing what the test checks.
FILLER = " ".join(["I take care to communicate clearly and to keep every commitment I make to colleagues."] * 10)
GOOD = {"subject": "Application: Backend Engineer",
        "body": f"Dear Ms Smith,\n\nI am Jane Doe...\n\n{FILLER}\n\nJane Doe",
        "whatsapp_message": "Hi Ms Smith, I'm Jane Doe - interested in the Backend Engineer role."}


def config(**overrides):
    values = dict(base_url="http://localhost:11434/v1", model="qwen3:1.7b", temperature=0.3,
                  max_tokens=768, timeout=180, max_jd_chars=6000, max_resume_chars=6000)
    values.update(overrides)
    return GenerationConfig(**values)


def ollama_reply(content):
    """A fake urlopen() context manager returning an OpenAI-style completion."""
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": content}}]}
    ).encode()
    return response


class SettingsTests(SimpleTestCase):
    def test_historical_qwen_defaults(self):
        self.assertEqual(settings.OLLAMA_BASE_URL, "http://localhost:11434/v1")
        self.assertEqual(settings.OLLAMA_MODEL, "qwen3:1.7b")
        self.assertEqual(settings.OLLAMA_TEMPERATURE, 0.3)
        self.assertEqual(settings.OLLAMA_MAX_TOKENS, 1500)  # raised for full-length emails
        self.assertEqual(settings.OLLAMA_TIMEOUT_SECONDS, 180)


class ClientTests(SimpleTestCase):
    def test_request_payload(self):
        with mock.patch(URLOPEN, return_value=ollama_reply("hello")) as urlopen:
            self.assertEqual(chat_completion([{"role": "user", "content": "x"}], config()), "hello")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:11434/v1/chat/completions")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 180)
        payload = json.loads(request.data)
        self.assertEqual(payload, {
            "model": "qwen3:1.7b", "messages": [{"role": "user", "content": "x"}],
            "temperature": 0.3, "max_tokens": 768, "stream": False, "think": False,
        })

    def test_only_local_base_urls(self):
        for url in ("http://127.0.0.1:11434/v1", "http://[::1]:11434/v1", "http://LOCALHOST:11434/v1"):
            with self.subTest(url=url), mock.patch(URLOPEN, return_value=ollama_reply("ok")):
                self.assertEqual(chat_completion([], config(base_url=url)), "ok")
        for url in ("https://api.openai.com/v1", "http://192.168.1.5:11434/v1", "http://ollama.example.com/v1"):
            with self.subTest(url=url), mock.patch(URLOPEN) as urlopen:
                with self.assertRaises(GenerationFailed) as ctx:
                    chat_completion([], config(base_url=url))
                self.assertEqual(ctx.exception.http_status, 503)
                urlopen.assert_not_called()

    def test_error_mapping(self):
        http_404 = urllib.error.HTTPError(
            "u", 404, "Not Found", {}, io.BytesIO(b'{"error":"model \'qwen3:1.7b\' not found"}')
        )
        cases = [
            (urllib.error.URLError(ConnectionRefusedError("refused")), 503, "Cannot reach Ollama"),
            (urllib.error.URLError(socket.timeout("timed out")), 504, "within 180 seconds"),
            (TimeoutError(), 504, "within 180 seconds"),
            (http_404, 502, "not found"),
        ]
        for error, status, text in cases:
            with self.subTest(error=type(error).__name__), mock.patch(URLOPEN, side_effect=error):
                with self.assertRaises(GenerationFailed) as ctx:
                    chat_completion([], config())
                self.assertEqual(ctx.exception.http_status, status)
                self.assertIn(text, ctx.exception.message)

    def test_bad_response_bodies(self):
        for raw in (b"not json", b"{}", b'{"choices": []}',
                    json.dumps({"choices": [{"message": {"content": "   "}}]}).encode()):
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = raw
            with self.subTest(raw=raw), mock.patch(URLOPEN, return_value=response):
                with self.assertRaises(GenerationFailed):
                    chat_completion([], config())


class ParsingTests(SimpleTestCase):
    def test_plain_fenced_think_and_prose(self):
        raw = json.dumps(GOOD)
        for text in (raw, f"```json\n{raw}\n```", f"<think>\nplanning...\n</think>\n{raw}",
                     f"Here is the JSON:\n{raw}\nHope it helps."):
            with self.subTest(text=text[:20]):
                content = parse_generated_content(text)
                self.assertEqual(content.subject, GOOD["subject"])
                self.assertEqual(content.body, GOOD["body"])
                self.assertEqual(content.whatsapp_message, GOOD["whatsapp_message"])

    def test_whatsapp_falls_back_to_body(self):
        content = parse_generated_content(json.dumps({"subject": "S", "body": "B"}))
        self.assertEqual(content.whatsapp_message, "B")

    def test_subject_normalized_and_capped(self):
        content = parse_generated_content(json.dumps({"subject": "A\n  B " + "x" * 600, "body": "B"}))
        self.assertTrue(content.subject.startswith("A B x"))
        self.assertEqual(len(content.subject), 500)

    def test_invalid_outputs(self):
        for text in ("Dear hiring team, ...", "[1, 2]", json.dumps({"subject": "", "body": "B"}),
                     json.dumps({"subject": "S"}), json.dumps({"subject": 5, "body": "B"}),
                     json.dumps({"subject": "S", "body": "Regards, [Your Name]"}),
                     json.dumps({"subject": "Role at [Company Name]", "body": "B"})):
            with self.subTest(text=text):
                with self.assertRaises(GenerationFailed):
                    parse_generated_content(text)


class GenerationAPITestCase(APITestCase):
    JD = ("Backend Engineer at Acme. We need Python and Django, PostgreSQL and REST APIs. "
          "2-4 years of experience. Kubernetes is a plus.")

    def setUp(self):
        super().setUp()
        cache.clear()  # throttle counters
        self.profile = Profile.objects.create(
            name="Jane Doe", target_roles="Backend Engineer", experience_years=3,
            professional_summary="Backend developer focused on Django.",
        )
        Skill.objects.create(name="Python", profile=self.profile)
        Skill.objects.create(name="Django", profile=self.profile)
        self.resume = Resume.objects.create(
            name="CV", profile=self.profile, is_default=True,
            file_path=upload("cv.pdf", make_pdf_bytes("Jane Doe Django developer at Initech 2021-2024")),
        )
        self.contact = Contact.objects.create(name="Ms Smith", email="smith@acme.com")
        self.job = JobOpportunity.objects.create(
            title="Backend Engineer", full_jd=self.JD, jd_fingerprint="fp",
            company=Company.objects.create(name="Acme"), status=JobStatus.SHORTLISTED,
            screening_outcome=ScreeningOutcome.SHORTLISTED,
        )
        self.job.contacts.add(self.contact)
        self.app = Application.objects.create(profile=self.profile, job=self.job, contact=self.contact)
        ApplicationEvent.objects.create(application=self.app, status="SHORTLISTED", notes="Application created.")
        self.url = f"/api/v1/applications/{self.app.id}/generate/"

    def generate(self, reply=None, error=None):
        kwargs = {"side_effect": error} if error else {"return_value": ollama_reply(reply or json.dumps(GOOD))}
        with mock.patch(URLOPEN, **kwargs) as urlopen:
            response = self.client.post(self.url, {}, format="json")
        return response, urlopen

    def reload(self):
        return Application.objects.get(pk=self.app.pk)


class FullEmailTests(GenerationAPITestCase):
    """A real, job-specific email: matching skills and contact in the prompt, ATS rules, and
    a short draft is retried once (with the reason), never padded or silently accepted."""

    SHORT = {"subject": "Application: Backend Engineer - Jane Doe", "body": "Dear Ms Smith,\nBackend Engineer at Acme.\nJane Doe",
             "whatsapp_message": ""}

    def prompt(self, urlopen, call=0):
        return json.loads(urlopen.call_args_list[call].args[0].data)["messages"]

    def test_prompt_has_matching_skills_contact_and_ats_rules(self):
        SendingAccount.objects.create(owner=self.user, from_email="jane@example.org", smtp_host="smtp.example.org",
                                      username="jane@example.org")
        _, urlopen = self.generate()
        system, user = (m["content"] for m in self.prompt(urlopen))
        self.assertIn("<matching_skills>\nDjango (also in the resume), Python (profile only)\n</matching_skills>", user)
        self.assertIn("<candidate_contact>\nEmail: jane@example.org\n</candidate_contact>", user)
        self.assertIn("applicant tracking systems (ATS)", system)
        self.assertIn("170-260 words", system)
        self.assertIn("no clichés", system)
        self.assertIn("Sound like a real person wrote it", system)
        self.assertIn("\"leverage\"", system)

    def test_short_draft_is_retried_once_with_the_reason(self):
        replies = [ollama_reply(json.dumps(self.SHORT)), ollama_reply(json.dumps(GOOD))]
        with mock.patch(URLOPEN, side_effect=replies) as urlopen:
            response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(urlopen.call_count, 2)
        retry = self.prompt(urlopen, 1)
        self.assertEqual(retry[-2]["role"], "assistant")
        self.assertIn("170-260 words", retry[-1]["content"])
        self.assertEqual(self.reload().generated_body, GOOD["body"])

    def test_short_twice_saves_nothing(self):
        replies = [ollama_reply(json.dumps(self.SHORT)), ollama_reply(json.dumps(self.SHORT))]
        with mock.patch(URLOPEN, side_effect=replies):
            response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 502)
        self.assertIn("only", response.data["detail"])
        self.assertIsNone(self.reload().generated_body)  # no padded or template email


class GenerateSuccessTests(GenerationAPITestCase):
    def test_success(self):
        response, urlopen = self.generate()
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data
        self.assertEqual(data["model"], "qwen3:1.7b")
        self.assertEqual(data["truncated"], {"job_description": False, "resume": False})
        app = data["application"]
        self.assertEqual(app["status"], "GENERATED")
        self.assertEqual(app["generated_subject"], GOOD["subject"])
        self.assertEqual(app["generated_body"], GOOD["body"])
        # The recipient has no phone number: no WhatsApp draft is kept.
        self.assertEqual(app["generated_whatsapp_message"], "")
        self.assertIsNotNone(app["generated_at"])
        self.assertIsNone(app["last_error"])
        self.assertIsNone(app["sent_at"])
        self.assertEqual(app["resume"], self.resume.id)  # default resume recorded
        urlopen.assert_called_once()

        events = ApplicationEvent.objects.filter(application=self.app).order_by("-id")
        self.assertEqual(events[0].status, "GENERATED")
        self.assertEqual(events[0].notes, "Content generated with qwen3:1.7b.")
        self.assertEqual(Application.objects.count(), 1)

    def test_prompt_uses_exact_jd_profile_and_resume(self):
        _, urlopen = self.generate()
        payload = json.loads(urlopen.call_args.args[0].data)
        system, user = payload["messages"][0]["content"], payload["messages"][1]["content"]
        self.assertEqual(system, SYSTEM_PROMPT)
        self.assertIn("Never invent", system)
        self.assertIn(f"<job_description>\n{self.JD}\n</job_description>", user)
        self.assertIn("Name: Jane Doe", user)
        self.assertIn("Skills: Django, Python", user)
        self.assertIn("Jane Doe Django developer at Initech 2021-2024", user)
        self.assertIn("Company: Acme", user)
        self.assertIn("Name: Ms Smith", user)

    def test_generation_does_not_touch_job(self):
        self.generate()
        job = JobOpportunity.objects.get(pk=self.job.pk)
        self.assertEqual(job.status, JobStatus.SHORTLISTED)
        self.assertEqual(job.screening_outcome, ScreeningOutcome.SHORTLISTED)

    def test_regenerate_replaces_drafts_without_extra_event(self):
        self.generate()
        events_before = ApplicationEvent.objects.count()
        response, _ = self.generate(json.dumps({**GOOD, "subject": "Second draft: Backend Engineer"}))
        self.assertEqual(response.data["application"]["generated_subject"], "Second draft: Backend Engineer")
        self.assertEqual(ApplicationEvent.objects.count(), events_before)  # GENERATED -> GENERATED

    def test_regenerate_from_reviewed_state_returns_to_generated(self):
        for status in ("OPENED", "READY_TO_SEND"):
            with self.subTest(status=status):
                Application.objects.filter(pk=self.app.pk).update(status=status)
                response, _ = self.generate()
                self.assertEqual(response.data["application"]["status"], "GENERATED")

    def test_success_clears_previous_error(self):
        Application.objects.filter(pk=self.app.pk).update(last_error="old failure")
        self.generate()
        self.assertIsNone(self.reload().last_error)

    def test_explicit_resume_is_used(self):
        other = Resume.objects.create(
            name="Alt", profile=self.profile,
            file_path=upload("alt.pdf", make_pdf_bytes("Alternative resume Flask experience")),
        )
        Application.objects.filter(pk=self.app.pk).update(resume=other)
        _, urlopen = self.generate()
        user = json.loads(urlopen.call_args.args[0].data)["messages"][1]["content"]
        self.assertIn("Alternative resume Flask experience", user)
        self.assertNotIn("Initech", user)

    def test_truncation_reported(self):
        with self.settings(GENERATION_MAX_JD_CHARS=20, GENERATION_MAX_RESUME_CHARS=10):
            response, urlopen = self.generate()
        self.assertEqual(response.data["truncated"], {"job_description": True, "resume": True})
        user = json.loads(urlopen.call_args.args[0].data)["messages"][1]["content"]
        self.assertIn("[...truncated]", user)


class GenerateFailureTests(GenerationAPITestCase):
    def assert_failure_preserves_state(self, response, status_code, status="SHORTLISTED",
                                       subject=None, events=1):
        self.assertEqual(response.status_code, status_code, response.data)
        self.assertEqual(response.data["application"], self.app.id)
        app = self.reload()
        self.assertEqual(app.status, status)
        self.assertEqual(app.generated_subject, subject)
        self.assertTrue(app.last_error)
        self.assertEqual(app.last_error, response.data["detail"])
        self.assertEqual(Application.objects.count(), 1)
        self.assertEqual(ApplicationEvent.objects.filter(application=self.app).count(), events)

    def test_ollama_unreachable(self):
        response, _ = self.generate(error=urllib.error.URLError(ConnectionRefusedError()))
        self.assert_failure_preserves_state(response, 503)
        self.assertIn("Cannot reach Ollama", response.data["detail"])

    def test_timeout(self):
        response, _ = self.generate(error=TimeoutError())
        self.assert_failure_preserves_state(response, 504)

    def test_invalid_model_output(self):
        response, _ = self.generate(reply="I'm sorry, I can't help with that.")
        self.assert_failure_preserves_state(response, 502)

    def test_placeholder_output_rejected(self):
        response, _ = self.generate(reply=json.dumps({**GOOD, "body": "Thanks, [Your Name]"}))
        self.assert_failure_preserves_state(response, 502)

    def test_failure_preserves_previous_drafts_and_status(self):
        self.generate()
        Application.objects.filter(pk=self.app.pk).update(status="READY_TO_SEND")
        response, _ = self.generate(error=TimeoutError())
        self.assert_failure_preserves_state(
            response, 504, status="READY_TO_SEND", subject=GOOD["subject"], events=2
        )
        self.assertEqual(self.reload().generated_body, GOOD["body"])

    def test_unreadable_resume_file(self):
        with open(self.resume.file_path.path, "wb") as fh:
            fh.write(b"%PDF-1.4 garbage")
        response, urlopen = self.generate()
        self.assert_failure_preserves_state(response, 422)
        urlopen.assert_not_called()

    def test_status_changed_during_generation_discards_result(self):
        def mark_sent_then_reply(*args, **kwargs):
            Application.objects.filter(pk=self.app.pk).update(status="SENT")
            return ollama_reply(json.dumps(GOOD))

        with mock.patch(URLOPEN, side_effect=mark_sent_then_reply):
            response = self.client.post(self.url, {}, format="json")
        self.assert_failure_preserves_state(response, 409, status="SENT")


class GenerateInputTests(GenerationAPITestCase):
    def assert_rejected_without_change(self, field):
        response, urlopen = self.generate()
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn(field, response.data)
        urlopen.assert_not_called()
        self.assertIsNone(self.reload().last_error)

    def test_no_resume(self):
        Resume.objects.all().delete()
        self.assert_rejected_without_change("resume")

    def test_resume_without_file(self):
        Resume.objects.filter(pk=self.resume.pk).update(file_path=None)
        self.assert_rejected_without_change("resume")

    def test_statuses_that_cannot_generate(self):
        for status in ("SENT", "FOLLOW_UP", "REJECTED", "SKIPPED"):
            with self.subTest(status=status):
                Application.objects.filter(pk=self.app.pk).update(status=status)
                self.assert_rejected_without_change("status")

    def test_body_rejected_auth_and_404(self):
        response = self.client.post(self.url, {"model": "gpt-4"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.anon.post(self.url, {}, format="json").status_code, 401)
        self.assertEqual(self.client.post("/api/v1/applications/999/generate/", {}, format="json").status_code, 404)
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_throttled(self):
        with mock.patch(URLOPEN, return_value=ollama_reply(json.dumps(GOOD))):
            codes = [self.client.post(self.url, {}, format="json").status_code for _ in range(21)]
        self.assertEqual(codes[:20], [200] * 20)
        self.assertEqual(codes[20], 429)


class NoSendingAndDraftEditTests(GenerationAPITestCase):
    def test_generation_sends_nothing(self):
        with mock.patch("smtplib.SMTP") as smtp, mock.patch("smtplib.SMTP_SSL") as smtp_ssl:
            response, urlopen = self.generate()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mail.outbox, [])
        smtp.assert_not_called()
        smtp_ssl.assert_not_called()
        # The only network call is the local Ollama request.
        self.assertEqual(urlopen.call_count, 1)
        self.assertTrue(urlopen.call_args.args[0].full_url.startswith("http://localhost:11434/"))
        self.assertIsNone(self.reload().sent_at)

    def test_user_can_edit_drafts_before_sending(self):
        self.generate()
        events = ApplicationEvent.objects.count()
        response = self.client.patch(
            f"/api/v1/applications/{self.app.id}/",
            {"generated_subject": "Edited", "generated_body": "Edited body\n"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["generated_subject"], "Edited")
        self.assertEqual(response.data["generated_body"], "Edited body\n")
        self.assertEqual(response.data["status"], "GENERATED")
        self.assertEqual(ApplicationEvent.objects.count(), events)

    def test_drafts_locked_after_sent(self):
        self.generate()
        self.client.patch(f"/api/v1/applications/{self.app.id}/", {"status": "SENT"}, format="json")
        response = self.client.patch(
            f"/api/v1/applications/{self.app.id}/", {"generated_body": "rewrite history"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.reload().generated_body, GOOD["body"])

    def test_subject_length_validated(self):
        response = self.client.patch(
            f"/api/v1/applications/{self.app.id}/", {"generated_subject": "x" * 501}, format="json"
        )
        self.assertEqual(response.status_code, 400)


class BuildMessagesUnitTests(GenerationAPITestCase):
    def test_unknown_recipient_and_missing_fields(self):
        self.app.contact = None
        messages, jd_cut, resume_cut = build_messages(self.app, "resume text", config())
        user = messages[1]["content"]
        self.assertIn("greet with 'Hello,'", user)
        self.assertIn("Location: (not provided)", user)
        self.assertFalse(jd_cut or resume_cut)

class JobSpecificGenerationTests(GenerationAPITestCase):
    """Job Automation phase 3: JD + profile + resume together, nothing generic or invented."""

    def test_whatsapp_draft_only_with_a_number(self):
        self.contact.whatsapp = "+91 98765 43210"
        self.contact.save()
        response, urlopen = self.generate()
        self.assertEqual(response.data["application"]["generated_whatsapp_message"], GOOD["whatsapp_message"])
        user = json.loads(urlopen.call_args.args[0].data)["messages"][1]["content"]
        self.assertIn("WhatsApp available: yes", user)

    def test_missing_whatsapp_message_is_a_failure_when_a_number_exists(self):
        self.contact.phone = "+91 98765 43210"
        self.contact.save()
        response, _ = self.generate(json.dumps({**GOOD, "whatsapp_message": ""}))
        self.assertEqual(response.status_code, 502)
        self.assertIn("WhatsApp", response.data["detail"])
        self.assertEqual(self.reload().status, "SHORTLISTED")  # no silent fallback

    def test_overlong_whatsapp_message_is_rejected(self):
        self.contact.whatsapp = "+91 98765 43210"
        self.contact.save()
        response, _ = self.generate(json.dumps({**GOOD, "whatsapp_message": "Backend Engineer " * 100}))
        self.assertEqual(response.status_code, 502)
        self.assertIn("too long", response.data["detail"])

    def test_generic_draft_is_rejected(self):
        generic = {"subject": "Job application", "body": f"Dear Hiring Team,\n\nPlease consider me. {FILLER}\n\nJane Doe",
                   "whatsapp_message": ""}
        response, _ = self.generate(json.dumps(generic))
        self.assertEqual(response.status_code, 502)
        self.assertIn("too generic", response.data["detail"])
        self.assertIn("too generic", self.reload().last_error)
        self.assertIsNone(self.reload().generated_body)

    def test_company_name_counts_as_job_specific(self):
        reply = {"subject": "Application to Acme", "body": f"Dear Ms Smith,\nI would like to join Acme. {FILLER}\nJane Doe"}
        response, _ = self.generate(json.dumps(reply))
        self.assertEqual(response.status_code, 200, response.data)

    def test_attachment_is_stated_from_the_selected_resume(self):
        _, urlopen = self.generate()
        user = json.loads(urlopen.call_args.args[0].data)["messages"][1]["content"]
        self.assertIn("<attachment>\nA resume is attached: CV\n</attachment>", user)
        system = json.loads(urlopen.call_args.args[0].data)["messages"][0]["content"]
        self.assertIn("ONLY if <attachment> says a resume is attached", system)

    def test_gaps_and_unsupported_terms_are_recorded(self):
        reply = {**GOOD, "body": "Dear Ms Smith,\nI have built Backend Engineer systems with Django and Kubernetes "
                                 f"on AWS. {FILLER}\nJane Doe", "gaps": ["Kubernetes", "  PostgreSQL  ", 42]}
        response, _ = self.generate(json.dumps(reply))
        self.assertEqual(response.status_code, 200, response.data)
        notes = response.data["application"]["generation_notes"]
        self.assertEqual(notes["gaps"], ["Kubernetes", "PostgreSQL"])
        # Django is in the profile; Kubernetes and AWS are in neither the profile nor the resume.
        self.assertEqual(notes["unsupported_terms"], ["aws", "kubernetes"])
        self.assertEqual(notes["resume"], self.resume.pk)
        self.assertEqual(notes["resume_name"], "CV")

    def test_incomplete_job_description_is_refused_before_calling_the_model(self):
        JobOpportunity.objects.filter(pk=self.job.pk).update(jd_complete=False)
        response, urlopen = self.generate()
        self.assertEqual(response.status_code, 400)
        self.assertIn("incomplete", str(response.data["job"]))
        urlopen.assert_not_called()

    def test_inactive_resume_is_refused(self):
        Resume.objects.filter(pk=self.resume.pk).update(is_active=False)
        Application.objects.filter(pk=self.app.pk).update(resume=self.resume)
        response, urlopen = self.generate()
        self.assertEqual(response.status_code, 400)
        urlopen.assert_not_called()

    def test_recruiter_name_from_the_post_is_used_for_the_greeting(self):
        self.contact.name = ""
        self.contact.save()
        JobOpportunity.objects.filter(pk=self.job.pk).update(recruiter_name="Riya Fictional")
        _, urlopen = self.generate()
        user = json.loads(urlopen.call_args.args[0].data)["messages"][1]["content"]
        self.assertIn("<recipient>\nName: Riya Fictional", user)

    def test_each_application_gets_its_own_drafts(self):
        other_job = JobOpportunity.objects.create(
            title="Data Engineer", full_jd="Data Engineer at Globex. Python and Django pipelines, 3 years.",
            jd_fingerprint="fp2", company=Company.objects.create(name="Globex"), status=JobStatus.SHORTLISTED)
        other_job.contacts.add(self.contact)
        other = Application.objects.create(profile=self.profile, job=other_job, contact=self.contact)
        self.generate()
        reply = {"subject": "Data Engineer application", "body": f"Dear Ms Smith,\nGlobex data work. {FILLER}\nJane Doe"}
        with mock.patch(URLOPEN, return_value=ollama_reply(json.dumps(reply))) as urlopen:
            self.client.post(f"/api/v1/applications/{other.id}/generate/", {}, format="json")
        user = json.loads(urlopen.call_args.args[0].data)["messages"][1]["content"]
        self.assertIn("Data Engineer at Globex", user)
        self.assertNotIn(self.JD, user)
        self.assertEqual(self.reload().generated_subject, GOOD["subject"])
        self.assertEqual(Application.objects.get(pk=other.pk).generated_subject, "Data Engineer application")
