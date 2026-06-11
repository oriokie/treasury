from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin
from .models import Transaction, AllocationRule


@admin.register(Transaction)
class TransactionAdmin(SimpleHistoryAdmin):
    list_display = ("date", "channel", "direction", "amount", "department",
                    "member", "allocation_status", "core_ref")
    list_filter = ("channel", "direction", "allocation_status", "department")
    search_fields = ("payer_name", "payer_phone", "reference", "core_ref",
                     "bank_receipt", "raw_narration")
    date_hierarchy = "date"
    raw_id_fields = ("member", "department", "statement_import")


@admin.register(AllocationRule)
class AllocationRuleAdmin(admin.ModelAdmin):
    list_display = ("reference", "department", "split_fund", "source", "created_at")
    list_filter = ("source",)
    search_fields = ("reference",)


from .models import SplitFund, SplitComponent


class SplitComponentInline(admin.TabularInline):
    model = SplitComponent
    extra = 2


@admin.register(SplitFund)
class SplitFundAdmin(admin.ModelAdmin):
    list_display = ("name", "percent_total", "active")
    list_filter = ("active",)
    search_fields = ("name",)
    inlines = [SplitComponentInline]

from .models import TransactionReversal
admin.site.register(TransactionReversal)
