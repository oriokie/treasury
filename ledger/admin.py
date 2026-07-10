from django.contrib import admin
from .models import Account, JournalEntry, JournalLine


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "type", "system_key", "active")
    list_filter = ("type", "active")
    search_fields = ("code", "name")


class LineInline(admin.TabularInline):
    model = JournalLine
    extra = 0


@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):
    list_display = ("date", "memo", "source_type", "source_id")
    list_filter = ("source_type",)
    inlines = [LineInline]
