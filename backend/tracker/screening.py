"""
Job screening against a profile (Milestone 3).

Order (locked business rules, docs/PROJECT_SPEC.md §2):

1. Keyword relevance: at least one profile skill must appear in the job
   description. No match -> REJECTED / NO_KEYWORD_MATCH (stop).
2. Experience eligibility, judged separately:
   * minimum unknown -> NEEDS_REVIEW / EXPERIENCE_UNKNOWN (never rejected);
   * minimum > profile.max_acceptable_experience_years
     -> REJECTED / EXPERIENCE_ABOVE_LIMIT (stop);
   * a minimum above profile.experience_years but within the ceiling is
     NOT rejected; it is recorded as ``above_actual_experience``.
3. Duplicate detection (soft evidence): same normalized title plus the same
   company or a shared contact -> NEEDS_REVIEW / POSSIBLE_DUPLICATE.
   Never rejected.
4. Otherwise SHORTLISTED / KEYWORD_MATCH.

The first triggered flag becomes ``screening_reason``. All flags are kept in
``screening_details["flags"]``. Screening never changes ``JobOpportunity.status``.
"""

import re
from dataclasses import dataclass, field
from functools import reduce
from operator import or_

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from tracker.models import (
    JobOpportunity,
    Profile,
    ScreeningOutcome,
    ScreeningReason,
)

RULES_VERSION = 1
MAX_DUPLICATE_CANDIDATES = 20


class ScreeningInputError(Exception):
    """Invalid screening input. ``field`` names the offending request field."""

    def __init__(self, field_name, message):
        super().__init__(message)
        self.field = field_name
        self.message = message


# ---------------------------------------------------------------------------
# Profile selection
# ---------------------------------------------------------------------------

def select_profile(profile=None):
    """Return ``profile``, or the only existing profile when it is None.

    Zero or several profiles without an explicit choice is an error.
    """
    if profile is not None:
        return profile
    profiles = list(Profile.objects.order_by("id")[:2])
    if not profiles:
        raise ScreeningInputError("profile", "No profile exists. Create a profile first.")
    if len(profiles) > 1:
        raise ScreeningInputError(
            "profile", "Multiple profiles exist. Specify 'profile' explicitly."
        )
    return profiles[0]


def resolve_profile(profile=None):
    """Return ``(profile, skills)`` for screening.

    Uses ``select_profile``; the profile must have at least one skill.
    """
    profile = select_profile(profile)
    skills = list(profile.skills.order_by("id"))
    if not skills:
        raise ScreeningInputError(
            "profile",
            f"Profile {profile.pk} has no skills. Add at least one skill before screening.",
        )
    return profile, skills


# ---------------------------------------------------------------------------
# Keyword relevance
# ---------------------------------------------------------------------------

def skill_pattern(name):
    """Case-insensitive pattern for a skill name with symbol-aware boundaries.

    * ``C`` does not match inside ``C++``/``C#``; ``Java`` not inside ``JavaScript``.
    * ``.NET`` does not match inside ``ASP.NET``; ``js`` not inside ``Node.js``.
    * ``React`` matches ``React.js`` (a trailing ``.js`` suffix is allowed).
    * Inner whitespace matches any whitespace (``machine   learning``).
    """
    tokens = name.split()
    if not tokens:
        return None
    body = r"\s+".join(re.escape(token) for token in tokens)
    return re.compile(rf"(?<![\w.]){body}(?![\w+#])", re.IGNORECASE)


def match_skills(text, skills):
    """Return the skills (in the given order) whose names occur in ``text``."""
    text = text or ""
    matched = []
    for skill in skills:
        pattern = skill_pattern(skill.name)
        if pattern and pattern.search(text):
            matched.append(skill)
    return matched


# ---------------------------------------------------------------------------
# Experience parsing (no guessing)
# ---------------------------------------------------------------------------

_NUM = r"(?<![\d.])(\d{1,2}(?:\.\d+)?)"  # not the tail of a longer number
_YEARS = r"(?:years?|yrs?)\b"
_EXPERIENCE_PATTERNS = [
    # (kind, regex); earlier patterns claim their text span first.
    ("upper", re.compile(rf"(?:up\s*to|upto|maximum(?:\s+of)?|max\.?|less\s+than|not\s+more\s+than)\s*{_NUM}\s*\+?\s*{_YEARS}", re.I)),
    ("range", re.compile(rf"{_NUM}\s*(?:-|–|—|to)\s*{_NUM}\s*\+?\s*{_YEARS}", re.I)),
    ("minimum", re.compile(rf"(?:minimum(?:\s+of)?|min\.?|at\s+least)\s*{_NUM}\s*\+?\s*{_YEARS}", re.I)),
    ("plus", re.compile(rf"{_NUM}\s*\+\s*{_YEARS}", re.I)),
    ("plain", re.compile(rf"{_NUM}\s*{_YEARS}", re.I)),
]
_ENTRY_LEVEL = re.compile(
    r"\b(?:freshers?|entry[\s-]level|no\s+(?:prior\s+)?experience\s+(?:is\s+)?required)\b",
    re.I,
)
# In a free-text JD, a "N years" mention only counts when experience wording is
# in the same clause: the window stops at punctuation and at the next number, so
# "founded 20 years ago and need 3+ years experience" does not read as 20.
_EXPERIENCE_CONTEXT = re.compile(r"experienc|\bexp\b|hands[\s-]on|professional", re.I)
_CLAUSE_BREAK = re.compile(r"[,;.!?\n()\[\]\d]")
_MAX_PLAUSIBLE_YEARS = 40


def _has_experience_context(text, start, end):
    after = text[end:end + 40]
    stop = _CLAUSE_BREAK.search(after)
    if stop:
        after = after[:stop.start()]
    before = text[max(0, start - 30):start]
    breaks = list(_CLAUSE_BREAK.finditer(before))
    if breaks:
        before = before[breaks[-1].end():]
    return bool(_EXPERIENCE_CONTEXT.search(before) or _EXPERIENCE_CONTEXT.search(after))


@dataclass
class ExperienceRequirement:
    min: float | None = None
    max: float | None = None
    source: str = "none"  # "field", "jd" or "none"
    matches: list = field(default_factory=list)

    def as_dict(self):
        return {"min": self.min, "max": self.max, "source": self.source, "matches": self.matches}


def _find_requirements(text, require_context):
    taken = []
    found = []
    for kind, pattern in _EXPERIENCE_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span()
            if any(start < t_end and end > t_start for t_start, t_end in taken):
                continue
            if require_context and not _has_experience_context(text, start, end):
                continue
            numbers = [float(n) for n in match.groups() if n is not None]
            if any(n > _MAX_PLAUSIBLE_YEARS for n in numbers):
                continue
            if kind == "upper":
                low, high = None, numbers[0]
            elif kind == "range":
                low, high = numbers
                if low > high:
                    continue
            else:
                low, high = numbers[0], None
            taken.append((start, end))
            found.append({"text": match.group(0), "min": low, "max": high})
    return found


def parse_experience(field_text, jd_text):
    """Parse the required experience; the dedicated field wins over the JD.

    Several requirements in one text -> the highest minimum applies (all must be
    met). Upper-bound-only phrases ("up to 3 years") leave the minimum unknown.
    """
    for source, text, require_context in (
        ("field", field_text or "", False),
        ("jd", jd_text or "", True),
    ):
        found = _find_requirements(text, require_context)
        if found:
            mins = [f["min"] for f in found if f["min"] is not None]
            maxes = [f["max"] for f in found if f["max"] is not None]
            return ExperienceRequirement(
                min=max(mins) if mins else None,
                max=max(maxes) if maxes else None,
                source=source,
                matches=found,
            )
    for source, text in (("field", field_text or ""), ("jd", jd_text or "")):
        match = _ENTRY_LEVEL.search(text)
        if match:
            return ExperienceRequirement(
                min=0.0, source=source,
                matches=[{"text": match.group(0), "min": 0.0, "max": None}],
            )
    return ExperienceRequirement()


# ---------------------------------------------------------------------------
# Duplicate candidates (soft evidence)
# ---------------------------------------------------------------------------

def find_duplicate_candidates(job):
    """Other jobs with the same normalized title AND same company or a shared contact."""
    company_name = job.company.normalized_name if job.company_id else None
    contact_ids = set(job.contacts.values_list("id", flat=True))
    conditions = []
    if company_name:
        conditions.append(Q(company__normalized_name=company_name))
    if contact_ids:
        conditions.append(Q(contacts__in=contact_ids))
    if not job.normalized_title or not conditions:
        return []

    candidates = (
        JobOpportunity.objects.filter(normalized_title=job.normalized_title)
        .exclude(pk=job.pk)
        .filter(reduce(or_, conditions))
        .distinct()
        .select_related("company")
        .prefetch_related("contacts")
        .order_by("id")[:MAX_DUPLICATE_CANDIDATES]
    )
    result = []
    for candidate in candidates:
        matched_on = ["title"]
        if company_name and candidate.company and candidate.company.normalized_name == company_name:
            matched_on.append("company")
        if contact_ids & {c.id for c in candidate.contacts.all()}:
            matched_on.append("contact")
        result.append({"job_id": candidate.id, "matched_on": matched_on})
    return result


# ---------------------------------------------------------------------------
# Screening
# ---------------------------------------------------------------------------

def screen_job(job, profile, skills):
    """Screen ``job`` against ``profile`` and save the result.

    ``skills`` must be the profile's skills (see ``resolve_profile``).
    Updates parsed experience, ``relevant_keywords`` and the ``screening_*``
    fields. Never changes ``job.status``.
    """
    matched = match_skills(job.full_jd, skills)
    experience = parse_experience(job.required_experience_text, job.full_jd)

    flags = []
    rejected = False
    duplicates = []
    if not matched:
        flags.append(ScreeningReason.NO_KEYWORD_MATCH)
        rejected = True
    else:
        if experience.min is None:
            flags.append(ScreeningReason.EXPERIENCE_UNKNOWN)
        elif experience.min > profile.max_acceptable_experience_years:
            flags.append(ScreeningReason.EXPERIENCE_ABOVE_LIMIT)
            rejected = True
        if not rejected:
            duplicates = find_duplicate_candidates(job)
            if duplicates:
                flags.append(ScreeningReason.POSSIBLE_DUPLICATE)

    if rejected:
        outcome = ScreeningOutcome.REJECTED
    elif flags:
        outcome = ScreeningOutcome.NEEDS_REVIEW
    else:
        outcome = ScreeningOutcome.SHORTLISTED
    reason = flags[0] if flags else ScreeningReason.KEYWORD_MATCH

    experience_details = experience.as_dict()
    experience_details.update(
        {
            "max_acceptable_years": profile.max_acceptable_experience_years,
            "profile_experience_years": profile.experience_years,
            "above_actual_experience": (
                experience.min is not None and experience.min > profile.experience_years
            ),
        }
    )

    job.parsed_min_experience = experience.min
    job.parsed_max_experience = experience.max
    job.screening_outcome = outcome
    job.screening_reason = reason
    job.screening_details = {
        "rules_version": RULES_VERSION,
        "profile_id": profile.pk,
        "matched_keywords": [skill.name for skill in matched],
        "experience": experience_details,
        "flags": [str(flag) for flag in flags],
        "duplicate_candidates": duplicates,
    }
    job.screened_at = timezone.now()

    with transaction.atomic():
        job.save(
            update_fields=[
                "parsed_min_experience",
                "parsed_max_experience",
                "screening_outcome",
                "screening_reason",
                "screening_details",
                "screened_at",
                "updated_at",
            ]
        )
        job.relevant_keywords.set(matched)
    return job
