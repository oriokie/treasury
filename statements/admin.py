from django.contrib import admin
from .models import StatementImport


@admin.register(StatementImport)
class StatementImportAdmin(admin.ModelAdmin):
    list_display = ("filename", "uploaded_by", "uploaded_at", "status",
                    "total_rows", "imported", "duplicates_skipped",
                    "queued_for_review", "failed")
    list_filter = ("status",)
    readonly_fields = ("uploaded_at",)

from .models import BankReconciliation, ReconciliationItem


class ReconItemInline(admin.TabularInline):
    model = ReconciliationItem
    extra = 1


@admin.register(BankReconciliation)
class BankReconciliationAdmin(admin.ModelAdmin):
    list_display = ("statement_date", "bank_balance", "adjusted_balance",
                    "book_balance", "is_reconciled")
    inlines = [ReconItemInline]

from .models import ReconciliationMatch
admin.site.register(ReconciliationMatch)

from statements.models import BankAccount
@admin.register(BankAccount)
class BankAccountAdmin(admin.ModelAdmin):
    list_display = ("name", "bank_name", "kind", "is_default", "active")
    list_filter = ("kind", "active")
