from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import (BenevolentCase, BenevolentContribution, BenevolentEventType,
                     BenevolentPayout, BenevolentScheme, CaseAttachment,
                     SchemeBenefitRule, SchemeDependant, SchemeMembership,
                     SchemePolicy)


class BenefitRuleInline(admin.TabularInline):
    model = SchemeBenefitRule
    extra = 0


class EventTypeInline(admin.TabularInline):
    model = BenevolentEventType
    extra = 0


@admin.register(BenevolentScheme)
class SchemeAdmin(SimpleHistoryAdmin):
    list_display = ("name", "code", "kind", "fund", "status", "opened_on")
    list_filter = ("status", "kind")
    search_fields = ("name", "code")
    inlines = [EventTypeInline]


@admin.register(SchemePolicy)
class PolicyAdmin(SimpleHistoryAdmin):
    list_display = ("scheme", "version", "status", "effective_from", "effective_to",
                    "benefit_mode", "contribution_mode")
    list_filter = ("status", "scheme", "benefit_mode")
    inlines = [BenefitRuleInline]
    # a locked version's rules are enforced read-only by the model itself; the
    # admin is not a way around that (save() raises), so nothing extra is needed


class DependantInline(admin.TabularInline):
    model = SchemeDependant
    extra = 0


@admin.register(SchemeMembership)
class MembershipAdmin(SimpleHistoryAdmin):
    list_display = ("number", "member", "scheme", "status", "joined_on")
    list_filter = ("scheme", "status")
    search_fields = ("number", "member__name")
    inlines = [DependantInline]


class PayoutInline(admin.TabularInline):
    model = BenevolentPayout
    extra = 0
    raw_id_fields = ("expense",)


class AttachmentInline(admin.TabularInline):
    model = CaseAttachment
    extra = 0


@admin.register(BenevolentCase)
class CaseAdmin(SimpleHistoryAdmin):
    list_display = ("number", "scheme", "event_type", "beneficiary_display",
                    "event_date", "status", "approved_amount")
    list_filter = ("scheme", "status", "event_type")
    search_fields = ("number", "membership__member__name", "beneficiary_name")
    readonly_fields = ("policy_snapshot", "eligibility_snapshot")
    inlines = [PayoutInline, AttachmentInline]


@admin.register(BenevolentContribution)
class ContributionAdmin(SimpleHistoryAdmin):
    list_display = ("scheme", "membership", "amount", "date", "period_label")
    list_filter = ("scheme",)
    raw_id_fields = ("transaction", "membership", "case")


admin.site.register(BenevolentEventType)


# ---- Phase 2 ---------------------------------------------------------------
from .models import BenevolentSettings, CaseApproval, PolicyProfile, SchemeNominee


@admin.register(BenevolentSettings)
class BenevolentSettingsAdmin(SimpleHistoryAdmin):
    list_display = ("__str__", "automation_enabled", "updated_at")

    def has_add_permission(self, request):
        return not BenevolentSettings.objects.exists()   # a singleton

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PolicyProfile)
class PolicyProfileAdmin(SimpleHistoryAdmin):
    list_display = ("name", "kind", "builtin", "created_at")
    list_filter = ("kind", "builtin")
    search_fields = ("name",)


@admin.register(CaseApproval)
class CaseApprovalAdmin(SimpleHistoryAdmin):
    list_display = ("case", "user", "decision", "amount", "created_at")
    list_filter = ("decision",)


@admin.register(SchemeNominee)
class SchemeNomineeAdmin(SimpleHistoryAdmin):
    list_display = ("name", "membership", "relationship", "share_percent", "is_successor")
    search_fields = ("name",)
