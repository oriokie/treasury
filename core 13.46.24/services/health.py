"""Financial-health KPIs and anomaly detection.

KPIs summarise direction of travel; anomalies surface things a treasurer should
look at now. All are computed from live data with simple, explainable rules
(no opaque model) so the figures can always be traced back to the ledger.
"""
import calendar
import datetime as dt
from decimal import Decimal

from django.db.models import Sum, Q

from giving.models import Transaction
from cashbook.models import Expense
from departments.models import Department
from reports.services import balances, budget as budget_svc


def _month_bounds(year, month):
    last = calendar.monthrange(year, month)[1]
    return dt.date(year, month, 1), dt.date(year, month, last)


def _prev_month(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _credit_total(start, end):
    return (Transaction.objects.filter(direction=Transaction.Direction.CREDIT,
            confirmed=True, is_reversal=False, is_reversed=False,
            date__gte=start, date__lte=end)
            .aggregate(t=Sum("amount"))["t"] or Decimal(0))


def _expense_total(start, end, dept=None):
    f = Q(status__in=[Expense.Status.APPROVED, Expense.Status.PAID],
          date__gte=start, date__lte=end)
    if dept is not None:
        f &= Q(department=dept)
    return Expense.objects.filter(f).aggregate(t=Sum("amount"))["t"] or Decimal(0)


def _pct_change(now, before):
    if not before:
        return None
    return (now - before) / before * Decimal(100)


def kpis():
    today = dt.date.today()
    y, m = today.year, today.month
    cur_s, cur_e = _month_bounds(y, m)
    py, pm = _prev_month(y, m)
    prev_s, prev_e = _month_bounds(py, pm)

    inc_now, inc_prev = _credit_total(cur_s, cur_e), _credit_total(prev_s, prev_e)
    exp_now, exp_prev = _expense_total(cur_s, cur_e), _expense_total(prev_s, prev_e)
    net_now, net_prev = inc_now - exp_now, inc_prev - exp_prev

    # trailing 3-month average giving (excludes current month)
    tot, n = Decimal(0), 0
    yy, mm = py, pm
    for _ in range(3):
        s, e = _month_bounds(yy, mm)
        tot += _credit_total(s, e); n += 1
        yy, mm = _prev_month(yy, mm)
    avg_giving = (tot / n) if n else Decimal(0)

    # trust remittance compliance
    trust = balances.trust_summary()
    collected = sum((r["collected"] for r in trust), Decimal(0))
    remitted = sum((r["remitted"] for r in trust), Decimal(0))
    compliance = (remitted / collected * 100) if collected else Decimal(100)

    # budget compliance YTD (prorated to elapsed months)
    bva = budget_svc.budget_vs_actual(y, "ANNUAL")
    elapsed = Decimal(m) / Decimal(12)
    ytd_budget = sum((r["budget"] for r in bva["rows"]), Decimal(0)) * elapsed
    ytd_actual = _expense_total(dt.date(y, 1, 1), today)
    budget_use = (ytd_actual / ytd_budget * 100) if ytd_budget else None

    def trend(v):
        if v is None:
            return "flat"
        return "up" if v > 2 else ("down" if v < -2 else "flat")

    cf = _pct_change(net_now, net_prev)
    eg = _pct_change(exp_now, exp_prev)
    gt = _pct_change(inc_now, avg_giving)

    return [
        {"label": "Cash-flow trend", "trend": trend(cf),
         "detail": f"Net {net_now:,.0f} this month vs {net_prev:,.0f} last month",
         "pct": cf},
        {"label": "Expense growth", "trend": trend(eg),
         "detail": f"{exp_now:,.0f} this month vs {exp_prev:,.0f} last month", "pct": eg,
         "invert": True},
        {"label": "Giving trend", "trend": trend(gt),
         "detail": f"{inc_now:,.0f} this month vs {avg_giving:,.0f} recent average",
         "pct": gt},
        {"label": "Remittance compliance", "trend": "up" if compliance >= 95 else "down",
         "detail": f"{remitted:,.0f} of {collected:,.0f} trust collected remitted",
         "pct": compliance, "is_level": True},
        {"label": "Budget compliance",
         "trend": ("down" if (budget_use or 0) > 100 else "up"),
         "detail": (f"{ytd_actual:,.0f} spent of {ytd_budget:,.0f} budget to date"
                    if ytd_budget else "No budget set"),
         "pct": budget_use, "is_level": True, "invert": True},
    ]


def anomalies():
    """Return a list of {severity, title, detail}. severity: danger|warn|info."""
    today = dt.date.today()
    y, m = today.year, today.month
    cur_s, cur_e = _month_bounds(y, m)
    alerts = []

    # 1) Department overspending vs trailing 3-month average
    for dept in Department.objects.filter(active=True, is_trust=False, parent__isnull=True):
        this_m = _expense_total(cur_s, cur_e, dept)
        if this_m <= 0:
            continue
        tot, n, yy, mm = Decimal(0), 0, *_prev_month(y, m)
        for _ in range(3):
            s, e = _month_bounds(yy, mm)
            tot += _expense_total(s, e, dept); n += 1
            yy, mm = _prev_month(yy, mm)
        avg = (tot / n) if n else Decimal(0)
        if avg > 0 and this_m > avg * Decimal("1.5"):
            pct = (this_m / avg * 100)
            alerts.append({"severity": "warn",
                           "title": f"{dept.name} spending {pct:.0f}% of its monthly average",
                           "detail": f"{this_m:,.0f} this month vs {avg:,.0f} average."})

    # 2) Department over budget (YTD prorated)
    bva = budget_svc.budget_vs_actual(y, "ANNUAL")
    for r in bva["rows"]:
        if r["budget"] and r["actual"] > r["budget"]:
            over = (r["actual"] - r["budget"]) / r["budget"] * 100
            alerts.append({"severity": "danger",
                           "title": f"{r['department'].name} over budget by {over:.0f}%",
                           "detail": f"Spent {r['actual']:,.0f} of {r['budget']:,.0f} budgeted."})

    # 3) Missing / overdue remittances — measured from the LAST remittance, not
    #    the first-ever gift, so funds that are remitted up to date don't look
    #    months overdue. One consolidated alert instead of one per fund.
    from core.models import SiteConfig
    from cashbook.models import RemittanceBatch
    from django.urls import reverse
    import calendar
    due_day = SiteConfig.get().trust_remit_due_day or 15
    this_due_day = min(due_day, calendar.monthrange(y, m)[1])
    past_due = today.day > this_due_day
    trust = balances.trust_summary()
    total_out = sum((r["to_remit"] for r in trust
                     if r["to_remit"] and r["to_remit"] > 0), Decimal(0))
    funds_out = sum(1 for r in trust if r["to_remit"] and r["to_remit"] > 0)
    if total_out > 0:
        last_remit = (RemittanceBatch.objects.filter(
            status=RemittanceBatch.Status.REMITTED, period_end__isnull=False)
            .order_by("-period_end").first())
        since = last_remit.period_end if last_remit else None
        days = (today - since).days if since else None
        if past_due:
            alerts.append({"severity": "danger", "key": "trust-remittance-overdue",
                           "title": "Trust remittance overdue",
                           "detail": f"{total_out:,.0f} across {funds_out} fund(s) is not "
                                     f"yet remitted and the due date (day {due_day}) has passed.",
                           "link": reverse("remittance_dashboard"),
                           "link_text": "Prepare remittance"})
        elif days is not None and days >= 35:
            alerts.append({"severity": "warn", "key": "trust-outstanding",
                           "title": "Trust outstanding",
                           "detail": f"{total_out:,.0f} collected since the last remittance "
                                     f"({days} days ago) is not yet remitted.",
                           "link": reverse("remittance_dashboard"),
                           "link_text": "Prepare remittance"})
        elif days is None:
            alerts.append({"severity": "warn", "key": "trust-no-remittance",
                           "title": "No trust remittance recorded",
                           "detail": f"{total_out:,.0f} of trust collected has no remittance "
                                     f"on record yet.",
                           "link": reverse("remittance_dashboard"),
                           "link_text": "Prepare remittance"})

    # 4) Sudden giving drop vs trailing 3-month average
    inc_now = _credit_total(cur_s, cur_e)
    tot, n, yy, mm = Decimal(0), 0, *_prev_month(y, m)
    for _ in range(3):
        s, e = _month_bounds(yy, mm)
        tot += _credit_total(s, e); n += 1
        yy, mm = _prev_month(yy, mm)
    avg = (tot / n) if n else Decimal(0)
    if avg > 0 and inc_now < avg * Decimal("0.6"):
        drop = (1 - inc_now / avg) * 100
        alerts.append({"severity": "warn",
                       "title": f"Giving down {drop:.0f}% versus recent average",
                       "detail": f"{inc_now:,.0f} this month vs {avg:,.0f} average."})

    # 5) Unusual single expense (> 3x its fund's historical average)
    from django.db.models import Avg
    recent = list(Expense.objects.filter(date__gte=cur_s, date__lte=cur_e,
                                    status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
                  .select_related("department"))
    # fund averages computed once (not per row) — close enough for a >3x heuristic
    fund_avgs = {r["department"]: r["a"] for r in
                 Expense.objects.values("department").annotate(a=Avg("amount"))}
    for exp in recent:
        hist = fund_avgs.get(exp.department_id)
        if hist and exp.amount > hist * 3 and exp.amount > 1000:
            alerts.append({"severity": "info",
                           "title": f"Unusually large expense in {exp.department.name}",
                           "detail": f"{exp.amount:,.0f} for “{exp.description}” "
                                     f"(fund average {hist:,.0f})."})

    # 6) Possible duplicates
    from core.views import _duplicate_expenses, _duplicate_offerings
    de, do = _duplicate_expenses(), _duplicate_offerings()
    if de:
        alerts.append({"severity": "info",
                       "title": f"{len(de)} possible duplicate expense group(s)",
                       "detail": "Review them on the Controls page."})
    if do:
        alerts.append({"severity": "info",
                       "title": f"{len(do)} possible duplicate offering group(s)",
                       "detail": "Review them on the Controls page."})

    order = {"danger": 0, "warn": 1, "info": 2}
    alerts.sort(key=lambda a: order.get(a["severity"], 3))
    return alerts
