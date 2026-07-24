"""Turn recurring-expense schedules into actual Expense records on their due dates.

Generation is idempotent (it never creates a second expense for the same schedule
and date) and never posts into a locked/closed period.
"""
import datetime as dt
from calendar import monthrange

from core.models import SiteConfig, period_locked
from core.utils import sabbath_week_of
from cashbook.models import Expense, RecurringExpense


def due_dates(sched, upto):
    """All due dates for a schedule from its start (or the day after it last
    generated) through `upto`, respecting an optional end date."""
    begin = sched.start_date or dt.date(2000, 1, 1)
    if sched.last_generated and sched.last_generated >= begin:
        begin = sched.last_generated + dt.timedelta(days=1)
    end = upto
    if sched.end_date and sched.end_date < end:
        end = sched.end_date
    out = []
    if begin > end:
        return out
    if sched.frequency == RecurringExpense.Frequency.SABBATH:
        # first Saturday on/after begin (Mon=0 … Sat=5), then weekly
        d = begin + dt.timedelta((5 - begin.weekday()) % 7)
        while d <= end:
            out.append(d)
            d += dt.timedelta(days=7)
    else:  # MONTHLY / QUARTERLY / YEARLY — step by months, anchored to start month
        step = {RecurringExpense.Frequency.MONTHLY: 1,
                RecurringExpense.Frequency.QUARTERLY: 3,
                RecurringExpense.Frequency.YEARLY: 12}.get(sched.frequency, 1)
        anchor = (sched.start_date or begin).month
        y, m = begin.year, begin.month
        while dt.date(y, m, 1) <= end:
            if (m - anchor) % step == 0:
                dom = min(sched.day_of_month or 1, monthrange(y, m)[1])
                d = dt.date(y, m, dom)
                if begin <= d <= end:
                    out.append(d)
            m += 1
            if m > 12:
                m, y = 1, y + 1
    return out


def generate_schedule(sched, upto=None, user=None):
    """Create the outstanding Expense records for one schedule. Returns count."""
    upto = upto or dt.date.today()
    cfg = SiteConfig.get()
    # The configuration flag — not the role of whoever clicked "Generate" — decides
    # whether generated items are auto-approved. High-value items (at/above the dual
    # approval threshold) are never auto-approved; they wait for treasurer sign-off.
    threshold = cfg.dual_approval_threshold or 0
    high_value = threshold and sched.amount >= threshold
    auto = (not cfg.require_expense_approval) and not high_value
    status = Expense.Status.APPROVED if auto else Expense.Status.PENDING
    actor = user or sched.created_by          # who recorded the generation
    approver = sched.created_by               # the schedule's owner approves, not the caller
    n = 0
    new_last = sched.last_generated
    hit_locked_gap = False
    for d in due_dates(sched, upto):
        if period_locked(d):
            # Do not advance last_generated past a locked period — leave this and
            # all later dates pending so they regenerate once the period unlocks.
            hit_locked_gap = True
            continue
        if not Expense.objects.filter(recurring=sched, date=d).exists():
            Expense.objects.create(
                date=d, sabbath_week=sabbath_week_of(d), department=sched.department,
                description=sched.description, amount=sched.amount, category=sched.category,
                # Taken from the schedule rather than hardcoded: a monthly
                # instalment on a capital purchase is scheduled but is not a
                # recurrent cost, and calling it one misstates the analysis.
                expenditure_type=(sched.expenditure_type
                                  or Expense.ExpenditureType.RECURRENT),
                claimant=sched.claimant, method=sched.method, status=status,
                # The supplier, and the rest of what a treasurer would have
                # typed. Without these the generated row arrives incomplete and
                # has to be edited by hand, which is what scheduling was for.
                vendor=sched.vendor,
                payee=(sched.payee or (sched.vendor.name if sched.vendor else ""))[:160],
                voucher_no=sched.voucher_no,
                paid_from_petty_cash=sched.paid_from_petty_cash,
                budget_line=sched.budget_line,
                recorded_by=actor, recurring=sched,
                approved_by=(approver if status == Expense.Status.APPROVED else None),
                paid_date=None)
            n += 1
        if not hit_locked_gap:
            new_last = d if (new_last is None or d > new_last) else new_last
    if new_last != sched.last_generated:
        sched.last_generated = new_last
        sched.save(update_fields=["last_generated"])
    return n


def generate_due(upto=None, user=None):
    """Generate outstanding entries for every active schedule. Returns total count."""
    upto = upto or dt.date.today()
    total = 0
    for sched in RecurringExpense.objects.filter(active=True):
        total += generate_schedule(sched, upto, user)
    return total


def next_due(sched, after=None):
    """The next date this schedule would generate (for display)."""
    after = after or dt.date.today()
    horizon = after + dt.timedelta(days=370)
    # peek forward without committing
    peek = RecurringExpense(
        description=sched.description, department=sched.department, amount=sched.amount,
        frequency=sched.frequency, day_of_month=sched.day_of_month,
        start_date=max(sched.start_date, after), end_date=sched.end_date,
        last_generated=None, created_by_id=sched.created_by_id)
    dates = due_dates(peek, horizon)
    return dates[0] if dates else None
