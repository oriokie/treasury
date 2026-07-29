"""The Contribution Engine.

Everything that can happen to a member's money, and everything that can happen to
what they owe — with the two kept firmly apart.

    MONEY IN     a receipt. `giving.Transaction`, exactly as in Phase 1.
                 Registration fees, renewal fees, dues, levies, voluntary gifts,
                 payment of a penalty.

    MONEY OUT    a payment voucher. `cashbook.Expense`, exactly as a benefit is.
                 A refund.

    OBLIGATIONS  no money at all. `MemberAdjustment`. Penalties charged, dues
                 waived, debts written off, corrections made. NOTHING POSTS.

The third of those is the one that gets built wrong, and it gets built wrong in the
same way every time: somebody books a waiver as an expense, or a penalty as income,
and the fund quietly starts reporting money that does not exist. See
`models_contrib.MemberAdjustment` for the argument in full.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction as db_tx
from django.utils import timezone

from benevolent.models import (BenevolentContribution, BenevolentScheme,
                               BenevolentSettings, ContributionIntake,
                               ContributionRefund, ContributionRule, MemberAdjustment,
                               SchemeMembership, SchemePolicy)
from benevolent.services import allocation as alloc_svc
from benevolent.services import contributions as contrib_svc


# ---------------------------------------------------------------------------
# Policy-driven validation
# ---------------------------------------------------------------------------

def validate(scheme, *, kind, membership=None, case=None, amount=None, date=None):
    """Is this contribution one the policy actually permits?

    Returns a list of problems, in words. Deliberately returns rather than raises,
    because the intake path wants to SHOW a treasurer what is wrong and let them
    fix it, while the manual path wants to refuse — and both should ask the same
    question of the same function rather than each deciding for itself what is
    legal.
    """
    date = date or _dt.date.today()
    problems = []
    policy = scheme.policy_on(date)

    if not scheme.accepts_contributions:
        problems.append(
            f"{scheme.name} is {scheme.get_status_display().lower()} and is not "
            f"accepting contributions.")
    if policy is None:
        problems.append(
            f"No policy was in force on {date:%d %b %Y}, so there is nothing to say "
            f"what is owed or permitted.")
        return problems

    K = BenevolentContribution.Kind
    periodic = (SchemePolicy.ContributionMode.FIXED_PERIODIC,
                SchemePolicy.ContributionMode.HYBRID)
    leviable = (SchemePolicy.ContributionMode.PER_CASE_LEVY,
                SchemePolicy.ContributionMode.HYBRID)

    if kind == K.DUES and policy.contribution_mode not in periodic:
        problems.append(
            f"This scheme has no periodic dues (its contribution mode is "
            f"{policy.get_contribution_mode_display().lower()}), so money cannot be "
            f"receipted as dues. It is probably a voluntary contribution.")
    if kind == K.LEVY:
        if policy.contribution_mode not in leviable:
            problems.append(
                f"This scheme does not levy per case, so money cannot be receipted as "
                f"a levy.")
        if case is None:
            problems.append("A levy has to say which case it was raised for.")
        elif case.status == case.Status.DRAFT:
            problems.append(
                f"{case.number} is still a draft — it has not been submitted for "
                f"review yet, so there is nothing settled to levy against.")
    if kind == K.REGISTRATION and not policy.registration_fee:
        problems.append("This policy charges no registration fee.")
    if kind == K.RENEWAL and not policy.renewal_fee:
        problems.append("This policy charges no renewal fee.")

    if kind in BenevolentContribution.OBLIGATIONS and membership is None:
        problems.append(
            f"A {kind.lower()} is a member meeting an obligation, so it must say which "
            f"member. Money from someone with no membership is a donation.")

    if membership is not None:
        if membership.scheme_id != scheme.pk:
            problems.append(
                f"{membership.number} belongs to {membership.scheme.code}, not "
                f"{scheme.code}.")
        if membership.status in SchemeMembership.ENDED_STATUSES \
                and kind != K.DONATION:
            problems.append(
                f"{membership.member.name} is "
                f"{membership.get_status_display().lower()} and owes the scheme nothing. "
                f"Money from them is a donation, not a contribution.")

    if case is not None and case.scheme_id != scheme.pk:
        problems.append(f"{case.number} belongs to a different scheme.")
    if case is not None and case.status not in case.LEVIABLE_STATUSES:
        # Only a case that never paid out has no levy to collect. A paid or
        # closed case is exactly when the levy money arrives, so refusing it
        # here left a member's contribution with nowhere to go.
        problems.append(
            f"{case.number} was {case.get_status_display().lower()} — no money "
            f"left the fund for it, so there is no levy to collect.")

    if amount is not None and Decimal(amount) <= 0:
        problems.append("A contribution must be a positive amount.")

    # a member with nothing to contribute towards their own bereavement is not
    # asked to levy themselves — and if they have been, the treasurer should
    # know before the money is receipted, not afterwards when the family asks
    # why. Uses the SAME weight the levy roster and the benefit deduction use,
    # so this validation can never disagree with what raise_case_levy actually
    # rosters them for.
    if kind == K.LEVY and case is not None and membership is not None \
            and case.membership_id == membership.pk:
        from benevolent.services.eligibility import _bereaved_weight
        if _bereaved_weight(policy, case) <= 0 or policy.bereaved_deduct_own_levy:
            problems.append(
                f"{membership.member.name} is the bereaved member on {case.number}, and "
                f"this policy does not levy them (directly) for their own case. Receipt "
                f"this as a voluntary contribution if they insisted on giving anyway.")
    return problems


# ---------------------------------------------------------------------------
# Intake — a receipt arrives from an unattended channel
# ---------------------------------------------------------------------------

@db_tx.atomic
def intake(transaction, *, scheme=None, user=None):
    """Take a receipt that is (or may be) scheme money and work out whose it is.

    THE MONEY IS ALREADY BANKED. `transaction` exists; it is in the ledger; it is on
    the bank reconciliation. This function decides only who it belongs to, and it is
    allowed to fail at that without any of the above becoming untrue.

    Outcomes:
      * confident, unambiguous, valid → attached to the member automatically
      * confident but AMBIGUOUS       → review queue (two candidates a whisker apart
                                        is not confidence; it is the allocator saying
                                        it cannot tell them apart)
      * plausible                     → review queue, with the suggestions
      * nothing                       → unmatched queue, with none
      * looks like a repeat           → duplicate queue, for a human to judge
    """
    cfg = BenevolentSettings.get()

    existing = ContributionIntake.objects.filter(transaction=transaction).first()
    if existing is not None:
        return existing

    result = alloc_svc.allocate(
        reference=transaction.reference or transaction.raw_narration,
        phone=transaction.payer_phone, name=transaction.payer_name,
        amount=transaction.amount, date=transaction.date,
        fund=transaction.department, scheme=scheme)

    item = ContributionIntake(
        transaction=transaction, scheme=result.scheme,
        confidence=result.confidence,
        candidates=result.as_dict()["candidates"],
        suggested_kind=result.kind or "",
        duplicate_of=result.duplicate_of,
        note="; ".join(result.notes)[:200])

    if result.scheme is None:
        item.status = ContributionIntake.Status.UNMATCHED
        item.save()
        return item

    best = result.best
    if best is not None:
        item.suggested_membership_id = best.membership_id
        item.suggested_case_id = best.case_id

    # a suspected duplicate never allocates itself, whatever the confidence. The
    # whole point of the flag is that a human looks at it.
    if result.duplicate_of is not None:
        item.status = ContributionIntake.Status.DUPLICATE
        item.save()
        return item

    problems = []
    if best is not None:
        membership = SchemeMembership.objects.filter(pk=best.membership_id).first()
        from benevolent.models import BenevolentCase
        case = BenevolentCase.objects.filter(pk=best.case_id).first() if best.case_id \
            else None
        problems = validate(
            result.scheme, kind=(best.kind or BenevolentContribution.Kind.VOLUNTARY),
            membership=membership, case=case, amount=transaction.amount,
            date=transaction.date)

    identity_ok = (cfg.auto_allocate
                   and best is not None
                   # the auto gate checks IDENTITY confidence, not total score: an
                   # amount matching what this member owes corroborates the money's
                   # purpose but says nothing about WHO paid (a hundred members owe
                   # exactly 500), so it must not lift a name-only guess over the
                   # threshold and post to the wrong person automatically.
                   and result.identity_confidence >= (cfg.auto_allocate_threshold or 85)
                   and not result.is_ambiguous)

    # Obligation apportionment (items 6/7/8) runs on IDENTITY alone. The
    # allocator may have flagged "a levy has to say which case" because it could
    # not pin the case from the narration — but the obligations engine pins it
    # itself (the single open case, or the oldest owed), so that particular
    # problem is not a blocker here; the engine decides the case and kind, and
    # validates each part as it applies it.
    if identity_ok:
        obligation_outcome = _maybe_auto_apply_obligations(
            item, membership, best, result, cfg)
        if obligation_outcome is not None:
            return obligation_outcome

    can_auto = identity_ok and not problems

    if can_auto:
        contribution = _attach(
            item, membership_id=best.membership_id, case_id=best.case_id,
            kind=best.kind or BenevolentContribution.Kind.VOLUNTARY,
            user=None, automatic=True, confidence=result.confidence)
        item.status = ContributionIntake.Status.AUTO
        item.contribution = contribution
        item.resolved_at = timezone.now()
        item.save()
        return item

    if problems:
        item.note = ("; ".join(problems))[:200]
    item.status = (ContributionIntake.Status.REVIEW
                   if (best is not None
                       and result.confidence >= (cfg.review_threshold or 40))
                   else ContributionIntake.Status.UNMATCHED)
    item.save()
    return item


@db_tx.atomic
def resolve(item, *, membership=None, case=None, kind=None, user, note="",
            learn=True):
    """A treasurer says whose the money is. The receipt does not move — it is
    already banked and already in the ledger. All that changes is the index row that
    says who gave it."""
    if not item.is_open:
        raise ValidationError(f"{item} is already {item.get_status_display().lower()}.")
    scheme = item.scheme
    if scheme is None:
        raise ValidationError(
            "This receipt has no scheme. Say which scheme it belongs to, or reject it "
            "as not scheme money.")

    kind = kind or item.suggested_kind or BenevolentContribution.Kind.VOLUNTARY
    problems = validate(scheme, kind=kind, membership=membership, case=case,
                        amount=item.amount, date=item.date)
    if problems:
        raise ValidationError(problems)

    contribution = _attach(
        item, membership_id=(membership.pk if membership else None),
        case_id=(case.pk if case else None), kind=kind, user=user,
        automatic=False, confidence=0)

    item.status = ContributionIntake.Status.RESOLVED
    item.contribution = contribution
    item.resolved_by = user
    item.resolved_at = timezone.now()
    if note:
        item.note = note[:200]
    item.save()

    if learn:
        _maybe_learn(item, scheme, kind)
    return contribution


@db_tx.atomic
def resolve_to_obligations(item, *, membership, user, targets=None, note=""):
    """A treasurer applies a queued payment to a member's obligations (item 7).

    Splits the payment across what the member owes — registration first, then
    case levies oldest-first — or across the specific obligations the treasurer
    chose (`targets`, keys from obligations.obligation_key). Handles the payment
    that clears two or three cases in arrears in one go. The receipt does not
    move; it is already banked.
    """
    from benevolent.services import obligations as ob_svc

    if not item.is_open:
        raise ValidationError(f"{item} is already {item.get_status_display().lower()}.")
    if membership is None:
        raise ValidationError("Choose the member whose obligations this settles.")
    if item.scheme is None:
        raise ValidationError("This receipt has no scheme.")
    if membership.scheme_id != item.scheme_id:
        raise ValidationError("That member belongs to a different scheme.")

    contributions = ob_svc.apply_payment_to_obligations(
        item.transaction, membership, user=user, targets=targets, note=note)

    item.status = ContributionIntake.Status.RESOLVED
    item.contribution = contributions[0] if contributions else None
    item.suggested_membership_id = membership.pk
    item.resolved_by = user
    item.resolved_at = timezone.now()
    if note:
        item.note = note[:200]
    elif len(contributions) > 1:
        item.note = f"Applied across {len(contributions)} obligations (oldest first)."
    item.save()
    return contributions


@db_tx.atomic
def reject(item, *, user, note=""):
    """Not scheme money after all.

    Note what this does NOT do: it does not reverse the receipt or move the money.
    The transaction stays exactly where the importer put it, in the ledger and on
    the bank reconciliation. Deciding a receipt is not benevolent money is a
    statement about ATTRIBUTION, not about whether the church received it — and
    conflating the two would let a treasurer make money disappear from the cash book
    by clicking "not ours".
    """
    if not item.is_open:
        raise ValidationError(f"{item} is already {item.get_status_display().lower()}.")
    item.status = ContributionIntake.Status.REJECTED
    item.resolved_by = user
    item.resolved_at = timezone.now()
    item.note = (note or "Not scheme money.")[:200]
    item.save()
    return item


def _maybe_auto_apply_obligations(item, membership, best, result, cfg):
    """Items 6/7/8: apply a confidently-identified member's payment to what they
    owe, splitting it across obligations. Returns a resolved ContributionIntake,
    or None to fall through to the ordinary single-contribution attach.

    Rules, all constitution-driven (settings):
      * apportion_to_obligations off      → fall through (one flat contribution)
      * member owes nothing               → fall through (voluntary / donation)
      * payment > everything owed         → review, if review_overpayments
      * payment spans >1 obligation       → review, if review_multi_obligation
      * single open case, amount = levy,
        member already paid their levy     → review (would post twice)
    """
    if not cfg.apportion_to_obligations:
        return None
    if membership is None:
        return None

    from benevolent.services import obligations as ob_svc
    from benevolent.models import BenevolentCase

    txn = item.transaction
    obligations = ob_svc.obligations_for(membership, as_of=txn.date)

    # A single open case the member has ALREADY fully paid: a second payment of
    # the levy amount is a probable duplicate/overpayment — never post it twice.
    open_cases = list(BenevolentCase.objects.filter(
        scheme=item.scheme, status__in=BenevolentCase.LEVIABLE_STATUSES))
    if (cfg.auto_allocate_single_open_case and len(open_cases) == 1):
        the_case = open_cases[0]
        already_paid = contrib_svc.levy_paid_by(membership, the_case)
        levy_ob = next((o for o in obligations
                        if o.case and o.case.pk == the_case.pk), None)
        if levy_ob is None and already_paid > 0:
            # nothing left to owe on the one open case, but money arrived: judge it
            item.status = ContributionIntake.Status.REVIEW
            item.suggested_membership_id = membership.pk
            item.suggested_case_id = the_case.pk
            item.note = (f"{membership.member.name} has already paid the levy for "
                         f"{the_case.number}. This looks like a repeat or an "
                         f"overpayment — please confirm.")[:200]
            item.save()
            return item

    if not obligations:
        return None      # owes nothing settleable → ordinary voluntary/donation

    allocations, leftover = ob_svc.plan_allocation(
        membership, txn.amount, as_of=txn.date)

    if leftover > 0 and cfg.review_overpayments:
        item.status = ContributionIntake.Status.REVIEW
        item.suggested_membership_id = membership.pk
        item.note = (f"{membership.member.name} paid {txn.amount}, which is more "
                     f"than the {txn.amount - leftover} they owe. Confirm the "
                     f"extra {leftover} is a voluntary gift.")[:200]
        item.save()
        return item

    if len(allocations) > 1 and cfg.review_multi_obligation_payments:
        item.status = ContributionIntake.Status.REVIEW
        item.suggested_membership_id = membership.pk
        item.note = (f"{membership.member.name}'s payment covers "
                     f"{len(allocations)} obligations. Confirm the split.")[:200]
        item.save()
        return item

    if not allocations:
        return None

    contributions = ob_svc.apply_payment_to_obligations(
        txn, membership, user=None, as_of=txn.date)
    for c in contributions:
        c.allocated_automatically = True
        c.allocation_confidence = result.confidence
        c.save(update_fields=["allocated_automatically", "allocation_confidence"])

    item.status = ContributionIntake.Status.AUTO
    item.contribution = contributions[0]
    item.suggested_membership_id = membership.pk
    item.resolved_at = timezone.now()
    if len(contributions) > 1:
        item.note = (f"Applied across {len(contributions)} obligations "
                     f"(oldest first).")[:200]
    item.save()
    return item


def _attach(item, *, membership_id, case_id, kind, user, automatic, confidence):
    """Create the index row against a receipt that already exists.

    Reuses `contributions.record_contribution` with the existing transaction, so
    there is ONE function that creates a BenevolentContribution, whatever door the
    money came in by. A second creation path is a second set of rules about period
    labels and kinds, and it would drift.
    """
    from benevolent.models import BenevolentCase
    membership = SchemeMembership.objects.filter(pk=membership_id).first()
    case = BenevolentCase.objects.filter(pk=case_id).first() if case_id else None

    contribution = contrib_svc.record_contribution(
        item.scheme, date=item.transaction.date, amount=item.transaction.amount,
        user=user, membership=membership,
        member=(membership.member if membership else item.transaction.member),
        case=case, kind=kind, existing_transaction=item.transaction,
        note=("Allocated automatically." if automatic else "Allocated by a treasurer."))
    contribution.allocated_automatically = automatic
    contribution.allocation_confidence = confidence
    contribution.save(update_fields=["allocated_automatically", "allocation_confidence"])
    return contribution


def _maybe_learn(item, scheme, kind):
    """Propose a narration rule once the same unrecognised narration has been
    allocated by hand a few times.

    Proposes — it does not create an ACTIVE rule. A rule that silently starts
    routing money because a treasurer happened to allocate three receipts the same
    way is a rule nobody agreed to. It is created inactive, and a human turns it on.
    """
    cfg = BenevolentSettings.get()
    if not cfg.learn_allocation_rules:
        return None
    ref = alloc_svc.normalise(item.transaction.reference)
    if not ref or len(ref) < 3:
        return None
    if ContributionRule.objects.filter(pattern=ref).exists():
        return None

    seen = ContributionIntake.objects.filter(
        transaction__reference__iexact=item.transaction.reference,
        status=ContributionIntake.Status.RESOLVED).count()
    if seen < 3:
        return None
    return ContributionRule.objects.create(
        pattern=ref[:60], match_type=ContributionRule.MatchType.CONTAINS,
        scheme=scheme, kind=kind, active=False, source="LEARNED", priority=5)


# ---------------------------------------------------------------------------
# Obligations — penalties, waivers, write-offs. NOTHING POSTS.
# ---------------------------------------------------------------------------

@db_tx.atomic
def charge(membership, *, kind, amount, reason, on=None, period_label="",
           case=None, user=None, comments=""):
    """Charge a penalty, or make some other charge against a member.

    NO ACCOUNTING ENTRY IS MADE, and that is not an omission. A penalty charged is
    not income: nobody has paid it, and they may never. Recognising it as revenue
    would book money the church does not have. It becomes income on the day it is
    actually paid — as an ordinary receipt, like everything else.

    Like an exemption, it takes a second person to approve: it changes what a member
    owes, and a treasurer who can fine a member single-handedly is a treasurer with
    a power nobody voted to give them.
    """
    adj = MemberAdjustment(
        membership=membership, kind=kind, amount=Decimal(amount),
        on=on or _dt.date.today(), period_label=period_label or "",
        reason=reason, comments=comments or "", case=case, raised_by=user,
        policy=membership.scheme.policy_on(on or _dt.date.today()))
    adj.full_clean(exclude=["approved_by", "case"])
    adj.save()
    _log(membership, f"{adj.get_kind_display()} of {adj.amount} proposed.",
         reason=reason, user=user)
    return adj


@db_tx.atomic
def charge_policy_fee(membership, *, amount, reason, on=None, user=None):
    """Raise a charge the POLICY computed, not a treasurer's own judgement —
    a reinstatement fee is what this exists for.

    Auto-approved, in the same spirit as `registry.grant_policy_exemption`: a
    published, constitution-set fee is not a new decision that needs a second
    signature the moment it is applied — it is the same decision, applied.
    Requiring a rubber-stamp approval here would only ever produce a rubber
    stamp. Marked `automated=True` so a member (or an auditor) can always tell
    it apart from a penalty a treasurer chose to impose in the moment.
    """
    on = on or _dt.date.today()
    adj = MemberAdjustment(
        membership=membership, kind=MemberAdjustment.Kind.CHARGE, amount=Decimal(amount),
        on=on, reason=reason, raised_by=user, approved_by=user,
        approved_at=timezone.now(), automated=True,
        policy=membership.scheme.policy_on(on))
    adj.full_clean(exclude=["case"])
    adj.save()
    _log(membership, f"{adj.get_kind_display()} of {adj.amount} charged automatically "
                     f"under policy — {reason}", reason=reason, user=user)
    return adj


def waive_on_import(membership, *, amount, reason, on=None, user=None):
    """Clear whatever a membership would otherwise show as owing, as part of
    bringing an EXISTING roster into the system — never a treasurer's own,
    in-the-moment forgiveness decision, so (like charge_policy_fee above) a
    second person's rubber stamp would add nothing. Marked `automated=True`
    for the same reason: an auditor can always tell a migration write-off
    apart from a discretionary waiver someone chose to grant.

    Deliberately its own function rather than a plain call to waive() with
    a bypassed approval: waive()/charge() enforce that a discretionary
    adjustment is approved by someone OTHER than who raised it, which is
    the right rule for a real waiver decision and the wrong one for a
    mechanical consequence of "this person's history predates this system."
    """
    if amount <= 0:
        return None
    on = on or _dt.date.today()
    adj = MemberAdjustment(
        membership=membership, kind=MemberAdjustment.Kind.WAIVER, amount=Decimal(amount),
        on=on, reason=reason, raised_by=user, approved_by=user,
        approved_at=timezone.now(), automated=True,
        policy=membership.scheme.policy_on(on))
    adj.full_clean(exclude=["case"])
    adj.save()
    _log(membership, f"{adj.get_kind_display()} of {adj.amount} cleared automatically on "
                     f"import — {reason}", reason=reason, user=user)
    return adj


@db_tx.atomic
def waive(membership, *, amount, reason, on=None, period_label="", user=None,
          write_off=False, comments=""):
    """Waive dues, or write off a debt.

    NO ACCOUNTING ENTRY. A waiver is not an expense: no money left the church. The
    church simply stopped asking for it. Booking it as a payment would show a cash
    outflow that never happened, and the cash book would stop agreeing with the bank.
    """
    return charge(
        membership,
        kind=(MemberAdjustment.Kind.WRITE_OFF if write_off
              else MemberAdjustment.Kind.WAIVER),
        amount=amount, reason=reason, on=on, period_label=period_label, user=user,
        comments=comments)


@db_tx.atomic
def approve_adjustment(adj, *, user):
    """A second person approves. Not the one who proposed it."""
    if adj.raised_by_id and user is not None and adj.raised_by_id == user.pk:
        raise ValidationError(
            "An adjustment must be approved by someone other than the person who "
            "proposed it. It changes what a member owes.")
    if adj.approved_by_id:
        raise ValidationError("That adjustment is already approved.")
    adj.approved_by = user
    adj.approved_at = timezone.now()
    adj.save(update_fields=["approved_by", "approved_at"])
    _log(adj.membership,
         f"{adj.get_kind_display()} of {adj.amount} approved.",
         reason=adj.reason, user=user)
    from benevolent.services import standing as standing_svc
    standing_svc.refresh(adj.membership, user=user)
    return adj


@db_tx.atomic
def reverse_adjustment(adj, *, user, reason):
    """Undo an adjustment. Never deleted — a charge that was made and withdrawn is
    part of the member's history, and a member who was fined and then let off has a
    right to have both facts on the record."""
    if not (reason or "").strip():
        raise ValidationError("Reversing an adjustment must record a reason.")
    adj.reversed_on = _dt.date.today()
    adj.reversed_reason = reason[:200]
    adj.save(update_fields=["reversed_on", "reversed_reason"])
    _log(adj.membership, f"{adj.get_kind_display()} of {adj.amount} reversed.",
         reason=reason, user=user)
    from benevolent.services import standing as standing_svc
    standing_svc.refresh(adj.membership, user=user)
    return adj


def adjustments_total(membership, as_of=None) -> Decimal:
    """The net effect of the obligations ledger on what this member owes.

    Positive = they owe more (penalties). Negative = they owe less (waivers,
    write-offs). Consumed by `arrears_for()`, which remains the ONE function that
    knows what a member owes.
    """
    as_of = as_of or _dt.date.today()
    total = Decimal(0)
    # Iterate the (possibly prefetched) relation and filter in Python rather than
    # issuing a .filter() query — a filtered call would bypass a batch prefetch
    # and re-hit the database per member. The set of adjustments per member is
    # tiny, so the in-Python date test is free.
    for adj in membership.adjustments.all():
        if adj.on > as_of:
            continue
        if adj.reversed_on and adj.reversed_on <= as_of:
            continue
        total += adj.signed
    return total


# ---------------------------------------------------------------------------
# Refunds — the one thing here that IS money leaving
# ---------------------------------------------------------------------------

@db_tx.atomic
def refund(membership, *, amount, reason, date=None, user=None, method=None,
           voucher_no=""):
    """Return money to a member.

    Distinct from REVERSING a receipt, and the distinction is the point:

      * A receipt that should never have existed — wrong member, duplicate, bounced
        — is REVERSED. The church never had that money.
      * A receipt that was CORRECT, where money is genuinely handed back, is
        REFUNDED. The church really received it and is really paying it out. Both
        facts belong in the cash book.

    Reversing a correct receipt to "cancel out" a refund would hide a real payment
    from the bank reconciliation and understate income and expenditure alike.

    So this raises an ordinary `cashbook.Expense` in PENDING. It clears the usual
    approval, gets a voucher, appears on the payment register and posts to the
    ledger like any other payment — the module does not approve its own payments,
    here or anywhere.
    """
    from cashbook.models import Expense

    date = date or _dt.date.today()
    amount = Decimal(amount)
    if amount <= 0:
        raise ValidationError("A refund must be a positive amount.")
    if not (reason or "").strip():
        raise ValidationError("A refund must record why the money is being returned.")

    scheme = membership.scheme
    given = contrib_svc.contributions_total(membership=membership)
    # A registration fee buys enrolment; it is not money held on a member's
    # behalf. `registration_fee_refundable` says whether the scheme gives it
    # back, and nothing read it — so a scheme that keeps the fee would still
    # refund it as part of "everything they contributed", and the leaver would
    # be handed money the constitution says the scheme retains.
    _pol = scheme.policy_on(date)
    if _pol is not None and not _pol.registration_fee_refundable:
        from benevolent.models import BenevolentContribution
        fees = contrib_svc.contributions_total(
            membership=membership, kinds=[BenevolentContribution.Kind.REGISTRATION])
        given = given - (fees or Decimal(0))
    if amount > given:
        raise ValidationError(
            f"{membership.member.name} has contributed {given} in total, so {amount} "
            f"cannot be refunded. A payment larger than what a member has given is not "
            f"a refund — it is a benefit, and it goes through a case.")

    policy = scheme.policy_on(date)
    # `refund_percent` is what the scheme's own rules say may be given back. It
    # was stored, shown on the setup form, and never once consulted — so a
    # scheme constituted to refund half of what a leaver had put in would hand
    # back all of it, and the register would show the constitution being
    # followed. Applied here as a ceiling rather than a fixed amount, because a
    # treasurer may have good reason to refund less.
    # 0 means unspecified, not "refund nothing" — the same convention every
    # other numeric limit in this policy uses (max_dependants, min_age,
    # max_levies_per_year all read 0 as no limit), and the field defaults to 0.
    # Reading it as a hard zero would have refused every refund on every scheme
    # that never set it, which is all of them.
    if (policy is not None and policy.refund_percent
            and 0 < policy.refund_percent < 100):
        ceiling = (given * policy.refund_percent / Decimal(100)).quantize(Decimal("0.01"))
        if amount > ceiling:
            raise ValidationError(
                f"Policy v{policy.version} refunds {policy.refund_percent}% of "
                f"contributions on exit. {membership.member.name} has "
                f"contributed {given}, so at most {ceiling} may be returned — "
                f"{amount} is more than the scheme's own rules allow. Amend the "
                f"policy if the scheme has decided otherwise.")
    if policy is not None and not policy.refund_contributions_on_exit:
        # a policy that does not refund on exit can still refund an overpayment, so
        # this is a warning carried on the voucher rather than a refusal — but it is
        # recorded, so nobody can pretend the constitution allowed it
        reason = (f"[Policy v{policy.version} does not provide for refunds on exit.] "
                  f"{reason}")

    # Built exactly as a benefit payout is (services/cases.record_payout), because
    # it IS the same thing accounting-wise: money leaving the scheme's fund on an
    # approved voucher. Mirroring it rather than inventing a second shape means the
    # payment register, the expense approval workflow and the ledger posting all
    # treat a refund the way they already treat every other payment.
    expense = Expense.objects.create(
        date=date, department=scheme.fund, amount=amount,
        description=f"Refund of contributions — {membership.member.name} "
                    f"({membership.number})"[:200],
        category=Expense.Category.BENEVOLENCE,
        funding_source=Expense.FundingSource.CONTRIBUTION,
        expenditure_type=Expense.ExpenditureType.RECURRENT,
        claimant=membership.member.name[:120],
        method=method or Expense.Method.CASH,
        voucher_no=voucher_no or "",
        status=Expense.Status.PENDING,          # never self-approved
        recorded_by=user)

    ref = ContributionRefund.objects.create(
        membership=membership, scheme=scheme, expense=expense,
        reason=reason, requested_by=user)
    _log(membership, f"Refund of {amount} raised (voucher pending approval).",
         reason=reason, user=user)
    return ref


def _log(membership, summary, *, reason="", user=None):
    from benevolent.models import MembershipEvent
    MembershipEvent.objects.create(
        membership=membership, kind=MembershipEvent.Kind.NOTE,
        summary=summary[:255], reason=reason or "", actor=user,
        automated=(user is None))
