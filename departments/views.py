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
from django.db import transaction as db_tx
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


# ---------------------------------------------------------------------------
# Bulk fund + budget import (items 6 & 7)
# ---------------------------------------------------------------------------
import difflib
from .models import Budget, BudgetLine


def _norm_fund(s):
    """Normalise a fund name for matching: uppercase, spaces for separators,
    collapse whitespace."""
    s = (s or "").upper().replace("_", " ").replace("-", " ").replace("/", " ")
    return " ".join(s.split())


def _match_department(name, depts):
    """Return (department or None, score). Tries exact normalised match first,
    then a couple of known synonyms, then fuzzy."""
    n = _norm_fund(name)
    by_norm = {_norm_fund(d.name): d for d in depts}
    if n in by_norm:
        return by_norm[n], 1.0
    # common singular/plural and abbreviation synonyms seen in this church's data
    synonyms = {
        "PATHFINDER": "PATHFINDERS", "CHILDREN": "CHILDREN MINISTRY",
        "MASTER GUIDE": "MASTER GUIDE", "TRUST FUND": "TRUST FUND",
        "LOCAL CHURCH BUDGET": "LCB LOCAL CHURCH BUDGET",
        "ASAM": "ADVENTIST SINGLE ADULT MINISTRIES",
        "APM": "ADVENTIST POSSIBILITY MINISTRY",
    }
    if n in synonyms and _norm_fund(synonyms[n]) in by_norm:
        return by_norm[_norm_fund(synonyms[n])], 0.95
    close = difflib.get_close_matches(n, list(by_norm), n=1, cutoff=0.84)
    if close:
        return by_norm[close[0]], 0.85
    return None, 0.0


class BulkFundImportView(TreasurerRequiredMixin, View):
    """Two-step wizard: upload a fund/budget spreadsheet, review how each row maps
    to an existing department (creating new funds or sub-groups where needed),
    then apply — writing the per-year Budget and optional monthly breakdown.

    Step 1 (GET): show the upload form.
    Step 1 (POST file): parse, match each fund, stash a plan in the session, show
                        the review/mapping table with a prompt for unmatched funds.
    Step 2 (POST apply): create departments as chosen and write budgets.
    """
    template_name = "departments/bulk_import.html"

    MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
              "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

    def get(self, request):
        return render(request, self.template_name, {"stage": "upload"})

    def post(self, request):
        if request.POST.get("apply"):
            return self._apply(request)
        return self._parse(request)

    # ---- step 1: parse the file and build a mapping plan ----
    def _parse(self, request):
        import openpyxl
        f = request.FILES.get("file")
        if not f:
            messages.error(request, "Choose a spreadsheet to upload.")
            return redirect("bulk_fund_import")
        try:
            wb = openpyxl.load_workbook(f, data_only=True)
        except Exception:
            messages.error(request, "Could not read that file — please upload a .xlsx.")
            return redirect("bulk_fund_import")
        try:
            year = int(request.POST.get("year") or _dt.date.today().year)
        except (TypeError, ValueError):
            year = _dt.date.today().year

        ws = wb["DEPARTMENTS"] if "DEPARTMENTS" in wb.sheetnames else wb.active
        # find the header row + the columns we care about (NAME, B/F, projected,
        # and the monthly income block). The budget sheet has two header rows.
        rows = list(ws.iter_rows(values_only=True))
        name_col = bf_col = pinc_col = pexp_col = None
        header_idx = None
        for i, r in enumerate(rows[:5]):
            cells = [str(c).strip().upper() if c is not None else "" for c in r]
            if "NAME" in cells:
                header_idx = i
                name_col = cells.index("NAME")
                for label, attr in (("B/F", "bf"), ("PROJECTED INCOME", "pinc"),
                                    ("PROJECTED EXPENSES", "pexp")):
                    if label in cells:
                        if attr == "bf": bf_col = cells.index(label)
                        elif attr == "pinc": pinc_col = cells.index(label)
                        elif attr == "pexp": pexp_col = cells.index(label)
                break
        if name_col is None:
            messages.error(request, "Couldn't find a NAME column — is this the budget "
                                    "workbook? Expected a DEPARTMENTS sheet with NAME, "
                                    "B/F and PROJECTED INCOME columns.")
            return redirect("bulk_fund_import")

        # locate the monthly columns. The workbook has two JAN…DEC blocks: an
        # INCOME block then an EXPENSES block. A budget is planned *spend*, so we
        # use the EXPENSES block (the second JAN) to stay consistent with the
        # PROJECTED EXPENSES headline. Fall back to the first block if there's
        # only one.
        month_start = None
        for r in rows[:5]:
            cells = [str(c).strip().upper() if c is not None else "" for c in r]
            jans = [j for j, v in enumerate(cells) if v == "JAN"]
            if jans:
                month_start = jans[1] if len(jans) > 1 else jans[0]
                break

        depts = list(Department.objects.all())
        plan = []
        for r in rows[header_idx + 1:]:
            if name_col >= len(r):
                continue
            raw_name = r[name_col]
            if not raw_name or not str(raw_name).strip():
                continue
            name = str(raw_name).strip()
            def num(col):
                if col is None or col >= len(r) or r[col] in (None, ""):
                    return 0.0
                try:
                    return float(r[col])
                except (TypeError, ValueError):
                    return 0.0
            pinc = num(pinc_col)
            pexp = num(pexp_col)
            bf = num(bf_col)
            if pinc == 0 and pexp == 0 and bf == 0:
                continue  # skip empty filler rows
            months = []
            if month_start is not None:
                for k in range(12):
                    months.append(num(month_start + k))
            dept, score = _match_department(name, depts)
            plan.append({
                "name": name,
                "bf": bf, "pinc": pinc, "pexp": pexp,
                "months": months,
                "match_id": dept.id if dept else None,
                "match_name": dept.name if dept else None,
                "score": score,
            })

        request.session["bulk_fund_plan"] = {"year": year, "rows": plan}
        # candidate departments for the manual-map dropdowns
        candidates = [{"id": d.id, "name": str(d)} for d in
                      sorted(depts, key=lambda x: str(x))]
        matched = [p for p in plan if p["match_id"]]
        unmatched = [p for p in plan if not p["match_id"]]
        return render(request, self.template_name, {
            "stage": "review", "year": year, "plan": plan,
            "candidates": candidates, "matched_count": len(matched),
            "unmatched_count": len(unmatched),
            "fund_types": Department.FundType.choices,
            "categories": Department.Category.choices,
        })

    # ---- step 2: apply the reviewed plan ----
    @db_tx.atomic
    def _apply(self, request):
        data = request.session.get("bulk_fund_plan")
        if not data:
            messages.error(request, "Your import session expired — please upload again.")
            return redirect("bulk_fund_import")
        year = data["year"]
        plan = data["rows"]
        created_funds = budgets_written = lines_written = skipped = 0

        for i, p in enumerate(plan):
            choice = request.POST.get(f"map_{i}", "")
            dept = None
            if choice.startswith("dept:"):
                dept = Department.objects.filter(pk=choice.split(":", 1)[1]).first()
            elif choice == "create":
                ftype = request.POST.get(f"ftype_{i}") or Department.FundType.LOCAL
                cat = request.POST.get(f"cat_{i}") or Department.Category.MINISTRY
                parent_id = request.POST.get(f"parent_{i}") or None
                parent = Department.objects.filter(pk=parent_id).first() if parent_id else None
                dept, _was = Department.objects.get_or_create(
                    name=p["name"].upper().strip(),
                    defaults={"fund_type": ftype, "category": cat, "parent": parent})
                if _was:
                    created_funds += 1
            elif choice == "skip" or (choice == "" and not p["match_id"]):
                skipped += 1
                continue
            else:
                # default: use the auto-matched department if present
                if p["match_id"]:
                    dept = Department.objects.filter(pk=p["match_id"]).first()
            if not dept:
                skipped += 1
                continue

            # write the year budget (projected expenses is the planned spend)
            amount = Decimal(str(p["pexp"] or p["pinc"] or 0))
            budget, _ = Budget.objects.update_or_create(
                year=year, department=dept,
                defaults={"amount": amount, "note": "Imported from budget workbook"})
            budgets_written += 1

            # optional monthly breakdown lines (only if the user asked for it)
            if request.POST.get("with_months") and p.get("months"):
                budget.lines.filter(name__startswith="Budget — ").delete()
                for mi, mval in enumerate(p["months"]):
                    if mval:
                        BudgetLine.objects.create(
                            budget=budget,
                            name=f"Budget — {self.MONTHS[mi]}",
                            amount=Decimal(str(mval)))
                        lines_written += 1
                budget.amount = budget.lines_total
                budget.save(update_fields=["amount"])

        request.session.pop("bulk_fund_plan", None)
        parts = [f"{budgets_written} budget(s) written for {year}"]
        if created_funds:
            parts.append(f"{created_funds} new fund(s) created")
        if lines_written:
            parts.append(f"{lines_written} monthly line(s)")
        if skipped:
            parts.append(f"{skipped} row(s) skipped")
        messages.success(request, ", ".join(parts) + ".")
        return redirect("budget")
