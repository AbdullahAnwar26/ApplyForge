from django.contrib import admin
from django.urls import include, path

from config.health import health


urlpatterns = [
    path('admin/', admin.site.urls),
    # Unauthenticated health check for proxies/platforms (config/health.py).
    path('api/v1/health/', health, name='health'),
    path('api/v1/', include('tracker.api.urls')),
    path('api/v1/collector/', include('collector.urls')),
]

# Uploaded resumes are private: /media/ is never served (not even with DEBUG=True).
# A user downloads their own resume through the authenticated
# GET /api/v1/resumes/{id}/download/ endpoint, which is limited to their account.