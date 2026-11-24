"""
Tracker API routes, mounted at ``/api/v1/`` by ``config/urls.py``.

All routes use the project defaults: ``SessionAuthentication401`` (user accounts) +
``IsAuthenticated`` and ``OptionalPagination``, and only ever see the signed-in
user's own data. Account routes are under ``auth/`` (``account_views.py``). Routes planned for later
milestones are listed in ``docs/PROJECT_SPEC.md``.
"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .account_views import CsrfView, LoginView, LogoutView, MeView, PasswordChangeView, SignupView
from .sending_views import EmailAccountTestView, EmailAccountView

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
    path("auth/csrf/", CsrfView.as_view(), name="auth-csrf"),
    path("auth/signup/", SignupView.as_view(), name="auth-signup"),
    path("auth/login/", LoginView.as_view(), name="auth-login"),
    path("auth/logout/", LogoutView.as_view(), name="auth-logout"),
    path("auth/me/", MeView.as_view(), name="auth-me"),
    path("auth/password/", PasswordChangeView.as_view(), name="auth-password"),
    path("sending/email-account/", EmailAccountView.as_view(), name="sending-email-account"),
    path("sending/email-account/test/", EmailAccountTestView.as_view(), name="sending-email-account-test"),
    path(
        "skill-categories/",
        SkillCategoryListView.as_view(),
        name="skill-category-list",
    ),
    path("", include(router.urls)),
]
