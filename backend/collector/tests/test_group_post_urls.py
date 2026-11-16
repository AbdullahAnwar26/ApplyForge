"""Group-post URL recovery: the post URN from percent-encoded LinkedIn links.

Seen in the v3 live inventory (1 of 9 posts, a group post): the post's URN was
percent-encoded in several of its own ``<a href>`` links. All markup and ids
here are fictional.
"""

from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from django.test import SimpleTestCase, TestCase

from collector.linkedin.extract import extract_posts, post_key
from collector.service import run_collection
from collector.sources import StaticPostSource
from tracker.ingestion import normalize_source_url
from tracker.models import JobOpportunity, Profile, Skill

from .linkedin_pages import dvn_post_html, sdui_search_page

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
POSTED = NOW - timedelta(days=2)


def post_id(when=POSTED, low_bits=12345):
    """A fictional LinkedIn-style id: creation time (ms) in the top bits."""
    return (int(when.timestamp() * 1000) << 22) | low_bits


URN = f"urn:li:activity:{post_id()}"
FEED_URL = f"https://www.linkedin.com/feed/update/{URN}/"


def enc(value, times=1):
    for _ in range(times):
        value = quote(value, safe="")
    return value


def group_post(links=(), commentary_links=(), nested="", heading=True, dvn=True):
    """A group post (actor = the group, no time label) with extra links in its header
    area, in its commentary, and optionally a nested (reshared) post."""
    header_links = "".join(f'<a href="{href}"><span>link</span></a>' for href in links)
    text_links = "".join(f' <a href="{href}">see</a>' for href in commentary_links)
    wrapper_open = '<div data-view-name="feed-full-update">' if dvn else "<div>"
    return f"""
<div role="listitem">{wrapper_open}<div class="zg">
 {'<h2><span>Feed post</span></h2>' if heading else ''}
 <a href="https://www.linkedin.com/groups/1234567/"><div data-view-name="feed-actor"><div><div>
  <p><span>Fictional Python Group</span></p></div></div></div></a>
 <div><div><p><span>Fictional Poster</span></p></div></div>
 <div><div><p><span><span>• 3rd+</span></span></p></div></div>
 <div><div><p><span>Recruiter</span></p></div></div>
 <div>{header_links}</div>
 <p data-view-name="feed-commentary"><span>We're hiring a Python Developer! 2+ years of experience.
  Mail jobs@fictional.test{text_links}</span></p>
 {nested}
</div></div></div>"""


def nested_post(urn):
    return f"""<div data-view-name="feed-full-update"><div>
 <a href="/feed/update/?highlightedUpdateUrn={enc(urn)}">inner</a>
 <p data-view-name="feed-commentary"><span>Inner reshared post.</span></p></div></div>"""


GROUP_LINK = f"https://www.linkedin.com/groups/1234567/?highlightedUpdateUrn={enc(URN)}"
RELATIVE_LINK = f"/feed/update/?updateEntityUrn={enc(URN)}&origin=GROUP"


class GroupPostUrlTests(SimpleTestCase):
    def one(self, html):
        posts = extract_posts(sdui_search_page(html), now=NOW)
        self.assertEqual(len(posts), 1)
        return posts[0]

    def test_encoded_urn_in_own_links_gives_the_post_url(self):
        post = self.one(group_post(links=[GROUP_LINK, RELATIVE_LINK, GROUP_LINK]))
        self.assertEqual(post.url, FEED_URL)
        # Group posts show no time label; the URN supplies the posting time.
        self.assertIsNone(post.posted_label)
        self.assertEqual(post.posted_at, POSTED.replace(microsecond=0))
        self.assertEqual((post.author_name, post.author_headline), ("Fictional Poster", "Recruiter"))

    def test_heading_layout_without_hooks(self):
        post = self.one(group_post(links=[RELATIVE_LINK], dvn=False))
        self.assertEqual(post.url, FEED_URL)

    def test_double_encoded_and_ugc_post(self):
        ugc = f"urn:li:ugcPost:{post_id(low_bits=7)}"
        post = self.one(group_post(links=[f"/groups/1234567/?u={enc(ugc, times=2)}"]))
        self.assertEqual(post.url, f"https://www.linkedin.com/feed/update/{ugc}/")

    def test_ambiguous_urns_give_no_url(self):
        other = f"urn:li:activity:{post_id(low_bits=99)}"
        post = self.one(group_post(links=[GROUP_LINK, f"/feed/update/?u={enc(other)}"]))
        self.assertIsNone(post.url)

    def test_links_in_the_post_text_are_ignored(self):
        post = self.one(group_post(commentary_links=[RELATIVE_LINK]))
        self.assertIsNone(post.url)

    def test_reshared_post_urn_is_ignored(self):
        inner = f"urn:li:activity:{post_id(low_bits=55)}"
        self.assertIsNone(self.one(group_post(nested=nested_post(inner))).url)
        # The outer post's own link still counts; the nested one does not make it ambiguous.
        self.assertEqual(self.one(group_post(links=[GROUP_LINK], nested=nested_post(inner))).url, FEED_URL)

    def test_external_links_are_ignored(self):
        for href in (f"https://fictional.example/share?u={enc(URN)}",
                     f"//fictional.example/?u={enc(URN)}",
                     f"http://www.linkedin.com/feed/update/?u={enc(URN)}",
                     f"https://linkedin.com.fictional.example/?u={enc(URN)}"):
            with self.subTest(href=href):
                self.assertIsNone(self.one(group_post(links=[href])).url)

    def test_implausible_ids_are_rejected(self):
        future = f"urn:li:activity:{post_id(when=NOW + timedelta(days=30))}"
        tiny = "urn:li:activity:12345"
        for urn in (future, tiny):
            with self.subTest(urn=urn):
                self.assertIsNone(self.one(group_post(links=[f"/feed/update/?u={enc(urn)}"])).url)

    def test_other_urn_types_are_ignored(self):
        post = self.one(group_post(links=[f"/in/someone/?miniProfileUrn={enc('urn:li:fs_miniProfile:ACoAAB')}"]))
        self.assertIsNone(post.url)

    def test_posts_without_encoded_links_are_unchanged(self):
        posts = extract_posts(sdui_search_page(
            dvn_post_html(author="Riya Fictional", headline="Talent Partner", seed="p")), now=NOW)
        self.assertIsNone(posts[0].url)

    def test_url_is_the_dedupe_key_and_matches_tracker_normalization(self):
        post = self.one(group_post(links=[GROUP_LINK]))
        self.assertEqual(post_key(post), URN)
        self.assertEqual(normalize_source_url(post.url), normalize_source_url(FEED_URL))


class GroupPostIngestionTests(TestCase):
    def test_repeat_collection_matches_by_url(self):
        profile = Profile.objects.create(name="Jane", target_roles="Python Developer",
                                         experience_years=3, max_acceptable_experience_years=6)
        for name in ("Python", "Django"):
            Skill.objects.create(name=name, profile=profile)
        post = extract_posts(sdui_search_page(group_post(links=[GROUP_LINK])), now=NOW)[0]
        first = run_collection(StaticPostSource([post]))
        second = run_collection(StaticPostSource([post]))
        self.assertEqual(first.counts["created"], 1)
        self.assertEqual(second.outcomes[0].status, "existing")
        self.assertEqual(second.outcomes[0].matched_by, "source_url")
        self.assertEqual(JobOpportunity.objects.get().source_url, normalize_source_url(FEED_URL))
