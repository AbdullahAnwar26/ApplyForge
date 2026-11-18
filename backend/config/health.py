"""
Health check: ``GET /api/v1/health/`` (Milestone 8.3).

For load balancers, reverse proxies and deployment platforms. Unauthenticated by
design, so it returns only a fixed status vocabulary - never user data, settings,
connection details, paths or exception text.

    200 {"status": "ok", "database": "ok"}
    503 {"status": "unavailable", "database": "unavailable"}

The only work done is opening the default database connection and running the
read-only query ``SELECT 1``. LinkedIn, Ollama, Redis and Celery are deliberately
not checked: they are optional features, not basic application health.

A plain Django view (not DRF): no token authentication, content negotiation,
browsable API or throttling is involved.
"""

import logging

from django.db import DatabaseError, connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_safe

logger = logging.getLogger("django")

HEALTHY = {"status": "ok", "database": "ok"}
UNAVAILABLE = {"status": "unavailable", "database": "unavailable"}


def database_is_available():
    """True when the default database answers ``SELECT 1``. Read-only; never raises."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return True
    except (DatabaseError, OSError) as exc:
        # Log the exception type only: driver messages can contain host names or credentials.
        logger.warning("Health check: database unavailable (%s)", type(exc).__name__)
        return False


@csrf_exempt  # safe: only GET/HEAD are accepted, so other methods get a consistent 405
@never_cache
@require_safe  # GET and HEAD only
def health(request):
    if database_is_available():
        return JsonResponse(HEALTHY, status=200)
    return JsonResponse(UNAVAILABLE, status=503)
