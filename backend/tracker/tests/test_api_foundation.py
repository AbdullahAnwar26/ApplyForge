"""
Tests for the Milestone 1 API foundation: StaticTokenAuthentication and
OptionalPagination.

All tests are SimpleTestCase, so Django raises if any of this code touches the
database. That is how "authentication does not access the database" is checked.
"""

from django.test import SimpleTestCase, override_settings
from rest_framework import serializers
from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from tracker.api.auth import StaticTokenAuthentication, StaticTokenUser
from tracker.api.pagination import OptionalPagination

TOKEN = "test-token-123"

factory = APIRequestFactory()


class ProtectedView(APIView):
    """Uses project defaults: StaticTokenAuthentication + IsAuthenticated."""

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
    def test_default_authentication_is_static_token(self):
        self.assertEqual(
            api_settings.DEFAULT_AUTHENTICATION_CLASSES,
            [StaticTokenAuthentication],
        )

    def test_default_permission_requires_authentication(self):
        names = [c.__name__ for c in api_settings.DEFAULT_PERMISSION_CLASSES]
        self.assertEqual(names, ["IsAuthenticated"])

    def test_default_pagination_is_optional_pagination(self):
        self.assertIs(api_settings.DEFAULT_PAGINATION_CLASS, OptionalPagination)

    def test_api_urls_module_loads(self):
        from tracker.api import urls

        self.assertTrue(urls.urlpatterns)


@override_settings(API_TOKEN=TOKEN)
class StaticTokenAuthenticationTests(SimpleTestCase):
    def assertUnauthorized(self, response):
        self.assertEqual(response.status_code, 401)
        self.assertTrue(response["WWW-Authenticate"].startswith("Bearer"))

    def test_valid_token_authenticates_in_memory_principal(self):
        response = call_protected(f"Bearer {TOKEN}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data, {"user": "api-token", "is_authenticated": True}
        )

    def test_scheme_is_case_insensitive(self):
        self.assertEqual(call_protected(f"bearer {TOKEN}").status_code, 200)

    def test_missing_header_returns_401_with_bearer_challenge(self):
        response = call_protected()
        self.assertUnauthorized(response)
        self.assertEqual(response["WWW-Authenticate"], 'Bearer realm="api"')

    def test_wrong_token_rejected(self):
        self.assertUnauthorized(call_protected("Bearer wrong-token"))

    def test_token_prefix_and_extension_rejected(self):
        self.assertUnauthorized(call_protected(f"Bearer {TOKEN[:-1]}"))
        self.assertUnauthorized(call_protected(f"Bearer {TOKEN}x"))

    def test_wrong_scheme_rejected(self):
        self.assertUnauthorized(call_protected(f"Token {TOKEN}"))
        self.assertUnauthorized(call_protected("Basic dXNlcjpwYXNz"))

    def test_scheme_without_token_rejected(self):
        self.assertUnauthorized(call_protected("Bearer"))

    def test_token_with_spaces_rejected(self):
        self.assertUnauthorized(call_protected(f"Bearer {TOKEN} extra"))

    def test_bare_token_without_scheme_rejected(self):
        self.assertUnauthorized(call_protected(TOKEN))

    def test_non_ascii_token_rejected(self):
        self.assertUnauthorized(call_protected("Bearer tést"))

    def test_authenticate_returns_none_without_header(self):
        request = factory.get("/protected/")
        self.assertIsNone(StaticTokenAuthentication().authenticate(request))

    def test_authenticate_returns_static_user_and_no_db_user(self):
        request = factory.get("/protected/", HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
        user, auth = StaticTokenAuthentication().authenticate(request)
        self.assertIsInstance(user, StaticTokenUser)
        self.assertTrue(user.is_authenticated)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertIsNone(auth)


class StaticTokenFailClosedTests(SimpleTestCase):
    def assertRejected(self, presented):
        response = call_protected(f"Bearer {presented}")
        self.assertEqual(response.status_code, 401)

    @override_settings(API_TOKEN="")
    def test_empty_configured_token_rejects_everything(self):
        self.assertRejected("anything")
        self.assertEqual(call_protected("Bearer ").status_code, 401)

    @override_settings(API_TOKEN="   ")
    def test_whitespace_configured_token_rejects_everything(self):
        self.assertRejected("anything")

    @override_settings(API_TOKEN=None)
    def test_none_configured_token_rejects_everything(self):
        self.assertRejected("None")

    @override_settings()
    def test_absent_configured_token_rejects_everything(self):
        from django.conf import settings

        del settings.API_TOKEN
        self.assertRejected("local-dev-token")
        self.assertEqual(call_protected().status_code, 401)


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
