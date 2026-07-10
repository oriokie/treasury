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
