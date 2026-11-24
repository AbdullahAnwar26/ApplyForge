"""
python manage.py collect_linkedin [--source linkedin|file] [--file posts.json] [--keywords ...]
                                  [--limit N] [--profile ID] [--headless] [--json]
                                  [--attribute-inventory]
python manage.py collect_linkedin --inventory-only --keywords "<one keyword>" [--headless]

``--inventory-only`` loads only the first search-results page for one keyword,
writes one post-attribute inventory report, and exits: no post parsing, no
ingestion, no database access.

Every collection is recorded as a CollectorRun (run history, Phase 6c);
``--inventory-only`` records nothing.

Collects recruiter hiring posts and ingests them through the existing tracker
ingestion and screening (in-process, no HTTP). ``linkedin`` (default) drives
a visible Chromium window with the session saved by ``linkedin_login``;
``file`` reads posts from a JSON file offline. Runs are started manually,
paced and small. Nothing is applied for or sent. A failed collection exits
with an error, never as a success.
"""

import json
import re
import sys

from django.core.management.base import BaseCommand, CommandError

from tracker.models import Profile
from tracker.screening import ScreeningInputError

from collector.linkedin.browser import default_browser
from collector.linkedin.source import LinkedInPostSource
from collector.posts import _EMAIL as POST_EMAIL
from collector.posts import _PHONE as POST_PHONE
from collector.models import CollectorRunTrigger
from collector.runs import record_collection
from collector.service import CollectionInputError
from collector.sources import JsonFilePostSource, SourceError

SOURCES = ("linkedin", "file")

# -v 3 extraction diagnostics: console only, contact details and links masked.
_URL = re.compile(r"(?:https?://|www\.)\S+|\b[\w.-]*linkedin\.com/\S*", re.I)
MAX_DIAGNOSTIC_FIELD = 120


def _mask(value):
    """Short, single-line display value with e-mails, phone numbers and links masked."""
    if not value:
        return "-"
    text = " ".join(str(value).split())
    text = _URL.sub("[link]", text)
    text = POST_EMAIL.sub("[email]", text)
    text = POST_PHONE.sub("[phone]", text)
    if len(text) > MAX_DIAGNOSTIC_FIELD:
        text = text[:MAX_DIAGNOSTIC_FIELD].rstrip() + "…"
    return text


def format_extraction(summary):
    """One diagnostic line: author, headline, relative-time label, commentary length."""
    return (
        f"extracted: author={_mask(summary.get('author'))!r} | "
        f"headline={_mask(summary.get('headline'))!r} | "
        f"time={_mask(summary.get('relative_time'))!r} | "
        f"commentary={summary.get('commentary_chars', 0)} chars"
        + (" (ends with '…')" if summary.get("commentary_truncated") else "")
    )


class Command(BaseCommand):
    help = "Collect LinkedIn recruiter hiring posts and ingest them as jobs (screened, never sent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--source", choices=SOURCES, default="linkedin",
            help="'linkedin' (browser, needs linkedin_login first) or 'file' (offline JSON).",
        )
        parser.add_argument("--file", help="JSON posts file for --source file.")
        parser.add_argument(
            "--headless", action="store_true",
            help="Run the browser without a window (default: visible, or COLLECTOR_HEADLESS).",
        )
        parser.add_argument(
            "--keywords", action="append", default=None,
            help="Search keywords (repeatable or comma-separated). Default: profile target roles.",
        )
        parser.add_argument("--limit", type=int, default=None, help="Maximum posts to process.")
        parser.add_argument(
            "--user", default=None,
            help="Account (username) the collected jobs belong to; required when several accounts exist.",
        )
        parser.add_argument("--profile", type=int, default=None, help="Profile id (required if the account has several).")
        parser.add_argument("--json", action="store_true", help="Print the full result as JSON.")
        parser.add_argument(
            "--attribute-inventory", action="store_true",
            help="Diagnostic: also write one post-attribute inventory (attribute names and public "
                 "post URNs only) to COLLECTOR_DEBUG_DIR. LinkedIn source only.",
        )
        parser.add_argument(
            "--inventory-only", action="store_true",
            help="Diagnostic: load only the first results page for ONE --keywords value, write one "
                 "attribute inventory and exit. No post parsing, ingestion or database access. "
                 "LinkedIn source only.",
        )

    def handle(self, *args, **options):
        if options["inventory_only"]:
            self._inventory_only(options)
            return  # never reaches the profile lookup, run_collection or ingestion
        source = self._build_source(options)
        owner = self._get_owner(options["user"])
        profile = self._get_profile(options["profile"], owner)
        keywords = None
        if options["keywords"]:
            keywords = [k for value in options["keywords"] for k in value.split(",")]

        try:
            run, result = record_collection(
                source, trigger=CollectorRunTrigger.COMMAND, owner=owner,
                keywords=keywords, profile=profile, limit=options["limit"],
            )
        except (ScreeningInputError, CollectionInputError) as exc:
            raise CommandError(f"{exc.message} (collector run {exc.run.pk} recorded as FAILED)")

        if options["json"]:
            data = {"run_id": run.pk, "run_status": run.status, **result.as_dict()}
            self._write(json.dumps(data, indent=2, ensure_ascii=False, default=str))
        else:
            self._report(result, options["verbosity"])
            self._write(f"Collector run {run.pk}: {run.status}.")
        if options["attribute_inventory"]:
            self._report_inventory(source)
        if result.source_error:
            raise CommandError(
                f"Collection FAILED after {len(result.outcomes)} post(s): {result.source_error}"
            )
        failed = result.counts["failed"]
        if failed:
            raise CommandError(
                f"Collection finished with {failed} post(s) that FAILED to ingest; see the log. "
                "Posts that succeeded were saved."
            )

    def _inventory_only(self, options):
        """Validate, then capture one inventory report. No database access at all."""
        if options["source"] != "linkedin" or options["file"]:
            raise CommandError("--inventory-only is only used with --source linkedin.")
        for flag in ("profile", "limit"):
            if options[flag] is not None:
                raise CommandError(f"--{flag} is not used with --inventory-only (no posts are collected).")
        if options["json"]:
            raise CommandError("--json is not used with --inventory-only; the report is already JSON.")
        keywords = [k.strip() for value in (options["keywords"] or []) for k in value.split(",") if k.strip()]
        if len(keywords) != 1:
            raise CommandError(
                "--inventory-only needs exactly one keyword, e.g. --keywords \"Python Backend Developer\"."
            )
        headless = True if options["headless"] else None
        source = LinkedInPostSource(
            browser_factory=lambda: default_browser(headless=headless),
            attribute_inventory=True,  # implied by --inventory-only
        )
        try:
            path = source.capture_inventory_only(keywords[0])
        except SourceError as exc:
            raise CommandError(f"Inventory-only FAILED: {exc}")
        report = json.loads(path.read_text(encoding="utf-8"))
        self._write(f"Inventory-only report saved: {path}")
        self._write(
            f"  layout {report['layout']}, {report['post_count']} post(s), "
            f"{report['posts_with_post_urns']} with public post URNs; "
            f"attributes carrying post URNs: {', '.join(report['attribute_names_carrying_post_urns']) or 'none'}"
        )
        self._write("  No posts were parsed or ingested; no database access.")

    def _report_inventory(self, source):
        for path in source.inventory_paths:
            self._write(f"Attribute inventory saved: {path}")
        for error in source.inventory_errors:
            self._write(f"Attribute inventory FAILED (collection unaffected): {error}")
        if not source.inventory_paths and not source.inventory_errors:
            self._write("Attribute inventory: not captured (no results page with posts was loaded).")

    def _build_source(self, options):
        if options["source"] == "linkedin":
            if options["file"]:
                raise CommandError("--file is only used with --source file.")
            headless = True if options["headless"] else None
            return LinkedInPostSource(
                browser_factory=lambda: default_browser(headless=headless),
                attribute_inventory=True if options["attribute_inventory"] else None,
            )
        if options["attribute_inventory"]:
            raise CommandError("--attribute-inventory is only used with --source linkedin.")
        if not options["file"]:
            raise CommandError("--file is required with --source file.")
        return JsonFilePostSource(options["file"])

    def _get_owner(self, username):
        """The account the run and its jobs belong to: --user, or the only account."""
        from django.contrib.auth import get_user_model

        User = get_user_model()
        if username:
            user = User.objects.filter(username__iexact=username).first()
            if user is None:
                raise CommandError(f"No account named {username!r}.")
            return user
        users = list(User.objects.order_by("id")[:2])
        if not users:
            # Before anyone has signed up: collect into the unowned pool, which the first
            # account to sign up receives (tracker.accounts.claim_unowned_data).
            return None
        if len(users) > 1:
            raise CommandError("Several accounts exist. Pass --user <username>.")
        return users[0]

    def _get_profile(self, profile_id, owner):
        if profile_id is None:
            return None
        try:
            return Profile.objects.get(pk=profile_id, owner=owner)
        except Profile.DoesNotExist:
            raise CommandError(f"Profile {profile_id} does not exist.")

    def _report(self, result, verbosity):
        counts = result.counts
        self._write(
            f"Collected {counts['found']} post(s) from '{result.source}' for profile {result.profile_id} "
            f"(keywords: {', '.join(result.keywords)}; limit {result.limit})."
        )
        self._write(
            f"  created {counts['created']}, existing {counts['existing']}, skipped {counts['skipped']}, "
            f"invalid {counts['invalid']}, failed {counts['failed']}"
        )
        if verbosity >= 2:
            for o in result.outcomes:
                detail = o.screening_outcome or o.matched_by or o.reason or ""
                job = f" job {o.job_id}" if o.job_id else ""
                self._write(f"  [{o.index}] {o.status}{job}: {o.title or '-'} {detail}".rstrip())
                if verbosity >= 3 and o.extraction is not None:
                    self._write("      " + format_extraction(o.extraction))

    def _write(self, text):
        # Windows consoles often cannot encode emojis found in posts.
        encoding = getattr(self.stdout._out, "encoding", None) or sys.getdefaultencoding()
        self.stdout.write(text.encode(encoding, "replace").decode(encoding))
