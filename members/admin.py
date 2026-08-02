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


from members.models import MemberTag  # noqa: E402


@admin.register(MemberTag)
class MemberTagAdmin(admin.ModelAdmin):
    """The church defines its own roles — board, committee, Sabbath School —
    and assigns them from the member and pledge pages."""
    list_display = ("name", "description", "active", "member_count")
    list_filter = ("active",)
    search_fields = ("name",)

    def member_count(self, obj):
        return obj.members.count()
    member_count.short_description = "Members"
