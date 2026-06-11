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
            messages.error(self.request, self.permission_message)
            return redirect("dashboard")
        return super().handle_no_permission()


class DataEntryRequiredMixin(RoleRequiredMixin):
    allow_check = staticmethod(roles.can_enter_data)
    permission_message = "Data entry requires Treasurer or Assistant role."


class TreasurerRequiredMixin(RoleRequiredMixin):
    allow_check = staticmethod(roles.can_approve)
    permission_message = "This action is restricted to Treasurers."


class ReadAccessMixin(LoginRequiredMixin):
    """Any authenticated user (incl. Auditor) may view."""
