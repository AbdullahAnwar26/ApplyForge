from django.db import models


class Profile(models.Model):
    name = models.CharField(max_length=255)
    professional_summary = models.TextField(blank=True, null=True)
    target_roles = models.TextField(
        help_text="Comma separated list of target roles"
    )
    experience_info = models.TextField(
        blank=True,
        null=True,
        help_text="General experience summary"
    )
    preferred_locations = models.TextField(
        blank=True,
        null=True,
        help_text="Comma separated preferred locations"
    )
    preferred_work_modes = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="e.g. Remote, Hybrid, On-site"
    )
    experience_years = models.FloatField(
    default=1.0,
    help_text="Candidate's actual professional experience in years"
    )   
    max_acceptable_experience_years = models.IntegerField(default=9)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class SkillCategory(models.TextChoices):
    SKILL = 'SKILL', 'Skill'
    ROLE = 'ROLE', 'Role'
    TECHNOLOGY = 'TECHNOLOGY', 'Technology'
    FRAMEWORK = 'FRAMEWORK', 'Framework'
    OTHER = 'OTHER', 'Other'


class Skill(models.Model):
    name = models.CharField(max_length=255)
    category = models.CharField(
        max_length=50,
        choices=SkillCategory.choices,
        default=SkillCategory.SKILL
    )
    profile = models.ForeignKey(
        Profile,
        related_name='skills',
        on_delete=models.CASCADE
    )

    class Meta:
        unique_together = ('name', 'profile')

    def __str__(self):
        return f"{self.name} ({self.category})"


class Resume(models.Model):
    name = models.CharField(max_length=255)

    file_path = models.FileField(
        upload_to='resumes/',
        blank=True,
        null=True,
        help_text="Uploaded resume file"
    )

    profile = models.ForeignKey(
        Profile,
        related_name='resumes',
        on_delete=models.CASCADE
    )

    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)
    resume_type = models.CharField(
        max_length=100,
        blank=True,
        null=True
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if self.is_default:
            Resume.objects.filter(
                profile=self.profile,
                is_default=True
            ).exclude(pk=self.pk).update(is_default=False)

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} - {self.profile.name}"