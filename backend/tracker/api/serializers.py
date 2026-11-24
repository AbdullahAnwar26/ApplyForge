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
    JobStatus,
    Profile,
    Resume,
    Skill,
)
from tracker.resume_text import ResumeTextError, extract_resume_text

from .ownership import OwnedModelSerializer, OwnedSerializer


class ProfileSerializer(OwnedModelSerializer):
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
            "phone_country_code",
            "experience_years",
            "max_acceptable_experience_years",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_phone_country_code(self, value):
        value = (value or "").strip().lstrip("+")
        if value and (not value.isdigit() or len(value) > 4):
            raise serializers.ValidationError("Enter the country calling code as digits, e.g. 91.")
        return value


class SkillSerializer(OwnedModelSerializer):
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


class ResumeSerializer(OwnedModelSerializer):
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

class CompanySerializer(OwnedModelSerializer):
    class Meta:
        model = Company
        fields = ["id", "name", "website", "created_at", "updated_at"]
        read_only_fields = fields


class ContactSerializer(OwnedModelSerializer):
    class Meta:
        model = Contact
        fields = ["id", "name", "email", "phone", "whatsapp", "company", "created_at", "updated_at"]
        read_only_fields = fields


class CompanySummarySerializer(OwnedModelSerializer):
    class Meta:
        model = Company
        fields = ["id", "name"]
        read_only_fields = fields


class JobListSerializer(OwnedModelSerializer):
    """Read-only job summary. Internal fields (normalized_title, jd_fingerprint) are not exposed.

    Review fields (Job Automation) use the view's prefetches: ``contacts`` and
    ``applications``; ``context["review"]`` carries the user's resumes and profile.
    """

    company = CompanySummarySerializer(read_only=True)
    matched_keywords = serializers.SerializerMethodField()
    contact_summary = serializers.SerializerMethodField()
    application = serializers.SerializerMethodField()
    eligibility = serializers.SerializerMethodField()
    matching_resume = serializers.SerializerMethodField()

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
            "recruiter_name",
            "jd_complete",
            "contacts_verified_at",
            "contact_summary",
            "application",
            "eligibility",
            "matching_resume",
        ]
        read_only_fields = fields

    def get_matched_keywords(self, obj):
        # Uses the prefetched relation; ordered in Python to avoid a query.
        return sorted(skill.name for skill in obj.relevant_keywords.all())

    def _review(self):
        return self.context.get("review") or {}

    def get_contact_summary(self, obj):
        contacts = list(obj.contacts.all())
        return {
            "emails": sorted({c.email for c in contacts if c.email}),
            "phones": sorted({c.phone for c in contacts if c.phone}),
            "whatsapp": sorted({c.whatsapp for c in contacts if c.whatsapp}),
        }

    def get_application(self, obj):
        applications = sorted(obj.applications.all(), key=lambda a: a.pk)
        if not applications:
            return None
        application = applications[0]
        return {"id": application.pk, "status": application.status, "sent_at": application.sent_at}

    def get_eligibility(self, obj):
        from tracker.review import job_eligibility

        return job_eligibility(obj, applications=list(obj.applications.all()), contacts=list(obj.contacts.all()),
                               has_resume=bool(self._review().get("resumes")))

    def get_matching_resume(self, obj):
        from tracker.review import match_resume

        review = self._review()
        profile = review.get("profile")
        applications = sorted(obj.applications.all(), key=lambda a: a.pk)
        resumes = review.get("resumes") or []
        if applications and applications[0].resume_id:
            chosen = next((r for r in resumes if r.pk == applications[0].resume_id), None)
            if chosen:
                return {"id": chosen.pk, "name": chosen.name, "reason": "Chosen for the application"}
        if profile is None:
            return None
        match = match_resume(profile, obj, [r for r in resumes if r.profile_id == profile.pk])
        return {"id": match.resume.pk, "name": match.resume.name, "reason": match.reason} if match else None


class JobDetailSerializer(JobListSerializer):
    contacts = ContactSerializer(many=True, read_only=True)

    class Meta(JobListSerializer.Meta):
        fields = JobListSerializer.Meta.fields + [
            "full_jd", "screening_details", "contacts", "recruiter_headline", "source_post_text", "extraction",
        ]
        read_only_fields = fields


class ContactEditSerializer(OwnedSerializer):
    id = serializers.IntegerField(required=False, min_value=1)
    name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    email = serializers.EmailField(max_length=254, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=50, required=False, allow_blank=True)
    whatsapp = serializers.CharField(max_length=50, required=False, allow_blank=True)

    def validate(self, attrs):
        for key in ("phone", "whatsapp"):
            value = attrs.get(key)
            if value and not 7 <= len(normalize_phone(value)) <= 15:
                raise serializers.ValidationError({key: ["Enter a phone number with 7 to 15 digits."]})
        if "id" not in attrs and not any(attrs.get(k) for k in ("email", "phone", "whatsapp")):
            raise serializers.ValidationError("A contact needs at least one of: email, phone, whatsapp.")
        return attrs


class JobReviewUpdateSerializer(OwnedSerializer):
    """PATCH jobs/{id}/ - the review decision and corrections to extracted details."""

    status = serializers.ChoiceField(choices=JobStatus.choices, required=False)
    title = serializers.CharField(max_length=255, required=False)
    company_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    location = serializers.CharField(max_length=255, required=False, allow_blank=True)
    work_mode = serializers.CharField(max_length=100, required=False, allow_blank=True)
    recruiter_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    required_experience_text = serializers.CharField(max_length=255, required=False, allow_blank=True)
    full_jd = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False)
    contacts = ContactEditSerializer(many=True, required=False)
    contacts_verified = serializers.BooleanField(required=False)


class JobBulkStatusSerializer(OwnedSerializer):
    jobs = serializers.ListField(child=serializers.IntegerField(min_value=1), allow_empty=False, max_length=500)
    status = serializers.ChoiceField(choices=[JobStatus.DISCOVERED, JobStatus.SHORTLISTED, JobStatus.SKIPPED])


class JobStatusUpdateSerializer(OwnedModelSerializer):
    """PATCH jobs/{id}/ - only the user-controlled workflow status is writable."""

    class Meta:
        model = JobOpportunity
        fields = ["status"]
        extra_kwargs = {"status": {"required": True}}


class ContactInputSerializer(OwnedSerializer):
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


class ProfileSelectionSerializer(OwnedSerializer):
    """Optional ``profile``; resolved by tracker.screening.resolve_profile."""

    profile = serializers.PrimaryKeyRelatedField(
        queryset=Profile.objects.all(), required=False, allow_null=True
    )


class JobSelectionSerializer(ProfileSelectionSerializer):
    jobs = serializers.ListField(child=serializers.IntegerField(min_value=1), allow_empty=False, max_length=500)


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
    # Collected posts (Job Automation); all optional for other ingestion clients.
    recruiter_name = serializers.CharField(max_length=255, required=False, allow_blank=True, allow_null=True)
    recruiter_headline = serializers.CharField(max_length=500, required=False, allow_blank=True, allow_null=True)
    source_post_text = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False)
    jd_complete = serializers.BooleanField(required=False, default=True)
    extraction = serializers.JSONField(required=False)

    def validate_extraction(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("Must be an object.")
        return value

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

class ApplicationJobSummarySerializer(OwnedModelSerializer):
    company = CompanySummarySerializer(read_only=True)

    class Meta:
        model = JobOpportunity
        fields = ["id", "title", "company", "status", "screening_outcome"]
        read_only_fields = fields


class ApplicationListSerializer(OwnedModelSerializer):
    """Read-only summary. Generated body/WhatsApp text only in the detail view."""

    job = ApplicationJobSummarySerializer(read_only=True)
    # Per channel: none / open (opened for the user to send) / sent / failed.
    send_state = serializers.SerializerMethodField()
    # The recruiter's address and number for the cards ("" when the post gave none).
    recipient_email = serializers.SerializerMethodField()
    recipient_phone = serializers.SerializerMethodField()
    # What "Send WhatsApp" opens: {"number": "+91…", "note": how the country code was
    # chosen, "problem": why it cannot be opened}. Empty when there is no number.
    whatsapp = serializers.SerializerMethodField()

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
            "follow_up_on",
            "updated_at",
            "send_state",
            "recipient_email",
            "recipient_phone",
            "whatsapp",
        ]
        read_only_fields = fields

    def get_recipient_email(self, application):
        return (application.contact.email or "") if application.contact else ""

    def get_recipient_phone(self, application):
        contact = application.contact
        return (contact.whatsapp or contact.phone or "") if contact else ""

    def get_whatsapp(self, application):
        from tracker.sending import resolve_whatsapp  # sending imports this package's models

        number = self.get_recipient_phone(application)
        if not number:
            return {"number": "", "note": "", "problem": ""}
        digits, problem, note = resolve_whatsapp(application, number)
        return {"number": f"+{digits}" if digits else "", "note": note, "problem": problem}

    def get_send_state(self, application):
        from tracker.sending import channel_states  # sending imports this package's models

        return channel_states(application)


class ApplicationDetailSerializer(ApplicationListSerializer):
    class Meta(ApplicationListSerializer.Meta):
        fields = ApplicationListSerializer.Meta.fields + [
            "generated_body",
            "generated_whatsapp_message",
            "last_error",
            "generation_notes",
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


class ApplicationUpdateSerializer(OwnedSerializer):
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
    # Follow-up reminder day; null clears it.
    follow_up_on = serializers.DateField(required=False, allow_null=True)
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


class ApplicationEventSerializer(OwnedModelSerializer):
    class Meta:
        model = ApplicationEvent
        fields = ["id", "kind", "status", "notes", "timestamp"]
        read_only_fields = fields


class ApplicationNoteSerializer(OwnedSerializer):
    """POST applications/{id}/notes/ - a note without a status change."""

    note = serializers.CharField(max_length=2000)
