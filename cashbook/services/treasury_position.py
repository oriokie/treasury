"""Treasury position service — the canonical cash-location and receivable
figures for the treasury: the petty-cash float balance, outstanding staff
advances (total, bank-issued, petty-issued), unpresented payment instruments,
cash in transit, and pending expense claims.

Relocated here VERBATIM from ``cashbook/views.py`` (recommendation #7 — that
module had grown into a god-file, and these functions are not view code: they
are the authoritative accounting implementations imported across the
application, the assistant, period close, statements reconciliation and the
Financial Metrics Registry). ``cashbook.views`` re-imports them under the old
names, so every existing import path keeps working; the metrics registry now
points HERE as the authoritative home.

Nothing about any calculation changed in the move — the bodies are identical.
The only additions are ``cash_in_transit_asof`` and ``pending_expense_claims``,
two previously-unnamed concepts the Treasurer's Report needs, defined from the
existing models (no new accounting rules).
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from django.db.models import Count, Sum


# ===========================================================================
# Petty cash float
# ===========================================================================

def petty_balance_asof(on):
    """The petty-cash float balance as at a date: top-ups less petty
    disbursements, plus cash refunded back into the box, less cash currently
    out with advance holders. Petty cash is a CASH LOCATION (a physical
    float), not a fund — ministry funds carry the actual cost of petty-paid
    expenses, so fund balances stay correct.

    (Moved verbatim from cashbook.views._petty_balance_asof.)"""
    from cashbook.models import (Expense, ExpenseRefund, PettyCashTopUp,
                                 StaffAdvance)
    topups = (PettyCashTopUp.objects.filter(date__lte=on)
              .aggregate(t=Sum("amount"))["t"] or Decimal(0))
    disb = (Expense.objects.filter(paid_from_petty_cash=True, date__lte=on,
            status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
            .aggregate(t=Sum("amount"))["t"] or Decimal(0))
    # cash refunded back into the petty box tops the float up again
    refunds_in = (ExpenseRefund.objects.filter(to_petty_cash=True, date__lte=on)
                  .aggregate(t=Sum("amount"))["t"] or Decimal(0))
    # advances issued out of the petty box, still unreturned, are also "out"
    # — deliberately petty_cash_out_asof, not petty_outstanding_asof: the
    # float's own balance must not change just because an advance was later
    # accounted for on paper (an expense record, not a cash movement)
    adv_out = Decimal(0)
    for adv in StaffAdvance.objects.filter(from_petty_cash=True,
                                           date_issued__lte=on):
        adv_out += adv.petty_cash_out_asof(on)
    return topups - disb + refunds_in - adv_out


# ===========================================================================
# Staff advances (receivables)
# ===========================================================================

def outstanding_bank_advances_total(as_of=None):
    """Outstanding advances issued from the BANK (not petty cash). These reduce
    the bank statement balance at issuance but are not yet an expense in the cash
    book, so until accounted for they are a reconciling item between bank and book.
    Petty-funded advances are excluded — those sit in the petty-cash float.
    Top-ups dated after `as_of` are excluded (they hadn't left the bank yet).

    (Moved verbatim from cashbook.views.)"""
    from cashbook.models import Expense, StaffAdvance
    as_of = as_of or _dt.date.today()
    total = Decimal(0)
    for adv in StaffAdvance.objects.filter(date_issued__lte=as_of,
                                           from_petty_cash=False
            ).exclude(status=StaffAdvance.Status.CLOSED):
        topups_after = (adv.topups.filter(date__gt=as_of)
                        .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        settled = (adv.expenses.filter(
            status__in=[Expense.Status.APPROVED, Expense.Status.PAID],
            date__lte=as_of).aggregate(t=Sum("amount"))["t"] or Decimal(0))
        bal = ((adv.amount or Decimal(0)) - topups_after - settled
               - (adv.returned_to_petty or Decimal(0)))
        if bal > 0:
            total += bal
    return total


def outstanding_petty_advances_total(as_of=None):
    """Outstanding advances issued from the PETTY-CASH box. The petty float
    (`petty_balance_asof`) already subtracts these — the cash has left the box
    and is with the advance holder — so on a bank reconciliation they must be
    listed as their own cash-at-hand item, or that money silently disappears
    from the worksheet.

    (Moved verbatim from cashbook.views.)"""
    from cashbook.models import StaffAdvance
    as_of = as_of or _dt.date.today()
    total = Decimal(0)
    for adv in StaffAdvance.objects.filter(from_petty_cash=True,
            date_issued__lte=as_of).exclude(status=StaffAdvance.Status.CLOSED):
        try:
            total += adv.petty_outstanding_asof(as_of)
        except Exception:  # noqa: BLE001
            continue
    return total


def outstanding_advances_total(as_of=None):
    """Money advanced to staff that has not yet been accounted for by receipts —
    i.e. a receivable. Computed as (amount advanced as of the date − expenses
    settled up to the date) for advances issued on/before `as_of` that are not
    yet closed. The amount advanced excludes top-ups dated after `as_of` (they
    had not been advanced yet), keeping both sides of the subtraction as of the
    same date. Only positive balances count (a shortfall is owed to staff, not a
    receivable).

    (Moved verbatim from cashbook.views.)"""
    from cashbook.models import Expense, StaffAdvance
    as_of = as_of or _dt.date.today()
    total = Decimal(0)
    for adv in StaffAdvance.objects.filter(date_issued__lte=as_of).exclude(
            status=StaffAdvance.Status.CLOSED):
        # amount advanced as of the date: current total less any top-ups that
        # were only added after the reporting date
        topups_after = (adv.topups.filter(date__gt=as_of)
                        .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        advanced = (adv.amount or Decimal(0)) - topups_after
        settled = (adv.expenses.filter(
            status__in=[Expense.Status.APPROVED, Expense.Status.PAID],
            date__lte=as_of).aggregate(t=Sum("amount"))["t"] or Decimal(0))
        bal = advanced - settled
        if bal > 0:
            total += bal
    return total


# ===========================================================================
# Unpresented payment instruments
# ===========================================================================

def unpresented_payments_qs(as_of=None):
    """Payment instruments outstanding at the bank AS AT a date: issued by
    then, not cleared/cancelled/voided/reversed by then — judged on the event
    DATES via PaymentInstrument.outstanding_asof, never today's status, so
    historical reconciliations stay correct no matter when they are run.
    Covers every bank-clearing method (cheque, EFT, RTGS, M-Pesa, other) —
    cash in hand never clears through the bank.

    (Moved verbatim from cashbook.views.)"""
    from cashbook.models import PaymentInstrument
    as_of = as_of or _dt.date.today()
    return PaymentInstrument.outstanding_asof(as_of).filter(
        method__in=PaymentInstrument.BANK_CLEARING_METHODS)


def unpresented_cheques_total(as_of=None):
    """Total of instruments issued but not yet cleared as at the date (name
    kept for the existing reconciliation call sites; covers all bank-clearing
    methods, not only cheques).

    (Moved verbatim from cashbook.views.)"""
    return unpresented_payments_qs(as_of).aggregate(t=Sum("amount"))["t"] \
        or Decimal(0)


# ===========================================================================
# Cash in transit (new named concept — defined from the existing
# reconciliation worksheet, no new accounting rules)
# ===========================================================================

def cash_in_transit_asof(as_of=None):
    """Deposits in transit as at a date: the IN_TRANSIT reconciling items on
    the most recent bank-reconciliation worksheet dated on or before `as_of`.
    Money receipted in the books but not yet reflected on the bank statement.

    Returns Decimal(0) when no reconciliation worksheet exists — the concept
    is only knowable from a prepared reconciliation, so absence of a worksheet
    means no in-transit amount is recorded, not that one is hidden."""
    from statements.models import BankReconciliation, ReconciliationItem
    as_of = as_of or _dt.date.today()
    rec = (BankReconciliation.objects.filter(statement_date__lte=as_of)
           .order_by("-statement_date", "-id").first())
    if rec is None:
        return Decimal(0)
    return (rec.items.filter(kind=ReconciliationItem.Kind.IN_TRANSIT)
            .aggregate(t=Sum("amount"))["t"] or Decimal(0))


# ===========================================================================
# Pending expense claims (new named concept)
# ===========================================================================

def pending_expense_claims(as_of=None):
    """Expense claims awaiting treasurer approval: count and total of expenses
    still in PENDING status, dated on or before `as_of`. NOTE: status is the
    CURRENT state (approval history is not replayed), so this answers "what is
    pending right now, of claims dated in/before the period" — the operational
    question the board pack asks — not a historical point-in-time
    reconstruction."""
    from cashbook.models import Expense
    as_of = as_of or _dt.date.today()
    agg = (Expense.objects.filter(status=Expense.Status.PENDING,
                                  date__lte=as_of)
           .aggregate(t=Sum("amount"), n=Count("id")))
    return {"count": agg["n"] or 0, "total": agg["t"] or Decimal(0)}


# ===========================================================================
# Accrual-basis overlay: payables, accruals, prepayments
# (relocated VERBATIM from cashbook/views.py — same rationale as the module
#  docstring: not view code, and the Financial Metrics Registry needs a
#  non-view home to import from. Previously called directly by the legacy
#  Statement of Financial Position — reports.financial_statements now reaches
#  these through registered metrics instead, per the "every figure through
#  the registry" rule, so the engine-based board-pack summary can show the
#  identical accrual-basis adjustments the legacy statement always has.)
# ===========================================================================

def open_payables_total(as_of=None):
    """Credit purchases still owed as at a date.

    A payable may be paid in instalments, so this is the invoice less what has
    actually been paid by the reporting date — not the whole invoice until the
    final payment arrives. Paying half a bill on the 10th reduces the liability
    on the 10th, which is the whole reason partial settlement exists: otherwise
    the balance sheet would carry a debt the church had already half discharged.

    The as-of treatment is unchanged in spirit: a payable incurred on or before
    the date counts, and only payments dated on or before it count against it,
    so settling on the 15th still shows in full on a 14th statement.

    Netted per payable rather than in one aggregate, because a payment can never
    reduce the liability below zero — an overpayment to one vendor must not
    cancel out what is owed to another. Hence the per-row `Greatest(..., 0)`
    before the outer sum, and hence this being one annotated query rather than a
    loop: the balance sheet must not issue a query per invoice.
    """
    from django.db.models import Case, Count, DecimalField, F, Q, Value, When
    from django.db.models.functions import Coalesce, Greatest
    from cashbook.models import Expense, Payable

    money = DecimalField(max_digits=14, decimal_places=2)
    zero = Value(Decimal("0"), output_field=money)
    counted = Q(payments__status__in=[Expense.Status.APPROVED, Expense.Status.PAID])

    if not as_of:
        # "Right now" — the cached flag is the fast path, and anything still
        # flagged unsettled owes its invoice less whatever has been paid.
        qs = Payable.objects.filter(settled=False)
        row = (qs.annotate(paid=Coalesce(Sum("payments__amount", filter=counted,
                                             output_field=money), zero))
                 .annotate(owed=Greatest(F("amount") - F("paid"), zero,
                                         output_field=money))
                 .aggregate(t=Sum("owed", output_field=money)))
        return row["t"] or Decimal(0)

    counted &= Q(payments__date__lte=as_of)
    row = (Payable.objects.filter(date__lte=as_of)
           .annotate(
               paid=Coalesce(Sum("payments__amount", filter=counted,
                                 output_field=money), zero),
               n_payments=Count("payments", filter=counted))
           .annotate(owed=Case(
               # A payable marked settled that has no payments to show for it.
               # This is how every payable settled BEFORE instalments existed
               # looks if its expense link was never recorded — and the flag is
               # the only evidence there is that the debt was discharged. Ignore
               # it and the balance sheet resurrects debts the church has
               # already paid, which is a far worse error than trusting a flag
               # a treasurer set deliberately.
               When(n_payments=0, settled=True, settled_on__lte=as_of, then=zero),
               default=Greatest(F("amount") - F("paid"), zero,
                                output_field=money),
               output_field=money))
           .aggregate(t=Sum("owed", output_field=money)))
    return row["t"] or Decimal(0)


def open_accruals_total(as_of=None):
    """Expenses incurred but not yet invoiced/paid, owed as at a date — same
    as-of treatment as open_payables_total."""
    from django.db.models import Q
    from cashbook.models import Accrual
    if as_of:
        qs = Accrual.objects.filter(date__lte=as_of).filter(
            Q(settled=False) | Q(settled_on__gt=as_of) | Q(settled_on__isnull=True))
    else:
        qs = Accrual.objects.filter(settled=False)
    return qs.aggregate(t=Sum("amount"))["t"] or Decimal(0)


def unexpired_prepayments_total(as_of=None):
    """The unexpired (not-yet-consumed) portion of every recorded prepayment
    as at a date — an asset (a future benefit already paid for)."""
    from cashbook.models import Prepayment
    as_of = as_of or _dt.date.today()
    return sum((p.unexpired(as_of) for p in Prepayment.objects.all()), Decimal(0))
