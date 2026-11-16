"""
Static bearer-token authentication for the ApplyForge API.

ApplyForge is a single-user tool. Every API request must carry the shared
token configured in ``settings.API_TOKEN``:

    Authorization: Bearer <API_TOKEN>

Behaviour:

* No ``Authorization`` header -> ``authenticate`` returns ``None``; the
  default ``IsAuthenticated`` permission then produces a 401 whose
  ``WWW-Authenticate`` header carries a Bearer challenge.
* Wrong scheme, malformed header, wrong token -> 401 (``AuthenticationFailed``).
* ``API_TOKEN`` missing, ``None``, empty or whitespace-only -> every presented
  token is rejected (fail closed).
* Tokens are compared in constant time (``hmac.compare_digest``).
* No database access: a valid token yields an in-memory principal; no Django
  ``User`` row is read or created.
"""

import hmac

from django.conf import settings
from rest_framework import exceptions
from rest_framework.authentication import (
    BaseAuthentication,
    get_authorization_header,
)

AUTH_SCHEME = "Bearer"
AUTH_REALM = "api"


class StaticTokenUser:
    """In-memory principal representing the holder of the static API token.

    It is not a database model and must not be saved or used as a foreign key.
    """

    # Stable identifier so per-user throttles (ScopedRateThrottle) share one bucket.
    pk = "static-api-token"
    id = pk
    username = "api-token"
    is_authenticated = True
    is_anonymous = False
    is_active = True
    is_staff = False
    is_superuser = False

    def __str__(self):
        return self.username

    def __eq__(self, other):
        return isinstance(other, StaticTokenUser)

    def __hash__(self):
        return hash(self.pk)


def _configured_token():
    """Return the configured token as bytes, or ``None`` if it is unusable."""
    token = getattr(settings, "API_TOKEN", None)
    if not isinstance(token, str) or not token.strip():
        return None
    return token.encode("utf-8")


class StaticTokenAuthentication(BaseAuthentication):
    """Authenticate requests carrying ``Authorization: Bearer <API_TOKEN>``."""

    def authenticate(self, request):
        header = get_authorization_header(request)
        if not header:
            # No credentials supplied; permission classes decide (-> 401).
            return None

        parts = header.split()
        if parts[0].lower() != AUTH_SCHEME.lower().encode("ascii"):
            raise exceptions.AuthenticationFailed(
                "Unsupported authorization scheme. Use 'Bearer <token>'."
            )
        if len(parts) != 2:
            raise exceptions.AuthenticationFailed(
                "Invalid authorization header. Use 'Bearer <token>'."
            )

        expected = _configured_token()
        if expected is None:
            # Fail closed: without a configured token nothing can authenticate.
            raise exceptions.AuthenticationFailed("Invalid token.")

        if not hmac.compare_digest(parts[1], expected):
            raise exceptions.AuthenticationFailed("Invalid token.")

        return (StaticTokenUser(), None)

    def authenticate_header(self, request):
        return f'{AUTH_SCHEME} realm="{AUTH_REALM}"'
