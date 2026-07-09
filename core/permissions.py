"""Class-based-view mixins that gate access by role."""
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib import messages
from django.shortcuts import redirect

from . import roles


class RoleRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Subclass and set `allow_check` to one of the role helpers."""

    allow_check = staticmethod(lambda u: u.is_authenticated)
    permission_message = "You don't have permission to do that."

    def test_func(self):
        return self.allow_check(self.request.user)

    def handle_no_permission(self):
        if self.request.user.is_authenticated:
            # a departmental leader is bounced to their own scoped dashboard,
            # never to a staff page they can't use
            if roles.is_leader(self.request.user):
                return redirect("leader_dashboard")
            messages.error(self.request, self.permission_message)
            return redirect("dashboard")
        return super().handle_no_permission()


class DataEntryRequiredMixin(RoleRequiredMixin):
    allow_check = staticmethod(roles.can_enter_data)
    permission_message = "Data entry requires Treasurer or Assistant role."


class TreasurerRequiredMixin(RoleRequiredMixin):
    allow_check = staticmethod(roles.can_approve)
    permission_message = "This action is restricted to Treasurers."


class AdvanceAccessMixin(RoleRequiredMixin):
    """Staff-advance management: treasurers/assistants, or any user granted the
    'manage staff advances' right."""
    allow_check = staticmethod(roles.can_manage_advances)
    permission_message = "You don't have the staff-advances right."


class LoanViewMixin(RoleRequiredMixin):
    """Read access to loans and lenders."""
    allow_check = staticmethod(roles.can_view_loans)
    permission_message = "You don't have the loans right."


class LoanManageMixin(RoleRequiredMixin):
    """Record loans, receipts, repayments; manage lenders and patterns."""
    allow_check = staticmethod(roles.can_manage_loans)
    permission_message = "You don't have the loans-management right."


class LoanConvertMixin(RoleRequiredMixin):
    """Convert to donation / write off — a treasurer decision."""
    allow_check = staticmethod(roles.can_convert_loans)
    permission_message = "Converting or writing off a loan is restricted to Treasurers."


class LiabilityViewMixin(RoleRequiredMixin):
    """Read access to the liability register. Department leaders are also
    admitted; the view itself scopes them to their own funds."""
    allow_check = staticmethod(
        lambda u: roles.can_view_liabilities(u) or roles.is_leader(u))
    permission_message = "You don't have the liability-transactions right."


class AllocateRequiredMixin(RoleRequiredMixin):
    """Allocate/resolve the giving review queue: treasurers/assistants, or any
    user granted the 'allocate transactions' right."""
    allow_check = staticmethod(roles.can_allocate)
    permission_message = "You don't have the allocation right."


class DebitClassifyRequiredMixin(RoleRequiredMixin):
    """Classify bank-statement debits: treasurers/assistants, or any user granted
    the 'classify debits' right."""
    allow_check = staticmethod(roles.can_classify_debits)
    permission_message = "You don't have the debit-classification right."


class ReadAccessMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Staff read access: Treasurer / Assistant / Auditor / admin.

    Departmental leaders are deliberately EXCLUDED — they are read-only but only
    over their own department, served by the dedicated, scoped leader views. This
    is the security linchpin: it stops a leader account reaching any of the full,
    unscoped office screens that use this mixin.
    """
    def test_func(self):
        return roles.is_staff_role(self.request.user)

    def handle_no_permission(self):
        if self.request.user.is_authenticated:
            if roles.is_leader(self.request.user):
                return redirect("leader_dashboard")
            if roles.is_elder(self.request.user):
                return redirect("elder_dashboard")
            messages.error(self.request, "You don't have permission to view that.")
            return redirect("dashboard")
        return super().handle_no_permission()


class ExecutiveAccessMixin(ReadAccessMixin):
    """The executive overview: staff roles (as ReadAccessMixin already allows),
    plus any user separately granted the view_executive_dashboard right — e.g.
    a church elder, for whom this is granted by default (see
    core.rights.GROUP_RIGHTS), without giving them the rest of the staff-only
    application that ReadAccessMixin alone would unlock."""
    def test_func(self):
        from .rights import has_right
        return super().test_func() or has_right(self.request.user,
                                                 "view_executive_dashboard")


class ReportAccessMixin(ReadAccessMixin):
    """Any report page: staff roles (as ReadAccessMixin already allows), plus
    any user separately granted the view_reports right — e.g. a church elder
    a treasurer has opted into report access. Not granted to elders by
    default (see core.rights.GROUP_RIGHTS); assignable via a profile."""
    def test_func(self):
        from .rights import has_right
        return super().test_func() or has_right(self.request.user, "view_reports")


class ElderRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """The elder's own dashboard: elders (and staff/admins, for setup and
    troubleshooting) — not leaders, not the general public."""
    def test_func(self):
        u = self.request.user
        return u.is_superuser or roles.is_elder(u) or roles.is_staff_role(u)

    def handle_no_permission(self):
        if self.request.user.is_authenticated:
            if roles.is_leader(self.request.user):
                return redirect("leader_dashboard")
            messages.error(self.request, "You don't have permission to view that.")
            return redirect("dashboard")
        return super().handle_no_permission()


class RightRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Gate a view on a granular right (see core.rights). Set `required_right`."""
    required_right = None
    permission_message = "You don't have permission to do that."

    def test_func(self):
        from .rights import has_right
        return has_right(self.request.user, self.required_right)

    def handle_no_permission(self):
        if self.request.user.is_authenticated:
            from . import roles
            if roles.is_leader(self.request.user):
                return redirect("leader_dashboard")
            messages.error(self.request, self.permission_message)
            return redirect("dashboard")
        return super().handle_no_permission()
