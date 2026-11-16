"""Post-attribute inventory tests. Offline; synthetic, fictional content only.

The inventory may record attribute NAMES, value LENGTHS, other-URN TYPES and
validated public post URNs - nothing else.
"""

import json
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from collector.linkedin.diagnostics import sanitize_html
from collector.linkedin.inventory import FILE_PREFIX, build_inventory, classify_value, save_inventory
from collector.linkedin.source import LinkedInPostSource

from .linkedin_pages import HIRING_TEXT, post_html, sdui_post_html, sdui_search_page, search_page, urn_for
from .test_linkedin_source import FakeBrowser

POST = urn_for(seq=11)                    # valid public post URN (fictional)
OTHER_POST = urn_for(seq=22)
RESHARED = urn_for(seq=33, kind="ugcPost")
PROFILE_URN = "urn:li:fsd_profile:ACoAAFictionalMemberId0123456789"
MEMBER_URN = "urn:li:member:123456789"
SECRET_STRINGS = (
    "Fake Person", "Fictional Headline", "We are hiring", "hr@fictional.test", "+91 90000 00000",
    "fake-person-slug", "ACoAAFictionalMemberId0123456789", "123456789", "zzclass", "track-blob-value",
    "c2VjcmV0LWJhc2U2NC1ibG9i",
)


def page(container_attrs="", ancestors=("", "", "", ""), inner="", blocks=""):
    """One current-layout post. ancestors[0] is the parent of the container; [3] is beyond the scan."""
    a1, a2, a3, a4 = ancestors
    return f"""<!DOCTYPE html><html><body><div id="root">
<div {a4}><div {a3}><div {a2}><div {a1}>
 <div class="zzclass one" {container_attrs}>
  <h2>Feed post</h2>
  <a href="https://www.linkedin.com/in/fake-person-slug/"><figure></figure></a>
  <a href="https://www.linkedin.com/in/fake-person-slug/" data-tracking="track-blob-value"><p>Fake Person</p></a>
  <p>Fictional Headline</p><p>3h • </p>
  <p>We are hiring a Python developer. Mail hr@fictional.test or call +91 90000 00000</p>
  {inner}
 </div>
</div></div></div></div>{blocks}</div></body></html>"""


def dump(report):
    return json.dumps(report, ensure_ascii=False)


class ClassifyValueTests(SimpleTestCase):
    def test_valid_post_urns(self):
        for value, expected in [
            (POST, [POST]),
            (POST.replace("activity", "ACTIVITY"), [POST]),
            (f"{{\"entityUrn\":\"{POST}\",\"x\":1}}", [POST]),
            (POST.replace(":", "%3A"), [POST]),                                     # URL-encoded
            (f"urn:li:fs_updateV2:({POST},MAIN_FEED)", [POST]),                    # nested
            (f"https://www.linkedin.com/feed/update/{POST}/?trk=x", [POST]),
            (f"/feed/update/{POST}/", [POST]),
            (f"https://www.linkedin.com/posts/fake_hiring-activity-{POST.rsplit(':', 1)[1]}-AbCd", [POST]),
        ]:
            with self.subTest(value=value):
                self.assertEqual(classify_value(value)["post_urns"], expected)

    def test_other_urns_record_type_only(self):
        info = classify_value(f"{PROFILE_URN} {MEMBER_URN} urn:li:fs_miniProfile:abc")
        self.assertEqual(info["post_urns"], [])
        self.assertEqual(info["other_urn_types"], {"fsd_profile": 1, "member": 1, "fs_miniProfile": 1})
        self.assertNotIn("ACoAA", dump(info))
        self.assertNotIn("123456789", dump(info))

    def test_malformed_post_ids(self):
        for value in ("urn:li:activity:123", "urn:li:activity:abc", "urn:li:share:" + "9" * 25, "urn:li:ugcPost:"):
            with self.subTest(value=value):
                info = classify_value(value)
                self.assertEqual(info["post_urns"], [])
                self.assertEqual(len(info["other_urn_types"]), 1)
                self.assertTrue(next(iter(info["other_urn_types"])).endswith("(malformed)"))

    def test_decoys(self):
        for value in ("7212345678901234567", "activity-7212345678901234567", "my activity log",
                      "https://example.com/feed/update/" + POST, "c2VjcmV0LWJhc2U2NC1ibG9i" * 4):
            with self.subTest(value=value[:40]):
                info = classify_value(value)
                self.assertEqual(info["post_urns"], [] if "example.com" not in value else [POST])
        # Non-LinkedIn link: the URN text itself is still a valid post URN; only LinkedIn
        # links are *normalised*, but a literal URN anywhere is recorded.
        self.assertEqual(classify_value("https://example.com/path")["post_urns"], [])

    def test_length_only_for_plain_values(self):
        self.assertEqual(classify_value("zzclass one"), {"length": 11, "post_urns": [], "other_urn_types": {}})


class BuildInventoryTests(SimpleTestCase):
    def test_container_urn_with_location(self):
        report = build_inventory(page(container_attrs=f'componentkey="expanded{POST}FeedType_MAIN"'), keyword="k")
        self.assertEqual((report["layout"], report["post_count"], report["posts_with_post_urns"]), ("current", 1, 1))
        post = report["posts"][0]
        self.assertEqual(post["distinct_post_urns"], [POST])
        self.assertEqual(post["post_urn_locations"],
                         [{"urn": POST, "scope": "container", "depth": 0, "tag": "div", "attribute": "componentkey",
                           "method": "plain"}])
        self.assertEqual(report["attribute_names_carrying_post_urns"], {"componentkey": 1})
        self.assertEqual(post["scopes"]["container"]["componentkey"]["post_urn_values"], 1)
        self.assertEqual(post["scopes"]["container"]["class"], {
            "count": 1, "max_length": len("zzclass one"), "post_urn_values": 0, "other_urn_types": {}})

    def test_profile_urns_type_only(self):
        report = build_inventory(page(container_attrs=f'data-actor="{PROFILE_URN}" data-member="{MEMBER_URN}"'))
        post = report["posts"][0]
        self.assertEqual(post["distinct_post_urns"], [])
        self.assertEqual(post["scopes"]["container"]["data-actor"]["other_urn_types"], {"fsd_profile": 1})
        self.assertNotIn("ACoAAFictionalMemberId", dump(report))

    def test_nested_reshare_keeps_depths(self):
        inner = f'<div data-view-name="reshare" data-urn="{RESHARED}"><p>Original fictional post</p></div>'
        report = build_inventory(page(container_attrs=f'data-urn="{POST}"', inner=inner))
        post = report["posts"][0]
        self.assertEqual(post["distinct_post_urns"], sorted([POST, RESHARED]))
        depths = {loc["urn"]: loc["depth"] for loc in post["post_urn_locations"]}
        self.assertEqual(depths[POST], 0)
        self.assertGreater(depths[RESHARED], 0)  # deeper than the container: the reshared post

    def test_conflicting_ids_on_container_are_both_reported(self):
        report = build_inventory(page(container_attrs=f'data-a="{POST}" data-b="{OTHER_POST}"'))
        post = report["posts"][0]
        self.assertEqual(post["distinct_post_urns"], sorted([POST, OTHER_POST]))
        self.assertEqual({loc["depth"] for loc in post["post_urn_locations"]}, {0})

    def test_malformed_and_decoy_values_are_not_post_urns(self):
        report = build_inventory(page(container_attrs=(
            'data-x="urn:li:activity:123" data-y="7212345678901234567" data-z="c2VjcmV0LWJhc2U2NC1ibG9i"')))
        post = report["posts"][0]
        self.assertEqual(post["distinct_post_urns"], [])
        self.assertEqual(post["scopes"]["container"]["data-x"]["other_urn_types"], {"activity(malformed)": 1})
        self.assertEqual(post["scopes"]["container"]["data-z"]["max_length"], 24)

    def test_ancestors_limited_to_three(self):
        report = build_inventory(page(ancestors=("", f'data-key="{POST}"', "", f'data-far="{OTHER_POST}"')))
        post = report["posts"][0]
        self.assertEqual(post["post_urn_locations"],
                         [{"urn": POST, "scope": "ancestor-2", "depth": -2, "tag": "div", "attribute": "data-key",
                           "method": "plain"}])
        self.assertNotIn(OTHER_POST, dump(report))  # 4th ancestor is out of scope
        self.assertNotIn("ancestor-4", post["scopes"])

    def test_embedded_blocks_counts_and_presence_only(self):
        blocks = (f'<code style="display:none">{{"a":"{POST}","b":"{POST}","c":"{OTHER_POST}",'
                  f'"who":"{PROFILE_URN}","name":"Fake Person"}}</code>'
                  f'<script type="application/json">{{"x":"{OTHER_POST}"}}</script>'
                  f'<script>var notJson = "{POST}";</script>')
        report = build_inventory(page(container_attrs=f'data-urn="{POST}"', blocks=blocks))
        self.assertEqual(report["embedded_blocks"], {
            "blocks_scanned": 2, "blocks_with_post_urns": 2, "post_urn_occurrences": 4, "distinct_post_urn_count": 2})
        self.assertEqual(report["posts"][0]["in_embedded_blocks"], {POST: True})
        self.assertNotIn(OTHER_POST, dump(report))           # only counted, never listed
        self.assertNotIn("ACoAAFictionalMemberId", dump(report))

    def test_post_without_any_ids(self):
        report = build_inventory(page())
        self.assertEqual((report["post_count"], report["posts_with_post_urns"]), (1, 0))
        self.assertEqual(report["posts"][0]["in_embedded_blocks"], {})

    def test_multiple_posts_and_legacy_layout(self):
        current = build_inventory(sdui_search_page(sdui_post_html(seed="a"), sdui_post_html(seed="b", slug="x")))
        self.assertEqual((current["layout"], current["post_count"]), ("current", 2))
        legacy = build_inventory(search_page(post_html(POST, HIRING_TEXT)))
        self.assertEqual(legacy["layout"], "legacy")
        self.assertEqual(legacy["posts"][0]["distinct_post_urns"], [POST])
        self.assertEqual(build_inventory("<html><body>nothing</body></html>")["layout"], "none")

    def test_unusual_attribute_names_masked(self):
        html = page(container_attrs='data-ok="1"').replace('data-ok="1"', 'data-ok="1" x-hiring-fake-person-slug-0000000000000000000000000000000000000000000000000="y"')
        report = build_inventory(html)
        names = report["posts"][0]["scopes"]["container"]
        self.assertIn("data-ok", names)
        self.assertTrue(any(name.startswith("<unusual-name len") for name in names))


class PrivacyTests(SimpleTestCase):
    def full_page(self):
        inner = f'<div data-urn="{RESHARED}" data-actor="{PROFILE_URN}"><a href="mailto:hr@fictional.test">mail</a></div>'
        blocks = f'<code>{{"u":"{POST}","p":"{PROFILE_URN}","n":"Fake Person","t":"track-blob-value"}}</code>'
        return page(container_attrs=f'componentkey="{POST}" data-view-name="feed-full-update" '
                                    f'data-tracking="track-blob-value" data-blob="c2VjcmV0LWJhc2U2NC1ibG9i"',
                    ancestors=('role="listitem"', f'data-member="{MEMBER_URN}"', "", ""),
                    inner=inner, blocks=blocks)

    def test_report_contains_no_text_names_contacts_or_other_values(self):
        text = dump(build_inventory(self.full_page(), keyword="python developer"))
        for secret in SECRET_STRINGS + ("mailto:",  "linkedin.com/in/"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, text)
        self.assertIn(POST, text)          # public post URNs are kept
        self.assertIn(RESHARED, text)
        self.assertIn("data-view-name", text)  # attribute names are kept
        # Version 2: allowlisted structural values (data-view-name / role) are kept too.
        self.assertIn("feed-full-update", text)
        self.assertIn("listitem", text)

    def test_sanitizer_still_strips_all_of_it(self):
        cleaned = sanitize_html(self.full_page())
        for removed in ("componentkey", "data-view-name", "data-tracking", "data-actor", "data-member",
                        "data-blob", "role=", "ACoAAFictionalMemberId", "<code", "track-blob-value"):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, cleaned)
        self.assertIn(f'data-urn="{RESHARED}"', cleaned)  # data-urn was already on the kept list


class SaveInventoryTests(SimpleTestCase):
    def test_same_tick_saves_never_overwrite_and_prune_keeps_newest(self):
        from datetime import datetime as real_datetime

        frozen = real_datetime(2026, 11, 16, 12, 0, 0, 123456)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("collector.linkedin.inventory.datetime") as clock:
            clock.now.return_value = frozen
            paths = [save_inventory({"n": i}, Path(tmp), keep=2) for i in range(4)]
            self.assertEqual(len({p.name for p in paths}), 4)          # unique despite one timestamp
            remaining = sorted(p.name for p in Path(tmp).glob(f"{FILE_PREFIX}*.json"))
            self.assertEqual(remaining, [paths[2].name, paths[3].name])  # the two newest survive
            self.assertEqual(json.loads(paths[3].read_text(encoding="utf-8")), {"n": 3})

    def test_save_and_prune(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = build_inventory(page(container_attrs=f'data-urn="{POST}"'))
            paths = [save_inventory(report, Path(tmp) / "debug", keep=2) for _ in range(4)]
            remaining = sorted(p.name for p in (Path(tmp) / "debug").glob(f"{FILE_PREFIX}*.json"))
            self.assertEqual(remaining, sorted(p.name for p in paths[-2:]))
            self.assertEqual(json.loads(paths[-1].read_text(encoding="utf-8"))["posts"][0]["distinct_post_urns"], [POST])
            self.assertFalse(any(p.suffix == ".html" for p in (Path(tmp) / "debug").iterdir()))


class SourceIntegrationTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def source(self, browser, **kwargs):
        return LinkedInPostSource(browser_factory=lambda: browser, max_scrolls=2,
                                  debug_dir=Path(self.tmp) / "debug", **kwargs)

    def pages(self):
        a, b = sdui_post_html(seed="a"), sdui_post_html(seed="b", slug="other", author="Omar Placeholder")
        return {"k1": [sdui_search_page(a), sdui_search_page(a, b)], "k2": [sdui_search_page(b)]}

    def inventory_files(self):
        return list((Path(self.tmp) / "debug").glob(f"{FILE_PREFIX}*.json")) if (Path(self.tmp) / "debug").exists() else []

    def test_disabled_by_default(self):
        source = self.source(FakeBrowser(self.pages()))
        self.assertFalse(source.attribute_inventory)
        posts = list(source.iter_posts(["k1", "k2"], 10))
        self.assertEqual(len(posts), 2)
        self.assertEqual(self.inventory_files(), [])

    @override_settings(COLLECTOR_ATTRIBUTE_INVENTORY=True)
    def test_enabled_by_setting_writes_exactly_one_report(self):
        source = self.source(FakeBrowser(self.pages()))
        posts = list(source.iter_posts(["k1", "k2"], 10))
        self.assertEqual(len(posts), 2)                      # collection unchanged
        self.assertEqual(len(self.inventory_files()), 1)     # once per run, despite scrolls and keywords
        self.assertEqual(source.inventory_paths, self.inventory_files())
        report = json.loads(source.inventory_paths[0].read_text(encoding="utf-8"))
        self.assertEqual((report["keyword"], report["post_count"]), ("k1", 1))

    def test_failure_never_breaks_collection(self):
        source = self.source(FakeBrowser(self.pages()), attribute_inventory=True)
        with mock.patch("collector.linkedin.source.build_inventory", side_effect=ValueError("boom")):
            posts = list(source.iter_posts(["k1", "k2"], 10))
        self.assertEqual(len(posts), 2)
        self.assertEqual(source.inventory_errors, ["ValueError: boom"])
        self.assertEqual(self.inventory_files(), [])


class CommandFlagTests(TestCase):
    def setUp(self):
        from tracker.models import Profile, Skill

        self.tmp = tempfile.mkdtemp()
        profile = Profile.objects.create(name="Test User", target_roles="k1", max_acceptable_experience_years=9)
        Skill.objects.create(name="Python", profile=profile)

    def call(self, *args):
        import io

        from django.core.management import call_command

        out = io.StringIO()
        browser = FakeBrowser({"k1": [sdui_search_page(sdui_post_html(seed="a"))]})
        with override_settings(COLLECTOR_DEBUG_DIR=Path(self.tmp) / "debug"), \
                mock.patch("collector.management.commands.collect_linkedin.default_browser", return_value=browser):
            call_command("collect_linkedin", *args, stdout=out, stderr=io.StringIO())
        return out.getvalue()

    def test_flag_enables_and_reports_path(self):
        output = self.call("--attribute-inventory")
        self.assertIn("Attribute inventory saved:", output)
        self.assertEqual(len(list((Path(self.tmp) / "debug").glob(f"{FILE_PREFIX}*.json"))), 1)

    def test_no_flag_no_report(self):
        output = self.call()
        self.assertNotIn("Attribute inventory", output)
        self.assertFalse((Path(self.tmp) / "debug").exists())

    def test_flag_rejected_for_file_source(self):
        from django.core.management import CommandError

        with self.assertRaisesMessage(CommandError, "only used with --source linkedin"):
            self.call("--source", "file", "--file", "x.json", "--attribute-inventory")
