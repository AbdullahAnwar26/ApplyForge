"""Post parsing tests (pure functions, no database)."""

import json
from datetime import datetime, timezone
from pathlib import Path

from django.test import SimpleTestCase

from collector.posts import (
    PostParseError,
    PostSkipped,
    RawPost,
    build_contacts,
    build_payload,
    extract_company,
    extract_emails,
    extract_experience_text,
    extract_location,
    extract_phones,
    extract_title,
    extract_work_mode,
    is_hiring_post,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_posts.json"


class HiringCueTests(SimpleTestCase):
    def test_hiring_posts(self):
        for text in (
            "We're hiring a backend developer", "#Hiring now", "We are looking for a QA lead",
            "Looking for a Python developer", "Urgent opening for testers", "Job opening: SRE",
            "3 vacancies in Pune", "Openings for Java engineers", "Send your CV to me",
            "Share your updated resume", "Immediate joiners only", "Walk-in drive on Saturday",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_hiring_post(text))

    def test_non_hiring_posts(self):
        for text in (
            "Thrilled to share I started a new position at Initech!",
            "Great talk on Django performance today.",
            "Looking forward to the conference next week.",
            "I am open to work as a Python developer",
        ):
            with self.subTest(text=text):
                self.assertFalse(is_hiring_post(text))


class ContactExtractionTests(SimpleTestCase):
    def test_emails(self):
        text = "Mail HR@Acme.Example. or jobs@acme.example, again hr@acme.example"
        self.assertEqual(extract_emails(text), ["hr@acme.example", "jobs@acme.example"])
        self.assertEqual(extract_emails("no email @ here, user@localhost"), [])

    def test_phones_and_whatsapp(self):
        text = "Call +91 98765 43210 or 9876501234\nWhatsApp: +1 (415) 555-0132\nAgain 98765-43210"
        phones, whatsapp = extract_phones(text)
        self.assertEqual(phones, ["+91 98765 43210", "9876501234"])
        self.assertEqual(whatsapp, ["+1 (415) 555-0132"])

    def test_numbers_that_are_not_phones(self):
        text = ("Batch 2023-2024, salary 4,50,000 to 6,00,000, ref 2024-11-12, "
                "3-5 years, ID 12345, version 1.2.3.4, CTC 12 LPA")
        self.assertEqual(extract_phones(text), ([], []))

    def test_single_contact_combines_identifiers_with_author(self):
        contacts = build_contacts("Email a@x.example or WhatsApp +91 98765 43210", "Priya")
        self.assertEqual(contacts, [{"name": "Priya", "email": "a@x.example", "whatsapp": "+91 98765 43210"}])

    def test_several_identifiers_are_separate_unnamed_contacts(self):
        contacts = build_contacts("a@x.example, b@x.example, call 9876501234", "Priya")
        self.assertEqual(contacts, [{"email": "a@x.example"}, {"email": "b@x.example"}, {"phone": "9876501234"}])

    def test_no_identifiers_no_contacts(self):
        self.assertEqual(build_contacts("Apply via the link", "Priya"), [])

    def test_author_name_optional(self):
        self.assertEqual(build_contacts("hr@x.example", None), [{"email": "hr@x.example"}])


class FieldExtractionTests(SimpleTestCase):
    def test_titles(self):
        cases = {
            "Role: Backend Engineer\nCompany: Globex": ("Backend Engineer", "label"),
            "Job Title - Data Analyst": ("Data Analyst", "label"),
            "We're hiring a Senior Python Developer at Acme Labs! 🚀": ("Senior Python Developer", "hiring_phrase"),
            "#Hiring for Java Full Stack Developer @ TCS, Chennai": ("Java Full Stack Developer", "hiring_phrase"),
            "Hiring Now: QA Automation Engineer (Selenium)": ("QA Automation Engineer", "hiring_phrase"),
            "Looking for an experienced Data Engineer to join our team": ("Data Engineer", "hiring_phrase"),
            "Urgent opening for React Developers - immediate joiners": ("React Developers", "hiring_phrase"),
            "We are hiring! Freshers welcome.": ("We are hiring", "first_line"),
            "🔥 Exciting opportunity 🔥\nSend CV.": ("Exciting opportunity", "first_line"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(extract_title(text), expected)

    def test_title_length_capped(self):
        title, _ = extract_title("Role: " + "Engineer " * 40)
        self.assertLessEqual(len(title), 120)

    def test_company(self):
        self.assertEqual(extract_company("Company: Globex Inc."), "Globex Inc")
        self.assertEqual(extract_company("We're hiring a Python Developer at Acme Labs in Pune"), "Acme Labs")
        self.assertEqual(extract_company("#Hiring for Java Developer @ TCS, Chennai"), "TCS")
        self.assertIsNone(extract_company("Hiring a developer. Great at teamwork."))

    def test_location_work_mode_experience(self):
        text = "Location: Pune, India\nHybrid or remote possible\nExperience: 2-4 yrs"
        self.assertEqual(extract_location(text), "Pune, India")
        self.assertEqual(extract_work_mode(text), "Remote, Hybrid")
        self.assertEqual(extract_experience_text(text), "2-4 yrs")
        self.assertEqual(extract_work_mode("Work from office (WFO)"), "On-site")
        self.assertIsNone(extract_work_mode("Great team"))
        self.assertIsNone(extract_location("Great team"))
        self.assertIsNone(extract_experience_text("Great team"))


class BuildPayloadTests(SimpleTestCase):
    def test_full_payload(self):
        posted = datetime(2026, 11, 10, 9, 30, tzinfo=timezone.utc)
        text = ("We're hiring a Senior Python Developer at Acme Labs!\nLocation: Bengaluru\n"
                "Experience: 3-5 years\nSend your CV to careers@acme.example")
        payload = build_payload(RawPost(text=text, url=" https://www.linkedin.com/feed/update/urn:li:activity:1/ ",
                                        author_name="Priya", posted_at=posted))
        self.assertEqual(payload, {
            "title": "Senior Python Developer",
            "full_jd": text,
            "source": "LINKEDIN",
            "contacts": [{"name": "Priya", "email": "careers@acme.example"}],
            "source_url": "https://www.linkedin.com/feed/update/urn:li:activity:1/",
            "company_name": "Acme Labs",
            "location": "Bengaluru",
            "required_experience_text": "3-5 years",
            "posted_date": "2026-11-10T09:30:00+00:00",
        })

    def test_full_jd_is_exact_post_text(self):
        text = "  Hiring: Backend Engineer\r\n\r\nWe use Django and PostgreSQL daily. 🚀  "
        payload = build_payload(RawPost(text=text))
        self.assertEqual(payload["full_jd"], "Hiring: Backend Engineer\n\nWe use Django and PostgreSQL daily. 🚀")
        self.assertNotIn("source_url", payload)
        self.assertNotIn("company_name", payload)
        self.assertEqual(payload["contacts"], [])

    def test_non_hiring_skipped(self):
        with self.assertRaises(PostSkipped):
            build_payload(RawPost(text="Thrilled to share that I started a new position at Initech today!"))

    def test_short_or_empty_invalid(self):
        for text in ("", "   ", "Hiring! DM me."):
            with self.subTest(text=text), self.assertRaises(PostParseError):
                build_payload(RawPost(text=text))

    def test_fixture_payloads_pass_ingest_serializer(self):
        from tracker.api.serializers import JobIngestSerializer

        posts = json.loads(FIXTURE.read_text(encoding="utf-8"))
        valid = 0
        for entry in posts:
            try:
                payload = build_payload(RawPost(text=entry["text"], url=entry.get("url"),
                                                author_name=entry.get("author_name")))
            except (PostSkipped, PostParseError):
                continue
            serializer = JobIngestSerializer(data=payload)
            self.assertTrue(serializer.is_valid(), serializer.errors)
            valid += 1
        self.assertEqual(valid, 5)
