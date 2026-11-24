"""
Tests for the API foundation: default authentication (session accounts, 401 when signed
out) and OptionalPagination. Account behaviour itself is in test_accounts.py.

All tests are SimpleTestCase, so Django raises if any of this code touches the database.
"""

from django.test import SimpleTestCase
from rest_framework import serializers
from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from tracker.api.auth import SessionAuthentication401
from tracker.api.pagination import OptionalPagination

factory = APIRequestFactory()


class ProtectedView(APIView):
    """Uses project defaults: SessionAuthentication401 + IsAuthenticated."""

    def get(self, request):
        return Response(
            {
                "user": str(request.user),
                "is_authenticated": request.user.is_authenticated,
            }
        )


class ItemSerializer(serializers.Serializer):
    id = serializers.IntegerField()


class ItemListView(ListAPIView):
    """Uses the project default pagination class (OptionalPagination)."""

    authentication_classes = []
    permission_classes = [AllowAny]
    serializer_class = ItemSerializer

    def get_queryset(self):
        return [{"id": i} for i in range(1, 121)]  # 120 items


protected_view = ProtectedView.as_view()
item_list_view = ItemListView.as_view()


def call_protected(authorization=None):
    extra = {}
    if authorization is not None:
        extra["HTTP_AUTHORIZATION"] = authorization
    return protected_view(factory.get("/protected/", **extra))


class ProjectSettingsWiringTests(SimpleTestCase):
    def test_default_authentication_is_session_accounts(self):
        self.assertEqual(
            api_settings.DEFAULT_AUTHENTICATION_CLASSES,
            [SessionAuthentication401],
        )

    def test_default_permission_requires_authentication(self):
        names = [c.__name__ for c in api_settings.DEFAULT_PERMISSION_CLASSES]
        self.assertEqual(names, ["IsAuthenticated"])

    def test_default_pagination_is_optional_pagination(self):
        self.assertIs(api_settings.DEFAULT_PAGINATION_CLASS, OptionalPagination)

    def test_api_urls_module_loads(self):
        from tracker.api import urls

        self.assertTrue(urls.urlpatterns)


class SessionAuthenticationTests(SimpleTestCase):
    """Signed-out requests get 401 with a Session challenge (the frontend then shows sign-in)."""

    def test_signed_out_request_is_401_with_session_challenge(self):
        response = call_protected()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["WWW-Authenticate"], 'Session realm="api"')

    def test_bearer_tokens_are_no_longer_accepted(self):
        response = call_protected("Bearer anything")
        self.assertEqual(response.status_code, 401)


class OptionalPaginationTests(SimpleTestCase):
    def get(self, query=""):
        return item_list_view(factory.get(f"/items/{query}"))

    def assertPaginated(self, response, *, count, size, first_id):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.data), {"count", "next", "previous", "results"}
        )
        self.assertEqual(response.data["count"], count)
        self.assertEqual(len(response.data["results"]), size)
        self.assertEqual(response.data["results"][0]["id"], first_id)

    def test_class_defaults(self):
        self.assertEqual(OptionalPagination.page_size, 50)
        self.assertEqual(OptionalPagination.max_page_size, 200)
        self.assertEqual(OptionalPagination.page_query_param, "page")
        self.assertEqual(OptionalPagination.page_size_query_param, "page_size")

    def test_no_params_returns_plain_list_of_everything(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.data, list)
        self.assertEqual(len(response.data), 120)

    def test_unrelated_params_do_not_trigger_pagination(self):
        response = self.get("?status=SHORTLISTED")
        self.assertIsInstance(response.data, list)
        self.assertEqual(len(response.data), 120)

    def test_page_param_uses_default_page_size(self):
        response = self.get("?page=1")
        self.assertPaginated(response, count=120, size=50, first_id=1)
        self.assertIsNone(response.data["previous"])
        self.assertIn("page=2", response.data["next"])

    def test_last_page(self):
        response = self.get("?page=3")
        self.assertPaginated(response, count=120, size=20, first_id=101)
        self.assertIsNone(response.data["next"])

    def test_page_size_alone_triggers_pagination(self):
        response = self.get("?page_size=10")
        self.assertPaginated(response, count=120, size=10, first_id=1)

    def test_page_and_page_size_together(self):
        response = self.get("?page=2&page_size=25")
        self.assertPaginated(response, count=120, size=25, first_id=26)

    def test_page_size_capped_at_maximum(self):
        items = [{"id": i} for i in range(1, 301)]
        view = ItemListView.as_view(get_queryset=lambda: items)
        response = view(factory.get("/items/?page_size=500"))
        self.assertPaginated(response, count=300, size=200, first_id=1)

    def test_invalid_page_size_falls_back_to_default(self):
        for value in ("abc", "0", "-5"):
            with self.subTest(page_size=value):
                response = self.get(f"?page_size={value}")
                self.assertPaginated(response, count=120, size=50, first_id=1)

    def test_out_of_range_page_returns_404(self):
        self.assertEqual(self.get("?page=99").status_code, 404)

    def test_non_integer_page_returns_404(self):
        self.assertEqual(self.get("?page=abc").status_code, 404)
