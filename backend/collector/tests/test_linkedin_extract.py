"""Offline tests for LinkedIn page classification and post extraction (synthetic HTML)."""

from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from collector.linkedin import selectors as S
from collector.linkedin.extract import (
    PageState,
    canonical_urn,
    classify_page,
    extract_posts,
    is_logged_in,
    post_key,
    posted_at_from_relative,
    posted_at_from_urn,
)

from .linkedin_pages import (
    AUTHWALL_PAGE,
    CHECKPOINT_PAGE,
    FEED_PAGE_NO_NAV,
    HIRING_TEXT,
    LOGIN_PAGE,
    NO_RESULTS_PAGE,
    NOT_HIRING_TEXT,
    POSTED,
    REACT_TEXT,
    UNRECOGNIZED_PAGE,
    post_html,
    search_page,
    urn_for,
)

SEARCH_URL = S.content_search_url("python developer")


class SelectorTests(SimpleTestCase):
    def test_search_url(self):
        self.assertEqual(
            SEARCH_URL,
            "https://www.linkedin.com/search/results/content/"
            "?keywords=python+developer&origin=GLOBAL_SEARCH_HEADER&sortBy=%22date_posted%22",
        )


class ClassifyTests(SimpleTestCase):
    def test_states(self):
        results = search_page(post_html(urn_for(), HIRING_TEXT))
        cases = [
            (SEARCH_URL, results, PageState.RESULTS),
            (SEARCH_URL, NO_RESULTS_PAGE, PageState.NO_RESULTS),
            (S.LOGIN_URL, LOGIN_PAGE, PageState.LOGIN_REQUIRED),
            ("https://www.linkedin.com/authwall?trk=x", AUTHWALL_PAGE, PageState.LOGIN_REQUIRED),
            (SEARCH_URL, LOGIN_PAGE, PageState.LOGIN_REQUIRED),  # login form served in place
            ("https://www.linkedin.com/checkpoint/challenge/x", AUTHWALL_PAGE, PageState.CHECKPOINT),
            ("https://www.linkedin.com/checkpoint/challengesV2/y", AUTHWALL_PAGE, PageState.CHECKPOINT),
            # The ordinary login form posts to /checkpoint/lg/... - that is a login, not a check.
            ("https://www.linkedin.com/checkpoint/lg/login-submit", LOGIN_PAGE, PageState.LOGIN_REQUIRED),
            (SEARCH_URL, CHECKPOINT_PAGE, PageState.CHECKPOINT),
            (SEARCH_URL, UNRECOGNIZED_PAGE, PageState.UNRECOGNIZED),
            (SEARCH_URL, "", PageState.UNRECOGNIZED),
        ]
        for url, html, expected in cases:
            with self.subTest(expected=expected, url=url):
                self.assertIs(classify_page(url, html), expected)



class IsLoggedInTests(SimpleTestCase):
    """Login detection must not depend on global-nav and must fail closed."""

    def test_feed_without_global_nav_is_logged_in(self):
        # The live Phase 6b regression: logged in on the home page, no global-nav.
        self.assertNotIn("global-nav", FEED_PAGE_NO_NAV)
        for url in ("https://www.linkedin.com/feed/", "https://www.linkedin.com/feed",
                    "https://linkedin.com/feed/?trk=nav_logo", "https://in.linkedin.com/feed/",
                    "https://www.linkedin.com/feed/update/urn:li:activity:1/"):
            with self.subTest(url=url):
                self.assertTrue(is_logged_in(url, FEED_PAGE_NO_NAV))

    def test_global_nav_is_an_extra_positive_signal(self):
        self.assertTrue(is_logged_in(S.FEED_URL, search_page()))
        self.assertTrue(is_logged_in(SEARCH_URL, NO_RESULTS_PAGE))  # non-feed page with global-nav

    def test_login_page(self):
        for url in (S.LOGIN_URL, "https://www.linkedin.com/uas/login?x",
                    "https://www.linkedin.com/checkpoint/lg/login-submit"):
            with self.subTest(url=url):
                self.assertFalse(is_logged_in(url, LOGIN_PAGE))
        # Login form served at the feed URL (no redirect) - still not logged in.
        self.assertFalse(is_logged_in(S.FEED_URL, LOGIN_PAGE))
        self.assertFalse(is_logged_in(S.FEED_URL, LOGIN_PAGE.replace("<body>", '<body><div id="global-nav">')))

    def test_sign_in_wall(self):
        self.assertFalse(is_logged_in("https://www.linkedin.com/authwall?trk=x", AUTHWALL_PAGE))
        self.assertFalse(is_logged_in("https://www.linkedin.com/signup/cold-join", AUTHWALL_PAGE))
        # Sign-in wall served at the feed URL without redirecting.
        self.assertFalse(is_logged_in(S.FEED_URL, AUTHWALL_PAGE))
        for marker in ('<a href="/login">Sign in</a>', '<a href="https://www.linkedin.com/signup">Join</a>',
                       '<div class="authwall-sign-in">x</div>', '<a href="/start/join">Join</a>'):
            with self.subTest(marker=marker):
                self.assertFalse(is_logged_in(S.FEED_URL, FEED_PAGE_NO_NAV.replace("</main>", marker + "</main>")))

    def test_other_sites_login_links_do_not_count_as_guest(self):
        self.assertIn("status.example.com/login", FEED_PAGE_NO_NAV)
        self.assertTrue(is_logged_in(S.FEED_URL, FEED_PAGE_NO_NAV))

    def test_security_challenge(self):
        for url, html in (("https://www.linkedin.com/checkpoint/challenge/abc", FEED_PAGE_NO_NAV),
                          ("https://www.linkedin.com/checkpoint/challengesV2/x", search_page()),
                          (S.FEED_URL, CHECKPOINT_PAGE)):
            with self.subTest(url=url):
                self.assertFalse(is_logged_in(url, html))

    def test_non_linkedin_pages(self):
        for url in ("https://example.com/feed/", "https://accounts.google.com/signin",
                    "https://linkedin.com.evil.example/feed/", "https://evil-linkedin.com/feed/",
                    "http://www.linkedin.com/feed/", "file:///C:/feed/", "about:blank", "", None):
            with self.subTest(url=url):
                self.assertFalse(is_logged_in(url, FEED_PAGE_NO_NAV))
                self.assertFalse(is_logged_in(url, search_page()))

    def test_ambiguous_states_fail_closed(self):
        cases = [
            (S.FEED_URL, ""),                                    # nothing loaded
            (S.FEED_URL, "<html><body>   </body></html>"),       # blank page
            ("https://www.linkedin.com/", FEED_PAGE_NO_NAV),     # not a feed path, no global-nav
            ("https://www.linkedin.com/feedback/", FEED_PAGE_NO_NAV),  # /feedback is not /feed
            ("https://www.linkedin.com/jobs/", "<html><body><main>Jobs</main></body></html>"),
            (SEARCH_URL, UNRECOGNIZED_PAGE.replace('id="global-nav"', 'id="other"')),
        ]
        for url, html in cases:
            with self.subTest(url=url, html=html[:30]):
                self.assertFalse(is_logged_in(url, html))


class ExtractTests(SimpleTestCase):
    def test_post_fields(self):
        urn = urn_for(seq=1)
        posts = extract_posts(search_page(post_html(urn, HIRING_TEXT)))
        self.assertEqual(len(posts), 1)
        post = posts[0]
        self.assertEqual(post.url, f"https://www.linkedin.com/feed/update/{urn}/")
        self.assertEqual(post.author_name, "Priya Sharma")  # hidden duplicate ignored
        self.assertEqual(post.author_headline, "Talent Acquisition at Acme Labs")
        self.assertEqual(post.posted_at, POSTED)
        self.assertEqual(post.text, (
            "We're hiring a Senior Python Developer at Acme Labs! 🚀\n"
            "Location: Bengaluru (Hybrid)\nExperience: 3-5 years\n"
            "Send your CV to careers@acme.example or WhatsApp +91 98765 43210\n"
            "#hiring #python"
        ))  # line breaks kept, "…see more" and "hashtag" labels removed

    def test_multiple_posts_in_order_and_deduplicated(self):
        a, b = urn_for(seq=1), urn_for(seq=2, kind="ugcPost")
        html = search_page(post_html(a, HIRING_TEXT), post_html(b, REACT_TEXT),
                           post_html(a, HIRING_TEXT), post_html(urn_for(seq=3), NOT_HIRING_TEXT))
        posts = extract_posts(html)
        self.assertEqual([p.url for p in posts][:2], [
            f"https://www.linkedin.com/feed/update/{a}/", f"https://www.linkedin.com/feed/update/{b}/",
        ])
        self.assertEqual(len(posts), 3)  # duplicate container dropped; non-hiring kept for the parser

    def test_urn_from_data_id_or_permalink(self):
        urn = urn_for(seq=7)
        by_data_id = extract_posts(search_page(post_html(urn, HIRING_TEXT, data_attr="data-id")))
        self.assertEqual(by_data_id[0].url, f"https://www.linkedin.com/feed/update/{urn}/")

    def test_relative_time_fallback(self):
        now = datetime(2026, 11, 16, tzinfo=timezone.utc)
        html = search_page(
            post_html(urn_for(seq=1), HIRING_TEXT, relative="3d").replace(
                f'data-urn="{urn_for(seq=1)}"', 'data-urn="urn:li:activity:12"'
            )
        )  # id too small to carry a plausible time
        post = extract_posts(html, now=now)[0]
        self.assertEqual(post.posted_at, now - timedelta(days=3))

    def test_empty_and_missing_pieces(self):
        html = search_page('<div class="feed-shared-update-v2" data-urn="urn:li:activity:5"></div>')
        post = extract_posts(html)[0]
        self.assertEqual(post.text, "")
        self.assertIsNone(post.author_name)
        self.assertEqual(extract_posts(NO_RESULTS_PAGE), [])
        self.assertEqual(extract_posts(""), [])


class TimeAndUrnTests(SimpleTestCase):
    def test_canonical_urn(self):
        self.assertEqual(canonical_urn("urn:li:UGCPOST:5"), "urn:li:ugcPost:5")
        self.assertEqual(canonical_urn("/feed/update/urn:li:activity:9/?x"), "urn:li:activity:9")
        self.assertIsNone(canonical_urn("urn:li:fs_miniProfile:abc"))

    def test_posted_at_from_urn(self):
        self.assertEqual(posted_at_from_urn(urn_for(POSTED, seq=123)), POSTED)
        self.assertIsNone(posted_at_from_urn("urn:li:activity:1"))           # before 2010
        future = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.assertIsNone(posted_at_from_urn(urn_for(future), now=POSTED))  # in the future
        self.assertIsNone(posted_at_from_urn("not a urn"))

    def test_relative_labels(self):
        now = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
        cases = {"5m": timedelta(minutes=5), "2h •": timedelta(hours=2), "3d": timedelta(days=3),
                 "1w": timedelta(weeks=1), "2mo": timedelta(days=60), "1yr": timedelta(days=365)}
        for label, delta in cases.items():
            with self.subTest(label=label):
                self.assertEqual(posted_at_from_relative(label, now), now - delta)
        self.assertIsNone(posted_at_from_relative("Edited", now))
        self.assertIsNone(posted_at_from_relative(None, now))

    def test_post_key(self):
        from collector.posts import RawPost

        url = "https://www.linkedin.com/posts/x_y-activity-7-abc"
        self.assertEqual(post_key(RawPost(text="t", url=url)), "urn:li:activity:7")
        # Without a URL (current layout): author + text.
        self.assertEqual(post_key(RawPost(text="t", author_name="A")), post_key(RawPost(text="t", author_name="A")))
        self.assertNotEqual(post_key(RawPost(text="t", author_name="A")), post_key(RawPost(text="t", author_name="B")))
