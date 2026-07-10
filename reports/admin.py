from django.contrib import admin

from .models import ReportSnapshot


@admin.register(ReportSnapshot)
class ReportSnapshotAdmin(admin.ModelAdmin):
    """Read-only view of immutable report snapshots. Snapshots are created by the
    snapshot service, never through the admin, and are immutable once finalised —
    so the admin is for inspection/audit only."""
    list_display = ("report_key", "period_end", "generated_at", "generated_by",
                    "report_version", "template_version", "finalised")
    list_filter = ("report_key", "finalised", "report_version")
    date_hierarchy = "generated_at"
    search_fields = ("report_key", "report_title")
    readonly_fields = ("report_key", "report_title", "period_start", "period_end",
                       "generated_at", "generated_by", "report_version",
                       "template_version", "schema_version", "payload",
                       "checksums", "filters", "metrics_used", "component_keys",
                       "render_meta", "finalised")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


from .models import (ReportDefinition, ReportSchedule, ScheduleRun,
                     ReportBranding, ReportUsage, ReportFavourite)


@admin.register(ReportDefinition)
class ReportDefinitionAdmin(admin.ModelAdmin):
    list_display = ("key", "title", "category", "permission", "enabled",
                    "template_version", "owner", "updated_at")
    list_filter = ("category", "enabled", "permission")
    search_fields = ("key", "title")


@admin.register(ReportSchedule)
class ReportScheduleAdmin(admin.ModelAdmin):
    list_display = ("name", "report_key", "frequency", "period_policy",
                    "enabled", "next_run", "last_run", "last_status")
    list_filter = ("frequency", "enabled", "last_status")
    search_fields = ("name", "report_key")


@admin.register(ScheduleRun)
class ScheduleRunAdmin(admin.ModelAdmin):
    list_display = ("schedule", "started_at", "status", "snapshot", "attempt")
    list_filter = ("status",)
    date_hierarchy = "started_at"

    def has_add_permission(self, request):
        return False


@admin.register(ReportBranding)
class ReportBrandingAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "church_name", "conference", "updated_at")
    list_filter = ("is_active",)


@admin.register(ReportUsage)
class ReportUsageAdmin(admin.ModelAdmin):
    list_display = ("report_key", "user", "viewed_at", "render_ms", "export_format")
    list_filter = ("report_key", "export_format")
    date_hierarchy = "viewed_at"

    def has_add_permission(self, request):
        return False


@admin.register(ReportFavourite)
class ReportFavouriteAdmin(admin.ModelAdmin):
    list_display = ("report_key", "user", "created_at")
