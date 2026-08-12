"""Budget items for a fund and year, with what has been spent against each.

ONE definition of "spent against this budget item" and therefore of what is
left on it. Two screens ask that question — the fund's Budget & goals page
(`FundBudgetView`) and the expense form's Budget item picker
(`BudgetItemsJSONView`) — and the second used to answer only half of it: it
returned each item's budgeted amount and nothing about the spend, so a
treasurer choosing "Catering — 30,000" had no way to see that 28,000 of it
was already committed.

Keeping the arithmetic here rather than in either view is the standing lesson
of this codebase: a rule implemented twice drifts, and the copy nobody tested
is the one that is wrong (#137b).
"""
from decimal import Decimal

from django.db.models import Sum

# What counts as spend against a budget item. PENDING deliberately does NOT:
# an unapproved claim has not committed the budget, and the fund-balance basis
# (reports.services.balances.fund_balance_parts) draws the line in the same
# place, so the two figures on the expense form agree about what "spent" means.
COUNTED_STATUSES = ("APPROVED", "PAID")


def budget_item_rows(dept, year):
    """[{line, budget, spent, remaining, pct}] for one fund and year, ordered
    by name. `remaining` may be negative — an item can be overspent, and
    hiding that would defeat the point of showing it."""
    from cashbook.models import BudgetLine, Expense

    lines = list(BudgetLine.objects
                 .filter(department_id=getattr(dept, "pk", dept), year=year)
                 .order_by("name"))
    if not lines:
        return []
    spend = {r["budget_line"]: r["t"] for r in (
        Expense.objects.filter(budget_line__in=lines,
                               status__in=COUNTED_STATUSES)
        .values("budget_line").annotate(t=Sum("amount")))}
    rows = []
    for b in lines:
        spent = spend.get(b.id) or Decimal(0)
        rows.append({
            "line": b,
            "budget": b.amount,
            "spent": spent,
            "remaining": b.amount - spent,
            # capped for a progress bar; 999 rather than unbounded so a wildly
            # overspent item can't stretch a layout
            "pct": int(min(spent / b.amount * 100, 999)) if b.amount else 0,
        })
    return rows


def untagged_spend(dept, year):
    """Spend on this fund in `year` tagged to no budget item, so a budget page
    never implies the item list accounts for everything."""
    from cashbook.models import Expense
    return (Expense.objects.filter(department_id=getattr(dept, "pk", dept),
                                   date__year=year, budget_line__isnull=True,
                                   status__in=COUNTED_STATUSES)
            .aggregate(t=Sum("amount"))["t"] or Decimal(0))
