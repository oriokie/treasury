"""Automatic bank reconciliation with confidence scoring.

Matches unreconciled bank DEBIT lines (statement withdrawals) to the internal
Expense each most likely pays, scoring on amount, date, reference/cheque and
payee/narration overlap. High-confidence matches are linked automatically;
medium-confidence matches are queued for a treasurer to confirm.

The score is transparent and additive so every figure can be explained:
    exact amount ............ 50   (required — a payment must equal the debit)
    same date ............... 25 / within 3 days 15 / within 10 days 8
    cheque/voucher in narration  20
    payee/description in narration 10
Capped at 100.  AUTO link ≥ 90,  REVIEW ≥ 55,  ignored below.
"""
from django.db import transaction as db_tx
import re
from decimal import Decimal

from django.utils import timezone

from giving.models import Transaction
from cashbook.models import Expense
from statements.models import ReconciliationMatch

AUTO_THRESHOLD = 90
REVIEW_THRESHOLD = 55


def _narration(debit):
    return " ".join(filter(None, [debit.raw_narration, debit.mpesa_ref,
                                   debit.reference, debit.payer_name])).lower()


def score(debit, expense):
    """Return (confidence, reason). Amount must match exactly or score is 0."""
    if abs(debit.amount - expense.amount) > Decimal("0.01"):
        return 0, ""
    pts, reasons = 50, ["exact amount"]
    days = abs((debit.date - expense.date).days)
    if days == 0:
        pts += 25; reasons.append("same date")
    elif days <= 3:
        pts += 15; reasons.append(f"within {days}d")
    elif days <= 10:
        pts += 8; reasons.append(f"within {days}d")
    narr = _narration(debit)
    if expense.voucher_no and expense.voucher_no.lower() in narr:
        pts += 20; reasons.append(f"cheque/voucher {expense.voucher_no}")
    tokens = [w for w in re.split(r"\W+", f"{expense.claimant} {expense.description}".lower())
              if len(w) > 3]
    if any(tok in narr for tok in tokens):
        pts += 10; reasons.append("payee in narration")
    return min(pts, 100), "; ".join(reasons)


def candidates_for(debit, expenses):
    best, best_reason, best_exp = 0, "", None
    for exp in expenses:
        c, reason = score(debit, exp)
        if c > best:
            best, best_reason, best_exp = c, reason, exp
    return best, best_reason, best_exp


@db_tx.atomic
def _link(debit, expense, user=None):
    """Link an expense to its bank debit (mirrors the manual debit-match)."""
    expense.bank_transaction = debit
    if expense.status != Expense.Status.PAID:
        expense.status = Expense.Status.PAID
        expense.paid_date = debit.date
    expense.save()
    if debit.department_id is None:
        debit.department = expense.department
    debit.allocation_status = Transaction.Status.MANUAL
    debit.save(update_fields=["department", "allocation_status"])


def run_auto_reconcile(user=None):
    """Scan unreconciled debits, create ReconciliationMatch rows, auto-link the
    high-confidence ones. Returns a summary dict."""
    # debits not already linked to an expense and without an open suggestion
    linked_debit_ids = set(Expense.objects.filter(bank_transaction__isnull=False)
                           .values_list("bank_transaction_id", flat=True))
    open_match_debit_ids = set(ReconciliationMatch.objects.filter(
        status__in=[ReconciliationMatch.Status.AUTO, ReconciliationMatch.Status.REVIEW,
                    ReconciliationMatch.Status.CONFIRMED]).values_list("transaction_id", flat=True))
    debits = (Transaction.objects.filter(direction=Transaction.Direction.DEBIT,
              is_reversal=False)
              .exclude(id__in=linked_debit_ids | open_match_debit_ids))
    # candidate expenses: recorded but not yet tied to a bank line
    expenses = list(Expense.objects.filter(bank_transaction__isnull=True)
                    .exclude(status=Expense.Status.REJECTED))
    auto = review = 0
    for debit in debits:
        conf, reason, exp = candidates_for(debit, expenses)
        if not exp or conf < REVIEW_THRESHOLD:
            continue
        status = (ReconciliationMatch.Status.AUTO if conf >= AUTO_THRESHOLD
                  else ReconciliationMatch.Status.REVIEW)
        m = ReconciliationMatch.objects.create(
            transaction=debit, expense=exp, confidence=conf, reason=reason, status=status)
        if status == ReconciliationMatch.Status.AUTO:
            _link(debit, exp, user)
            m.confirmed_by = user
            m.confirmed_at = timezone.now()
            m.save(update_fields=["confirmed_by", "confirmed_at"])
            expenses.remove(exp)        # don't reuse a linked expense
            auto += 1
        else:
            review += 1
    return {"auto": auto, "review": review}


def confirm(match, user):
    if match.expense and match.transaction:
        _link(match.transaction, match.expense, user)
    match.status = ReconciliationMatch.Status.CONFIRMED
    match.confirmed_by = user
    match.confirmed_at = timezone.now()
    match.save(update_fields=["status", "confirmed_by", "confirmed_at"])


def reject(match):
    # if it had been auto-linked, unlink the expense
    if match.status == ReconciliationMatch.Status.AUTO and match.expense:
        exp = match.expense
        if exp.bank_transaction_id == match.transaction_id:
            exp.bank_transaction = None
            exp.save(update_fields=["bank_transaction"])
    match.status = ReconciliationMatch.Status.REJECTED
    match.save(update_fields=["status"])
