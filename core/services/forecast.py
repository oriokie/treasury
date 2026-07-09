"""Forward-looking cash-flow projection.

Existing reports are historical; this projects the cash position forward over a
horizon (30 days / quarter / year) from three drivers:

* historical giving  — an average run-rate from the last six months of income;
* recurring expenses — the actual upcoming due dates of recurring schedules,
  plus a discretionary spend run-rate for everything else;
* pledges            — outstanding pledge installments expected to fall due.

It is deliberately transparent (every component is returned) and clearly
indicative — real giving and spending vary month to month.
"""
import datetime as dt
from decimal import Decimal

from django.db.models import Sum

from giving.models import Transaction
from cashbook.models import Expense, RecurringExpense


def _credits(**extra):
    return Transaction.objects.filter(direction=Transaction.Direction.CREDIT,
        confirmed=True, is_reversal=False, is_reversed=False,
        excluded_from_income=False, **extra)


def _effective_expenses(**extra):
    return (Expense.objects.filter(
        status__in=[Expense.Status.APPROVED, Expense.Status.PAID], **extra)
        .exclude(doc_class=Expense.DocClass.LIABILITY))


def cash_now():
    """Current cash & bank position — the same figure the Statement of
    Financial Position shows as "cash", computed the same way."""
    from departments.models import current_cash_position
    return current_cash_position()


def _monthly_equiv(sched):
    """Approximate monthly cost of a recurring schedule, for the run-rate split."""
    f = sched.frequency
    a = sched.amount or Decimal(0)
    if f == RecurringExpense.Frequency.SABBATH:
        return a * Decimal("4.345")          # ~weeks per month
    if f == RecurringExpense.Frequency.MONTHLY:
        return a
    if f == RecurringExpense.Frequency.QUARTERLY:
        return a / 3
    if f == RecurringExpense.Frequency.YEARLY:
        return a / 12
    return a


def _recurring_due(today, end):
    """Sum of recurring-expense amounts whose due dates fall in (today, end]."""
    from cashbook.services.recurring import due_dates
    total = Decimal(0)
    for s in RecurringExpense.objects.filter(active=True):
        for d in due_dates(s, end):
            if today < d <= end:
                total += s.amount or Decimal(0)
    return total


def _pledge_inflow(today, end):
    """Outstanding pledge installments expected to fall due in (today, end],
    capped at each pledge's remaining balance (campaign giving, treated as
    over-and-above the regular run-rate)."""
    from pledges.models import Pledge
    total = Decimal(0)
    actives = Pledge.objects.filter(status=Pledge.Status.ACTIVE)
    for p in actives:
        remaining = p.outstanding
        if remaining <= 0:
            continue
        due = sum((amt for d, amt in p.expected_installments() if today < d <= end),
                  Decimal(0))
        total += min(due, remaining)
    return total


def project(days):
    """Project the cash position `days` ahead. Returns a breakdown dict."""
    today = dt.date.today()
    end = today + dt.timedelta(days=days)
    months = Decimal(days) / Decimal(30)

    opening = cash_now()

    # giving run-rate from the last 6 months
    six_ago = today - dt.timedelta(days=182)
    giving_6mo = _credits(date__gte=six_ago, date__lte=today).aggregate(
        t=Sum("amount"))["t"] or Decimal(0)
    giving_monthly = giving_6mo / 6
    proj_giving = (giving_monthly * months).quantize(Decimal("0.01"))

    # pledges expected in window (campaign giving on top of the run-rate)
    pledge_in = _pledge_inflow(today, end).quantize(Decimal("0.01"))

    # spending: precise upcoming recurring + a discretionary run-rate for the rest
    spend_6mo = _effective_expenses(date__gte=six_ago, date__lte=today).aggregate(
        t=Sum("amount"))["t"] or Decimal(0)
    spend_monthly = spend_6mo / 6
    recurring_monthly = sum((_monthly_equiv(s) for s in
                             RecurringExpense.objects.filter(active=True)), Decimal(0))
    discretionary_monthly = max(Decimal(0), spend_monthly - recurring_monthly)
    recurring_in_window = _recurring_due(today, end).quantize(Decimal("0.01"))
    proj_discretionary = (discretionary_monthly * months).quantize(Decimal("0.01"))
    proj_spend = recurring_in_window + proj_discretionary

    projected = opening + proj_giving + pledge_in - proj_spend

    return {
        "days": days, "as_of": today, "horizon_end": end,
        "opening": opening,
        "proj_giving": proj_giving,
        "pledge_in": pledge_in,
        "recurring": recurring_in_window,
        "discretionary": proj_discretionary,
        "proj_spend": proj_spend,
        "projected": projected,
        "net": projected - opening,
    }


def horizons():
    """The three standard horizons for the forecast page/cards."""
    return {"30 days": project(30), "Quarter": project(91), "Year": project(365)}
