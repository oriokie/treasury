"""Ingest a single bank transaction (from the real-time CBS webhook) using the
exact same rules as the statement importer: member matching, fund allocation,
split funds, database-level dedup, import-confirmation gating and close-aware
service-Sabbath assignment. Keeping this in one place means the live feed and the
file import can never drift apart in how they recognise a donation."""
from decimal import Decimal

from django.db import transaction as db_tx
from django.db.models import Q

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

    from statements.services.register import _is_mpesa_receipt

    # database-level dedup (the bank re-delivers until it gets a 2XX).
    #
    # A bank can share ONE reference across several genuinely-distinct movements:
    #   - a mobile-banking sweep batching payments, told apart by their unique
    #     M-Pesa receipts (on bank_receipt);
    #   - a journal batching charges, told apart by amount + direction (stamp
    #     duty 250, excise 300, cheque book 1,500 under one CB0170485260413).
    # So an unseen unique receipt is always new; and a core_ref match only counts
    # as a duplicate when the AMOUNT and DIRECTION also match — otherwise it is a
    # sibling line in the same batch and must not be dropped.
    if bank_receipt and Transaction.objects.filter(bank_receipt=bank_receipt).exists():
        return None, "duplicate"

    receipt_is_unique = _is_mpesa_receipt(bank_receipt)
    if not receipt_is_unique and core_ref:
        base = core_ref
        if Transaction.objects.filter(
                Q(core_ref__iexact=base) | Q(core_ref__istartswith=f"{base}-S"),
                amount=abs(amount),
                direction=direction).exists():
            return None, "duplicate"

    # avoid the core_ref UNIQUE collision when a batch shares one core_ref
    if core_ref and Transaction.objects.filter(
            Q(core_ref=core_ref) | Q(core_ref__startswith=f"{core_ref}-S")).exists():
        n = 1
        while Transaction.objects.filter(core_ref=f"{core_ref}-S{n}").exists():
            n += 1
        core_ref = f"{core_ref}-S{n}"

    is_credit = direction == Transaction.Direction.CREDIT
    member = dept = dev_group = split_fund = None
    campaign = None
    campaign_group = ""
    status = Transaction.Status.REVIEW

    loan_hit = None
    if is_credit:
        # Same loan recognition as the file importer (see importer.py): a loan
        # narration is a liability, never income, and never creates a Member.
        from loans.services.narration import detect_loan
        lp = detect_loan(reference)
        if lp is not None and lp.kind == "RECEIPT":
            if lp.fund_id:
                from loans.services.loans import intake_bank_receipt
                lt = intake_bank_receipt(
                    lp, date=date, amount=amount, reference=reference,
                    phone=phone, name=name, raw_narration=raw_narration,
                    core_ref=core_ref, bank_receipt=bank_receipt,
                    mpesa_ref=mpesa_ref or "", bank_account=bank_account)
                return lt.receipt_transaction, "created"
            loan_hit = lp   # fund unknown -> review queue, no Member created

    if is_credit and loan_hit is None:
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
            # DEV_GROUP_NA means "clearly development, but which group is
            # unknown from the reference text alone" — give a configured
            # campaign's member table a chance to pin down the exact group
            # from the payer's name/phone, same as when dept was never
            # resolved at all (and the same fix already applied to the file
            # importer — this webhook path was the other half of the same
            # bug, missed the first time round).
            dev_group_unknown = (resolver == "DEV_GROUP_NA")
            if dept is None or dev_group_unknown:
                # rules missed — try the campaign fallback (e.g. camp expenses)
                from giving.services.allocation import campaign_allocate
                campaign, campaign_group, cdept, cstatus = campaign_allocate(
                    reference, name, phone)
                if cdept is not None and (dept is None or cstatus == "AUTO"):
                    dept = cdept
                    status = (Transaction.Status.AUTO if cstatus == "AUTO"
                              else Transaction.Status.REVIEW)

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
        campaign=campaign, campaign_group=(campaign_group or ""),
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
