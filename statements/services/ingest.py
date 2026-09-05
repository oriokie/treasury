"""Ingest a single bank transaction (from the real-time CBS webhook) using the
exact same rules as the statement importer: member matching, fund allocation,
split funds, database-level dedup, reversal recognition, import-confirmation
gating and close-aware service-Sabbath assignment. Keeping this in one place
means the live feed and the file import can never drift apart in how they
recognise a donation."""
import datetime as dt
from decimal import Decimal

from django.db import transaction as db_tx
from django.db.models import Q
from django.utils import timezone

from giving.models import Transaction, SplitFund
from giving.services.allocation import allocate
from members.services.matching import match_or_create_member
from statements.models import BankAccount
from statements.services.importer import _resolve   # reuse, never diverge


# How far back to FETCH candidates for the reversal check below. This is not the
# rule about how close in time a pair must be — that rule belongs to the
# importer's `_reversal_row_pairs` and is asked, not restated. This is only how
# wide a net to throw at the database before asking it, and it is deliberately
# wider than the rule so that tightening or loosening the rule never silently
# stops working here because the fetch got there first.
_REVERSAL_FETCH_DAYS = 31


def _movement_row(*, date, amount, direction, mpesa_ref, raw_narration):
    """One movement in the shape the statement importer's row-pairing reads.

    The importer works on parsed statement rows (credit/debit columns, a date, a
    reference and the narration); the live feed works on Transactions. Rendering
    a Transaction back into a row is what lets both be handed to the same
    predicate instead of a second one being written for this side.
    """
    amt = abs(amount or Decimal(0))
    is_credit = direction == Transaction.Direction.CREDIT
    return {"date": date,
            "credit": amt if is_credit else Decimal(0),
            "debit": Decimal(0) if is_credit else amt,
            "mpesa_ref": (mpesa_ref or "").strip().upper() or None,
            "raw_narration": raw_narration or ""}


def _reversal_counterpart(*, date, amount, direction, mpesa_ref, raw_narration,
                          bank_account):
    """The earlier transaction this inbound event is undoing, or None.

    A bank entry made in ERROR and then undone is a NON-EVENT: the bank credits
    the church by mistake and takes it back, and nothing was really received.
    Posting the credit as income overstates a church's giving by the amount of
    the bank's own mistake — which is why the file importer pairs a statement's
    mistaken entry with its reversal and posts neither (`run_import`, via
    `_reversal_row_pairs`).

    The live feed could not do that, and did not try. It sees ONE event per
    request, and the reversal arrives minutes or hours after the entry it undoes,
    in a separate call, by which time the original has already been allocated,
    posted to the ledger and possibly receipted. So the pairing has to run the
    other way round: when an event arrives, look BACK for its counterpart among
    the transactions this account has recently taken in. This module's docstring
    promised the live feed and the file import could never drift apart on how
    they read a bank line; on reversals they had never agreed at all.

    What COUNTS as a pair is not decided here. The two movements are rendered
    back into statement rows and handed to the importer's own
    `_reversal_row_pairs`, so there is still exactly one definition of "these two
    entries are the bank undoing itself" — opposite direction, equal amount,
    close in time, and either a shared bank reference or a narration that says
    so. A keyword is required, and that conservatism is the point: a false
    positive here suppresses real income, which is far worse than leaving a
    genuine reversal unrecognised.
    """
    from statements.services.importer import _reversal_row_pairs

    if amount is None or not amount:
        return None
    opposite = (Transaction.Direction.DEBIT
                if direction == Transaction.Direction.CREDIT
                else Transaction.Direction.CREDIT)
    window = dt.timedelta(days=_REVERSAL_FETCH_DAYS)
    # Only the bank's own entries on the SAME account can be reversed by the
    # bank, and neither half of a pair already recognised may be re-used: a
    # reversal reverses one entry, not every entry of that amount. Most recent
    # first, because the likeliest thing a bank is undoing is the last entry of
    # its kind rather than one from three weeks ago.
    candidates = (Transaction.objects
                  .filter(channel=Transaction.Channel.BANK, direction=opposite,
                          amount=abs(amount), bank_account=bank_account,
                          is_reversed=False, is_reversal=False,
                          date__gte=date - window, date__lte=date + window)
                  .order_by("-date", "-id"))
    incoming = _movement_row(date=date, amount=amount, direction=direction,
                             mpesa_ref=mpesa_ref, raw_narration=raw_narration)
    for cand in candidates[:50]:
        earlier = _movement_row(date=cand.date, amount=cand.amount,
                                direction=cand.direction, mpesa_ref=cand.mpesa_ref,
                                raw_narration=cand.raw_narration)
        if _reversal_row_pairs([incoming, earlier]):
            return cand
    return None


def ingest_event(*, date, amount, direction, reference, phone, name, raw_narration,
                 core_ref, bank_receipt=None, mpesa_ref=None, bank_account=None):
    """Create the Transaction(s) for one inbound event.

    Returns (transaction, outcome) where outcome is 'created' or 'duplicate'.
    A duplicate (already-seen core_ref / bank_receipt) creates nothing.

    An event that reverses an earlier one is still 'created': the bank really did
    make the entry and the feed log must show it was handled. It is created
    flagged as a reversal, and the entry it undoes is flagged reversed, so
    neither counts as income anywhere.
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

    # Is the bank undoing one of its own earlier entries? If so this event is one
    # half of a NON-EVENT and must not be allocated to a fund, matched to a
    # member or counted as income — and neither must the entry it undoes, which
    # is already on the books. Both are marked below; see `_reversal_counterpart`
    # for why the live feed has to look backwards where the file importer can
    # simply pair two rows it holds at once.
    reversal_of = _reversal_counterpart(
        date=date, amount=amount, direction=direction, mpesa_ref=mpesa_ref,
        raw_narration=raw_narration, bank_account=bank_account)

    loan_hit = None
    if is_credit and reversal_of is None:
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

    if is_credit and loan_hit is None and reversal_of is None:
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
                code_pinned = False
                try:
                    from pledges.services.codes import pledge_code_allocate
                    _p, pdept, pstatus = pledge_code_allocate(reference)
                    if pdept is not None:
                        dept = pdept
                        status = Transaction.Status.AUTO
                        code_pinned = True
                except Exception:  # noqa: BLE001
                    pass
                if not code_pinned:
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
        # The bank's own undoing of an earlier entry is recorded — the money did
        # move, and the register must say what the bank said — but it is not
        # income, and `TransactionQuerySet.active` / `.confirmed_credits` already
        # exclude both halves of a reversed pair everywhere it matters. Set at
        # creation rather than patched on afterwards so the ledger's post_save
        # never posts an entry that would only have to be withdrawn again.
        # Both halves are marked REVERSED, not one of them REVERSAL. The
        # distinction matters because `signed_cash_case` signs an `is_reversal`
        # row negative whatever its direction: right for a mistaken credit
        # taken back by a debit, and wrong for the opposite shape, where the
        # bank debits in error and puts the money back with a CREDIT — that
        # corrective credit would have signed negative too and the pair would
        # have read as minus twice the amount instead of nothing.
        # `is_reversed` carries no sign of its own, so each row signs by its own
        # direction and the pair nets to zero either way round, while
        # `TransactionQuerySet.active`/`.confirmed_credits` still exclude both
        # from income and the ledger still declines to journal either.
        is_reversed=reversal_of is not None,
        reversed_at=timezone.now() if reversal_of is not None else None,
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
        if reversal_of is not None:
            # The other half. It was posted as real when it arrived — allocated
            # to a fund and journalled — because nothing then said it was about
            # to be undone. Saving it marks it reversed AND, through the ledger's
            # post_save, withdraws the journal entry that recognised the income:
            # `post_transaction` replaces the entries for a transaction and then
            # declines to post anything for a reversed one.
            reversal_of.is_reversed = True
            reversal_of.reversed_at = timezone.now()
            reversal_of.save(update_fields=["is_reversed", "reversed_at"])
        # Pledge match (incl. match_code in the reference) — same hook as file
        # import. Best-effort; never break ingest.
        if (is_credit and confirmed and not t.is_reversed
                and not getattr(t, "excluded_from_income", False)):
            try:
                from pledges.services.matching import handle_new_contribution
                handle_new_contribution(t)
            except Exception:  # noqa: BLE001
                pass
        return t, "created"
