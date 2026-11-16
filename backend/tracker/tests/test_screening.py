"""Unit tests for screening and ingestion helpers (Milestone 3)."""

from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase

from tracker.ingestion import jd_fingerprint, normalize_source_url
from tracker.models import (
    Company,
    Contact,
    JobOpportunity,
    JobStatus,
    Profile,
    ScreeningOutcome,
    ScreeningReason,
    Skill,
)
from tracker.screening import (
    RULES_VERSION,
    ScreeningInputError,
    match_skills,
    parse_experience,
    resolve_profile,
    screen_job,
)


def skills(*names):
    return [SimpleNamespace(name=name) for name in names]


def matched(text, *names):
    return [s.name for s in match_skills(text, skills(*names))]


class KeywordMatchingTests(SimpleTestCase):
    def test_case_insensitive(self):
        self.assertEqual(matched("Strong PYTHON and django", "Python", "Django"), ["Python", "Django"])

    def test_punctuation_sensitive_skills(self):
        text = "We use C++, C#, .NET 8 and Node.js."
        self.assertEqual(matched(text, "C++", "C#", ".NET", "Node.js"), ["C++", "C#", ".NET", "Node.js"])

    def test_c_does_not_match_inside_cpp_or_csharp(self):
        self.assertEqual(matched("C++ and C# only", "C"), [])
        self.assertEqual(matched("Embedded C, some C++", "C"), ["C"])

    def test_cpp_does_not_match_plain_c(self):
        self.assertEqual(matched("Embedded C programming", "C++"), [])

    def test_java_not_inside_javascript(self):
        self.assertEqual(matched("JavaScript developer", "Java"), [])
        self.assertEqual(matched("Java/Spring developer", "Java"), ["Java"])

    def test_dotnet_not_inside_aspnet(self):
        self.assertEqual(matched("ASP.NET Core", ".NET"), [])
        self.assertEqual(matched("ASP.NET Core", "ASP.NET"), ["ASP.NET"])

    def test_js_suffix(self):
        self.assertEqual(matched("React.js preferred", "React"), ["React"])
        self.assertEqual(matched("Node.js backend", "js"), [])

    def test_word_boundaries(self):
        self.assertEqual(matched("Golang and Google", "Go"), [])
        self.assertEqual(matched("Go, Rust", "Go"), ["Go"])

    def test_multi_word_skill_any_whitespace(self):
        self.assertEqual(matched("Machine\n  Learning engineer", "machine learning"), ["machine learning"])

    def test_regex_characters_are_literal(self):
        self.assertEqual(matched("Uses A.B testing", "A*B"), [])


class ExperienceParsingTests(SimpleTestCase):
    def parse(self, jd="", field=""):
        e = parse_experience(field, jd)
        return e.min, e.max, e.source

    def test_field_formats(self):
        self.assertEqual(self.parse(field="3-5 years"), (3.0, 5.0, "field"))
        self.assertEqual(self.parse(field="2 yrs"), (2.0, None, "field"))
        self.assertEqual(self.parse(field="5+ Years"), (5.0, None, "field"))
        self.assertEqual(self.parse(field="1.5 years"), (1.5, None, "field"))

    def test_field_wins_over_jd(self):
        self.assertEqual(
            self.parse(field="2 years", jd="8+ years of experience"), (2.0, None, "field")
        )

    def test_jd_formats(self):
        cases = {
            "5+ years of experience in Python": (5.0, None),
            "Experience: 4+ years": (4.0, None),
            "Minimum 4 years experience": (4.0, None),
            "At least 6 years of professional work": (6.0, None),
            "3 to 5 yrs exp": (3.0, 5.0),
            "Experience required: 2-4 years.": (2.0, 4.0),
        }
        for jd, expected in cases.items():
            with self.subTest(jd=jd):
                self.assertEqual(self.parse(jd)[:2], expected)

    def test_highest_minimum_applies(self):
        jd = "3+ years Python experience and 7+ years overall experience"
        self.assertEqual(self.parse(jd)[:2], (7.0, None))

    def test_unrelated_years_ignored(self):
        self.assertEqual(
            self.parse("Founded 20 years ago, we need 2+ years of hands-on experience")[:2],
            (2.0, None),
        )
        self.assertEqual(
            self.parse("We were founded 20 years ago and need 3+ years experience")[:2],
            (3.0, None),
        )
        self.assertEqual(self.parse("Serving clients for 15 years. Python wanted."), (None, None, "none"))

    def test_unknown_is_not_guessed(self):
        for jd in ("Experience preferred", "6 months experience", "Senior engineer", "", "in 2023 years of growth"):
            with self.subTest(jd=jd):
                self.assertEqual(self.parse(jd), (None, None, "none"))

    def test_upper_bound_only_leaves_minimum_unknown(self):
        self.assertEqual(self.parse("Up to 3 years experience"), (None, 3.0, "jd"))

    def test_entry_level(self):
        self.assertEqual(self.parse("Freshers welcome!"), (0.0, None, "jd"))
        self.assertEqual(self.parse("Freshers welcome, 2+ years experience a plus")[:2], (2.0, None))

    def test_implausible_numbers_ignored(self):
        self.assertEqual(self.parse(field="50 years")[0], None)


class NormalizationTests(SimpleTestCase):
    def test_linkedin_variants_collapse(self):
        expected = "https://www.linkedin.com/jobs/view/3812345678/"
        for url in (
            "https://www.linkedin.com/jobs/view/3812345678/",
            "https://www.linkedin.com/jobs/view/3812345678",
            "https://linkedin.com/jobs/view/senior-dev-at-acme-3812345678/?trk=abc&refId=x",
            "https://in.linkedin.com/jobs/view/3812345678?originalSubdomain=in",
            "https://www.linkedin.com/jobs/search/?currentJobId=3812345678&keywords=python",
        ):
            with self.subTest(url=url):
                self.assertEqual(normalize_source_url(url), expected)

    def test_generic_url(self):
        self.assertEqual(
            normalize_source_url("HTTPS://Jobs.Example.com:443/a?utm_source=x&b=2&a=1#frag"),
            "https://jobs.example.com/a?a=1&b=2",
        )
        self.assertEqual(normalize_source_url("http://example.com:8080"), "http://example.com:8080/")
        self.assertIsNone(normalize_source_url("  "))

    def test_fingerprint_ignores_case_and_whitespace(self):
        self.assertEqual(
            jd_fingerprint("Python  Developer\n\nRemote "), jd_fingerprint("python developer remote")
        )
        self.assertNotEqual(jd_fingerprint("Python developer"), jd_fingerprint("Python developers"))
        self.assertEqual(len(jd_fingerprint("x")), 64)


def make_job(title="Backend Engineer", jd="Python developer, 3+ years of experience", company=None, **extra):
    return JobOpportunity.objects.create(
        title=title, full_jd=jd, jd_fingerprint=jd_fingerprint(jd + title), company=company, **extra
    )


class ProfileResolutionTests(TestCase):
    def test_no_profile(self):
        with self.assertRaises(ScreeningInputError) as ctx:
            resolve_profile()
        self.assertIn("No profile exists", ctx.exception.message)

    def test_single_profile_auto_selected(self):
        profile = Profile.objects.create(name="A", target_roles="E")
        Skill.objects.create(name="Python", profile=profile)
        selected, selected_skills = resolve_profile()
        self.assertEqual(selected, profile)
        self.assertEqual([s.name for s in selected_skills], ["Python"])

    def test_multiple_profiles_require_explicit(self):
        a = Profile.objects.create(name="A", target_roles="E")
        b = Profile.objects.create(name="B", target_roles="E")
        Skill.objects.create(name="Go", profile=b)
        with self.assertRaises(ScreeningInputError) as ctx:
            resolve_profile()
        self.assertIn("Multiple profiles", ctx.exception.message)
        self.assertEqual(resolve_profile(b)[0], b)
        with self.assertRaises(ScreeningInputError):
            resolve_profile(a)  # no skills

    def test_profile_without_skills(self):
        Profile.objects.create(name="A", target_roles="E")
        with self.assertRaises(ScreeningInputError) as ctx:
            resolve_profile()
        self.assertEqual(ctx.exception.field, "profile")
        self.assertIn("no skills", ctx.exception.message)


class ScreenJobTests(TestCase):
    def setUp(self):
        self.profile = Profile.objects.create(
            name="Jane", target_roles="Backend", experience_years=2, max_acceptable_experience_years=5
        )
        self.python = Skill.objects.create(name="Python", profile=self.profile)
        self.django = Skill.objects.create(name="Django", profile=self.profile)
        self.skills = [self.python, self.django]

    def screen(self, job):
        screen_job(job, self.profile, self.skills)
        job.refresh_from_db()
        return job

    def test_shortlisted(self):
        job = self.screen(make_job(jd="Python and Django, 2-4 years of experience"))
        self.assertEqual(job.screening_outcome, ScreeningOutcome.SHORTLISTED)
        self.assertEqual(job.screening_reason, ScreeningReason.KEYWORD_MATCH)
        details = job.screening_details
        self.assertEqual(details["rules_version"], RULES_VERSION)
        self.assertEqual(details["profile_id"], self.profile.id)
        self.assertEqual(details["matched_keywords"], ["Python", "Django"])
        self.assertEqual(details["flags"], [])
        self.assertEqual(details["duplicate_candidates"], [])
        self.assertEqual(details["experience"]["min"], 2.0)
        self.assertEqual(details["experience"]["max"], 4.0)
        self.assertEqual((job.parsed_min_experience, job.parsed_max_experience), (2.0, 4.0))
        self.assertEqual(set(job.relevant_keywords.all()), {self.python, self.django})
        self.assertIsNotNone(job.screened_at)

    def test_no_keyword_match_rejected(self):
        job = self.screen(make_job(jd="Rust developer, 15+ years of experience"))
        self.assertEqual(job.screening_outcome, ScreeningOutcome.REJECTED)
        self.assertEqual(job.screening_reason, ScreeningReason.NO_KEYWORD_MATCH)
        self.assertEqual(job.screening_details["flags"], ["NO_KEYWORD_MATCH"])
        self.assertFalse(job.relevant_keywords.exists())

    def test_one_keyword_is_enough(self):
        job = self.screen(make_job(jd="Django role. 1 year of experience."))
        self.assertEqual(job.screening_outcome, ScreeningOutcome.SHORTLISTED)
        self.assertEqual(job.screening_details["matched_keywords"], ["Django"])

    def test_experience_above_limit_rejected(self):
        job = self.screen(make_job(jd="Python, 6+ years of experience"))
        self.assertEqual(job.screening_outcome, ScreeningOutcome.REJECTED)
        self.assertEqual(job.screening_reason, ScreeningReason.EXPERIENCE_ABOVE_LIMIT)
        self.assertEqual(job.screening_details["flags"], ["EXPERIENCE_ABOVE_LIMIT"])

    def test_experience_at_limit_and_above_actual_not_rejected(self):
        job = self.screen(make_job(jd="Python, 5+ years of experience"))
        self.assertEqual(job.screening_outcome, ScreeningOutcome.SHORTLISTED)
        self.assertTrue(job.screening_details["experience"]["above_actual_experience"])

    def test_unknown_experience_needs_review(self):
        job = self.screen(make_job(jd="Senior Python developer wanted."))
        self.assertEqual(job.screening_outcome, ScreeningOutcome.NEEDS_REVIEW)
        self.assertEqual(job.screening_reason, ScreeningReason.EXPERIENCE_UNKNOWN)
        self.assertIsNone(job.parsed_min_experience)

    def test_possible_duplicate_needs_review(self):
        acme = Company.objects.create(name="Acme")
        first = make_job(company=acme, jd="Python 2+ years experience. Team A.")
        second = self.screen(make_job(company=Company.objects.create(name=" ACME "),
                                      jd="Python 2+ years experience. Team B."))
        self.assertEqual(second.screening_outcome, ScreeningOutcome.NEEDS_REVIEW)
        self.assertEqual(second.screening_reason, ScreeningReason.POSSIBLE_DUPLICATE)
        self.assertEqual(
            second.screening_details["duplicate_candidates"],
            [{"job_id": first.id, "matched_on": ["title", "company"]}],
        )

    def test_duplicate_by_shared_contact(self):
        contact = Contact.objects.create(email="hr@acme.com")
        first = make_job(jd="Python 2+ years experience. A.")
        first.contacts.add(contact)
        second = make_job(jd="Python 2+ years experience. B.")
        second.contacts.add(contact)
        second = self.screen(second)
        self.assertEqual(second.screening_reason, ScreeningReason.POSSIBLE_DUPLICATE)
        self.assertEqual(
            second.screening_details["duplicate_candidates"][0]["matched_on"], ["title", "contact"]
        )

    def test_same_title_different_company_is_not_duplicate(self):
        make_job(company=Company.objects.create(name="Acme"), jd="Python 2+ years experience A")
        job = self.screen(make_job(company=Company.objects.create(name="Globex"),
                                   jd="Python 2+ years experience B"))
        self.assertEqual(job.screening_outcome, ScreeningOutcome.SHORTLISTED)

    def test_reason_precedence_unknown_experience_before_duplicate(self):
        acme = Company.objects.create(name="Acme")
        make_job(company=acme, jd="Python role A")
        job = self.screen(make_job(company=acme, jd="Python role B"))
        self.assertEqual(job.screening_outcome, ScreeningOutcome.NEEDS_REVIEW)
        self.assertEqual(job.screening_reason, ScreeningReason.EXPERIENCE_UNKNOWN)
        self.assertEqual(job.screening_details["flags"], ["EXPERIENCE_UNKNOWN", "POSSIBLE_DUPLICATE"])

    def test_rejection_stops_before_duplicate_check(self):
        acme = Company.objects.create(name="Acme")
        make_job(company=acme, jd="Python 9+ years experience A")
        job = self.screen(make_job(company=acme, jd="Python 9+ years experience B"))
        self.assertEqual(job.screening_reason, ScreeningReason.EXPERIENCE_ABOVE_LIMIT)
        self.assertEqual(job.screening_details["duplicate_candidates"], [])

    def test_no_keyword_takes_precedence_over_experience(self):
        job = self.screen(make_job(jd="Rust, 20+ years of experience"))
        self.assertEqual(job.screening_reason, ScreeningReason.NO_KEYWORD_MATCH)

    def test_screening_never_changes_status(self):
        for status in JobStatus.values:
            with self.subTest(status=status):
                job = make_job(jd=f"Rust only {status}", status=status)
                self.assertEqual(self.screen(job).status, status)

    def test_rescreen_updates_result(self):
        job = self.screen(make_job(jd="Kotlin, 2+ years of experience"))
        self.assertEqual(job.screening_outcome, ScreeningOutcome.REJECTED)
        first_screened_at = job.screened_at

        kotlin = Skill.objects.create(name="Kotlin", profile=self.profile)
        screen_job(job, self.profile, self.skills + [kotlin])
        job.refresh_from_db()
        self.assertEqual(job.screening_outcome, ScreeningOutcome.SHORTLISTED)
        self.assertEqual(list(job.relevant_keywords.all()), [kotlin])
        self.assertGreaterEqual(job.screened_at, first_screened_at)
