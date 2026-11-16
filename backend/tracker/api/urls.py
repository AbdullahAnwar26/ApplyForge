"""
Tracker API routes, mounted at ``/api/v1/`` by ``config/urls.py``.

All routes use the project defaults: ``StaticTokenAuthentication`` +
``IsAuthenticated`` and ``OptionalPagination``. Routes planned for later
milestones are listed in ``docs/PROJECT_SPEC.md``.
"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    ApplicationViewSet,
    CompanyViewSet,
    ContactViewSet,
    JobViewSet,
    ProfileViewSet,
    ResumeViewSet,
    SkillCategoryListView,
    SkillViewSet,
)

router = DefaultRouter(trailing_slash=True)
router.include_root_view = False
router.register("profiles", ProfileViewSet, basename="profile")
router.register("skills", SkillViewSet, basename="skill")
router.register("resumes", ResumeViewSet, basename="resume")
router.register("jobs", JobViewSet, basename="job")
router.register("companies", CompanyViewSet, basename="company")
router.register("contacts", ContactViewSet, basename="contact")
router.register("applications", ApplicationViewSet, basename="application")

urlpatterns = [
    path(
        "skill-categories/",
        SkillCategoryListView.as_view(),
        name="skill-category-list",
    ),
    path("", include(router.urls)),
]
