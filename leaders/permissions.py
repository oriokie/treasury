"""Access control for the departmental-leader area.

A leader is read-only and may see ONLY the departments assigned to them. Every
leader view inherits LeaderRequiredMixin (gates the role) and resolves the
allowed department set server-side via departments_led_by(); a leader can never
widen that set through a URL or query parameter.
"""
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.shortcuts import redirect

from core import roles


class LeaderRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    def test_func(self):
        return roles.is_leader(self.request.user)

    def handle_no_permission(self):
        # office staff who land here go to the main dashboard; everyone else
        # follows the normal login flow
        if self.request.user.is_authenticated and roles.is_staff_role(self.request.user):
            return redirect("dashboard")
        return super().handle_no_permission()


def allowed_departments(user):
    from departments.models import departments_led_by
    return departments_led_by(user)


def assert_department_allowed(user, dept_id):
    """Return the Department if the leader may see it, else None. Used to guard
    detail views — the id comes from the URL and must be checked, never trusted."""
    return allowed_departments(user).filter(pk=dept_id).first()
