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

def _credits():
    """Confirmed credits on the report's basis — see reports.services.asat."""
    from reports.services import asat
    return asat.credits()


def _exp():
    """Expense rows on the report's basis — see reports.services.asat."""
    from reports.services import asat
    return asat.expenses()


MONTHS = [(m, calendar.month_abbr[m]) for m in range(1, 13)]


def _credit_month_map(year):
    """{department_id: {month: total}} of credits in the year."""
    qs = (_credits()
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
    qs = (_exp().exclude(doc_class=Expense.DocClass.LIABILITY).filter(
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
    base = _credits().filter(excluded_from_income=False)
    credit = (base.filter(date__year=year)
              .annotate(m=TruncMonth("date")).values("m")
              .annotate(t=Sum("amount")))
    trust = (base.filter(date__year=year, department__is_trust=True)
             .annotate(m=TruncMonth("date")).values("m")
             .annotate(t=Sum("amount")))
    exp = (_exp().exclude(doc_class=Expense.DocClass.LIABILITY).filter(
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


def month_buckets(start, end):
    """The calendar months spanned by [start, end], each clipped to the period.

    Returns ``[{"key": (year, month), "label": "Jan 2026", "short": "Jan",
    "start": date, "end": date}]``. A period inside one calendar month yields a
    single bucket, which is what makes the board pack's collection and trust
    tables read as "the month" for a one-month period and "month by month" for
    a longer one, from the same code.
    """
    import datetime as _dt
    if not start or not end or end < start:
        return []
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        first = _dt.date(y, m, 1)
        last = _dt.date(y, m, calendar.monthrange(y, m)[1])
        out.append({
            "key": (y, m),
            "label": first.strftime("%b %Y"),
            "short": calendar.month_abbr[m],
            "start": max(first, start),
            "end": min(last, end),
        })
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _month_key(d):
    return (d.year, d.month)


def collections_summary_period(start, end):
    """Collections, trust, local and expenditure per calendar month over any
    period — the period-aware form of :func:`collections_summary`.

    Expenditure is what the church SPENT: effective expenses excluding
    liability documents. A liability document settles an obligation rather than
    buying anything — a trust remittance hands the conference money that was
    never the church's, and the contra raised when a loan is converted to a
    donation retires the debt against the gift without a shilling moving. Both
    would otherwise land here as spending the church never did, which is what
    made this table disagree with the Income & Expenditure statement (whose
    ``_effective_expense_qs`` has always excluded the whole liability class).

    The credit side is the same basis the Collections Summary report uses —
    confirmed credits with ``excluded_from_income=False``, trust being credits
    on a trust fund — so the two can never disagree for the same dates.
    """
    buckets = month_buckets(start, end)
    if not buckets:
        return {"rows": [], "totals": {}, "multi_month": False}

    base = (_credits()
            .filter(excluded_from_income=False, date__gte=start, date__lte=end))
    credit = (base.annotate(m=TruncMonth("date")).values("m")
              .annotate(t=Sum("amount")))
    trust = (base.filter(department__is_trust=True)
             .annotate(m=TruncMonth("date")).values("m")
             .annotate(t=Sum("amount")))
    exp = (_exp().exclude(doc_class=Expense.DocClass.LIABILITY)
           .filter(date__gte=start, date__lte=end,
                   status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
           .annotate(m=TruncMonth("date")).values("m")
           .annotate(t=Sum("amount")))

    def to_map(qs):
        return {_month_key(r["m"]): (r["t"] or Decimal(0)) for r in qs if r["m"]}

    cm, tm, em = to_map(credit), to_map(trust), to_map(exp)
    rows = []
    tot_c = tot_t = tot_e = Decimal(0)
    for b in buckets:
        c = cm.get(b["key"], Decimal(0))
        t = tm.get(b["key"], Decimal(0))
        e = em.get(b["key"], Decimal(0))
        rows.append({"label": b["label"], "start": b["start"], "end": b["end"],
                     "collections": c, "trust": t, "local": c - t,
                     "expenditure": e, "net": c - e})
        tot_c += c
        tot_t += t
        tot_e += e
    return {
        "rows": rows,
        "multi_month": len(buckets) > 1,
        "totals": {"collections": tot_c, "trust": tot_t, "local": tot_c - tot_t,
                   "expenditure": tot_e, "net": tot_c - tot_e},
    }


def trust_monthly_period(start, end):
    """Trust-fund collections per trust account per calendar month over any
    period — the period-aware form of :func:`trust_monthly`.

    Same credit basis as :func:`collections_summary_period`, so the trust
    column there equals this table's grand total for the same dates. Funds with
    no collection in the period are dropped: a board pack listing forty empty
    trust accounts is a table nobody reads.
    """
    buckets = month_buckets(start, end)
    if not buckets:
        return {"months": [], "rows": [], "col_totals": [], "grand": Decimal(0)}

    qs = (_credits()
          .filter(excluded_from_income=False, department__is_trust=True,
                  date__gte=start, date__lte=end)
          .annotate(m=TruncMonth("date"))
          .values("department_id", "m")
          .annotate(t=Sum("amount")))
    by_dept = defaultdict(lambda: defaultdict(Decimal))
    for r in qs:
        if r["department_id"] and r["m"]:
            by_dept[r["department_id"]][_month_key(r["m"])] += r["t"] or Decimal(0)

    depts = _ordered_departments(
        Department.objects.filter(is_trust=True, id__in=list(by_dept)))
    rows = []
    col_totals = {b["key"]: Decimal(0) for b in buckets}
    grand = Decimal(0)
    for d in depts:
        cells = [by_dept[d.id].get(b["key"], Decimal(0)) for b in buckets]
        total = sum(cells, Decimal(0))
        if not total:
            continue
        rows.append({"dept": d, "cells": cells, "total": total})
        for b, v in zip(buckets, cells):
            col_totals[b["key"]] += v
        grand += total
    rows.sort(key=lambda r: -r["total"])
    return {"months": buckets, "rows": rows,
            "col_totals": [col_totals[b["key"]] for b in buckets],
            "grand": grand}


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
    base = (_credits()
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
    exp = (_exp().exclude(doc_class=Expense.DocClass.LIABILITY)
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
