"""Month-by-month aggregates for the monthly collection/expense reports.

All values come from database aggregates (TruncMonth + Sum), never Python loops
over individual ledger rows.
"""
import calendar
from collections import defaultdict
from decimal import Decimal

from django.db.models import Sum
from django.db.models.functions import TruncMonth

from cashbook.models import Expense
from departments.models import Department
from giving.models import Transaction

MONTHS = [(m, calendar.month_abbr[m]) for m in range(1, 13)]


def _credit_month_map(year):
    """{department_id: {month: total}} of credits in the year."""
    qs = (Transaction.objects.confirmed_credits()
          .filter(date__year=year, excluded_from_income=False)
          .annotate(m=TruncMonth("date"))
          .values("department_id", "m")
          .annotate(t=Sum("amount")))
    out = defaultdict(lambda: defaultdict(Decimal))
    for r in qs:
        if r["department_id"] and r["m"]:
            out[r["department_id"]][r["m"].month] += r["t"] or Decimal(0)
    return out


def _expense_month_map(year):
    qs = (Expense.objects.exclude(category=Expense.Category.REMITTANCE).filter(
            date__year=year,
            status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
          .annotate(m=TruncMonth("date"))
          .values("department_id", "m")
          .annotate(t=Sum("amount")))
    out = defaultdict(lambda: defaultdict(Decimal))
    for r in qs:
        if r["department_id"] and r["m"]:
            out[r["department_id"]][r["m"].month] += r["t"] or Decimal(0)
    return out


def _rows_from_map(departments, month_map):
    """Build [{dept, cells[12], total}], plus column totals and grand total."""
    rows = []
    col_totals = {m: Decimal(0) for m, _ in MONTHS}
    grand = Decimal(0)
    for d in departments:
        cells = [month_map.get(d.id, {}).get(m, Decimal(0)) for m, _ in MONTHS]
        total = sum(cells, Decimal(0))
        if total == 0 and not any(cells):
            # still show the account, but it's all zero
            pass
        rows.append({"dept": d, "cells": cells, "total": total})
        for (m, _), v in zip(MONTHS, cells):
            col_totals[m] += v
        grand += total
    return rows, [col_totals[m] for m, _ in MONTHS], grand


def _ordered_departments(qs=None):
    """Departments ordered so each parent is followed by its sub-accounts."""
    qs = qs if qs is not None else Department.objects.all()
    by_parent = defaultdict(list)
    tops = []
    for d in qs:
        if d.parent_id:
            by_parent[d.parent_id].append(d)
        else:
            tops.append(d)
    ordered = []
    for t in tops:
        ordered.append(t)
        ordered.extend(sorted(by_parent.get(t.id, []), key=lambda x: x.name))
    # any sub whose parent isn't in the set
    placed = {d.id for d in ordered}
    for d in qs:
        if d.id not in placed:
            ordered.append(d)
    return ordered


def collections_by_account(year):
    depts = _ordered_departments(Department.objects.all())
    rows, col_totals, grand = _rows_from_map(depts, _credit_month_map(year))
    return {"months": MONTHS, "rows": rows, "col_totals": col_totals, "grand": grand}


def expenses_by_account(year):
    depts = _ordered_departments(Department.objects.all())
    rows, col_totals, grand = _rows_from_map(depts, _expense_month_map(year))
    return {"months": MONTHS, "rows": rows, "col_totals": col_totals, "grand": grand}


def trust_monthly(year):
    depts = _ordered_departments(Department.objects.filter(is_trust=True))
    rows, col_totals, grand = _rows_from_map(depts, _credit_month_map(year))
    return {"months": MONTHS, "rows": rows, "col_totals": col_totals, "grand": grand}


def collections_summary(year):
    """Per month: total collections, trust-fund collections, and expenditure.
    Mirrors the church's collection definition exactly — excludes rows flagged
    excluded_from_income (e.g. bank M-Pesa lines that are also receipted as
    envelopes), so the same contribution is never counted twice."""
    base = Transaction.objects.confirmed_credits().filter(excluded_from_income=False)
    credit = (base.filter(date__year=year)
              .annotate(m=TruncMonth("date")).values("m")
              .annotate(t=Sum("amount")))
    trust = (base.filter(date__year=year, department__is_trust=True)
             .annotate(m=TruncMonth("date")).values("m")
             .annotate(t=Sum("amount")))
    exp = (Expense.objects.exclude(category=Expense.Category.REMITTANCE).filter(
                date__year=year,
                status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
           .annotate(m=TruncMonth("date")).values("m")
           .annotate(t=Sum("amount")))

    def to_map(qs):
        return {r["m"].month: (r["t"] or Decimal(0)) for r in qs if r["m"]}

    cm, tm, em = to_map(credit), to_map(trust), to_map(exp)
    rows = []
    tot_c = tot_t = tot_e = Decimal(0)
    for m, label in MONTHS:
        c, t, e = cm.get(m, Decimal(0)), tm.get(m, Decimal(0)), em.get(m, Decimal(0))
        rows.append({"month": label, "collections": c, "trust": t,
                     "local": c - t, "expenditure": e, "net": c - e})
        tot_c += c; tot_t += t; tot_e += e
    return {"rows": rows, "tot_collections": tot_c, "tot_trust": tot_t,
            "tot_local": tot_c - tot_t, "tot_expenditure": tot_e,
            "tot_net": tot_c - tot_e}


def collections_detail(start, end):
    """Detailed collections for a given period, broken down by fund.

    Uses exactly the same definition as collections_summary() — confirmed
    credits with excluded_from_income=False — so the grand total reconciles to
    that report's Collections figure for the same dates. Trust funds are those
    flagged is_trust; everything else (local funds and any unallocated credit)
    is Local, mirroring the summary's `local = collections - trust`.

    Returns per-fund rows plus the period's trust/local/collections totals and
    the matching expenditure and net, so the page can show the same headline
    figures as the summary alongside the breakdown.
    """
    from django.db.models import Count, Sum as _Sum
    base = (Transaction.objects.confirmed_credits()
            .filter(excluded_from_income=False, date__gte=start, date__lte=end))
    agg = (base.values("department", "department__name", "department__is_trust")
           .annotate(amount=_Sum("amount"), n=Count("id")))
    rows = []
    for r in agg:
        amt = r["amount"] or Decimal(0)
        is_trust = bool(r["department__is_trust"])
        rows.append({
            "fund": r["department__name"] or "(Unallocated)",
            "is_trust": is_trust,
            "type": "Trust" if is_trust else "Local",
            "n": r["n"],
            "amount": amt,
        })
    # trust funds first, then local, each by amount descending
    rows.sort(key=lambda x: (not x["is_trust"], -x["amount"]))
    tot_trust = sum((x["amount"] for x in rows if x["is_trust"]), Decimal(0))
    tot_collections = sum((x["amount"] for x in rows), Decimal(0))
    tot_local = tot_collections - tot_trust
    exp = (Expense.objects.exclude(category=Expense.Category.REMITTANCE)
           .filter(date__gte=start, date__lte=end,
                   status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
           .aggregate(t=Sum("amount"))["t"] or Decimal(0))
    return {
        "rows": rows,
        "n_funds": len(rows),
        "n_receipts": sum(x["n"] for x in rows),
        "tot_trust": tot_trust,
        "tot_local": tot_local,
        "tot_collections": tot_collections,
        "tot_expenditure": exp,
        "tot_net": tot_collections - exp,
    }
