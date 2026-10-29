"""Apply optional ``StkGiver`` data from Co-op bank-feed relays.

When an M-Pesa STK gift settles, Co-op's credit has no giver name. The church
app (or a relay) may attach ``StkGiver`` on the credit, or send a late update
keyed by ``TransactionId``. A late update may arrive before the credit — we
keep it and apply it when the credit lands.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.db import transaction as db_tx
from django.utils import timezone

from giving.models import ContributionAttempt, Transaction
from members.services.matching import is_person_name
from statements.models import BankEvent, PendingStkGiver


def parse_stk_giver(raw):
    """Return a normalised dict or None if ``raw`` is not a usable object."""
    if not isinstance(raw, dict):
        return None
    name = " ".join(str(raw.get("Name") or "").split()).strip()
    phone = "".join(c for c in str(raw.get("Phone") or "") if c.isdigit())
    receipt = str(raw.get("Receipt") or "").strip().upper()
    reference = str(raw.get("Reference") or "").strip()
    purpose = str(raw.get("Purpose") or "").strip()
    amount = raw.get("Amount")
    try:
        amount_s = f"{Decimal(str(amount)):.2f}" if amount not in (None, "") else ""
    except (InvalidOperation, TypeError, ValueError):
        amount_s = str(amount or "").strip()
    group = raw.get("Group")
    try:
        group_i = int(group) if group not in (None, "") else None
    except (TypeError, ValueError):
        group_i = None
    if not (name or phone or receipt):
        return None
    return {
        "Name": name[:120],
        "Phone": phone[:12],
        "Receipt": receipt[:30],
        "Reference": reference[:60],
        "Amount": amount_s[:20],
        "Purpose": purpose[:40],
        "Group": group_i,
    }


def apply_stk_giver_to_transaction(txn, giver, *, bank_name=""):
    """Attach giver details to ``txn``. Prefer an existing bank narration name.

    Per Co-op docs: if the bank narration already has a name, keep that as
    ``payer_name`` and record the form name as \"name given on the form\".
    """
    if txn is None or not giver:
        return False
    form_name = (giver.get("Name") or "").strip()
    phone = (giver.get("Phone") or "").strip()
    receipt = (giver.get("Receipt") or "").strip()
    changed = []
    bank_name = " ".join(str(bank_name or "").split()).strip()
    bank_is_person = is_person_name(bank_name)
    existing = (txn.payer_name or "").strip()
    existing_is_person = is_person_name(existing)

    if bank_is_person:
        if existing.upper() != bank_name.upper():
            txn.payer_name = bank_name
            changed.append("payer_name")
        if form_name and form_name.upper() != bank_name.upper():
            note = f"Name given on the form: {form_name}"
            raw = txn.raw_narration or ""
            if note not in raw:
                txn.raw_narration = (raw + ("\n" if raw else "") + note)[:500]
                changed.append("raw_narration")
    elif form_name and (not existing or not existing_is_person):
        # No real bank name — use the form name (replace a mistaken reference).
        if existing.upper() != form_name.upper():
            txn.payer_name = form_name
            changed.append("payer_name")

    if phone and not (txn.payer_phone or "").strip():
        txn.payer_phone = phone[:12]
        changed.append("payer_phone")
    if receipt:
        if not (txn.mpesa_ref or "").strip():
            txn.mpesa_ref = receipt[:30]
            changed.append("mpesa_ref")
        if not (txn.bank_receipt or "").strip():
            txn.bank_receipt = receipt[:30]
            changed.append("bank_receipt")

    if not changed:
        return False
    txn.save(update_fields=list(dict.fromkeys(changed)))
    return True


def match_local_stk_attempt(receipt, phone=""):
    """Find a contribution-link attempt that matches this bank credit."""
    receipt = (receipt or "").strip().upper()
    related = ("member", "member__dev_group", "fund")
    if receipt:
        attempt = (ContributionAttempt.objects
                   .filter(mpesa_receipt__iexact=receipt)
                   .select_related(*related)
                   .order_by("-created_at")
                   .first())
        if attempt:
            return attempt
    phone = "".join(c for c in str(phone or "") if c.isdigit())
    if phone:
        return (ContributionAttempt.objects
                .filter(phone=phone, status=ContributionAttempt.Status.SUCCESS)
                .filter(mpesa_receipt="")
                .select_related(*related)
                .order_by("-created_at")
                .first())
    return None


def giver_from_attempt(attempt):
    if attempt is None:
        return None
    group_no = None
    member = attempt.member
    if member is not None and getattr(member, "dev_group_id", None):
        group_no = getattr(member.dev_group, "number", None)
    return parse_stk_giver({
        "Name": attempt.giver_name or "",
        "Phone": attempt.phone or "",
        "Receipt": attempt.mpesa_receipt or "",
        "Reference": attempt.code or "",
        "Amount": str(attempt.amount),
        "Purpose": "DEVGR",
        "Group": group_no,
    })


def apply_to_bank_event(evt, giver=None, *, bank_name=""):
    """Apply ``giver`` (or a local STK match) to the event's transaction."""
    if evt is None:
        return False
    txn = evt.transaction
    if txn is None and evt.cbs_transaction_id:
        txn = Transaction.objects.filter(core_ref=evt.cbs_transaction_id).first()
        if txn:
            evt.transaction = txn
            evt.save(update_fields=["transaction"])

    receipt = ""
    phone = ""
    if giver:
        receipt = giver.get("Receipt") or ""
        phone = giver.get("Phone") or ""
    if not receipt and txn:
        receipt = txn.mpesa_ref or txn.bank_receipt or ""
    if not phone and txn:
        phone = txn.payer_phone or ""

    if giver is None:
        attempt = match_local_stk_attempt(receipt, phone)
        giver = giver_from_attempt(attempt)
        if attempt and txn and attempt.member_id:
            # Credit the member whose link was used, not a payer matched by phone.
            fields = []
            if txn.member_id != attempt.member_id:
                txn.member_id = attempt.member_id
                fields.append("member_id")
            if not txn.attributed_via_code:
                txn.attributed_via_code = True
                fields.append("attributed_via_code")
            if attempt.code and not (txn.reference or "").strip():
                txn.reference = attempt.code
                fields.append("reference")
            if attempt.fund_id and not txn.department_id:
                txn.department_id = attempt.fund_id
                fields.append("department_id")
            if fields:
                txn.save(update_fields=fields)

    if not giver:
        return False
    return apply_stk_giver_to_transaction(txn, giver, bank_name=bank_name)


def store_or_apply_update(transaction_id, giver):
    """Late giver update: apply now, or hold until the credit arrives."""
    transaction_id = str(transaction_id or "").strip()
    giver = parse_stk_giver(giver)
    if not transaction_id or not giver:
        return False, "Need TransactionId and StkGiver."

    with db_tx.atomic():
        evt = (BankEvent.objects
               .select_for_update()
               .filter(cbs_transaction_id=transaction_id)
               .first())
        if evt is not None:
            apply_to_bank_event(evt, giver)
            PendingStkGiver.objects.filter(cbs_transaction_id=transaction_id).delete()
            return True, "Applied."

        PendingStkGiver.objects.update_or_create(
            cbs_transaction_id=transaction_id,
            defaults={
                "giver": giver,
                "received_at": timezone.now(),
            },
        )
        return True, "Stored; will apply when the credit arrives."


def apply_pending_for(transaction_id, evt, *, bank_name=""):
    """If a late update was stored for this credit, apply it now."""
    transaction_id = str(transaction_id or "").strip()
    pending = PendingStkGiver.objects.filter(cbs_transaction_id=transaction_id).first()
    giver = parse_stk_giver(pending.giver) if pending else None
    applied = apply_to_bank_event(evt, giver, bank_name=bank_name)
    if pending:
        pending.delete()
    return applied
