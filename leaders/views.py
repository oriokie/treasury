import datetime as dt
from decimal import Decimal

from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import TemplateView

from core.permissions import RoleRequiredMixin  # noqa (kept for symmetry)
from core.utils import parse_period
from reports.services import balances
from departments.models import Department, DevelopmentGroup
from members.models import mask_phone
from .permissions import LeaderRequiredMixin, allowed_departments, assert_department_allowed


def _scoped_rows(user, start, end):
    """Per-department balance rows (un-consolidated) limited to the leader's
    departments. Filtering happens here, server-side, on the id set."""
    allowed_ids = set(allowed_departments(user).values_list("id", flat=True))
    rows = balances.department_summary(start, end, consolidated=False)
    return [r for r in rows if r["department"].id in allowed_ids]


class LeaderDashboardView(LeaderRequiredMixin, TemplateView):
    template_name = "leaders/dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        start, end = parse_period(self.request)
        ctx["start"], ctx["end"] = start, end
        rows = _scoped_rows(self.request.user, start, end)
        # only the directly-led departments head the list; their sub-accounts are
        # shown nested under them
        led_ids = set(allowed_departments(self.request.user)
                      .filter(parent__isnull=True).values_list("id", flat=True))
        # group sub-accounts under their parent
        by_parent = {}
        tops = []
        for r in rows:
            d = r["department"]
            if d.parent_id:
                by_parent.setdefault(d.parent_id, []).append(r)
            else:
                tops.append(r)
        for r in tops:
            r["subrows"] = by_parent.get(r["department"].id, [])
        ctx["rows"] = tops
        ctx["total_receipts"] = sum((r["receipts"] for r in rows), Decimal(0))
        ctx["total_expenses"] = sum((r["expenses_operating"] for r in rows), Decimal(0))
        ctx["dept_count"] = len({r["department"].id for r in rows})
        return ctx


class LeaderDepartmentDetailView(LeaderRequiredMixin, TemplateView):
    template_name = "leaders/department_detail.html"

    def get(self, request, *args, **kwargs):
        dept = assert_department_allowed(request.user, kwargs["pk"])
        if not dept:
            # not their department — refuse, send back to their dashboard
            return redirect("leader_dashboard")
        self.dept = dept
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        start, end = parse_period(self.request)
        ctx["start"], ctx["end"] = start, end
        dept = self.dept
        ctx["dept"] = dept

        # this department's row + its sub-accounts (the leader may see those)
        allowed_ids = set(allowed_departments(self.request.user).values_list("id", flat=True))
        rows = balances.department_summary(start, end, consolidated=False)
        ctx["row"] = next((r for r in rows if r["department"].id == dept.id), None)
        ctx["subrows"] = [r for r in rows
                          if r["department"].parent_id == dept.id
                          and r["department"].id in allowed_ids]

        # recent collections (credits) for this department — read only
        from giving.models import Transaction
        txns = (Transaction.objects.filter(department=dept,
                    direction=Transaction.Direction.CREDIT, confirmed=True,
                    is_reversal=False, is_reversed=False,
                    date__gte=start, date__lte=end)
                .select_related("member").order_by("-date")[:50])
        ctx["collections"] = [{
            "date": t.date, "amount": t.amount,
            "channel": t.get_channel_display(),
            "who": t.member.name if t.member_id else (t.payer_name or "—"),
            "phone": mask_phone(t.member.phone if t.member_id else t.payer_phone),
            "reference": t.reference,
        } for t in txns]

        # expenses charged to this department — read only
        from cashbook.models import Expense
        exps = (Expense.objects.filter(department=dept,
                    date__gte=start, date__lte=end)
                .order_by("-date")[:50])
        ctx["expenses"] = exps

        # development groups (for a development leader) — show progress vs target
        if dept.category == Department.Category.DEVELOPMENT:
            from giving.models import Transaction
            from django.db.models import Sum
            groups = DevelopmentGroup.objects.filter(active=True)
            dg_rows = []
            for g in groups:
                collected = (Transaction.objects.filter(dev_group=g,
                                direction=Transaction.Direction.CREDIT,
                                confirmed=True, is_reversal=False, is_reversed=False)
                             .aggregate(s=Sum("amount"))["s"] or Decimal(0))
                tgt = g.target or Decimal(0)
                pct = int(min(collected / tgt * 100, 100)) if tgt else None
                dg_rows.append({"group": g, "collected": collected,
                                "target": tgt, "pct": pct})
            ctx["dev_groups"] = dg_rows
        else:
            ctx["dev_groups"] = None

        # pledges toward this department's campaigns — amounts visible, phones masked
        try:
            from pledges.models import Pledge
            pledges = (Pledge.objects.filter(
                          campaign__target_department=dept)
                       .exclude(status=Pledge.Status.CANCELLED)
                       .select_related("member", "campaign").order_by("-start_date"))
            ctx["pledges"] = [{
                "member": p.member.name,
                "phone": mask_phone(p.member.phone),
                "campaign": p.campaign.name,
                "amount": p.amount, "paid": p.paid, "outstanding": p.outstanding,
                "status": p.get_status_display(),
            } for p in pledges]
            ctx["pledge_total"] = sum((p["amount"] for p in ctx["pledges"]), Decimal(0))
        except Exception:
            ctx["pledges"] = []
            ctx["pledge_total"] = Decimal(0)

        return ctx
