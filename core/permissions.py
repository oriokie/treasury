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
