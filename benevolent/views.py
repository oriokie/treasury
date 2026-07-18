"""Benevolent module views.

Deliberately thin. Every decision that touches money or policy goes through
benevolent.services, so accounting integrity and the audit trail live in one
place and cannot be bypassed by a second code path in a view.
"""
import datetime as dt
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from core.permissions import (BenevolentApproveMixin, BenevolentCaseMixin,
                              BenevolentFinanceMixin, BenevolentRegistrationMixin,
                              BenevolentSetupMixin, BenevolentViewMixin)

from .forms import (ApproveForm, AttachmentForm, BenefitRuleForm, CaseForm,
                    ContributionForm, DependantForm, EventTypeForm,
                    PayoutForm, PolicyForm, RejectForm, SchemeForm)
from .models import (BenevolentCase, BenevolentContribution, BenevolentEventType,
                     BenevolentScheme, SchemeBenefitRule, SchemeDependant,
                     SchemeMembership, SchemePolicy)
from .services import cases as case_svc
from .services import contributions as contrib_svc
from .services import reporting as report_svc
from .services import schemes as scheme_svc
from .services.eligibility import evaluate, evaluate_case


def _period(request):
    """The date window every screen shares. Defaults to the current year, which
    is the natural period for a welfare scheme's annual caps and dues.

    A `?year=YYYY` takes priority over explicit `start`/`end` when present —
    the friendly way most of this module's screens let someone pick a period,
    without needing to type two ISO dates by hand.
    """
    today = dt.date.today()
    year_raw = request.GET.get("year")
    if year_raw:
        try:
            y = int(year_raw)
            return dt.date(y, 1, 1), dt.date(y, 12, 31)
        except ValueError:
            pass
    try:
        start = dt.date.fromisoformat(request.GET.get("start") or "")
    except ValueError:
        start = dt.date(today.year, 1, 1)
    try:
        end = dt.date.fromisoformat(request.GET.get("end") or "")
    except ValueError:
        end = today
    return start, end


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

class BenevolentDashboardView(BenevolentViewMixin, View):
    def get(self, request):
        start, end = _period(request)
        rows = report_svc.scheme_summary(start, end)
        stats = report_svc.case_statistics(start, end)
        recent = (BenevolentCase.objects.select_related("scheme", "event_type",
                                                        "membership__member")
                  .prefetch_related("payouts__expense")[:10])
        action = (BenevolentCase.objects
                  .filter(status__in=[BenevolentCase.Status.SUBMITTED,
                                      BenevolentCase.Status.ASSESSED,
                                      BenevolentCase.Status.APPROVED,
                                      BenevolentCase.Status.PARTLY_PAID])
                  .select_related("scheme", "event_type", "membership__member")
                  .prefetch_related("payouts__expense")
                  .order_by("event_date")[:12])

        # Phase 9: "your queues" — contextual, role-aware counts. Each is only
        # ever COMPUTED (never just hidden) when the viewer actually holds the
        # matching right, so a Registration Officer's dashboard load never
        # pays for a committee-vote query it will not show, and each uses a
        # single grouped/filtered query rather than a per-case loop.
        from core import roles
        queues = {}
        if roles.can_manage_benevolent_finance(request.user):
            from benevolent.models import BenevolentNotification, ContributionIntake
            queues["intake"] = ContributionIntake.objects.filter(
                status__in=ContributionIntake.OPEN_STATUSES).count()
            queues["notifications_failed"] = BenevolentNotification.objects.filter(
                status=BenevolentNotification.Status.FAILED).count()
        if roles.can_manage_benevolent_cases(request.user):
            queues["assessment"] = BenevolentCase.objects.filter(
                status=BenevolentCase.Status.SUBMITTED).count()
        if roles.can_approve_benevolent(request.user):
            queues["approval"] = BenevolentCase.objects.filter(
                status=BenevolentCase.Status.ASSESSED).count()
        if roles.can_vote_benevolent(request.user):
            # ASSESSED cases this specific person has not yet voted on — one
            # query, not one per case.
            queues["my_votes"] = (
                BenevolentCase.objects.filter(status=BenevolentCase.Status.ASSESSED)
                .exclude(committee_approvals__user=request.user).count())
        if roles.can_register_benevolent_members(request.user):
            from benevolent.models import SchemeMembership
            queues["pending_admission"] = SchemeMembership.objects.filter(
                status=SchemeMembership.Status.PENDING).count()

        return render(request, "benevolent/dashboard.html", {
            "rows": rows, "totals": report_svc.totals(rows), "stats": stats,
            "recent": recent, "action": action, "start": start, "end": end,
            "schemes": BenevolentScheme.objects.all(),
            "arrears": report_svc.arrears_total(as_of=end),
            "queues": queues,
            "is_committee_chair": roles.is_benevolent_committee_chair(request.user),
        })


# ---------------------------------------------------------------------------
# Schemes & policies (setup)
# ---------------------------------------------------------------------------

class SchemeListView(BenevolentViewMixin, View):
    def get(self, request):
        start, end = _period(request)
        return render(request, "benevolent/scheme_list.html", {
            "rows": report_svc.scheme_summary(start, end),
            "drafts": BenevolentScheme.objects.filter(
                status=BenevolentScheme.Status.DRAFT).select_related("fund"),
            "start": start, "end": end})


class SchemeFormView(BenevolentSetupMixin, View):
    def _obj(self, pk):
        return get_object_or_404(BenevolentScheme, pk=pk) if pk else None

    def get(self, request, pk=None):
        obj = self._obj(pk)
        return render(request, "benevolent/scheme_form.html",
                      {"form": SchemeForm(instance=obj), "scheme": obj})

    def post(self, request, pk=None):
        obj = self._obj(pk)
        form = SchemeForm(request.POST, instance=obj)
        if form.is_valid():
            scheme = form.save(commit=False)
            if not scheme.pk:
                scheme.created_by = request.user
            try:
                scheme.full_clean(exclude=["slug"])
                scheme.save()
            except ValidationError as e:
                for msg in e.messages:
                    form.add_error(None, msg)
            else:
                messages.success(request, f"Scheme '{scheme.name}' saved.")
                return redirect("benevolent_scheme_detail", pk=scheme.pk)
        return render(request, "benevolent/scheme_form.html",
                      {"form": form, "scheme": obj})


class SchemeDetailView(BenevolentViewMixin, View):
    def get(self, request, pk):
        scheme = get_object_or_404(
            BenevolentScheme.objects.select_related("fund"), pk=pk)
        start, end = _period(request)
        policies = list(scheme.policies.prefetch_related("benefit_rules__event_type"))
        return render(request, "benevolent/scheme_detail.html", {
            "scheme": scheme,
            "policy": scheme.current_policy,
            "policies": policies,
            "event_types": scheme.event_types.all(),
            "balance": report_svc.scheme_balance(scheme),
            "contributions": report_svc.contributions_total(start, end, scheme),
            "payouts": report_svc.payouts_total(start, end, scheme),
            "committed": report_svc.approved_unpaid_total(scheme),
            "stats": report_svc.case_statistics(start, end, scheme),
            "members": scheme.memberships.filter(
                status=SchemeMembership.Status.ACTIVE).count(),
            "cases": (scheme.cases.select_related("event_type", "membership__member")
                      .prefetch_related("payouts__expense")[:10]),
            "start": start, "end": end,
        })


class SchemeActionView(BenevolentSetupMixin, View):
    """Open / suspend / close a scheme."""
    def post(self, request, pk, action):
        scheme = get_object_or_404(BenevolentScheme, pk=pk)
        fn = {"activate": scheme_svc.activate_scheme,
              "suspend": scheme_svc.suspend_scheme,
              "close": scheme_svc.close_scheme}.get(action)
        if fn is None:
            messages.error(request, "Unknown action.")
        else:
            try:
                fn(scheme, user=request.user)
                messages.success(request, f"{scheme.name} is now "
                                          f"{scheme.get_status_display().lower()}.")
            except ValidationError as e:
                messages.error(request, "; ".join(e.messages))
        return redirect("benevolent_scheme_detail", pk=pk)


class EventTypeView(BenevolentSetupMixin, View):
    """The scheme's vocabulary of qualifying events."""
    def get(self, request, pk):
        scheme = get_object_or_404(BenevolentScheme, pk=pk)
        edit = None
        if request.GET.get("edit"):
            edit = scheme.event_types.filter(pk=request.GET["edit"]).first()
        return render(request, "benevolent/event_types.html", {
            "scheme": scheme, "form": EventTypeForm(instance=edit), "editing": edit,
            "event_types": scheme.event_types.all()})

    def post(self, request, pk):
        scheme = get_object_or_404(BenevolentScheme, pk=pk)
        edit = scheme.event_types.filter(pk=request.POST.get("edit_id")).first() \
            if request.POST.get("edit_id") else None
        form = EventTypeForm(request.POST, instance=edit)
        if form.is_valid():
            et = form.save(commit=False)
            et.scheme = scheme
            et.save()
            messages.success(request, f"Event type '{et.name}' saved.")
            return redirect("benevolent_event_types", pk=pk)
        return render(request, "benevolent/event_types.html", {
            "scheme": scheme, "form": form, "editing": edit,
            "event_types": scheme.event_types.all()})


class PolicyFormView(BenevolentSetupMixin, View):
    """Draft a policy. Editing an ACTIVE-but-unused version is allowed; editing a
    version that has decided a case is impossible by design (the model refuses),
    so the only route to changing settled rules is a new version."""

    def _scheme(self, pk):
        return get_object_or_404(BenevolentScheme, pk=pk)

    def get(self, request, pk, policy_id=None):
        scheme = self._scheme(pk)
        policy = get_object_or_404(SchemePolicy, pk=policy_id, scheme=scheme) \
            if policy_id else None
        return render(request, "benevolent/policy_form.html", {
            "scheme": scheme, "policy": policy,
            "form": PolicyForm(instance=policy),
            "rule_form": BenefitRuleForm(scheme=scheme),
            "rules": (policy.benefit_rules.select_related("event_type") if policy else []),
        })

    def post(self, request, pk, policy_id=None):
        scheme = self._scheme(pk)
        policy = get_object_or_404(SchemePolicy, pk=policy_id, scheme=scheme) \
            if policy_id else None
        form = PolicyForm(request.POST, instance=policy)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.scheme = scheme
            if not obj.pk:
                obj.created_by = request.user
            try:
                obj.save()
            except ValidationError as e:
                for m in e.messages:
                    form.add_error(None, m)
            else:
                messages.success(request, f"Policy v{obj.version} saved as a draft.")
                return redirect("benevolent_policy_edit", pk=scheme.pk, policy_id=obj.pk)
        return render(request, "benevolent/policy_form.html", {
            "scheme": scheme, "policy": policy, "form": form,
            "rule_form": BenefitRuleForm(scheme=scheme),
            "rules": (policy.benefit_rules.select_related("event_type") if policy else []),
        })


class PolicyRuleView(BenevolentSetupMixin, View):
    """Add or remove a benefit-schedule line on a draft policy."""
    def post(self, request, pk, policy_id):
        policy = get_object_or_404(SchemePolicy, pk=policy_id, scheme_id=pk)
        back = redirect("benevolent_policy_edit", pk=pk, policy_id=policy_id)
        if request.POST.get("delete"):
            rule = policy.benefit_rules.filter(pk=request.POST["delete"]).first()
            if rule and not policy.is_locked:
                rule.delete()
                messages.success(request, "Benefit line removed.")
            else:
                messages.error(request, "That policy version is locked; its schedule is fixed.")
            return back
        form = BenefitRuleForm(request.POST, scheme=policy.scheme)
        if form.is_valid():
            rule = form.save(commit=False)
            rule.policy = policy
            try:
                rule.save()
                messages.success(request, f"Benefit for '{rule.event_type.name}' saved.")
            except ValidationError as e:
                messages.error(request, "; ".join(e.messages))
        else:
            messages.error(request, "Check the benefit line: " + form.errors.as_text())
        return back


class PolicyActionView(BenevolentSetupMixin, View):
    """Publish a draft, withdraw one, or start a new version from an existing one."""
    def post(self, request, pk, policy_id, action):
        policy = get_object_or_404(SchemePolicy, pk=policy_id, scheme_id=pk)
        try:
            if action == "publish":
                scheme_svc.publish_policy(policy, user=request.user)
                messages.success(
                    request, f"Policy v{policy.version} is in force from "
                             f"{policy.effective_from:%d %b %Y}. It is now permanent: any "
                             f"further change must be a new version.")
            elif action == "withdraw":
                scheme_svc.withdraw_policy(policy, user=request.user)
                messages.success(request, f"Policy v{policy.version} withdrawn.")
            elif action == "new-version":
                try:
                    eff = dt.date.fromisoformat(request.POST.get("effective_from") or "")
                except ValueError:
                    messages.error(request, "Give the date the new rules take effect.")
                    return redirect("benevolent_scheme_detail", pk=pk)
                draft = scheme_svc.new_version_from(policy, effective_from=eff,
                                                    user=request.user)
                messages.success(
                    request, f"Draft v{draft.version} created from v{policy.version}. "
                             f"Edit it and publish when ready — v{policy.version} and every "
                             f"case decided under it are untouched.")
                return redirect("benevolent_policy_edit", pk=pk, policy_id=draft.pk)
            else:
                messages.error(request, "Unknown action.")
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
        return redirect("benevolent_scheme_detail", pk=pk)


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------

class MembershipListView(BenevolentViewMixin, View):
    def get(self, request):
        q = (request.GET.get("q") or "").strip()
        f_scheme = request.GET.get("scheme") or ""
        f_status = request.GET.get("status") or ""
        qs = (SchemeMembership.objects.select_related("scheme", "member")
              .order_by("scheme__name", "member__name"))
        if q:
            qs = qs.filter(Q(member__name__icontains=q) | Q(number__icontains=q))
        if f_scheme:
            qs = qs.filter(scheme_id=f_scheme)
        if f_status:
            qs = qs.filter(status=f_status)
        export = request.GET.get("export")
        if export in ("xlsx", "csv"):
            from benevolent.exports import export_response, membership_rows
            from core.models import SiteConfig
            header, rows = membership_rows(qs, user=request.user)
            return export_response(
                export, filename="benevolent-members",
                title="Benevolent — memberships", header=header, rows=rows,
                church=SiteConfig.get().church_name)
        page = Paginator(qs, 50).get_page(request.GET.get("page"))
        return render(request, "benevolent/membership_list.html", {
            "page_obj": page, "memberships": page.object_list, "q": q,
            "f_scheme": f_scheme, "f_status": f_status,
            "schemes": BenevolentScheme.objects.exclude(
                status=BenevolentScheme.Status.DRAFT),
            "statuses": SchemeMembership.Status.choices})


class MembershipCreateView(BenevolentRegistrationMixin, View):
    """RETIRED — redirects to the full registration screen.

    This was Phase 1's enrolment form. Phase 3 built `RegisterView`
    (`benevolent_register`), which does everything this did and more:
    households, dependants, a spouse, date of birth, and (since the last
    round) registering someone who is not on the church roll at all. Nothing
    in the UI has linked here since; the URL survived only because it was
    still routed, which means a bookmark or an old link could still reach a
    strictly WORSE registration form than the one every other route leads to
    — a second, divergent code path for the same job, quietly waiting to be
    stumbled into.

    Kept as a redirect rather than deleted: an old bookmark should land on
    the right screen, not a 404. The duplicate FORM (`MembershipForm`) and
    its template are gone.
    """

    def get(self, request, pk):
        return redirect("benevolent_register", pk=pk)

    def post(self, request, pk):
        return redirect("benevolent_register", pk=pk)


class MembershipDetailView(BenevolentViewMixin, View):
    def get(self, request, pk):
        m = get_object_or_404(
            SchemeMembership.objects.select_related("scheme", "member"), pk=pk)
        from .forms import (AdjustmentForm, ExemptionForm, FeeForm,
                            HouseholdMemberForm, LifecycleForm, MembershipEditForm,
                            NomineeForm, RefundForm, TransferForm)
        from .services import registry as reg_svc
        from .services import standing as standing_svc
        policy = m.scheme.policy_on()
        ctx = report_svc.member_statement(m)
        # the standing is shown WITH its reasoning, live — the cached column is what
        # the register lists, but on the member's own page a treasurer should see the
        # working, not just the verdict
        result = standing_svc.assess(m)
        ctx.update({
            "dependants": m.dependants.filter(active=True),
            "dependant_form": DependantForm(),
            "policy": policy,
            "nominees": m.nominees.filter(active=True),
            "nominee_form": NomineeForm(),
            "fee_form": FeeForm(),
            "renewal_due": m.renewal_due_on(policy),
            "renewal_overdue": m.renewal_overdue(policy),
            "months_idle": m.months_since_contribution(),
            "cover_from": m.cover_from,
            # ---- Phase 3 ----
            "standing": result,
            "facts": result.facts,
            "household": reg_svc.household_members(m),
            "household_form": HouseholdMemberForm(),
            "relationship_choices": SchemeDependant.Relationship.choices,
            "membership_edit_form": MembershipEditForm(instance=m),
            "exemptions": m.exemptions.select_related("granted_by", "approved_by"),
            "exemption_form": ExemptionForm(),
            "transfer_form": TransferForm(),
            "lifecycle_form": LifecycleForm(),
            "events": m.events.select_related("actor")[:30],
            # ---- Phase 4: the member's account ----
            "adjustments": m.adjustments.select_related("raised_by", "approved_by"),
            "adjustment_form": AdjustmentForm(),
            "refund_form": RefundForm(),
            "refunds": m.refunds.select_related("expense"),
        })
        if m.status == SchemeMembership.Status.DECEASED:
            # If a case for this member's own death already exists (auto-opened,
            # or raised previously), the DECEASED panel should link straight to
            # it — offering "Raise a case" again would invite a duplicate. Only
            # when none exists does the panel offer the (pre-filled) new-case
            # link, mirroring open_case_for_death()'s own idempotency query.
            ctx["own_death_case"] = (
                BenevolentCase.objects.filter(
                    scheme=m.scheme, membership=m, dependant__isnull=True,
                    status__in=BenevolentCase.OPEN_STATUSES)
                .order_by("-event_date").first())
        return render(request, "benevolent/membership_detail.html", ctx)

    def post(self, request, pk):
        """Add a dependant, or withdraw the membership."""
        from core.roles import can_manage_benevolent
        m = get_object_or_404(SchemeMembership, pk=pk)
        if not can_manage_benevolent(request.user):
            messages.error(request, "You don't have the benevolent-administration right.")
            return redirect("benevolent_membership_detail", pk=pk)
        if request.POST.get("withdraw"):
            try:
                scheme_svc.withdraw_membership(m, user=request.user)
                messages.success(request, f"{m.member.name} withdrawn from {m.scheme.name}.")
            except ValidationError as e:
                messages.error(request, "; ".join(e.messages))
            return redirect("benevolent_membership_detail", pk=pk)
        form = DependantForm(request.POST)
        if form.is_valid():
            d = form.save(commit=False)
            d.membership = m
            d.save()
            messages.success(request, f"{d.name} registered as a dependant.")
        else:
            messages.error(request, "Check the dependant details.")
        return redirect("benevolent_membership_detail", pk=pk)


# ---------------------------------------------------------------------------
# Contributions
# ---------------------------------------------------------------------------

class ContributionListView(BenevolentViewMixin, View):
    def get(self, request):
        start, end = _period(request)
        f_scheme = request.GET.get("scheme") or ""
        scheme = BenevolentScheme.objects.filter(pk=f_scheme).first() if f_scheme else None
        qs = contrib_svc.contributions_qs(scheme=scheme, start=start, end=end) \
            .select_related("scheme", "membership__member", "transaction")
        export = request.GET.get("export")
        if export in ("xlsx", "csv"):
            from benevolent.exports import contribution_rows, export_response
            from core.models import SiteConfig
            header, rows = contribution_rows(qs, user=request.user)
            return export_response(
                export, filename="benevolent-contributions",
                title="Benevolent — contributions", header=header, rows=rows,
                church=SiteConfig.get().church_name)
        page = Paginator(qs, 50).get_page(request.GET.get("page"))
        this_year = dt.date.today().year
        earliest = (contrib_svc.contributions_qs()
                   .order_by("transaction__date")
                   .values_list("transaction__date", flat=True).first())
        first_year = earliest.year if earliest else this_year
        years = list(range(this_year, first_year - 1, -1))
        return render(request, "benevolent/contribution_list.html", {
            "page_obj": page, "contributions": page.object_list,
            "total": contrib_svc.contributions_total(scheme=scheme, start=start, end=end),
            "schemes": BenevolentScheme.objects.exclude(
                status=BenevolentScheme.Status.DRAFT),
            "f_scheme": f_scheme, "start": start, "end": end,
            "years": years,
            "selected_year": (start.year if start.year == end.year
                              and start == dt.date(start.year, 1, 1)
                              and end == dt.date(end.year, 12, 31) else None)})


class ContributionCreateView(BenevolentFinanceMixin, View):
    def get(self, request, pk):
        scheme = get_object_or_404(BenevolentScheme, pk=pk)
        return render(request, "benevolent/contribution_form.html", {
            "scheme": scheme, "form": ContributionForm(scheme=scheme),
            "policy": scheme.policy_on()})

    def post(self, request, pk):
        scheme = get_object_or_404(BenevolentScheme, pk=pk)
        form = ContributionForm(request.POST, scheme=scheme)
        if form.is_valid():
            d = form.cleaned_data
            # Screen for exceptions before recording. Blocking ones (a closed
            # period, a non-positive amount) stop the save; advisory ones (a
            # future date, a possible duplicate, an out-of-cover date) are shown
            # as warnings but do not prevent a treasurer who means it.
            from benevolent.services import exceptions as exc_svc
            problems = exc_svc.screen_contribution(
                scheme, date=d["date"], amount=d["amount"],
                membership=d.get("membership"),
                payer_type=d.get("payer_type") or None)
            blocking = [p for p in problems if p.blocking]
            if blocking and not request.POST.get("confirm_override"):
                for p in blocking:
                    form.add_error(None, f"{p.label}: {p.detail}")
                return render(request, "benevolent/contribution_form.html", {
                    "scheme": scheme, "form": form, "policy": scheme.policy_on(),
                    "warnings": [p for p in problems if not p.blocking]})
            try:
                c = contrib_svc.record_contribution(
                    scheme, date=d["date"], amount=d["amount"], user=request.user,
                    membership=d.get("membership"), member=d.get("member"),
                    case=d.get("case"),
                    channel=d.get("channel"), period_label=d.get("period_label") or None,
                    note=d.get("note") or "",
                    payer_type=d.get("payer_type") or None,
                    payer_name=d.get("payer_name") or "")
            except ValidationError as e:
                for msg in e.messages:
                    form.add_error(None, msg)
            else:
                warn = [p for p in problems if not p.blocking]
                msg = f"Contribution of {c.amount} receipted into {scheme.fund.name}."
                if warn:
                    msg += " Noted: " + "; ".join(p.label.lower() for p in warn) + "."
                messages.success(request, msg)
                return redirect("benevolent_scheme_detail", pk=scheme.pk)
        return render(request, "benevolent/contribution_form.html", {
            "scheme": scheme, "form": form, "policy": scheme.policy_on()})


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

class CaseListView(BenevolentViewMixin, View):
    def get(self, request):
        q = (request.GET.get("q") or "").strip()
        f_scheme = request.GET.get("scheme") or ""
        f_status = request.GET.get("status") or ""
        qs = (BenevolentCase.objects
              .select_related("scheme", "event_type", "membership__member")
              .prefetch_related("payouts__expense"))
        if q:
            qs = qs.filter(Q(number__icontains=q)
                           | Q(external_reference__icontains=q)
                           | Q(membership__member__name__icontains=q)
                           | Q(beneficiary_name__icontains=q))
        if f_scheme:
            qs = qs.filter(scheme_id=f_scheme)
        if f_status:
            qs = qs.filter(status=f_status)
        export = request.GET.get("export")
        if export in ("xlsx", "csv"):
            from benevolent.exports import case_rows, export_response
            from core.models import SiteConfig
            header, rows = case_rows(qs, user=request.user)
            return export_response(
                export, filename="benevolent-cases",
                title="Benevolent — cases", header=header, rows=rows,
                church=SiteConfig.get().church_name)
        page = Paginator(qs, 50).get_page(request.GET.get("page"))
        return render(request, "benevolent/case_list.html", {
            "page_obj": page, "cases": page.object_list, "q": q,
            "f_scheme": f_scheme, "f_status": f_status,
            "schemes": BenevolentScheme.objects.exclude(
                status=BenevolentScheme.Status.DRAFT),
            "statuses": BenevolentCase.Status.choices})


class CaseCreateView(BenevolentCaseMixin, View):
    def get(self, request, pk):
        scheme = get_object_or_404(BenevolentScheme, pk=pk)
        # A "raise case" link from a death record passes ?dependant= or
        # ?membership=; either way the form opens pre-filled with everything the
        # scheme already knows, so nothing correct has to be retyped.
        membership = dependant = None
        dep_id = request.GET.get("dependant")
        mem_id = request.GET.get("membership")
        if dep_id:
            dependant = SchemeDependant.objects.filter(
                pk=dep_id, membership__scheme=scheme).select_related(
                "membership__member").first()
        if mem_id and not dependant:
            membership = SchemeMembership.objects.filter(
                pk=mem_id, scheme=scheme).select_related("member").first()
        initial = case_svc.derive_case_defaults(
            scheme, membership=membership, dependant=dependant)
        # The death date is already on file the moment it was recorded — a
        # dependant's own died_on, or the member's own died_on for their own
        # death — so it is pre-filled here too, the same "never retype what
        # the scheme already knows" principle derive_case_defaults follows for
        # the beneficiary and the fixed benefit amount.
        died_on = None
        if dependant is not None:
            died_on = dependant.died_on
        elif membership is not None:
            died_on = membership.died_on
        if died_on:
            initial["event_date"] = died_on
            initial["reported_date"] = died_on
        return render(request, "benevolent/case_form.html", {
            "scheme": scheme,
            "form": CaseForm(scheme=scheme, initial=initial),
            "policy": scheme.current_policy})

    def post(self, request, pk):
        scheme = get_object_or_404(BenevolentScheme, pk=pk)
        form = CaseForm(request.POST, scheme=scheme)
        if form.is_valid():
            d = form.cleaned_data
            try:
                case = case_svc.create_case(
                    scheme, membership=d.get("membership"), event_type=d["event_type"],
                    dependant=d.get("dependant"),
                    beneficiary_name=d.get("beneficiary_name") or "",
                    event_date=d["event_date"], reported_date=d.get("reported_date"),
                    description=d.get("description") or "",
                    claimed_amount=d.get("claimed_amount"),
                    funding_target=d.get("funding_target"), user=request.user)
            except ValidationError as e:
                for msg in e.messages:
                    form.add_error(None, msg)
            else:
                # persist the derived relationship (create_case doesn't take it)
                rel = d.get("beneficiary_relationship")
                if rel and not case.beneficiary_relationship:
                    case.beneficiary_relationship = rel
                    case.save(update_fields=["beneficiary_relationship"])

                # The fast path (item 5): raise, submit, assess and approve in
                # one step. The FORM already refuses to offer this where the
                # policy requires a different approver, and it is gated here a
                # second time on the Approve right itself — raising a case only
                # needs BenevolentCaseMixin, but approving one is a money
                # decision and needs BenevolentApproveMixin regardless of what
                # the policy permits between raiser and approver.
                if d.get("create_and_approve"):
                    from core.roles import can_approve_benevolent
                    if not can_approve_benevolent(request.user):
                        messages.warning(
                            request, f"Case {case.number} was drafted, but you do not "
                                    f"have the right to approve a case, so it was left "
                                    f"as a draft for someone who does.")
                        return redirect("benevolent_case_detail", pk=case.pk)
                    try:
                        case_svc.submit_case(case, user=request.user)
                        case_svc.assess_case(case, user=request.user)
                        if case.status == BenevolentCase.Status.ASSESSED:
                            case = case_svc.approve_case(
                                case, amount=case.assessed_amount, user=request.user,
                                allow_self_approval=True)
                            messages.success(
                                request, f"Case {case.number} raised and approved for "
                                        f"{case.approved_amount}. Raise a payment voucher "
                                        f"to pay it.")
                        else:
                            messages.warning(
                                request, f"Case {case.number} was submitted and assessed, "
                                        f"but stopped there — see the case for why it "
                                        f"could not be approved automatically.")
                    except ValidationError as e:
                        messages.warning(
                            request, f"Case {case.number} was drafted, but the "
                                    f"approve-immediately step stopped: {'; '.join(e.messages)} "
                                    f"The case is saved as far as it got — continue it "
                                    f"from the case page.")
                    return redirect("benevolent_case_detail", pk=case.pk)

                messages.success(request, f"Case {case.number} drafted.")
                return redirect("benevolent_case_detail", pk=case.pk)
        return render(request, "benevolent/case_form.html", {
            "scheme": scheme, "form": form, "policy": scheme.current_policy})


class CaseUpdateView(BenevolentCaseMixin, View):
    """Correct a case's own details — DRAFT only; see update_case's own
    docstring for why. A real CRUD gap: create and cancel both existed, but
    nothing let a Case Officer fix a typo before submitting."""

    def get(self, request, pk):
        case = get_object_or_404(BenevolentCase.objects.select_related("scheme"), pk=pk)
        if case.status != BenevolentCase.Status.DRAFT:
            messages.error(request, f"{case.number} is "
                                    f"{case.get_status_display().lower()} — only a draft "
                                    f"case can be edited. Cancel and re-raise it instead.")
            return redirect("benevolent_case_detail", pk=pk)
        return render(request, "benevolent/case_form.html", {
            "scheme": case.scheme, "case": case,
            "form": CaseForm(scheme=case.scheme, instance=case),
            "policy": case.scheme.current_policy})

    def post(self, request, pk):
        case = get_object_or_404(BenevolentCase.objects.select_related("scheme"), pk=pk)
        if case.status != BenevolentCase.Status.DRAFT:
            messages.error(request, f"{case.number} is no longer a draft.")
            return redirect("benevolent_case_detail", pk=pk)
        form = CaseForm(request.POST, scheme=case.scheme, instance=case)
        if form.is_valid():
            d = form.cleaned_data
            try:
                case_svc.update_case(
                    case, event_type=d["event_type"], event_date=d["event_date"],
                    membership=d.get("membership"), dependant=d.get("dependant"),
                    beneficiary_name=d.get("beneficiary_name") or "",
                    reported_date=d.get("reported_date"),
                    description=d.get("description") or "",
                    claimed_amount=d.get("claimed_amount"), user=request.user)
            except ValidationError as e:
                for msg in e.messages:
                    form.add_error(None, msg)
            else:
                messages.success(request, f"{case.number} updated.")
                return redirect("benevolent_case_detail", pk=case.pk)
        return render(request, "benevolent/case_form.html", {
            "scheme": case.scheme, "case": case, "form": form,
            "policy": case.scheme.current_policy})


class CaseDetailView(BenevolentViewMixin, View):
    def get(self, request, pk):
        case = get_object_or_404(
            BenevolentCase.objects.select_related(
                "scheme", "scheme__fund", "event_type", "membership__member", "policy")
            .prefetch_related("payouts__expense", "attachments"), pk=pk)
        # a live preview for a case not yet assessed; for an assessed case the
        # FROZEN snapshot is what's shown — never a re-run, which could silently
        # differ from what was actually decided
        if case.eligibility_snapshot:
            preview = None
        else:
            preview = evaluate_case(case)
        from .forms import BereavedDecisionForm, FundingTargetForm, VoteForm
        from .services.eligibility import missing_required_documents
        committee = case_svc.committee_state(case)
        levy = contrib_svc.levy_summary(case)
        return render(request, "benevolent/case_detail.html", {
            "case": case, "preview": preview,
            "snapshot": case.eligibility_snapshot or {},
            "payouts": case.payouts.select_related("expense").all(),
            "approve_form": ApproveForm(initial={"amount": case.assessed_amount}),
            "reject_form": RejectForm(),
            "payout_form": PayoutForm(initial={"amount": case.available_to_voucher}),
            "attach_form": AttachmentForm(case=case),
            "committee": committee,
            "vote_form": VoteForm(initial={"amount": case.assessed_amount}),
            "my_vote": next((v for v in committee["votes"]
                             if v.user_id == request.user.pk), None),
            "levy": levy,
            "fund_balance": case.scheme.balance,
            # a scheme funded by ongoing dues has no per-case levy roster to
            # show "who has and hasn't paid" — this is the equivalent view
            # for that case, computed only when there's no levy roster
            # already answering the same question more sharply
            "standing_snapshot": (report_svc.scheme_standing_snapshot(case.scheme)
                                  if levy is None else None),
            # ---- Phase 5 ----
            "funding_target_form": FundingTargetForm(
                initial={"amount": case.funding_target or case.assessed_amount}),
            "bereaved_decision_form": BereavedDecisionForm(),
            "missing_documents": missing_required_documents(case.event_type, case),
            "events": case.events.select_related("actor")[:50],
        })


class CaseActionView(BenevolentCaseMixin, View):
    """Submit / assess / attach / cancel. Approval and rejection have their own
    view with a stricter permission — a money decision is not administration."""

    def post(self, request, pk, action):
        case = get_object_or_404(BenevolentCase, pk=pk)
        try:
            if action == "submit":
                case_svc.submit_case(case, user=request.user)
                messages.success(request, f"{case.number} submitted for assessment.")
            elif action == "assess":
                result = case_svc.assess_case(case, user=request.user)
                if result.eligible:
                    messages.success(
                        request, f"{case.number} meets policy v{result.policy.version}. "
                                 f"Entitlement: {result.entitlement.amount}.")
                else:
                    failed = ", ".join(c.label for c in result.blocking_failures)
                    messages.warning(
                        request, f"{case.number} does NOT meet the policy ({failed}). It can "
                                 f"still be approved with a recorded reason, if the policy "
                                 f"allows an override.")
            elif action == "attach":
                form = AttachmentForm(request.POST, request.FILES, case=case)
                if form.is_valid():
                    a = form.save(commit=False)
                    a.case = case
                    a.uploaded_by = request.user
                    a.save()
                    case_svc.log_document_added(
                        case, a.document_type or a.label, user=request.user)
                    messages.success(request, "Document attached.")
                else:
                    messages.error(request, "Choose a file to attach.")
            elif action == "cancel":
                case_svc.cancel_case(case, user=request.user,
                                     reason=request.POST.get("reason", ""))
                messages.success(request, f"{case.number} cancelled.")
            elif action == "close":
                case_svc.close_case(case, user=request.user)
                messages.success(request, f"{case.number} closed.")
            else:
                messages.error(request, "Unknown action.")
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
        return redirect("benevolent_case_detail", pk=pk)


class CaseDecisionView(BenevolentApproveMixin, View):
    """Approve or reject — the money decision."""

    def post(self, request, pk, action):
        case = get_object_or_404(BenevolentCase, pk=pk)
        try:
            if action == "approve":
                form = ApproveForm(request.POST)
                if not form.is_valid():
                    messages.error(request, "Enter the amount to approve.")
                    return redirect("benevolent_case_detail", pk=pk)
                case_svc.approve_case(
                    case, amount=form.cleaned_data["amount"], user=request.user,
                    override_reason=form.cleaned_data.get("override_reason") or "",
                    allow_self_approval=not case.policy.require_different_approver)
                messages.success(
                    request, f"{case.number} approved for {case.approved_amount}. Raise a "
                             f"payment voucher to pay it — the voucher still needs the "
                             f"usual expense approval.")
            elif action == "reject":
                form = RejectForm(request.POST)
                if not form.is_valid():
                    messages.error(request, "A rejection must record a reason.")
                    return redirect("benevolent_case_detail", pk=pk)
                case_svc.reject_case(case, reason=form.cleaned_data["reason"],
                                     user=request.user)
                messages.success(request, f"{case.number} rejected.")
            else:
                messages.error(request, "Unknown action.")
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
        return redirect("benevolent_case_detail", pk=pk)


class CasePayoutView(BenevolentCaseMixin, View):
    """Raise the payment voucher. It enters the ordinary expense queue in
    PENDING — this module never approves its own payments."""

    def post(self, request, pk):
        case = get_object_or_404(BenevolentCase, pk=pk)
        form = PayoutForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Check the payout details.")
            return redirect("benevolent_case_detail", pk=pk)
        d = form.cleaned_data
        # Fund solvency (item 8): can the fund afford this payout right now? This
        # warns when the payout would exceed the cash available after existing
        # approvals, and blocks only where the scheme's settings say a fund must
        # never overdraw. A church may legitimately approve against a levy still
        # being collected, so the default is to warn, not refuse.
        from benevolent.services import solvency as sol_svc
        afford = sol_svc.can_fund_payout(case.scheme, d["amount"])
        if afford.level == "block":
            messages.error(request, afford.detail)
            return redirect("benevolent_case_detail", pk=pk)
        try:
            payout = case_svc.record_payout(
                case, amount=d["amount"], date=d["date"], user=request.user,
                payee_name=d.get("payee_name") or "", method=d.get("method"),
                voucher_no=d.get("voucher_no") or "",
                paid_from_petty_cash=d.get("paid_from_petty_cash") or False,
                note=d.get("note") or "")
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
        else:
            base = (f"Payment voucher for {payout.amount} raised on {case.number}. "
                    f"It is pending approval in the expenses queue like any other claim.")
            if afford.level == "warn":
                messages.warning(request, afford.detail)
            messages.success(request, base)
        return redirect("benevolent_case_detail", pk=pk)


class FundFromBalanceView(BenevolentCaseMixin, View):
    """Phase 11: the explicit, logged choice to pay a case from the fund's
    existing balance rather than raising a per-case levy. Does not itself
    move any money or change the case's status — record_payout() has never
    required a levy — it only puts a stated, dated decision on the case's
    own history, made with the balance actually shown at the moment of
    deciding."""

    def post(self, request, pk):
        case = get_object_or_404(BenevolentCase, pk=pk)
        reason = (request.POST.get("reason") or "").strip()
        try:
            case_svc.fund_from_balance(case, user=request.user, reason=reason)
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
        else:
            messages.success(
                request, f"Recorded: {case.number} will be funded from "
                         f"{case.scheme.fund.name}'s balance, not a levy.")
        return redirect("benevolent_case_detail", pk=pk)


class CaseFundingTargetView(BenevolentCaseMixin, View):
    """Set or change what a case is aiming to raise. A fundraising goal, not a
    policy decision — anyone who can administer the scheme can set one."""

    def post(self, request, pk):
        case = get_object_or_404(BenevolentCase, pk=pk)
        from .forms import FundingTargetForm
        form = FundingTargetForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Enter a positive funding target.")
            return redirect("benevolent_case_detail", pk=pk)
        try:
            case_svc.set_funding_target(
                case, amount=form.cleaned_data["amount"], user=request.user)
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
        else:
            messages.success(request, f"Funding target set at {form.cleaned_data['amount']}.")
        return redirect("benevolent_case_detail", pk=pk)


class CaseBereavedDecisionView(BenevolentApproveMixin, View):
    """The committee's ruling on the bereaved member's own contribution, under
    a COMMITTEE_DECIDES policy. Gated at the same permission as approving a
    benefit — it is a money decision about the same case."""

    def post(self, request, pk):
        case = get_object_or_404(BenevolentCase, pk=pk)
        from .forms import BereavedDecisionForm
        form = BereavedDecisionForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Record the committee's decision and why.")
            return redirect("benevolent_case_detail", pk=pk)
        try:
            case_svc.decide_bereaved_contribution(
                case, waived=(form.cleaned_data["waived"] == "1"),
                reason=form.cleaned_data["reason"], user=request.user)
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
        else:
            messages.success(request, "The committee's decision is recorded.")
        return redirect("benevolent_case_detail", pk=pk)


class CaseStatementView(BenevolentViewMixin, View):
    """The WhatsApp update — who contributed to this case, and who did not.

    A benevolent scheme runs on the plain fact that everybody can see who stood
    with the bereaved family. The treasurer was assembling this by hand, from a
    spreadsheet, after every single case. The system holds every fact in it.
    """
    template_name = "benevolent/case_statement.html"

    def get(self, request, pk):
        from benevolent.services import statement as stmt_svc
        from core.models import SiteConfig
        case = get_object_or_404(
            BenevolentCase.objects.select_related("scheme", "membership__member"), pk=pk)
        data = stmt_svc.case_statement(case)
        text = stmt_svc.as_text(data, currency=SiteConfig.get().currency_symbol)

        if request.GET.get("format") == "txt":
            from django.http import HttpResponse
            resp = HttpResponse(text, content_type="text/plain; charset=utf-8")
            resp["Content-Disposition"] = (
                f'attachment; filename="{case.number}_statement.txt"')
            return resp

        return render(request, self.template_name,
                      {"case": case, "d": data, "text": text})
