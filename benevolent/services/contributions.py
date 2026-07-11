"""Money INTO a scheme.

A contribution is an ordinary giving.Transaction CREDIT on the scheme's fund,
attributed to the contributing member. It is income of a designated local fund
(unlike a loan receipt, which is financing), so it needs no special accounting
at all: the existing posting engine books it DR Cash / CR Income, the fund
balance rises, the cash book and bank reconciliation pick it up, and the member's
giving statement shows it — all with no benevolent-specific code.

BenevolentContribution indexes that receipt with the two facts the receipt cannot
carry: which enrolment it settles, and which dues period (or case levy) it is
for. Amounts are always read back off the Transaction, never copied, so the
scheme's contribution totals and the fund's receipts can never disagree.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction as db_tx
from django.db.models import Q, Sum

from benevolent.models import (BenevolentContribution, BenevolentScheme,
                               SchemeMembership, SchemePolicy)


# ---------------------------------------------------------------------------
# Period labels — the vocabulary for "which dues period does this settle?"
# ---------------------------------------------------------------------------

def period_label_for(date, frequency):
    """The canonical label for the dues period a date falls in. One definition,
    used by recording, arrears and reporting alike."""
    if frequency == SchemePolicy.Frequency.ANNUAL:
        return f"{date.year}"
    if frequency == SchemePolicy.Frequency.QUARTERLY:
        return f"{date.year}-Q{((date.month - 1) // 3) + 1}"
    return f"{date.year}-{date.month:02d}"      # MONTHLY (the default)


def periods_between(start, end, frequency):
    """Every dues period from `start` to `end` inclusive, as (label, first_day)
    pairs in order. The single definition of 'which periods have fallen due',
    shared by the arrears calculation and the dues schedule.

    The date is carried alongside the label because arrears must resolve the
    policy that was in force during EACH period, not just the current one.
    """
    if end < start:
        return []
    out, seen = [], set()
    d = start
    while d <= end:
        lbl = period_label_for(d, frequency)
        if lbl not in seen:
            seen.add(lbl)
            out.append((lbl, d))
        # step by a day: cheap, exact, and immune to month-length edge cases
        d += _dt.timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# Reading contributions (always through the source document)
# ---------------------------------------------------------------------------

def _effective_q(prefix="transaction__"):
    """THE definition of a contribution that counts — deliberately identical to
    the income-credit definition in core.metrics, so a contribution counts here
    exactly when the fund's receipts count it too."""
    return Q(**{f"{prefix}confirmed": True,
                f"{prefix}is_reversed": False,
                f"{prefix}is_reversal": False})


def contributions_qs(scheme=None, membership=None, start=None, end=None,
                     period_label=None, kinds=None, case=None):
    """Contributions, optionally narrowed to certain KINDS of money.

    The kind matters, and getting it wrong is not cosmetic. A levy raised for
    someone else's bereavement, and a registration fee, are both money a member
    pays the scheme — but neither is a DUE. If they were counted as dues, a member
    who paid a 500 levy would appear to have cleared five months of their own
    subscription, their arrears would silently vanish, and the scheme's arrears
    book would go quietly and permanently wrong.
    """
    qs = BenevolentContribution.objects.filter(_effective_q())
    if scheme is not None:
        qs = qs.filter(scheme=scheme)
    if membership is not None:
        qs = qs.filter(membership=membership)
    if start:
        qs = qs.filter(transaction__date__gte=start)
    if end:
        qs = qs.filter(transaction__date__lte=end)
    if period_label:
        qs = qs.filter(period_label=period_label)
    if case is not None:
        qs = qs.filter(case=case)
    if kinds:
        qs = qs.filter(kind__in=list(kinds))
    return qs.select_related("transaction", "membership", "membership__member")


def contributions_total(scheme=None, membership=None, start=None, end=None,
                        period_label=None, kinds=None, case=None) -> Decimal:
    """Aggregated in the database off the Transaction's own amount column."""
    agg = contributions_qs(scheme, membership, start, end, period_label,
                           kinds, case).aggregate(t=Sum("transaction__amount"))
    return agg["t"] or Decimal(0)


# what actually counts against a member's dues — and nothing else does
DUES_KINDS = [BenevolentContribution.Kind.DUES]


def levy_collected(case) -> Decimal:
    """What the levy for one case has actually raised. This IS the benefit under a
    pooled (harambee) policy, so it is a figure the engine depends on — not a
    reporting nicety."""
    return contributions_total(case=case)


def levy_paid_by(membership, case) -> Decimal:
    return contributions_total(membership=membership, case=case)


def _dues_rows(membership, as_of=None):
    """Period-by-period dues for one membership: what fell due, at the rate that
    was actually in force in THAT period, and what has been paid against it.

    Dues accrue from the member's cover date (or the scheme's first policy, if
    later — nobody can owe dues under rules that did not yet exist), and each
    period is charged at the rate of the policy in force during it.

    Resolving the policy per period matters, and not only for tidiness: charging
    everything at the CURRENT policy's rate, from the CURRENT policy's effective
    date, would mean that simply publishing a new version silently wiped every
    member's arrears — a treasurer could clear the whole scheme's debt by
    republishing the same rules with a new date. Historical dues, like historical
    decisions, are fixed by the policy that was actually in force.

    Only the PERIODIC part of a hybrid scheme accrues here. A per-case levy is not
    a due: it is raised when a case happens, cannot be "in arrears" before then,
    and is tracked against the case that raised it.
    """
    as_of = as_of or _dt.date.today()
    scheme = membership.scheme
    first = (scheme.policies
             .filter(status__in=[SchemePolicy.Status.ACTIVE,
                                 SchemePolicy.Status.SUPERSEDED])
             .order_by("effective_from").values_list("effective_from", flat=True).first())
    if first is None:
        return []

    start = max(membership.cover_from, first)
    end = min(as_of, membership.left_on or as_of)
    if end < start:
        return []

    periodic = (SchemePolicy.ContributionMode.FIXED_PERIODIC,
                SchemePolicy.ContributionMode.HYBRID)

    # the frequency can itself change between versions, so step period by period
    # using the frequency of the policy in force at the point we have reached
    rows, seen = [], set()
    d = start
    while d <= end:
        policy = scheme.policy_on(d)
        if policy is None or policy.contribution_mode not in periodic \
                or not policy.contribution_amount:
            d += _dt.timedelta(days=1)
            continue
        label = period_label_for(d, policy.contribution_frequency)
        if label not in seen:
            seen.add(label)
            rows.append({"period": label,
                         "due": Decimal(policy.contribution_amount),
                         "policy_version": policy.version})
        d += _dt.timedelta(days=1)

    # dues waived after the member's own case (a bereaved-member rule)
    waived = _waived_periods(membership, as_of)
    for r in rows:
        r["waived"] = r["period"] in waived
        if r["waived"]:
            r["due"] = Decimal(0)
        r["paid"] = contributions_total(membership=membership,
                                        period_label=r["period"], kinds=DUES_KINDS)
        r["outstanding"] = max(Decimal(0), r["due"] - r["paid"])
    return rows


def _waived_periods(membership, as_of=None):
    """The dues periods waived for a member after their own case.

    Many constitutions give a bereaved member a few months' grace on their dues.
    Implemented here rather than as a manual adjustment because it is a RULE — it
    must apply consistently and it must be visible in the dues schedule, not
    remembered by whoever happens to be treasurer that year.
    """
    from benevolent.models import BenevolentCase
    as_of = as_of or _dt.date.today()
    out = set()
    cases = membership.cases.filter(
        status__in=[BenevolentCase.Status.APPROVED, BenevolentCase.Status.PARTLY_PAID,
                    BenevolentCase.Status.PAID, BenevolentCase.Status.CLOSED])
    for case in cases:
        policy = case.policy or membership.scheme.policy_on(case.event_date)
        if policy is None or not policy.bereaved_dues_waiver_months:
            continue
        freq = policy.contribution_frequency
        d = case.event_date
        for _ in range(policy.bereaved_dues_waiver_months):
            if d > as_of:
                break
            out.add(period_label_for(d, freq))
            # step into the next period
            d = (d.replace(day=28) + _dt.timedelta(days=7)).replace(day=1)
    return out


def arrears_for(membership, policy=None, as_of=None) -> Decimal:
    """What a member still owes in dues, as at a date.

    Only meaningful where dues fall due periodically; every other contribution
    mode has nothing that CAN fall into arrears, and returns zero rather than
    inventing a debt.

    The total owed is measured against everything the member has actually paid
    over the same span — the money is what matters, not how a payment happened
    to be labelled — so a member who paid a lump sum covering several months is
    correctly not in arrears.
    """
    as_of = as_of or _dt.date.today()
    rows = _dues_rows(membership, as_of)
    if not rows:
        return Decimal(0)
    due = sum((r["due"] for r in rows), Decimal(0))
    scheme = membership.scheme
    first = (scheme.policies
             .filter(status__in=[SchemePolicy.Status.ACTIVE,
                                 SchemePolicy.Status.SUPERSEDED])
             .order_by("effective_from").values_list("effective_from", flat=True).first())
    start = max(membership.cover_from, first)
    end = min(as_of, membership.left_on or as_of)
    # Only DUES pay off dues. A levy, a registration fee and a donation are all
    # money the member has given the scheme, and none of them is a subscription.
    paid = contributions_total(membership=membership, start=start, end=end,
                               kinds=DUES_KINDS)
    return max(Decimal(0), due - paid)


def dues_schedule(membership, policy=None, as_of=None):
    """Period-by-period: what fell due, what was paid, what is outstanding.
    Drives the member's contribution statement."""
    return _dues_rows(membership, as_of)


# ---------------------------------------------------------------------------
# Recording a contribution
# ---------------------------------------------------------------------------

@db_tx.atomic
def record_contribution(scheme, *, date, amount, user=None, membership=None,
                        member=None, channel=None, period_label=None, case=None,
                        note="", reference="", existing_transaction=None, fund=None,
                        kind=None):
    """Take money into a scheme.

    Creates (or adopts) the fund receipt and indexes it. Passing
    `existing_transaction` adopts a receipt already on the books — e.g. a bank
    credit the treasurer has identified as scheme dues — rather than creating a
    second one, so the money is never counted twice.
    """
    from core.models import period_locked, service_sabbath_for
    from core.utils import sabbath_week_of
    from giving.models import Transaction

    amount = Decimal(amount or 0)
    if amount <= 0:
        raise ValidationError("A contribution must be a positive amount.")
    if not scheme.accepts_contributions:
        raise ValidationError(
            f"{scheme.name} is {scheme.get_status_display().lower()} and cannot take contributions.")
    if membership is not None and membership.scheme_id != scheme.pk:
        raise ValidationError("That membership belongs to a different scheme.")

    lock = period_locked(date)
    if lock:
        raise ValidationError(f"{lock} is a closed accounting period; it cannot be posted to.")

    member = member or (membership.member if membership else None)
    # `fund` lets a registration/renewal fee be receipted somewhere other than the
    # benefit pool, where the settings say so. Everything else about the receipt
    # is identical — a fee is money like any other and gets no special accounting.
    fund = fund or scheme.fund
    policy = scheme.policy_on(date)
    # What KIND of money is this? Inferred where the caller has not said, because
    # getting it wrong is what lets a levy or a fee silently pay off a member's
    # dues. Money attached to a case is always a levy. Money from a non-member is
    # a donation, since only a member can owe dues.
    periodic = (SchemePolicy.ContributionMode.FIXED_PERIODIC,
                SchemePolicy.ContributionMode.HYBRID)
    if kind is None:
        if case is not None:
            kind = BenevolentContribution.Kind.LEVY
        elif membership is None:
            kind = BenevolentContribution.Kind.DONATION
        elif policy and policy.contribution_mode in periodic:
            kind = BenevolentContribution.Kind.DUES
        else:
            kind = BenevolentContribution.Kind.DONATION

    # only DUES carry a dues period; giving one to a levy or a fee would have it
    # settle a subscription it was never paid for
    if period_label is None and kind == BenevolentContribution.Kind.DUES and policy \
            and policy.contribution_mode in periodic:
        period_label = period_label_for(date, policy.contribution_frequency)

    if existing_transaction is not None:
        txn = existing_transaction
        if hasattr(txn, "benevolent_contribution"):
            raise ValidationError("That receipt is already recorded as a scheme contribution.")
        if txn.direction != Transaction.Direction.CREDIT:
            raise ValidationError("Only a CREDIT can be a contribution.")
        txn.department = fund
        if member and not txn.member_id:
            txn.member = member
        txn.allocation_status = Transaction.Status.MANUAL
        txn.save()
    else:
        svc = service_sabbath_for(date)
        txn = Transaction.objects.create(
            date=date, service_sabbath=svc, sabbath_week=sabbath_week_of(svc),
            channel=channel or Transaction.Channel.CASH,
            direction=Transaction.Direction.CREDIT, amount=amount,
            department=fund, member=member,
            allocation_status=Transaction.Status.MANUAL,
            reference=(reference or f"{scheme.code} contribution")[:60],
            payer_name=(member.name if member else "")[:120],
            payer_phone=((member.phone or "") if member else "")[:12],
            raw_narration=note or "")

    contribution = BenevolentContribution.objects.create(
        scheme=scheme, membership=membership, transaction=txn, kind=kind,
        period_label=period_label or "", case=case, note=note, recorded_by=user)

    # a receipt saved before the index row existed is re-posted so the ledger
    # sees the final shape of the document (same belt-and-braces as loans)
    _repost(txn)
    return contribution


def _repost(txn):
    from ledger.services import posting
    if posting.chart_ready():
        posting.post_transaction(txn)


@db_tx.atomic
def raise_case_levy(case, *, amount=None, user=None):
    """Prepare a per-case levy: who owes it, and how much.

    Deliberately does NOT create receipts — nobody has paid yet. It is the working
    list a treasurer collects against; each payment is then an ordinary
    `record_contribution(case=case)`.

    The bereaved member is handled here, because it is the one part of a levy
    round a church will never get wrong on paper and software very easily gets
    wrong in code: under almost every real constitution, the family receiving the
    benefit is NOT asked to chip in towards their own benefit. Where a policy says
    otherwise (`bereaved_deduct_own_levy`), they are levied and it comes out of
    what they receive instead — never both.
    """
    policy = case.policy or case.scheme.policy_on(case.event_date)
    if policy is None:
        raise ValidationError("No policy is in force for this case, so no levy can be raised.")
    leviable = (SchemePolicy.ContributionMode.PER_CASE_LEVY,
                SchemePolicy.ContributionMode.HYBRID)
    if policy.contribution_mode not in leviable:
        raise ValidationError(
            f"{case.scheme.name}'s policy does not raise levies per case "
            f"(its contribution mode is {policy.get_contribution_mode_display().lower()}).")
    per_member = Decimal(
        amount if amount is not None
        else (policy.levy_amount or policy.contribution_amount))
    if per_member <= 0:
        raise ValidationError("The levy amount must be positive.")

    members = (SchemeMembership.objects
               .filter(scheme=case.scheme, status=SchemeMembership.Status.ACTIVE)
               .select_related("member").order_by("member__name"))
    rows, exempt = [], []
    for m in members:
        if (policy.bereaved_exempt_own_levy and case.membership_id == m.pk):
            exempt.append(m)
            continue
        paid = levy_paid_by(m, case)
        rows.append({"membership": m, "due": per_member, "paid": paid,
                     "outstanding": max(Decimal(0), per_member - paid)})

    expected = per_member * len(rows)
    collected = sum((r["paid"] for r in rows), Decimal(0))
    return {"case": case, "per_member": per_member, "rows": rows,
            "exempt": exempt, "policy": policy,
            "expected": expected, "collected": collected,
            "outstanding": max(Decimal(0), expected - collected)}


def levy_summary(case):
    """The levy round for a case, or None where the policy does not levy. Safe to
    call from a template: it never raises."""
    try:
        return raise_case_levy(case)
    except ValidationError:
        return None


# ---------------------------------------------------------------------------
# Registration and renewal fees
# ---------------------------------------------------------------------------

@db_tx.atomic
def record_fee(membership, *, kind, amount=None, date=None, user=None,
               channel=None, note=""):
    """Receipt a registration or renewal fee.

    A fee is money like any other, so it goes through the same
    `record_contribution` path and lands in the ledger as an ordinary receipt —
    there is no separate "fee" accounting. What differs is only WHERE it lands:
    the settings' `registration_fee_fund`, if one is configured (a church that
    wants fees kept out of the benefit pool), and otherwise the scheme's own fund.

    Recording the fee also *does* something: it satisfies the policy's
    registration check, or moves the renewal date on. A fee that is receipted but
    does not update the membership is a fee the eligibility engine cannot see.
    """
    from benevolent.models import BenevolentSettings
    if kind not in ("REGISTRATION", "RENEWAL"):
        raise ValidationError("A fee is either a REGISTRATION or a RENEWAL fee.")

    date = date or _dt.date.today()
    scheme = membership.scheme
    policy = scheme.policy_on(date)
    if policy is None:
        raise ValidationError("No policy is in force, so no fee is due.")

    if amount is None:
        amount = (policy.registration_fee if kind == "REGISTRATION"
                  else policy.renewal_fee)
    amount = Decimal(amount or 0)
    if amount <= 0:
        raise ValidationError(f"This policy charges no {kind.lower()} fee.")

    settings = BenevolentSettings.get()
    fund = settings.registration_fee_fund or scheme.fund

    label = "Registration fee" if kind == "REGISTRATION" else "Renewal fee"
    contribution = record_contribution(
        scheme, date=date, amount=amount, user=user, membership=membership,
        channel=channel, note=(note or f"{label} — {membership.number}"),
        reference=f"{scheme.code} {label.upper()}", fund=fund,
        # a fee is not a due, and must never be able to settle one
        kind=BenevolentContribution.Kind.FEE, period_label="")

    if kind == "REGISTRATION":
        membership.registration_fee_paid = True
        membership.save(update_fields=["registration_fee_paid"])
    else:
        _advance_renewal(membership, policy, date)
    return contribution


def _advance_renewal(membership, policy, on):
    """Move a membership's subscription on by one renewal period."""
    step = 2 if policy.renewal_period == SchemePolicy.RenewalPeriod.BIENNIAL else 1
    base = membership.renewed_until or membership.renewal_due_on(policy, as_of=on) or on
    try:
        nxt = base.replace(year=base.year + step)
    except ValueError:      # 29 Feb
        nxt = base.replace(year=base.year + step, day=28)
    membership.renewed_until = nxt
    if membership.status == SchemeMembership.Status.EXPIRED:
        membership.status = SchemeMembership.Status.ACTIVE
    membership.save(update_fields=["renewed_until", "status"])
    return nxt
