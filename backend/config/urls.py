from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path


urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/v1/', include('tracker.api.urls')),
    # DEFERRED TO PHASE 6c: path('api/v1/collector/', include('collector.urls')).
    # The collector app exists (Phase 6a) but has no API endpoints yet; use the
    # `collect_linkedin` management command. See docs/PROJECT_SPEC.md.
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)