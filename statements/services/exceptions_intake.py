"""Taking a bank-register exception to the books.

A register exception is a bank line with no matching transaction in our books
(MISSING_IN_LEDGER) — or, more rarely, a transaction the bank never mentioned
(MISSING_IN_BANK). "Take it to the books" is right in principle, but a raw
credit or debit is not always new money, and posting it blindly would corrupt
balances. So the treasurer says WHAT KIND of thing each exception is, and each
kind hits — or deliberately does not hit — the books differently:

  NEW_MOVEMENT   A genuine receipt or payment the books have simply never seen.
                 → a REVIEW transaction (credit or debit) in the review queue,
                   for normal allocation. This is the only disposition that
                   creates money the books did not have.

  BANKING        The bank credit that is the OTHER LEG of cash already receipted
                 — Sabbath cash counted (and already in the fund), then
                 deposited. → an is_banking transaction: it reconciles the bank
                 line and counts toward the bank position, but is NOT income and
                 belongs to no fund, because recognising the income again on
                 deposit would double-count it.

  ALREADY_BOOKED The movement is already in the books under a different entry —
                 an expense already recorded, one withdrawal that paid several
                 expenses, a receipt entered by hand. → NO posting; the exception
                 is linked to the existing entr(y/ies) and closed, so it stops
                 being re-raised without any new money being created.

  BANK_CHARGE    A fee the bank levied that nobody recorded — stamp duty, ledger
                 fees, cheque-book charges. → a posted Expense (BANK_CHARGE
                 category) against a chosen fund, which is the correct place for
                 a real cost that left the account.

Every disposition closes the exception with a reason and a named person, so the
discrepancy report does not re-raise an explained item — the whole point of
storing exceptions rather than recomputing them.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction as db_tx
from django.utils import timezone

from giving.models import Transaction
from statements.models_register import RegisterException


# The dispositions, and which exception kinds each one is valid for.
NEW_MOVEMENT = "NEW_MOVEMENT"
BANKING = "BANKING"
ALREADY_BOOKED = "ALREADY_BOOKED"
BANK_CHARGE = "BANK_CHARGE"

DISPOSITIONS = {
    NEW_MOVEMENT: "Genuine new movement — send to the review queue",
    BANKING: "Banking of cash already receipted — reconcile only, no income",
    ALREADY_BOOKED: "Already in our books elsewhere — link and close",
    BANK_CHARGE: "Bank charge — post as an expense",
}


class DispositionNotApplicable(ValidationError):
    """This disposition does not fit this exception (e.g. BANKING on a debit)."""


def _line_of(exc):
    """The bank StatementLine behind a MISSING_IN_LEDGER exception. A
    MISSING_IN_BANK exception has no bank line — it is OUR row the bank never
    confirmed — so most 'take to books' dispositions do not apply to it."""
    return exc.line


def _is_credit(exc):
    """A credit exception moved money IN (positive frozen amount)."""
    if exc.line_id and exc.line:
        return bool(exc.line.credit)
    return exc.amount > 0


def applicable_dispositions(exc):
    """Which dispositions make sense for this exception, so the UI only offers
    the ones that fit and a bulk action can skip the rest with a clear reason."""
    out = []
    if exc.kind != RegisterException.Kind.MISSING_IN_LEDGER:
        # a MISSING_IN_BANK exception is our row the bank never confirmed; taking
        # it "to the books" makes no sense — it is already in them. It is handled
        # by the ordinary resolve/ignore, not here.
        return out
    credit = _is_credit(exc)
    out.append(NEW_MOVEMENT)          # always valid: it is real bank movement
    out.append(ALREADY_BOOKED)        # always valid: it may already be recorded
    if credit:
        out.append(BANKING)           # only a CREDIT can be the banking of cash
    else:
        out.append(BANK_CHARGE)       # only a DEBIT can be a bank charge
    return out


@db_tx.atomic
def take_to_books(exc, *, disposition, user, account, note="",
                  department=None, linked_transaction_ids=None):
    """Apply one disposition to one exception. Returns a short result dict.

    Raises DispositionNotApplicable if the disposition does not fit the
    exception (a debit cannot be banking; a credit cannot be a bank charge),
    so a bulk caller can skip it and report why.
    """
    if not exc.is_open:
        raise ValidationError(f"This exception is already {exc.get_status_display().lower()}.")
    if disposition not in DISPOSITIONS:
        raise ValidationError("Unknown disposition.")
    if disposition not in applicable_dispositions(exc):
        raise DispositionNotApplicable(
            f"{DISPOSITIONS[disposition]} does not fit this "
            f"{'credit' if _is_credit(exc) else 'debit'} exception.")

    if disposition == NEW_MOVEMENT:
        return _to_review_queue(exc, user=user, account=account, note=note)
    if disposition == BANKING:
        return _as_banking(exc, user=user, account=account, note=note)
    if disposition == ALREADY_BOOKED:
        return _link_already_booked(
            exc, user=user, note=note,
            linked_transaction_ids=linked_transaction_ids)
    if disposition == BANK_CHARGE:
        return _as_bank_charge(exc, user=user, account=account, note=note,
                               department=department)


# ---------------------------------------------------------------------------
# the four dispositions
# ---------------------------------------------------------------------------

def _to_review_queue(exc, *, user, account, note):
    """A genuine new movement → a REVIEW transaction (credit or debit) for
    normal allocation. This is the only disposition that adds money."""
    line = _line_of(exc)
    credit = _is_credit(exc)
    amount = abs(exc.amount)
    date = (line.date if line else exc.date)

    txn = Transaction.objects.create(
        date=date, channel=Transaction.Channel.BANK,
        direction=(Transaction.Direction.CREDIT if credit
                   else Transaction.Direction.DEBIT),
        amount=amount, department=None,
        allocation_status=Transaction.Status.REVIEW, confirmed=True,
        bank_account=account,
        reference=(line.reference if line else exc.ref or "")[:60],
        payer_name=(line.payer_name if line else "")[:120],
        payer_phone=(line.payer_phone if line else "")[:12],
        core_ref=_unique_core_ref(line.core_ref if line else exc.ref),
        mpesa_ref=((line.mpesa_ref if line else "") or "")[:30],
        bank_receipt=_unique_receipt(line.receipt if line else ""),
        raw_narration=(line.raw_narration if line else exc.detail or ""),
    )
    _close(exc, user, note or "Taken to the review queue as a genuine new "
                              "movement.", transaction=None)
    return {"action": NEW_MOVEMENT, "transaction_id": txn.pk,
            "message": f"{amount:,.2f} sent to the review queue."}


def _as_banking(exc, *, user, account, note):
    """The banking of cash already receipted → an is_banking credit that
    reconciles the bank line but is not income and touches no fund."""
    line = _line_of(exc)
    amount = abs(exc.amount)
    date = (line.date if line else exc.date)

    txn = Transaction.objects.create(
        date=date, channel=Transaction.Channel.BANK,
        direction=Transaction.Direction.CREDIT, amount=amount,
        department=None, is_banking=True, excluded_from_income=True,
        allocation_status=Transaction.Status.MANUAL, confirmed=True,
        bank_account=account,
        reference=(line.reference if line else exc.ref or "")[:60],
        core_ref=_unique_core_ref(line.core_ref if line else exc.ref),
        mpesa_ref=((line.mpesa_ref if line else "") or "")[:30],
        bank_receipt=_unique_receipt(line.receipt if line else ""),
        raw_narration=("[Banking of already-receipted cash] "
                       + (line.raw_narration if line else exc.detail or ""))[:1000],
    )
    _close(exc, user,
           note or "Banking of cash already receipted — reconciled without "
                   "recognising income again (the offering was already booked "
                   "when the cash was counted).",
           transaction=None)
    return {"action": BANKING, "transaction_id": txn.pk,
            "message": f"{amount:,.2f} reconciled as banking — no income, no fund."}


def _link_already_booked(exc, *, user, note, linked_transaction_ids):
    """The movement is already in the books elsewhere → no posting, just close
    with a link. Covers an expense already recorded, one withdrawal that paid
    several expenses, or a hand-entered receipt."""
    ids = [int(i) for i in (linked_transaction_ids or []) if str(i).strip()]
    linked = list(Transaction.objects.filter(pk__in=ids)) if ids else []
    detail = note or "Already recorded in our books under another entry."
    if linked:
        refs = ", ".join(f"#{t.pk} ({t.amount:,.2f})" for t in linked)
        detail = f"{detail} Linked to {refs}."
    _close(exc, user, detail[:255], transaction=None)
    return {"action": ALREADY_BOOKED, "linked": len(linked),
            "message": "Closed as already booked — no new entry created."}


def _as_bank_charge(exc, *, user, account, note, department):
    """A bank fee nobody recorded → a posted Expense in the BANK_CHARGE
    category, against a chosen fund."""
    from cashbook.models import Expense
    if department is None:
        raise ValidationError("Choose the fund the bank charge should be posted to.")
    line = _line_of(exc)
    amount = abs(exc.amount)
    date = (line.date if line else exc.date)

    expense = Expense.objects.create(
        date=date, department=department, amount=amount,
        description=(note or (line.raw_narration if line else exc.detail)
                     or "Bank charge")[:200],
        category=Expense.Category.BANK_CHARGE,
        method=Expense.Method.BANK,
        status=Expense.Status.PAID, paid_date=date,
        recorded_by=user, approved_by=user)

    # a matching bank DEBIT so the register reconciles this charge, linked to the
    # expense (auto-reconcile) so it is not double-counted against the fund.
    txn = Transaction.objects.create(
        date=date, channel=Transaction.Channel.BANK,
        direction=Transaction.Direction.DEBIT, amount=amount,
        department=department, allocation_status=Transaction.Status.MANUAL,
        confirmed=True, bank_account=account,
        reference=(line.reference if line else exc.ref or "")[:60],
        core_ref=_unique_core_ref(line.core_ref if line else exc.ref),
        raw_narration=("[Bank charge] "
                       + (line.raw_narration if line else exc.detail or ""))[:1000],
    )
    if hasattr(expense, "bank_transaction"):
        expense.bank_transaction = txn
        expense.save(update_fields=["bank_transaction"])

    _close(exc, user, note or f"Posted as a bank charge to {department.name}.",
           transaction=None)
    return {"action": BANK_CHARGE, "expense_id": expense.pk,
            "transaction_id": txn.pk,
            "message": f"{amount:,.2f} posted as a bank charge to {department.name}."}


# ---------------------------------------------------------------------------
# bulk
# ---------------------------------------------------------------------------

def bulk_take_to_books(exceptions, *, disposition, user, account, note="",
                       department=None):
    """Apply ONE disposition to several exceptions. Items the disposition does
    not fit are SKIPPED and reported (not silently dropped, not fatal to the
    batch) — a debit cannot be banking, a credit cannot be a bank charge, and a
    MISSING_IN_BANK exception is not taken to the books at all.

    Returns {"done": [...], "skipped": [(exc, reason), ...]}.
    """
    done, skipped = [], []
    for exc in exceptions:
        if not exc.is_open:
            skipped.append((exc, "already resolved"))
            continue
        if disposition not in applicable_dispositions(exc):
            reason = ("not a bank-statement line" if exc.kind !=
                      RegisterException.Kind.MISSING_IN_LEDGER else
                      f"a {'credit' if _is_credit(exc) else 'debit'} cannot be "
                      f"'{DISPOSITIONS[disposition].split(' — ')[0].lower()}'")
            skipped.append((exc, reason))
            continue
        # ALREADY_BOOKED with no explicit links in bulk is allowed — it simply
        # records that the batch is already on the books; linking specific
        # entries is a per-item action.
        try:
            result = take_to_books(
                exc, disposition=disposition, user=user, account=account,
                note=note, department=department)
            done.append((exc, result))
        except DispositionNotApplicable as e:
            skipped.append((exc, "; ".join(e.messages)))
        except ValidationError as e:
            skipped.append((exc, "; ".join(e.messages)))
    return {"done": done, "skipped": skipped}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _close(exc, user, resolution, *, transaction=None):
    exc.status = RegisterException.Status.RESOLVED
    exc.resolved_by = user
    exc.resolved_at = timezone.now()
    exc.resolution = resolution[:255]
    exc.save(update_fields=["status", "resolved_by", "resolved_at", "resolution"])


def _unique_core_ref(base):
    """core_ref is UNIQUE on Transaction. A register line's core_ref may already
    be on the books (that is often WHY there is an exception — a partial match),
    so suffix to avoid an IntegrityError, keeping the "-S" convention."""
    base = (base or "").strip().upper() or None
    if not base:
        return None
    if not Transaction.objects.filter(core_ref=base).exists() and \
            not Transaction.objects.filter(core_ref__startswith=f"{base}-S").exists():
        return base
    n = 1
    while Transaction.objects.filter(core_ref=f"{base}-S{n}").exists():
        n += 1
    return f"{base}-S{n}"


def _unique_receipt(base):
    base = (base or "").strip().upper() or None
    if not base:
        return None
    if Transaction.objects.filter(bank_receipt=base).exists():
        return None
    return base[:20]
