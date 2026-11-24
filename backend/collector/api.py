"""
Collector API, mounted at ``/api/v1/collector/``. Every query is limited to the
signed-in user's runs.

* ``GET  runs/``                run history, newest first. Filters: ``status``,
                                ``trigger``, ``profile``. Paginated with ``page``/``page_size``.
* ``GET  runs/{id}/``           one run, with live progress while it is RUNNING.
* ``GET  runs/{id}/items/``     what happened to each post the run examined.
* ``POST runs/``                start a collection in the background; 202 with the run.
* ``POST runs/{id}/cancel/``    ask a running collection to stop (keeps what it collected).
* ``POST runs/{id}/continue/``  start a new run with the same search for the jobs still missing.

POST runs/ body: ``query`` (required), ``target`` (number of NEW jobs wanted,
1..COLLECTOR_API_MAX_POSTS_PER_RUN), ``date_range`` (``any``, ``past-24h``,
``past-week``, ``past-month``, ``custom`` with ``date_from`` and optional
``date_to``), ``profile`` (id; required when several profiles exist). Unknown
fields are rejected. One collection runs at a time on the server (the LinkedIn
browser profile is shared): 409 while another is RUNNING.

Nothing is ever applied for or sent. Throttle scope for starting: ``collector-run``.
"""

import threading

from django.conf import settings
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from tracker.models import Profile
from tracker.api.ownership import OwnedModelSerializer, OwnedSerializer
from tracker.screening import ScreeningInputError

from . import progressive
from .models import CollectedPost, CollectedPostStatus, CollectorRun, CollectorRunStatus, CollectorRunTrigger, DateRange
from .runs import active_run

MAX_QUERY_LENGTH = 200

# One collection per process at a time; the RUNNING-row check covers other processes.
_run_lock = threading.Lock()


def make_api_source():
    """The post source for API runs: LinkedIn with the saved session (see linkedin_login).
    Module level so tests can replace it; tests never open a live browser."""
    from .linkedin.browser import default_browser
    from .linkedin.source import LinkedInPostSource

    return LinkedInPostSource(browser_factory=lambda: default_browser())


class CollectorRunSerializer(OwnedModelSerializer):
    duration_seconds = serializers.FloatField(read_only=True)
    remaining = serializers.SerializerMethodField()

    class Meta:
        model = CollectorRun
        fields = (
            "id", "status", "trigger", "source", "profile", "keywords", "limit", "counts",
            "job_ids", "error", "started_at", "finished_at", "duration_seconds",
            "search_query", "date_range", "date_from", "date_to", "cancel_requested",
            "stage", "stop_reason", "heartbeat_at", "remaining",
        )
        read_only_fields = fields

    def get_remaining(self, run):
        if not run.limit:
            return None
        return max(0, run.limit - done_towards_target(run))


def done_towards_target(run):
    """Jobs counted towards the run's target: ready to apply (runs before that count
    existed counted every new job)."""
    counts = run.counts or {}
    return int(counts.get(progressive.READY, counts.get("created", 0)))


class CollectedPostSerializer(OwnedModelSerializer):
    class Meta:
        model = CollectedPost
        fields = ("id", "position", "status", "post_url", "title", "job", "matched_by", "reason",
                  "posted_at", "truncated", "created_at")
        read_only_fields = fields


class CollectionStartSerializer(OwnedSerializer):
    query = serializers.CharField(max_length=MAX_QUERY_LENGTH, trim_whitespace=True)
    target = serializers.IntegerField(required=False, min_value=1)
    date_range = serializers.ChoiceField(choices=DateRange.choices, required=False, default=DateRange.ANY)
    date_from = serializers.DateField(required=False, allow_null=True)
    date_to = serializers.DateField(required=False, allow_null=True)
    profile = serializers.PrimaryKeyRelatedField(queryset=Profile.objects.all(), required=False)

    def validate_target(self, value):
        maximum = progressive.max_target()
        if value > maximum:
            raise ValidationError(f"Choose at most {maximum} jobs per run.")
        return value

    def validate(self, attrs):
        unknown = sorted(set(self.initial_data) - set(self.fields))
        if unknown:
            raise ValidationError({name: ["Unknown field."] for name in unknown})
        if attrs.get("date_range") != DateRange.CUSTOM:
            attrs["date_from"] = attrs["date_to"] = None
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
        # Only starting a run is throttled; reads and cancelling are not.
        self.throttle_scope = "collector-run" if self.action in ("create", "continue_run") else None
        return super().get_throttles()

    def get_queryset(self):
        progressive.reconcile_interrupted()
        queryset = CollectorRun.objects.filter(owner=self.request.user)
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
        start = CollectionStartSerializer(data=request.data, context={"request": request})
        start.is_valid(raise_exception=True)
        data = start.validated_data
        default_target = min(settings.COLLECTOR_API_DEFAULT_POSTS_PER_RUN, progressive.max_target())
        return self._start(
            request, query=data["query"], target=data.get("target", default_target),
            date_range=data.get("date_range") or DateRange.ANY, date_from=data.get("date_from"),
            date_to=data.get("date_to"), profile=data.get("profile"),
        )

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        run = self.get_object()
        if run.status != CollectorRunStatus.RUNNING:
            raise ValidationError(
                {"status": [f"This run is not running (it is {run.get_status_display().lower()})."]}
            )
        progressive.request_cancel(run)
        run.refresh_from_db()
        return Response(CollectorRunSerializer(run).data, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=["post"], url_path="continue")
    def continue_run(self, request, pk=None):
        """Collect the jobs a cancelled/interrupted/stopped run did not reach."""
        run = self.get_object()
        if run.status == CollectorRunStatus.RUNNING:
            raise ValidationError({"status": ["This run is still running."]})
        remaining = max(0, (run.limit or 0) - done_towards_target(run))
        if not run.search_query or remaining < 1:
            raise ValidationError({"status": ["Nothing is left to collect for this run."]})
        return self._start(
            request, query=run.search_query, target=min(remaining, progressive.max_target()),
            date_range=run.date_range, date_from=run.date_from, date_to=run.date_to,
            profile=run.profile,
        )

    @action(detail=True, methods=["get"])
    def items(self, request, pk=None):
        run = self.get_object()
        items = run.items.order_by("position")
        item_status = _choice_param(request, "status", CollectedPostStatus.values)
        if item_status:
            items = items.filter(status=item_status)
        context = {"request": request}
        page = self.paginate_queryset(items)
        if page is not None:
            return self.get_paginated_response(CollectedPostSerializer(page, many=True, context=context).data)
        return Response(CollectedPostSerializer(items, many=True, context=context).data)

    def _start(self, request, **params):
        if not _run_lock.acquire(blocking=False):
            return self._conflict(active_run(), request.user)
        try:
            running = active_run()
            if running is not None:
                return self._conflict(running, request.user)
            try:
                run = progressive.start_collection(make_api_source(), owner=request.user, **params)
            except (ScreeningInputError, progressive.CollectionRequestError) as exc:
                return Response({exc.field: [exc.message]}, status=status.HTTP_400_BAD_REQUEST)
        finally:
            _run_lock.release()
        run.refresh_from_db()
        return Response({"run": CollectorRunSerializer(run).data}, status=status.HTTP_202_ACCEPTED)

    @staticmethod
    def _conflict(running, user):
        # One collection runs at a time on the server; another account's run id is not revealed.
        mine = running is not None and running.owner_id == user.pk
        detail = (
            "Your previous collection is still running. Wait for it to finish or cancel it." if mine
            else "Another collection is running on this server. Try again in a few minutes."
        )
        return Response({"detail": detail, "run": running.pk if mine else None},
                        status=status.HTTP_409_CONFLICT)
