from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin
from .models import Member, MemberAlias, PossibleDuplicate


class AliasInline(admin.TabularInline):
    model = MemberAlias
    extra = 1


@admin.register(Member)
class MemberAdmin(SimpleHistoryAdmin):
    list_display = ("name", "phone", "group", "dev_group", "source", "active")
    list_filter = ("group", "source", "active")
    search_fields = ("name", "name_key", "phone")
    inlines = [AliasInline]


@admin.register(PossibleDuplicate)
class PossibleDuplicateAdmin(admin.ModelAdmin):
    list_display = ("member", "created_at", "resolved")
    list_filter = ("resolved",)
