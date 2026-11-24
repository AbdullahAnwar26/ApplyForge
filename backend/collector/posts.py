"""
LinkedIn recruiter hiring posts -> ingestion payloads (Milestone 6, Phase 6a).

Pure functions, no browser and no database. ``build_payload`` returns a dict in
the shape ``tracker.api.serializers.JobIngestSerializer`` validates; the
collection service then hands it to ``tracker.ingestion.ingest_job``.

Nothing is invented:

* ``full_jd`` is the post text itself (screening and experience parsing run
  on it later).
* The title is a role phrase taken from the post ("Hiring: X", "Role: X",
  "looking for a X"), otherwise the post's first line.
* Company, location, work mode and experience are only set when the post
  states them; otherwise they are left empty.
* Contacts come only from email addresses and phone numbers written in the post.
"""

import re
from dataclasses import dataclass
from datetime import datetime

MAX_TITLE_CHARS = 120
MIN_TEXT_CHARS = 40
MIN_PHONE_DIGITS = 10
MAX_PHONE_DIGITS = 15


@dataclass(frozen=True)
class RawPost:
    """One collected post, as a source (fake file now, browser in Phase 6b) yields it."""

    text: str
    url: str | None = None
    author_name: str | None = None
    author_headline: str | None = None
    posted_at: datetime | None = None
    # The relative-time label as displayed (e.g. "3h"); for -v 3 diagnostics only,
    # never stored. posted_at above is what ingestion uses.
    posted_label: str | None = None
    # The source could not reveal the whole post (its "… more" control was still
    # there after expansion). Such a post is never treated as a complete JD.
    truncated: bool = False


class PostSkipped(Exception):
    """Not a hiring post. Counted as skipped, not as an error."""


class PostParseError(Exception):
    """A hiring post that cannot be turned into a valid payload."""


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

_HIRING_CUES = re.compile(
    r"#?\bhiring\b|\bwe(?:'re| are)\s+looking\s+for\b|\blooking\s+for\s+(?:an?\s+)?(?:experienced\s+|skilled\s+)?"
    r"[a-z]+\s+(?:developer|engineer|designer|analyst|manager|intern|lead|architect|specialist|consultant|tester)\b"
    r"|\b(?:job|urgent)\s+opening|\bvacanc(?:y|ies)\b|\bopenings?\s+for\b"
    r"|\b(?:send|share|drop|mail|email)\s+(?:your\s+|me\s+your\s+|us\s+your\s+)?(?:updated\s+)?(?:cv|resume|profile)s?\b"
    r"|\bimmediate\s+joiners?\b|\bwalk[\s-]?in\s+(?:drive|interview)\b",
    re.I,
)
_EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w-])")
# Phone candidates: optional +, then digits with spaces/dashes/dots/parentheses.
_PHONE = re.compile(r"(?<![\w+(])\(?\+?\(?\d[\d\s().-]{8,20}\d(?!\w)")
_WHATSAPP_CUE = re.compile(r"whats\s*app|\bwa\b|\bwa\.me\b", re.I)

_LABEL = r"^[^\w\n]*{label}\s*[:\-–]\s*(?P<value>[^\n]+)$"
_TITLE_LABELS = re.compile(
    _LABEL.format(label=r"(?:job\s+title|position|role|designation|opening|profile)"), re.I | re.M
)
_COMPANY_LABELS = re.compile(
    _LABEL.format(label=r"(?:company(?:\s+name)?|hiring\s+company|organi[sz]ation|employer|client)"), re.I | re.M
)
# Values after "at"/"Company:" that are not a company.
_NOT_A_COMPANY = {
    "remote", "hybrid", "onsite", "on-site", "home", "office", "the office", "our team", "us", "our company",
    "the company", "a startup", "a product company", "a leading company", "na", "n/a", "confidential",
}

# Role nouns (singular or plural) that make a phrase a job title.
_ROLE_NOUN = re.compile(
    r"^(?:developer|engineer|architect|analyst|designer|manager|intern|consultant|specialist|tester|"
    r"administrator|scientist|programmer|lead|recruiter|devops|sre|qa)s?$",
    re.I,
)
_WEAK_ROLE_NOUNS = {"lead", "leads", "qa"}  # need a qualifier ("Tech Lead"), alone they are ambiguous
# Words that cannot be part of a role phrase (they end the backward scan).
_ROLE_STOPWORDS = {
    "a", "an", "the", "and", "or", "for", "of", "our", "my", "your", "their", "to", "with", "as", "at", "in",
    "on", "from", "by", "this", "that", "who", "all", "any", "is", "are", "be", "we", "we're", "i", "you",
    "looking", "hiring", "seeking", "need", "needs", "needed", "want", "wanted", "required", "join", "joiners",
    "immediate", "urgent", "urgently", "experienced", "skilled", "talented", "passionate", "motivated",
    "dynamic", "great", "strong", "good", "top", "best", "multiple", "several", "few", "team", "opening",
    "openings", "vacancy", "vacancies", "position", "positions", "role", "roles", "job", "jobs", "now",
}
# A title made ONLY of these words is a generic hiring phrase, not a role.
_GENERIC_TITLE_WORDS = {
    "immediate", "joiner", "joiners", "required", "requirement", "requirements", "urgent", "urgently",
    "hiring", "we", "are", "we're", "were", "now", "actively", "opening", "openings", "vacancy", "vacancies",
    "opportunity", "opportunities", "fresher", "freshers", "walk-in", "walkin", "walk", "in", "position",
    "positions", "multiple", "job", "jobs", "alert", "alerts", "apply", "exciting", "career", "join", "our",
    "team", "needed", "wanted", "available", "open", "new", "role", "roles", "is", "for", "a", "an", "the",
}
_LOCATION_LABELS = re.compile(_LABEL.format(label=r"(?:location|job\s+location|work\s+location|based\s+in)"), re.I | re.M)
_EXPERIENCE_LABELS = re.compile(_LABEL.format(label=r"(?:experience|exp|years\s+of\s+experience)"), re.I | re.M)
_HIRING_PHRASE = re.compile(
    r"(?:#?\bhiring\b(?:\s+(?:for|now))?|we(?:'re| are)\s+looking\s+for|looking\s+for|openings?\s+for)"
    r"\s*[:\-–]?\s*(?:an?\s+|the\s+|(?:\d+\s+)?)?"
    r"(?:(?:experienced|skilled|talented|passionate|motivated|dynamic)\s+)?"
    r"(?P<title>[^\n.!?|,;:()#]{3,100})",
    re.I,
)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s")
_TITLE_STOP = re.compile(
    r"\s+(?:@|(?:at|with|in|for|to\s+join|who|based|location|exp(?:erience)?|immediate|urgently)\b).*$", re.I
)
_AT_COMPANY = re.compile(
    r"\s(?:at|@)\s+(?P<company>[A-Z][\w&.\- ]{1,60}?)(?=\s*(?:[,.!|\n(]|$|\s+(?:in|for|-|–)\s))"
)
_WORK_MODES = (
    ("Remote", re.compile(r"\bremote\b|\bwork\s+from\s+home\b|\bwfh\b", re.I)),
    ("Hybrid", re.compile(r"\bhybrid\b", re.I)),
    ("On-site", re.compile(r"\bon[\s-]?site\b|\bwork\s+from\s+office\b|\bwfo\b|\bin[\s-]office\b", re.I)),
)
_EDGE_JUNK = re.compile(r"^[^\w(]+|[^\w)+#]+$")  # emojis, bullets, punctuation at the ends


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean(value, limit=None):
    value = _EDGE_JUNK.sub("", " ".join((value or "").split()))
    if limit and len(value) > limit:
        value = value[:limit].rsplit(" ", 1)[0] or value[:limit]
    return value


def is_hiring_post(text):
    return bool(_HIRING_CUES.search(text or ""))


# Job seekers' posts ("I am actively looking for new opportunities", "Open to
# work", "Recruiters and hiring managers, ...") share words with hiring posts.
# Observed in the live validation run: 2 of 5 search results were seekers.
_SEEKER_CUES = re.compile(
    r"#?\bopen\s*to\s*work\b"
    r"|\bopen\s+to\s+(?:new\s+|exciting\s+)?(?:job\s+|career\s+|internship\s+)?(?:opportunit|roles?\b|positions?\b|internships?\b)"
    r"|\b(?:i\s+am|i'm|i’m|am)\s+(?:currently\s+|actively\s+|now\s+)*(?:looking|seeking|searching)\s+for\s+"
    r"(?:a\s+|an\s+|new\s+|my\s+next\s+|exciting\s+|full[\s-]time\s+|part[\s-]time\s+|remote\s+)*"
    r"(?:job|role|opportunit|position|internship|opening|career)"
    r"|\bcurrently\s+(?:looking|seeking)\s+for\s*:"
    r"|\brecruiters?\s+(?:and|&)\s+hiring\s+managers\b|\bhiring\s+managers?\s+(?:and|&)\s+recruiters?\b"
    r"|\bhire\s+me\b|\blooking\s+for\s+my\s+next\s+(?:role|opportunity|challenge|position)\b",
    re.I,
)
# Employer language that outweighs seeker cues (e.g. "we are hiring ... open to work visas").
_EMPLOYER_CUES = re.compile(
    r"\bwe(?:'re|’re|\s+are)\s+(?:hiring|looking\s+for)\b|\bjoin\s+(?:our|my)\s+team\b"
    r"|\b(?:send|share|drop|mail|email)\s+(?:your|ur)\s+(?:updated\s+)?(?:cv|resume|profile)\b"
    r"|\bapply\s+(?:now|here|at|via|through)\b|\bjob\s+description\b"
    r"|^\s*(?:responsibilities|requirements|qualifications|role|position|job\s+title)\s*[:\-–]",
    re.I | re.M,
)


def is_job_seeker_post(text):
    """True when the author is looking for work rather than offering it."""
    text = text or ""
    return bool(_SEEKER_CUES.search(text)) and not _EMPLOYER_CUES.search(text)


# Things that look like an email or a phone number but are not one.
_FILE_LIKE_TLD = re.compile(r"\.(?:png|jpe?g|gif|webp|svg|pdf|docx?|zip)$", re.I)
_DATE_LIKE = re.compile(r"^\(?\d{1,4}[./-]\d{1,2}[./-]\d{2,4}(?!\d)")
_TRUNCATION_TAIL = re.compile(r"(?:…|\.\.\.)\s*(?:(?:see|show)\s+more)?\s*$", re.I)


def looks_truncated(text):
    """True when the text visibly stops mid-way ("…", "... see more")."""
    return bool(_TRUNCATION_TAIL.search((text or "").rstrip()))


def _valid_email(email):
    local, _, domain = email.partition("@")
    return bool(local and "." in domain and not _FILE_LIKE_TLD.search(domain)
                and ".." not in email and not domain.startswith("."))


def extract_emails(text):
    seen = []
    for match in _EMAIL.finditer(text or ""):
        email = match.group(0).strip(".").lower()
        if _valid_email(email) and email not in seen:
            seen.append(email)
    return seen


def _phone_problem(raw, digits):
    """Why a phone-shaped string is not a usable number, or None."""
    if not MIN_PHONE_DIGITS <= len(digits) <= MAX_PHONE_DIGITS:
        return "wrong number of digits"
    if _DATE_LIKE.match(raw):
        return "looks like a date"
    if len(set(digits[-MIN_PHONE_DIGITS:])) == 1:
        return "repeated digit"
    return None


def extract_phones(text):
    """Return ``(phones, whatsapp_numbers)`` as written (deduplicated by digits)."""
    phones, whatsapp, seen_digits = [], [], set()
    for line in (text or "").splitlines():
        is_whatsapp_line = bool(_WHATSAPP_CUE.search(line))
        for match in _PHONE.finditer(line):
            raw = match.group(0).strip(" .-")
            digits = re.sub(r"\D", "", raw)
            # Same number with and without country code -> compare the last 10 digits.
            key = digits[-MIN_PHONE_DIGITS:]
            if _phone_problem(raw, digits) or key in seen_digits:
                continue
            seen_digits.add(key)
            (whatsapp if is_whatsapp_line else phones).append(raw)
    return phones, whatsapp


_WORD = re.compile(r"[A-Za-z][\w.+#/'’-]*|[A-Za-z]")  # keeps "We're" as one word


def _words(text):
    return [w.strip(".") or w for w in _WORD.findall(text or "")]


def has_role_noun(title):
    return any(_ROLE_NOUN.match(word) for word in _words(title))


def is_generic_title(title):
    """True for hiring slogans with no role ("Immediate Joiners Required", "We're Hiring")."""
    words = [w.lower() for w in _words(title)]
    return bool(words) and not has_role_noun(title) and all(w in _GENERIC_TITLE_WORDS for w in words)


def find_role_phrase(text, max_lines=15):
    """First "<qualifiers> <role noun>" phrase in the opening lines, e.g. "Python
    Backend Developer" in "Immediate Joiners Required | Python Backend Developer"."""
    for line in (text or "").splitlines()[:max_lines]:
        words = _WORD.findall(line)
        for index, word in enumerate(words):
            if not _ROLE_NOUN.match(word.strip(".")):
                continue
            start = index
            while start > 0 and index - start < 3 and words[start - 1].strip(".").lower() not in _ROLE_STOPWORDS:
                start -= 1
            phrase = words[start:index + 1]
            if len(phrase) == 1 and word.strip(".").lower() in _WEAK_ROLE_NOUNS:
                continue
            return " ".join(phrase)
    return None


def _trim_after_role(title):
    """End a role title at its last role noun: "backend engineers to build our
    platform" -> "backend engineers"."""
    matches = [m for m in _WORD.finditer(title) if _ROLE_NOUN.match(m.group(0).strip("."))]
    return title[: matches[-1].end()].strip() if matches else title


def _tidy_case(title):
    """Capitalise an all-lowercase role title word by word ("backend engineers" ->
    "Backend Engineers"); words with digits or symbols ("node.js") are kept."""
    if title.isupper() and len(title) > 3:
        # ALL-CAPS ("INTERNS", "SENIOR AI DEVELOPER"): title case, keeping short
        # acronyms (AI, QA, SDE, ML) as written.
        return " ".join(
            w if (len(w) <= 3 or not w.isalpha()) else w[0] + w[1:].lower() for w in title.split(" ")
        )
    if title != title.lower():
        return title
    small = {"of", "and", "or", "for", "in", "at", "to", "the", "a", "an"}
    out = []
    for position, word in enumerate(title.split(" ")):
        if word.isalpha() and (position == 0 or word not in small):
            word = word[0].upper() + word[1:]
        out.append(word)
    return " ".join(out)


def extract_title(text):
    """Return ``(title, source)``; source is "label", "hiring_phrase", "role_phrase" or "first_line".

    Order: explicit label; a hiring phrase that names a role; a role phrase in the
    opening lines; any other non-generic hiring phrase; the first sentence. Generic
    slogans ("Immediate Joiners Required") are only used as that last fallback.
    """
    label = _TITLE_LABELS.search(text)
    if label:
        title = _clean(label.group("value"), MAX_TITLE_CHARS)
        if title and not is_generic_title(title):
            return _tidy_case(title), "label"
    phrases = []
    for match in _HIRING_PHRASE.finditer(text):
        title = _clean(_TITLE_STOP.sub("", match.group("title")), MAX_TITLE_CHARS)
        if len(title) >= 3 and not title.lower().startswith(("you", "candidates", "someone", "people")):
            phrases.append(title)
    for title in phrases:
        if has_role_noun(title) and not is_generic_title(title):
            return _tidy_case(_trim_after_role(title)), "hiring_phrase"
    role = find_role_phrase(text)
    if role:
        return _tidy_case(_clean(role, MAX_TITLE_CHARS)), "role_phrase"
    for title in phrases:
        if not is_generic_title(title):
            return title, "hiring_phrase"
    for line in text.splitlines():
        first_sentence = _SENTENCE_END.split(line.strip(), maxsplit=1)[0]
        title = _clean(first_sentence, MAX_TITLE_CHARS)
        if title:
            return title, "first_line"
    return "", "none"


def _company_or_none(value):
    value = _clean(value, 255)
    if not value or value.lower() in _NOT_A_COMPANY or not re.search(r"[A-Za-z]", value):
        return None
    return value


def extract_company(text):
    """A company only when the post names one explicitly ("Company: X", or
    "hiring ... at X" / "@ X" on the hiring line). Never the post's author."""
    label = _COMPANY_LABELS.search(text)
    if label:
        company = _company_or_none(label.group("value"))
        if company:
            return company
    for match in _HIRING_PHRASE.finditer(text):
        line_end = text.find("\n", match.start())
        segment = text[match.start(): line_end if line_end != -1 else len(text)]
        at = _AT_COMPANY.search(segment)
        if at:
            company = _company_or_none(at.group("company"))
            if company:
                return company
    return None


def extract_location(text):
    label = _LOCATION_LABELS.search(text)
    return (_clean(label.group("value"), 255) or None) if label else None


def extract_work_mode(text):
    modes = [name for name, pattern in _WORK_MODES if pattern.search(text)]
    return ", ".join(modes) or None


def extract_experience_text(text):
    label = _EXPERIENCE_LABELS.search(text)
    return (_clean(label.group("value"), 255) or None) if label else None


def contact_evidence(text):
    """Every email/phone found in the post, with the line it was found on.

    Stored with the job so the user can check each extracted detail against the
    post. Only what is written in the post; nothing is looked up or guessed.
    """
    evidence = []
    emails = set(extract_emails(text))
    phones, whatsapp = extract_phones(text)
    phones, whatsapp = set(phones), set(whatsapp)
    for line in (text or "").splitlines():
        snippet = " ".join(line.split())[:300]
        for match in _EMAIL.finditer(line):
            value = match.group(0).strip(".").lower()
            if value in emails and not any(e["value"] == value for e in evidence):
                evidence.append({"kind": "email", "value": value, "evidence": snippet})
        for match in _PHONE.finditer(line):
            raw = match.group(0).strip(" .-")
            if any(e["value"] == raw for e in evidence):
                continue
            if raw in whatsapp:
                evidence.append({"kind": "whatsapp", "value": raw, "evidence": snippet})
            elif raw in phones:
                evidence.append({"kind": "phone", "value": raw, "evidence": snippet})
    return evidence


def build_contacts(text, author_name=None):
    """Contacts from identifiers written in the post.

    One email and at most one phone and one WhatsApp number -> a single
    contact named after the post author. Otherwise one contact per
    identifier, unnamed (which person owns which number is unknown).
    """
    emails = extract_emails(text)
    phones, whatsapp = extract_phones(text)
    if not (emails or phones or whatsapp):
        return []
    if len(emails) <= 1 and len(phones) <= 1 and len(whatsapp) <= 1:
        contact = {"name": _clean(author_name, 255)} if author_name and _clean(author_name) else {}
        if emails:
            contact["email"] = emails[0]
        if phones:
            contact["phone"] = phones[0]
        if whatsapp:
            contact["whatsapp"] = whatsapp[0]
        return [contact]
    return (
        [{"email": e} for e in emails]
        + [{"phone": p} for p in phones]
        + [{"whatsapp": w} for w in whatsapp]
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_payload(post):
    """Turn a RawPost into a JobIngestSerializer payload.

    Raises PostSkipped for non-hiring posts and PostParseError for hiring
    posts without usable text.
    """
    text = (post.text or "").replace("\r\n", "\n").strip()
    if len(text) < MIN_TEXT_CHARS:
        raise PostParseError(f"Post text is shorter than {MIN_TEXT_CHARS} characters.")
    if not is_hiring_post(text):
        raise PostSkipped("No hiring cue found in the post.")
    if is_job_seeker_post(text):
        raise PostSkipped("Job-seeker post (someone looking for work), not a job opening.")

    title, _title_source = extract_title(text)
    if not title:
        raise PostParseError("No title could be taken from the post.")

    truncated = bool(post.truncated) or looks_truncated(text)
    payload = {
        "title": title,
        "full_jd": text,
        "source": "LINKEDIN",
        "contacts": build_contacts(text, post.author_name),
        "jd_complete": not truncated,
        "source_post_text": text,
        "extraction": {
            "title_source": _title_source,
            "contacts": contact_evidence(text),
            "truncated": truncated,
        },
    }
    optional = {
        "source_url": (post.url or "").strip() or None,
        "company_name": extract_company(text),
        "location": extract_location(text),
        "work_mode": extract_work_mode(text),
        "required_experience_text": extract_experience_text(text),
        "posted_date": post.posted_at.isoformat() if post.posted_at else None,
        "recruiter_name": _clean(post.author_name, 255) or None,
        "recruiter_headline": _clean(post.author_headline, 500) or None,
    }
    payload.update({key: value for key, value in optional.items() if value})
    return payload
