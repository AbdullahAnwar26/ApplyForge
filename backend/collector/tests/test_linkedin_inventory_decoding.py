"""Inventory v2: bounded componentkey decoding and allowlisted structural values.
Offline, synthetic values only; every URN is fictional."""

import base64
import json
import time
from urllib.parse import quote

from django.test import SimpleTestCase

from collector.linkedin.inventory import (
    MAX_DECODE_NODES,
    NOT_ALLOWLISTED,
    REPORT_VERSION,
    build_inventory,
    decode_post_urns,
    structural_value,
)

from .linkedin_pages import urn_for
from .test_linkedin_inventory import page

POST = urn_for(seq=55)
OTHER = urn_for(seq=66, kind="ugcPost")
PROFILE_URN = "urn:li:fsd_profile:ACoAAFictionalMemberId0123456789"


def b64(text_or_bytes, pad=True):
    raw = text_or_bytes.encode() if isinstance(text_or_bytes, str) else text_or_bytes
    out = base64.b64encode(raw).decode()
    return out if pad else out.rstrip("=")


def b64url(raw):
    return base64.urlsafe_b64encode(raw).decode()


def pct_all(text):
    """Percent-encode EVERY character, so no raw base64 run survives undecoded."""
    return "".join(f"%{byte:02X}" for byte in text.encode())


class DecodePostUrnsTests(SimpleTestCase):
    def test_base64_variants(self):
        self.assertEqual(decode_post_urns(b64(POST)), {POST: ["base64"]})
        self.assertEqual(decode_post_urns(b64(POST, pad=False)), {POST: ["base64"]})
        urlsafe = b64url(b"\xfb\xff\xfe" + POST.encode())           # forces '-' / '_' characters
        self.assertTrue("-" in urlsafe or "_" in urlsafe)
        self.assertEqual(decode_post_urns(urlsafe), {POST: ["base64url"]})

    def test_delimited_segment_inside_a_value(self):
        self.assertEqual(decode_post_urns(f"auto:{b64(POST)}:MAIN"), {POST: ["base64"]})
        self.assertEqual(decode_post_urns(f'{{"k":"{b64(POST)}"}}'), {POST: ["base64"]})

    def test_glued_segments_decode_at_any_alignment(self):
        for prefix in ("expanded", "expandedX", "expandedXY", "expandedXYZ"):   # 8, 9, 10, 11 chars
            with self.subTest(prefix=prefix):
                self.assertEqual(decode_post_urns(f"{prefix}{b64(POST, pad=False)}FeedType"), {POST: ["base64"]})

    def test_url_and_base64_chains(self):
        self.assertEqual(decode_post_urns(pct_all(b64(POST))), {POST: ["url>base64"]})
        self.assertEqual(decode_post_urns(b64(quote(POST, safe=""))), {POST: ["base64>url"]})
        self.assertEqual(decode_post_urns(b64(b64(POST))), {POST: ["base64>base64"]})
        # A shorter chain wins when several work ('=' padding percent-encoded, body still raw).
        self.assertEqual(decode_post_urns(quote(b64(POST), safe="")), {POST: ["base64"]})

    def test_structured_and_binary_payloads(self):
        self.assertEqual(decode_post_urns(b64(json.dumps({"entityUrn": POST, "x": 1}))), {POST: ["base64"]})
        protobuf_like = b"\x0a\x23" + POST.encode() + b"\x12\x04MAIN\xff\x00"
        self.assertEqual(decode_post_urns(b64(protobuf_like)), {POST: ["base64"]})
        both = b64(f"{POST}|{OTHER}")
        self.assertEqual(decode_post_urns(both), {POST: ["base64"], OTHER: ["base64"]})

    def test_nesting_is_bounded_to_three_steps(self):
        self.assertEqual(decode_post_urns(b64(b64(b64(POST)))), {POST: ["base64>base64>base64"]})
        self.assertEqual(decode_post_urns(b64(b64(b64(b64(POST))))), {})          # 4 steps: not followed
        self.assertEqual(decode_post_urns(pct_all(b64(b64(POST)))), {POST: ["url>base64>base64"]})

    def test_only_valid_public_post_urns(self):
        for payload in ("urn:li:activity:123", "urn:li:activity:abc", "urn:li:share:" + "9" * 25,
                        PROFILE_URN, "urn:li:member:123456789", "activity 7212345678901234567",
                        "no urn here at all, just text"):
            with self.subTest(payload=payload):
                self.assertEqual(decode_post_urns(b64(payload)), {})

    def test_literal_and_percent_urns_are_left_to_the_plain_scan(self):
        self.assertEqual(decode_post_urns(POST), {})
        self.assertEqual(decode_post_urns(quote(POST, safe="")), {})

    def test_malformed_and_decoy_input(self):
        for value in ("", None, "not base64 !!!", "A" * 17, "abc+def-ghi_jkl/mnop==",  # mixed alphabets
                      "QUJD" * 4 + "Q",                                              # length % 4 == 1
                      "c2VjcmV0LWJhc2U2NC1ibG9i", "%E0%A4%A", "%%%%", "=" * 40, "////////////////"):
            with self.subTest(value=str(value)[:30]):
                self.assertEqual(decode_post_urns(value), {})

    def test_bounded_work_on_large_or_adversarial_values(self):
        many_runs = " ".join(b64(f"decoy {i} " * 3) for i in range(500)) + " " + b64(POST)
        huge = "A" * 2_000_000
        start = time.monotonic()
        self.assertEqual(decode_post_urns(huge), {})
        decode_post_urns(many_runs)          # must terminate quickly; the URN may be beyond the bounds
        self.assertLess(time.monotonic() - start, 5)
        self.assertGreater(MAX_DECODE_NODES, 0)


class AdjacentUrnRegressionTests(SimpleTestCase):
    def test_plain_scan_finds_adjacent_urns(self):
        from collector.linkedin.inventory import classify_value

        for joiner in ("|", ":", "/", "\\"):
            with self.subTest(joiner=joiner):
                info = classify_value(f"{POST}{joiner}{OTHER}")
                self.assertEqual(info["post_urns"], sorted([POST, OTHER]))
        info = classify_value(f"{POST}|{PROFILE_URN}")
        self.assertEqual((info["post_urns"], info["other_urn_types"]), ([POST], {"fsd_profile": 1}))


class StructuralValueTests(SimpleTestCase):
    def test_allowlist(self):
        for value in ("feed-full-update", "listitem", "article", "search-result-lockup-v-two"):
            with self.subTest(value=value):
                self.assertEqual(structural_value(value), value)

    def test_everything_else_is_redacted(self):
        for value in ("Feed-Full", "feed_full", "feed--x", "-x", "x-", "item2", "feed full", "a" * 41,
                      "", None, POST, "hr@fictional.test", "john smith", "<script>", "feed-full-update ",
                      "riya-fictional-0001"):
            with self.subTest(value=value):
                self.assertEqual(structural_value(value), NOT_ALLOWLISTED)


class InventoryV2Tests(SimpleTestCase):
    def test_encoded_componentkey_location(self):
        report = build_inventory(page(container_attrs=f'componentkey="auto:{b64(POST)}:MAIN"'))
        self.assertEqual(report["version"], REPORT_VERSION)
        post = report["posts"][0]
        self.assertEqual(post["post_urn_locations"], [
            {"urn": POST, "scope": "container", "depth": 0, "tag": "div", "attribute": "componentkey",
             "method": "base64"}])
        self.assertEqual(report["post_urn_methods"], {"base64": 1})
        self.assertEqual(report["attribute_names_carrying_post_urns"], {"componentkey": 1})
        self.assertEqual(post["scopes"]["container"]["componentkey"]["post_urn_values"], 1)

    def test_decoding_is_limited_to_componentkey(self):
        report = build_inventory(page(container_attrs=f'data-foo="{b64(POST)}" data-view-name="{b64(POST)}"'))
        self.assertEqual(report["posts"][0]["distinct_post_urns"], [])

    def test_plain_wins_over_decoded_duplicate(self):
        report = build_inventory(page(container_attrs=f'componentkey="{POST}:{b64(POST)}"'))
        methods = [loc["method"] for loc in report["posts"][0]["post_urn_locations"]]
        self.assertEqual(methods, ["plain"])

    def test_percent_and_post_link_methods(self):
        slug_link = f"https://www.linkedin.com/posts/fake_hiring-activity-{POST.rsplit(':', 1)[1]}-AbCd"
        report = build_inventory(page(container_attrs=f'data-a="{quote(POST, safe="")}" data-b="{slug_link}"'))
        by_attr = {loc["attribute"]: loc["method"] for loc in report["posts"][0]["post_urn_locations"]}
        self.assertEqual(by_attr, {"data-a": "url", "data-b": "post-link"})

    def test_conflicts_and_reshares_stay_visible(self):
        inner = f'<div componentkey="x:{b64(OTHER)}:y"><p>Original fictional post</p></div>'
        report = build_inventory(page(container_attrs=f'data-urn="{POST}"', inner=inner))
        post = report["posts"][0]
        self.assertEqual(post["distinct_post_urns"], sorted([POST, OTHER]))
        located = {loc["urn"]: (loc["depth"], loc["method"]) for loc in post["post_urn_locations"]}
        self.assertEqual(located[POST], (0, "plain"))
        self.assertGreater(located[OTHER][0], 0)
        self.assertEqual(located[OTHER][1], "base64")

    def test_structural_values_per_scope_and_page(self):
        html = page(container_attrs='data-view-name="Some Mixed_Value" componentkey="k"',
                    ancestors=('data-view-name="feed-full-update"', 'role="listitem"', "", ""),
                    inner='<span role="button"></span><span role="button"></span>')
        report = build_inventory(html)
        values = report["posts"][0]["structural_values"]
        self.assertEqual(values["ancestor-1"], {"data-view-name": {"feed-full-update": 1}})
        self.assertEqual(values["ancestor-2"], {"role": {"listitem": 1}})
        self.assertEqual(values["container"], {"data-view-name": {NOT_ALLOWLISTED: 1}})
        self.assertEqual(values["descendants"], {"role": {"button": 2}})
        self.assertEqual(report["structural_values_by_post"]["ancestor-1"], {"data-view-name": {"feed-full-update": 1}})
        self.assertNotIn("Some Mixed_Value", json.dumps(report))

    def test_privacy_of_decoded_content(self):
        secret_json = json.dumps({"entityUrn": POST, "author": "Fake Person", "email": "hr@fictional.test",
                                  "actor": PROFILE_URN, "text": "We are hiring"})
        encoded = b64(secret_json)
        report_text = json.dumps(build_inventory(page(container_attrs=f'componentkey="k:{encoded}:z"')))
        self.assertIn(POST, report_text)
        for secret in ("Fake Person", "hr@fictional.test", "ACoAAFictionalMemberId", "We are hiring",
                       encoded, "entityUrn", "fsd_profile"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, report_text)

    def test_no_urns_anywhere_matches_live_finding_shape(self):
        # Opaque componentkeys (like the live run) must yield no URNs and no values.
        opaque = "c2VjcmV0LWJhc2U2NC1ibG9i" + "Q" * 20
        report = build_inventory(page(container_attrs=f'componentkey="{opaque}"',
                                      ancestors=('data-view-name="feed-full-update"', 'role="listitem"',
                                                 f'componentkey="{opaque}"', "")))
        self.assertEqual((report["posts_with_post_urns"], report["post_urn_methods"]), (0, {}))
        self.assertNotIn(opaque, json.dumps(report))


class HeaderShapeTests(SimpleTestCase):
    """Inventory v3: header shapes reveal layout, never names or post text."""

    def test_shapes_are_privacy_safe_and_informative(self):
        from collector.linkedin.inventory import text_shape

        self.assertEqual(text_shape("Riya Fictional"), "W W")
        self.assertEqual(text_shape("Talent Partner at Example Labs | 12,345 followers"), "W W W W W | 9,9 W")
        self.assertEqual(text_shape("1m •"), "1m •")
        self.assertEqual(text_shape("2w • Edited"), "9W • W")   # contains a word -> shaped
        self.assertEqual(text_shape("--"), "--")
        for name in ("Mohd", "Mohd Rhys", "Ros", "Doy"):   # unit-letter-only words are still shaped
            self.assertNotIn(name, text_shape(name))
        self.assertEqual(text_shape("2mo •"), "2mo •")
        self.assertEqual(text_shape("• 2nd"), "• 9W")
        self.assertEqual(text_shape("hr@fictional.test +91 98765 43210"), "W@W.W +9 9 9")

    def test_report_records_header_layout(self):
        from .linkedin_pages import dvn_post_html

        html = dvn_post_html(author="Riya Fictional", headline="Talent Partner at Example Labs", relative="3h",
                             text="We're hiring! Mail jobs@fictional.test", seed="h")
        report = build_inventory(sdui_search_page_with(html))
        shapes = report["posts"][0]["header_shapes"]["entries"]
        name = next(e for e in shapes if e["in_profile_link"] and e["shape"] == "W W")
        self.assertTrue(name["in_actor"])
        self.assertEqual(name["view_name"], "feed-actor")
        self.assertTrue(any(e["shape"] == "3h •" for e in shapes))
        self.assertTrue(any(e["shape"] == "W W W W W" and not e["in_profile_link"] for e in shapes))
        text = json.dumps(report)
        for secret in ("Riya", "Fictional", "Talent Partner", "Example Labs", "jobs@fictional.test", "hiring"):
            self.assertNotIn(secret, text)

    def test_stops_at_commentary_and_is_bounded(self):
        from .linkedin_pages import dvn_post_html

        report = build_inventory(sdui_search_page_with(dvn_post_html(seed="k")))
        entries = report["posts"][0]["header_shapes"]["entries"]
        self.assertLessEqual(len(entries), 25)
        self.assertFalse(any(e["view_name"] == "feed-commentary" for e in entries))


def sdui_search_page_with(post_html):
    from .linkedin_pages import sdui_search_page

    return sdui_search_page(post_html)
