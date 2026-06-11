"""Executive dashboard data: KPI cards and Chart.js series."""
import calendar
import datetime as dt
from decimal import Decimal

from django.db.models import Sum, Count, Q
from django.db.models.functions import TruncMonth

from giving.models import Transaction
from cashbook.models import Expense
from departments.models import Department
from reports.services import balances


def _f(v):
    return float(v or 0)


def _credits(**extra):
    return Transaction.objects.filter(direction=Transaction.Direction.CREDIT,
                                      confirmed=True, is_reversal=False,
                                      is_reversed=False, excluded_from_income=False,
                                      **extra)


def _debits(**extra):
    return Transaction.objects.filter(direction=Transaction.Direction.DEBIT,
                                      confirmed=True, is_reversal=False,
                                      is_reversed=False, **extra)


def cards():
    today = dt.date.today()
    month_start = today.replace(day=1)
    year = today.year
    from core.models import SiteConfig
    _cfg = SiteConfig.get()

    coll_month = (_credits(date__gte=month_start, date__lte=today)
                  .aggregate(t=Sum("amount"))["t"] or Decimal(0))
    coll_year = (_credits(date__year=year).aggregate(t=Sum("amount"))["t"] or Decimal(0))

    # Cash & bank balance = the cash-book balance (ties to the bank reconciliation):
    # opening cash position + all confirmed income (excluding double-counted lines)
    # − all approved/paid payments (expenses AND trust remittances reduce cash).
    income_all = _credits().aggregate(t=Sum("amount"))["t"] or Decimal(0)
    payments_all = (Expense.objects.filter(
        status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
        .aggregate(t=Sum("amount"))["t"] or Decimal(0))
    opening = (_cfg.opening_bank_balance + _cfg.opening_cash_on_hand
               - _cfg.opening_unremitted_trust)
    cash_balance = opening + income_all - payments_all

    trust_out = sum((r["to_remit"] for r in balances.trust_summary()), Decimal(0))

    pending = Expense.objects.filter(
        status__in=[Expense.Status.PENDING, Expense.Status.APPROVED])
    pending_total = pending.aggregate(t=Sum("amount"))["t"] or Decimal(0)

    unrec = Transaction.objects.filter(allocation_status=Transaction.Status.REVIEW).count()

    from cashbook.views import open_payables_total, open_accruals_total
    commitments = open_payables_total() + open_accruals_total()

    cards = [
        {"label": "Collections this month", "value": coll_month, "money": True, "tone": "ok"},
        {"label": "Collections (year to date)", "value": coll_year, "money": True, "tone": "ok"},
        {"label": "Cash & bank balance", "value": cash_balance, "money": True,
         "sub": "after expenses & remittances", "tone": "ok"},
        {"label": "Outstanding trust", "value": trust_out, "money": True,
         "tone": "warn" if trust_out > 0 else "ok"},
        {"label": "Pending expenses", "value": pending_total, "money": True,
         "sub": f"{pending.count()} item(s)", "tone": "warn" if pending.count() else "ok"},
        {"label": "Items in review", "value": unrec, "money": False,
         "tone": "warn" if unrec else "ok"},
    ]
    if commitments > 0:
        from django.urls import reverse
        cards.append({"label": "Payables & accruals", "value": commitments,
                      "money": True, "sub": "owed but unpaid", "tone": "warn",
                      "link": reverse("accruals")})
    return cards


def _monthly_series(qs, date_field="date", months=12, end=None):
    """Return (labels, values) of monthly sums for the trailing `months`."""
    end = end or dt.date.today()
    first = (end.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
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
           for row in qs.filter(**{f"{date_field}__gte": window_start})
           .annotate(mo=TruncMonth(date_field)).values("mo")
           .annotate(t=Sum("amount")).order_by("mo")}
    labels = [s.strftime("%b %y") for s in starts]
    values = [_f(agg.get(s, 0)) for s in starts]
    return labels, values


def charts():
    today = dt.date.today()
    year = today.year

    g_labels, g_values = _monthly_series(_credits(), months=12)
    inc_labels, inc_values = _monthly_series(_credits(date__year=year), months=12)
    eff = (Expense.objects.filter(status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
           .exclude(category=Expense.Category.REMITTANCE))
    exp_labels, exp_values = _monthly_series(eff, months=12)

    # department spending (top-level, current year, top 8)
    parent_of = {d.id: (d.parent_id or d.id) for d in Department.objects.all()}
    name_of = {d.id: d.name for d in Department.objects.all()}
    dept_agg = {}
    for r in (eff.filter(date__year=year).values("department_id")
              .annotate(t=Sum("amount"))):
        top = parent_of.get(r["department_id"], r["department_id"])
        dept_agg[top] = dept_agg.get(top, 0) + _f(r["t"])
    top_depts = sorted(dept_agg.items(), key=lambda x: -x[1])[:8]
    dep_labels = [name_of.get(i, "—") for i, _ in top_depts]
    dep_values = [v for _, v in top_depts]

    # trust outstanding by fund
    trust = [(r["department"].name, _f(r["to_remit"])) for r in balances.trust_summary()
             if r["to_remit"] and r["to_remit"] > 0]
    tr_labels = [n for n, _ in trust]
    tr_values = [v for _, v in trust]

    return {
        "giving_trend": {"labels": g_labels, "values": g_values},
        "monthly_income": {"labels": inc_labels, "values": inc_values},
        "monthly_expenses": {"labels": exp_labels, "values": exp_values},
        "department_spending": {"labels": dep_labels, "values": dep_values},
        "trust_balances": {"labels": tr_labels, "values": tr_values},
    }


def insights():
    """Richer executive analytics beyond the headline cards: month-on-month
    movement, giving concentration, channel mix, expense ratio, average gift,
    and the top givers — each returned as a small dict the template renders as
    a tile. All figures respect the trust/local split and exclude double-counted
    envelope detail."""
    import datetime as _dt
    from django.db.models import Sum, Count, Avg
    from giving.models import Transaction
    from cashbook.models import Expense

    def _money(v):
        return f"KSh {Decimal(v or 0):,.0f}"

    today = _dt.date.today()
    y = today.year
    m_start = today.replace(day=1)
    prev_end = m_start - _dt.timedelta(days=1)
    prev_start = prev_end.replace(day=1)

    def _credits(**f):
        return (Transaction.objects.confirmed_credits()
                .filter(excluded_from_income=False, **f))

    this_month = _credits(date__gte=m_start, date__lte=today).aggregate(
        t=Sum("amount"))["t"] or Decimal(0)
    last_month = _credits(date__gte=prev_start, date__lte=prev_end).aggregate(
        t=Sum("amount"))["t"] or Decimal(0)
    mom = None
    if last_month:
        mom = float((this_month - last_month) / last_month * 100)

    ytd = _credits(date__year=y).aggregate(t=Sum("amount"))["t"] or Decimal(0)
    ytd_exp = (Expense.objects.filter(
        status__in=[Expense.Status.APPROVED, Expense.Status.PAID], date__year=y)
        .exclude(category=Expense.Category.REMITTANCE)
        .aggregate(t=Sum("amount"))["t"] or Decimal(0))
    exp_ratio = float(ytd_exp / ytd * 100) if ytd else None

    # channel mix YTD (bank vs cash vs envelope-as-detail excluded)
    chan = {r["channel"]: r["t"] for r in _credits(date__year=y)
            .values("channel").annotate(t=Sum("amount"))}

    # average gift + giver count this year
    gifts = _credits(date__year=y).aggregate(n=Count("id"), avg=Avg("amount"))
    givers = (_credits(date__year=y).exclude(member__isnull=True)
              .values("member").distinct().count())

    # giving concentration: share from the top 10% of givers
    by_member = list(_credits(date__year=y).exclude(member__isnull=True)
                     .values("member").annotate(t=Sum("amount")).order_by("-t"))
    concentration = None
    if by_member:
        top_n = max(1, len(by_member) // 10)
        top_sum = sum((r["t"] for r in by_member[:top_n]), Decimal(0))
        total = sum((r["t"] for r in by_member), Decimal(0))
        if total:
            concentration = float(top_sum / total * 100)

    # privacy-safe participation: aggregates only, never names or per-person totals
    amounts = sorted(_credits(date__year=y).values_list("amount", flat=True))
    median = Decimal(0)
    if amounts:
        mid = len(amounts) // 2
        median = (amounts[mid] if len(amounts) % 2
                  else (amounts[mid - 1] + amounts[mid]) / 2)
    new_givers = (_credits(date__gte=m_start, date__lte=today)
                  .exclude(member__isnull=True)
                  .exclude(member__created_at__lt=m_start)
                  .values("member").distinct().count())

    tiles = [
        {"label": "This month vs last", "value": this_month, "money": True,
         "delta": mom, "sub": f"vs {_money(last_month)} last month"},
        {"label": "Expense ratio (YTD)", "pct": exp_ratio,
         "sub": f"{_money(ytd_exp)} spent of {_money(ytd)} received",
         "tone": "warn" if (exp_ratio or 0) > 80 else "ok"},
        {"label": "Average donation (YTD)", "value": gifts["avg"] or Decimal(0),
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
