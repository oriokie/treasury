from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin
from .models import FixedAsset, DepreciationRule, AssetClass, Location


@admin.register(FixedAsset)
class FixedAssetAdmin(SimpleHistoryAdmin):
    list_display = ("name", "tag", "asset_class", "status", "acquired_on", "cost", "disposed")
    list_filter = ("status", "asset_class", "category", "disposed")
    search_fields = ("name", "tag", "reference", "serial_no")
    autocomplete_fields = ("asset_class", "location_fk", "custodian", "parent")


@admin.register(AssetClass)
class AssetClassAdmin(SimpleHistoryAdmin):
    list_display = ("name", "code", "depreciable", "is_cwip", "default_method", "default_rate", "active")
    search_fields = ("name", "code")


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ("full_path", "church", "active")
    search_fields = ("name",)


admin.site.register(DepreciationRule)
