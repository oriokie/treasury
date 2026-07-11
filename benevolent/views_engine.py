"""Phase 4 views — the contribution engine and the intake queue."""
import datetime as dt

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from core.permissions import (BenevolentApproveMixin, BenevolentManageMixin,
                              BenevolentSettingsMixin, BenevolentViewMixin)

from .forms import AdjustmentForm, ContributionRuleForm, IntakeResolveForm, RefundForm
from .models import (BenevolentCase, BenevolentContribution, BenevolentScheme,
                     ContributionIntake, ContributionRule, MemberAdjustment,
                     SchemeMembership)
from .services import allocation as alloc_svc
from .services import engine as engine_svc


class IntakeQueueView(BenevolentManageMixin, View):
    """Receipts that are scheme money but are not yet attached to a member.

    The banner on this page matters as much as the list: THE MONEY IS ALREADY
    BANKED. Every row here is already a receipt, already in the fund, already in the
    general ledger and already on the bank reconciliation. What is missing is only
    the answer to "whose is it?" — and a treasurer who thinks this queue represents
    lost money will make bad decisions to clear it.
    """

    def get(self, request):
        f_status = request.GET.get("status") or ""
        qs = (ContributionIntake.objects
              .select_related("transaction", "scheme", "suggested_membership__member",
                              "suggested_case", "duplicate_of__transaction")
              .order_by("-created_at"))
        if f_status:
            qs = qs.filter(status=f_status)
        else:
            qs = qs.filter(status__in=ContributionIntake.OPEN_STATUSES)

        page = Paginator(qs, 30).get_page(request.GET.get("page"))
        counts = dict(ContributionIntake.objects.values_list("status")
                      .annotate(n=Count("id")))
        open_value = (ContributionIntake.objects
                      .filter(status__in=ContributionIntake.OPEN_STATUSES)
                      .aggregate(t=Sum("transaction__amount"))["t"] or 0)

        return render(request, "benevolent/intake_queue.html", {
            "page_obj": page, "items": page.object_list,
            "statuses": ContributionIntake.Status.choices,
            "f_status": f_status, "counts": counts,
            "open_value": open_value,
            "open_count": sum(counts.get(s, 0)
                              for s in ContributionIntake.OPEN_STATUSES),
        })


class IntakeItemView(BenevolentManageMixin, View):
    """One queue item, with everything the allocator thought and why."""

    def get(self, request, pk):
        item = get_object_or_404(
            ContributionIntake.objects.select_related(
                "transaction", "scheme", "duplicate_of__transaction"), pk=pk)
        return render(request, "benevolent/intake_item.html", {
            "item": item, "form": IntakeResolveForm(item=item),
            "candidates": item.candidates or [],
        })

    def post(self, request, pk):
        item = get_object_or_404(ContributionIntake, pk=pk)

        if request.POST.get("reject"):
            try:
                engine_svc.reject(item, user=request.user,
                                  note=(request.POST.get("note") or ""))
                messages.success(
                    request,
                    "Marked as not scheme money. Note that the receipt itself is "
                    "untouched — it stays in the ledger and on the bank reconciliation. "
                    "Deciding money is not ours is a statement about attribution, not "
                    "about whether the church received it.")
            except ValidationError as e:
                messages.error(request, "; ".join(e.messages))
            return redirect("benevolent_intake_queue")

        form = IntakeResolveForm(request.POST, item=item)
        if not form.is_valid():
            messages.error(request, "Say who the money belongs to.")
            return render(request, "benevolent/intake_item.html", {
                "item": item, "form": form, "candidates": item.candidates or []})
        d = form.cleaned_data
        try:
            c = engine_svc.resolve(
                item, membership=d.get("membership"), case=d.get("case"),
                kind=d["kind"], user=request.user, note=d.get("note") or "")
        except ValidationError as e:
            for msg in e.messages:
                form.add_error(None, msg)
            return render(request, "benevolent/intake_item.html", {
                "item": item, "form": form, "candidates": item.candidates or []})

        messages.success(
            request, f"{c.amount} attributed to "
                     f"{c.membership.member.name if c.membership else 'a donation'} "
                     f"as {c.get_kind_display().lower()}.")
        return redirect("benevolent_intake_queue")


class ContributionRuleView(BenevolentSettingsMixin, View):
    """The narration rules — configurable, and the same shape as the main allocation
    engine's, so a church learns one idea and not two."""

    def get(self, request):
        edit = None
        if request.GET.get("edit"):
            edit = ContributionRule.objects.filter(pk=request.GET["edit"]).first()
        return render(request, "benevolent/rules.html", {
            "rules": ContributionRule.objects.select_related("scheme"),
            "form": ContributionRuleForm(instance=edit),
            "editing": edit,
            "proposed": ContributionRule.objects.filter(
                source="LEARNED", active=False).select_related("scheme"),
        })

    def post(self, request):
        if request.POST.get("delete"):
            ContributionRule.objects.filter(pk=request.POST["delete"]).delete()
            messages.success(request, "Rule deleted.")
            return redirect("benevolent_rules")
        if request.POST.get("activate"):
            r = ContributionRule.objects.filter(pk=request.POST["activate"]).first()
            if r:
                r.active = True
                r.save(update_fields=["active"])
                messages.success(request, f"'{r.pattern}' is now routing money to "
                                          f"{r.scheme.code}.")
            return redirect("benevolent_rules")

        edit = ContributionRule.objects.filter(
            pk=request.POST.get("edit_id")).first() if request.POST.get("edit_id") else None
        form = ContributionRuleForm(request.POST, instance=edit)
        if form.is_valid():
            form.save()
            messages.success(request, "Rule saved.")
            return redirect("benevolent_rules")
        messages.error(request, "Check the rule.")
        return render(request, "benevolent/rules.html", {
            "rules": ContributionRule.objects.select_related("scheme"),
            "form": form, "editing": edit,
            "proposed": ContributionRule.objects.filter(
                source="LEARNED", active=False)})


class AllocationTestView(BenevolentManageMixin, View):
    """Try a narration against the allocator and see exactly what it would do.

    Worth its own screen. A treasurer who can ask "what would you do with this?" and
    see every signal will trust the queue; one who can only watch money arrive in the
    wrong place will not.
    """

    def get(self, request):
        ref = request.GET.get("reference") or ""
        phone = request.GET.get("phone") or ""
        name = request.GET.get("name") or ""
        amount = request.GET.get("amount") or ""
        result = None
        if ref or phone or name:
            from decimal import Decimal
            try:
                amt = Decimal(amount) if amount else None
            except Exception:  # noqa: BLE001
                amt = None
            result = alloc_svc.allocate(reference=ref, phone=phone, name=name,
                                        amount=amt, date=dt.date.today())
        return render(request, "benevolent/allocation_test.html", {
            "reference": ref, "phone": phone, "name": name, "amount": amount,
            "result": result,
            "weights": sorted(alloc_svc.WEIGHTS.items(), key=lambda kv: -kv[1]),
        })


class AdjustmentView(BenevolentManageMixin, View):
    """Propose a penalty, a waiver or a write-off. No money moves."""

    def post(self, request, pk):
        m = get_object_or_404(SchemeMembership, pk=pk)
        form = AdjustmentForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Check the adjustment — a reason is required.")
            return redirect("benevolent_membership_detail", pk=pk)
        d = form.cleaned_data
        try:
            engine_svc.charge(m, kind=d["kind"], amount=d["amount"],
                              reason=d["reason"], on=d["on"],
                              period_label=d.get("period_label") or "",
                              user=request.user)
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
        else:
            messages.success(
                request,
                "Proposed. It changes nothing until someone else approves it — and it "
                "posts no accounting entry either way: a penalty is not income until it "
                "is paid, and a waiver is not an expense at all.")
        return redirect("benevolent_membership_detail", pk=pk)


class AdjustmentDecisionView(BenevolentApproveMixin, View):
    def post(self, request, pk, action):
        adj = get_object_or_404(
            MemberAdjustment.objects.select_related("membership"), pk=pk)
        try:
            if action == "approve":
                engine_svc.approve_adjustment(adj, user=request.user)
                messages.success(request, f"{adj.get_kind_display()} approved.")
            elif action == "reverse":
                engine_svc.reverse_adjustment(
                    adj, user=request.user,
                    reason=(request.POST.get("reason") or "").strip())
                messages.success(request, f"{adj.get_kind_display()} reversed.")
            else:
                messages.error(request, "Unknown action.")
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
        return redirect("benevolent_membership_detail", pk=adj.membership_id)


class RefundView(BenevolentManageMixin, View):
    """Return money to a member. A payment voucher, like any other."""

    def post(self, request, pk):
        m = get_object_or_404(SchemeMembership, pk=pk)
        form = RefundForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Check the refund.")
            return redirect("benevolent_membership_detail", pk=pk)
        d = form.cleaned_data
        try:
            r = engine_svc.refund(
                m, amount=d["amount"], reason=d["reason"], date=d["date"],
                method=d["method"], voucher_no=d.get("voucher_no") or "",
                user=request.user)
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
        else:
            messages.success(
                request,
                f"Refund voucher for {r.amount} raised. It is pending approval in the "
                f"expenses queue like any other payment — this module never approves "
                f"its own.")
        return redirect("benevolent_membership_detail", pk=pk)
