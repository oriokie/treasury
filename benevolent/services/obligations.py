"""The obligations ledger (Round 9, items 6/7/8).

ONE function answers "what does this member owe the scheme, and in what order?"
and everything else reads it: the auto-allocator when it decides where a payment
goes, the review queue when a treasurer assigns a payment by hand, and the
member's own statement. Having a single ordered list is the whole point — a
payment that clears "the next thing owed" must clear the SAME next thing whether
a machine or a person applied it, or the arrears book quietly goes wrong.

Priority order, oldest-first within each tier:

  1. Registration fee — you are not really a member until it is paid, and every
     later obligation assumes membership, so it comes first.
  2. Renewal fee — the subscription that keeps membership live.
  3. Case levies — one per open case the member is on the roster for, OLDEST
     CASE FIRST (a family whose bereavement was three months ago has been
     waiting longest; the newest case can wait).
  4. Dues arrears — periodic subscription arrears.

Each obligation carries what is due, what has been paid against it, and what is
still outstanding, so a payment can be walked down the list settling each in turn.

Nothing here settles anything or moves money — it only reports. Applying a
payment against these obligations is `apply_payment_to_obligations` below, which
splits the TRANSACTION (never the contribution: BenevolentContribution.amount is
a property reading transaction.amount, so two contributions cannot share one
transaction and show different amounts).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from django.core.exceptions import ValidationError
from django.db import transaction as db_tx

from benevolent.models import (BenevolentCase, BenevolentContribution,
                               SchemeMembership, SchemePolicy)


# ---------------------------------------------------------------------------
# The obligation record
# ---------------------------------------------------------------------------

@dataclass
class Obligation:
    kind: str                       # BenevolentContribution.Kind value
    label: str
    due: Decimal
    paid: Decimal
    order: int                      # lower = settle first
    case: Optional[object] = None   # BenevolentCase, for a levy
    period_label: str = ""          # for dues
    detail: str = ""

    @property
    def outstanding(self) -> Decimal:
        return max(Decimal(0), self.due - self.paid)

    @property
    def is_settled(self) -> bool:
        return self.outstanding <= 0

    def as_dict(self):
        return {"kind": self.kind, "label": self.label,
                "due": float(self.due), "paid": float(self.paid),
                "outstanding": float(self.outstanding), "order": self.order,
                "case_id": self.case.pk if self.case else None,
                "case_number": self.case.number if self.case else "",
                "period_label": self.period_label, "detail": self.detail}


# ---------------------------------------------------------------------------
# Building the ordered list
# ---------------------------------------------------------------------------

def obligations_for(membership, *, as_of=None, include_settled=False):
    """Everything this member owes, in the order it should be settled.

    `include_settled=True` returns fully-paid obligations too (useful for a
    statement); by default only what is still outstanding is returned.
    """
    as_of = as_of or _dt.date.today()
    from benevolent.services.contributions import (arrears_for, levy_paid_by)

    scheme = membership.scheme
    policy = scheme.policy_on(as_of)
    out: list[Obligation] = []
    order = 0

    if policy is None:
        return out

    # 1. registration fee ---------------------------------------------------
    if (policy.registration_required and policy.registration_fee
            and not membership.registration_fee_paid):
        fee = Decimal(policy.registration_fee)
        paid = _paid_of_kind(membership, BenevolentContribution.Kind.REGISTRATION)
        out.append(Obligation(
            kind=BenevolentContribution.Kind.REGISTRATION,
            label="Registration fee", due=fee, paid=paid, order=order,
            detail=f"{scheme.code} registration"))
        order += 1

    # 2. renewal fee --------------------------------------------------------
    if policy.renewal_fee:
        due_on = membership.renewal_due_on(policy, as_of=as_of)
        if due_on and due_on <= as_of:
            fee = Decimal(policy.renewal_fee)
            # only the renewals paid since the current period became due count
            paid = _paid_of_kind(membership, BenevolentContribution.Kind.RENEWAL,
                                 since=membership.renewed_until)
            out.append(Obligation(
                kind=BenevolentContribution.Kind.RENEWAL,
                label="Renewal fee", due=fee, paid=paid, order=order,
                detail=f"{scheme.code} renewal"))
            order += 1

    # 3. case levies — OLDEST CASE FIRST ------------------------------------
    leviable = (SchemePolicy.ContributionMode.PER_CASE_LEVY,
                SchemePolicy.ContributionMode.HYBRID)
    if policy.contribution_mode in leviable:
        # Leviable, not open: the church pays the family first and levies the
        # membership afterwards, so a case is almost always PAID by the time a
        # member's levy money arrives. Filtering on open cases stopped the
        # obligation appearing at precisely the point it was owed.
        cases = (BenevolentCase.objects
                 .filter(scheme=scheme, status__in=BenevolentCase.LEVIABLE_STATUSES)
                 .order_by("event_date", "id"))   # oldest event first
        for case in cases:
            levy = _levy_due_for(membership, case, policy, as_of)
            if levy is None or levy <= 0:
                continue
            paid = levy_paid_by(membership, case)
            ob = Obligation(
                kind=BenevolentContribution.Kind.LEVY,
                label=f"Levy for {case.number}", due=levy, paid=paid,
                order=order, case=case,
                detail=f"{case.beneficiary_display} · {case.event_date:%d %b %Y}")
            if include_settled or not ob.is_settled:
                out.append(ob)
            order += 1

    # 4. dues arrears -------------------------------------------------------
    arrears = arrears_for(membership, policy, as_of=as_of)
    if arrears > 0:
        out.append(Obligation(
            kind=BenevolentContribution.Kind.DUES,
            label="Dues arrears", due=arrears, paid=Decimal(0), order=order,
            detail="Outstanding periodic dues"))
        order += 1

    if include_settled:
        return out
    return [o for o in out if not o.is_settled]


def total_outstanding(membership, *, as_of=None) -> Decimal:
    return sum((o.outstanding for o in obligations_for(membership, as_of=as_of)),
               Decimal(0))


# ---------------------------------------------------------------------------
# Applying a payment down the list
# ---------------------------------------------------------------------------

def plan_allocation(membership, amount, *, as_of=None, targets=None):
    """Walk `amount` down the obligations list, returning the split plan.

    Returns (allocations, leftover) where allocations is a list of
    (Obligation, amount_to_apply) and leftover is any money above what is owed.

    `targets` optionally restricts/orders which obligations to pay: a list of
    keys from `obligation_key()` (e.g. a treasurer choosing to pay two specific
    cases in arrears). When given, only those obligations are paid, in the order
    listed; when omitted, the natural priority order is used.
    """
    as_of = as_of or _dt.date.today()
    amount = Decimal(amount)
    obligations = obligations_for(membership, as_of=as_of)

    if targets:
        by_key = {obligation_key(o): o for o in obligations}
        ordered = [by_key[k] for k in targets if k in by_key]
    else:
        ordered = obligations

    allocations = []
    remaining = amount
    for ob in ordered:
        if remaining <= 0:
            break
        take = min(remaining, ob.outstanding)
        if take > 0:
            allocations.append((ob, take))
            remaining -= take
    return allocations, remaining


def obligation_key(ob: Obligation) -> str:
    """A stable string identifying one obligation, for a form to post back."""
    if ob.case is not None:
        return f"LEVY:{ob.case.pk}"
    if ob.period_label:
        return f"{ob.kind}:{ob.period_label}"
    return f"{ob.kind}"


@db_tx.atomic
def apply_payment_to_obligations(intake_or_txn, membership, *, user=None,
                                 targets=None, as_of=None, note=""):
    """Settle a member's obligations from one payment, splitting the TRANSACTION.

    The receipt is already banked. This walks the payment down the obligations
    list (or the treasurer's chosen `targets`), and for each obligation it settles
    it creates a child transaction carrying that obligation's share, with its own
    BenevolentContribution of the right KIND (a levy is a LEVY, a fee is a fee) so
    a levy can never silently clear dues and the arrears book stays honest.

    Splitting the transaction — not making several contributions point at one
    transaction — is REQUIRED: BenevolentContribution.amount is a property that
    reads transaction.amount, so two contributions on one transaction would both
    report the whole amount, and the scheme's books would double-count.

    Returns the list of BenevolentContribution rows created. Any money above what
    is owed is left on a final VOLUNTARY contribution (an overpayment the member
    genuinely gave), unless the caller has already diverted it to review.
    """
    from giving.models import Transaction
    from benevolent.services.contributions import record_contribution

    txn = _resolve_txn(intake_or_txn)
    scheme = membership.scheme
    total = txn.amount

    allocations, leftover = plan_allocation(
        membership, total, as_of=as_of, targets=targets)

    if not allocations:
        raise ValidationError(
            "This member has nothing outstanding to apply this payment to. "
            "Record it as a voluntary contribution, or choose an obligation.")

    # Build the (department, amount, dev_group) parts. Everything lands on the
    # scheme's own fund — a levy, a fee and dues are all scheme money — so the
    # split is purely to carry distinct amounts, each then indexed with its kind.
    parts = [(txn.department, amt, None) for _ob, amt in allocations]
    if leftover > 0:
        parts.append((txn.department, leftover, None))

    contributions = []
    if len(parts) == 1:
        # exactly one obligation (the common case): no split needed, just index
        ob, amt = allocations[0]
        contributions.append(_index_part(
            txn, membership, ob, scheme, user, note))
    else:
        children = txn.split_into(parts, user=user)
        # children[i] lines up with parts[i]
        for i, (ob, _amt) in enumerate(allocations):
            contributions.append(_index_part(
                children[i], membership, ob, scheme, user, note))
        if leftover > 0:
            # the trailing part is the overpayment
            over_txn = children[len(allocations)]
            contributions.append(record_contribution(
                scheme, date=over_txn.date, amount=over_txn.amount, user=user,
                membership=membership, existing_transaction=over_txn,
                kind=BenevolentContribution.Kind.VOLUNTARY,
                note=(note or "Overpayment beyond obligations")[:200]))

    return contributions


def _index_part(txn, membership, ob: Obligation, scheme, user, note):
    """Attach a BenevolentContribution of the obligation's kind to a (child)
    transaction. Reuses record_contribution with existing_transaction so the
    money is never created twice."""
    from benevolent.services.contributions import record_contribution
    # split_into() reuses the parent object as the first part; its cached
    # reverse `benevolent_contribution` accessor can be stale after we deleted
    # an earlier index row, so re-fetch to see the true database state.
    txn.refresh_from_db()
    if hasattr(txn, "benevolent_contribution"):
        try:
            del txn.benevolent_contribution
        except AttributeError:
            pass
    return record_contribution(
        scheme, date=txn.date, amount=txn.amount, user=user,
        membership=membership, existing_transaction=txn,
        case=ob.case, kind=ob.kind,
        period_label=ob.period_label or "",
        note=(note or ob.label)[:200])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _resolve_txn(intake_or_txn):
    from giving.models import Transaction
    from benevolent.models import ContributionIntake
    if isinstance(intake_or_txn, ContributionIntake):
        return intake_or_txn.transaction
    if isinstance(intake_or_txn, Transaction):
        return intake_or_txn
    raise ValidationError("Expected a transaction or an intake item.")


def _paid_of_kind(membership, kind, since=None):
    qs = BenevolentContribution.objects.filter(membership=membership, kind=kind)
    if since is not None:
        qs = qs.filter(transaction__date__gte=since)
    return sum((c.amount for c in qs.select_related("transaction")), Decimal(0))


def _levy_due_for(membership, case, policy, as_of):
    """What THIS member owes towards ONE case's levy, mirroring raise_case_levy's
    per-member logic so the obligations list and the levy roster never disagree.
    Returns None if the member is not on this case's roster at all."""
    from benevolent.services.contributions import raise_case_levy
    from django.core.exceptions import ValidationError as _VE
    try:
        roster = raise_case_levy(case)
    except _VE:
        return None
    for row in roster["rows"]:
        if row["membership"].pk == membership.pk:
            return Decimal(row["due"])
    return None
