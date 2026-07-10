from django.contrib import admin
from .models import SiteConfig, SmsLog


@admin.register(SiteConfig)
class SiteConfigAdmin(admin.ModelAdmin):
    list_display = ("church_name", "field_name", "sms_enabled",
                    "require_expense_approval", "updated_at")


@admin.register(SmsLog)
class SmsLogAdmin(admin.ModelAdmin):
    list_display = ("to", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("to", "message")

from .models import PeriodLock
admin.site.register(PeriodLock)

from .models import YearEndClose, FundCarryForward
admin.site.register(YearEndClose)
admin.site.register(FundCarryForward)

from core.models import Notification
@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("kind","message","recipient","read","created_at")
    list_filter = ("kind","read")


from core.models import InsightStatus, InsightStatusHistory


@admin.register(InsightStatus)
class InsightStatusAdmin(admin.ModelAdmin):
    list_display = ("code", "subject", "state", "updated_by", "updated_at")
    list_filter = ("state", "code")
    search_fields = ("fingerprint", "code", "subject")
    readonly_fields = ("fingerprint",)


@admin.register(InsightStatusHistory)
class InsightStatusHistoryAdmin(admin.ModelAdmin):
    list_display = ("status", "state", "changed_by", "changed_at")
    list_filter = ("state",)

    def has_add_permission(self, request):
        return False
