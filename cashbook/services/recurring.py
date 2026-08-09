"""Turn recurring-expense schedules into actual Expense records on their due dates.

Generation is idempotent (it never creates a second expense for the same schedule
and date) and never posts into a locked/closed period.
"""
import datetime as dt
from calendar import monthrange

from core.models import SiteConfig, period_locked
from core.utils import sabbath_week_of
from cashbook.models import Expense, RecurringExpense
from cashbook.services.expenses import new_expense_status


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
    actor = user or sched.created_by          # who recorded the generation
    # What state the generated row starts in is NOT decided here. That rule
    # lives in `services.expenses.new_expense_status`, which the spreadsheet
    # import and the batch screen both read through `expenses.record`; this
    # path used to restate it as "APPROVED if auto else PENDING", which is the
    # rule minus its first and most easily forgotten clause: money paid out of
    # the petty cash tin has already left the drawer, so the row is PAID, not
    # awaiting somebody's approval. A schedule with `paid_from_petty_cash` ticked
    # therefore generated PENDING rows under the default configuration
    # (`require_expense_approval` defaults on), and `petty_balance_asof` counts
    # only APPROVED/PAID disbursements — so the float went on reporting cash
    # the box no longer held, until someone approved a payment that had already
    # happened.
    #
    # The dual-approval threshold still governs `auto` above, and still holds a
    # high-value bank payment back for sign-off. It cannot hold back a
    # petty-cash one: that money is spent, and recording it as pending would
    # not un-spend it, only hide it from the float.
    #
    # `user=sched.created_by`, not the caller: the schedule's owner is the
    # approver of record. Generation is often run by a cron job with no user at
    # all, and whoever clicks "Generate" is not thereby approving anything.
    status, approver, is_paid = new_expense_status(
        paid_from_petty_cash=sched.paid_from_petty_cash,
        auto_approve=auto, user=sched.created_by)
    n = 0
    new_last = sched.last_generated
    hit_locked_gap = False
    for d in due_dates(sched, upto):
        if period_locked(d):
            # Do not advance last_generated past a locked period — leave this and
            # all later dates pending so they regenerate once the period unlocks.
            hit_locked_gap = True
            continue
        # Keyed on the instalment, not on the date the cash moved. An
        # instalment settled early sits under an earlier `date` but carries
        # `recurring_due_date=d`, so it is recognised here and the schedule does
        # not raise a second charge for the same period. Before this, paying
        # early meant recording the expense by hand — invisible to a
        # date-based check — and the fund was charged twice.
        if not Expense.objects.filter(recurring=sched, recurring_due_date=d).exists():
            Expense.objects.create(
                date=d, recurring_due_date=d,
                sabbath_week=sabbath_week_of(d), department=sched.department,
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
                approved_by=approver,
                # A petty-cash row is paid on the day the instalment falls due,
                # which is the day the cash left the tin — the same pairing of
                # status and paid_date `services.expenses.record` writes.
                paid_date=(d if is_paid else None))
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


def upcoming_instalments(sched, count=3, after=None):
    """The next few unsettled due dates for a schedule, soonest first.

    "Unsettled" means no expense yet carries that `recurring_due_date` — so an
    instalment already paid early drops out of the list and cannot be paid twice.
    """
    after = after or dt.date.today()
    horizon = after + dt.timedelta(days=400)
    settled = set(Expense.objects.filter(recurring=sched)
                  .values_list("recurring_due_date", flat=True))
    peek = RecurringExpense(
        description=sched.description, department=sched.department,
        amount=sched.amount, frequency=sched.frequency,
        day_of_month=sched.day_of_month, start_date=sched.start_date,
        end_date=sched.end_date, last_generated=None,
        created_by_id=sched.created_by_id)
    out = [d for d in due_dates(peek, horizon) if d not in settled]
    return out[:count]


def pay_early(sched, due_date, on=None, user=None):
    """Settle a scheduled instalment before its due date.

    A church pays ahead more often than the schedule allowed for: the payee is
    travelling, the office closes over a holiday, or a quarter's rent is settled
    with one cheque. There was no way to do that through the schedule, so it was
    done by hand — and because generation deduplicated on the expense *date*, a
    hand-written early payment was invisible and the schedule raised the charge
    again on the due date. The fund was debited twice with nothing to flag it.

    Two dates are involved and they mean different things:

      * `date` is when the money leaves. Fund balances are kept on a cash basis,
        so this has to be the real payment date or the fund reads wrong for the
        period in between.
      * `recurring_due_date` is the instalment being settled. It keeps the
        schedule's own accounting straight and stops the double charge.

    Refuses a due date that is already settled, and refuses to post into a
    locked period. Returns the created Expense.
    """
    on = on or dt.date.today()
    if sched.end_date and due_date > sched.end_date:
        raise ValueError("That instalment falls after the schedule ends.")
    if due_date not in due_dates(
            RecurringExpense(
                description=sched.description, department=sched.department,
                amount=sched.amount, frequency=sched.frequency,
                day_of_month=sched.day_of_month, start_date=sched.start_date,
                end_date=sched.end_date, last_generated=None,
                created_by_id=sched.created_by_id),
            due_date):
        raise ValueError("That date is not one of this schedule's due dates.")
    if Expense.objects.filter(recurring=sched, recurring_due_date=due_date).exists():
        raise ValueError("That instalment has already been recorded.")
    if period_locked(on):
        raise ValueError("That accounting period is closed.")

    cfg = SiteConfig.get()
    threshold = cfg.dual_approval_threshold or 0
    high_value = threshold and sched.amount >= threshold
    # Paying early does not lower the bar for approval: an amount that needs
    # sign-off on its due date still needs it a fortnight sooner.
    auto = (not cfg.require_expense_approval) and not high_value
    # Through the same single rule as generation — see the note in
    # `generate_schedule`. This path had the identical copy of the status
    # formula and the identical hole in it: an instalment paid early out of the
    # petty cash tin came out PENDING and stayed outside the float's own
    # arithmetic, though the money had already gone.
    status, approver, is_paid = new_expense_status(
        paid_from_petty_cash=sched.paid_from_petty_cash,
        auto_approve=auto, user=sched.created_by)
    actor = user or sched.created_by
    return Expense.objects.create(
        date=on, recurring_due_date=due_date, sabbath_week=sabbath_week_of(on),
        department=sched.department, description=sched.description,
        amount=sched.amount, category=sched.category,
        expenditure_type=(sched.expenditure_type
                          or Expense.ExpenditureType.RECURRENT),
        claimant=sched.claimant, method=sched.method, status=status,
        vendor=sched.vendor,
        payee=(sched.payee or (sched.vendor.name if sched.vendor else ""))[:160],
        voucher_no=sched.voucher_no,
        paid_from_petty_cash=sched.paid_from_petty_cash,
        budget_line=sched.budget_line,
        recorded_by=actor, recurring=sched,
        approved_by=approver,
        # `on`, not the due date: paid_date records when the money actually
        # moved, and paying early is the whole point of this function.
        paid_date=(on if is_paid else None))
