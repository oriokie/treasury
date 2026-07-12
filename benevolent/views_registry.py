"""Phase 3 views — the Benevolent Member Registry.

Thin, as everything in this module is: the registry service owns every write, so
that no view can move a membership through its lifecycle without a
`MembershipEvent` being written and standing being recomputed.
"""
import datetime as dt

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from core.permissions import (BenevolentApproveMixin, BenevolentRegistrationMixin,
                              BenevolentViewMixin)

from .forms import (ExemptionForm, HouseholdMemberForm, LifecycleForm,
                    RegistrationForm, TransferForm)
from .models import (BenevolentScheme, MembershipEvent, MembershipExemption,
                     RegistrationType, SchemeDependant, SchemeMembership, Standing)
from .services import registry as reg_svc
from .services import standing as standing_svc


class RegistryView(BenevolentViewMixin, View):
    """The register — every enrolment, with where each member stands."""

    def get(self, request):
        f_scheme = request.GET.get("scheme") or ""
        scheme = BenevolentScheme.objects.filter(pk=f_scheme).first() if f_scheme else None
        qs = reg_svc.registry(
            scheme=scheme,
            standing=request.GET.get("standing") or None,
            status=request.GET.get("status") or None,
            q=(request.GET.get("q") or "").strip())
        page = Paginator(qs, 50).get_page(request.GET.get("page"))
        return render(request, "benevolent/registry.html", {
            "page_obj": page, "memberships": page.object_list,
            "counts": reg_svc.standing_counts(scheme),
            "schemes": BenevolentScheme.objects.exclude(
                status=BenevolentScheme.Status.DRAFT),
            "standings": Standing.choices,
            "statuses": SchemeMembership.Status.choices,
            "f_scheme": f_scheme,
            "f_standing": request.GET.get("standing") or "",
            "f_status": request.GET.get("status") or "",
            "q": (request.GET.get("q") or "").strip(),
            "total": qs.count(),
        })


class RegisterView(BenevolentRegistrationMixin, View):
    """Register a member — individually or as a household."""

    def get(self, request, pk):
        scheme = get_object_or_404(BenevolentScheme, pk=pk)
        return render(request, "benevolent/register.html", {
            "scheme": scheme, "form": RegistrationForm(scheme=scheme),
            "policy": scheme.policy_on()})

    def post(self, request, pk):
        scheme = get_object_or_404(BenevolentScheme, pk=pk)
        form = RegistrationForm(request.POST, scheme=scheme)
        if form.is_valid():
            d = form.cleaned_data
            try:
                m = reg_svc.register(
                    scheme, d["member"], joined_on=d["joined_on"], user=request.user,
                    registration_type=d["registration_type"],
                    household_name=d.get("household_name") or "",
                    date_of_birth=d.get("date_of_birth"),
                    notes=d.get("notes") or "",
                    spouse=(d.get("spouse") or (d.get("spouse_name") or None)))
            except ValidationError as e:
                for msg in e.messages:
                    form.add_error(None, msg)
            else:
                messages.success(
                    request,
                    f"{m.member.name} registered as {m.number} — "
                    f"{m.get_status_display().lower()}, "
                    f"{m.get_standing_display().lower()}.")
                return redirect("benevolent_membership_detail", pk=m.pk)
        return render(request, "benevolent/register.html", {
            "scheme": scheme, "form": form, "policy": scheme.policy_on()})


class MembershipLifecycleView(BenevolentRegistrationMixin, View):
    """Suspend, reinstate, withdraw, record a death, close, or transfer.

    Every one of these is a decision a person makes and is answerable for, so every
    one requires a reason and every one is logged. Nothing here touches `standing`:
    that is recomputed afterwards, and is free to disagree.
    """

    def post(self, request, pk, action):
        m = get_object_or_404(
            SchemeMembership.objects.select_related("scheme", "member"), pk=pk)
        form = LifecycleForm(request.POST)
        reason = (request.POST.get("reason") or "").strip()
        on = None
        if form.is_valid():
            on = form.cleaned_data["on"]
            reason = form.cleaned_data["reason"]

        try:
            if action == "suspend":
                reg_svc.suspend(m, user=request.user, reason=reason, on=on)
                messages.success(request, f"{m.member.name} suspended.")
            elif action == "reinstate":
                reg_svc.reinstate(m, user=request.user, reason=reason, on=on)
                messages.success(
                    request, f"{m.member.name} reinstated. Note that any waiting period "
                             f"runs again from today.")
            elif action == "withdraw":
                reg_svc.withdraw(m, user=request.user, reason=reason, on=on)
                messages.success(request, f"{m.member.name} withdrawn from the scheme.")
            elif action == "deceased":
                reg_svc.record_death(m, died_on=on or dt.date.today(),
                                     user=request.user, reason=reason)
                messages.success(
                    request,
                    f"{m.member.name} recorded as deceased. The membership is NOT closed — "
                    f"a claim on their own death is what they paid in for, and it can "
                    f"still be raised, assessed and paid.")
            elif action == "close":
                reg_svc.close(m, user=request.user, reason=reason, on=on)
                messages.success(request, f"{m.number} closed.")
            elif action == "refuse":
                reg_svc.refuse(m, user=request.user, reason=reason)
                messages.success(request, "Registration refused.")
            elif action == "transfer":
                tform = TransferForm(request.POST)
                if not tform.is_valid():
                    messages.error(request, "Choose who the membership passes to, and why.")
                    return redirect("benevolent_membership_detail", pk=pk)
                new = reg_svc.transfer(
                    m, tform.cleaned_data["to_member"], on=tform.cleaned_data["on"],
                    user=request.user, reason=tform.cleaned_data["reason"])
                messages.success(
                    request,
                    f"Membership transferred to {new.member.name} ({new.number}), keeping "
                    f"the joining date of {new.joined_on:%d %b %Y} — the years already "
                    f"paid in stay with the household.")
                return redirect("benevolent_membership_detail", pk=new.pk)
            else:
                messages.error(request, "Unknown action.")
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
        return redirect("benevolent_membership_detail", pk=pk)


class HouseholdView(BenevolentRegistrationMixin, View):
    """Add or remove a person from a household registration."""

    def post(self, request, pk):
        m = get_object_or_404(SchemeMembership, pk=pk)
        if request.POST.get("remove"):
            dep = m.dependants.filter(pk=request.POST["remove"]).first()
            if dep:
                reg_svc.remove_dependant(
                    dep, user=request.user,
                    reason=(request.POST.get("reason") or ""))
                messages.success(
                    request, f"{dep.display_name} removed from cover. They remain covered "
                             f"for any event that happened before today — a claim already "
                             f"earned is not taken away.")
            return redirect("benevolent_membership_detail", pk=pk)

        form = HouseholdMemberForm(request.POST)
        if not form.is_valid():
            messages.error(request, "; ".join(
                e for errs in form.errors.values() for e in errs))
            return redirect("benevolent_membership_detail", pk=pk)
        d = form.cleaned_data
        try:
            dep = reg_svc.add_dependant(
                m, relationship=d["relationship"], member=d.get("member"),
                name=d.get("name") or "", date_of_birth=d.get("date_of_birth"),
                registered_on=d["registered_on"], user=request.user)
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
        else:
            messages.success(
                request, f"{dep.display_name} added to the household as "
                         f"{dep.get_relationship_display().lower()}.")
        return redirect("benevolent_membership_detail", pk=pk)


class ExemptionView(BenevolentRegistrationMixin, View):
    """Propose an exemption. Approving one is a separate, higher right — it
    relieves a member of an obligation everyone else is carrying."""

    def post(self, request, pk):
        m = get_object_or_404(SchemeMembership, pk=pk)
        form = ExemptionForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Check the exemption — a reason is required.")
            return redirect("benevolent_membership_detail", pk=pk)
        d = form.cleaned_data
        try:
            reg_svc.grant_exemption(
                m, kind=d["kind"], reason=d["reason"], from_date=d["from_date"],
                to_date=d.get("to_date"), exempt_dues=d["exempt_dues"],
                exempt_levies=d["exempt_levies"], comments=d.get("comments") or "",
                user=request.user)
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
        else:
            messages.success(
                request, "Exemption proposed. It does NOT take effect until someone else "
                         "approves it — an exemption relieves a member of an obligation "
                         "everyone else is carrying.")
        return redirect("benevolent_membership_detail", pk=pk)


class ExemptionDecisionView(BenevolentApproveMixin, View):
    """Approve or revoke an exemption. A money decision."""

    def post(self, request, pk, action):
        ex = get_object_or_404(
            MembershipExemption.objects.select_related("membership"), pk=pk)
        try:
            if action == "approve":
                reg_svc.approve_exemption(ex, user=request.user)
                messages.success(
                    request, f"Exemption approved. {ex.membership.member.name} is now "
                             f"{ex.membership.get_standing_display().lower()}.")
            elif action == "revoke":
                reg_svc.revoke_exemption(
                    ex, user=request.user,
                    reason=(request.POST.get("reason") or "").strip())
                messages.success(request, "Exemption ended.")
            else:
                messages.error(request, "Unknown action.")
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
        return redirect("benevolent_membership_detail", pk=ex.membership_id)


class StandingRefreshView(BenevolentRegistrationMixin, View):
    """Recompute a member's standing, on demand, and show the working."""

    def post(self, request, pk):
        m = get_object_or_404(SchemeMembership, pk=pk)
        result = standing_svc.refresh(m, user=request.user)
        messages.info(
            request,
            f"{m.member.name} is {m.get_standing_display().lower()} — {result.reason}")
        return redirect("benevolent_membership_detail", pk=pk)
