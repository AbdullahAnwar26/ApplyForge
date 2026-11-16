"""Collector API routes, mounted at ``/api/v1/collector/`` (see collector/api.py)."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .api import CollectorRunViewSet

router = DefaultRouter(trailing_slash=True)
router.include_root_view = False
router.register("runs", CollectorRunViewSet, basename="collector-run")

urlpatterns = [path("", include(router.urls))]
