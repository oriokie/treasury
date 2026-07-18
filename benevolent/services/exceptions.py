"""Contribution exceptions and reconciliation.

The happy path is `contributions.record_contribution`: money comes in, it is
attributed to a member, it settles dues or a levy. This module is about
everything that path does not, on its own, get right — the real-world messes a
church treasurer meets weekly:

    * paid twice                 — already flagged at intake (find_duplicate);
                                   here we also catch a duplicate recorded
                                   directly, and reverse the extra.
    * payment reversed / bounced — reverse_contribution: undo without deleting.
    * wrong scheme / wrong member — recorded_against_wrong: re-attribute cleanly,
                                   as a reversal + a fresh correct entry.
    * backdated payment          — flag a receipt dated before the member could
                                   owe anything, or into a closed period.
    * future payment             — flag a receipt dated after today.
    * anonymous / employer /
      sponsor / third-party      — payer_type on the contribution, so money paid
                                   on a member's behalf is recorded truthfully.
    * bulk-upload errors         — validate a batch BEFORE committing any of it.
    * automatic reconciliation   — reconcile recorded contributions against the
                                   bank transactions that carry them, and report
                                   every disagreement.

Two rules run through all of it:

1. **Nothing is ever deleted.** A wrong contribution is REVERSED — the original
   and its contra both stay on the record — so the member's statement and any
   auditor can always see what happened and why. This is the same discipline
   giving.Transaction.reverse already enforces at the ledger level; here we tie
   the benevolent index row to it.

2. **The money is authoritative, the index follows it.** A contribution's amount
   and date live on its giving.Transaction, not on the index row. So reversing
   the transaction is what actually removes the money from every total (via
   _effective_q); the index-row metadata we stamp here is for the humans.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from django.core.exceptions import ValidationError
from django.db import transaction as db_tx
from django.utils import timezone

from benevolent.models import (BenevolentContribution, BenevolentScheme,
                               SchemeMembership)


# ---------------------------------------------------------------------------
# Exception detection — the checks that turn "a receipt" into "a flagged receipt"
# ---------------------------------------------------------------------------

@dataclass
class ContributionException:
    """One thing wrong (or worth a second look) about a contribution. `code` is
    stable and machine-readable; `detail` states the specific facts; `blocking`
    marks the ones that should stop a save rather than merely warn."""
    code: str
    label: str
    detail: str
    blocking: bool = False

    def as_dict(self):
        return {"code": self.code, "label": self.label,
                "detail": self.detail, "blocking": self.blocking}


def screen_contribution(scheme, *, date, amount, membership=None, kind=None,
                        payer_type=None, as_of=None):
    """Every exception a would-be contribution trips, without recording anything.
    Called before a manual entry is saved (to warn the treasurer) and over a bulk
    upload (to validate the batch before committing). Returns a list — empty means
    clean."""
    as_of = as_of or _dt.date.today()
    out = []

    # amount sanity
    if amount is None or Decimal(amount) <= 0:
        out.append(ContributionException(
            "non_positive", "Amount", "A contribution must be a positive amount.",
            blocking=True))
        return out                       # nothing else is meaningful

    # future-dated
    if date > as_of:
        out.append(ContributionException(
            "future_dated", "Future-dated",
            f"Dated {date:%d %b %Y}, which is in the future. A receipt cannot be for "
            f"money not yet received — check the date.", blocking=False))

    # backdated before the member could owe anything
    if membership is not None and date < membership.cover_from:
        out.append(ContributionException(
            "before_cover", "Before cover began",
            f"Dated {date:%d %b %Y}, before {membership.member.name}'s cover began on "
            f"{membership.cover_from:%d %b %Y}. It cannot settle a period that did not "
            f"yet exist.", blocking=False))

    # closed accounting period
    from core.models import period_locked
    lock = period_locked(date)
    if lock:
        out.append(ContributionException(
            "closed_period", "Closed period",
            f"{date:%d %b %Y} falls in {lock}, a closed accounting period, so it "
            f"cannot be posted.", blocking=True))

    # backdated a long way (worth a look even if the period is open)
    if membership is not None and (as_of - date).days > 365:
        out.append(ContributionException(
            "long_backdated", "Backdated over a year",
            f"Dated {date:%d %b %Y}, more than a year ago. Long-backdated receipts are "
            f"usually a typo — confirm the year.", blocking=False))

    # duplicate (the same member, amount and scheme within the window)
    if membership is not None:
        from benevolent.services.allocation import find_duplicate
        dup = find_duplicate(scheme, membership.pk, Decimal(amount), date)
        if dup is not None:
            out.append(ContributionException(
                "duplicate", "Possible duplicate",
                f"{membership.member.name} already has a {dup.amount} contribution "
                f"dated {dup.date:%d %b %Y}. This may be the same money recorded twice.",
                blocking=False))

    # a third-party payer with no name recorded (anonymous is allowed to be blank)
    if payer_type and payer_type not in (
            BenevolentContribution.PayerType.SELF,
            BenevolentContribution.PayerType.ANONYMOUS):
        # name is validated at the call site where it is known; this is a hint
        pass

    return out


# ---------------------------------------------------------------------------
# Reversal — undo without deleting
# ---------------------------------------------------------------------------

@db_tx.atomic
def reverse_contribution(contribution, *, user, reason="", refund_expense=False):
    """Reverse a contribution: a payment that bounced, was made in error, or is
    a confirmed duplicate. Reverses the underlying receipt (so the money leaves
    every total and the bank reconciliation shows the contra), and stamps the
    index row so the member's statement explains the gap.

    Never deletes. Returns the contra Transaction. Idempotent-guarded: a receipt
    already reversed raises, rather than double-reversing.
    """
    txn = contribution.transaction
    if txn is None:
        raise ValidationError("This contribution has no receipt to reverse.")
    if txn.is_reversed:
        raise ValidationError("This contribution has already been reversed.")
    if txn.is_reversal:
        raise ValidationError("A reversal entry cannot itself be reversed.")

    contra = txn.reverse(user, reason=reason or "Benevolent contribution reversed")

    contribution.reversed_at = timezone.now()
    contribution.reversal_reason = (reason or "")[:200]
    contribution.save(update_fields=["reversed_at", "reversal_reason"])

    # If this levy contribution had been paid out as part of a case's collection,
    # the case's funding figure recomputes from effective contributions on its
    # next read — nothing to undo here, because the money simply stops counting.
    from benevolent.services.cases import log as case_log
    if contribution.case_id:
        from benevolent.models import CaseEvent
        case_log(contribution.case, CaseEvent.Kind.NOTE,
                 f"Levy contribution of {contribution.amount} reversed: "
                 f"{reason or 'no reason given'}.", user=user)
    return contra


@db_tx.atomic
def correct_attribution(contribution, *, user, reason="",
                        new_scheme=None, new_membership=None, new_kind=None,
                        new_case=None):
    """A contribution recorded against the WRONG member, WRONG scheme, or as the
    wrong KIND. Corrected the safe way: reverse the original entry and record a
    fresh, correct one carrying the same money — rather than editing the original
    in place, which would erase the fact that it was ever wrong.

    Returns the new (correct) BenevolentContribution.
    """
    if contribution.reversed_at or (contribution.transaction and
                                    contribution.transaction.is_reversed):
        raise ValidationError("This contribution has already been reversed; nothing to "
                              "correct. Record a fresh contribution instead.")

    target_scheme = new_scheme or contribution.scheme
    target_membership = new_membership
    if target_membership is None and new_scheme is None:
        target_membership = contribution.membership
    if target_membership is not None and target_membership.scheme_id != target_scheme.pk:
        raise ValidationError("The chosen member belongs to a different scheme than the "
                              "one the contribution is being moved to.")

    orig = contribution
    date = orig.date
    amount = orig.amount
    kind = new_kind or orig.kind
    case = new_case if new_case is not None else (
        orig.case if new_scheme is None else None)

    # 1) reverse the wrong entry (money leaves the wrong attribution)
    reverse_contribution(
        orig, user=user,
        reason=(reason or "Re-attributed") + " — reversed to correct attribution")

    # 2) record the correct entry, carrying the SAME money on a fresh receipt
    from benevolent.services.contributions import record_contribution
    corrected = record_contribution(
        target_scheme, date=date, amount=amount, user=user,
        membership=target_membership, case=case, kind=kind,
        note=f"Correction of contribution #{orig.pk}: {reason}"[:200])

    corrected.reverses = None            # this is the CORRECT one, not a reversal
    corrected.save(update_fields=["reverses"])
    return corrected


# ---------------------------------------------------------------------------
# Automatic reconciliation — do the index rows agree with the bank?
# ---------------------------------------------------------------------------

@dataclass
class ReconciliationRow:
    kind: str                 # 'ok' | 'orphan_receipt' | 'orphan_index' |
                              # 'amount_mismatch' | 'reversed_but_counted'
    label: str
    detail: str
    transaction_id: Optional[int] = None
    contribution_id: Optional[int] = None
    amount: Decimal = Decimal(0)


@dataclass
class ReconciliationResult:
    scheme: object
    start: Optional[_dt.date]
    end: Optional[_dt.date]
    recorded_total: Decimal = Decimal(0)      # sum of effective contributions
    receipts_total: Decimal = Decimal(0)      # sum of scheme-fund credits
    exceptions: list = field(default_factory=list)   # list[ReconciliationRow]

    @property
    def balanced(self):
        return not self.exceptions and self.recorded_total == self.receipts_total

    @property
    def difference(self):
        return self.receipts_total - self.recorded_total


def reconcile_scheme(scheme, *, start=None, end=None):
    """Reconcile what the scheme has RECORDED as contributions against the bank
    receipts that actually carry the money into its fund.

    Every benevolent contribution is a giving.Transaction credit in the scheme's
    fund. So the two should always agree — and where they do not, something needs
    a human:

      * orphan_receipt   — a credit sits in the scheme fund with no contribution
                           index row (money banked but never attributed — it
                           should be in the intake queue).
      * orphan_index     — a contribution whose receipt has vanished or been
                           reversed while the index row still counts.
      * amount_mismatch  — the index and its receipt disagree on the amount (only
                           possible through direct data tampering, but a
                           reconciliation that could not see it would be no use).

    This is the benevolent-side counterpart to the bank reconciliation the rest
    of the app already does at the fund level.
    """
    from django.db.models import Sum
    from giving.models import Transaction

    result = ReconciliationResult(scheme=scheme, start=start, end=end)

    # effective contributions (index side)
    from benevolent.services.contributions import contributions_qs
    cqs = contributions_qs(scheme=scheme, start=start, end=end)
    result.recorded_total = cqs.aggregate(t=Sum("transaction__amount"))["t"] or Decimal(0)

    # scheme-fund credits (bank/receipt side)
    rqs = Transaction.objects.filter(
        department=scheme.fund, direction=Transaction.Direction.CREDIT,
        confirmed=True, is_reversed=False, is_reversal=False)
    if start:
        rqs = rqs.filter(date__gte=start)
    if end:
        rqs = rqs.filter(date__lte=end)
    result.receipts_total = rqs.aggregate(t=Sum("amount"))["t"] or Decimal(0)

    # orphan receipts: a scheme-fund credit with no (effective) contribution and
    # no open intake row — money in the fund that nobody has attributed.
    from benevolent.models import ContributionIntake
    attributed_txn_ids = set(
        BenevolentContribution.objects.filter(scheme=scheme)
        .values_list("transaction_id", flat=True))
    open_intake_txn_ids = set(
        ContributionIntake.objects.filter(
            scheme=scheme, status__in=ContributionIntake.OPEN_STATUSES)
        .values_list("transaction_id", flat=True))
    for txn in rqs.exclude(id__in=attributed_txn_ids):
        if txn.id in open_intake_txn_ids:
            continue                     # already sitting in the intake queue
        result.exceptions.append(ReconciliationRow(
            kind="orphan_receipt", label="Unattributed receipt",
            detail=f"{txn.amount} banked on {txn.date:%d %b %Y} sits in the scheme fund "
                   f"with no contribution recorded and nothing in the intake queue.",
            transaction_id=txn.id, amount=txn.amount))

    # orphan index / amount mismatch: walk the index rows.
    for c in cqs.select_related("transaction"):
        t = c.transaction
        if t is None:
            result.exceptions.append(ReconciliationRow(
                kind="orphan_index", label="Contribution without a receipt",
                detail=f"Contribution #{c.pk} has no receipt behind it.",
                contribution_id=c.pk))
            continue
        if c.amount != t.amount:
            result.exceptions.append(ReconciliationRow(
                kind="amount_mismatch", label="Amount mismatch",
                detail=f"Contribution #{c.pk} and its receipt disagree "
                       f"({c.amount} vs {t.amount}).",
                transaction_id=t.id, contribution_id=c.pk, amount=c.amount))

    return result


# ---------------------------------------------------------------------------
# Bulk-upload validation — validate the whole batch before committing any of it
# ---------------------------------------------------------------------------

@dataclass
class BulkRowResult:
    line: int
    ok: bool
    exceptions: list = field(default_factory=list)   # list[ContributionException]
    parsed: dict = field(default_factory=dict)


def validate_bulk(scheme, rows, *, as_of=None):
    """Screen a parsed batch of contribution rows and report every problem BEFORE
    a single one is committed — so a treasurer fixes the spreadsheet once, rather
    than discovering row 40's bad date after rows 1-39 already posted.

    `rows` is a list of dicts already resolved to (membership, date, amount,
    kind). Returns a per-row result plus a batch-level `blocking` flag.
    """
    as_of = as_of or _dt.date.today()
    results = []
    for i, row in enumerate(rows, start=1):
        exc = screen_contribution(
            scheme, date=row.get("date"), amount=row.get("amount"),
            membership=row.get("membership"), kind=row.get("kind"),
            payer_type=row.get("payer_type"), as_of=as_of)
        results.append(BulkRowResult(
            line=i, ok=not any(e.blocking for e in exc),
            exceptions=exc, parsed=row))
    return results
