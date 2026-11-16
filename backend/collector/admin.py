from django.contrib import admin

from .models import CollectorRun


@admin.register(CollectorRun)
class CollectorRunAdmin(admin.ModelAdmin):
    list_display = ("id", "status", "trigger", "source", "profile", "started_at", "finished_at")
    list_filter = ("status", "trigger", "source")
    readonly_fields = [f.name for f in CollectorRun._meta.fields]

    def has_add_permission(self, request):
        return False
