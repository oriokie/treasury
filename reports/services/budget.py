"""Budget vs actual: year-scoped budgets, period proration, variance.

Variance convention (treasurer-friendly):
  variance   = budget − actual     (positive = under budget / remaining;
                                     negative = overspent)
  variance % = variance / budget × 100
"""
import calendar
import datetime as dt
from decimal import Decimal

from django.db.models import Sum, Q

from departments.models import Department, Budget, BudgetLine
from cashbook.models import Expense


def budget_amount(year, dept):
    """Annual budget for a fund in a year. When the year-scoped Budget has
    breakdown lines, the budget is the sum of those lines; otherwise the Budget's
    own amount, falling back to the legacy Department.annual_budget."""
    b = Budget.objects.filter(year=year, department=dept).first()
    if b is not None:
        lines_total = b.lines_total
        if lines_total:
            return lines_total
        return b.amount or Decimal(0)
    return dept.annual_budget or Decimal(0)


def budget_amounts_bulk(year, depts):
    """Same computation as budget_amount(), for many departments at once — a
    small constant number of queries instead of one (plus a second for
    lines_total) per department. Used wherever a budget figure is needed
    alongside every fund at once (e.g. the executive overview's spend-by-fund
    breakdown), which previously queried once per distinct top-level fund.
    Returns {department_id: amount}."""
    from django.db.models import Sum
    depts = list(depts)
    dept_ids = [d.id for d in depts]
    budgets = {b.department_id: b for b in
               Budget.objects.filter(year=year, department_id__in=dept_ids)}
    budget_ids = [b.id for b in budgets.values()]
    lines_sum = {}
    if budget_ids:
        for row in (BudgetLine.objects.filter(budget_id__in=budget_ids)
                    .values("budget_id").annotate(t=Sum("amount"))):
            lines_sum[row["budget_id"]] = row["t"] or Decimal(0)
    out = {}
    for d in depts:
        b = budgets.get(d.id)
        if b is not None:
            lt = lines_sum.get(b.id, Decimal(0))
            out[d.id] = lt if lt else (b.amount or Decimal(0))
        else:
            out[d.id] = d.annual_budget or Decimal(0)
    return out


def period_range(year, period="ANNUAL", month=None, quarter=None):
    """Return (start, end, fraction_of_year, label) for the chosen period."""
    period = (period or "ANNUAL").upper()
    if period == "MONTH":
        m = int(month or dt.date.today().month)
        last = calendar.monthrange(year, m)[1]
        return (dt.date(year, m, 1), dt.date(year, m, last),
                Decimal(1) / Decimal(12), dt.date(year, m, 1).strftime("%B %Y"))
    if period == "QUARTER":
        q = int(quarter or ((dt.date.today().month - 1) // 3 + 1))
        start_m = (q - 1) * 3 + 1
        end_m = start_m + 2
        last = calendar.monthrange(year, end_m)[1]
        return (dt.date(year, start_m, 1), dt.date(year, end_m, last),
                Decimal(1) / Decimal(4), f"Q{q} {year}")
    return (dt.date(year, 1, 1), dt.date(year, 12, 31), Decimal(1), str(year))


def _actuals_by_top_department(start, end):
    """Approved/paid expense totals rolled up to the top-level fund."""
    eff = Q(status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
    base = Expense.objects.filter(eff, date__gte=start, date__lte=end)
    parent_of = {}
    for d in Department.objects.select_related("parent"):
        parent_of[d.id] = (d.parent_id or d.id)
    agg = {}
    for r in base.values("department_id").annotate(total=Sum("amount")):
        top = parent_of.get(r["department_id"], r["department_id"])
        agg[top] = agg.get(top, Decimal(0)) + (r["total"] or Decimal(0))
    return agg


def budget_vs_actual(year, period="ANNUAL", month=None, quarter=None):
    """Per top-level fund: budget (prorated to the period), actual, variance, %.
    Only funds that can hold expenses (non-trust) and have a budget or spend."""
    start, end, frac, label = period_range(year, period, month, quarter)
    actuals = _actuals_by_top_department(start, end)
    top_depts = list(Department.objects.filter(active=True, parent__isnull=True,
                                                is_trust=False))
    annual_by_dept = budget_amounts_bulk(year, top_depts)
    rows = []
    tot_b = tot_a = Decimal(0)
    for dept in top_depts:
        annual = annual_by_dept.get(dept.id, Decimal(0))
        budget_period = (annual * frac).quantize(Decimal("0.01"))
        actual = actuals.get(dept.id, Decimal(0))
        if budget_period == 0 and actual == 0:
            continue
        variance = budget_period - actual
        pct = (variance / budget_period * 100) if budget_period else None
        rows.append({
            "department": dept, "budget": budget_period, "actual": actual,
            "variance": variance, "variance_pct": pct,
            "over": variance < 0,
        })
        tot_b += budget_period
        tot_a += actual
    rows.sort(key=lambda r: r["actual"], reverse=True)
    tot_var = tot_b - tot_a
    totals = {"budget": tot_b, "actual": tot_a, "variance": tot_var,
              "variance_pct": (tot_var / tot_b * 100) if tot_b else None}
    return {"rows": rows, "totals": totals, "label": label,
            "start": start, "end": end, "period": (period or "ANNUAL").upper()}


def line_actuals(budget, start, end):
    """Per BudgetLine actuals (only meaningful where a category is set)."""
    eff = Q(status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
    out = []
    for ln in budget.lines.all():
        actual = Decimal(0)
        if ln.category:
            actual = (Expense.objects.filter(
                eff, department=budget.department, category=ln.category,
                date__gte=start, date__lte=end).aggregate(t=Sum("amount"))["t"] or Decimal(0))
        out.append({"line": ln, "actual": actual,
                    "variance": (ln.amount or Decimal(0)) - actual})
    return out


def board_budget(year):
    """Board-facing budget summary for a year: each department's planned budget
    split by source of funds (own / Local Church Budget / other), the church-wide
    totals, and the per-department amounts the Local Church Budget is expected to
    incur (departmental allocations). Prior-year totals are included for pegging."""
    from departments.models import Budget, lcb_fund
    lcb = lcb_fund()
    budgets = (Budget.objects.filter(year=year)
               .select_related("department").prefetch_related("lines__source_fund"))
    py = {b.department_id: b.lines_total
          for b in Budget.objects.filter(year=year - 1).prefetch_related("lines")}
    rows, lcb_alloc = [], []
    tot = {"budget": Decimal(0), "own": Decimal(0), "lcb": Decimal(0),
           "other": Decimal(0), "prior": Decimal(0)}
    for b in budgets:
        own = lcbv = other = Decimal(0)
        lines = list(b.lines.all())
        for ln in lines:
            k = ln.source_kind
            if k == "LCB":
                lcbv += ln.amount
            elif k == "OTHER":
                other += ln.amount
            else:
                own += ln.amount
        total = own + lcbv + other
        if not lines and b.amount:        # headline amount with no breakdown -> own funds
            total = b.amount
            own = b.amount
        if total == 0:
            continue
        prior = py.get(b.department_id, Decimal(0))
        rows.append({"dept": b.department, "total": total, "own": own, "lcb": lcbv,
                     "other": other, "prior": prior, "is_trust": b.department.is_trust})
        tot["budget"] += total; tot["own"] += own; tot["lcb"] += lcbv
        tot["other"] += other; tot["prior"] += prior
        if lcbv:
            lcb_alloc.append({"dept": b.department, "amount": lcbv})
    rows.sort(key=lambda r: (-r["total"], r["dept"].name))
    lcb_alloc.sort(key=lambda r: -r["amount"])
    return {"rows": rows, "totals": tot, "lcb_alloc": lcb_alloc,
            "lcb_fund": lcb, "year": year}
