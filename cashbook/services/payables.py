"""Settling an obligation — a payable or an accrual — in full, or a bit at a time.

A payable is an invoice the church has received and not yet paid. Until now it
could only be discharged in one movement: one button, one expense for the whole
amount, done. Real vendors are rarely paid that way. A hardware bill gets 20,000
this month and the rest when the harvest comes in, and a treasurer with only an
all-or-nothing button has two bad options — record the whole thing as paid when
it is not, or record nothing and let the cash book disagree with the bank.

So settlement is now a sequence of payments, and the amount still owed is the
invoice less what has actually been paid. That figure is computed from the
payments themselves (``Payable.paid_total``) and never stored twice.

**The accounting consequence, which is the point of the exercise.** A payable
that is half paid is a liability for the other half. ``open_payables_total`` in
``treasury_position`` now nets each payable down by the payments made on or
before the reporting date, so a part-paid invoice reduces the balance sheet the
day the money leaves — not when the last instalment happens to arrive.
"""
import datetime as _dt
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction as db_tx

from ..models import Accrual, Expense, Payable
from core.utils import sabbath_week_of


def _link_field(obligation):
    """Which column on Expense points back at this kind of obligation."""
    return "accrual" if isinstance(obligation, Accrual) else "payable"


def refresh_settlement(payable, *, save=True):
    """Bring the cached `settled`/`settled_on` flags back in line.

    The flags are a cache of what the payments say — they exist so that "what do
    we still owe" stays an indexed query, and because reports and the backup
    export already read them. This is the ONLY function that writes them, which
    is what stops the cache becoming a second opinion.

    `settled_on` is the date of the instalment that cleared the balance, not the
    date of the first payment: the liability survives until the last shilling.
    """
    counted = (payable.payments
               .filter(status__in=Payable.COUNTED_STATUSES)
               .order_by("date", "id"))
    running, cleared_on = Decimal("0"), None
    for payment in counted:
        running += payment.amount
        if running >= payable.amount:
            cleared_on = payment.date
            break

    payable.settled = cleared_on is not None
    payable.settled_on = cleared_on
    if save:
        payable.save(update_fields=["settled", "settled_on"])
    return payable


@db_tx.atomic
def settle(payable, *, amount=None, user, on=None, method=Expense.Method.BANK,
           reference="", note="", paid_from_petty_cash=False):
    """Pay some or all of a payable, recording the money as a real expense.

    `amount=None` means "the rest of it" — the common case, and the one the old
    single-shot button did.

    The payment is an ordinary ``Expense`` in the payable's own fund, so it
    reaches the cash book, the fund balance and the ledger by exactly the route
    every other payment takes. Nothing here posts to the ledger itself; making
    the expense IS the posting, and a second path would be a second version of
    the truth.
    """
    on = on or _dt.date.today()
    outstanding = payable.balance

    if outstanding <= 0:
        raise ValidationError("That payable is already settled in full.")

    if amount in (None, ""):
        amount = outstanding
    amount = Decimal(str(amount)).quantize(Decimal("0.01"))

    if amount <= 0:
        raise ValidationError("Enter how much is being paid.")
    if amount > outstanding:
        # Refused rather than silently capped. Paying more than is owed is
        # either a typo or a credit the vendor now holds, and both need a human
        # to say which — quietly writing off the difference would hide it.
        raise ValidationError(
            f"That is more than is still owed. The balance on this payable is "
            f"{outstanding:,.2f}.")
    if on < payable.date:
        raise ValidationError(
            "A payment cannot be dated before the invoice it settles.")

    part = amount < outstanding
    description = f"{'Part payment' if part else 'Settle'}: {payable.description}"

    expense = Expense.objects.create(
        date=on, sabbath_week=sabbath_week_of(on),
        department=payable.department,
        description=description[:200],
        amount=amount, category=payable.category, method=method,
        status=Expense.Status.PAID, paid_date=on,
        payee=(getattr(payable, "vendor", "") or "")[:160],
        # Carry the supplier through, so a payment made by settling a bill lands
        # on the supplier's account without anyone re-selecting them.
        vendor=getattr(payable, "supplier", None),
        # The bank/M-Pesa code for this instalment goes on the voucher number —
        # Expense has no separate reference field, and voucher_no is what the
        # rest of the cash book already prints against a payment.
        voucher_no=(reference or "")[:30],
        paid_from_petty_cash=paid_from_petty_cash,
        recorded_by=user, approved_by=user,
        **{_link_field(payable): payable})

    refresh_settlement(payable)
    return expense


@db_tx.atomic
def unlink_payment(expense, *, user=None):
    """Detach a payment from its payable, e.g. when it was linked in error.

    The expense itself is left alone — the money did leave the account, and
    deleting it to undo a mis-linking would be fixing a paperwork mistake by
    losing a real payment. Only the link is removed, and the payable's balance
    recovers by that much.
    """
    payable = expense.payable or getattr(expense, "accrual", None)
    if payable is None:
        return None
    field = _link_field(payable)
    setattr(expense, field, None)
    expense.save(update_fields=[field])
    refresh_settlement(payable)
    return payable
