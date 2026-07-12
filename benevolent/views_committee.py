"""Phase 6 views — committee roster management, and the consolidated
overrides & exceptions audit view."""
import datetime as dt

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from core.permissions import BenevolentSetupMixin, BenevolentViewMixin

from .forms import CommitteeMemberForm, CommitteeRoleForm, RemoveSeatForm
from .models import (BenevolentCase, BenevolentScheme, CaseEvent, CommitteeMember,
                     MemberAdjustment, MembershipExemption)
from .services import committee as committee_svc


class CommitteeRosterView(BenevolentSetupMixin, View):
    """Who sits on a scheme's committee, and their seat. Configuring this is a
    constitutional decision — the same permission level as publishing a
    policy — not day-to-day case administration."""

    def get(self, request, pk):
        scheme = get_object_or_404(BenevolentScheme, pk=pk)
        return render(request, "benevolent/committee_roster.html", {
            "scheme": scheme,
            "seats": committee_svc.roster(scheme, active_only=False)
                        .select_related("user", "added_by", "removed_by"),
            "form": CommitteeMemberForm(),
            "has_roster": committee_svc.has_roster(scheme),
            "role_choices": CommitteeMember.Role.choices,
        })

    def post(self, request, pk):
        scheme = get_object_or_404(BenevolentScheme, pk=pk)
        form = CommitteeMemberForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Choose a person and a role.")
        else:
            try:
                committee_svc.add_member(
                    scheme, form.cleaned_data["user"], role=form.cleaned_data["role"],
                    added_by=request.user)
            except ValidationError as e:
                messages.error(request, "; ".join(e.messages))
            else:
                messages.success(
                    request, f"{form.cleaned_data['user']} seated as "
                             f"{form.cleaned_data['role']}.")
        return redirect("benevolent_committee_roster", pk=pk)


class CommitteeSeatActionView(BenevolentSetupMixin, View):
    """Change a seat's role, or remove it. The seat's own history — who added
    them, when, who removed them, why — is never deleted; a removal just
    marks the row inactive."""

    def post(self, request, pk, seat_id, action):
        scheme = get_object_or_404(BenevolentScheme, pk=pk)
        seat = get_object_or_404(CommitteeMember, pk=seat_id, scheme=scheme)
        try:
            if action == "role":
                form = CommitteeRoleForm(request.POST)
                if not form.is_valid():
                    messages.error(request, "Choose a role.")
                else:
                    committee_svc.change_role(seat, role=form.cleaned_data["role"],
                                              changed_by=request.user)
                    messages.success(request, f"{seat.user}'s role updated.")
            elif action == "remove":
                form = RemoveSeatForm(request.POST)
                reason = form.data.get("reason", "") if form.is_valid() else ""
                committee_svc.remove_member(seat, removed_by=request.user, reason=reason)
                messages.success(request, f"{seat.user} removed from the committee.")
            else:
                messages.error(request, "Unknown action.")
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
        return redirect("benevolent_committee_roster", pk=pk)


class OverridesExceptionsView(BenevolentViewMixin, View):
    """Every exceptional decision across the module, in one place: cases
    approved despite failing a check, committee votes, exemptions granted,
    and penalties or policy fees charged — the consolidated view a board or
    an external auditor actually wants, rather than four separate screens
    each showing part of the picture.

    Read-only. Nothing is decided here; this is where decisions already made
    elsewhere are reviewed together.
    """
    def get(self, request):
        scheme_id = request.GET.get("scheme")
        since_raw = request.GET.get("since")
        try:
            since = dt.date.fromisoformat(since_raw) if since_raw else \
                dt.date.today() - dt.timedelta(days=90)
        except ValueError:
            since = dt.date.today() - dt.timedelta(days=90)

        cases = (BenevolentCase.objects
                .exclude(override_reason="")
                .filter(approved_at__date__gte=since)
                .select_related("scheme", "approved_by", "policy"))
        exemptions = (MembershipExemption.objects
                     .filter(approved_at__isnull=False, from_date__gte=since)
                     .select_related("membership__member", "membership__scheme",
                                     "granted_by", "approved_by", "policy"))
        adjustments = (MemberAdjustment.objects
                      .filter(approved_at__isnull=False, on__gte=since)
                      .select_related("membership__member", "membership__scheme",
                                      "raised_by", "approved_by", "policy"))
        votes_scheme_filter = {}
        if scheme_id:
            cases = cases.filter(scheme_id=scheme_id)
            exemptions = exemptions.filter(membership__scheme_id=scheme_id)
            adjustments = adjustments.filter(membership__scheme_id=scheme_id)
            votes_scheme_filter = {"case__scheme_id": scheme_id}

        from .models import CaseApproval
        votes = (CaseApproval.objects.filter(created_at__date__gte=since,
                                             **votes_scheme_filter)
                .select_related("case", "case__scheme", "user"))

        return render(request, "benevolent/overrides_exceptions.html", {
            "schemes": BenevolentScheme.objects.all(),
            "f_scheme": scheme_id or "",
            "since": since,
            "overridden_cases": cases.order_by("-approved_at"),
            "exemptions": exemptions.order_by("-approved_at"),
            "adjustments": adjustments.order_by("-approved_at"),
            "votes": votes.order_by("-created_at"),
        })
