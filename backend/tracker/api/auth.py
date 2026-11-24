"""
API authentication (accounts milestone): Django user accounts with session cookies.

* Signing in (``POST auth/login/`` or ``auth/signup/``) starts a Django session; the browser
  sends the session cookie (HttpOnly) with every same-origin request.
* Unsafe requests (POST/PUT/PATCH/DELETE) also need the CSRF token: the ``csrftoken`` cookie
  (set by ``GET auth/csrf/``) copied into the ``X-CSRFToken`` header. Login and sign-up check
  it too, so another site cannot sign a visitor into an account (login CSRF).
* Without a valid session every API request gets ``401`` with
  ``WWW-Authenticate: Session realm="api"``, so the frontend shows the sign-in page.

Every queryset is limited to the signed-in user's own data (see ``tracker.accounts``).
"""

from rest_framework.authentication import SessionAuthentication

AUTH_REALM = "api"


class SessionAuthentication401(SessionAuthentication):
    """DRF session authentication that answers 401 (not 403) when nobody is signed in."""

    def authenticate_header(self, request):
        return f'Session realm="{AUTH_REALM}"'

    def check_csrf(self, request):
        """Enforce CSRF for a request that is not (yet) authenticated, e.g. a login."""
        self.enforce_csrf(request)
