"""Payment-instrument lifecycle. Every state change flows through
apply_event(), which (atomically) moves the status, stamps the event's OWN
date field — business dates never overwrite each other — and records a
PaymentEvent row (user, business date, from/to status, reference, comment).
The current status is therefore always the product of the latest lifecycle
event, and the full history is on the instrument's timeline.

None of this touches the ledger: the instrument's source document (the
expense voucher, remittance, refund or transfer) is the accounting; the
instrument tracks HOW and WHEN the money physically moved.
"""
import datetime as dt

from django.core.exceptions import ValidationError
from django.db import transaction as db_tx


# event -> (resulting status, date field stamped by the event)
_TRANSITIONS = {
    "APPROVE": ("APPROVED", None),
    "PREPARE": ("PREPARED", "date_prepared"),
    "ISSUE": ("ISSUED", "date_issued"),
    "PRESENT": ("PRESENTED", "date_presented"),
    "CLEAR": ("CLEARED", "date_cleared"),
    "CANCEL": ("CANCELLED", "date_cancelled"),
    "REJECT": ("REJECTED", "date_cancelled"),
    "VOID": ("VOIDED", "date_voided"),
    "REVERSE": ("REVERSED", "date_reversed"),
    "EXPIRE": ("EXPIRED", "date_cancelled"),
}

# events that only make sense on an instrument that reached the bank pipeline
_NEEDS_ISSUED = {"PRESENT", "CLEAR", "REVERSE"}
# events refused on a cleared instrument (money moved — reverse it instead)
_BLOCKED_WHEN_CLEARED = {"APPROVE", "PREPARE", "ISSUE", "PRESENT", "CANCEL",
                         "REJECT", "VOID", "EXPIRE"}


@db_tx.atomic
def apply_event(inst, event, user=None, *, on=None, comment="",
                reference="", bank_transaction=None):
    """Apply one lifecycle event to a payment instrument. Returns the
    PaymentEvent recorded. `on` is the business date (defaults to today);
    for CLEAR it is the bank's clearance date and drives every historical
    reconciliation afterwards."""
    from cashbook.models import PaymentEvent, PaymentInstrument

    event = (event or "").upper()
    if event not in _TRANSITIONS:
        raise ValidationError(f"Unknown payment event '{event}'.")
    new_status, date_field = _TRANSITIONS[event]
    on = on or dt.date.today()

    if inst.status == PaymentInstrument.Status.CLEARED \
            and event in _BLOCKED_WHEN_CLEARED:
        raise ValidationError(
            "This payment has already cleared the bank — the money moved. "
            "Reverse it instead of changing its state.")
    if event in _NEEDS_ISSUED and not inst.date_issued:
        raise ValidationError(
            f"Cannot mark an un-issued instrument as {new_status.lower()}.")
    if event == "CLEAR" and inst.date_issued and on < inst.date_issued:
        raise ValidationError(
            "The cleared date cannot be before the issue date.")

    from_status = inst.status
    fields = ["status"]
    inst.status = new_status
    if date_field:
        # ISSUE keeps an existing issue date (re-marking never rewrites the
        # original business date); every other event stamps its own field
        if date_field == "date_issued" and inst.date_issued:
            pass
        else:
            setattr(inst, date_field, on)
            fields.append(date_field)
    if event == "APPROVE":
        from django.utils import timezone
        inst.approved_by = user
        inst.approved_at = timezone.now()
        fields += ["approved_by", "approved_at"]
    if event == "CLEAR" and bank_transaction is not None:
        if getattr(bank_transaction, "direction", "DEBIT") != "DEBIT":
            raise ValidationError("Only a bank DEBIT can clear a payment.")
        inst.bank_transaction = bank_transaction
        fields.append("bank_transaction")
        reference = reference or (bank_transaction.core_ref
                                  or f"debit #{bank_transaction.pk}")
    inst.save(update_fields=fields)

    # A petty-cash cheque is TWO movements, not one: money leaves the bank, and
    # money arrives in the tin. Both must be recorded or the books do not add up
    # — record only the cheque and the float is understated; record only the
    # top-up and the bank is overstated.
    #
    # So the top-up is created HERE, when the cheque is issued (which is when the
    # treasurer walks it to the bank and brings the notes back), rather than left
    # for someone to remember to enter separately. A hand that enters one half and
    # forgets the other is exactly what this exists to prevent.
    if event == "ISSUE" and inst.source_kind == PaymentInstrument.SourceKind.PETTY_CASH:
        _replenish_petty_cash(inst, user, on)
    if event in ("CANCEL", "VOID", "REVERSE", "REJECT") \
            and inst.source_kind == PaymentInstrument.SourceKind.PETTY_CASH:
        _unreplenish_petty_cash(inst)

    return PaymentEvent.objects.create(
        payment=inst, event=event, from_status=from_status,
        to_status=new_status, on=on, user=user,
        reference=reference[:120], comment=comment[:200])


def _replenish_petty_cash(inst, user, on):
    """Put the cash from an issued petty-cash cheque into the float.

    Idempotent: re-marking a cheque as issued must not top the float up twice.
    Cancelling or voiding it removes the top-up again — see
    `_unreplenish_petty_cash`, called from the same state machine.
    """
    from cashbook.models import PettyCashTopUp
    if PettyCashTopUp.objects.filter(instrument=inst).exists():
        return
    PettyCashTopUp.objects.create(
        date=on, amount=inst.amount, instrument=inst,
        note=(f"Petty cash replenished by "
              f"{inst.get_method_display().lower()}"
              + (f" {inst.instrument_number}" if inst.instrument_number else "")
              + ".")[:200],
        recorded_by=user)


def _unreplenish_petty_cash(inst):
    """The cheque did not happen after all — take the cash back out of the float.

    A cancelled or voided cheque never became notes in the tin, so a float that
    still counts it is a float that will not reconcile against the money actually
    there.
    """
    from cashbook.models import PettyCashTopUp
    PettyCashTopUp.objects.filter(instrument=inst).delete()


@db_tx.atomic
def reissue(inst, user, *, number="", on=None, comment=""):
    """Cancel a (lost/stale/spoilt) instrument and open a fresh draft copy for
    the same obligation — the standard cancelled-cheque / re-issued-cheque
    flow. The old instrument keeps its full history; the new one references
    it."""
    from cashbook.models import PaymentInstrument
    if inst.status == PaymentInstrument.Status.CLEARED:
        raise ValidationError("A cleared payment cannot be re-issued.")
    on = on or dt.date.today()
    apply_event(inst, "CANCEL", user, on=on,
                comment=comment or "Cancelled for re-issue")
    copy = PaymentInstrument.objects.create(
        method=inst.method, instrument_number=(number or "")[:40],
        payee=inst.payee, amount=inst.amount, bank_account=inst.bank_account,
        source_kind=inst.source_kind, expense=inst.expense,
        remittance_batch=inst.remittance_batch, refund=inst.refund,
        transfer=inst.transfer,
        note=f"Re-issue of {inst.instrument_number or f'payment #{inst.pk}'}"[:200],
        recorded_by=user, status=PaymentInstrument.Status.DRAFT)
    if inst.pk:
        copy.extra_expenses.set(inst.extra_expenses.all())
    from cashbook.models import PaymentEvent
    PaymentEvent.objects.create(
        payment=copy, event=PaymentEvent.Event.REISSUE,
        from_status="", to_status="DRAFT", on=on, user=user,
        reference=(inst.instrument_number or str(inst.pk))[:120],
        comment=f"Replaces cancelled instrument #{inst.pk}")
    return copy


def clear_for_bank_debit(txn, user, expenses=None):
    """Debit-queue integration: when an imported bank DEBIT is matched to
    expense voucher(s), clear their outstanding payment instruments with the
    DEBIT'S DATE as the cleared date and link the debit for the
    reconciliation trail. Instruments already cleared (or already linked to a
    different debit) are left alone — no duplicates, ever. Returns the
    instruments cleared."""
    from cashbook.models import PaymentInstrument
    from django.db.models import Q
    if expenses is None:
        expenses = list(txn.matched_expenses.all()) \
            if hasattr(txn, "matched_expenses") else []
    exp_ids = [e.pk for e in expenses]
    if not exp_ids:
        return []
    qs = (PaymentInstrument.objects.filter(
            Q(expense_id__in=exp_ids) | Q(extra_expenses__id__in=exp_ids))
          .filter(status__in=PaymentInstrument.OUTSTANDING_STATES,
                  bank_transaction__isnull=True)
          .distinct())
    cleared = []
    for inst in qs:
        apply_event(inst, "CLEAR", user, on=txn.date,
                    bank_transaction=txn,
                    comment="Cleared by matched bank debit")
        cleared.append(inst)
    return cleared


def auto_clear_cheques_for_debits(txns, user):
    """Clear outstanding cheques the bank has now shown as debited.

    Called from the statement importer, over the DEBIT transactions it has just
    posted. A cheque number is exact — the bank issues each one once, prints it
    in the narration, and it is the same number written on the cheque stub — so a
    number match is not a guess and does not need a human to confirm it.

    The amount must agree as well. A number that matches with a DIFFERENT amount
    is a cheque that was altered, or partly paid, or misread — and that wants
    somebody's eyes on it, not a silent tick. It is left outstanding and shows up
    in the debit queue, where the existing suggestion machinery will offer it.

    An amount-only match is never auto-applied: two cheques for the same amount
    are perfectly ordinary, and guessing between them would clear the wrong one.
    """
    from cashbook.models import PaymentInstrument

    outstanding = list(
        PaymentInstrument.objects
        .filter(status__in=PaymentInstrument.OUTSTANDING_STATES,
                bank_transaction__isnull=True)
        .exclude(instrument_number=""))
    if not outstanding:
        return []

    cleared = []
    for txn in txns:
        if txn.direction != "DEBIT":
            continue
        narration = (txn.raw_narration or txn.reference or "").upper()
        if not narration:
            continue
        for inst in outstanding:
            num = (inst.instrument_number or "").strip()
            if not num:
                continue
            # match the number with and without its leading zeros — a bank prints
            # "CHQ No.000412" while a cheque book may be recorded as "412"
            variants = {num.upper(), num.lstrip("0").upper()}
            if not any(v and v in narration for v in variants):
                continue
            if inst.amount != txn.amount:
                # the number matches but the money does not — leave it for a human
                continue
            apply_event(inst, "CLEAR", user=user, on=txn.date,
                        comment=f"Cleared automatically: the bank's statement shows "
                                f"this cheque debited on {txn.date:%d %b %Y}.")
            inst.bank_transaction = txn
            inst.save(update_fields=["bank_transaction"])
            cleared.append(inst)
            outstanding.remove(inst)
            break
    return cleared


def suggest_instrument_for_debit(txn):
    """A cheap match suggestion for the debit queue: an outstanding instrument
    whose number appears in the debit's narration, else a unique
    exact-amount outstanding instrument. Suggestion only — never auto-applied."""
    from cashbook.models import PaymentInstrument
    qs = PaymentInstrument.objects.filter(
        status__in=PaymentInstrument.OUTSTANDING_STATES,
        bank_transaction__isnull=True)
    narration = (txn.raw_narration or "").upper()
    if narration:
        for inst in qs.exclude(instrument_number="")[:200]:
            if inst.instrument_number.upper() in narration:
                return inst, "number"
    exact = list(qs.filter(amount=txn.amount)[:2])
    if len(exact) == 1:
        return exact[0], "amount"
    return None, None
