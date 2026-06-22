import datetime as dt
from decimal import Decimal

from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import TemplateView

from core.permissions import RoleRequiredMixin  # noqa (kept for symmetry)
from core.utils import parse_period
from reports.services import balances
from departments.models import Department, DevelopmentGroup
from members.models import mask_phone
from core.rights import display_phone, display_giver
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
        # A row heads the list (is a "top") when its parent is NOT also in the
        # leader's set — i.e. it's a root of the leader's allowed tree. So a leader
        # assigned the parent fund sees it with its sub-accounts nested; a leader
        # assigned a single subgroup directly still sees that subgroup at the top
        # (it isn't hidden under a parent they don't lead).
        allowed_ids = set(allowed_departments(self.request.user)
                          .values_list("id", flat=True))
        by_parent = {}
        tops = []
        for r in rows:
            d = r["department"]
            if d.parent_id and d.parent_id in allowed_ids:
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
        export = request.GET.get("export")
        if export in ("groups_csv", "groups_xlsx"):
            return self._export_groups(request, export)
        return super().get(request, *args, **kwargs)

    def _dev_group_rows(self, start, end):
        from decimal import Decimal
        from django.db.models import Sum
        from giving.models import Transaction
        # one grouped query for all groups, not an aggregate per group
        collected_map = {r["dev_group"]: (r["s"] or Decimal(0)) for r in
            Transaction.objects.filter(
                direction=Transaction.Direction.CREDIT, confirmed=True,
                is_reversal=False, is_reversed=False, dev_group__isnull=False,
                date__gte=start, date__lte=end)
            .values("dev_group").annotate(s=Sum("amount"))}
        rows = []
        for g in DevelopmentGroup.objects.filter(active=True):
            collected = collected_map.get(g.id, Decimal(0))
            tgt = g.target or Decimal(0)
            pct = int(min(collected / tgt * 100, 100)) if tgt else None
            rows.append({"group": g, "collected": collected, "target": tgt, "pct": pct})
        rows.sort(key=lambda r: r["collected"], reverse=True)
        return rows

    def _export_groups(self, request, export):
        from reports.exports import csv_response, xlsx_response
        from core.models import SiteConfig
        start, end = parse_period(request)
        rows = self._dev_group_rows(start, end)
        header = ["Group", "Collected", "Target", "% of target"]
        data = [[str(r["group"]), float(r["collected"]), float(r["target"]),
                 (r["pct"] if r["pct"] is not None else "")] for r in rows]
        title = (f"{self.dept.name} — development groups, "
                 f"{start:%d %b %Y} to {end:%d %b %Y}")
        fn = f"dev_groups_{self.dept.slug or self.dept.id}_{start:%Y%m%d}_{end:%Y%m%d}"
        if export == "groups_xlsx":
            return xlsx_response(f"{fn}.xlsx", header, data, title=title,
                                 church=SiteConfig.get().church_name)
        return csv_response(f"{fn}.csv", header, data)

    def get_context_data(self, **kwargs):
        import json
        from decimal import Decimal
        from django.db.models import Sum, Count
        from django.db.models.functions import ExtractMonth
        from giving.models import Transaction
        from cashbook.models import Expense
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
        # the whole area this leader manages here: the department + visible subs
        dept_ids = {dept.id} | {r["department"].id for r in ctx["subrows"]}

        MN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        credit_q = dict(department_id__in=dept_ids,
                        direction=Transaction.Direction.CREDIT, confirmed=True,
                        is_reversal=False, is_reversed=False, excluded_from_income=False)

        # --- headline KPIs ---------------------------------------------------
        receipts = (Transaction.objects.filter(date__gte=start, date__lte=end, **credit_q)
                    .aggregate(s=Sum("amount"))["s"] or Decimal(0))
        spend = (Expense.objects.filter(department_id__in=dept_ids,
                    status__in=[Expense.Status.APPROVED, Expense.Status.PAID],
                    date__gte=start, date__lte=end)
                 .exclude(category=Expense.Category.REMITTANCE)
                 .aggregate(s=Sum("amount"))["s"] or Decimal(0))
        gift_count = (Transaction.objects.filter(date__gte=start, date__lte=end, **credit_q)
                      .count())
        ctx["kpi"] = {
            "closing": ctx["row"]["closing"] if ctx["row"] else Decimal(0),
            "receipts": receipts, "spend": spend, "gifts": gift_count,
            "net": receipts - spend,
        }

        # --- monthly trend (receipts vs spend) -------------------------------
        mrec = {r["m"]: float(r["t"] or 0) for r in
                Transaction.objects.filter(date__gte=start, date__lte=end, **credit_q)
                .annotate(m=ExtractMonth("date")).values("m").annotate(t=Sum("amount"))}
        mexp = {r["m"]: float(r["t"] or 0) for r in
                Expense.objects.filter(department_id__in=dept_ids,
                    status__in=[Expense.Status.APPROVED, Expense.Status.PAID],
                    date__gte=start, date__lte=end)
                .exclude(category=Expense.Category.REMITTANCE)
                .annotate(m=ExtractMonth("date")).values("m").annotate(t=Sum("amount"))}
        active_m = sorted(set(mrec) | set(mexp))
        ctx["monthly_json"] = json.dumps({
            "labels": [MN[m - 1] for m in active_m],
            "receipts": [mrec.get(m, 0) for m in active_m],
            "expenses": [mexp.get(m, 0) for m in active_m]})
        ctx["has_monthly"] = len(active_m) >= 2

        # --- income by channel ----------------------------------------------
        ch_labels = {"BANK": "Bank / M-Pesa", "CASH": "Cash", "ENVELOPE": "Envelopes"}
        ch = (Transaction.objects.filter(date__gte=start, date__lte=end, **credit_q)
              .values("channel").annotate(t=Sum("amount")).order_by("-t"))
        ctx["channel_json"] = json.dumps([
            {"label": ch_labels.get(c["channel"], c["channel"] or "Other"),
             "value": float(c["t"] or 0)} for c in ch if c["t"]])
        ctx["has_channel"] = bool(ch)

        # --- top contributors ------------------------------------------------
        top = (Transaction.objects.filter(date__gte=start, date__lte=end, **credit_q)
               .values("member__name", "payer_name")
               .annotate(t=Sum("amount"), n=Count("id")).order_by("-t")[:6])
        ctx["top_givers"] = [{
            "who": display_giver(self.request.user, r["member__name"] or r["payer_name"]) or "—",
            "total": r["t"] or Decimal(0), "n": r["n"]} for r in top]

        # --- budget vs actual ------------------------------------------------
        if dept.annual_budget:
            pct = int(min(spend / dept.annual_budget * 100, 100)) if dept.annual_budget else 0
            ctx["budget"] = {"budget": dept.annual_budget, "spent": spend, "pct": pct,
                             "over": spend > dept.annual_budget}
        else:
            ctx["budget"] = None

        # --- recent collections / expenses previews --------------------------
        txns = (Transaction.objects.filter(department=dept,
                    direction=Transaction.Direction.CREDIT, confirmed=True,
                    is_reversal=False, is_reversed=False,
                    date__gte=start, date__lte=end)
                .select_related("member").order_by("-date")[:8])
        ctx["collections"] = [{
            "date": t.date, "amount": t.amount, "channel": t.get_channel_display(),
            "who": display_giver(self.request.user, t.member.name if t.member_id else t.payer_name) or "—",
            "phone": display_phone(self.request.user, t.member.phone if t.member_id else t.payer_phone),
            "reference": t.reference,
        } for t in txns]
        ctx["expenses"] = (Expense.objects.filter(department=dept,
                    date__gte=start, date__lte=end).order_by("-date")[:8])

        # which of these funds may actually carry expenses — used to hide the
        # expenses preview and columns for collection-only subgroups.
        from departments.models import expense_departments
        elig_ids = {d.id for d in expense_departments()}
        ctx["expenses_eligible"] = dept.id in elig_ids
        for r in ctx["subrows"]:
            r["expenses_eligible"] = r["department"].id in elig_ids
        ctx["any_sub_expenses"] = any(r.get("expenses_eligible") for r in ctx["subrows"])

        # --- development groups (for a development leader) -------------------
        if dept.category == Department.Category.DEVELOPMENT:
            ctx["dev_groups"] = self._dev_group_rows(start, end)
            ctx["dev_collected"] = sum((r["collected"] for r in ctx["dev_groups"]),
                                       Decimal(0))
        else:
            ctx["dev_groups"] = None

        # --- pledges summary -------------------------------------------------
        try:
            from pledges.models import Pledge
            pledges = (Pledge.objects.filter(campaign__target_department=dept)
                       .exclude(status=Pledge.Status.CANCELLED)
                       .select_related("member", "campaign"))
            p_total = sum((p.amount for p in pledges), Decimal(0))
            p_paid = sum((p.paid for p in pledges), Decimal(0))
            ctx["pledge_summary"] = {
                "count": pledges.count(), "pledged": p_total, "paid": p_paid,
                "outstanding": p_total - p_paid,
                "pct": int(min(p_paid / p_total * 100, 100)) if p_total else 0,
            } if pledges.exists() else None
        except Exception:
            ctx["pledge_summary"] = None

        return ctx


# ===========================================================================
# Detailed, downloadable leader pages (item 2)
# ===========================================================================
def _leads_a_development_dept(user):
    return allowed_departments(user).filter(
        category=Department.Category.DEVELOPMENT).exists()


def _collection_rows(dept, start, end, user):
    """Full collections (credits) for a department in a period, newest first."""
    from giving.models import Transaction
    txns = (Transaction.objects.filter(
                department=dept, direction=Transaction.Direction.CREDIT,
                confirmed=True, is_reversal=False, is_reversed=False,
                date__gte=start, date__lte=end)
            .select_related("member", "dev_group").order_by("-date", "-id"))
    out = []
    for t in txns:
        out.append({
            "date": t.date, "amount": t.amount, "channel": t.get_channel_display(),
            "who": display_giver(user, t.member.name if t.member_id else t.payer_name) or "—",
            "phone": display_phone(user, t.member.phone if t.member_id else t.payer_phone),
            "reference": t.reference or "",
            "group": t.dev_group.label if t.dev_group_id else "",
        })
    return out


class LeaderCollectionsView(LeaderRequiredMixin, TemplateView):
    """Full, downloadable collections list for one of the leader's departments."""
    template_name = "leaders/collections.html"

    def get(self, request, *args, **kwargs):
        self.dept = assert_department_allowed(request.user, kwargs["pk"])
        if not self.dept:
            return redirect("leader_dashboard")
        start, end = parse_period(request)
        export = request.GET.get("export")
        if export in ("csv", "xlsx"):
            from reports.exports import csv_response, xlsx_response
            from core.models import SiteConfig
            rows = _collection_rows(self.dept, start, end, self.request.user)
            header = ["Date", "Contributor", "Phone", "Reference", "Channel", "Group", "Amount"]
            data = [[r["date"].isoformat(), r["who"], r["phone"], r["reference"],
                     r["channel"], r["group"], float(r["amount"])] for r in rows]
            title = f"{self.dept.name} collections {start:%d %b %Y}–{end:%d %b %Y}"
            fn = f"collections_{self.dept.slug or self.dept.id}"
            if export == "xlsx":
                return xlsx_response(f"{fn}.xlsx", header, data, title=title,
                                     church=SiteConfig.get().church_name)
            return csv_response(f"{fn}.csv", header, data)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        start, end = parse_period(self.request)
        ctx["dept"] = self.dept
        ctx["start"], ctx["end"] = start, end
        rows = _collection_rows(self.dept, start, end, self.request.user)
        ctx["rows"] = rows
        ctx["total"] = sum((r["amount"] for r in rows), Decimal(0))
        ctx["count"] = len(rows)
        return ctx


class LeaderExpensesView(LeaderRequiredMixin, TemplateView):
    """Full, downloadable expense list for one of the leader's departments."""
    template_name = "leaders/expenses.html"

    def get(self, request, *args, **kwargs):
        self.dept = assert_department_allowed(request.user, kwargs["pk"])
        if not self.dept:
            return redirect("leader_dashboard")
        start, end = parse_period(request)
        export = request.GET.get("export")
        if export in ("csv", "xlsx"):
            from reports.exports import csv_response, xlsx_response
            from core.models import SiteConfig
            from cashbook.models import Expense
            exps = (Expense.objects.filter(department=self.dept,
                        date__gte=start, date__lte=end).order_by("-date", "-id"))
            header = ["Date", "Description", "Category", "Claimant", "Method",
                      "Status", "Amount"]
            data = [[e.date.isoformat(), e.description, e.get_category_display(),
                     e.claimant or "", e.get_method_display(),
                     e.get_status_display(), float(e.amount)] for e in exps]
            title = f"{self.dept.name} expenses {start:%d %b %Y}–{end:%d %b %Y}"
            fn = f"expenses_{self.dept.slug or self.dept.id}"
            if export == "xlsx":
                return xlsx_response(f"{fn}.xlsx", header, data, title=title,
                                     church=SiteConfig.get().church_name)
            return csv_response(f"{fn}.csv", header, data)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        from cashbook.models import Expense
        from django.db.models import Sum
        ctx = super().get_context_data(**kwargs)
        start, end = parse_period(self.request)
        ctx["dept"] = self.dept
        ctx["start"], ctx["end"] = start, end
        exps = (Expense.objects.filter(department=self.dept,
                    date__gte=start, date__lte=end).order_by("-date", "-id"))
        ctx["expenses"] = exps
        ctx["total"] = exps.aggregate(s=Sum("amount"))["s"] or Decimal(0)
        ctx["count"] = exps.count()
        return ctx


class LeaderGroupDetailView(LeaderRequiredMixin, TemplateView):
    """Drill-down for a single development group: its performance vs target and
    the full per-contributor list, downloadable. Visible only to a leader who
    leads a development department."""
    template_name = "leaders/group_detail.html"

    def get(self, request, *args, **kwargs):
        if not _leads_a_development_dept(request.user):
            return redirect("leader_dashboard")
        self.group = get_object_or_404(DevelopmentGroup, pk=kwargs["pk"])
        start, end = parse_period(request)
        export = request.GET.get("export")
        if export in ("csv", "xlsx"):
            from reports.exports import csv_response, xlsx_response
            from core.models import SiteConfig
            data_obj = balances.dev_group_members(self.group, start, end)
            header = ["Contributor", "Phone", "Contributions", "Total"]
            data = [[display_giver(request.user, r["name"]), display_phone(request.user, r["phone"]), r["count"], float(r["total"])]
                    for r in data_obj["rows"]]
            title = f"{self.group.label} contributions {start:%d %b %Y}–{end:%d %b %Y}"
            fn = f"group_{self.group.number}_contributions"
            if export == "xlsx":
                return xlsx_response(f"{fn}.xlsx", header, data, title=title,
                                     church=SiteConfig.get().church_name)
            return csv_response(f"{fn}.csv", header, data)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        start, end = parse_period(self.request)
        ctx["start"], ctx["end"] = start, end
        g = self.group
        ctx["group"] = g
        data_obj = balances.dev_group_members(g, start, end)
        # mask phones for display
        ctx["rows"] = [{"name": display_giver(self.request.user, r["name"]), "phone": display_phone(self.request.user, r["phone"]),
                        "count": r["count"], "total": r["total"]}
                       for r in data_obj["rows"]]
        ctx["total"] = data_obj["total"]
        ctx["target"] = g.target or Decimal(0)
        ctx["pct"] = (int(min(data_obj["total"] / g.target * 100, 100))
                      if g.target else None)
        return ctx


class LeaderPledgesView(LeaderRequiredMixin, TemplateView):
    """Full, downloadable pledge list for one of the leader's departments."""
    template_name = "leaders/pledges.html"

    def get(self, request, *args, **kwargs):
        self.dept = assert_department_allowed(request.user, kwargs["pk"])
        if not self.dept:
            return redirect("leader_dashboard")
        export = request.GET.get("export")
        if export in ("csv", "xlsx"):
            from reports.exports import csv_response, xlsx_response
            from core.models import SiteConfig
            rows = self._rows()
            header = ["Member", "Phone", "Campaign", "Pledged", "Paid",
                      "Outstanding", "Status"]
            data = [[r["member"], r["phone"], r["campaign"], float(r["amount"]),
                     float(r["paid"]), float(r["outstanding"]), r["status"]]
                    for r in rows]
            title = f"{self.dept.name} pledges"
            fn = f"pledges_{self.dept.slug or self.dept.id}"
            if export == "xlsx":
                return xlsx_response(f"{fn}.xlsx", header, data, title=title,
                                     church=SiteConfig.get().church_name)
            return csv_response(f"{fn}.csv", header, data)
        return super().get(request, *args, **kwargs)

    def _rows(self):
        from pledges.models import Pledge
        out = []
        try:
            pledges = (Pledge.objects.filter(campaign__target_department=self.dept)
                       .exclude(status=Pledge.Status.CANCELLED)
                       .select_related("member", "campaign").order_by("-start_date"))
            for p in pledges:
                out.append({
                    "member": display_giver(self.request.user, p.member.name), "phone": display_phone(self.request.user, p.member.phone),
                    "campaign": p.campaign.name, "amount": p.amount,
                    "paid": p.paid, "outstanding": p.outstanding,
                    "status": p.get_status_display(), "pct": p.percent_paid
                    if hasattr(p, "percent_paid") else
                    (int(min(p.paid / p.amount * 100, 100)) if p.amount else 0)})
        except Exception:
            pass
        return out

    def get_context_data(self, **kwargs):
        from decimal import Decimal
        ctx = super().get_context_data(**kwargs)
        ctx["dept"] = self.dept
        rows = self._rows()
        ctx["rows"] = rows
        ctx["pledged"] = sum((r["amount"] for r in rows), Decimal(0))
        ctx["paid"] = sum((r["paid"] for r in rows), Decimal(0))
        ctx["outstanding"] = sum((r["outstanding"] for r in rows), Decimal(0))
        ctx["pct"] = (int(min(ctx["paid"] / ctx["pledged"] * 100, 100))
                      if ctx["pledged"] else 0)
        return ctx
