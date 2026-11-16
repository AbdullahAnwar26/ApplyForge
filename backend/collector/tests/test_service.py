"""Collection service tests: posts -> real tracker ingestion and screening (in-process)."""

from pathlib import Path
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings

import tracker.ingestion
from collector.posts import RawPost
from collector.service import CollectionInputError, run_collection
from collector.sources import JsonFilePostSource, SourceError, StaticPostSource
from tracker.models import Application, Company, Contact, JobOpportunity, Profile, Skill
from tracker.screening import ScreeningInputError

FIXTURE = Path(__file__).parent / "fixtures" / "sample_posts.json"
POST_URL = "https://www.linkedin.com/feed/update/urn:li:activity:{}/"


def hiring_post(n, title="Python Developer", company="Acme Labs", extra="Experience: 2-4 years"):
    return RawPost(
        text=f"We're hiring a {title} at {company}!\n{extra}\nPython and Django. Send your CV to jobs{n}@acme.example",
        url=POST_URL.format(7300000000000000000 + n),
        author_name="Priya Sharma",
    )


class CollectorTestCase(TestCase):
    def setUp(self):
        self.profile = Profile.objects.create(
            name="Jane", target_roles="Python Developer, Backend Engineer",
            experience_years=3, max_acceptable_experience_years=6,
        )
        for name in ("Python", "Django", "React"):
            Skill.objects.create(name=name, profile=self.profile)

    def collect_fixture(self, **kwargs):
        return run_collection(JsonFilePostSource(FIXTURE), **kwargs)


class FixtureCollectionTests(CollectorTestCase):
    def test_fixture_run_creates_and_screens_jobs(self):
        result = self.collect_fixture()
        self.assertEqual(result.counts, {"created": 5, "existing": 0, "skipped": 1, "invalid": 1,
                                         "failed": 0, "found": 7})
        self.assertIsNone(result.source_error)
        self.assertEqual(result.profile_id, self.profile.id)
        self.assertEqual(result.keywords, ["Python Developer", "Backend Engineer"])

        by_index = {o.index: o for o in result.outcomes}
        self.assertEqual(by_index[0].screening_outcome, "SHORTLISTED")
        self.assertEqual(by_index[1].screening_outcome, "SHORTLISTED")
        self.assertEqual(by_index[2].status, "skipped")
        self.assertEqual(by_index[3].screening_outcome, "NEEDS_REVIEW")   # experience unknown
        self.assertEqual(by_index[4].screening_outcome, "REJECTED")       # 10+ years > 6
        self.assertEqual(by_index[5].screening_outcome, "REJECTED")       # Rust: no keyword
        self.assertEqual(by_index[6].status, "invalid")

        jobs = {j.id: j for j in JobOpportunity.objects.all()}
        self.assertEqual(set(jobs), set(result.job_ids))
        first = jobs[by_index[0].job_id]
        self.assertEqual(first.source, "LINKEDIN")
        self.assertEqual(first.source_url, POST_URL.format(7200000000000000001))  # tracking params removed
        self.assertEqual(first.title, "Senior Python Developer")
        self.assertEqual(first.company.name, "Acme Labs")
        self.assertEqual(first.location, "Bengaluru (Hybrid)")
        self.assertEqual(first.required_experience_text, "3-5 years")
        self.assertIsNotNone(first.posted_date)
        self.assertEqual(first.screening_details["profile_id"], self.profile.id)
        contact = first.contacts.get()
        self.assertEqual((contact.name, contact.email, contact.whatsapp),
                         ("Priya Sharma", "careers@acme.example", "+91 98765 43210"))

        slug_post = jobs[by_index[1].job_id]
        self.assertEqual(slug_post.source_url, POST_URL.format(7200000000000000002))
        self.assertEqual(slug_post.status, "DISCOVERED")  # screening never changes status

    def test_second_run_is_idempotent(self):
        self.collect_fixture()
        counts_before = (JobOpportunity.objects.count(), Company.objects.count(), Contact.objects.count())
        result = self.collect_fixture()
        self.assertEqual(result.counts["created"], 0)
        self.assertEqual(result.counts["existing"], 5)
        self.assertTrue(all(o.matched_by == "source_url" for o in result.outcomes if o.status == "existing"))
        self.assertEqual(
            (JobOpportunity.objects.count(), Company.objects.count(), Contact.objects.count()), counts_before
        )

    def test_nothing_applied_or_sent(self):
        self.collect_fixture()
        self.assertEqual(Application.objects.count(), 0)
        self.assertEqual(mail.outbox, [])


class DuplicateTests(CollectorTestCase):
    def test_share_link_and_feed_link_are_the_same_post(self):
        feed = hiring_post(1)
        share = RawPost(
            text=feed.text + " (edited)",
            url="https://www.linkedin.com/posts/priya_hiring-python-activity-7300000000000000001-XyZ1?utm_source=share",
        )
        result = run_collection(StaticPostSource([feed, share]))
        self.assertEqual([o.status for o in result.outcomes], ["created", "existing"])
        self.assertEqual(result.outcomes[1].matched_by, "source_url")
        self.assertEqual(JobOpportunity.objects.count(), 1)

    def test_repost_with_same_text_matches_fingerprint(self):
        original = hiring_post(1)
        repost = RawPost(text="  " + original.text.upper() + "  ", url=POST_URL.format(7399999999999999999))
        result = run_collection(StaticPostSource([original, repost]))
        self.assertEqual(result.outcomes[1].status, "existing")
        self.assertEqual(result.outcomes[1].matched_by, "jd_fingerprint")

    def test_similar_post_is_possible_duplicate_not_rejected(self):
        a = hiring_post(1, extra="Experience: 2-4 years. Team A.")
        b = hiring_post(2, extra="Experience: 2-4 years. Team B.")
        result = run_collection(StaticPostSource([a, b]))
        self.assertEqual([o.status for o in result.outcomes], ["created", "created"])
        job_b = JobOpportunity.objects.get(pk=result.outcomes[1].job_id)
        self.assertEqual(job_b.screening_outcome, "NEEDS_REVIEW")
        self.assertEqual(job_b.screening_reason, "POSSIBLE_DUPLICATE")
        self.assertEqual(Company.objects.count(), 1)  # company reused


class ErrorIsolationTests(CollectorTestCase):
    def test_invalid_payload_is_counted_not_fatal(self):
        bad_url = RawPost(text=hiring_post(1).text, url="javascript:alert(1)")
        result = run_collection(StaticPostSource([bad_url, hiring_post(2)]))
        self.assertEqual([o.status for o in result.outcomes], ["invalid", "created"])
        self.assertIn("source_url", result.outcomes[0].reason)

    def test_ingest_exception_is_isolated(self):
        real = tracker.ingestion.ingest_job
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("database hiccup")
            return real(*args, **kwargs)

        with mock.patch("collector.service.ingest_job", side_effect=flaky):
            result = run_collection(StaticPostSource([hiring_post(1), hiring_post(2, title="Django Developer")]))
        self.assertEqual([o.status for o in result.outcomes], ["failed", "created"])
        self.assertIn("database hiccup", result.outcomes[0].reason)
        self.assertEqual(JobOpportunity.objects.count(), 1)

    def test_source_error_keeps_partial_results(self):
        class BrokenSource:
            name = "broken"

            def iter_posts(self, keywords, limit):
                yield hiring_post(1)
                raise SourceError("browser crashed")

        result = run_collection(BrokenSource())
        self.assertEqual(result.source_error, "browser crashed")
        self.assertEqual(result.counts["created"], 1)


class ProfileAndInputTests(CollectorTestCase):
    def assert_source_untouched(self, source):
        self.assertEqual(source.calls, [])
        self.assertEqual(JobOpportunity.objects.count(), 0)

    def test_no_profile(self):
        Profile.objects.all().delete()
        source = StaticPostSource([hiring_post(1)])
        with self.assertRaises(ScreeningInputError):
            run_collection(source)
        self.assert_source_untouched(source)

    def test_multiple_profiles_require_explicit_choice(self):
        other = Profile.objects.create(name="Other", target_roles="Rust Engineer")
        Skill.objects.create(name="Rust", profile=other)
        source = StaticPostSource([hiring_post(1)])
        with self.assertRaises(ScreeningInputError) as ctx:
            run_collection(source)
        self.assertIn("Multiple profiles", ctx.exception.message)
        self.assert_source_untouched(source)

        result = run_collection(StaticPostSource([hiring_post(1)]), profile=other)
        job = JobOpportunity.objects.get()
        self.assertEqual(result.keywords, ["Rust Engineer"])
        self.assertEqual(job.screening_details["profile_id"], other.id)
        self.assertEqual(job.screening_reason, "NO_KEYWORD_MATCH")

    def test_profile_without_skills(self):
        Skill.objects.all().delete()
        source = StaticPostSource([hiring_post(1)])
        with self.assertRaises(ScreeningInputError):
            run_collection(source)
        self.assert_source_untouched(source)

    def test_keywords(self):
        source = StaticPostSource([])
        run_collection(source, keywords=[" django ", "", "python"])
        self.assertEqual(source.calls[0][0], ["django", "python"])

        Profile.objects.filter(pk=self.profile.pk).update(target_roles=" , ")
        source = StaticPostSource([hiring_post(1)])
        with self.assertRaises(CollectionInputError) as ctx:
            run_collection(source)
        self.assertEqual(ctx.exception.field, "keywords")
        self.assert_source_untouched(source)

    @override_settings(COLLECTOR_DEFAULT_POSTS_PER_RUN=3, COLLECTOR_MAX_POSTS_PER_RUN=5)
    def test_limits(self):
        posts = [hiring_post(n, title=f"Python Developer {n}") for n in range(10)]
        source = StaticPostSource(posts)
        result = run_collection(source)
        self.assertEqual((result.limit, len(result.outcomes), source.calls[0][1]), (3, 3, 3))
        self.assertEqual(len(run_collection(StaticPostSource(posts), limit=5).outcomes), 5)
        for bad in (0, 6, -1, "3", True):
            with self.subTest(limit=bad), self.assertRaises(CollectionInputError):
                run_collection(StaticPostSource(posts), limit=bad)

    def test_source_that_ignores_limit_is_capped(self):
        class Greedy:
            name = "greedy"

            def iter_posts(self, keywords, limit):
                for n in range(100):
                    yield hiring_post(n, title=f"Python Developer {n}")

        self.assertEqual(len(run_collection(Greedy(), limit=4).outcomes), 4)


class InProcessOnlyTests(CollectorTestCase):
    def test_no_http_and_ingest_called_directly(self):
        def no_network(*args, **kwargs):
            raise AssertionError("collector made a network request")

        with mock.patch("urllib.request.urlopen", side_effect=no_network), \
                mock.patch("http.client.HTTPConnection.request", side_effect=no_network), \
                mock.patch("collector.service.ingest_job", wraps=tracker.ingestion.ingest_job) as spy:
            result = self.collect_fixture()
        self.assertEqual(result.counts["created"], 5)
        self.assertEqual(spy.call_count, 5)  # skipped/invalid posts never reach ingestion
        profile_arg = spy.call_args.args[1]
        self.assertEqual(profile_arg, self.profile)
