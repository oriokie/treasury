from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin
from .models import Envelope, EnvelopeLine


class LineInline(admin.TabularInline):
    model = EnvelopeLine
    extra = 0


@admin.register(Envelope)
class EnvelopeAdmin(SimpleHistoryAdmin):
    list_display = ("receipt_no", "date", "contributor_name", "channel", "total", "sms_sent")
    list_filter = ("channel", "date", "sms_sent")
    search_fields = ("receipt_no", "contributor_name")
    inlines = [LineInline]

from envelopes.models import CountSession
@admin.register(CountSession)
class CountSessionAdmin(admin.ModelAdmin):
    list_display = ("date","counted_total","expected_total","recorded_by")
