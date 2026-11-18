from django.conf import settings
from django.conf.urls.static import static
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

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)