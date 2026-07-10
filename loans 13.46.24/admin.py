from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import Lender, Loan, LoanAttachment, LoanNarrationPattern, LoanTransaction


@admin.register(Lender)
class LenderAdmin(SimpleHistoryAdmin):
    list_display = ("name", "phone", "member", "status", "source")
    search_fields = ("name", "phone", "national_id")
    list_filter = ("status", "source")


class LoanTransactionInline(admin.TabularInline):
    model = LoanTransaction
    extra = 0
    raw_id_fields = ("receipt_transaction", "income_transaction", "expense")


@admin.register(Loan)
class LoanAdmin(SimpleHistoryAdmin):
    list_display = ("number", "lender", "fund", "loan_date", "status")
    search_fields = ("number", "lender__name")
    list_filter = ("status", "fund")
    inlines = [LoanTransactionInline]


@admin.register(LoanNarrationPattern)
class LoanNarrationPatternAdmin(SimpleHistoryAdmin):
    list_display = ("pattern", "match_type", "kind", "fund", "active", "seeded")
    list_filter = ("kind", "active")


admin.site.register(LoanAttachment)
