from django.contrib import admin
from .models import Department, DevelopmentGroup


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("name", "parent", "fund_type", "category", "opening_balance",
                    "active")
    list_filter = ("fund_type", "category", "active", "parent")
    search_fields = ("name",)


@admin.register(DevelopmentGroup)
class DevelopmentGroupAdmin(admin.ModelAdmin):
    list_display = ("number", "name", "target", "active")
    list_filter = ("active",)
    search_fields = ("number", "name")

from .models import Budget, BudgetLine
admin.site.register(Budget)
admin.site.register(BudgetLine)
