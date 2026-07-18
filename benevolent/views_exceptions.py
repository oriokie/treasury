"""Views for the contribution-exceptions workflow (item 3): reverse a
contribution, correct its attribution, and reconcile a scheme against the bank.
"""
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from core.permissions import (BenevolentFinanceMixin, BenevolentManageMixin,
                              BenevolentViewMixin)

from .models import (BenevolentCase, BenevolentContribution, BenevolentScheme,
                     SchemeMembership)
from .services import exceptions as exc_svc


class ContributionReverseView(BenevolentFinanceMixin, View):
    """Reverse a contribution — a bounced payment, a mistake, or a confirmed
    duplicate. Never deletes; leaves the original and its contra on the record."""

    def post(self, request, pk):
        c = get_object_or_404(
            BenevolentContribution.objects.select_related("transaction", "scheme"), pk=pk)
        reason = (request.POST.get("reason") or "").strip()
        if not reason:
            messages.error(request, "Give a reason for the reversal — it stays on the "
                                    "member's statement so the gap is explained.")
            return redirect("benevolent_contribution_list")
        try:
            exc_svc.reverse_contribution(c, user=request.user, reason=reason)
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
        else:
            messages.success(
                request, f"Contribution of {c.amount} reversed. Both the original and the "
                         f"reversal remain on the record.")
        return redirect("benevolent_contribution_list")


class ContributionCorrectView(BenevolentFinanceMixin, View):
    """Re-attribute a contribution recorded against the wrong member, wrong
    scheme, or as the wrong kind. Reverses the wrong entry and books a correct
    one carrying the same money."""

    def get(self, request, pk):
        c = get_object_or_404(
            BenevolentContribution.objects.select_related(
                "transaction", "scheme", "membership__member"), pk=pk)
        return render(request, "benevolent/contribution_correct.html", {
            "c": c,
            "schemes": BenevolentScheme.objects.exclude(
                status=BenevolentScheme.Status.DRAFT).order_by("name"),
            "kinds": BenevolentContribution.Kind.choices,
        })

    def post(self, request, pk):
        c = get_object_or_404(
            BenevolentContribution.objects.select_related("transaction", "scheme"), pk=pk)
        reason = (request.POST.get("reason") or "").strip()
        if not reason:
            messages.error(request, "Give a reason for the correction.")
            return redirect("benevolent_contribution_correct", pk=pk)

        new_scheme = None
        sid = request.POST.get("new_scheme")
        if sid and str(sid) != str(c.scheme_id):
            new_scheme = get_object_or_404(BenevolentScheme, pk=sid)

        new_membership = None
        mid = request.POST.get("new_membership")
        if mid:
            new_membership = SchemeMembership.objects.filter(pk=mid).first()

        new_kind = request.POST.get("new_kind") or None

        try:
            corrected = exc_svc.correct_attribution(
                c, user=request.user, reason=reason,
                new_scheme=new_scheme, new_membership=new_membership,
                new_kind=new_kind)
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
            return redirect("benevolent_contribution_correct", pk=pk)
        messages.success(
            request, f"Contribution re-attributed. The original was reversed and a "
                     f"corrected entry of {corrected.amount} recorded.")
        return redirect("benevolent_contribution_list")


class ReconciliationView(BenevolentViewMixin, View):
    """Reconcile what a scheme has recorded as contributions against the bank
    receipts that carry the money — the benevolent-side counterpart to the fund
    bank reconciliation."""

    def get(self, request, pk):
        scheme = get_object_or_404(BenevolentScheme, pk=pk)
        from core.utils import parse_period
        start, end = parse_period(request)
        result = exc_svc.reconcile_scheme(scheme, start=start, end=end)
        return render(request, "benevolent/reconciliation.html", {
            "scheme": scheme, "result": result, "start": start, "end": end,
        })


class FundPositionView(BenevolentViewMixin, View):
    """The scheme fund's solvency: where the cash stands once every promise
    already made is honoured, and a plain forward projection of whether it is
    sustainable at the current run-rate (item 8: fund depletion, negative
    balance, reserved commitments, pending approved payouts, cash forecasting)."""

    def get(self, request, pk):
        from benevolent.services import solvency as sol_svc
        scheme = get_object_or_404(BenevolentScheme, pk=pk)
        months = 6
        try:
            months = max(3, min(12, int(request.GET.get("months", 6))))
        except (TypeError, ValueError):
            pass
        position = sol_svc.fund_position(scheme)
        forecast = sol_svc.forecast_scheme(scheme, months=months)
        return render(request, "benevolent/fund_position.html", {
            "scheme": scheme, "position": position, "forecast": forecast,
            "months": months,
        })


class FraudScanView(BenevolentViewMixin, View):
    """Red flags across the schemes — control breaches, membership abuse,
    identity/collusion patterns and contribution manipulation. Every item is a
    signal for a human to judge, never a verdict, and the scan blocks nothing."""

    def get(self, request):
        from benevolent.services import fraud as fraud_svc
        f_scheme = request.GET.get("scheme") or ""
        scheme = BenevolentScheme.objects.filter(pk=f_scheme).first() if f_scheme else None
        signals = fraud_svc.scan(scheme=scheme)
        # group by severity for display
        grouped = {"high": [], "medium": [], "low": []}
        for s in signals:
            grouped.setdefault(s.severity, []).append(s)
        return render(request, "benevolent/fraud_scan.html", {
            "signals": signals, "grouped": grouped,
            "summary": {"high": len(grouped["high"]),
                        "medium": len(grouped["medium"]),
                        "low": len(grouped["low"]), "total": len(signals)},
            "schemes": BenevolentScheme.objects.exclude(
                status=BenevolentScheme.Status.DRAFT),
            "f_scheme": f_scheme,
        })


class TaskListView(BenevolentViewMixin, View):
    """The review-task inbox: things automation noticed that need a human to
    decide. Automation never acts on a status; it raises a task here and waits."""

    def get(self, request):
        from benevolent.models import BenevolentTask
        f_scheme = request.GET.get("scheme") or ""
        f_status = request.GET.get("status") or "OPEN"
        qs = (BenevolentTask.objects.select_related(
            "scheme", "membership__member", "dependant", "case")
            .order_by("-created_at"))
        if f_scheme:
            qs = qs.filter(scheme_id=f_scheme)
        if f_status in dict(BenevolentTask.Status.choices):
            qs = qs.filter(status=f_status)
        from django.core.paginator import Paginator
        page = Paginator(qs, 40).get_page(request.GET.get("page"))
        open_count = BenevolentTask.objects.filter(
            status=BenevolentTask.Status.OPEN).count()
        return render(request, "benevolent/task_list.html", {
            "page_obj": page, "tasks": page.object_list,
            "schemes": BenevolentScheme.objects.exclude(
                status=BenevolentScheme.Status.DRAFT),
            "f_scheme": f_scheme, "f_status": f_status, "open_count": open_count,
            "statuses": BenevolentTask.Status.choices,
        })


class TaskResolveView(BenevolentManageMixin, View):
    """Mark a task actioned or dismissed. Records that the task was dealt with;
    it does not itself change any membership status — the human takes the actual
    action by following the task's link."""

    def post(self, request, pk):
        from benevolent.models import BenevolentTask
        from benevolent.services import automation as automation_svc
        task = get_object_or_404(BenevolentTask, pk=pk)
        action = request.POST.get("action")
        if action not in ("done", "dismiss"):
            messages.error(request, "Choose whether the task is actioned or dismissed.")
            return redirect("benevolent_task_list")
        automation_svc.resolve_task(
            task, user=request.user, action=action,
            note=request.POST.get("note") or "")
        messages.success(
            request, f"Task {'actioned' if action == 'done' else 'dismissed'}.")
        return redirect("benevolent_task_list")
