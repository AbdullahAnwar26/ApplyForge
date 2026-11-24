from django.conf import settings
from django.db import models
from .company import Company, Contact
from .profile import Skill

class JobSource(models.TextChoices):
    LINKEDIN = 'LINKEDIN', 'LinkedIn'
    NAUKRI = 'NAUKRI', 'Naukri'
    INDEED = 'INDEED', 'Indeed'
    COMPANY_WEBSITE = 'COMPANY_WEBSITE', 'Company Website'
    MANUAL = 'MANUAL', 'Manual'
    OTHER = 'OTHER', 'Other'

class JobStatus(models.TextChoices):
    DISCOVERED = 'DISCOVERED', 'Discovered'
    SHORTLISTED = 'SHORTLISTED', 'Shortlisted'
    SKIPPED = 'SKIPPED', 'Skipped'
    APPLIED = 'APPLIED', 'Applied'

class ScreeningOutcome(models.TextChoices):
    """Automatic screening verdict. Kept separate from the user-driven JobStatus."""
    SHORTLISTED = 'SHORTLISTED', 'Shortlisted'
    REJECTED = 'REJECTED', 'Rejected'
    NEEDS_REVIEW = 'NEEDS_REVIEW', 'Needs review'

class ScreeningReason(models.TextChoices):
    KEYWORD_MATCH = 'KEYWORD_MATCH', 'Keyword match'
    NO_KEYWORD_MATCH = 'NO_KEYWORD_MATCH', 'No keyword match'
    EXPERIENCE_ABOVE_LIMIT = 'EXPERIENCE_ABOVE_LIMIT', 'Experience above limit'
    EXPERIENCE_UNKNOWN = 'EXPERIENCE_UNKNOWN', 'Experience unknown'
    POSSIBLE_DUPLICATE = 'POSSIBLE_DUPLICATE', 'Possible duplicate'

class JobOpportunity(models.Model):
    # The account this belongs to (accounts milestone). Every API query is limited to the
    # signed-in user's rows. NULL only for data created before accounts existed; such rows
    # are invisible until claimed (first account created, or `manage.py claim_unowned_data`).
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="jobs", on_delete=models.CASCADE,
        null=True, blank=True, db_index=True,
    )
    title = models.CharField(max_length=255, db_index=True)
    normalized_title = models.CharField(max_length=255, db_index=True)
    company = models.ForeignKey(Company, related_name='jobs', on_delete=models.SET_NULL, null=True, blank=True)
    full_jd = models.TextField()
    jd_fingerprint = models.CharField(max_length=64, db_index=True, help_text="Hash of JD to detect duplicates")

    source = models.CharField(max_length=50, choices=JobSource.choices, default=JobSource.OTHER)
    source_url = models.URLField(max_length=1024, blank=True, null=True, db_index=True)

    posted_date = models.DateTimeField(blank=True, null=True)
    discovered_date = models.DateTimeField(auto_now_add=True)

    location = models.CharField(max_length=255, blank=True, null=True)
    work_mode = models.CharField(max_length=100, blank=True, null=True)

    required_experience_text = models.CharField(max_length=255, blank=True, null=True)
    parsed_min_experience = models.FloatField(blank=True, null=True)
    parsed_max_experience = models.FloatField(blank=True, null=True)

    relevant_keywords = models.ManyToManyField(Skill, related_name='jobs', blank=True)
    status = models.CharField(max_length=50, choices=JobStatus.choices, default=JobStatus.DISCOVERED, db_index=True)

    contacts = models.ManyToManyField(Contact, related_name='jobs', blank=True)

    # Screening results (migration 0008). NULL outcome = not screened yet.
    screening_outcome = models.CharField(max_length=20, choices=ScreeningOutcome.choices, blank=True, null=True, db_index=True)
    screening_reason = models.CharField(max_length=40, choices=ScreeningReason.choices, blank=True, null=True)
    screening_details = models.JSONField(default=dict, blank=True)
    screened_at = models.DateTimeField(blank=True, null=True)

    # Collected posts (Job Automation). The post author as LinkedIn shows them (often the
    # recruiter); the original post text exactly as collected (never edited, while
    # ``full_jd`` may be corrected by the user); whether the whole post could be read.
    recruiter_name = models.CharField(max_length=255, blank=True, default="")
    recruiter_headline = models.CharField(max_length=500, blank=True, default="")
    source_post_text = models.TextField(blank=True, default="")
    # False: the post was cut off ("… more" could not be opened). Such a job is not used
    # for generation until the user pastes the full description.
    jd_complete = models.BooleanField(default=True)
    # {"title_source": str, "truncated": bool,
    #  "contacts": [{"kind": "email"|"phone"|"whatsapp", "value": str, "evidence": str}],
    #  "edited_fields": [str]}  - how each detail was found, so the user can verify it.
    extraction = models.JSONField(default=dict, blank=True)
    # The user checked the extracted contact details against the post.
    contacts_verified_at = models.DateTimeField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # Per owner (accounts milestone): the same posting may be saved by different users.
            models.UniqueConstraint(
                fields=['owner', 'source_url'],
                condition=models.Q(source_url__isnull=False) & ~models.Q(source_url=''),
                name='unique_job_owner_source_url',
            ),
            models.UniqueConstraint(
                fields=['owner', 'jd_fingerprint'],
                condition=models.Q(jd_fingerprint__isnull=False) & ~models.Q(jd_fingerprint=''),
                name='unique_job_owner_jd_fingerprint',
            ),
        ]

    def save(self, *args, **kwargs):
        if self.title:
            self.normalized_title = self.title.lower().strip()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.title} at {self.company.name if self.company else 'Unknown'}"
