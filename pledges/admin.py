from django.contrib import admin
from .models import PledgeCampaign, Pledge, PledgePayment, PledgeReminderLog


@admin.register(PledgeCampaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ("name", "status", "goal_amount", "start_date", "end_date")
    list_filter = ("status",)
    search_fields = ("name",)


@admin.register(Pledge)
class PledgeAdmin(admin.ModelAdmin):
    list_display = ("member", "campaign", "amount", "status", "frequency",
                    "start_date", "end_date")
    list_filter = ("status", "frequency", "campaign")
    search_fields = ("member__name", "campaign__name")
    raw_id_fields = ("member",)


@admin.register(PledgePayment)
class PledgePaymentAdmin(admin.ModelAdmin):
    list_display = ("pledge", "amount", "date", "source", "transaction")
    list_filter = ("source",)
    raw_id_fields = ("pledge", "transaction")


@admin.register(PledgeReminderLog)
class ReminderAdmin(admin.ModelAdmin):
    list_display = ("pledge", "channel", "to", "ok", "sent_at")
    list_filter = ("channel", "ok")
