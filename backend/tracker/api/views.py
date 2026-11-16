from collections import Counter

from django.db import transaction
from django.db.models import Count, F
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from tracker.applications import (
    AWAITING_USER_ACTION,
    POST_SEND_STATUSES,
    ApplicationWorkflowError,
    DuplicateApplicationError,
    change_status,
    check_status_change,
    create_application,
    validate_relationships,
)
from tracker.generation import (
    GenerationFailed,
    GenerationInputError,
    generate_for_application,
)
from tracker.ingestion import ingest_job
from tracker.models import (
    Application,
    ApplicationChannel,
    ApplicationStatus,
    Company,
    Contact,
    JobOpportunity,
    JobSource,
    JobStatus,
    Profile,
    Resume,
    ScreeningOutcome,
    ScreeningReason,
    Skill,
    SkillCategory,
)
from tracker.resume_text import ResumeTextError, extract_resume_text
from tracker.screening import (
    ScreeningInputError,
    resolve_profile,
    screen_job,
    select_profile,
)

from .serializers import (
    ApplicationCreateSerializer,
    ApplicationDetailSerializer,
    ApplicationEventSerializer,
    ApplicationListSerializer,
    ApplicationUpdateSerializer,
    CompanySerializer,
    ContactSerializer,
    JobDetailSerializer,
    JobIngestSerializer,
    JobListSerializer,
    JobStatusUpdateSerializer,
    ProfileSelectionSerializer,
    ProfileSerializer,
    ResumeSerializer,
    ScreenAllSerializer,
    SkillSerializer,
)


def _positive_int_param(request, name):
    """Return an integer query parameter, or None if absent. Invalid -> 400."""
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise ValidationError({name: ["Must be a positive integer."]})
    return value


def _choice_param(request, name, choices, extra=()):
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return None
    allowed = list(choices) + list(extra)
    if raw not in allowed:
        raise ValidationError({name: [f"Must be one of: {', '.join(allowed)}."]})
    return raw


def _resolve_profile_or_400(profile):
    try:
        return resolve_profile(profile)
    except ScreeningInputError as exc:
        raise ValidationError({exc.field: [exc.message]})


def _bool_param(request, name):
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return None
    lowered = raw.lower()
    if lowered in {"true", "1"}:
        return True
    if lowered in {"false", "0"}:
        return False
    raise ValidationError({name: ["Must be 'true' or 'false'."]})


def _delete_files_on_commit(files):
    """Remove stored files once the database change is committed."""
    storage_items = [(f.storage, f.name) for f in files if f and f.name]

    def _delete():
        for storage, name in storage_items:
            storage.delete(name)

    transaction.on_commit(_delete)


class ProfileViewSet(viewsets.ModelViewSet):
    serializer_class = ProfileSerializer
    queryset = Profile.objects.order_by("id")

    def destroy(self, request, *args, **kwargs):
        profile = self.get_object()
        # Deleting a profile cascades to its applications. Refuse rather
        # than silently erase application history.
        if profile.applications.exists():
            return Response(
                {
                    "detail": (
                        "This profile has applications and cannot be deleted. "
                        "Remove its applications first."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )
        with transaction.atomic():
            files = [r.file_path for r in profile.resumes.all()]
            profile.delete()
            _delete_files_on_commit(files)
        return Response(status=status.HTTP_204_NO_CONTENT)


class SkillViewSet(viewsets.ModelViewSet):
    """Skills/keywords. Filters: ``?profile=<id>``, ``?category=<value>``."""

    serializer_class = SkillSerializer

    def get_queryset(self):
        queryset = Skill.objects.order_by("profile_id", "name", "id")
        profile_id = _positive_int_param(self.request, "profile")
        if profile_id is not None:
            queryset = queryset.filter(profile_id=profile_id)
        category = self.request.query_params.get("category")
        if category:
            if category not in SkillCategory.values:
                raise ValidationError(
                    {"category": [f"Must be one of: {', '.join(SkillCategory.values)}."]}
                )
            queryset = queryset.filter(category=category)
        return queryset


class SkillCategoryListView(APIView):
    """Fixed skill categories (``SkillCategory`` choices). Read-only."""

    def get(self, request):
        return Response(
            [
                {"value": value, "label": label}
                for value, label in SkillCategory.choices
            ]
        )


class ResumeViewSet(viewsets.ModelViewSet):
    """Resumes. Upload with multipart ``file``.

    Filters: ``?profile=<id>``, ``?is_active=true|false``.
    """

    serializer_class = ResumeSerializer

    def get_queryset(self):
        queryset = Resume.objects.order_by("profile_id", "-is_default", "id")
        profile_id = _positive_int_param(self.request, "profile")
        if profile_id is not None:
            queryset = queryset.filter(profile_id=profile_id)
        is_active = _bool_param(self.request, "is_active")
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active)
        return queryset

    def perform_update(self, serializer):
        # The FieldFile object keeps the old name after a new file is assigned.
        old_file = serializer.instance.file_path
        with transaction.atomic():
            resume = serializer.save()
            if old_file and resume.file_path.name != old_file.name:
                _delete_files_on_commit([old_file])

    def perform_destroy(self, instance):
        with transaction.atomic():
            file = instance.file_path
            instance.delete()
            _delete_files_on_commit([file])

    @action(detail=True, methods=["get"])
    def text(self, request, pk=None):
        """Extract and return the resume's text from its stored file."""
        resume = self.get_object()
        if not resume.file_path:
            return Response(
                {"detail": "This resume has no uploaded file."},
                status=status.HTTP_409_CONFLICT,
            )
        try:
            with resume.file_path.open("rb") as stored:
                text = extract_resume_text(stored, resume.file_path.name)
        except FileNotFoundError:
            return Response(
                {"detail": "The resume file is missing from storage."},
                status=status.HTTP_409_CONFLICT,
            )
        except ResumeTextError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        return Response({"id": resume.id, "text": text, "characters": len(text)})


# ---------------------------------------------------------------------------
# Companies, contacts and jobs (Milestone 3)
# ---------------------------------------------------------------------------

class CompanyViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = CompanySerializer
    queryset = Company.objects.order_by("name", "id")


class ContactViewSet(viewsets.ReadOnlyModelViewSet):
    """Filter: ``?company=<id>``."""

    serializer_class = ContactSerializer

    def get_queryset(self):
        queryset = Contact.objects.order_by("id")
        company_id = _positive_int_param(self.request, "company")
        if company_id is not None:
            queryset = queryset.filter(company_id=company_id)
        return queryset


class JobViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Jobs: list/retrieve, PATCH ``status`` only, ingest and screen.

    List filters: ``status``, ``screening_outcome`` (or ``UNSCREENED``),
    ``screening_reason``, ``source``, ``company``, ``profile`` (screened against).
    """

    http_method_names = ["get", "post", "patch", "head", "options"]
    lookup_value_regex = r"\d+"

    def get_queryset(self):
        queryset = (
            JobOpportunity.objects.select_related("company")
            .prefetch_related("relevant_keywords")
            .order_by("-discovered_date", "-id")
        )
        if self.action != "list":
            return queryset.prefetch_related("contacts")

        request = self.request
        job_status = _choice_param(request, "status", JobStatus.values)
        if job_status:
            queryset = queryset.filter(status=job_status)
        outcome = _choice_param(
            request, "screening_outcome", ScreeningOutcome.values, extra=["UNSCREENED"]
        )
        if outcome == "UNSCREENED":
            queryset = queryset.filter(screening_outcome__isnull=True)
        elif outcome:
            queryset = queryset.filter(screening_outcome=outcome)
        reason = _choice_param(request, "screening_reason", ScreeningReason.values)
        if reason:
            queryset = queryset.filter(screening_reason=reason)
        source = _choice_param(request, "source", JobSource.values)
        if source:
            queryset = queryset.filter(source=source)
        company_id = _positive_int_param(request, "company")
        if company_id is not None:
            queryset = queryset.filter(company_id=company_id)
        profile_id = _positive_int_param(request, "profile")
        if profile_id is not None:
            queryset = queryset.filter(screening_details__profile_id=profile_id)
        return queryset

    def get_serializer_class(self):
        if self.action == "list":
            return JobListSerializer
        if self.action == "partial_update":
            return JobStatusUpdateSerializer
        return JobDetailSerializer

    def partial_update(self, request, *args, **kwargs):
        unexpected = sorted(set(request.data) - {"status"})
        if unexpected:
            raise ValidationError(
                {field: ["This field is read-only."] for field in unexpected}
            )
        job = self.get_object()
        serializer = JobStatusUpdateSerializer(job, data=request.data, partial=False)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(JobDetailSerializer(self.get_object()).data)

    @action(detail=False, methods=["post"])
    def ingest(self, request):
        """Idempotent ingestion. 201 when created, 200 for an exact existing match."""
        serializer = JobIngestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        profile, skills = _resolve_profile_or_400(data.get("profile"))
        result = ingest_job(data, profile, skills)
        job = self.get_queryset().get(pk=result.job.pk)
        return Response(
            {
                "created": result.created,
                "matched_by": result.matched_by,
                "job": JobDetailSerializer(job).data,
            },
            status=status.HTTP_201_CREATED if result.created else status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"])
    def screen(self, request, pk=None):
        """Re-screen one job. Never changes ``status``."""
        job = self.get_object()
        serializer = ProfileSelectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile, skills = _resolve_profile_or_400(serializer.validated_data.get("profile"))
        screen_job(job, profile, skills)
        return Response(JobDetailSerializer(self.get_queryset().get(pk=job.pk)).data)

    @action(detail=False, methods=["post"], url_path="screen", url_name="screen-all")
    def screen_all(self, request):
        """Re-screen all jobs, or only ``jobs: [ids]``. Returns outcome counts."""
        serializer = ScreenAllSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile, skills = _resolve_profile_or_400(serializer.validated_data.get("profile"))

        jobs = (
            JobOpportunity.objects.select_related("company").order_by("id")
        )
        requested = serializer.validated_data.get("jobs")
        if requested is not None:
            requested = set(requested)
            jobs = jobs.filter(pk__in=requested)
            missing = requested - set(jobs.values_list("pk", flat=True))
            if missing:
                raise ValidationError(
                    {"jobs": [f"Unknown job ids: {', '.join(map(str, sorted(missing)))}."]}
                )

        counts = Counter()
        with transaction.atomic():
            for job in jobs:
                screen_job(job, profile, skills)
                counts[job.screening_outcome] += 1
        return Response(
            {
                "profile": profile.pk,
                "screened": sum(counts.values()),
                "outcomes": {value: counts.get(value, 0) for value in ScreeningOutcome.values},
            }
        )


# ---------------------------------------------------------------------------
# Applications (Milestone 4)
# ---------------------------------------------------------------------------

def _workflow_error_400(exc):
    return ValidationError({exc.field: [exc.message]})


def _duplicate_409(exc):
    return Response(
        {"detail": str(exc), "application": exc.existing.pk},
        status=status.HTTP_409_CONFLICT,
    )


class ApplicationViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Application tracking. Never sends email or WhatsApp messages.

    List filters: ``profile``, ``job``, ``company``, ``status`` (comma-separated
    allowed), ``channel``, ``awaiting_action=true|false``.
    """

    http_method_names = ["get", "post", "patch", "head", "options"]
    lookup_value_regex = r"\d+"
    throttle_scope = None  # only `generate` is throttled ("application-generation")
    DRAFT_FIELDS = ("generated_subject", "generated_body", "generated_whatsapp_message")
    UPDATABLE_FIELDS = {"status", "channel", "contact", "resume", "note", *DRAFT_FIELDS}

    def get_queryset(self):
        queryset = Application.objects.select_related("job__company").order_by(
            F("queue_position").asc(nulls_last=True), "-created_at", "-id"
        )
        if self.action != "list":
            return queryset
        request = self.request
        for name, lookup in (("profile", "profile_id"), ("job", "job_id"), ("company", "job__company_id")):
            value = _positive_int_param(request, name)
            if value is not None:
                queryset = queryset.filter(**{lookup: value})
        raw_status = request.query_params.get("status")
        if raw_status:
            statuses = [s.strip() for s in raw_status.split(",") if s.strip()]
            invalid = [s for s in statuses if s not in ApplicationStatus.values]
            if invalid or not statuses:
                raise ValidationError(
                    {"status": [f"Must be one or more of: {', '.join(ApplicationStatus.values)}."]}
                )
            queryset = queryset.filter(status__in=statuses)
        channel = _choice_param(request, "channel", ApplicationChannel.values)
        if channel:
            queryset = queryset.filter(channel=channel)
        awaiting = _bool_param(request, "awaiting_action")
        if awaiting is True:
            queryset = queryset.filter(status__in=AWAITING_USER_ACTION)
        elif awaiting is False:
            queryset = queryset.exclude(status__in=AWAITING_USER_ACTION)
        return queryset

    def get_serializer_class(self):
        if self.action == "list":
            return ApplicationListSerializer
        return ApplicationDetailSerializer

    def create(self, request):
        """Create an application for an explicitly chosen (SHORTLISTED) job."""
        serializer = ApplicationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            profile = select_profile(data.get("profile"))
        except ScreeningInputError as exc:
            raise ValidationError({exc.field: [exc.message]})
        try:
            application = create_application(
                profile,
                data["job"],
                contact=data.get("contact"),
                resume=data.get("resume"),
                channel=data.get("channel"),
                note=data.get("note"),
            )
        except ApplicationWorkflowError as exc:
            raise _workflow_error_400(exc)
        except DuplicateApplicationError as exc:
            return _duplicate_409(exc)
        application = self.get_queryset().get(pk=application.pk)
        return Response(
            ApplicationDetailSerializer(application).data, status=status.HTTP_201_CREATED
        )

    def partial_update(self, request, pk=None):
        unexpected = sorted(set(request.data) - self.UPDATABLE_FIELDS)
        if unexpected:
            raise ValidationError({field: ["This field is read-only."] for field in unexpected})
        serializer = ApplicationUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if not set(data) - {"note"}:
            raise ValidationError(
                {"non_field_errors": [
                    "Provide at least one of: status, channel, contact, resume, "
                    "generated_subject, generated_body, generated_whatsapp_message."
                ]}
            )

        with transaction.atomic():
            application = self.get_object()
            application = Application.objects.select_for_update().get(pk=application.pk)
            contact = data["contact"] if "contact" in data else application.contact
            channel = data.get("channel", application.channel)
            check_contact = "contact" in data or "channel" in data
            try:
                validate_relationships(
                    application.profile,
                    application.job,
                    contact=contact if check_contact else None,
                    resume=data.get("resume") if "resume" in data else None,
                    channel=channel,
                )
                if "status" in data:
                    check_status_change(application, data["status"])
            except ApplicationWorkflowError as exc:
                raise _workflow_error_400(exc)
            edits_drafts = any(field in data for field in self.DRAFT_FIELDS)
            if edits_drafts and (
                application.sent_at is not None or application.status in POST_SEND_STATUSES
            ):
                raise ValidationError(
                    {"non_field_errors": ["Drafts of an application that was already sent cannot be edited."]}
                )

            changed = False
            for field in ("contact", "resume", "channel", *self.DRAFT_FIELDS):
                if field in data and getattr(application, field) != data[field]:
                    setattr(application, field, data[field])
                    changed = True
            status_changed = "status" in data and change_status(
                application, data["status"], note=data.get("note")
            )
            if changed and not status_changed:
                application.save()

        return Response(ApplicationDetailSerializer(self.get_queryset().get(pk=application.pk)).data)

    @action(detail=True, methods=["post"], throttle_scope="application-generation")
    def generate(self, request, pk=None):
        """Generate drafts with local Ollama/Qwen. Never sends anything.

        Success: 200 with the application (status GENERATED). Input problems:
        400, nothing changed. Generation failure: 422/502/503/504 with
        ``detail``; ``last_error`` is set, status and earlier drafts are kept.
        """
        application = self.get_object()
        if set(request.data):
            raise ValidationError({"non_field_errors": ["This endpoint takes no body."]})
        try:
            result = generate_for_application(application)
        except GenerationInputError as exc:
            raise ValidationError({exc.field: [exc.message]})
        except GenerationFailed as exc:
            return Response(
                {"detail": exc.message, "application": application.pk},
                status=exc.http_status,
            )
        return Response(
            {
                "application": ApplicationDetailSerializer(
                    self.get_queryset().get(pk=application.pk)
                ).data,
                "model": result.model,
                "truncated": {
                    "job_description": result.jd_truncated,
                    "resume": result.resume_truncated,
                },
            }
        )

    @action(detail=True, methods=["get"])
    def events(self, request, pk=None):
        """Status history, newest first."""
        application = self.get_object()
        events = application.history.order_by("-timestamp", "-id")
        page = self.paginate_queryset(events)
        if page is not None:
            return self.get_paginated_response(ApplicationEventSerializer(page, many=True).data)
        return Response(ApplicationEventSerializer(events, many=True).data)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """Dashboard counts. Optional ``?profile=<id>``."""
        queryset = Application.objects.select_related("job__company")
        profile_id = _positive_int_param(request, "profile")
        if profile_id is not None:
            queryset = queryset.filter(profile_id=profile_id)

        counts = dict(
            queryset.order_by().values_list("status").annotate(n=Count("id")).values_list("status", "n")
        )
        by_status = {value: counts.get(value, 0) for value in ApplicationStatus.values}
        awaiting = queryset.filter(status__in=AWAITING_USER_ACTION)
        return Response(
            {
                "total": sum(by_status.values()),
                "by_status": by_status,
                "awaiting_action": {
                    "count": sum(by_status[s] for s in AWAITING_USER_ACTION),
                    "statuses": list(AWAITING_USER_ACTION),
                    "oldest": ApplicationListSerializer(
                        awaiting.order_by("updated_at", "id")[:10], many=True
                    ).data,
                },
                "recent": ApplicationListSerializer(
                    queryset.order_by("-created_at", "-id")[:10], many=True
                ).data,
            }
        )
