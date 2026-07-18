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


# NOTE: `periods_between(start, end, frequency)` was removed here.
#
# Its docstring claimed to be "the single definition of 'which periods have
# fallen due', shared by the arrears calculation and the dues schedule" — and
# that claim had quietly become false. Phase 10's N+1 fix rewrote `_dues_rows()`
# to resolve the policy in force for each DAY as it steps, because the dues
# FREQUENCY itself can change between policy versions; a function taking a
# single fixed `frequency` argument structurally cannot express that, so
# `_dues_rows()` stopped calling it and nothing else ever did.
#
# Deleted rather than left in place: a dead function asserting it is the single
# source of truth for a rule that has since moved is worse than no function at
# all — it is precisely how a future change gets made in the wrong place, and
# nobody notices because the code they edited was never running.
#
# The rule now lives in exactly one place: `_dues_rows()` below.


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


def contributions_in_period(membership, period_label, on_or_before=None):
    """Dues paid AGAINST one period, optionally only those received on or before a
    date. The `on_or_before` bound is what lets the unbroken-record rule ask
    'was this period paid ON TIME', as distinct from 'is it paid now' — a period
    back-paid months late is settled today but was still missed at the time."""
    qs = contributions_qs(membership=membership, period_label=period_label,
                          kinds=DUES_KINDS)
    if on_or_before is not None:
        qs = qs.filter(transaction__date__lte=on_or_before)
    return qs.aggregate(t=Sum("transaction__amount"))["t"] or Decimal(0)


# What actually counts against a member's dues — and nothing else does. Defined on
# the model so there is ONE list, not one here and a different one in a report.
DUES_KINDS = BenevolentContribution.SETTLES_DUES


def levy_collected(case) -> Decimal:
    """What the levy for one case has actually raised. This IS the benefit under a
    pooled (harambee) policy, so it is a figure the engine depends on — not a
    reporting nicety."""
    return contributions_total(case=case)


def levy_paid_by(membership, case) -> Decimal:
    # A batch pass can pre-load every (membership, case) levy total in one grouped
    # query and stash the per-member slice here, keyed by case id — so the
    # missed-case-levies scan makes no per-case round trip. Same total either way.
    cache = getattr(membership, "_levy_paid_cache", None)
    if cache is not None:
        return cache.get(case.pk, Decimal(0))
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

    # Phase 10: every policy version this scheme has EVER published, fetched
    # ONCE, and resolved in memory from here on. This used to be
    # `scheme.policy_on(d)` — a database query — called once per DAY between
    # a member's cover date and `as_of`. For a member of a few years'
    # standing that is well over a thousand queries to answer one question,
    # and `_dues_rows` is called for every member on every arrears
    # calculation — the dashboard, the eligibility engine, every report in
    # Phase 8, the reminder job. A measured query-count regression test
    # (benevolent/test_phase10.py) is what actually caught this; it is
    # exactly the "verify performance and scalability" a production
    # readiness review exists to do. The resolution RULE is unchanged — see
    # BenevolentScheme.policy_on's own docstring for why SUPERSEDED versions
    # must still resolve for dates inside their own window — only WHERE it
    # runs (Python, once cached, not the database, repeatedly) has changed.
    # Reuse the per-instance version cache when a prior call in the same pass
    # (e.g. arrears_for, then the tenure facts, both inside one facts_for) has
    # already fetched them — the identical query against the identical rows.
    # The cache is only ever this same scheme's ACTIVE/SUPERSEDED versions, so
    # reading it changes nothing but the number of round trips.
    versions = getattr(membership, "_policy_versions", None)
    if versions is None:
        versions = list(scheme.policies.filter(
            status__in=[SchemePolicy.Status.ACTIVE, SchemePolicy.Status.SUPERSEDED]
        ).order_by("-effective_from", "-version"))
    if not versions:
        return []
    # _waived_periods (called below, in this same pass) needs to resolve the
    # policy in force on an exemption's start date — the same versions we have
    # just fetched. Stashed here so it resolves in memory rather than issuing a
    # second, identical query against the same rows.
    membership._policy_versions = versions

    def _policy_at(d):
        for v in versions:                 # already ordered latest-first
            if v.effective_from <= d and (v.effective_to is None or v.effective_to >= d):
                return v
        return None

    first = min(v.effective_from for v in versions)
    # The caller (arrears_for) needs this same boundary and used to re-query the
    # database for it — the identical value this function has already computed in
    # memory. Stashed on the membership instance so it is fetched once, not twice.
    membership._dues_window_start = first
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
        policy = _policy_at(d)
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

    # What has actually been PAID against each of these periods — fetched ONCE,
    # grouped by period, rather than one query per period.
    #
    # This is recommendation #70b, closed. Phase 10 fixed the catastrophic N+1
    # here (policy_on() was being queried once per DAY of membership history);
    # this is the smaller one it deliberately left, honestly logged rather than
    # rushed: `contributions_total()` was still being called once per dues
    # PERIOD, so a member with three years of monthly dues cost ~36 queries to
    # answer "what do they owe" — and arrears_for() runs for every active member
    # on the dashboard, the arrears report and every standing recomputation.
    # Measured at ~22 queries per member on the dashboard before this change.
    #
    # The RULE is unchanged: `_effective_q()` still defines which contributions
    # count (the same definition core.metrics uses for fund receipts), and
    # DUES_KINDS still defines which kinds settle dues. Only the number of round
    # trips has changed — a single grouped aggregate instead of one query per
    # period. `contributions_total()` remains the single definition of the sum
    # and is still what every other caller uses; this is that same query, asked
    # once for every period at once.
    # A batch pass can pre-load every member's paid-by-period totals in one
    # grouped query keyed by (membership, period) and stash the per-member slice
    # here, so this per-member aggregate is skipped. Same values either way — it
    # is the identical _effective_q()/DUES_KINDS query, asked once for the whole
    # scheme instead of once per member.
    paid_by_period = getattr(membership, "_paid_by_period_cache", None)
    if paid_by_period is None:
        paid_by_period = dict(
            BenevolentContribution.objects
            .filter(membership=membership, kind__in=DUES_KINDS)
            .filter(_effective_q())
            .values_list("period_label")
            .annotate(total=Sum("transaction__amount"))
        )

    for r in rows:
        r["waived"] = r["period"] in waived
        if r["waived"]:
            r["due"] = Decimal(0)
        r["paid"] = paid_by_period.get(r["period"]) or Decimal(0)
        r["outstanding"] = max(Decimal(0), r["due"] - r["paid"])
    return rows


def _waived_periods(membership, as_of=None):
    """The dues periods a member does not owe for — because they were bereaved, or
    because they are exempt.

    Both are RULES, and both belong HERE rather than in the standing engine or the
    UI, for one reason: `arrears_for()` is the single place in this system that
    knows what a member owes, and it is called by the register, the eligibility
    engine and the arrears DEDUCTION on a benefit. If exemptions were applied
    anywhere else, an exempt member would show as clear on the register and STILL
    have money docked from their bereavement payout. There must be exactly one
    answer to "what does this member owe", and this is where it is computed.
    """
    from benevolent.models import BenevolentCase
    from benevolent.services.standing import live_exemption

    as_of = as_of or _dt.date.today()
    out = set()

    def _policy_on(d):
        """The policy in force on `d` — resolved against the version list
        `_dues_rows` has already fetched, where this is called as part of that
        pass (the overwhelmingly common case), and by query otherwise. Same
        resolution rule either way: see BenevolentScheme.policy_on."""
        cached = getattr(membership, "_policy_versions", None)
        if cached is None:
            return membership.scheme.policy_on(d)
        for v in cached:               # already ordered latest-first
            if v.effective_from <= d and (v.effective_to is None or v.effective_to >= d):
                return v
        return None

    # --- exemptions: an excused member owes nothing for the excused period ---
    for ex in membership.exemptions.all():
        if not ex.exempt_dues or not ex.is_approved:
            continue
        policy = _policy_on(ex.from_date)
        if policy is None:
            continue
        freq = policy.contribution_frequency
        d = ex.from_date
        end = min(ex.to_date or as_of, ex.revoked_on or as_of, as_of)
        while d <= end:
            out.add(period_label_for(d, freq))
            d = (d.replace(day=28) + _dt.timedelta(days=7)).replace(day=1)

    # --- an automatic age exemption is an exemption like any other -----------
    policy_now = _policy_on(as_of)
    if policy_now is not None and policy_now.exemption_age and membership.date_of_birth:
        dob = membership.date_of_birth
        exempt_from = dob.replace(year=dob.year + policy_now.exemption_age)
        d = max(exempt_from, membership.cover_from)
        while d <= as_of:
            out.add(period_label_for(d, policy_now.contribution_frequency))
            d = (d.replace(day=28) + _dt.timedelta(days=7)).replace(day=1)

    cached_cases = getattr(membership, "_own_settled_cases_cache", None)
    if cached_cases is not None:
        cases = cached_cases
    else:
        cases = membership.cases.filter(
            status__in=[BenevolentCase.Status.APPROVED, BenevolentCase.Status.PARTLY_PAID,
                        BenevolentCase.Status.PAID, BenevolentCase.Status.CLOSED])
    for case in cases:
        policy = case.policy or _policy_on(case.event_date)
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
    # _dues_rows just fetched every policy version and computed this boundary;
    # re-querying the database for the same value here was a second round trip
    # for an answer already in memory. It always sets this before returning a
    # non-empty rows list, so the fallback below is belt-and-braces, not a
    # path this actually takes.
    first = getattr(membership, "_dues_window_start", None)
    if first is None:
        scheme = membership.scheme
        first = (scheme.policies
                 .filter(status__in=[SchemePolicy.Status.ACTIVE,
                                     SchemePolicy.Status.SUPERSEDED])
                 .order_by("effective_from")
                 .values_list("effective_from", flat=True).first())
    start = max(membership.cover_from, first)
    end = min(as_of, membership.left_on or as_of)
    # Only DUES pay off dues. A levy, a registration fee and a donation are all
    # money the member has given the scheme, and none of them is a subscription.
    # A batch pass may have already summed dues-paid per member (the same
    # _effective_q()/DUES_KINDS total); use it rather than re-query.
    paid_cache = getattr(membership, "_dues_paid_total_cache", None)
    if paid_cache is not None:
        paid = paid_cache
    else:
        paid = contributions_total(membership=membership, start=start, end=end,
                                   kinds=DUES_KINDS)

    # Phase 4: the obligations ledger. A penalty charged INCREASES what is owed; a
    # waiver or a write-off REDUCES it. Neither is money and neither posts — see
    # models_contrib.MemberAdjustment. Folding them in here, rather than anywhere
    # else, is what keeps this the ONE function that knows what a member owes: the
    # register, the eligibility engine, the arrears deduction on a benefit and the
    # member's own statement all ask it, and they all get the same answer.
    from benevolent.services.engine import adjustments_total
    adjustments = adjustments_total(membership, as_of=as_of)

    return max(Decimal(0), due + adjustments - paid)


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
                        kind=None, payer_type=None, payer_name=""):
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
            # only a member can owe anything, so money from a non-member is a gift
            kind = BenevolentContribution.Kind.DONATION
        elif policy and policy.contribution_mode in periodic:
            kind = BenevolentContribution.Kind.DUES
        else:
            # an enrolled member giving to a scheme with no dues is contributing
            # voluntarily — which is not the same as a stranger's donation, and the
            # member's statement should not call it one
            kind = BenevolentContribution.Kind.VOLUNTARY

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

    # Who paid, where the caller said. Default SELF; a memberless donation with
    # no named payer is genuinely anonymous, so record it as such rather than
    # leaving a misleading SELF on a row that has no member at all.
    if payer_type is None:
        if membership is None and not payer_name:
            payer_type = BenevolentContribution.PayerType.ANONYMOUS
        else:
            payer_type = BenevolentContribution.PayerType.SELF

    contribution = BenevolentContribution.objects.create(
        scheme=scheme, membership=membership, transaction=txn, kind=kind,
        period_label=period_label or "", case=case, note=note, recorded_by=user,
        payer_type=payer_type, payer_name=(payer_name or "")[:120])

    # a receipt saved before the index row existed is re-posted so the ledger
    # sees the final shape of the document (same belt-and-braces as loans)
    _repost(txn)

    if case is not None and case.funding_target:
        _maybe_announce_funding_reached(case)
    return contribution


def _maybe_announce_funding_reached(case):
    """Tell whoever the settings say should hear it, the FIRST time a case's
    funding target is met — never on every contribution after, and never if
    the case has no target to measure against in the first place."""
    case.refresh_from_db(fields=["funding_target"])
    if not case.funding_target or not case.funding_fully_raised:
        return
    from benevolent.models import CaseEvent
    if case.events.filter(kind=CaseEvent.Kind.FUNDING_REACHED).exists():
        return
    from benevolent.services.cases import log as case_log
    from benevolent.services.cases import _notify
    case_log(case, CaseEvent.Kind.FUNDING_REACHED,
            f"Funding target of {case.funding_target} reached "
            f"({case.funding_collected} collected).", automated=True)
    _notify(case, "funding_target_reached",
            f"{case.number} has reached its funding target of {case.funding_target}.")


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

    The bereaved member's own line is worked out from the SAME weight function
    the entitlement calculation uses (`eligibility._bereaved_weight`), so this
    roster and the benefit computation can never disagree about how much — or
    whether — the bereaved member owes towards their own case:

      * EXEMPT               — off the roster entirely.
      * CONTRIBUTES / REDUCED, collected by DEDUCTION — off the roster too
        (collected from their benefit instead; leaving them on it as well
        would charge them twice for the one contribution — a real bug found
        and fixed while building this).
      * CONTRIBUTES / REDUCED, collected normally — on the roster, at the
        full or reduced amount.
      * COMMITTEE_DECIDES, undecided — off the roster (nobody is chased on
        the strength of a rule nobody has actually applied yet); decided
        "must contribute" — on the roster in full; decided "waived" — off.
    """
    from benevolent.services.eligibility import _bereaved_weight

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
        if case.membership_id == m.pk:
            weight = _bereaved_weight(policy, case)
            if weight <= 0 or policy.bereaved_deduct_own_levy:
                exempt.append(m)
                continue
            due = (per_member * weight).quantize(Decimal("0.01"))
        else:
            # a member formally excused from levies is not on the roster either.
            # Left out, they would be chased for money the church has already
            # decided in writing that they do not owe — which is worse than not
            # having the exemption at all, because now it is on file and being
            # ignored.
            from benevolent.services.standing import live_exemption
            ex = live_exemption(m, case.event_date)
            if ex and ex.exempt_levies:
                exempt.append(m)
                continue
            due = per_member
        paid = levy_paid_by(m, case)
        rows.append({"membership": m, "due": due, "paid": paid,
                     "outstanding": max(Decimal(0), due - paid)})

    expected = sum((r["due"] for r in rows), Decimal(0))
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
        kind=(BenevolentContribution.Kind.REGISTRATION if kind == "REGISTRATION"
              else BenevolentContribution.Kind.RENEWAL),
        period_label="")

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
    # Note what this does NOT do any more: it does not "un-expire" the membership.
    # An overdue renewal was never a lifecycle decision — it was a derived fact —
    # so paying the renewal simply changes the fact, and standing recomputes from
    # it. There is no status to put back, which is precisely the point of having
    # separated the two.
    membership.save(update_fields=["renewed_until"])
    from benevolent.services import standing as _standing
    _standing.refresh(membership)
    from benevolent.services import notify as notify_svc
    from benevolent.models import NotificationEvent
    notify_svc.send(NotificationEvent.RENEWAL_CONFIRMED, membership=membership)
    return nxt
