import os

from rest_framework import serializers
from rest_framework.fields import empty

from tracker.ingestion import normalize_phone
from tracker.models import (
    Application,
    ApplicationChannel,
    ApplicationEvent,
    ApplicationStatus,
    Company,
    Contact,
    JobOpportunity,
    JobSource,
    Profile,
    Resume,
    Skill,
)
from tracker.resume_text import ResumeTextError, extract_resume_text


class ProfileSerializer(serializers.ModelSerializer):
    experience_years = serializers.FloatField(min_value=0, required=False)
    max_acceptable_experience_years = serializers.IntegerField(
        min_value=0, required=False
    )

    class Meta:
        model = Profile
        fields = [
            "id",
            "name",
            "professional_summary",
            "target_roles",
            "experience_info",
            "preferred_locations",
            "preferred_work_modes",
            "experience_years",
            "max_acceptable_experience_years",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class SkillSerializer(serializers.ModelSerializer):
    # Uniqueness of (name, profile) is enforced by the model's unique_together,
    # which ModelSerializer turns into a validator (400 on duplicates).

    class Meta:
        model = Skill
        fields = ["id", "name", "category", "profile"]
        read_only_fields = ["id"]


class FormSafeBooleanField(serializers.BooleanField):
    """BooleanField that treats an omitted multipart/form key as "not sent".

    DRF's BooleanField reads a missing HTML-form key as ``False`` (checkbox
    semantics), which would silently create uploaded resumes as inactive.
    """

    default_empty_html = empty


class ResumeSerializer(serializers.ModelSerializer):
    """Resume metadata plus a write-only upload field.

    The storage path and media URL are not exposed: in DEBUG, ``/media/`` is
    served without authentication. Only the original file name is returned.
    """

    file = serializers.FileField(
        source="file_path", write_only=True, required=False
    )
    file_name = serializers.SerializerMethodField()
    file_type = serializers.SerializerMethodField()
    name = serializers.CharField(max_length=255, required=False)
    is_active = FormSafeBooleanField(required=False, default=True)
    is_default = FormSafeBooleanField(required=False, default=False)

    class Meta:
        model = Resume
        fields = [
            "id",
            "profile",
            "name",
            "file",
            "file_name",
            "file_type",
            "resume_type",
            "is_active",
            "is_default",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_file_name(self, obj):
        return os.path.basename(obj.file_path.name) if obj.file_path else None

    def get_file_type(self, obj):
        if not obj.file_path:
            return None
        return os.path.splitext(obj.file_path.name)[1].lstrip(".").lower() or None

    def validate_file(self, upload):
        # Reject unsupported, corrupt or text-less files before saving them.
        try:
            extract_resume_text(upload, upload.name)
        except ResumeTextError as exc:
            raise serializers.ValidationError(str(exc))
        return upload

    def validate(self, attrs):
        if self.instance is None:
            upload = attrs.get("file_path")
            if upload is None:
                raise serializers.ValidationError(
                    {"file": ["A PDF or DOCX resume file is required."]}
                )
            if not attrs.get("name"):
                attrs["name"] = os.path.splitext(os.path.basename(upload.name))[0][:255]
        return attrs


# ---------------------------------------------------------------------------
# Companies, contacts and jobs (Milestone 3)
# ---------------------------------------------------------------------------

class CompanySerializer(serializers.ModelSerializer):
    class Meta:
        model = Company
        fields = ["id", "name", "website", "created_at", "updated_at"]
        read_only_fields = fields


class ContactSerializer(serializers.ModelSerializer):
    class Meta:
        model = Contact
        fields = ["id", "name", "email", "phone", "whatsapp", "company", "created_at", "updated_at"]
        read_only_fields = fields


class CompanySummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Company
        fields = ["id", "name"]
        read_only_fields = fields


class JobListSerializer(serializers.ModelSerializer):
    """Read-only job summary. Internal fields (normalized_title, jd_fingerprint) are not exposed."""

    company = CompanySummarySerializer(read_only=True)
    matched_keywords = serializers.SerializerMethodField()

    class Meta:
        model = JobOpportunity
        fields = [
            "id",
            "title",
            "company",
            "source",
            "source_url",
            "location",
            "work_mode",
            "posted_date",
            "discovered_date",
            "required_experience_text",
            "parsed_min_experience",
            "parsed_max_experience",
            "status",
            "screening_outcome",
            "screening_reason",
            "screened_at",
            "matched_keywords",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_matched_keywords(self, obj):
        # Uses the prefetched relation; ordered in Python to avoid a query.
        return sorted(skill.name for skill in obj.relevant_keywords.all())


class JobDetailSerializer(JobListSerializer):
    contacts = ContactSerializer(many=True, read_only=True)

    class Meta(JobListSerializer.Meta):
        fields = JobListSerializer.Meta.fields + ["full_jd", "screening_details", "contacts"]
        read_only_fields = fields


class JobStatusUpdateSerializer(serializers.ModelSerializer):
    """PATCH jobs/{id}/ - only the user-controlled workflow status is writable."""

    class Meta:
        model = JobOpportunity
        fields = ["status"]
        extra_kwargs = {"status": {"required": True}}


class ContactInputSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    email = serializers.EmailField(max_length=254, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=50, required=False, allow_blank=True)
    whatsapp = serializers.CharField(max_length=50, required=False, allow_blank=True)

    def validate(self, attrs):
        for key in ("phone", "whatsapp"):
            value = attrs.get(key)
            if value and len(normalize_phone(value)) < 7:
                raise serializers.ValidationError({key: ["Must contain at least 7 digits."]})
        if not any(attrs.get(k) for k in ("email", "phone", "whatsapp")):
            raise serializers.ValidationError(
                "A contact needs at least one of: email, phone, whatsapp."
            )
        return attrs


class ProfileSelectionSerializer(serializers.Serializer):
    """Optional ``profile``; resolved by tracker.screening.resolve_profile."""

    profile = serializers.PrimaryKeyRelatedField(
        queryset=Profile.objects.all(), required=False, allow_null=True
    )


class JobIngestSerializer(ProfileSelectionSerializer):
    title = serializers.CharField(max_length=255)
    full_jd = serializers.CharField()
    source = serializers.ChoiceField(choices=JobSource.choices, required=False)
    source_url = serializers.URLField(max_length=1024, required=False, allow_blank=True, allow_null=True)
    company_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    company_website = serializers.URLField(max_length=500, required=False, allow_blank=True)
    contacts = ContactInputSerializer(many=True, required=False)
    location = serializers.CharField(max_length=255, required=False, allow_blank=True)
    work_mode = serializers.CharField(max_length=100, required=False, allow_blank=True)
    posted_date = serializers.DateTimeField(required=False, allow_null=True)
    required_experience_text = serializers.CharField(max_length=255, required=False, allow_blank=True)

    def validate_source_url(self, value):
        if value and not value.lower().startswith(("http://", "https://")):
            raise serializers.ValidationError("Only http and https URLs are accepted.")
        return value


class ScreenAllSerializer(ProfileSelectionSerializer):
    jobs = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False, allow_empty=False
    )


# ---------------------------------------------------------------------------
# Applications (Milestone 4)
# ---------------------------------------------------------------------------

class ApplicationJobSummarySerializer(serializers.ModelSerializer):
    company = CompanySummarySerializer(read_only=True)

    class Meta:
        model = JobOpportunity
        fields = ["id", "title", "company", "status", "screening_outcome"]
        read_only_fields = fields


class ApplicationListSerializer(serializers.ModelSerializer):
    """Read-only summary. Generated body/WhatsApp text only in the detail view."""

    job = ApplicationJobSummarySerializer(read_only=True)

    class Meta:
        model = Application
        fields = [
            "id",
            "profile",
            "job",
            "contact",
            "resume",
            "status",
            "channel",
            "generated_subject",
            "queue_position",
            "created_at",
            "generated_at",
            "opened_at",
            "sent_at",
            "updated_at",
        ]
        read_only_fields = fields


class ApplicationDetailSerializer(ApplicationListSerializer):
    class Meta(ApplicationListSerializer.Meta):
        fields = ApplicationListSerializer.Meta.fields + [
            "generated_body",
            "generated_whatsapp_message",
            "last_error",
        ]
        read_only_fields = fields


class ApplicationCreateSerializer(ProfileSelectionSerializer):
    job = serializers.PrimaryKeyRelatedField(queryset=JobOpportunity.objects.all())
    contact = serializers.PrimaryKeyRelatedField(
        queryset=Contact.objects.all(), required=False, allow_null=True
    )
    resume = serializers.PrimaryKeyRelatedField(
        queryset=Resume.objects.all(), required=False, allow_null=True
    )
    channel = serializers.ChoiceField(choices=ApplicationChannel.choices, required=False)
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class ApplicationUpdateSerializer(serializers.Serializer):
    """PATCH applications/{id}/ - user-editable fields only."""

    status = serializers.ChoiceField(choices=ApplicationStatus.choices, required=False)
    channel = serializers.ChoiceField(choices=ApplicationChannel.choices, required=False)
    contact = serializers.PrimaryKeyRelatedField(
        queryset=Contact.objects.all(), required=False, allow_null=True
    )
    resume = serializers.PrimaryKeyRelatedField(
        queryset=Resume.objects.all(), required=False, allow_null=True
    )
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    # User edits of the drafts before sending (rule 9). No event is recorded.
    generated_subject = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, max_length=500
    )
    generated_body = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, trim_whitespace=False
    )
    generated_whatsapp_message = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, trim_whitespace=False
    )


class ApplicationEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = ApplicationEvent
        fields = ["id", "status", "notes", "timestamp"]
        read_only_fields = fields
