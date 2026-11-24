"""
Idempotent job ingestion (Milestone 3).

Used by the ``jobs/ingest/`` endpoint and, later, in-process by the collector
(no HTTP calls back to this backend).

Duplicate handling (locked rule 5):

1. Exact source URL: compares the normalized URL and the raw URL, so rows
   stored before normalization still match.
2. JD fingerprint: SHA-256 of the lower-cased, whitespace-collapsed description.
3. Title/company/contact evidence is NOT used to block ingestion. It is soft
   evidence recorded by screening (``POSSIBLE_DUPLICATE``).

An exact match returns the existing job unchanged. A unique-constraint race
on create is resolved by re-reading the existing job.
"""

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from django.db import IntegrityError, transaction

from tracker.models import Company, Contact, JobOpportunity, JobSource
from tracker.screening import screen_job

TRACKING_PARAMS = {
    "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "trk", "trkinfo",
    "trackingid", "refid", "ref", "lipi", "originalsubdomain",
}
_LINKEDIN_VIEW = re.compile(r"^/jobs/view/(?:[^/]*?-)?(\d+)/?$")
# LinkedIn posts (Milestone 6): /feed/update/urn:li:<type>:<id>/ and the
# share link /posts/<author>_<slug>-<type>-<id>-<suffix>/ name the same post.
_LINKEDIN_POST_TYPES = r"activity|share|ugcPost"
_LINKEDIN_FEED_UPDATE = re.compile(
    rf"^/feed/update/urn:li:({_LINKEDIN_POST_TYPES}):(\d+)/?$", re.I
)
_LINKEDIN_POST_SLUG = re.compile(
    rf"^/posts/[^/]*?[-_]({_LINKEDIN_POST_TYPES})-(\d+)(?:-[A-Za-z0-9_]+)?/?$", re.I
)
_CANONICAL_POST_TYPES = {"activity": "activity", "share": "share", "ugcpost": "ugcPost"}
_DEFAULT_PORTS = {"http": "80", "https": "443"}


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize_whitespace(value):
    return " ".join((value or "").split())


def normalize_source_url(url):
    """Canonical form of a job URL, or None for an empty value.

    * Lower-cases the scheme and host, drops default ports and fragments.
    * Drops tracking parameters (``utm_*``, ``trk``, ``refId``, ...); sorts the rest.
    * LinkedIn job links (``/jobs/view/<slug>-<id>``, ``?currentJobId=<id>``)
      become ``https://www.linkedin.com/jobs/view/<id>/``.
    * LinkedIn post links (``/feed/update/urn:li:activity:<id>``, also URL-encoded,
      and ``/posts/<slug>-activity-<id>-<suffix>``) become
      ``https://www.linkedin.com/feed/update/urn:li:activity:<id>/``
      (``share`` and ``ugcPost`` ids keep their own type).
    """
    url = (url or "").strip()
    if not url:
        return None
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    query = parse_qsl(parts.query, keep_blank_values=True)

    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        view = _LINKEDIN_VIEW.match(parts.path)
        job_id = view.group(1) if view else None
        if job_id is None:
            job_id = next(
                (v for k, v in query if k.lower() == "currentjobid" and v.isdigit()), None
            )
        if job_id:
            return f"https://www.linkedin.com/jobs/view/{job_id}/"
        path = unquote(parts.path)
        post = _LINKEDIN_FEED_UPDATE.match(path) or _LINKEDIN_POST_SLUG.match(path)
        if post:
            post_type = _CANONICAL_POST_TYPES[post.group(1).lower()]
            return f"https://www.linkedin.com/feed/update/urn:li:{post_type}:{post.group(2)}/"

    netloc = host
    if port and str(port) != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{port}"
    kept = sorted(
        (k, v) for k, v in query
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
    )
    return urlunsplit((scheme, netloc, parts.path or "/", urlencode(kept), ""))


def jd_fingerprint(full_jd):
    """SHA-256 hex digest of the normalized job description."""
    normalized = normalize_whitespace(full_jd).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def normalize_phone(value):
    """Digits only, matching ``Contact.save``."""
    return "".join(filter(str.isdigit, value or ""))


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def find_existing_job(url_candidates, fingerprint, owner):
    """Return ``(job, matched_by)`` for an exact URL or fingerprint match among ``owner``'s jobs."""
    jobs = JobOpportunity.objects.filter(owner=owner)
    urls = [u for u in url_candidates if u]
    if urls:
        job = jobs.filter(source_url__in=urls).order_by("id").first()
        if job:
            return job, "source_url"
    job = jobs.filter(jd_fingerprint=fingerprint).order_by("id").first()
    if job:
        return job, "jd_fingerprint"
    return None, None


def get_or_create_company(name, website=None, *, owner):
    name = normalize_whitespace(name)
    if not name:
        return None
    existing = Company.objects.filter(owner=owner, normalized_name=name.lower()).order_by("id").first()
    if existing:
        return existing  # reused as-is; never overwritten
    return Company.objects.create(owner=owner, name=name, website=website or None)


def get_or_create_contact(data, company=None, *, owner):
    """Reuse one of ``owner``'s contacts by normalized email, then phone, then WhatsApp."""
    email = (data.get("email") or "").strip().lower()
    phone = normalize_phone(data.get("phone"))
    whatsapp = normalize_phone(data.get("whatsapp"))
    lookups = []
    if email:
        lookups.append({"normalized_email": email})
    if phone:
        lookups.append({"normalized_phone": phone})
    if whatsapp:
        lookups.append({"normalized_whatsapp": whatsapp})
    for lookup in lookups:
        existing = Contact.objects.filter(owner=owner, **lookup).order_by("id").first()
        if existing:
            return existing
    return Contact.objects.create(
        owner=owner,
        name=normalize_whitespace(data.get("name")) or None,
        email=data.get("email") or None,
        phone=data.get("phone") or None,
        whatsapp=data.get("whatsapp") or None,
        company=company,
    )


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

def _complete_truncated(job, data, fingerprint):
    """A job saved from a cut-off post gets the full text when the same post (same URL)
    is collected again complete. Nothing else about the existing job changes."""
    if job.jd_complete or not data.get("jd_complete", True):
        return
    if "full_jd" in (job.extraction or {}).get("edited_fields", []):
        return  # the user already pasted the description; never overwrite their text
    clash = JobOpportunity.objects.filter(owner=job.owner, jd_fingerprint=fingerprint).exclude(pk=job.pk)
    if clash.exists():
        return
    extraction = dict(job.extraction or {})
    if data.get("extraction"):
        extraction.update({k: v for k, v in data["extraction"].items() if k != "edited_fields"})
    job.full_jd = data["full_jd"].strip()
    job.source_post_text = (data.get("source_post_text") or job.full_jd).strip()
    job.jd_fingerprint = fingerprint
    job.jd_complete = True
    job.extraction = extraction
    job.save(update_fields=["full_jd", "source_post_text", "jd_fingerprint", "jd_complete",
                            "extraction", "updated_at"])


@dataclass
class IngestResult:
    job: JobOpportunity
    created: bool
    matched_by: str | None = None


def ingest_job(data, profile, skills):
    """Create and screen a job, or return the existing exact match unchanged.

    ``data`` is validated input (see ``JobIngestSerializer``). ``profile`` and
    ``skills`` come from ``screening.resolve_profile``. The job belongs to the
    profile's owner; duplicates are matched among that owner's jobs only.
    """
    owner = profile.owner
    raw_url = (data.get("source_url") or "").strip() or None
    source_url = normalize_source_url(raw_url)
    fingerprint = jd_fingerprint(data["full_jd"])
    url_candidates = {source_url, raw_url}

    existing, matched_by = find_existing_job(url_candidates, fingerprint, owner)
    if existing:
        _complete_truncated(existing, data, fingerprint)
        return IngestResult(existing, created=False, matched_by=matched_by)

    try:
        with transaction.atomic():
            company = get_or_create_company(
                data.get("company_name"), data.get("company_website"), owner=owner
            )
            job = JobOpportunity.objects.create(
                owner=owner,
                title=normalize_whitespace(data["title"]),
                full_jd=data["full_jd"].strip(),
                jd_fingerprint=fingerprint,
                source=data.get("source") or JobSource.OTHER,
                source_url=source_url,
                company=company,
                posted_date=data.get("posted_date"),
                location=normalize_whitespace(data.get("location")) or None,
                work_mode=normalize_whitespace(data.get("work_mode")) or None,
                required_experience_text=(
                    normalize_whitespace(data.get("required_experience_text")) or None
                ),
                recruiter_name=normalize_whitespace(data.get("recruiter_name")),
                recruiter_headline=normalize_whitespace(data.get("recruiter_headline")),
                source_post_text=(data.get("source_post_text") or "").strip(),
                jd_complete=data.get("jd_complete", True),
                extraction=data.get("extraction") or {},
            )
            contacts = [
                get_or_create_contact(contact, company, owner=owner)
                for contact in data.get("contacts") or []
            ]
            if contacts:
                job.contacts.add(*contacts)
            screen_job(job, profile, skills)
    except IntegrityError:
        # A concurrent ingest created the same URL/fingerprint first.
        existing, matched_by = find_existing_job(url_candidates, fingerprint, owner)
        if existing:
            return IngestResult(existing, created=False, matched_by=matched_by)
        raise
    return IngestResult(job, created=True)
