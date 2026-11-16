"""
Collector API (Phase 6c), mounted at ``/api/v1/collector/``.

* ``GET  runs/``       run history, newest first. Filters: ``status``, ``trigger``,
                       ``profile``. Paginated only when ``page``/``page_size`` is given.
* ``GET  runs/{id}/``  one run.
* ``POST runs/``       start a LinkedIn collection and wait for it (synchronous, small).

POST body (all optional): ``keywords`` (list of strings; default: the profile's
target roles), ``limit`` (1..COLLECTOR_API_MAX_POSTS_PER_RUN, default
COLLECTOR_API_DEFAULT_POSTS_PER_RUN), ``profile`` (id; required when several
profiles exist). Unknown fields are rejected - in particular there is no file
source over HTTP. Only one run at a time (409 while another is RUNNING).

Responses: 201 SUCCEEDED; 502 PARTIAL/FAILED (source failure or posts that
failed to ingest - what succeeded is kept); 400 invalid input (a FAILED run is
recorded when the collection service rejected it); 409 another run is active;
500 unexpected error (recorded as FAILED, details only in the server log).
Nothing is ever applied for or sent. Throttle scope: ``collector-run``.
"""

import threading

from django.conf import settings
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from tracker.models import Profile
from tracker.screening import ScreeningInputError

from .models import CollectorRun, CollectorRunStatus, CollectorRunTrigger
from .runs import active_run, record_collection
from .service import CollectionInputError

MAX_KEYWORDS = 10
MAX_KEYWORD_LENGTH = 100

# One collection per process at a time; the RUNNING-row check covers other processes.
_run_lock = threading.Lock()


def make_api_source():
    """The post source for API runs: LinkedIn with the saved session (see linkedin_login).
    Module level so tests can replace it; tests never open a live browser."""
    from .linkedin.browser import default_browser
    from .linkedin.source import LinkedInPostSource

    return LinkedInPostSource(browser_factory=lambda: default_browser())


class CollectorRunSerializer(serializers.ModelSerializer):
    duration_seconds = serializers.FloatField(read_only=True)

    class Meta:
        model = CollectorRun
        fields = (
            "id", "status", "trigger", "source", "profile", "keywords", "limit", "counts",
            "job_ids", "error", "started_at", "finished_at", "duration_seconds",
        )
        read_only_fields = fields


class CollectorRunStartSerializer(serializers.Serializer):
    keywords = serializers.ListField(
        child=serializers.CharField(max_length=MAX_KEYWORD_LENGTH, trim_whitespace=True),
        required=False, allow_empty=False, max_length=MAX_KEYWORDS,
    )
    limit = serializers.IntegerField(required=False, min_value=1)
    profile = serializers.PrimaryKeyRelatedField(queryset=Profile.objects.all(), required=False)

    def validate_limit(self, value):
        maximum = min(settings.COLLECTOR_API_MAX_POSTS_PER_RUN, settings.COLLECTOR_MAX_POSTS_PER_RUN)
        if value > maximum:
            raise ValidationError(f"Ensure this value is less than or equal to {maximum}.")
        return value

    def validate(self, attrs):
        unknown = sorted(set(self.initial_data) - set(self.fields))
        if unknown:
            raise ValidationError({name: ["Unknown field."] for name in unknown})
        return attrs


def _choice_param(request, name, choices):
    raw = request.query_params.get(name)
    if raw in (None, ""):
        return None
    if raw not in choices:
        raise ValidationError({name: [f"Must be one of: {', '.join(choices)}."]})
    return raw


def _positive_int_param(request, name):
    raw = request.query_params.get(name)
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise ValidationError({name: ["Must be a positive integer."]})
    return value


class CollectorRunViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = CollectorRunSerializer
    http_method_names = ["get", "post", "head", "options"]
    lookup_value_regex = r"\d+"

    def get_throttles(self):
        # Only starting a run is throttled; history reads are not.
        self.throttle_scope = "collector-run" if self.action == "create" else None
        return super().get_throttles()

    def get_queryset(self):
        queryset = CollectorRun.objects.all()
        if self.action != "list":
            return queryset
        filters = {
            "status": _choice_param(self.request, "status", CollectorRunStatus.values),
            "trigger": _choice_param(self.request, "trigger", CollectorRunTrigger.values),
            "profile_id": _positive_int_param(self.request, "profile"),
        }
        return queryset.filter(**{k: v for k, v in filters.items() if v is not None})

    def create(self, request):
        if not isinstance(request.data, dict):
            raise ValidationError({"non_field_errors": ["Expected a JSON object."]})
        start = CollectorRunStartSerializer(data=request.data)
        start.is_valid(raise_exception=True)
        data = start.validated_data
        limit = data.get("limit", min(settings.COLLECTOR_API_DEFAULT_POSTS_PER_RUN,
                                      settings.COLLECTOR_API_MAX_POSTS_PER_RUN))

        if not _run_lock.acquire(blocking=False):
            return self._conflict(active_run())
        try:
            running = active_run()
            if running is not None:
                return self._conflict(running)
            try:
                run, result = record_collection(
                    make_api_source(), trigger=CollectorRunTrigger.API,
                    keywords=data.get("keywords"), profile=data.get("profile"), limit=limit,
                )
            except (ScreeningInputError, CollectionInputError) as exc:
                return Response(
                    {exc.field: [exc.message], "run": exc.run.pk},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            except Exception as exc:  # recorded as FAILED by record_collection (and logged)
                return Response(
                    {"detail": "The collector run failed unexpectedly; see the server log.",
                     "run": getattr(getattr(exc, "run", None), "pk", None)},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )
        finally:
            _run_lock.release()

        body = {
            "run": CollectorRunSerializer(run).data,
            "outcomes": result.as_dict()["outcomes"],
        }
        code = status.HTTP_201_CREATED if run.status == CollectorRunStatus.SUCCEEDED else status.HTTP_502_BAD_GATEWAY
        if code != status.HTTP_201_CREATED:
            body["detail"] = run.error or f"{run.counts.get('failed', 0)} post(s) failed to ingest."
        return Response(body, status=code)

    @staticmethod
    def _conflict(running):
        return Response(
            {"detail": "Another collector run is in progress.", "run": running.pk if running else None},
            status=status.HTTP_409_CONFLICT,
        )
