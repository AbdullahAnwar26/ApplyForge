"""
Opt-in page-number pagination.

* Neither ``page`` nor ``page_size`` in the query string -> pagination is
  skipped and list endpoints return a plain JSON array.
* Either parameter present -> standard DRF ``PageNumberPagination`` response:
  ``{"count", "next", "previous", "results"}``.

Page size defaults to ``REST_FRAMEWORK["PAGE_SIZE"]`` (50 in this project, 50
if unset) and is capped at 200. Non-positive or non-integer ``page_size``
values fall back to the default (DRF behaviour); an out-of-range or invalid
``page`` returns 404 (DRF behaviour).
"""

from rest_framework.pagination import PageNumberPagination
from rest_framework.settings import api_settings

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


class OptionalPagination(PageNumberPagination):
    page_size = api_settings.PAGE_SIZE or DEFAULT_PAGE_SIZE
    page_query_param = "page"
    page_size_query_param = "page_size"
    max_page_size = MAX_PAGE_SIZE

    def is_requested(self, request):
        params = request.query_params
        return (
            self.page_query_param in params
            or self.page_size_query_param in params
        )

    def paginate_queryset(self, queryset, request, view=None):
        if not self.is_requested(request):
            return None
        return super().paginate_queryset(queryset, request, view)
