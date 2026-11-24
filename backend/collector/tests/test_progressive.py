"""Progressive background collection (Job Automation, phase 1).

Fictional posts only, offline sources only. Runs execute inline (the producer
thread still runs, but never touches the database).
"""

import tempfile
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.test import TestCase, override_settings
from django.utils import timezone

from collector import progressive
from collector.linkedin.browser import PageSnapshot
from collector.linkedin.source import LinkedInPostSource
from collector.models import CollectedPost, CollectorRun, CollectorRunStatus, DateRange
from collector.posts import RawPost, build_payload, contact_evidence, extract_emails, extract_phones, looks_truncated
from collector.progressive import CollectionRequestError, date_window, execute_run, start_collection
from collector.sources import SourceError, StaticPostSource
from tracker.models import JobOpportunity, Profile, Skill
from tracker.tests.helpers import make_user

from .linkedin_pages import post_html, search_page, urn_for

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=dt_timezone.utc)


def job_post(n, *, posted_at=None, truncated=False, text=None, url=True):
    body = text or (
        f"We're hiring a Python Backend Developer at Fictional Co {n}!\n"
        f"Location: Pune\nExperience: 2-4 years\nPython, Django and PostgreSQL.\n"
        f"Send your CV to hr{n}@fictional-co{n}.example or WhatsApp +91 98{n:08d}"
    )
    return RawPost(
        text=body + ("…" if truncated else ""),
        url=f"https://www.linkedin.com/feed/update/urn:li:activity:{7300000000000000000 + n}/" if url else None,
        author_name=f"Recruiter {n}", author_headline=f"Talent Partner at Fictional Co {n}",
        posted_at=posted_at, truncated=truncated,
    )


def chatter(n):
    return RawPost(text=f"Grateful for an amazing team offsite number {n}! Thanks everyone for coming along.",
                   url=f"https://www.linkedin.com/feed/update/urn:li:activity:{7400000000000000000 + n}/")


class ProgressiveTestCase(TestCase):
    def setUp(self):
        self.user = make_user("maya-test")
        self.profile = Profile.objects.create(owner=self.user, name="Maya", target_roles="Python Developer",
                                              experience_years=3, max_acceptable_experience_years=6)
        for skill in ("Python", "Django"):
            Skill.objects.create(name=skill, profile=self.profile)
        self.launch = mock.patch.object(progressive, "launch", side_effect=lambda fn: fn())
        self.launch.start()
        self.addCleanup(mock.patch.stopall)

    def collect(self, posts_or_source, target, **kwargs):
        source = posts_or_source if hasattr(posts_or_source, "name") else StaticPostSource(posts_or_source)
        run = start_collection(source, owner=kwargs.pop("owner", self.user), profile=kwargs.pop("profile", None),
                               query=kwargs.pop("query", "Python Backend Developer"), target=target, **kwargs)
        run.refresh_from_db()
        return run


class QuantityTests(ProgressiveTestCase):
    def test_more_than_ten_jobs(self):
        run = self.collect([job_post(n) for n in range(1, 31)], target=25)
        self.assertEqual(run.status, CollectorRunStatus.SUCCEEDED)
        self.assertEqual(run.counts["created"], 25)
        self.assertEqual(run.stop_reason, "target_reached")
        self.assertEqual(JobOpportunity.objects.filter(owner=self.user).count(), 25)

    def test_fifty_jobs_among_other_posts(self):
        posts = []
        for n in range(1, 81):
            posts.append(job_post(n))
            if n % 2 == 0:
                posts.append(chatter(n))
        run = self.collect(posts, target=50)
        self.assertEqual(run.counts["created"], 50)
        self.assertEqual(run.counts["skipped"], 24)  # non-job posts examined on the way
        self.assertEqual(run.counts["found"], 74)
        self.assertEqual(CollectedPost.objects.filter(run=run).count(), 74)
        self.assertEqual(len(run.job_ids), 50)

    def test_hundred_jobs(self):
        run = self.collect([job_post(n) for n in range(1, 151)], target=100)
        self.assertEqual(run.counts["created"], 100)
        self.assertEqual(run.counts["found"], 100)  # stopped as soon as the target was reached
        self.assertEqual(run.status, CollectorRunStatus.SUCCEEDED)

    def test_source_exhausted_before_target(self):
        run = self.collect([job_post(n) for n in range(1, 8)], target=200)
        self.assertEqual(run.status, CollectorRunStatus.SUCCEEDED)
        self.assertEqual(run.stop_reason, "exhausted")
        self.assertEqual(run.counts["created"], 7)
        self.assertEqual(run.stage, "Done: no more matching posts")

    def test_duplicates_do_not_count_toward_the_target(self):
        self.collect([job_post(n) for n in range(1, 4)], target=3)
        posts = [job_post(n) for n in range(1, 4)] + [job_post(n) for n in range(4, 9)]
        run = self.collect(posts, target=5)
        self.assertEqual(run.counts["existing"], 3)
        self.assertEqual(run.counts["created"], 5)
        self.assertEqual(JobOpportunity.objects.count(), 8)
        statuses = list(CollectedPost.objects.filter(run=run).values_list("status", flat=True))
        self.assertEqual(statuses, ["existing"] * 3 + ["created"] * 5)

    @override_settings(COLLECTOR_MIN_POSTS_EXAMINED=20, COLLECTOR_POSTS_EXAMINED_PER_JOB=2)
    def test_post_budget_stops_a_run_of_mostly_non_job_posts(self):
        run = self.collect([chatter(n) for n in range(1, 60)], target=5)
        self.assertEqual(run.stop_reason, "examined_limit")
        self.assertEqual(run.counts["found"], 20)

    def test_time_limit(self):
        ticks = iter(range(0, 10**6, 400))  # each check advances 400 s
        with override_settings(COLLECTOR_MAX_RUN_MINUTES=1):
            run = CollectorRun.objects.create(owner=self.user, trigger="api", source="static", profile=self.profile,
                                              keywords=["Python"], limit=100, search_query="Python")
            execute_run(run.pk, StaticPostSource([job_post(n) for n in range(1, 50)]), clock=lambda: next(ticks))
        run.refresh_from_db()
        self.assertEqual(run.stop_reason, "time_limit")
        self.assertLess(run.counts["created"], 49)
        self.assertEqual(run.status, CollectorRunStatus.SUCCEEDED)

    def test_progress_is_saved_while_running(self):
        seen = []
        original = progressive._Run.save

        def spy(state, stage=None, **extra):
            original(state, stage=stage, **extra)
            seen.append(CollectorRun.objects.get(pk=state.run.pk).counts["created"])

        with mock.patch.object(progressive._Run, "save", spy):
            self.collect([job_post(n) for n in range(1, 6)], target=5)
        self.assertEqual(seen[-1], 5)
        self.assertTrue({1, 2, 3, 4} <= set(seen))  # counts were stored after each post


class CancellationTests(ProgressiveTestCase):
    def test_cancel_stops_the_run_and_keeps_what_was_collected(self):
        calls = {"n": 0}

        def cancel_after_three(run_id):
            calls["n"] += 1  # checked after each post
            return calls["n"] >= 3

        with mock.patch.object(progressive, "_cancel_requested", side_effect=cancel_after_three):
            run = self.collect([job_post(n) for n in range(1, 60)], target=50)
        self.assertEqual(run.status, CollectorRunStatus.CANCELLED)
        self.assertEqual(run.stop_reason, "cancelled")
        self.assertEqual(run.counts["created"], 3)
        self.assertEqual(JobOpportunity.objects.count(), 3)
        self.assertEqual(run.stage, "Cancelled")
        self.assertIsNotNone(run.finished_at)

    def test_request_cancel_only_affects_running_runs(self):
        run = CollectorRun.objects.create(owner=self.user, trigger="api", source="x", status="SUCCEEDED")
        progressive.request_cancel(run)
        run.refresh_from_db()
        self.assertFalse(run.cancel_requested)


class InterruptionTests(ProgressiveTestCase):
    @override_settings(COLLECTOR_HEARTBEAT_STALE_SECONDS=60)
    def test_stale_heartbeat_marks_interrupted_and_keeps_progress(self):
        beat = timezone.now() - timedelta(minutes=3)
        run = CollectorRun.objects.create(owner=self.user, trigger="api", source="linkedin", limit=100,
                                          search_query="Python", heartbeat_at=beat, counts={"created": 40})
        alive = CollectorRun.objects.create(owner=self.user, trigger="api", source="linkedin",
                                            heartbeat_at=timezone.now())
        progressive.reconcile_interrupted()
        run.refresh_from_db()
        alive.refresh_from_db()
        self.assertEqual(run.status, CollectorRunStatus.INTERRUPTED)
        self.assertEqual(run.finished_at, beat)
        self.assertEqual(run.counts, {"created": 40})
        self.assertIn("continue", run.error)
        self.assertEqual(alive.status, CollectorRunStatus.RUNNING)

    def test_crashing_source_is_recorded(self):
        class Crashing:
            name = "static"

            def stream_posts(self, keywords, **kwargs):
                yield job_post(1)
                raise RuntimeError("segfault-ish")

        with self.assertLogs("collector", "ERROR"):
            run = self.collect(Crashing(), target=5)
        self.assertEqual(run.status, CollectorRunStatus.PARTIAL)
        self.assertEqual(run.counts["created"], 1)
        self.assertIn("unexpectedly", run.error)

    def test_source_error_after_posts(self):
        class Failing:
            name = "linkedin"

            def stream_posts(self, keywords, **kwargs):
                yield job_post(1)
                yield job_post(2)
                raise SourceError("LinkedIn is showing a security check.")

        run = self.collect(Failing(), target=5)
        self.assertEqual(run.status, CollectorRunStatus.PARTIAL)
        self.assertEqual(run.error, "LinkedIn is showing a security check.")
        self.assertEqual(run.counts["created"], 2)


class DateFilterTests(ProgressiveTestCase):
    def test_date_window_mapping(self):
        with mock.patch("django.utils.timezone.now", return_value=NOW):
            self.assertEqual(date_window("any").linkedin_filter, None)
            self.assertEqual(date_window("past-24h").linkedin_filter, "past-24h")
            self.assertEqual(date_window("past-24h").start, NOW - timedelta(days=1))
            self.assertEqual(date_window("past-week").linkedin_filter, "past-week")
            self.assertEqual(date_window("past-month").linkedin_filter, "past-month")
            # Custom: the narrowest LinkedIn filter that covers the range.
            self.assertEqual(date_window("custom", date(2026, 11, 23)).linkedin_filter, "past-24h")
            self.assertEqual(date_window("custom", date(2026, 11, 18)).linkedin_filter, "past-week")
            self.assertEqual(date_window("custom", date(2026, 11, 1), date(2026, 11, 10)).linkedin_filter,
                             "past-month")
            self.assertIsNone(date_window("custom", date(2026, 6, 1)).linkedin_filter)
            for args, field in [(("custom",), "date_from"), (("custom", date(2026, 12, 1)), "date_from"),
                                (("custom", date(2026, 11, 10), date(2026, 11, 1)), "date_to"),
                                (("yesterday",), "date_range")]:
                with self.subTest(args=args), self.assertRaises(CollectionRequestError) as ctx:
                    date_window(*args)
                self.assertEqual(ctx.exception.field, field)

    def test_custom_range_keeps_only_posts_inside_it(self):
        def at(day):
            return datetime(2026, 11, day, 10, 0, tzinfo=dt_timezone.utc)

        posts = [job_post(1, posted_at=at(20)), job_post(2, posted_at=at(12)), job_post(3, posted_at=at(8)),
                 job_post(4, posted_at=None), job_post(5, posted_at=at(15))]
        with mock.patch("django.utils.timezone.now", return_value=NOW):
            run = self.collect(posts, target=10, date_range=DateRange.CUSTOM,
                               date_from=date(2026, 11, 10), date_to=date(2026, 11, 16))
        items = dict(CollectedPost.objects.filter(run=run).values_list("position", "status"))
        self.assertEqual(items, {0: "out_of_range", 1: "created", 2: "out_of_range", 3: "created", 4: "created"})
        self.assertEqual(run.counts["out_of_range"], 2)

    def test_target_counts_jobs_ready_to_apply(self):
        # "3 jobs" = 3 jobs the user can apply to: posts without a contact or cut off are
        # still saved, but collection goes on until 3 have a contact and a full description.
        no_contact = "We're hiring a Python Backend Developer at Fictional Co {n}!\nLocation: Pune\nPython and Django."
        posts = [job_post(1), job_post(2, text=no_contact.format(n=2)), job_post(3, truncated=True),
                 job_post(4), job_post(5, text=no_contact.format(n=5)), job_post(6), job_post(7)]
        run = self.collect(posts, target=3)
        self.assertEqual(run.stop_reason, "target_reached")
        self.assertEqual(run.counts["ready"], 3)
        self.assertEqual(run.counts["created"], 6)  # 1, 2, 3, 4, 5, 6 saved; 7 not needed
        from collector.api import done_towards_target

        self.assertEqual(done_towards_target(run), 3)  # "remaining" and "continue" use this

    def test_many_older_posts_in_a_row_end_the_run(self):
        old = NOW - timedelta(days=10)
        posts = [job_post(1, posted_at=NOW)] + [job_post(n, posted_at=old) for n in range(2, 40)]
        with mock.patch("django.utils.timezone.now", return_value=NOW):
            run = self.collect(posts, target=30, date_range=DateRange.PAST_WEEK)
        self.assertEqual(run.stop_reason, "exhausted")
        self.assertEqual(run.counts["created"], 1)
        self.assertEqual(run.counts["out_of_range"], progressive.OLDER_IN_A_ROW_TO_STOP)

    def test_linkedin_filter_is_passed_to_the_source(self):
        source = StaticPostSource([])
        self.collect(source, target=5, date_range=DateRange.PAST_24H)
        self.assertEqual(source.calls[0][1]["date_posted"], "past-24h")


class ExtractionTests(ProgressiveTestCase):
    def test_truncated_post_is_saved_as_incomplete_and_completed_later(self):
        run = self.collect([job_post(1, truncated=True)], target=1)
        job = JobOpportunity.objects.get()
        self.assertFalse(job.jd_complete)
        self.assertTrue(job.extraction["truncated"])
        self.assertTrue(CollectedPost.objects.get(run=run).truncated)
        # The same post collected again, this time fully expanded: the job is completed.
        self.collect([job_post(1)], target=1)
        job.refresh_from_db()
        self.assertTrue(job.jd_complete)
        self.assertFalse(job.full_jd.endswith("…"))
        self.assertEqual(JobOpportunity.objects.count(), 1)

    def test_user_pasted_description_is_never_overwritten(self):
        self.collect([job_post(1, truncated=True)], target=1)
        job = JobOpportunity.objects.get()
        job.full_jd = "My own pasted description"
        job.extraction = {**job.extraction, "edited_fields": ["full_jd"]}
        job.save()
        self.collect([job_post(1)], target=1)
        job.refresh_from_db()
        self.assertEqual(job.full_jd, "My own pasted description")

    def test_original_post_recruiter_and_evidence_are_kept(self):
        self.collect([job_post(7)], target=1)
        job = JobOpportunity.objects.get()
        self.assertEqual(job.recruiter_name, "Recruiter 7")
        self.assertEqual(job.recruiter_headline, "Talent Partner at Fictional Co 7")
        self.assertEqual(job.source_post_text, job.full_jd)
        kinds = {(c["kind"], c["value"]) for c in job.extraction["contacts"]}
        self.assertEqual(kinds, {("email", "hr7@fictional-co7.example"), ("whatsapp", "+91 9800000007")})
        for item in job.extraction["contacts"]:
            self.assertIn(item["value"], item["evidence"])  # the line it came from
        contact = job.contacts.get()
        self.assertEqual((contact.email, contact.whatsapp), ("hr7@fictional-co7.example", "+91 9800000007"))

    def test_missing_contact_details_are_not_invented(self):
        text = ("We're hiring a Python Backend Developer at Fictional Co!\nLocation: Remote\n"
                "Apply through the careers page of our website.")
        self.collect([job_post(1, text=text)], target=1)
        job = JobOpportunity.objects.get()
        self.assertEqual(job.contacts.count(), 0)
        self.assertEqual(job.extraction["contacts"], [])

    def test_invalid_contact_details_are_ignored(self):
        text = ("We're hiring a Python Developer!\nDrive date: 2026-11-28 10:30 or 28.11.2026 11.00\n"
                "Logo: banner@2x.png\nCall 0000000000 or 99999 99999\nSalary 10-15 LPA")
        self.assertEqual(extract_emails(text), [])
        self.assertEqual(extract_phones(text), ([], []))
        self.assertEqual(contact_evidence(text), [])

    def test_valid_numbers_with_country_codes(self):
        phones, whatsapp = extract_phones("Call +91-98765-43210\nWhatsApp: (+44) 7700 900123")
        self.assertEqual(phones, ["+91-98765-43210"])
        self.assertEqual(whatsapp, ["(+44) 7700 900123"])

    def test_truncation_markers(self):
        for text in ("Great role…", "Great role... see more", "Great role …see more"):
            self.assertTrue(looks_truncated(text), text)
        for text in ("Great role. Apply now!", "Salary: 10…15 LPA is fine"):
            self.assertFalse(looks_truncated(text), text)
        self.assertFalse(build_payload(job_post(1, truncated=True))["jd_complete"])


class IsolationTests(ProgressiveTestCase):
    def test_runs_and_jobs_belong_to_the_collecting_user(self):
        other = make_user("sam-test")
        Skill.objects.create(name="Python", profile=Profile.objects.create(owner=other, name="Sam",
                                                                           target_roles="Developer"))
        mine = self.collect([job_post(1), job_post(2)], target=2)
        theirs = self.collect([job_post(1), job_post(2)], target=2, owner=other)
        self.assertEqual(theirs.counts["created"], 2)  # the same posts, saved independently
        self.assertEqual(JobOpportunity.objects.filter(owner=self.user).count(), 2)
        self.assertEqual(JobOpportunity.objects.filter(owner=other).count(), 2)
        self.assertEqual(mine.owner, self.user)
        self.assertEqual(theirs.owner, other)
        self.assertTrue(all(j.owner == other for j in JobOpportunity.objects.filter(pk__in=theirs.job_ids)))

    def test_another_users_profile_cannot_be_used(self):
        other = make_user("sam-test")
        theirs = Profile.objects.create(owner=other, name="Sam", target_roles="Developer")
        with self.assertRaises(Exception):
            self.collect([job_post(1)], target=1, profile=theirs)
        self.assertFalse(CollectorRun.objects.exists())


# ---------------------------------------------------------------------------
# LinkedIn source, streaming: pagination, expansion, termination (fake browser)
# ---------------------------------------------------------------------------

class PagedBrowser:
    """Results that grow as the user scrolls: pages[i] is the page after i scrolls/clicks."""

    def __init__(self, pages, show_more_from=None, expanded_after=0):
        self.pages, self.show_more_from = pages, show_more_from
        self.expanded_after = expanded_after
        self.loads, self.events, self.expansions = 0, [], 0

    def __enter__(self):
        self.events.append("enter")
        return self

    def __exit__(self, *exc):
        self.events.append("exit")
        return False

    def _page(self):
        html = self.pages[min(self.loads, len(self.pages) - 1)]
        if callable(html):
            html = html(self.expansions > self.expanded_after)
        return PageSnapshot(url=self.url, html=html)

    def goto(self, url):
        self.url = url
        self.events.append(("goto", parse_qs(urlsplit(url).query)))
        return self._page()

    def snapshot(self):
        return self._page()

    def expand_see_more(self, max_clicks=15):
        self.expansions += 1
        return 0

    def scroll(self):
        self.events.append("scroll")
        self.loads += 1

    def load_more_results(self):
        if self.show_more_from is not None and self.loads >= self.show_more_from:
            self.events.append("show_more")
            self.loads += 1
            return True
        return False


def li_post(n, see_more=False):
    text = (f"We're hiring a Python Backend Developer at Fictional Co {n}!\nExperience: 2-4 years\n"
            f"Send your CV to hr{n}@fictional-co{n}.example")
    return post_html(urn_for(seq=n), text, see_more=see_more)


def page_with(count):
    return search_page(*(li_post(n) for n in range(1, count + 1)))


class LinkedInStreamingTests(ProgressiveTestCase):
    def source(self, browser, **kwargs):
        tmp = tempfile.mkdtemp()
        return LinkedInPostSource(browser_factory=lambda: browser, max_scrolls=kwargs.get("max_scrolls", 50),
                                  debug_dir=Path(tmp), debug_files_kept=1)

    def test_scrolls_through_many_pages_until_the_target(self):
        browser = PagedBrowser([page_with(5 * (i + 1)) for i in range(30)])
        run = self.collect(self.source(browser), target=60)
        self.assertEqual(run.counts["created"], 60)
        self.assertEqual(run.stop_reason, "target_reached")
        self.assertGreaterEqual(browser.events.count("scroll"), 11)
        self.assertEqual(browser.events[-1], "exit")  # the browser was closed

    def test_uses_show_more_results_when_offered(self):
        browser = PagedBrowser([page_with(4 * (i + 1)) for i in range(10)], show_more_from=1)
        run = self.collect(self.source(browser), target=20)
        self.assertEqual(run.counts["created"], 20)
        self.assertIn("show_more", browser.events)

    def test_exhausted_results_end_the_run(self):
        browser = PagedBrowser([page_with(3), page_with(6), page_with(6)])
        run = self.collect(self.source(browser), target=100)
        self.assertEqual(run.counts["created"], 6)
        self.assertEqual(run.stop_reason, "exhausted")
        # 1 new page + 3 scrolls without anything new, then stop.
        self.assertEqual(browser.events.count("scroll"), 4)

    def test_date_filter_is_in_the_search_url(self):
        browser = PagedBrowser([page_with(2)])
        self.collect(self.source(browser), target=2, date_range=DateRange.PAST_WEEK, query="Python Backend")
        goto = next(e for e in browser.events if isinstance(e, tuple))[1]
        self.assertEqual(goto["datePosted"], ['"past-week"'])
        self.assertEqual(goto["keywords"], ["Python Backend"])
        self.assertEqual(goto["sortBy"], ['"date_posted"'])

    def test_collapsed_post_gets_a_second_expansion_then_is_flagged(self):
        # Post 1 stays collapsed ("…see more" still there); post 2 opened normally.
        page = search_page(li_post(1, see_more=True), li_post(2))
        browser = PagedBrowser([page])
        self.collect(self.source(browser), target=2)
        self.assertGreaterEqual(browser.expansions, 2)  # an extra pass for the collapsed post
        incomplete = JobOpportunity.objects.get(jd_complete=False)
        self.assertIn("Fictional Co 1", incomplete.full_jd)
        self.assertTrue(JobOpportunity.objects.get(jd_complete=True))

    def test_post_that_opens_on_the_second_pass_is_complete(self):
        page = lambda expanded: search_page(li_post(1, see_more=not expanded))  # noqa: E731
        browser = PagedBrowser([page], expanded_after=1)
        self.collect(self.source(browser), target=1)
        self.assertTrue(JobOpportunity.objects.get().jd_complete)
