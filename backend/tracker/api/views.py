import os
from collections import Counter
from datetime import date

from django.db import transaction
from django.http import FileResponse, HttpResponse
from django.utils import timezone
from django.db.models import Count, F, Prefetch, Q
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
    add_note,
    change_status,
    check_status_change,
    create_application,
    follow_ups_due,
    validate_relationships,
)
from tracker.generation import (
    GenerationFailed,
    GenerationInputError,
    generate_for_application,
)
from tracker.ingestion import ingest_job
from tracker.review import select_job, update_job_details
from tracker.sending import (
    SendingError,
    abandon_handoff,
    attachment_name,
    build_preview,
    confirm_handoff,
    confirm_whatsapp_sent,
    draft_file,
    open_compose,
    send_email,
    verify_handoff,
)
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
    SendRecord,
    SendRecordStatus,
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

from .ownership import owned
from .sending_views import (
    ComposeRequestSerializer,
    HandoffConfirmSerializer,
    HandoffRecordSerializer,
    SendRecordSerializer,
    SendRequestSerializer,
    WhatsAppSentSerializer,
    sending_error_response,
)
from .serializers import (
    ApplicationCreateSerializer,
    ApplicationDetailSerializer,
    ApplicationEventSerializer,
    ApplicationListSerializer,
    ApplicationNoteSerializer,
    ApplicationUpdateSerializer,
    CompanySerializer,
    ContactSerializer,
    JobDetailSerializer,
    JobIngestSerializer,
    JobListSerializer,
    JobBulkStatusSerializer,
    JobReviewUpdateSerializer,
    JobSelectionSerializer,
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


def _select_profile_or_400(profile, owner):
    try:
        return select_profile(profile, owner=owner)
    except ScreeningInputError as exc:
        raise ValidationError({exc.field: [exc.message]})


def _review_context(user):
    """Per-request data for the review fields of job serializers (one query each)."""
    resumes = list(owned(Resume.objects.filter(is_active=True).exclude(file_path=""), user)
                   .exclude(file_path__isnull=True).order_by("id"))
    profiles = list(owned(Profile.objects.order_by("id"), user)[:2])
    return {"resumes": resumes, "profile": profiles[0] if len(profiles) == 1 else None}


def _resolve_profile_or_400(profile, owner):
    try:
        return resolve_profile(profile, owner=owner)
    except ScreeningInputError as exc:
        raise ValidationError({exc.field: [exc.message]})


def _today_param(request):
    """The client's calendar day (``?today=YYYY-MM-DD``) for follow-up checks, or None.

    Reminders are calendar days: the browser's day is the right one for "due today".
    Without the parameter the server's local date (settings.TIME_ZONE) is used.
    """
    raw = request.query_params.get("today")
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValidationError({"today": ["Use the format YYYY-MM-DD."]})


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

    def get_queryset(self):
        return owned(Profile.objects.order_by("id"), self.request.user)

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

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
        queryset = owned(Skill.objects.order_by("profile_id", "name", "id"), self.request.user)
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
        queryset = owned(Resume.objects.order_by("profile_id", "-is_default", "id"), self.request.user)
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
    def download(self, request, pk=None):
        """The stored file, for its owner only (another account's id is 404)."""
        resume = self.get_object()
        if not resume.file_path:
            return Response({"detail": "This resume has no uploaded file."}, status=status.HTTP_409_CONFLICT)
        try:
            handle = resume.file_path.open("rb")
        except FileNotFoundError:
            return Response({"detail": "The resume file is missing from storage."}, status=status.HTTP_410_GONE)
        # The same clean name as the email attachment ("<Candidate>_Resume.pdf").
        name = attachment_name(resume.profile.name, os.path.basename(resume.file_path.name))
        response = FileResponse(handle, as_attachment=True, filename=name)
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-store"
        return response

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

    def get_queryset(self):
        return owned(Company.objects.order_by("name", "id"), self.request.user)


class ContactViewSet(viewsets.ReadOnlyModelViewSet):
    """Filter: ``?company=<id>``."""

    serializer_class = ContactSerializer

    def get_queryset(self):
        queryset = owned(Contact.objects.order_by("id"), self.request.user)
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
        queryset = owned(
            JobOpportunity.objects.select_related("company")
            .prefetch_related("relevant_keywords", "contacts", "applications")
            .order_by("-discovered_date", "-id"),
            self.request.user,
        )
        if self.action != "list":
            return queryset

        request = self.request
        run_id = _positive_int_param(request, "run")
        if run_id is not None:
            # Jobs a collection run found (created or recognised as already saved).
            queryset = queryset.filter(
                collected_posts__run_id=run_id, collected_posts__run__owner=request.user
            ).distinct()
        complete = _bool_param(request, "jd_complete")
        if complete is not None:
            queryset = queryset.filter(jd_complete=complete)
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
        search = (request.query_params.get("search") or "").strip()
        if len(search) > 200:
            raise ValidationError({"search": ["Use at most 200 characters."]})
        for term in search.split():
            # Every word must appear somewhere: title, company, location or description.
            queryset = queryset.filter(
                Q(title__icontains=term)
                | Q(company__name__icontains=term)
                | Q(location__icontains=term)
                | Q(full_jd__icontains=term)
            )
        return queryset

    def get_serializer_class(self):
        if self.action == "list":
            return JobListSerializer
        if self.action == "partial_update":
            return JobReviewUpdateSerializer
        return JobDetailSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["review"] = _review_context(self.request.user)
        return context

    def _detail(self, job_id):
        return JobDetailSerializer(self.get_queryset().get(pk=job_id), context=self.get_serializer_context()).data

    def partial_update(self, request, *args, **kwargs):
        allowed = set(JobReviewUpdateSerializer().fields)
        unexpected = sorted(set(request.data) - allowed)
        if unexpected:
            raise ValidationError(
                {field: ["This field is read-only."] for field in unexpected}
            )
        if not request.data:
            raise ValidationError({"non_field_errors": [f"Provide at least one of: {', '.join(sorted(allowed))}."]})
        job = self.get_object()
        serializer = JobReviewUpdateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        try:
            update_job_details(job, serializer.validated_data, request.user)
        except ApplicationWorkflowError as exc:
            raise _workflow_error_400(exc)
        return Response(self._detail(job.pk))

    @action(detail=True, methods=["post"])
    def select(self, request, pk=None):
        """Select this job for application: shortlists it and creates ONE application
        (matching resume and best contact pre-filled). 201 created / 200 already selected."""
        job = self.get_object()
        serializer = ProfileSelectionSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        profile = _select_profile_or_400(serializer.validated_data.get("profile"), request.user)
        try:
            result = select_job(job, profile)
        except ApplicationWorkflowError as exc:
            raise _workflow_error_400(exc)
        return Response(
            {"application": ApplicationDetailSerializer(result.application).data, "created": result.created,
             "job": self._detail(job.pk)},
            status=status.HTTP_201_CREATED if result.created else status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="select")
    def select_many(self, request):
        """Select several jobs at once. Already-selected jobs are reported, never duplicated."""
        serializer = JobSelectionSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        profile = _select_profile_or_400(data.get("profile"), request.user)
        jobs = {job.pk: job for job in self.get_queryset().filter(pk__in=data["jobs"])}
        missing = [job_id for job_id in data["jobs"] if job_id not in jobs]
        if missing:
            raise ValidationError({"jobs": [f"Job {job_id} not found." for job_id in missing]})
        results = []
        for job_id in dict.fromkeys(data["jobs"]):
            try:
                result = select_job(jobs[job_id], profile)
            except ApplicationWorkflowError as exc:
                results.append({"job": job_id, "application": None, "created": False, "error": exc.message})
                continue
            results.append({"job": job_id, "application": result.application.pk, "created": result.created,
                            "status": result.application.status})
        return Response({
            "results": results,
            "created": sum(1 for r in results if r["created"]),
            "already_selected": sum(1 for r in results if r["application"] and not r["created"]),
        })

    @action(detail=False, methods=["post"], url_path="bulk-status")
    def bulk_status(self, request):
        """Mark several jobs as to-review / selected / skipped (no applications are created)."""
        serializer = JobBulkStatusSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        queryset = owned(JobOpportunity.objects.filter(pk__in=data["jobs"]), request.user)
        found = set(queryset.values_list("pk", flat=True))
        missing = [job_id for job_id in data["jobs"] if job_id not in found]
        if missing:
            raise ValidationError({"jobs": [f"Job {job_id} not found." for job_id in missing]})
        updated = queryset.exclude(status=JobStatus.APPLIED).update(status=data["status"], updated_at=timezone.now())
        return Response({"updated": updated})

    @action(detail=False, methods=["post"])
    def ingest(self, request):
        """Idempotent ingestion. 201 when created, 200 for an exact existing match."""
        serializer = JobIngestSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        profile, skills = _resolve_profile_or_400(data.get("profile"), request.user)
        result = ingest_job(data, profile, skills)
        job = self.get_queryset().get(pk=result.job.pk)
        return Response(
            {
                "created": result.created,
                "matched_by": result.matched_by,
                "job": JobDetailSerializer(job, context=self.get_serializer_context()).data,
            },
            status=status.HTTP_201_CREATED if result.created else status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"])
    def screen(self, request, pk=None):
        """Re-screen one job. Never changes ``status``."""
        job = self.get_object()
        serializer = ProfileSelectionSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        profile, skills = _resolve_profile_or_400(serializer.validated_data.get("profile"), request.user)
        screen_job(job, profile, skills)
        return Response(self._detail(job.pk))

    @action(detail=False, methods=["post"], url_path="screen", url_name="screen-all")
    def screen_all(self, request):
        """Re-screen all jobs, or only ``jobs: [ids]``. Returns outcome counts."""
        serializer = ScreenAllSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        profile, skills = _resolve_profile_or_400(serializer.validated_data.get("profile"), request.user)

        jobs = owned(JobOpportunity.objects.select_related("company").order_by("id"), request.user)
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
    UPDATABLE_FIELDS = {"status", "channel", "contact", "resume", "note", "follow_up_on", *DRAFT_FIELDS}

    def get_queryset(self):
        queryset = owned(
            Application.objects.select_related("job__company", "contact", "profile").prefetch_related(
                Prefetch("send_records", queryset=SendRecord.objects.defer("body"))
            ).order_by(
                F("queue_position").asc(nulls_last=True), "-created_at", "-id"
            ),
            self.request.user,
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
        if _bool_param(request, "follow_up_due") is True:
            queryset = follow_ups_due(queryset, today=_today_param(request))
        return queryset

    def get_serializer_class(self):
        if self.action == "list":
            return ApplicationListSerializer
        return ApplicationDetailSerializer

    def create(self, request):
        """Create an application for an explicitly chosen (SHORTLISTED) job."""
        serializer = ApplicationCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            profile = select_profile(data.get("profile"), owner=request.user)
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
        serializer = ApplicationUpdateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if not set(data) - {"note"}:
            raise ValidationError(
                {"non_field_errors": [
                    "Provide at least one of: status, channel, contact, resume, follow_up_on, "
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
            for field in ("contact", "resume", "channel", "follow_up_on", *self.DRAFT_FIELDS):
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

    @action(detail=True, methods=["get"], url_path="send-preview")
    def send_preview(self, request, pk=None):
        """Exactly what would be sent (recipient, sender, subject, body, attachment) and
        its ``version``, which ``send``/``whatsapp-sent`` must echo back. Sends nothing."""
        application = self.get_object()
        channel = _choice_param(request, "channel", ApplicationChannel.values) or application.channel
        return Response(build_preview(application, channel).as_dict())

    @action(detail=True, methods=["post"], throttle_scope="application-send")
    def send(self, request, pk=None):
        """Send the approved email with the resume attached. Requires ``confirm: true`` and the
        preview ``version``. 200 sent / 502 delivery failed (recorded, retry allowed)."""
        application = self.get_object()
        serializer = SendRequestSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            record = send_email(application, version=data["version"], confirm=data["confirm"])
        except SendingError as exc:
            return sending_error_response(exc)
        body = {
            "record": SendRecordSerializer(record).data,
            "application": ApplicationDetailSerializer(self.get_queryset().get(pk=application.pk)).data,
        }
        if record.status != SendRecordStatus.SENT:
            body["detail"] = record.error
            return Response(body, status=status.HTTP_502_BAD_GATEWAY)
        return Response(body)

    @action(detail=True, methods=["post"], url_path="whatsapp-sent", throttle_scope="application-send")
    def whatsapp_sent(self, request, pk=None):
        """Record that the user sent the previewed WhatsApp message themselves."""
        application = self.get_object()
        serializer = WhatsAppSentSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            record = confirm_whatsapp_sent(application, version=data["version"], confirm=data["confirm"])
        except SendingError as exc:
            return sending_error_response(exc)
        return Response({
            "record": SendRecordSerializer(record).data,
            "application": ApplicationDetailSerializer(self.get_queryset().get(pk=application.pk)).data,
        })

    def _handoff_response(self, application, record, http_status=status.HTTP_200_OK, **extra):
        body = {
            "record": SendRecordSerializer(record).data,
            "application": ApplicationDetailSerializer(self.get_queryset().get(pk=application.pk)).data,
            **extra,
        }
        return Response(body, status=http_status)

    @action(detail=True, methods=["post"], throttle_scope="application-send")
    def compose(self, request, pk=None):
        """Open the approved message for the user to send themselves (never sends):
        ``{"channel": "EMAIL", "method": "mailbox"|"eml", "version"}`` or
        ``{"channel": "WHATSAPP", "method": "link", "version"}``. 200 with the record
        (COMPOSE_OPENED; ``open_url`` to open, ``download_url`` for an ``eml`` draft); 502
        when the draft could not be saved (recorded, the application is SEND_FAILED)."""
        application = self.get_object()
        serializer = ComposeRequestSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            record = open_compose(application, channel=data["channel"], method=data["method"],
                                  version=data.get("version"))
        except SendingError as exc:
            return sending_error_response(exc)
        if record.status == SendRecordStatus.FAILED:
            return self._handoff_response(application, record, status.HTTP_502_BAD_GATEWAY, detail=record.error)
        download_url = (f"applications/{application.pk}/compose/draft/?record={record.pk}"
                        if record.provider == "eml_file" else "")
        return self._handoff_response(application, record, download_url=download_url)

    @action(detail=True, methods=["post"], url_path="compose/confirm", throttle_scope="application-send")
    def compose_confirm(self, request, pk=None):
        """``{"record", "confirm": true}``: the user sent the opened message themselves."""
        application = self.get_object()
        serializer = HandoffConfirmSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        try:
            record = confirm_handoff(application, record_id=serializer.validated_data["record"],
                                     confirm=serializer.validated_data["confirm"])
        except SendingError as exc:
            return sending_error_response(exc)
        return self._handoff_response(application, record)

    @action(detail=True, methods=["post"], url_path="compose/verify", throttle_scope="application-send")
    def compose_verify(self, request, pk=None):
        """``{"record"}``: look for the drafted email in the mailbox's Sent folder (read only).
        ``{"verified": true}`` marks it sent; ``false`` changes nothing."""
        application = self.get_object()
        serializer = HandoffRecordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        try:
            record, found = verify_handoff(application, record_id=serializer.validated_data["record"])
        except SendingError as exc:
            return sending_error_response(exc)
        detail = "" if found else (f"No email to {record.recipient} sent after "
                                   f"{timezone.localtime(record.created_at):%H:%M} was found in your Sent folder yet.")
        return self._handoff_response(application, record, verified=found, detail=detail)

    @action(detail=True, methods=["post"], url_path="compose/abandon", throttle_scope="application-send")
    def compose_abandon(self, request, pk=None):
        """``{"record"}``: the opened message was not sent; it can be opened again."""
        application = self.get_object()
        serializer = HandoffRecordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        try:
            record = abandon_handoff(application, record_id=serializer.validated_data["record"])
        except SendingError as exc:
            return sending_error_response(exc)
        return self._handoff_response(application, record)

    @action(detail=True, methods=["get"], url_path="compose/draft")
    def compose_draft(self, request, pk=None):
        """``?record=``: the open draft as an .eml file (resume attached) that desktop email
        apps open as a new message. Owner only; nothing is sent or changed."""
        application = self.get_object()
        record_id = _positive_int_param(request, "record")
        if record_id is None:
            raise ValidationError({"record": ["This parameter is required."]})
        try:
            filename, content = draft_file(application, record_id)
        except SendingError as exc:
            return sending_error_response(exc)
        response = HttpResponse(content, content_type="message/rfc822")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-store"
        return response

    @action(detail=True, methods=["get"], url_path="send-records")
    def send_records(self, request, pk=None):
        application = self.get_object()
        return Response(SendRecordSerializer(application.send_records.all(), many=True).data)

    @action(detail=True, methods=["get"])
    def events(self, request, pk=None):
        """Status history, newest first."""
        application = self.get_object()
        events = application.history.order_by("-timestamp", "-id")
        page = self.paginate_queryset(events)
        if page is not None:
            return self.get_paginated_response(ApplicationEventSerializer(page, many=True).data)
        return Response(ApplicationEventSerializer(events, many=True).data)

    @action(detail=True, methods=["post"])
    def notes(self, request, pk=None):
        """Add a note to the history without changing the status. 201 with the event."""
        application = self.get_object()
        serializer = ApplicationNoteSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        event = add_note(application, serializer.validated_data["note"])
        return Response(ApplicationEventSerializer(event).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """Dashboard counts. Optional ``?profile=<id>``."""
        queryset = owned(Application.objects.select_related("job__company"), request.user)
        profile_id = _positive_int_param(request, "profile")
        if profile_id is not None:
            queryset = queryset.filter(profile_id=profile_id)

        counts = dict(
            queryset.order_by().values_list("status").annotate(n=Count("id")).values_list("status", "n")
        )
        by_status = {value: counts.get(value, 0) for value in ApplicationStatus.values}
        awaiting = queryset.filter(status__in=AWAITING_USER_ACTION)
        due = follow_ups_due(queryset, today=_today_param(request))
        return Response(
            {
                "total": sum(by_status.values()),
                "by_status": by_status,
                # Ever sent: stays counted after a later Follow-up/Interview/Offer/Rejected.
                "sent_total": queryset.filter(sent_at__isnull=False).count(),
                "follow_ups_due": {
                    "count": due.count(),
                    "items": ApplicationListSerializer(due[:10], many=True).data,
                },
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
