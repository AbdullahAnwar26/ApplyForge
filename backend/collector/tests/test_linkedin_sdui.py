"""
Current LinkedIn search layout ("SDUI"), offline. Fixtures mirror only the
STRUCTURE observed in the Phase 6b live diagnostic (hashed class names, a
"Feed post" heading per post, a profile link, headline, relative time, text
paragraph with an inline "… more" button, no data-urn and no permalink).
All names, companies, addresses and text are fictional.
"""

import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.test import SimpleTestCase, TestCase

from collector.linkedin import selectors as S
from collector.linkedin.extract import PageState, classify_page, extract_posts, post_key
from collector.linkedin.source import LinkedInPostSource, LoginRequired
from collector.posts import build_payload
from collector.service import run_collection
from tracker.api.serializers import JobIngestSerializer
from tracker.models import JobOpportunity, Profile, Skill

from .linkedin_pages import CHECKPOINT_PAGE, LOGIN_PAGE, sdui_post_html, sdui_search_page
from .test_linkedin_source import FakeBrowser

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
SEARCH_URL = S.content_search_url("python backend developer")

POST_A = sdui_post_html(
    author="Riya Fictional", headline="Talent Partner at Example Labs", relative="3h",
    text="We're hiring a Python Backend Developer at Example Labs!\nExperience: 1-3 years\n"
         "Django, REST APIs, PostgreSQL\nSend your CV to jobs@examplelabs.test",
    slug="riya-fictional-0001", seed="a",
)
POST_B = sdui_post_html(
    author="Omar Placeholder", headline="Engineering Manager", relative="1d",
    text="Looking for a Django Developer to join our team in Pune.\nShare your resume: hr@placeholder.test",
    slug="omar-placeholder-0002", seed="b",
)


class SduiClassifyTests(SimpleTestCase):
    def test_results_page_recognised(self):
        self.assertIs(classify_page(SEARCH_URL, sdui_search_page(POST_A, POST_B)), PageState.RESULTS)

    def test_page_without_post_headings_is_unrecognized(self):
        page = sdui_search_page(POST_A.replace(">Feed post<", ">Something else<"))
        self.assertIs(classify_page(SEARCH_URL, page), PageState.UNRECOGNIZED)

    def test_login_and_security_pages_unchanged(self):
        self.assertIs(classify_page(S.LOGIN_URL, sdui_search_page(POST_A)), PageState.LOGIN_REQUIRED)
        self.assertIs(classify_page("https://www.linkedin.com/checkpoint/challenge/x", sdui_search_page(POST_A)),
                      PageState.CHECKPOINT)
        self.assertIs(classify_page(SEARCH_URL, LOGIN_PAGE), PageState.LOGIN_REQUIRED)
        self.assertIs(classify_page(SEARCH_URL, CHECKPOINT_PAGE), PageState.CHECKPOINT)


class SduiExtractTests(SimpleTestCase):
    def test_fields(self):
        posts = extract_posts(sdui_search_page(POST_A, POST_B), now=NOW)
        self.assertEqual(len(posts), 2)
        a = posts[0]
        self.assertEqual(a.author_name, "Riya Fictional")          # not "• 2nd"
        self.assertEqual(a.author_headline, "Talent Partner at Example Labs")
        self.assertEqual(a.posted_at, NOW - timedelta(hours=3))
        self.assertIsNone(a.url)                                     # no permalink in this layout
        self.assertEqual(a.text, (
            "We're hiring a Python Backend Developer at Example Labs!\nExperience: 1-3 years\n"
            "Django, REST APIs, PostgreSQL\nSend your CV to jobs@examplelabs.test\n#hiring"
        ))
        self.assertNotIn("more", a.text.split("\n")[-1])            # "… more" button dropped
        self.assertNotIn("Follow", a.text)
        b = posts[1]
        self.assertEqual((b.author_name, b.author_headline), ("Omar Placeholder", "Engineering Manager"))
        self.assertEqual(b.posted_at, NOW - timedelta(days=1))

    def test_class_names_are_irrelevant(self):
        page = sdui_search_page(POST_A, POST_B)
        renamed = re.sub(r'class="[^"]*"', lambda m: f'class="q{abs(hash(m.group(0))) % 9999}"', page)
        self.assertEqual(extract_posts(page, now=NOW), extract_posts(renamed, now=NOW))

    def test_duplicates_removed_but_different_authors_kept(self):
        same_text_other_author = sdui_post_html(
            author="Lena Example", text="We're hiring a Python Backend Developer at Example Labs!\n"
                                        "Experience: 1-3 years\nDjango, REST APIs, PostgreSQL\n"
                                        "Send your CV to jobs@examplelabs.test", slug="lena-0003", seed="c",
        )
        posts = extract_posts(sdui_search_page(POST_A, POST_A, POST_B, same_text_other_author), now=NOW)
        self.assertEqual([p.author_name for p in posts], ["Riya Fictional", "Omar Placeholder", "Lena Example"])
        self.assertEqual(len({post_key(p) for p in posts}), 3)

    def test_short_post_text_is_still_the_body_not_the_headline(self):
        post = sdui_post_html(headline="Senior Talent Acquisition Partner at a Very Long Company Name",
                              text="Hiring! Mail cv@short.test", seed="d")
        extracted = extract_posts(sdui_search_page(post), now=NOW)[0]
        self.assertEqual(extracted.text, "Hiring! Mail cv@short.test\n#hiring")
        self.assertEqual(extracted.author_headline, "Senior Talent Acquisition Partner at a Very Long Company Name")

    def test_missing_time_label(self):
        post = extract_posts(sdui_search_page(sdui_post_html(relative="", seed="e")), now=NOW)[0]
        self.assertIsNone(post.posted_at)
        self.assertIn("We're hiring a Backend Engineer!", post.text)

    def test_legacy_layout_still_preferred_when_present(self):
        from .linkedin_pages import HIRING_TEXT, post_html, search_page, urn_for

        posts = extract_posts(search_page(post_html(urn_for(seq=1), HIRING_TEXT)))
        self.assertEqual(posts[0].url, f"https://www.linkedin.com/feed/update/{urn_for(seq=1)}/")

    def test_payload_is_valid_for_ingestion(self):
        post = extract_posts(sdui_search_page(POST_A), now=NOW)[0]
        payload = build_payload(post)
        self.assertNotIn("source_url", payload)
        self.assertEqual(payload["title"], "Python Backend Developer")
        self.assertEqual(payload["contacts"], [{"name": "Riya Fictional", "email": "jobs@examplelabs.test"}])
        serializer = JobIngestSerializer(data=payload)
        self.assertTrue(serializer.is_valid(), serializer.errors)


class SduiSourceAndIngestionTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        profile = Profile.objects.create(name="Test User", target_roles="python backend developer",
                                         experience_years=1.3, max_acceptable_experience_years=9)
        for name in ("Python", "Django", "REST APIs", "PostgreSQL"):
            Skill.objects.create(name=name, profile=profile)

    def source(self, browser):
        return LinkedInPostSource(browser_factory=lambda: browser, max_scrolls=2,
                                  debug_dir=Path(self.tmp) / "debug")

    def test_scroll_dedupes_url_less_posts(self):
        browser = FakeBrowser({"python backend developer": [
            sdui_search_page(POST_A), sdui_search_page(POST_A, POST_B), sdui_search_page(POST_A, POST_B),
        ]})
        posts = list(self.source(browser).iter_posts(["python backend developer"], 10))
        self.assertEqual([p.author_name for p in posts], ["Riya Fictional", "Omar Placeholder"])
        self.assertEqual(browser.events.count("scroll"), 2)

    def test_ingestion_without_urls_is_idempotent_by_fingerprint(self):
        pages = {"python backend developer": [sdui_search_page(POST_A, POST_B)]}
        first = run_collection(self.source(FakeBrowser(pages)))
        self.assertEqual(first.counts["created"], 2)
        self.assertTrue(all(j.source_url is None for j in JobOpportunity.objects.all()))
        self.assertTrue(all(j.source == "LINKEDIN" for j in JobOpportunity.objects.all()))
        second = run_collection(self.source(FakeBrowser(pages)))
        self.assertEqual(second.counts["existing"], 2)
        self.assertTrue(all(o.matched_by == "jd_fingerprint" for o in second.outcomes))
        self.assertEqual(JobOpportunity.objects.count(), 2)

    def test_login_wall_still_stops_the_run(self):
        browser = FakeBrowser({"python backend developer": [sdui_search_page(POST_A), LOGIN_PAGE]})
        with self.assertRaises(LoginRequired):
            list(self.source(browser).iter_posts(["python backend developer"], 10))
