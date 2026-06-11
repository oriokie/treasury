from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin
from .models import FixedAsset, DepreciationRule


@admin.register(FixedAsset)
class FixedAssetAdmin(SimpleHistoryAdmin):
    list_display = ("name", "category", "acquired_on", "cost", "disposed")
    list_filter = ("category", "disposed")
    search_fields = ("name", "reference")


admin.site.register(DepreciationRule)
