"""Executive dashboard data: KPI cards and Chart.js series."""
import datetime as dt
from decimal import Decimal

from django.db.models import Sum, Count, Avg, Max
from django.db.models.functions import ExtractMonth, TruncMonth

from giving.models import Transaction
from cashbook.models import Expense
from departments.models import Department, current_cash_position
from reports.services import balances


def _f(v):
    return float(v or 0)


def _credits(**extra):
    # Consolidated: the income-credit basis lives in core.metrics; this wrapper
    # keeps the dashboard's existing call sites working unchanged.
    from core.metrics import income_credits
    return income_credits(**extra)


def _debits(**extra):
    return Transaction.objects.filter(direction=Transaction.Direction.DEBIT,
                                      confirmed=True, is_reversal=False,
                                      is_reversed=False, **extra)


def _period_bounds(start=None, end=None):
    today = dt.date.today()
    end = end or today
    start = start or end.replace(day=1)
    return start, end


def quick_facts(start=None, end=None):
    """Fast at-a-glance facts for the selected period — pure DB aggregates."""
    start, end = _period_bounds(start, end)
    facts = []

    top = (_credits(date__gte=start, date__lte=end).exclude(department__isnull=True)
           .values("department__name").annotate(t=Sum("amount")).order_by("-t").first())
    if top and top["department__name"]:
        facts.append({"label": "Top fund (period)", "text": top["department__name"],
                      "value": top["t"], "money": True})

    cat = (Expense.objects.filter(
                date__gte=start, date__lte=end,
                status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
           .values("category").annotate(t=Sum("amount")).order_by("-t").first())
    if cat:
        facts.append({"label": "Top spend category",
                      "text": dict(Expense.Category.choices).get(cat["category"], cat["category"]),
                      "value": cat["t"], "money": True})

    givers = (_credits(date__gte=start, date__lte=end)
              .exclude(member__isnull=True).values("member").distinct().count())
    facts.append({"label": "Givers in period", "text": f"{givers} contributor(s)",
                  "value": None, "money": False})

    big = (_credits(date__gte=start, date__lte=end).aggregate(m=Max("amount"))["m"])
    if big:
        facts.append({"label": "Largest single gift", "text": "",
                      "value": big, "money": True})
    return facts


def cards(start=None, end=None):
    start, end = _period_bounds(start, end)
    today = dt.date.today()
    year = end.year
    year_start = dt.date(year, 1, 1)

    # One pass for period + YTD collections.
    period_agg = _credits(date__gte=start, date__lte=end).aggregate(
        t=Sum("amount"), n=Count("id"), avg=Avg("amount"))
    coll_period = period_agg["t"] or Decimal(0)
    ytd_end = min(end, today) if end.year == today.year else end
    coll_year = (_credits(date__gte=year_start, date__lte=ytd_end)
                 .aggregate(t=Sum("amount"))["t"] or Decimal(0))

    cash_balance = current_cash_position()

    from core.metrics import metrics as _m
    trust_out = _m.trust_to_remit()

    pending_agg = Expense.objects.filter(
        status__in=[Expense.Status.PENDING, Expense.Status.APPROVED]
    ).aggregate(t=Sum("amount"), n=Count("id"))
    pending_total = pending_agg["t"] or Decimal(0)
    pending_n = pending_agg["n"] or 0

    unrec = Transaction.objects.filter(
        allocation_status=Transaction.Status.REVIEW).count()

    from cashbook.views import open_payables_total, open_accruals_total
    commitments = open_payables_total() + open_accruals_total()

    exp_period = (Expense.objects.filter(
        date__gte=start, date__lte=end,
        status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
        .exclude(doc_class=Expense.DocClass.LIABILITY)
        .aggregate(t=Sum("amount"))["t"] or Decimal(0))
    net = coll_period - exp_period

    cards = [
        {"label": "Collections (period)", "value": coll_period, "money": True, "tone": "ok"},
        {"label": "Collections (YTD)", "value": coll_year, "money": True, "tone": "ok"},
        {"label": "Expenses (period)", "value": exp_period, "money": True,
         "tone": "warn" if exp_period else "ok"},
        {"label": "Net (period)", "value": net, "money": True,
         "tone": "ok" if net >= 0 else "warn",
         "sub": "collections − expenses"},
        {"label": "Cash & bank", "value": cash_balance, "money": True, "tone": "ok"},
        {"label": "Outstanding trust", "value": trust_out, "money": True,
         "tone": "warn" if trust_out > 0 else "ok"},
        {"label": "Pending expenses", "value": pending_total, "money": True,
         "sub": f"{pending_n} item(s)", "tone": "warn" if pending_n else "ok"},
        {"label": "Items in review", "value": unrec, "money": False,
         "tone": "warn" if unrec else "ok"},
    ]
    if period_agg["n"]:
        cards.append({
            "label": "Avg gift (period)",
            "value": period_agg["avg"] or Decimal(0),
            "money": True, "tone": "ok",
            "sub": f"{period_agg['n']} donation(s)",
        })
    if commitments > 0:
        from django.urls import reverse
        cards.append({"label": "Payables & accruals", "value": commitments,
                      "money": True, "sub": "owed but unpaid", "tone": "warn",
                      "link": reverse("accruals")})
    return cards


def _monthly_series(qs, date_field="date", months=12, end=None):
    """Return (labels, values) of monthly sums for the trailing `months`."""
    end = end or dt.date.today()
    # build the window of month starts
    starts = []
    y, m = end.year, end.month
    for _ in range(months):
        starts.append(dt.date(y, m, 1))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    starts.reverse()
    window_start = starts[0]
    agg = {row["mo"].date() if hasattr(row["mo"], "date") else row["mo"]: row["t"]
           for row in qs.filter(**{f"{date_field}__gte": window_start,
                                   f"{date_field}__lte": end})
           .annotate(mo=TruncMonth(date_field)).values("mo")
           .annotate(t=Sum("amount")).order_by("mo")}
    labels = [s.strftime("%b %y") for s in starts]
    values = [_f(agg.get(s, 0)) for s in starts]
    return labels, values


def charts(start=None, end=None):
    start, end = _period_bounds(start, end)
    year = end.year

    g_labels, g_values = _monthly_series(_credits(), months=12, end=end)
    eff = (Expense.objects.filter(status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
           .exclude(doc_class=Expense.DocClass.LIABILITY))
    exp_labels, exp_values = _monthly_series(eff, months=12, end=end)
    inc_labels, inc_values = _monthly_series(
        _credits(date__year=year), months=12, end=end)

    # Receipts vs expenses for the chart year up to `end`.
    MN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    year_start = dt.date(year, 1, 1)
    mrec = {r["m"]: _f(r["t"]) for r in
            _credits(date__gte=year_start, date__lte=end)
            .annotate(m=ExtractMonth("date")).values("m").annotate(t=Sum("amount"))}
    mexp = {r["m"]: _f(r["t"]) for r in
            eff.filter(date__gte=year_start, date__lte=end)
            .annotate(m=ExtractMonth("date")).values("m").annotate(t=Sum("amount"))}
    last_m = end.month
    rve_labels = [MN[m - 1] for m in range(1, last_m + 1)]
    rve_receipts = [mrec.get(m, 0) for m in range(1, last_m + 1)]
    rve_expenses = [mexp.get(m, 0) for m in range(1, last_m + 1)]

    # department spending (top-level, period year, top 8) — one Department fetch
    depts = list(Department.objects.all().only("id", "name", "parent_id"))
    parent_of = {d.id: (d.parent_id or d.id) for d in depts}
    name_of = {d.id: d.name for d in depts}
    dept_agg = {}
    for r in (eff.filter(date__gte=start, date__lte=end).values("department_id")
              .annotate(t=Sum("amount"))):
        top = parent_of.get(r["department_id"], r["department_id"])
        dept_agg[top] = dept_agg.get(top, 0) + _f(r["t"])
    top_depts = sorted(dept_agg.items(), key=lambda x: -x[1])[:8]
    dep_labels = [name_of.get(i, "—") for i, _ in top_depts]
    dep_values = [v for _, v in top_depts]

    trust = [(r["department"].name, _f(r["to_remit"])) for r in balances.trust_summary()
             if r["to_remit"] and r["to_remit"] > 0]
    tr_labels = [n for n, _ in trust]
    tr_values = [v for _, v in trust]

    return {
        "giving_trend": {"labels": g_labels, "values": g_values},
        "monthly_income": {"labels": inc_labels, "values": inc_values},
        "monthly_expenses": {"labels": exp_labels, "values": exp_values},
        "receipts_vs_expenses_year": {"labels": rve_labels, "receipts": rve_receipts,
                                      "expenses": rve_expenses},
        "department_spending": {"labels": dep_labels, "values": dep_values},
        "trust_balances": {"labels": tr_labels, "values": tr_values},
    }


def insights(start=None, end=None):
    """Richer executive analytics for the selected period."""
    start, end = _period_bounds(start, end)
    today = end
    y = end.year
    m_start = end.replace(day=1)
    prev_end = m_start - dt.timedelta(days=1)
    prev_start = prev_end.replace(day=1)

    def _money(v):
        return f"KSh {Decimal(v or 0):,.0f}"

    def _cr(**f):
        return (Transaction.objects.confirmed_credits()
                .filter(excluded_from_income=False, **f))

    this_month = _cr(date__gte=m_start, date__lte=today).aggregate(
        t=Sum("amount"))["t"] or Decimal(0)
    last_month = _cr(date__gte=prev_start, date__lte=prev_end).aggregate(
        t=Sum("amount"))["t"] or Decimal(0)
    mom = None
    if last_month:
        mom = float((this_month - last_month) / last_month * 100)

    period_total = _cr(date__gte=start, date__lte=end).aggregate(
        t=Sum("amount"))["t"] or Decimal(0)
    period_exp = (Expense.objects.filter(
        status__in=[Expense.Status.APPROVED, Expense.Status.PAID],
        date__gte=start, date__lte=end)
        .exclude(doc_class=Expense.DocClass.LIABILITY)
        .aggregate(t=Sum("amount"))["t"] or Decimal(0))
    exp_ratio = float(period_exp / period_total * 100) if period_total else None

    chan = {r["channel"]: r["t"] for r in _cr(date__gte=start, date__lte=end)
            .values("channel").annotate(t=Sum("amount"))}

    gifts = _cr(date__gte=start, date__lte=end).aggregate(
        n=Count("id"), avg=Avg("amount"))
    givers = (_cr(date__gte=start, date__lte=end).exclude(member__isnull=True)
              .values("member").distinct().count())

    by_member = list(_cr(date__gte=start, date__lte=end).exclude(member__isnull=True)
                     .values("member").annotate(t=Sum("amount")).order_by("-t"))
    concentration = None
    if by_member:
        top_n = max(1, len(by_member) // 10)
        top_sum = sum((r["t"] for r in by_member[:top_n]), Decimal(0))
        total = sum((r["t"] for r in by_member), Decimal(0))
        if total:
            concentration = float(top_sum / total * 100)

    amounts = sorted(_cr(date__gte=start, date__lte=end).values_list("amount", flat=True))
    median = Decimal(0)
    if amounts:
        mid = len(amounts) // 2
        median = (amounts[mid] if len(amounts) % 2
                  else (amounts[mid - 1] + amounts[mid]) / 2)
    new_givers = (_cr(date__gte=m_start, date__lte=today)
                  .exclude(member__isnull=True)
                  .exclude(member__created_at__lt=m_start)
                  .values("member").distinct().count())

    tiles = [
        {"label": "This month vs last", "value": this_month, "money": True,
         "delta": mom, "sub": f"vs {_money(last_month)} last month"},
        {"label": "Expense ratio (period)", "pct": exp_ratio,
         "sub": f"{_money(period_exp)} spent of {_money(period_total)} received",
         "tone": "warn" if (exp_ratio or 0) > 80 else "ok"},
        {"label": "Average donation", "value": gifts["avg"] or Decimal(0),
         "money": True, "sub": f"{gifts['n'] or 0} donations from {givers} givers"},
        {"label": "Top-givers concentration", "pct": concentration,
         "sub": "share of giving from the top 10% of givers"
                if concentration is not None else "not enough member data"},
    ]
    return {
        "tiles": tiles,
        "channel_mix": {
            "bank": float(chan.get("BANK", 0) or 0),
            "cash": float(chan.get("CASH", 0) or 0),
            "envelope": float(chan.get("ENVELOPE", 0) or 0),
        },
        "participation": {
            "givers": givers,
            "gifts": gifts["n"] or 0,
            "median": median,
            "new_givers": new_givers,
        },
    }
