"""Data for the comprehensive monthly Treasurer's Report.

Assembles the sections a board expects each month — collections, trust and LCB
trends, a multi-year trend, expense and local-fund breakdowns — reusing the
shared balances helpers so every figure ties to the rest of the system.
"""
import calendar
import datetime as dt
from decimal import Decimal

from django.db.models import Sum

from giving.models import Transaction
from cashbook.models import Expense
from departments.models import Department, lcb_fund
from reports.services import balances

PAID = [Expense.Status.APPROVED, Expense.Status.PAID]


def month_bounds(d):
    start = d.replace(day=1)
    end = d.replace(day=calendar.monthrange(d.year, d.month)[1])
    return start, end


def months_back(as_of, n):
    """Last `n` months ending in as_of's month, oldest first."""
    y, m = as_of.year, as_of.month
    seq = []
    for _ in range(n):
        seq.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    out = []
    for (yy, mm) in reversed(seq):
        s = dt.date(yy, mm, 1)
        e = dt.date(yy, mm, calendar.monthrange(yy, mm)[1])
        out.append({"year": yy, "month": mm, "start": s, "end": e,
                    "label": s.strftime("%b %y")})
    return out


def collections_summary(s, e):
    rows = balances.department_summary(s, e)
    total = sum((r["receipts"] for r in rows), Decimal(0))
    trust = sum((r["receipts"] for r in rows if r["is_trust"]), Decimal(0))
    return {"total": total, "trust": trust, "local": total - trust, "rows": rows}


def trust_receipted_trend(as_of, months=4):
    """Per trust fund, RECEIPTED receipts in each of the last `months` months."""
    cols = months_back(as_of, months)
    per_month = [balances.receipts_by_department(c["start"], c["end"], receipted=True)
                 for c in cols]
    rows = []
    for d in Department.objects.filter(fund_type=Department.FundType.TRUST,
                                       active=True).order_by("name"):
        cells = [pm.get(d.id, Decimal(0)) for pm in per_month]
        if any(cells):
            rows.append({"dept": d, "cells": cells, "total": sum(cells, Decimal(0))})
    col_totals = [sum((r["cells"][i] for r in rows), Decimal(0))
                  for i in range(len(cols))]
    return {"columns": cols, "rows": rows, "col_totals": col_totals}


def lcb_subaccount_trend(as_of, months=4):
    """LCB and its sub-accounts, receipts each of the last `months` months."""
    lcb = lcb_fund()
    cols = months_back(as_of, months)
    per_month = [balances.receipts_by_department(c["start"], c["end"]) for c in cols]
    rows = []
    if lcb:
        targets = [lcb] + list(lcb.subgroups.all().order_by("name"))
        for d in targets:
            cells = [pm.get(d.id, Decimal(0)) for pm in per_month]
            if any(cells):
                rows.append({"dept": d, "cells": cells,
                             "total": sum(cells, Decimal(0)),
                             "is_parent": d.id == lcb.id})
    col_totals = [sum((r["cells"][i] for r in rows), Decimal(0))
                  for i in range(len(cols))]
    return {"lcb": lcb, "columns": cols, "rows": rows, "col_totals": col_totals}


def _trust_dept_ids():
    return list(Department.objects.filter(
        fund_type=Department.FundType.TRUST).values_list("id", flat=True))


def yearly_trend(as_of, years=5):
    """Collections, trust and expenses for each of the last `years` years, each
    measured from year-start to the current month (like-for-like YTD). Live
    actuals are used where present; otherwise prior-year monthly history fills in."""
    from core.models import HistoricalMonth
    trust_ids = _trust_dept_ids()
    out = []
    for i in range(years - 1, -1, -1):
        yr = as_of.year - i
        ystart = dt.date(yr, 1, 1)
        last_day = calendar.monthrange(yr, as_of.month)[1]
        yend = dt.date(yr, as_of.month, last_day)
        credits = Transaction.objects.confirmed_credits().filter(
            date__gte=ystart, date__lte=yend, excluded_from_income=False)
        collection = credits.aggregate(t=Sum("amount"))["t"] or Decimal(0)
        trust = (credits.filter(department_id__in=trust_ids)
                 .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        expense = (Expense.objects.filter(date__gte=ystart, date__lte=yend, status__in=PAID)
                   .exclude(category=Expense.Category.REMITTANCE)
                   .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        source = "actual"
        if collection == 0:
            hm = HistoricalMonth.objects.filter(year=yr, month__lte=as_of.month)
            if hm.exists():
                agg = hm.aggregate(c=Sum("collection"), t=Sum("trust_fund"),
                                   e=Sum("expenditure"))
                collection = agg["c"] or Decimal(0)
                trust = agg["t"] or Decimal(0)
                expense = agg["e"] or Decimal(0)
                source = "history"
        out.append({"year": yr, "collection": collection, "trust": trust,
                    "expense": expense, "local": collection - trust, "source": source})
    return out


def lcb_expense_categories(s, e):
    """Categories of LCB spend for the month with totals (largest first)."""
    lcb = lcb_fund()
    if not lcb:
        return []
    ids = [lcb.id] + list(lcb.subgroups.values_list("id", flat=True))
    qs = (Expense.objects.filter(date__gte=s, date__lte=e, status__in=PAID,
                                 department_id__in=ids)
          .exclude(category=Expense.Category.REMITTANCE)
          .values("category").annotate(t=Sum("amount")).order_by("-t"))
    label = dict(Expense.Category.choices)
    return [{"label": label.get(r["category"], r["category"]), "total": r["t"]}
            for r in qs if r["t"]]


def local_funds_breakdown(rows):
    """Local funds with activity this period, sorted by receipts (largest first)."""
    locals_ = [r for r in rows if not r["is_trust"]
               and ((r["receipts"] or 0) or (r["closing"] or 0))]
    locals_.sort(key=lambda r: (r["receipts"] or Decimal(0)), reverse=True)
    return locals_


def recent_reconciliation(as_of):
    """The most recent bank reconciliation on or before the month end."""
    from statements.models import BankReconciliation
    return (BankReconciliation.objects.filter(statement_date__lte=as_of)
            .order_by("-statement_date").first())
