"""Regression tests for LinkedIn post URL normalization (Milestone 6, Phase 6a)."""

from django.test import SimpleTestCase

from tracker.ingestion import normalize_source_url

ACTIVITY = "https://www.linkedin.com/feed/update/urn:li:activity:7212345678901234567/"


class LinkedInPostUrlTests(SimpleTestCase):
    def test_activity_variants_collapse(self):
        for url in (
            ACTIVITY,
            "https://www.linkedin.com/feed/update/urn:li:activity:7212345678901234567",
            "https://linkedin.com/feed/update/urn:li:activity:7212345678901234567/?utm_source=share&utm_medium=member_desktop",
            "https://in.linkedin.com/feed/update/urn:li:activity:7212345678901234567/#comments",
            "https://www.linkedin.com/feed/update/urn%3Ali%3Aactivity%3A7212345678901234567/",
            "https://www.linkedin.com/posts/jane-recruiter_hiring-python-activity-7212345678901234567-AbCd?utm_source=share",
            "https://www.linkedin.com/posts/jane-recruiter_hiring-python-activity-7212345678901234567-Ab_d/",
            "http://WWW.LINKEDIN.COM/posts/acme_activity-7212345678901234567-x1y2",
        ):
            with self.subTest(url=url):
                self.assertEqual(normalize_source_url(url), ACTIVITY)

    def test_share_and_ugc_post_keep_their_type(self):
        self.assertEqual(
            normalize_source_url("https://in.linkedin.com/posts/acme_hiring-ugcPost-7212345678901234999-xY_z/"),
            "https://www.linkedin.com/feed/update/urn:li:ugcPost:7212345678901234999/",
        )
        self.assertEqual(
            normalize_source_url("https://www.linkedin.com/feed/update/urn:li:share:7212345678901234000"),
            "https://www.linkedin.com/feed/update/urn:li:share:7212345678901234000/",
        )
        self.assertNotEqual(
            normalize_source_url("https://www.linkedin.com/feed/update/urn:li:share:1/"),
            normalize_source_url("https://www.linkedin.com/feed/update/urn:li:activity:1/"),
        )

    def test_existing_job_link_behavior_unchanged(self):
        self.assertEqual(
            normalize_source_url("https://www.linkedin.com/jobs/view/senior-dev-at-acme-3812345678/?trk=abc"),
            "https://www.linkedin.com/jobs/view/3812345678/",
        )
        self.assertEqual(
            normalize_source_url("https://www.linkedin.com/jobs/search/?currentJobId=123&keywords=py"),
            "https://www.linkedin.com/jobs/view/123/",
        )

    def test_other_linkedin_pages_use_generic_normalization(self):
        self.assertEqual(
            normalize_source_url("https://www.linkedin.com/in/jane-recruiter/?trk=x"),
            "https://www.linkedin.com/in/jane-recruiter/",
        )
        self.assertEqual(
            normalize_source_url("https://www.linkedin.com/posts/no-id-here/"),
            "https://www.linkedin.com/posts/no-id-here/",
        )
        self.assertEqual(
            normalize_source_url("https://www.linkedin.com/feed/update/urn:li:activity:abc/"),
            "https://www.linkedin.com/feed/update/urn:li:activity:abc/",
        )

    def test_non_linkedin_host_with_similar_path_untouched(self):
        self.assertEqual(
            normalize_source_url("https://example.com/feed/update/urn:li:activity:1/"),
            "https://example.com/feed/update/urn:li:activity:1/",
        )
