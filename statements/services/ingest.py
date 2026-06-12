"""Ingest a single bank transaction (from the real-time CBS webhook) using the
exact same rules as the statement importer: member matching, fund allocation,
split funds, database-level dedup, import-confirmation gating and close-aware
service-Sabbath assignment. Keeping this in one place means the live feed and the
file import can never drift apart in how they recognise a donation."""
from decimal import Decimal

from django.db import transaction as db_tx

from giving.models import Transaction, SplitFund
from giving.services.allocation import allocate
from members.services.matching import match_or_create_member
from statements.models import BankAccount
from statements.services.importer import _resolve   # reuse, never diverge


def ingest_event(*, date, amount, direction, reference, phone, name, raw_narration,
                 core_ref, bank_receipt=None, mpesa_ref=None, bank_account=None):
    """Create the Transaction(s) for one inbound event.

    Returns (transaction, outcome) where outcome is 'created' or 'duplicate'.
    A duplicate (already-seen core_ref / bank_receipt) creates nothing.
    """
    from core.models import SiteConfig, service_sabbath_for
    from core.utils import sabbath_week_of

    cfg = SiteConfig.get()
    require_confirm = cfg.require_import_confirmation
    bank_account = bank_account or BankAccount.get_default()

    # normalise dedup keys to uppercase so deduplication is exact regardless of
    # database collation (consistent with the statement importer/parser).
    core_ref = (core_ref or "").strip().upper() or None
    bank_receipt = (bank_receipt or "").strip().upper() or None
    mpesa_ref = (mpesa_ref or "").strip().upper() or None

    # database-level dedup (the bank re-delivers until it gets a 2XX)
    if core_ref and Transaction.objects.filter(core_ref=core_ref).exists():
        return None, "duplicate"
    if bank_receipt and Transaction.objects.filter(bank_receipt=bank_receipt).exists():
        return None, "duplicate"
    if mpesa_ref and Transaction.objects.filter(mpesa_ref=mpesa_ref).exists():
        return None, "duplicate"

    is_credit = direction == Transaction.Direction.CREDIT
    member = dept = dev_group = split_fund = None
    status = Transaction.Status.REVIEW

    if is_credit:
        member, _ = match_or_create_member(name, phone)
        resolver, alloc_status = allocate(reference, date)
        if isinstance(resolver, SplitFund):
            split_fund = resolver
            status = (Transaction.Status.AUTO if alloc_status == "AUTO"
                      else Transaction.Status.LEARNED)
        else:
            dept, dev_group = _resolve(resolver)
            status = ((Transaction.Status.AUTO if alloc_status == "AUTO"
                       else Transaction.Status.LEARNED)
                      if dept is not None else Transaction.Status.REVIEW)

    confirmed = True
    if require_confirm and status in (Transaction.Status.AUTO, Transaction.Status.LEARNED):
        confirmed = False

    svc = service_sabbath_for(date)
    common = dict(
        date=date, sabbath_week=sabbath_week_of(svc), service_sabbath=svc,
        channel=Transaction.Channel.BANK, direction=direction, member=member,
        reference=(reference or "")[:60], payer_name=(name or "")[:120],
        payer_phone=(phone or "")[:12], mpesa_ref=(mpesa_ref or "")[:30],
        allocation_status=status, bank_account=bank_account, confirmed=confirmed,
        raw_narration=raw_narration or "")

    with db_tx.atomic():
        if split_fund is not None:
            parts = split_fund.split(amount)
            first = None
            for i, (pdept, pamt) in enumerate(parts):
                t = Transaction.objects.create(
                    amount=pamt, department=pdept,
                    core_ref=core_ref if i == 0 else
                             (f"{core_ref}-S{i}" if core_ref else None),
                    bank_receipt=bank_receipt if i == 0 else None, **common)
                first = first or t
            return first, "created"
        t = Transaction.objects.create(
            amount=amount, department=dept, dev_group=dev_group,
            core_ref=core_ref, bank_receipt=bank_receipt, **common)
        return t, "created"
