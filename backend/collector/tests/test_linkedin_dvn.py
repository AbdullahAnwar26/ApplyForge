"""
data-view-name hooks (feed-full-update / feed-commentary / feed-actor), preferred
over the English heading and the structural heuristics, with per-field fallback.
Offline; synthetic, fictional content only.
"""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.test import SimpleTestCase, TestCase

from collector.linkedin import selectors as S
from collector.linkedin.extract import PageState, classify_page, extract_posts, post_key
from collector.linkedin.source import LinkedInPostSource
from collector.posts import PostSkipped, build_payload
from collector.service import run_collection
from tracker.api.serializers import JobIngestSerializer
from tracker.models import JobOpportunity, Profile, Skill

from .linkedin_pages import dvn_post_html, sdui_post_html, sdui_search_page
from .test_linkedin_source import FakeBrowser

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
SEARCH_URL = S.content_search_url("python backend developer")
UI_WORDS = ("Follow", "Connect", "Like", "Comment", "Repost", "Send", "more", "Open control menu",
            "• 2nd", "Visit my site", "12")

POST_A = dvn_post_html(
    author="Riya Fictional", headline="Talent Partner at Example Labs", relative="3h",
    text="We're hiring a Python Backend Developer at Example Labs!\nExperience: 1-3 years\n"
         "Django, REST APIs\nSend your CV to jobs@examplelabs.test",
    slug="riya-0001", seed="a",
)
POST_B = dvn_post_html(
    author="Omar Placeholder", headline="Engineering Manager", relative="1d",
    text="Looking for a Django Developer in Pune.\nShare your resume: hr@placeholder.test",
    slug="omar-0002", seed="b",
)


def page(*posts):
    return sdui_search_page(*posts)


def only(html):
    posts = extract_posts(html, now=NOW)
    assert len(posts) == 1, posts
    return posts[0]


class ContainerTests(SimpleTestCase):
    def test_hook_classifies_and_takes_priority(self):
        html = page(POST_A, POST_B)
        self.assertIs(classify_page(SEARCH_URL, html), PageState.RESULTS)
        posts = extract_posts(html, now=NOW)
        self.assertEqual([p.author_name for p in posts], ["Riya Fictional", "Omar Placeholder"])

    def test_language_neutral_without_english_heading(self):
        for heading in (None, "Beitrag im Feed", "Publication du fil"):
            with self.subTest(heading=heading):
                html = page(dvn_post_html(heading=heading, seed="g"))
                self.assertIs(classify_page(SEARCH_URL, html), PageState.RESULTS)
                self.assertEqual(only(html).author_name, "Riya Fictional")

    def test_boundaries_between_posts(self):
        posts = extract_posts(page(POST_A, POST_B), now=NOW)
        self.assertNotIn("Omar", posts[0].text + (posts[0].author_headline or ""))
        self.assertNotIn("Riya", posts[1].text + (posts[1].author_headline or ""))
        self.assertIn("jobs@examplelabs.test", posts[0].text)
        self.assertNotIn("hr@placeholder.test", posts[0].text)

    def test_nested_reshare_is_not_a_separate_post_and_does_not_leak(self):
        reshared = dvn_post_html(author="Inner Author", text="Original inner post text about Rust.",
                                 slug="inner-0003", seed="r", heading=None)
        outer = dvn_post_html(text="We're hiring! See below.\nMail cv@outer.test", nested=reshared, seed="o")
        posts = extract_posts(page(outer), now=NOW)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].author_name, "Riya Fictional")
        self.assertEqual(posts[0].text, "We're hiring! See below.\nMail cv@outer.test")
        self.assertNotIn("Inner", posts[0].text)


class FieldTests(SimpleTestCase):
    def test_author_commentary_headline_time(self):
        post = only(page(POST_A))
        self.assertEqual(post.author_name, "Riya Fictional")
        self.assertEqual(post.author_headline, "Talent Partner at Example Labs")
        self.assertEqual(post.posted_at, NOW - timedelta(hours=3))
        self.assertEqual(post.text, "We're hiring a Python Backend Developer at Example Labs!\n"
                                    "Experience: 1-3 years\nDjango, REST APIs\nSend your CV to jobs@examplelabs.test")

    def test_optional_elements_and_buttons_do_not_contaminate(self):
        html = page(dvn_post_html(cta="Visit my site", image_caption="Team photo at the fictional office party",
                                  seed="c"))
        post = only(html)
        self.assertEqual(post.text, "We're hiring a Python Backend Developer!\nSend your CV to jobs@example.test")
        body_without_own_words = post.text.replace("Send your CV", "")  # "Send" is legitimately in the text
        for word in UI_WORDS + ("Team photo",):
            with self.subTest(word=word):
                self.assertNotIn(word, body_without_own_words)
        self.assertNotIn("•", post.author_name)

    def test_commentary_hook_beats_the_largest_paragraph_heuristic(self):
        # A long image caption after the time label would win the structural guess.
        caption = "A very long fictional image caption that is much longer than the post text itself " * 2
        post = only(page(dvn_post_html(text="Hiring! Mail cv@short.test", image_caption=caption, seed="h")))
        self.assertEqual(post.text, "Hiring! Mail cv@short.test")

    def test_no_invented_url(self):
        for post in extract_posts(page(POST_A, POST_B), now=NOW):
            self.assertIsNone(post.url)


class FallbackTests(SimpleTestCase):
    def test_missing_commentary_hook_uses_structural_text(self):
        post = only(page(dvn_post_html(commentary=False, seed="m")))
        self.assertEqual(post.text, "We're hiring a Python Backend Developer!\nSend your CV to jobs@example.test")

    def test_empty_commentary_hook_falls_back(self):
        html = page(dvn_post_html(commentary="empty", seed="e").replace(
            "</div>\n </div>\n</div>",  # add a plain text paragraph the structural rule can find
            "<p>Hiring now, mail fallback@example.test</p></div>\n </div>\n</div>", 1))
        post = only(html)
        self.assertEqual(post.text, "Hiring now, mail fallback@example.test")

    def test_missing_or_empty_actor_hook_uses_profile_link(self):
        for actor in (False, "empty"):
            with self.subTest(actor=actor):
                post = only(page(dvn_post_html(actor=actor, seed="x")))
                expected = "Riya Fictional" if actor is False else None
                # Empty actor hook: the structural fallback finds no non-empty name in that link
                # either, so the avatar/other links are tried; none has text -> None.
                self.assertEqual(post.author_name, expected)

    def test_no_hooks_at_all_uses_heading_path(self):
        post = only(sdui_search_page(sdui_post_html(author="Heading Path", seed="q")))
        self.assertEqual(post.author_name, "Heading Path")

    def test_pages_mixing_layouts_prefer_hooks(self):
        posts = extract_posts(page(POST_A, sdui_post_html(author="Heading Only", seed="z")), now=NOW)
        self.assertEqual([p.author_name for p in posts], ["Riya Fictional"])  # hooked layout wins


class PayloadAndDedupTests(SimpleTestCase):
    def test_payload_fields_preserved(self):
        payload = build_payload(only(page(POST_A)))
        self.assertEqual(payload["title"], "Python Backend Developer")
        self.assertEqual(payload["company_name"], "Example Labs")
        self.assertEqual(payload["required_experience_text"], "1-3 years")
        self.assertEqual(payload["contacts"], [{"name": "Riya Fictional", "email": "jobs@examplelabs.test"}])
        self.assertNotIn("source_url", payload)
        self.assertTrue(JobIngestSerializer(data=payload).is_valid())

    def test_non_hiring_post_still_skipped(self):
        post = only(page(dvn_post_html(text="Thrilled to share that I started a new position at Initech today!",
                                       seed="n")))
        with self.assertRaises(PostSkipped):
            build_payload(post)

    def test_duplicates_removed(self):
        posts = extract_posts(page(POST_A, POST_A, POST_B), now=NOW)
        self.assertEqual(len(posts), 2)
        self.assertEqual(len({post_key(p) for p in posts}), 2)


class CollectionTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        profile = Profile.objects.create(name="Test User", target_roles="python backend developer",
                                         experience_years=1.3, max_acceptable_experience_years=9)
        for name in ("Python", "Django", "REST APIs"):
            Skill.objects.create(name=name, profile=profile)

    def source(self, pages):
        return LinkedInPostSource(browser_factory=lambda: FakeBrowser(pages), max_scrolls=1,
                                  debug_dir=Path(self.tmp) / "debug")

    def test_end_to_end_counts_and_no_urls(self):
        not_hiring = dvn_post_html(author="Sam Lee", slug="sam-0009", seed="s",
                                   text="Thrilled to share that I started a new position at Initech today!")
        result = run_collection(self.source({"python backend developer": [page(POST_A, POST_B, not_hiring)]}))
        self.assertEqual((result.counts["created"], result.counts["skipped"]), (2, 1))
        self.assertTrue(all(job.source_url is None for job in JobOpportunity.objects.all()))
        job = JobOpportunity.objects.get(title="Python Backend Developer")
        self.assertEqual(job.contacts.get().name, "Riya Fictional")
        again = run_collection(self.source({"python backend developer": [page(POST_A, POST_B, not_hiring)]}))
        self.assertEqual(again.counts["existing"], 2)
        self.assertFalse((Path(self.tmp) / "debug").exists())
