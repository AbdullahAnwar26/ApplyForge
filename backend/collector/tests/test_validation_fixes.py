"""
Regression tests for the four problems found by the live validation run:

1. commentary expansion ("… more") - structural, language-neutral control
   identification; never action controls; wait for asynchronous expansion;
2. headline equal to the author for organisation (/company/) authors;
3. personal-author header variants (header inside the author link, edited labels);
4. generic hiring slogans used as job titles; lowercase titles; explicit companies.

Offline; synthetic, fictional content only.
"""

import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.test import SimpleTestCase, TestCase
from lxml import html as lxml_html

from collector.linkedin import selectors as S
from collector.linkedin.browser import LinkedInBrowser
from collector.linkedin.extract import extract_posts
from collector.linkedin.source import LinkedInPostSource
from collector.posts import (
    RawPost,
    build_payload,
    extract_company,
    extract_experience_text,
    extract_location,
    extract_title,
    extract_work_mode,
    is_generic_title,
)
from collector.service import run_collection
from tracker.models import JobOpportunity, Profile, Skill

from .linkedin_pages import dvn_post_html, sdui_search_page
from .test_linkedin_browser import CHROMIUM, QUIET_ARGS, SEARCH_PREFIX
from .test_linkedin_source import FakeBrowser

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def matched_controls(html):
    """Which buttons the see-more selector would click (by a data-test label)."""
    root = lxml_html.fromstring(html)
    return sorted(b.get("data-test") or "?" for b in root.xpath(S.SEE_MORE_BUTTON))


def org_post_html(name="Example Labs", subtitle="12,345 followers", relative="2m", seed="o", actor=True,
                  text="We're hiring backend engineers to build our platform.\nThe details: Onsite in Seoul."):
    actor_attr = ' data-view-name="feed-actor"' if actor else ""
    return f"""
<div role="listitem"><div data-view-name="feed-full-update"><div class="z{seed}">
 <a data-view-name="feed-actor-image" href="https://www.linkedin.com/company/example-labs/"><img></a>
 <a{actor_attr} href="https://www.linkedin.com/company/example-labs/"><div><p><span>{name}</span></p></div></a>
 <div><p><span>{name}</span></p></div>
 <div><p><span>{subtitle}</span></p></div>
 <div><p><span>{relative} • </span></p></div>
 <button data-view-name="relationship-building-button"><span>Follow</span></button>
 <p data-view-name="feed-commentary"><span>{text.replace(chr(10), '<br>')}</span></p>
 <div><button data-view-name="reaction-button">Like</button></div>
</div></div></div>"""


def header_in_link_post_html(seed="p", relative="1h • Edited •",
                             headline="Senior Recruiter at Fictional Talent | Hiring engineers"):
    """Personal author whose WHOLE header (name, degree, headline, time) is inside the /in/ link."""
    return f"""
<div role="listitem"><div data-view-name="feed-full-update"><div class="z{seed}">
 <a data-view-name="feed-actor-image" href="https://www.linkedin.com/in/fictional-person/"><img></a>
 <a data-view-name="feed-actor" href="https://www.linkedin.com/in/fictional-person/">
  <div><p><span>Fictional Person</span></p><p><span>• 2nd</span></p>
  <p><span>{headline}</span></p><p><span>{relative}</span></p></div>
 </a>
 <p data-view-name="feed-commentary"><span>We're hiring a Django Developer! Mail jobs@fictional.test</span></p>
 <div><button data-view-name="reaction-button">Like</button></div>
</div></div></div>"""


# ---------------------------------------------------------------------------
# 1. Commentary expansion
# ---------------------------------------------------------------------------

class SeeMoreSelectorTests(SimpleTestCase):
    def post(self, inner, seed="a"):
        return (f'<div data-view-name="feed-full-update"><div class="z{seed}">'
                f'<a data-view-name="feed-actor" href="/in/x"><p>Name</p></a><p>3h •</p>{inner}'
                '<div><button data-test="react" data-view-name="reaction-button">Like</button>'
                '<button data-test="comment" data-view-name="feed-comment-button">Comment</button>'
                '<button data-test="share" data-view-name="feed-share-button">Repost</button>'
                '<button data-test="menu" data-view-name="feed-control-menu" aria-haspopup="true"></button></div>'
                "</div></div>")

    def test_inside_commentary(self):
        html = self.post('<p data-view-name="feed-commentary"><span>Text<button data-test="more" aria-hidden="true">'
                         "<span>…</span><span>more</span></button></span></p>")
        self.assertEqual(matched_controls(html), ["more"])

    def test_outside_commentary_hook_but_inside_the_post_paragraph(self):
        html = self.post('<p><span data-view-name="feed-commentary">Text</span>'
                         '<button data-test="more" aria-hidden="true">… more</button></p>')
        self.assertEqual(matched_controls(html), ["more"])

    def test_language_neutral(self):
        for label in ("… mehr", "… plus", "…", "… さらに表示"):
            with self.subTest(label=label):
                html = self.post(f'<p data-view-name="feed-commentary">Text<button data-test="more" '
                                 f'aria-hidden="true">{label}</button></p>')
                self.assertEqual(matched_controls(html), ["more"])

    def test_multiple_posts_each_matched_once(self):
        html = "".join(self.post(f'<p data-view-name="feed-commentary">T{i}<button data-test="more{i}" '
                                 'aria-hidden="true">… more</button></p>', seed=str(i)) for i in range(3))
        self.assertEqual(matched_controls(html), ["more0", "more1", "more2"])

    def test_absent_button_and_unrelated_controls_never_matched(self):
        html = self.post('<p data-view-name="feed-commentary">No more to read here</p>'
                         '<p><button data-test="load-comments" data-view-name="feed-comment-button">Load more comments'
                         '</button></p>'
                         '<p><button data-test="menu-in-p" aria-haspopup="true" aria-hidden="true">…</button></p>'
                         '<p><button data-test="expanded" aria-hidden="true" aria-expanded="true">… less</button></p>'
                         '<p><button data-test="plain">Show more</button></p>'
                         '<div><button data-test="outside-p" aria-hidden="true">… more</button></div>')
        self.assertEqual(matched_controls(html), [])

    def test_paragraph_buttons_outside_posts_never_matched(self):
        html = ('<aside><p>Sidebar<button data-test="sidebar" aria-hidden="true">… more</button></p></aside>'
                + self.post('<p data-view-name="feed-commentary">Text</p>'))
        self.assertEqual(matched_controls(html), [])


ASYNC_JS = """
<script>
window.__actionClicks = 0;
document.addEventListener('click', function (e) {
  if (e.target.closest('button[data-view-name]')) { window.__actionClicks++; return; }
  var b = e.target.closest('p button');
  if (!b) return;
  var p = b.closest('p');
  b.remove();
  p.querySelector('span').insertAdjacentText('beforeend', '…');   // placeholder while "loading"
  setTimeout(function () {
    p.querySelector('span').textContent = p.getAttribute('data-full');
  }, 800);
});
</script>"""


def async_post(i):
    short = f"Post {i}: we're hiring a Python Developer"
    full = f"Post {i}: we're hiring a Python Developer. Full text with 2+ years of experience. Mail p{i}@fictional.test"
    return (f'<div role="listitem"><div data-view-name="feed-full-update"><div class="z{i}">'
            f'<a data-view-name="feed-actor" href="https://www.linkedin.com/in/p{i}/"><p>Person {i}</p></a>'
            f'<div><p>Recruiter {i}</p></div><div><p>{i + 1}m •</p></div>'
            f'<p data-view-name="feed-commentary" data-full="{full}"><span>{short}'
            '<button aria-hidden="true"><span>…</span><span> more</span></button></span></p>'
            '<div><button data-view-name="reaction-button">Like</button>'
            '<button data-view-name="feed-control-menu" aria-haspopup="true"></button></div>'
            "</div></div></div>")


@unittest.skipUnless(CHROMIUM, "Playwright Chromium is not installed")
class AsyncExpansionRealChromiumTests(TestCase):
    """Real Chromium, offline. The expanded text arrives 800 ms after the click (as a
    network-loaded expansion would); before the fix the snapshot captured the "…"
    placeholder, and positional clicking skipped every other post."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        profile = Profile.objects.create(name="Test", target_roles="python developer", max_acceptable_experience_years=9)
        Skill.objects.create(name="Python", profile=profile)
        self.body = sdui_search_page(*(async_post(i) for i in range(3)), extra_body=ASYNC_JS)

    def route(self, route):
        if route.request.url.startswith(SEARCH_PREFIX):
            route.fulfill(status=200, content_type="text/html", body=self.body)
        else:
            route.abort()

    def test_all_posts_fully_expanded_and_no_action_controls_clicked(self):
        browser = LinkedInBrowser(profile_dir=Path(self.tmp) / "profile", headless=True, timeout_seconds=10,
                                  delay_seconds=0, launch_args=QUIET_ARGS, route_handler=self.route)
        clicks = {}

        real_expand = LinkedInBrowser.expand_see_more

        def spy(self_, *args, **kwargs):
            count = real_expand(self_, *args, **kwargs)
            clicks["count"] = count
            clicks["action"] = self_.page.evaluate("window.__actionClicks")
            return count

        LinkedInBrowser.expand_see_more = spy
        self.addCleanup(setattr, LinkedInBrowser, "expand_see_more", real_expand)
        source = LinkedInPostSource(browser_factory=lambda: browser, max_scrolls=0, debug_dir=Path(self.tmp) / "d")
        result = run_collection(source, limit=3)
        self.assertIsNone(result.source_error)
        self.assertEqual(clicks, {"count": 3, "action": 0})
        texts = sorted(JobOpportunity.objects.values_list("full_jd", flat=True))
        self.assertEqual(len(texts), 3)
        for text in texts:
            self.assertIn("Full text with 2+ years of experience.", text)
            self.assertFalse(text.rstrip().endswith("…"))


# ---------------------------------------------------------------------------
# 2 + 3. Headline, organisation and personal-author layouts
# ---------------------------------------------------------------------------

class HeaderTests(SimpleTestCase):
    def one(self, post_html):
        posts = extract_posts(sdui_search_page(post_html), now=NOW)
        self.assertEqual(len(posts), 1)
        return posts[0]

    def test_organisation_author_headline_is_not_the_name(self):
        for actor in (True, False):
            with self.subTest(actor_hook=actor):
                post = self.one(org_post_html(actor=actor))
                self.assertEqual(post.author_name, "Example Labs")
                self.assertEqual(post.author_headline, "12,345 followers")
                self.assertNotEqual(post.author_headline, post.author_name)
                self.assertEqual(post.posted_label, "2m •")
                self.assertEqual(post.posted_at, NOW - timedelta(minutes=2))
                self.assertTrue(post.text.startswith("We're hiring backend engineers"))

    def test_headline_never_equals_author_even_when_only_name_repeats(self):
        post = self.one(org_post_html(subtitle="Example Labs"))   # only the name, twice
        self.assertIsNone(post.author_headline)

    def test_personal_author_standard_layout(self):
        post = self.one(dvn_post_html(author="Riya Fictional", headline="Talent Partner", relative="3h", seed="s"))
        self.assertEqual((post.author_name, post.author_headline, post.posted_label),
                         ("Riya Fictional", "Talent Partner", "3h •"))

    def test_personal_author_header_inside_the_profile_link(self):
        post = self.one(header_in_link_post_html())
        self.assertEqual(post.author_name, "Fictional Person")
        self.assertEqual(post.author_headline, "Senior Recruiter at Fictional Talent | Hiring engineers")
        self.assertEqual(post.posted_label, "1h • Edited •")
        self.assertEqual(post.posted_at, NOW - timedelta(hours=1))
        self.assertEqual(post.text, "We're hiring a Django Developer! Mail jobs@fictional.test")

    def test_headlines_starting_with_numbers_are_not_times(self):
        for headline in ("3D Artist at Fictional Studio", "5m followers", "2nd year student", "24h support lead"):
            with self.subTest(headline=headline):
                post = self.one(header_in_link_post_html(headline=headline, relative="4d"))
                self.assertEqual(post.author_headline, headline)
                self.assertEqual(post.posted_label, "4d")

    def test_missing_time_label(self):
        post = self.one(header_in_link_post_html(relative=""))
        self.assertIsNone(post.posted_label)
        self.assertIsNone(post.posted_at)
        self.assertEqual(post.author_name, "Fictional Person")
        self.assertEqual(post.text, "We're hiring a Django Developer! Mail jobs@fictional.test")

    def test_degree_marker_is_never_the_headline(self):
        post = self.one(header_in_link_post_html(headline="• 3rd+"))
        self.assertIsNone(post.author_headline)

    def test_relative_time_units(self):
        for label, delta in (("45s", timedelta(seconds=45)), ("5m", timedelta(minutes=5)),
                             ("2h • Edited", timedelta(hours=2)), ("3d", timedelta(days=3)),
                             ("1w", timedelta(weeks=1)), ("2mo •", timedelta(days=60)), ("1yr", timedelta(days=365))):
            with self.subTest(label=label):
                post = self.one(header_in_link_post_html(relative=label))
                self.assertEqual(post.posted_at, NOW - delta)

    def test_no_invented_urls(self):
        for html in (org_post_html(), header_in_link_post_html(), dvn_post_html()):
            self.assertIsNone(self.one(html).url)


# ---------------------------------------------------------------------------
# 4. Titles and companies
# ---------------------------------------------------------------------------

class TitleTests(SimpleTestCase):
    def test_generic_slogans_detected(self):
        for title in ("Immediate Joiners Required", "We're Hiring", "Urgent Opening", "Multiple Openings",
                      "Job Alert", "Freshers Required", "Now Hiring"):
            with self.subTest(title=title):
                self.assertTrue(is_generic_title(title))
        for title in ("Python Developer", "Backend Engineers", "Tech Lead", "Data Analyst Intern"):
            with self.subTest(title=title):
                self.assertFalse(is_generic_title(title))

    def test_role_preferred_over_slogan(self):
        cases = {
            "📢 Immediate Joiners Required for Python Developer role\nApply now": "Python Developer",
            "#Hiring Immediate Joiners Required | Python Backend Developer | Remote": "Python Backend Developer",
            "Immediate Joiners Required!\nWe need a Django Developer with 2+ years.": "Django Developer",
            "URGENT OPENING\nPosition: Senior Data Engineer\nLocation: Pune": "Senior Data Engineer",
            "Hiring: Python Tech Lead at Umbrella Corp": "Python Tech Lead",
        }
        for text, expected in cases.items():
            with self.subTest(text=text[:40]):
                self.assertEqual(extract_title(text)[0], expected)

    def test_lowercase_roles_capitalised_without_changing_meaning(self):
        self.assertEqual(extract_title("We're hiring backend engineers to build our platform.")[0], "Backend Engineers")
        self.assertEqual(extract_title("Role: head of engineering")[0], "Head of Engineering")
        self.assertEqual(extract_title("Role: node.js developer")[0], "node.js Developer")
        self.assertEqual(extract_title("Role: Senior iOS Developer")[0], "Senior iOS Developer")  # mixed case kept

    def test_safe_fallback_when_no_role_exists(self):
        self.assertEqual(extract_title("Immediate Joiners Required!\nSend your CV today."),
                         ("Immediate Joiners Required", "first_line"))
        self.assertEqual(extract_title("We are hiring! Freshers welcome."), ("We are hiring", "first_line"))
        self.assertEqual(extract_title("We are looking for someone to lead the team.")[1], "first_line")

    def test_existing_titles_unchanged(self):
        cases = {
            "Role: Backend Engineer\nCompany: Globex": ("Backend Engineer", "label"),
            "We're hiring a Senior Python Developer at Acme Labs! 🚀": ("Senior Python Developer", "hiring_phrase"),
            "#Hiring for Java Full Stack Developer @ TCS, Chennai": ("Java Full Stack Developer", "hiring_phrase"),
            "Hiring Now: QA Automation Engineer (Selenium)": ("QA Automation Engineer", "hiring_phrase"),
            "Looking for an experienced Data Engineer to join our team": ("Data Engineer", "hiring_phrase"),
            "Urgent opening for React Developers - immediate joiners": ("React Developers", "hiring_phrase"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text[:40]):
                self.assertEqual(extract_title(text), expected)


class CompanyTests(SimpleTestCase):
    def test_explicit_only(self):
        self.assertEqual(extract_company("Company Name: Initrode Pvt Ltd\nRole: Developer"), "Initrode Pvt Ltd")
        self.assertEqual(extract_company("Hiring Company - Globex Corp"), "Globex Corp")
        self.assertEqual(extract_company("We're hiring a Python Developer at Acme Labs in Pune"), "Acme Labs")
        self.assertEqual(extract_company("#Hiring for Java Developer @ TCS, Chennai"), "TCS")

    def test_unknown_or_non_company_values(self):
        for text in ("Immediate Joiners Required for Python Developer role", "Company: Confidential",
                     "Company: N/A", "Client: Remote", "We're hiring a Data Analyst at Remote!",
                     "Join our growing team!\nWe're hiring a Data Analyst."):
            with self.subTest(text=text[:40]):
                self.assertIsNone(extract_company(text))

    def test_author_organisation_is_not_the_hiring_company(self):
        post = extract_posts(sdui_search_page(org_post_html()), now=NOW)[0]
        self.assertEqual(post.author_name, "Example Labs")
        self.assertNotIn("company_name", build_payload(post))


class OtherFieldsPreservedTests(SimpleTestCase):
    def test_experience_location_work_mode_contacts(self):
        text = ("📢 Immediate Joiners Required\nRole: python developer\nLocation: Pune, India\n"
                "Experience: 0–1 Year\nWork from office\nSend your CV to jobs@fictional.test or call +91 98765 43210")
        payload = build_payload(RawPost(text=text, author_name="Riya Fictional"))
        self.assertEqual(payload["title"], "Python Developer")
        self.assertEqual(payload["location"], "Pune, India")
        self.assertEqual(payload["required_experience_text"], "0–1 Year")
        self.assertEqual(payload["work_mode"], "On-site")
        self.assertEqual(payload["contacts"], [{"name": "Riya Fictional", "email": "jobs@fictional.test",
                                                "phone": "+91 98765 43210"}])
        self.assertEqual((extract_location(text), extract_experience_text(text), extract_work_mode(text)),
                         ("Pune, India", "0–1 Year", "On-site"))


class DedupUnchangedTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        profile = Profile.objects.create(name="T", target_roles="python developer", max_acceptable_experience_years=9)
        Skill.objects.create(name="Python", profile=profile)
        Skill.objects.create(name="Django", profile=profile)

    def test_repost_by_another_page_matches_by_fingerprint(self):
        text = "We're hiring a Python Developer! 2+ years of experience. Mail cv@fictional.test"
        original = dvn_post_html(author="Page One", slug="one", seed="1", text=text)
        repost = org_post_html(name="Page Two", seed="2", text=text)
        pages = {"python developer": [sdui_search_page(original, repost)]}
        result = run_collection(LinkedInPostSource(browser_factory=lambda: FakeBrowser(pages), max_scrolls=0,
                                                   debug_dir=Path(self.tmp) / "d"))
        self.assertEqual([o.status for o in result.outcomes], ["created", "existing"])
        self.assertEqual(result.outcomes[1].matched_by, "jd_fingerprint")
        self.assertTrue(all(j.source_url is None for j in JobOpportunity.objects.all()))


# ---------------------------------------------------------------------------
# 5. Job-seeker posts (found by the second live validation run: 2 of 5 results)
# ---------------------------------------------------------------------------

class JobSeekerTests(SimpleTestCase):
    SEEKERS = (
        "Hi everyone! I am actively looking for new opportunities as a Full Stack / Backend Developer. "
        "Please feel free to send me a DM or connect.",
        "🚀 Open to Internship Opportunities | Computer Science Student\nCurrently looking for: Software "
        "Development Internships. Recruiters and hiring managers, I would appreciate your guidance.",
        "#OpenToWork Python developer with 2 years of experience. I'm seeking a remote role. Hire me!",
        "After 5 great years I am looking for my next role in backend engineering. Hiring managers and recruiters welcome.",
    )
    EMPLOYERS = (
        "We're hiring a Python Developer! Send your CV to jobs@fictional.test",
        "Hiring: Django Developer\nRequirements: 2+ years\nApply now via the link.",
        "I am looking for a Senior Python Developer for my client. Mail resume@fictional.test",
        "We are hiring! Recruiters and hiring managers on our team will review applications. Apply here.",
        "Open to work visas? We're hiring backend engineers in Berlin.",
    )

    def test_seekers_skipped(self):
        from collector.posts import PostSkipped, is_job_seeker_post

        for text in self.SEEKERS:
            with self.subTest(text=text[:40]):
                self.assertTrue(is_job_seeker_post(text))
                with self.assertRaises(PostSkipped):  # no hiring cue, or the seeker rule
                    build_payload(RawPost(text=text))

    def test_seeker_rule_applies_even_with_hiring_words(self):
        from collector.posts import PostSkipped, is_hiring_post

        text = ("#hiring #python I am actively looking for new opportunities as a Backend Developer. "
                "Recruiters and hiring managers, please reach out!")
        self.assertTrue(is_hiring_post(text))
        with self.assertRaisesMessage(PostSkipped, "Job-seeker post"):
            build_payload(RawPost(text=text))

    def test_employers_and_recruiters_kept(self):
        from collector.posts import is_job_seeker_post

        for text in self.EMPLOYERS:
            with self.subTest(text=text[:40]):
                self.assertFalse(is_job_seeker_post(text))
                self.assertIn("title", build_payload(RawPost(text=text)))


# ---------------------------------------------------------------------------
# 6. Header layouts confirmed by the v3 live inventory (9 posts, shapes only)
# ---------------------------------------------------------------------------

def group_post_html(seed="g", with_time=False):
    """Group post: the actor is the GROUP (link to /groups/), the person's name sits
    outside the actor followed by a degree marker, and no time label is shown."""
    time_html = '<div><div><p><span>3h</span></p></div></div>' if with_time else ""
    return f"""
<div role="listitem"><div data-view-name="feed-full-update"><div class="z{seed}">
 <h2><span>Feed post</span></h2>
 <a href="https://www.linkedin.com/groups/1234567/"><div data-view-name="feed-actor"><div><div>
  <p><span>Python Developers (Jobs and Careers)</span></p></div></div></div></a>
 <div><button data-view-name="feed-group-join-toggle"><span><span>Join</span></span></button></div>
 <div><div><p><span>Fictional Poster</span></p></div></div>
 <div><div><p><span><span><span>• 3rd+</span></span></span></p></div></div>
 <div><div><p><span>Recruiter</span></p></div></div>
 {time_html}
 <p data-view-name="feed-commentary"><span>We're hiring a Python Developer! 2+ years of experience.
  Mail jobs@fictional.test</span></p>
</div></div></div>"""


def company_no_subtitle_html(seed="c"):
    return f"""
<div role="listitem"><div data-view-name="feed-full-update"><div class="z{seed}">
 <h2><span>Feed post</span></h2>
 <a href="https://www.linkedin.com/company/fictional-co/" data-view-name="feed-actor"><div><div>
  <p><span>Fictional Co</span></p></div></div></a>
 <div><div><p><span>2m</span></p></div></div>
 <button data-view-name="edge-creation-follow-action"><span>Follow</span></button>
 <p data-view-name="feed-commentary"><span>Hiring: Backend Engineer. Mail hr@fictional.test</span></p>
</div></div></div>"""


class ObservedHeaderLayoutTests(SimpleTestCase):
    def one(self, html):
        posts = extract_posts(sdui_search_page(html), now=NOW)
        self.assertEqual(len(posts), 1)
        return posts[0]

    def test_group_post_author_is_the_person_not_the_group(self):
        post = self.one(group_post_html())
        self.assertEqual(post.author_name, "Fictional Poster")
        self.assertEqual(post.author_headline, "Recruiter")
        self.assertIsNone(post.posted_label)          # group posts show no time
        self.assertIsNone(post.posted_at)
        self.assertIn("We're hiring a Python Developer!", post.text)
        for field in (post.author_name, post.author_headline, post.text):
            self.assertNotIn("Python Developers (Jobs and Careers)", field)
            self.assertNotIn("Join", field)

    def test_group_post_with_time_still_works(self):
        post = self.one(group_post_html(with_time=True))
        self.assertEqual((post.author_name, post.author_headline, post.posted_label),
                         ("Fictional Poster", "Recruiter", "3h"))

    def test_company_post_without_subtitle(self):
        post = self.one(company_no_subtitle_html())
        self.assertEqual((post.author_name, post.author_headline, post.posted_label),
                         ("Fictional Co", None, "2m"))
        self.assertEqual(post.posted_at, NOW - timedelta(minutes=2))

    def test_person_post_layout_from_inventory(self):
        post = self.one(dvn_post_html(author="Riya Fictional", headline="Talent Partner", relative="8m", seed="q"))
        self.assertEqual((post.author_name, post.author_headline, post.posted_label),
                         ("Riya Fictional", "Talent Partner", "8m •"))

    def test_mixed_page_each_layout_correct(self):
        posts = extract_posts(sdui_search_page(group_post_html("1"), company_no_subtitle_html("2"),
                                               dvn_post_html(author="Riya Fictional", headline="Talent Partner",
                                                             seed="3")), now=NOW)
        self.assertEqual([p.author_name for p in posts], ["Fictional Poster", "Fictional Co", "Riya Fictional"])
        self.assertEqual([p.author_headline for p in posts], ["Recruiter", None, "Talent Partner"])
        self.assertTrue(all(p.url is None for p in posts))


class SecondValidationRunTests(SimpleTestCase):
    """Variants seen in the third live validation run."""

    def test_name_and_degree_in_one_paragraph(self):
        html = f"""
<div role="listitem"><div data-view-name="feed-full-update"><div class="zn">
 <h2><span>Feed post</span></h2>
 <a href="https://www.linkedin.com/groups/99/"><div data-view-name="feed-actor"><p><span>Some Group</span></p></div></a>
 <div><div><p><span>Fictional Person • 3rd+</span></p></div></div>
 <div><div><p><span>Hiring Manager at Fictional Bank</span></p></div></div>
 <div><div><p><span>4m</span></p></div></div>
 <p data-view-name="feed-commentary"><span>We're hiring a Technical Lead! Mail jobs@fictional.test</span></p>
</div></div></div>"""
        post = extract_posts(sdui_search_page(html), now=NOW)[0]
        self.assertEqual(post.author_name, "Fictional Person")
        self.assertEqual(post.author_headline, "Hiring Manager at Fictional Bank")
        self.assertEqual(post.posted_label, "4m")

    def test_all_caps_titles(self):
        self.assertEqual(extract_title("We're hiring INTERNS for our summer batch")[0], "Interns")
        self.assertEqual(extract_title("Role: SENIOR AI DEVELOPER")[0], "Senior AI Developer")
        self.assertEqual(extract_title("Role: QA ENGINEER")[0], "QA Engineer")
        self.assertEqual(extract_title("Role: SDE")[0], "SDE")
