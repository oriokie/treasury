"""The policy engine.

Given a scheme, a claimant and an event, this decides two things:

    * is the claim ELIGIBLE?   — by running every rule the policy carries
    * what is it WORTH?        — by applying the policy's benefit calculation

Both answers are produced by reading SchemePolicy / SchemeBenefitRule fields and
nothing else. There is no scheme-specific branching anywhere in this module: a
Medical Fund and a Bereavement Fund differ only in the values on their policy
rows. That is what makes this an engine rather than one hard-coded fund.

Phase 2 grew the rule set from 11 checks to 17, and the benefit calculation from
four modes to six, without changing that shape by one line. A rule is still one
policy field plus one small `_check_*` function of (policy, facts) → Check; a
benefit calculation is still one branch of `compute_entitlement`. That was the
test of whether Phase 1's engine was designed right, and it passed.

Transparency remains a design requirement, not a nicety. The engine never returns
a bare yes/no — it returns every check it ran, whether each passed, and the actual
figures compared. That structure is what gets frozen onto the case as its
permanent `eligibility_snapshot`, so an auditor can reconstruct exactly why a
claim was allowed or refused years later.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import asdict, dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from benevolent.models import (BenevolentCase, BenevolentScheme, SchemeBenefitRule,
                               SchemeMembership, SchemePolicy)


def _money(v) -> Decimal:
    return Decimal(v or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


_ROUND_TO = {
    SchemePolicy.Rounding.NONE: None,
    SchemePolicy.Rounding.TEN: Decimal("10"),
    SchemePolicy.Rounding.HUNDRED: Decimal("100"),
    SchemePolicy.Rounding.THOUSAND: Decimal("1000"),
}


@dataclass(frozen=True)
class Check:
    """One rule, evaluated. `code` is stable and machine-readable so an
    integration (or a future report) can key off it; `detail` is the human
    sentence, always stating the figures actually compared."""
    code: str
    label: str
    passed: bool
    detail: str = ""
    blocking: bool = True     # a non-blocking check is advisory only

    def as_dict(self):
        return asdict(self)


@dataclass
class Entitlement:
    """What the policy says the claim is worth, and how that was arrived at."""
    amount: Decimal = Decimal(0)
    basis: str = ""                                 # the mode used, in words
    workings: list = field(default_factory=list)    # each step, in order
    deductions: list = field(default_factory=list)  # each deduction, in order

    def as_dict(self):
        return {"amount": str(_money(self.amount)), "basis": self.basis,
                "workings": list(self.workings), "deductions": list(self.deductions)}


@dataclass
class EligibilityResult:
    eligible: bool
    checks: list                          # list[Check]
    entitlement: Entitlement
    policy: Optional[SchemePolicy] = None
    evaluated_on: Optional[_dt.date] = None

    @property
    def blocking_failures(self):
        return [c for c in self.checks if c.blocking and not c.passed]

    @property
    def warnings(self):
        return [c for c in self.checks if not c.blocking and not c.passed]

    def as_dict(self):
        """The JSON-safe form frozen onto a case. Deliberately self-contained:
        it records the answer, every check behind it, the entitlement workings
        and which policy version was applied."""
        return {
            "eligible": self.eligible,
            "policy_version": self.policy.version if self.policy else None,
            "policy_id": self.policy.pk if self.policy else None,
            "evaluated_on": (self.evaluated_on or _dt.date.today()).isoformat(),
            "checks": [c.as_dict() for c in self.checks],
            "entitlement": self.entitlement.as_dict(),
        }


# ---------------------------------------------------------------------------
# The individual rules. Each is a small, isolated function of (policy, facts)
# -> Check, so a new rule is one function plus one policy field — never a change
# to the flow at the bottom of this file.
# ---------------------------------------------------------------------------

def _check_scheme_open(scheme) -> Check:
    ok = scheme.is_open
    return Check("scheme_open", "Scheme is open to new cases", ok,
                 "The scheme is active." if ok
                 else f"The scheme is {scheme.get_status_display().lower()}.")


def _check_membership(policy, membership) -> Check:
    if not policy.membership_required:
        return Check("membership", "Membership", True,
                     "This policy allows claims from non-members.", blocking=False)
    if membership is None:
        return Check("membership", "Claimant is enrolled", False,
                     "The policy requires enrolment, and no membership was given.")
    # SUSPENDED / EXPELLED / WITHDRAWN / PENDING members are not covered. LAPSED
    # and INACTIVE deliberately are NOT listed: whether those bar a claim is a
    # question for the arrears and inactivity rules, which may still let it
    # through (with a deduction). That is a policy decision, and it must not be
    # pre-empted by a status lookup buried here.
    bad = (SchemeMembership.Status.SUSPENDED, SchemeMembership.Status.EXPELLED,
           SchemeMembership.Status.WITHDRAWN, SchemeMembership.Status.PENDING)
    ok = membership.status not in bad
    return Check("membership", "Claimant is enrolled and covered", ok,
                 f"Membership {membership.number} is "
                 f"{membership.get_status_display().lower()}.")


def _check_registration(policy, membership) -> Check:
    """Formal admission: admitted, fee paid, papers on file."""
    label = "Registration complete"
    if not policy.registration_required:
        return Check("registration", label, True,
                     "This policy does not require formal registration.", blocking=False)
    if membership is None:
        return Check("registration", label, False, "No membership to check.")
    missing = []
    if not membership.registered_on:
        missing.append("not yet admitted")
    if policy.registration_fee and not membership.registration_fee_paid:
        missing.append(f"registration fee of {_money(policy.registration_fee)} unpaid")
    if policy.require_registration_form and not membership.registration_form_on_file:
        missing.append("no signed application form on file")
    if policy.require_id_document and not membership.id_document_on_file:
        missing.append("no identity document on file")
    ok = not missing
    return Check("registration", label, ok,
                 "Registration is complete."
                 if ok else "; ".join(missing).capitalize() + ".")


def _check_joining_age(policy, membership) -> Check:
    """Age AT JOINING, not age today. A scheme that caps entry at 70 does not
    throw a member out on their 71st birthday — and reading it the other way
    would quietly cancel the cover of exactly the people most likely to need it."""
    label = "Joining age within limits"
    if not (policy.min_age or policy.max_age):
        return Check("joining_age", label, True, "No age limits apply.", blocking=False)
    if membership is None or not membership.date_of_birth:
        return Check("joining_age", label, True,
                     "No date of birth on file, so the age limits cannot be checked.",
                     blocking=False)
    dob = membership.date_of_birth
    joined = membership.cover_from
    age = joined.year - dob.year - ((joined.month, joined.day) < (dob.month, dob.day))
    if policy.min_age and age < policy.min_age:
        return Check("joining_age", label, False,
                     f"Aged {age} at joining; the minimum is {policy.min_age}.")
    if policy.max_age and age > policy.max_age:
        return Check("joining_age", label, False,
                     f"Aged {age} at joining; the maximum is {policy.max_age}.")
    return Check("joining_age", label, True, f"Aged {age} at joining.")


def _check_waiting_period(policy, membership, event_date, rule) -> Check:
    """The waiting period runs from `membership.cover_from` — see that property
    for why reinstatement moves it."""
    days = policy.waiting_period_days
    if rule is not None and rule.waiting_period_days is not None:
        days = rule.waiting_period_days
    if membership is not None and membership.reinstated_on \
            and policy.reinstatement_waiting_days:
        days = max(days or 0, policy.reinstatement_waiting_days)
    label = "Waiting period served"
    if not days:
        return Check("waiting_period", label, True, "No waiting period applies.",
                     blocking=False)
    if membership is None:
        return Check("waiting_period", label, False,
                     "No membership, so the waiting period cannot be verified.")
    served = (event_date - membership.cover_from).days
    ok = served >= days
    since = ("reinstatement" if membership.reinstated_on
             else "registration" if membership.registered_on else "enrolment")
    return Check("waiting_period", label, ok,
                 f"{served} day(s) from {since} to the event; {days} required.")


def _check_min_contributions(policy, membership) -> Check:
    n = policy.min_contributions
    label = "Minimum contributions made"
    if not n:
        return Check("min_contributions", label, True,
                     "No minimum contribution count applies.", blocking=False)
    if membership is None:
        return Check("min_contributions", label, False,
                     "No membership, so contributions cannot be counted.")
    made = membership.contribution_count
    ok = made >= n
    return Check("min_contributions", label, ok,
                 f"{made} contribution(s) recorded; {n} required.")


def _check_arrears(policy, membership, event_date) -> Check:
    """Arrears are not automatically a bar.

    A policy chooses IGNORE, BLOCK or DEDUCT — and DEDUCT (pay the benefit, net
    off what is owed) is what most real constitutions actually do, because
    refusing a bereaved family over two months of dues is not what a welfare
    scheme is for. Only BLOCK is a blocking check; DEDUCT surfaces as an advisory
    warning here and is applied in compute_entitlement.
    """
    label = "Contributions up to date"
    treatment = policy.arrears_treatment
    # backwards compatibility with Phase 1: the old boolean still means BLOCK
    if policy.arrears_block and treatment == SchemePolicy.ArrearsTreatment.IGNORE:
        treatment = SchemePolicy.ArrearsTreatment.BLOCK
    if treatment == SchemePolicy.ArrearsTreatment.IGNORE:
        return Check("arrears", label, True,
                     "Arrears do not affect a claim under this policy.", blocking=False)
    if membership is None:
        return Check("arrears", label, False,
                     "No membership, so arrears cannot be assessed.")
    from benevolent.services.contributions import arrears_for
    owed = arrears_for(membership, policy, as_of=event_date)
    allowed = policy.max_arrears_allowed or Decimal(0)
    ok = owed <= allowed
    if treatment == SchemePolicy.ArrearsTreatment.DEDUCT:
        return Check("arrears", label, ok,
                     f"Arrears of {_money(owed)} at the event date. This policy pays the "
                     f"benefit and deducts them, so the claim is not refused.",
                     blocking=False)
    return Check("arrears", label, ok,
                 f"Arrears of {_money(owed)} at the event date; up to {_money(allowed)} "
                 f"permitted before a claim is blocked.")


def _check_renewal(policy, membership, event_date) -> Check:
    label = "Subscription renewed"
    if not policy.renewal_required or \
            policy.renewal_period == SchemePolicy.RenewalPeriod.NONE:
        return Check("renewal", label, True, "This policy has no renewal.", blocking=False)
    if membership is None:
        return Check("renewal", label, False, "No membership to check.")
    due = membership.renewal_due_on(policy, as_of=event_date)
    if not membership.renewal_overdue(policy, as_of=event_date):
        return Check("renewal", label, True,
                     f"Renewed to {due:%d %b %Y}." if due else "Renewal is up to date.")
    detail = (f"Renewal fell due on {due:%d %b %Y} and the "
              f"{policy.renewal_grace_days}-day grace period had passed by the event.")
    # a policy that does NOT lapse on non-renewal still wants this seen — hence an
    # advisory check rather than silence
    return Check("renewal", label, False, detail,
                 blocking=bool(policy.lapse_on_non_renewal))


def _check_inactivity(policy, membership, event_date) -> Check:
    label = "Member is contributing"
    if not policy.inactivity_months or \
            policy.inactivity_action == SchemePolicy.InactivityAction.NONE:
        return Check("inactivity", label, True, "No inactivity rule applies.",
                     blocking=False)
    if membership is None:
        return Check("inactivity", label, False, "No membership to check.")
    months = membership.months_since_contribution(as_of=event_date)
    ok = months < policy.inactivity_months
    # FLAG means "note it", not "refuse it" — only the harder actions bar a claim
    blocking = policy.inactivity_action in (
        SchemePolicy.InactivityAction.SUSPEND, SchemePolicy.InactivityAction.LAPSE,
        SchemePolicy.InactivityAction.EXPEL)
    return Check("inactivity", label, ok,
                 f"{months} month(s) since the last contribution; this policy treats "
                 f"{policy.inactivity_months} as inactive "
                 f"({policy.get_inactivity_action_display().lower()}).",
                 blocking=blocking)


def _check_claim_window(policy, event_date, reported_date) -> Check:
    days = policy.claim_window_days
    label = "Reported within the claim window"
    if not days:
        return Check("claim_window", label, True, "No claim window applies.",
                     blocking=False)
    elapsed = (reported_date - event_date).days
    ok = elapsed <= days
    return Check("claim_window", label, ok,
                 f"Reported {elapsed} day(s) after the event; the window is {days} day(s).")


def _check_event_covered(policy, event_type, rule) -> Check:
    label = "Event is covered"
    if event_type is None:
        return Check("event_covered", label, False, "No event type was given.")
    if not event_type.active:
        return Check("event_covered", label, False,
                     f"'{event_type.name}' is no longer a covered event.")
    if policy.benefit_mode == SchemePolicy.BenefitMode.SCHEDULE and rule is None:
        return Check("event_covered", label, False,
                     f"The benefit schedule has no line for '{event_type.name}', "
                     f"so it is not covered by this policy version.")
    return Check("event_covered", label, True, f"'{event_type.name}' is covered.")


def _check_beneficiary_covered(policy, membership, case, event_type) -> Check:
    """The household rules: is the PERSON the benefit is for actually covered?

    A dependant registered after the event does not count — that is the entire
    reason dependants are registered in advance rather than named afterwards.
    """
    label = "Beneficiary is covered"
    dep = getattr(case, "dependant", None) if case is not None else None
    if dep is None:
        return Check("beneficiary", label, True,
                     "The claim is for the member themselves.", blocking=False)
    if event_type is not None and not event_type.covers_dependants:
        return Check("beneficiary", label, False,
                     f"'{event_type.name}' does not cover dependants.")
    event_date = case.event_date

    if dep.registered_on and dep.registered_on > event_date:
        return Check("beneficiary", label, False,
                     f"{dep.name} was registered on {dep.registered_on:%d %b %Y}, AFTER the "
                     f"event on {event_date:%d %b %Y}. A dependant must be on record before "
                     f"the event to be covered.")
    if not dep.active:
        return Check("beneficiary", label, False, f"{dep.name} is no longer covered.")

    if policy.dependant_age_limit and dep.date_of_birth and \
            dep.relationship == dep.Relationship.CHILD:
        b = dep.date_of_birth
        age = (event_date.year - b.year
               - ((event_date.month, event_date.day) < (b.month, b.day)))
        if age > policy.dependant_age_limit:
            return Check("beneficiary", label, False,
                         f"{dep.name} was {age} at the event; children are covered to "
                         f"{policy.dependant_age_limit}.")

    if policy.max_dependants and membership is not None:
        covered = list(membership.dependants.filter(active=True)
                       .order_by("registered_on", "id"))
        if policy.spouse_auto_covered:
            covered = [d for d in covered if d.relationship != d.Relationship.SPOUSE]
        allowed_ids = [d.pk for d in covered[:policy.max_dependants]]
        is_spouse = dep.relationship == dep.Relationship.SPOUSE
        if dep.pk not in allowed_ids and not (is_spouse and policy.spouse_auto_covered):
            return Check("beneficiary", label, False,
                         f"This membership covers {policy.max_dependants} dependant(s), and "
                         f"{dep.name} is beyond that number (cover goes to those registered "
                         f"first).")
    return Check("beneficiary", label, True, f"{dep.name} is a covered dependant.")


def _check_claim_frequency(policy, membership, event_type, event_date, rule,
                           exclude_case=None) -> Check:
    """Per-year caps: on the number of cases overall, and on the number of this
    event type. Counted on decided cases only (a rejected or cancelled claim has
    not consumed anything)."""
    label = "Within the annual claim limit"
    limits = []
    if policy.max_claims_per_year:
        limits.append(("any", policy.max_claims_per_year, None))
    if rule is not None and rule.max_per_year:
        limits.append(("event", rule.max_per_year, event_type))
    if not limits:
        return Check("claim_frequency", label, True, "No annual claim limit applies.",
                     blocking=False)
    if membership is None:
        return Check("claim_frequency", label, True,
                     "No membership, so per-member limits do not apply.", blocking=False)

    counted = [BenevolentCase.Status.APPROVED, BenevolentCase.Status.PARTLY_PAID,
               BenevolentCase.Status.PAID, BenevolentCase.Status.CLOSED]
    details = []
    ok = True
    for scope, limit, et in limits:
        qs = membership.cases.filter(status__in=counted, event_date__year=event_date.year)
        if et is not None:
            qs = qs.filter(event_type=et)
        if exclude_case is not None and exclude_case.pk:
            qs = qs.exclude(pk=exclude_case.pk)
        used = qs.count()
        if used >= limit:
            ok = False
        what = "claim(s)" if scope == "any" else f"'{et.name}' claim(s)"
        details.append(f"{used} of {limit} {what} used in {event_date.year}")
    return Check("claim_frequency", label, ok, "; ".join(details))


def _check_annual_benefit_cap(policy, membership, event_date, exclude_case=None) -> Check:
    label = "Within the annual benefit cap"
    cap = policy.max_benefit_per_year or Decimal(0)
    if not cap:
        return Check("annual_cap", label, True, "No annual benefit cap applies.",
                     blocking=False)
    if membership is None:
        return Check("annual_cap", label, True,
                     "No membership, so the per-member cap does not apply.", blocking=False)
    qs = membership.cases.filter(
        status__in=[BenevolentCase.Status.APPROVED, BenevolentCase.Status.PARTLY_PAID,
                    BenevolentCase.Status.PAID, BenevolentCase.Status.CLOSED],
        event_date__year=event_date.year)
    if exclude_case is not None and exclude_case.pk:
        qs = qs.exclude(pk=exclude_case.pk)
    used = sum((c.approved_amount or Decimal(0) for c in qs), Decimal(0))
    ok = used < cap
    return Check("annual_cap", label, ok,
                 f"{_money(used)} of {_money(cap)} used in {event_date.year}.")


def _check_documents(policy, event_type, case) -> Check:
    label = "Supporting document attached"
    needed = policy.require_documents or (
        event_type is not None and event_type.requires_document)
    if not needed:
        return Check("documents", label, True, "No document is required.", blocking=False)
    if case is None or not case.pk:
        return Check("documents", label, False,
                     "A supporting document is required before this case can be approved.")
    n = case.attachments.count()
    ok = n > 0
    return Check("documents", label, ok,
                 f"{n} document(s) attached." if ok
                 else "A supporting document is required and none is attached.")


def _check_nominee(policy, membership, case, event_type) -> Check:
    """Where a policy pays nominees, a claim on the MEMBER'S OWN death needs
    someone on file to pay. This is a gap the engine REPORTS rather than guesses
    about — paying a benefit to whoever turns up at the church office is precisely
    how welfare schemes end up in front of a board."""
    label = "Nominee on file"
    if policy.inheritance_mode != SchemePolicy.InheritanceMode.NOMINEE:
        return Check("nominee", label, True,
                     "This policy does not pay by nomination.", blocking=False)
    if membership is None or case is None:
        return Check("nominee", label, True, "Not applicable.", blocking=False)
    if case.dependant_id:
        return Check("nominee", label, True,
                     "The claim is for a dependant, so it is paid to the member.",
                     blocking=False)
    n = membership.nominees.filter(active=True).count()
    ok = n > 0
    return Check("nominee", label, ok,
                 f"{n} nominee(s) on file." if ok
                 else "This policy pays the member's nominees, and none is on file. Record "
                      "one before the benefit is paid.",
                 blocking=False)


# ---------------------------------------------------------------------------
# Entitlement — what the claim is worth
# ---------------------------------------------------------------------------

def _round(amount, policy):
    step = _ROUND_TO.get(policy.benefit_rounding)
    if not step or amount <= 0:
        return _money(amount)
    return (amount / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step


def compute_entitlement(policy, rule, claimed_amount=None, membership=None,
                        case=None, scheme=None) -> Entitlement:
    """Apply the policy's benefit calculation. Six modes; the per-event rule
    always wins over the policy default where it says something, which is what
    lets one policy carry a whole schedule of different benefits.

    Then the figure is reduced, in this order — and the order matters, because a
    deduction taken before a cap and one taken after it give different answers,
    and a member WILL notice:

        1. cap, floor, rounding      — what the policy will pay at all
        2. arrears / own-levy        — what THIS member is owed of that

    Every step is recorded, so the arithmetic is shown rather than asserted.
    """
    e = Entitlement()
    mode = policy.benefit_mode
    claimed = Decimal(claimed_amount) if claimed_amount is not None else None

    if mode == SchemePolicy.BenefitMode.FIXED:
        base = (rule.amount if (rule is not None and rule.amount)
                else policy.benefit_amount)
        e.amount = _money(base)
        e.basis = "Fixed benefit"
        e.workings.append(f"Fixed benefit: {_money(base)}")

    elif mode == SchemePolicy.BenefitMode.SCHEDULE:
        if rule is None:
            e.basis = "Benefit schedule"
            e.workings.append("No schedule line for this event: nothing payable.")
            return e
        e.amount = _money(rule.amount)
        e.basis = "Benefit schedule"
        e.workings.append(
            f"Schedule line for '{rule.event_type.name}': {_money(rule.amount)}")

    elif mode == SchemePolicy.BenefitMode.PERCENTAGE:
        pct = (rule.percent if (rule is not None and rule.percent)
               else policy.benefit_percent)
        e.basis = "Percentage of the assessed cost"
        if claimed is None or claimed <= 0:
            e.workings.append("No assessed cost given, so no percentage can be applied.")
            return e
        e.amount = _money(claimed * Decimal(pct) / Decimal(100))
        e.workings.append(
            f"{pct}% of the assessed cost {_money(claimed)} = {_money(e.amount)}")

    elif mode == SchemePolicy.BenefitMode.DISCRETIONARY:
        e.basis = "Discretionary, within the cap"
        e.amount = _money(claimed) if claimed else Decimal(0)
        e.workings.append(
            f"Discretionary: the amount requested ({_money(e.amount)}) is taken as the "
            f"starting point; the approver sets the final figure within the cap.")

    elif mode == SchemePolicy.BenefitMode.POOLED:
        # "the family gets whatever the members raise" — the harambee model. The
        # benefit is not a promise made in advance; it is the money actually
        # collected for THIS case, which is why such a scheme can never become
        # insolvent, and why it must be computed from the levy and not guessed.
        e.basis = "Whatever the levy for this case collects"
        if case is None or not case.pk:
            e.workings.append(
                "The benefit is what the levy for this case collects, so it can only be "
                "computed once the case exists and collection has begun.")
            return e
        from benevolent.services.contributions import levy_collected
        got = levy_collected(case)
        e.amount = _money(got)
        e.workings.append(f"Levy collected for {case.number} so far: {_money(got)}")

    elif mode == SchemePolicy.BenefitMode.PER_MEMBER_MULTIPLE:
        # "the levy × the membership" — the benefit the scheme PROMISES if
        # everybody pays. Deliberately distinct from POOLED: that is what was
        # actually raised, this is what was pledged, and a scheme ought to know
        # which of the two it is committing to.
        e.basis = "The levy × the active membership"
        levy = policy.levy_amount or policy.contribution_amount or Decimal(0)
        sch = scheme or (case.scheme if case is not None else None) or (
            membership.scheme if membership is not None else None)
        n = 0
        if sch is not None:
            n = sch.memberships.filter(status=SchemeMembership.Status.ACTIVE).count()
            if policy.bereaved_exempt_own_levy and membership is not None:
                n = max(0, n - 1)
        e.amount = _money(Decimal(levy) * n)
        e.workings.append(
            f"Levy {_money(levy)} × {n} contributing member(s) = {_money(e.amount)}"
            + (" (the bereaved member is not levied for their own case)"
               if policy.bereaved_exempt_own_levy and membership is not None else ""))

    # ---- 1. cap, floor, rounding: what the policy will pay at all ----------
    cap = rule.cap if (rule is not None and rule.cap is not None) else policy.benefit_cap
    if cap is not None and e.amount > cap:
        e.workings.append(f"Capped at {_money(cap)} (was {_money(e.amount)}).")
        e.amount = _money(cap)
    floor = policy.benefit_floor
    if floor is not None and Decimal(0) < e.amount < floor:
        e.workings.append(f"Raised to the minimum benefit of {_money(floor)}.")
        e.amount = _money(floor)
    if policy.benefit_rounding != SchemePolicy.Rounding.NONE and e.amount > 0:
        before = e.amount
        e.amount = _round(e.amount, policy)
        if e.amount != before:
            e.workings.append(
                f"Rounded {policy.get_benefit_rounding_display().lower()}: "
                f"{_money(before)} → {_money(e.amount)}")

    # ---- 2. deductions: what THIS member is owed of it ---------------------
    if membership is not None and e.amount > 0:
        e.amount = _apply_deductions(e, policy, membership, case)

    return e


def _apply_deductions(e, policy, membership, case):
    """Arrears and own-levy deductions.

    Kept separate from the calculation above because they are about the MEMBER,
    not the benefit: two members with the same bereavement are entitled to the
    same benefit, and may still be PAID different amounts because one of them
    owes the scheme money. Conflating the two would make the benefit schedule a
    lie.
    """
    amount = e.amount

    treatment = policy.arrears_treatment
    if treatment == SchemePolicy.ArrearsTreatment.DEDUCT:
        from benevolent.services.contributions import arrears_for
        as_of = case.event_date if (case is not None and case.pk) else None
        owed = arrears_for(membership, policy, as_of=as_of)
        if owed > 0:
            take = min(owed, amount)
            amount -= take
            e.deductions.append(
                f"Arrears of {_money(owed)} deducted ({_money(take)} taken; "
                f"{_money(amount)} payable).")

    # deducting the member's own levy and exempting them from it are two answers
    # to the same question, so only one can apply
    if policy.bereaved_deduct_own_levy and not policy.bereaved_exempt_own_levy:
        levy = Decimal(policy.levy_amount or 0)
        if levy > 0:
            take = min(levy, amount)
            amount -= take
            e.deductions.append(
                f"The member's own levy of {_money(levy)} deducted from their benefit "
                f"({_money(amount)} payable).")

    return _money(max(Decimal(0), amount))


# ---------------------------------------------------------------------------
# The public entry point
# ---------------------------------------------------------------------------

def evaluate(scheme, *, event_type, event_date, membership=None,
             reported_date=None, claimed_amount=None, case=None,
             policy=None) -> EligibilityResult:
    """Run the policy in force at the EVENT date against these facts.

    The policy is resolved from the event date, never from today — so a case
    reported late is still decided by the rules that were in force when the event
    happened, which is the whole reason policies are versioned.
    """
    reported_date = reported_date or _dt.date.today()
    policy = policy or scheme.policy_on(event_date)

    if policy is None:
        return EligibilityResult(
            eligible=False,
            checks=[Check("policy", "A policy was in force", False,
                          f"No policy version was in force on {event_date:%d %b %Y}, so this "
                          f"case cannot be assessed. Publish a policy effective on or before "
                          f"that date.")],
            entitlement=Entitlement(basis="No policy"),
            policy=None, evaluated_on=reported_date)

    rule = policy.rule_for(event_type)

    checks = [
        Check("policy", "A policy was in force", True,
              f"Version {policy.version}, effective {policy.effective_from:%d %b %Y}."),
        _check_scheme_open(scheme),
        _check_event_covered(policy, event_type, rule),
        _check_membership(policy, membership),
        _check_registration(policy, membership),
        _check_joining_age(policy, membership),
        _check_waiting_period(policy, membership, event_date, rule),
        _check_min_contributions(policy, membership),
        _check_arrears(policy, membership, event_date),
        _check_renewal(policy, membership, event_date),
        _check_inactivity(policy, membership, event_date),
        _check_beneficiary_covered(policy, membership, case, event_type),
        _check_claim_window(policy, event_date, reported_date),
        _check_claim_frequency(policy, membership, event_type, event_date, rule,
                               exclude_case=case),
        _check_annual_benefit_cap(policy, membership, event_date, exclude_case=case),
        _check_documents(policy, event_type, case),
        _check_nominee(policy, membership, case, event_type),
    ]

    entitlement = compute_entitlement(policy, rule, claimed_amount,
                                      membership=membership, case=case, scheme=scheme)
    eligible = all(c.passed for c in checks if c.blocking)

    return EligibilityResult(eligible=eligible, checks=checks, entitlement=entitlement,
                             policy=policy, evaluated_on=reported_date)


def evaluate_case(case) -> EligibilityResult:
    """Re-evaluate an existing case against the policy in force at its event
    date. Used by the assess step and by the case screen's live preview."""
    return evaluate(
        case.scheme, event_type=case.event_type, event_date=case.event_date,
        membership=case.membership, reported_date=case.reported_date,
        claimed_amount=case.claimed_amount, case=case)
