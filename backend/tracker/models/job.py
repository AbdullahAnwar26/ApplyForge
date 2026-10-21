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

class JobOpportunity(models.Model):
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

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['source_url'],
                condition=models.Q(source_url__isnull=False) & ~models.Q(source_url=''),
                name='unique_job_source_url',
            ),
            models.UniqueConstraint(
                fields=['jd_fingerprint'],
                condition=models.Q(jd_fingerprint__isnull=False) & ~models.Q(jd_fingerprint=''),
                name='unique_job_jd_fingerprint',
            ),
        ]

    def save(self, *args, **kwargs):
        if self.title:
            self.normalized_title = self.title.lower().strip()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.title} at {self.company.name if self.company else 'Unknown'}"
