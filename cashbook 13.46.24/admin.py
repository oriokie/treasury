from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin
from .models import Expense


@admin.register(Expense)
class ExpenseAdmin(SimpleHistoryAdmin):
    list_display = ("date", "description", "department", "amount", "category",
                    "method", "status", "recorded_by", "approved_by")
    list_filter = ("status", "category", "method", "department")
    search_fields = ("description", "claimant", "voucher_no")
    date_hierarchy = "date"

from .models import RemittanceBatch
admin.site.register(RemittanceBatch)

from .models import FundTransfer
admin.site.register(FundTransfer)
from .models import RecurringExpense
admin.site.register(RecurringExpense)

from cashbook.models import StaffAdvance
@admin.register(StaffAdvance)
class StaffAdvanceAdmin(admin.ModelAdmin):
    list_display = ("staff_name","department","amount","status","date_issued")
    list_filter = ("status",)
