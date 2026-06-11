from django.contrib import messages
from django.urls import reverse_lazy
from django.views.generic import ListView, CreateView, UpdateView, DeleteView

from core.permissions import ReadAccessMixin, TreasurerRequiredMixin
from .forms import DepartmentForm, DevelopmentGroupForm
from .models import Department, DevelopmentGroup


class DepartmentListView(ReadAccessMixin, ListView):
    model = Department
    template_name = "departments/list.html"
    context_object_name = "departments"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        tops = (Department.objects.filter(parent__isnull=True)
                .prefetch_related("subgroups"))
        dev_groups = DevelopmentGroup.objects.all()
        tree = []
        for f in tops:
            node = {"fund": f, "subs": list(f.subgroups.all()), "dev_groups": []}
            if f.name.lower() == "development":
                node["dev_groups"] = list(dev_groups)
            tree.append(node)
        ctx["fund_tree"] = tree
        ctx["dev_groups"] = dev_groups
        from giving.models import SplitFund
        ctx["split_funds"] = SplitFund.objects.prefetch_related("components__department")
        return ctx


class DepartmentCreateView(TreasurerRequiredMixin, CreateView):
    model = Department
    form_class = DepartmentForm
    template_name = "departments/form.html"
    success_url = reverse_lazy("department_list")

    def form_valid(self, form):
        messages.success(self.request, "Fund created.")
        return super().form_valid(form)


class DepartmentUpdateView(TreasurerRequiredMixin, UpdateView):
    model = Department
    form_class = DepartmentForm
    template_name = "departments/form.html"
    success_url = reverse_lazy("department_list")

    def form_valid(self, form):
        messages.success(self.request, "Fund updated.")
        return super().form_valid(form)


# ---- Development groups (CRUD) ----
class DevGroupCreateView(TreasurerRequiredMixin, CreateView):
    model = DevelopmentGroup
    form_class = DevelopmentGroupForm
    template_name = "departments/dev_group_form.html"
    success_url = reverse_lazy("department_list")

    def form_valid(self, form):
        messages.success(self.request, "Development group created.")
        return super().form_valid(form)


class DevGroupUpdateView(TreasurerRequiredMixin, UpdateView):
    model = DevelopmentGroup
    form_class = DevelopmentGroupForm
    template_name = "departments/dev_group_form.html"
    success_url = reverse_lazy("department_list")

    def form_valid(self, form):
        messages.success(self.request, "Development group updated.")
        return super().form_valid(form)


class DevGroupDeleteView(TreasurerRequiredMixin, DeleteView):
    model = DevelopmentGroup
    template_name = "departments/dev_group_confirm_delete.html"
    success_url = reverse_lazy("department_list")

    def form_valid(self, form):
        messages.success(self.request, "Development group deleted.")
        return super().form_valid(form)


import datetime as _dt
from decimal import Decimal, InvalidOperation
from django.shortcuts import render, redirect
from django.views import View


class BudgetView(TreasurerRequiredMixin, View):
    """Annual budgeting: set each fund's annual budget and brought-forward
    opening balance in one place (kept off the structural Funds page)."""
    template_name = "departments/budget.html"

    def _ordered(self):
        from collections import defaultdict
        kids = defaultdict(list)
        tops = []
        for d in Department.objects.filter(active=True).select_related("parent"):
            (kids[d.parent_id].append(d) if d.parent_id else tops.append(d))
        rows = []
        for t in sorted(tops, key=lambda x: x.name):
            rows.append({"d": t, "child": False})
            for c in sorted(kids.get(t.id, []), key=lambda x: x.name):
                rows.append({"d": c, "child": True})
        return rows

    def get(self, request):
        from .models import Budget
        year = int(request.GET.get("year") or _dt.date.today().year)
        budget_objs = {b.department_id: b for b in
                       Budget.objects.filter(year=year).prefetch_related("lines")}
        rows = self._ordered()
        for r in rows:
            b = budget_objs.get(r["d"].id)
            if b is not None:
                lt = b.lines_total
                r["budget"] = lt if lt else b.amount
                r["from_lines"] = bool(lt)
            else:
                r["budget"] = None
                r["from_lines"] = False
        return render(request, self.template_name,
                      {"rows": rows, "year": year,
                       "years": range(_dt.date.today().year + 1, _dt.date.today().year - 5, -1)})

    def post(self, request):
        from .models import Budget
        year = int(request.POST.get("year") or _dt.date.today().year)
        n = 0
        for d in Department.objects.filter(active=True):
            b = request.POST.get(f"budget_{d.id}")
            o = request.POST.get(f"opening_{d.id}")
            changed = False
            if b is not None:
                try:
                    amt = Decimal(b) if b.strip() else None
                    if amt is None:
                        Budget.objects.filter(year=year, department=d).delete()
                    else:
                        Budget.objects.update_or_create(
                            year=year, department=d, defaults={"amount": amt})
                    n += 1
                except (InvalidOperation, AttributeError):
                    pass
            if o is not None:
                try:
                    d.opening_balance = Decimal(o) if o.strip() else Decimal(0)
                    changed = True
                except (InvalidOperation, AttributeError):
                    pass
            if changed:
                d.save(update_fields=["opening_balance"])
        messages.success(request, f"Saved budgets and opening balances for {year}.")
        return redirect(f"{reverse_lazy('budget')}?year={year}")


class BudgetLinesView(TreasurerRequiredMixin, View):
    """Manage the breakdown lines for one fund's annual budget."""
    template_name = "departments/budget_lines.html"

    def get(self, request, pk):
        from .models import Budget, BudgetLine, lcb_fund
        from cashbook.models import Expense
        from django.shortcuts import get_object_or_404
        dept = get_object_or_404(Department, pk=pk)
        year = int(request.GET.get("year") or _dt.date.today().year)
        budget, _ = Budget.objects.get_or_create(year=year, department=dept)
        lcb = lcb_fund()
        other_funds = Department.objects.filter(active=True).exclude(pk=dept.id)
        if lcb:
            other_funds = other_funds.exclude(pk=lcb.id)
        prior = Budget.objects.filter(year=year - 1, department=dept).first()
        return render(request, self.template_name, {
            "dept": dept, "year": year, "budget": budget,
            "lines": budget.lines.select_related("source_fund").all(),
            "categories": Expense.Category.choices,
            "lcb": lcb, "other_funds": other_funds.order_by("name"),
            "prior_year": year - 1,
            "prior_total": (prior.lines_total if prior else None),
            "prior_has_lines": bool(prior and prior.lines.exists()),
            "years": range(_dt.date.today().year + 1, _dt.date.today().year - 5, -1),
        })

    def post(self, request, pk):
        from .models import Budget, BudgetLine
        from django.shortcuts import get_object_or_404
        dept = get_object_or_404(Department, pk=pk)
        year = int(request.POST.get("year") or _dt.date.today().year)
        budget, _ = Budget.objects.get_or_create(year=year, department=dept)
        action = request.POST.get("action")
        if action == "add":
            name = (request.POST.get("name") or "").strip()
            if name:
                try:
                    amt = Decimal(request.POST.get("amount") or "0")
                except InvalidOperation:
                    amt = Decimal(0)
                src_id = request.POST.get("source_fund") or None
                source = Department.objects.filter(pk=src_id).first() if src_id else None
                BudgetLine.objects.create(budget=budget, name=name,
                                          category=request.POST.get("category", ""),
                                          amount=amt, source_fund=source)
                messages.success(request, f"Added budget line \u201c{name}\u201d.")
        elif action == "delete":
            BudgetLine.objects.filter(pk=request.POST.get("line"), budget=budget).delete()
            messages.success(request, "Budget line removed.")
        elif action == "copy_prior":
            prior = Budget.objects.filter(year=year - 1, department=dept).first()
            if prior and prior.lines.exists():
                n = 0
                for ln in prior.lines.all():
                    BudgetLine.objects.create(budget=budget, name=ln.name,
                        category=ln.category, amount=ln.amount, source_fund=ln.source_fund)
                    n += 1
                messages.success(request, f"Copied {n} line(s) from {year - 1}. "
                                          f"Adjust the amounts as needed.")
            else:
                messages.info(request, f"No {year - 1} breakdown to copy from.")
        # keep the headline Budget amount in step with the breakdown
        budget.amount = budget.lines_total
        budget.save(update_fields=["amount"])
        return redirect(f"{reverse_lazy('budget_lines', args=[dept.id])}?year={year}")
