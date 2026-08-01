"""The standing engine.

Where does this member stand? One word, computed — never typed in.

    Good standing · Exempt · Grace period · Arrears · Inactive
    (and, dominating all of them, the lifecycle: Pending · Suspended ·
     Withdrawn · Deceased · Closed)

Two things about this module matter more than the code in it.

**1. Standing is DERIVED, and the column is a cache.**
`assess()` is a pure function of (membership, policy, date). Nothing may hand-set
a standing. `refresh()` writes the answer to `SchemeMembership.standing` so the
register can be listed and filtered without recomputing four hundred rows — but
that column is only ever a cache, so recomputing it can never lose information,
and a nightly job writing to it can never overwrite a treasurer's decision (which
lives on `status`, a different column, which this module never touches).

**2. Standing and eligibility must never disagree.**
They answer different questions — standing is a *summary* for the register,
eligibility is the *decision* on a claim — but if they disagreed about a plain fact
like "is this member in arrears", the module would be lying to somebody. So they do
not each compute it. `MembershipFacts` is computed once, here, and both consume it.
There is exactly one place in this system that knows how many months a member is
behind, and this is it.

The distinction is worth keeping straight, because it is where the design earns
its keep:

    standing  = where the member STANDS         (a fact about the member)
    eligibility = whether the claim is PAYABLE  (a decision about a case)

A member in ARREARS may still be paid — that is the arrears *treatment*, which is
the policy's business, and DEDUCT is the commonest answer. Standing reports; the
policy decides.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from django.db import transaction as db_tx

from benevolent.models import (BenevolentCase, MembershipEvent, SchemeMembership,
                               SchemePolicy, Standing)


# ---------------------------------------------------------------------------
# The facts — computed once, consumed by standing AND by eligibility
# ---------------------------------------------------------------------------

@dataclass
class MembershipFacts:
    """Everything true about a member's contributions, as at a date.

    Deliberately a plain data object with no opinions. It says the member owes
    900 and has missed three levies; it does not say whether that is bad. What is
    bad is a matter for the policy, and different schemes answer it differently.
    """
    as_of: _dt.date
    policy: Optional[SchemePolicy] = None
    arrears: Decimal = Decimal(0)
    months_idle: int = 0
    had_something_to_pay: bool = True
    missed_cases: int = 0
    exemption: object = None          # the live MembershipExemption, if any
    age: Optional[int] = None
    days_past_due: int = 0            # how long they have been behind
    renewal_due_on: Optional[_dt.date] = None
    renewal_overdue: bool = False
    grace_days: int = 0
    # tenure / payment-record facts (Phase: eligibility rules)
    paid_periods: int = 0            # dues periods fully settled
    total_periods: int = 0           # dues periods that have fallen due at all
    missed_periods: int = 0          # periods still wholly or partly outstanding
    arrears_periods: int = 0         # periods with any amount still outstanding
    paid_up_since: Optional[_dt.date] = None  # when the record last became fully clear

    @property
    def exempt_from_dues(self):
        return bool(self.exemption) or self.age_exempt

    @property
    def age_exempt(self):
        p, a = self.policy, self.age
        return bool(p and p.exemption_age and a is not None and a >= p.exemption_age)

    @property
    def in_grace(self):
        """Behind, but not yet beyond the policy's grace. While this is true the
        member is COVERED — a grace period that did not cover would not be grace,
        it would just be a politer word for arrears."""
        return self.arrears > 0 and 0 < self.days_past_due <= self.grace_days


def _had_something_to_pay(membership, policy, as_of):
    """Was this member ever actually asked for money in the idle window?

    A dues scheme always has something owing, so the answer is yes. A per-case
    levy scheme only asks when there is a case, so a member who has paid nothing
    for a year because the church buried nobody has done nothing wrong — and a
    rule that cannot tell the difference punishes them for the congregation's
    good fortune.

    Hybrid schemes charge dues as well as levies, so they answer yes too.
    """
    if policy is None:
        return True
    if policy.contribution_mode != SchemePolicy.ContributionMode.PER_CASE_LEVY:
        return True          # dues are owed whether or not anyone has died
    window_start = as_of - _dt.timedelta(days=30 * (policy.inactivity_months or 0))
    start = max(window_start, membership.cover_from or window_start)
    return BenevolentCase.objects.filter(
        scheme=membership.scheme, event_date__gte=start, event_date__lte=as_of,
        status__in=[BenevolentCase.Status.APPROVED,
                    BenevolentCase.Status.PARTLY_PAID,
                    BenevolentCase.Status.PAID,
                    BenevolentCase.Status.CLOSED],
    ).exclude(membership=membership).exists()


def missed_case_levies(membership, policy=None, as_of=None) -> int:
    """How many of the scheme's recent case levies this member did not pay into.

    The measure that matters in a levy scheme, where there are no monthly dues to
    fall behind on. It catches the member who never stands with a bereaved family
    and then expects the family to stand with them — a thing every real welfare
    scheme has a rule about, and which a dues-shaped system cannot even see.

    Two ways to count, per `policy.inactivity_missed_cases_window`:

    CONSECUTIVE (the original, and the default): an unbroken run of misses,
    backwards from the most recent case. A member who missed three levies two
    years ago and has paid every one since is not the problem this mode is for.

    ROLLING_YEAR: any missed levies in the last 12 months, even with paid ones
    in between — for a policy that wants to catch sporadic non-payers, not just
    someone who has stopped paying outright.

    Either way, cases raised for the member themselves are skipped: they were
    never levied for their own bereavement (or, where the policy levies them,
    it comes out of what they receive), so counting it as a miss would punish
    them for being bereaved.
    """
    from benevolent.services.contributions import levy_paid_by

    as_of = as_of or _dt.date.today()
    policy = policy or membership._policy_on_cached(as_of)
    if policy is None or not policy.inactivity_missed_cases:
        return 0
    leviable = (SchemePolicy.ContributionMode.PER_CASE_LEVY,
                SchemePolicy.ContributionMode.HYBRID)
    if policy.contribution_mode not in leviable:
        return 0

    # This case list is per-SCHEME, not per-member — identical for every member
    # of the scheme. A batch pass loads it once and stashes it on the scheme so
    # the per-member scan reuses it instead of re-querying the same rows N times.
    cases = getattr(membership.scheme, "_missed_case_scan_cache", None)
    if cases is None:
        cases = list(BenevolentCase.objects
                     .filter(scheme=membership.scheme,
                             event_date__lte=as_of,
                             status__in=[BenevolentCase.Status.APPROVED,
                                         BenevolentCase.Status.PARTLY_PAID,
                                         BenevolentCase.Status.PAID,
                                         BenevolentCase.Status.CLOSED])
                     .order_by("-event_date"))
    # each member only counts cases from their own cover date onward
    cases = [c for c in cases if c.event_date >= membership.cover_from]

    if policy.inactivity_missed_cases_window == SchemePolicy.MissedCasesWindow.ROLLING_YEAR:
        window_start = as_of - _dt.timedelta(days=365)
        missed = 0
        for case in cases:
            if case.event_date < window_start:
                break                       # cases are ordered newest-first
            if case.membership_id == membership.pk:
                continue                   # never levied for their own case
            if levy_paid_by(membership, case) <= 0:
                missed += 1
        return missed

    missed = 0
    for case in cases:
        if case.membership_id == membership.pk:
            continue                       # never levied for their own case
        if levy_paid_by(membership, case) > 0:
            break                          # they paid this one: the run ends here
        missed += 1
    return missed


def facts_for_scheme(scheme, as_of=None, memberships=None):
    """`facts_for` for EVERY member of a scheme, in a bounded number of queries.

    `facts_for` is correct but expensive in a loop: called per member it issues
    ~15-20 queries each (dues paid, levy payments, the case scan, adjustments,
    exemptions, last contribution…), so a report over a 200-member scheme fired
    thousands of round trips — the N+1 the engineering review flagged as P1-3.

    This does NOT reimplement the facts (a second implementation would drift from
    the register and the eligibility engine — the whole reason `facts_for` is the
    single source). Instead it PRE-LOADS, scheme-wide and in a handful of grouped
    queries, exactly the data each per-member call would otherwise fetch one at a
    time, stashes each member's slice on the membership instance, and then calls
    the SAME `facts_for`. Every leaf query inside `facts_for` finds its answer
    already in a cache, so the loop adds no round trips. Same numbers, a fraction
    of the queries.

    Returns an ordered ``[(membership, MembershipFacts), …]``.
    """
    from decimal import Decimal as _Dec

    from django.db.models import Max, Sum

    from benevolent.models import (BenevolentCase, BenevolentContribution,
                                   SchemeMembership, SchemePolicy)
    from benevolent.services.contributions import (DUES_KINDS, _effective_q,
                                                   period_label_for)

    as_of = as_of or _dt.date.today()
    if memberships is None:
        memberships = list(scheme.memberships.select_related("member")
                           .prefetch_related("exemptions", "adjustments"))
    else:
        memberships = list(memberships)
    if not memberships:
        return []
    mem_ids = [m.pk for m in memberships]

    # (1) Policy versions — once for the scheme, shared by every member.
    versions = list(scheme.policies.filter(
        status__in=[SchemePolicy.Status.ACTIVE, SchemePolicy.Status.SUPERSEDED]
    ).order_by("-effective_from", "-version"))

    # (2) Every dues payment, dated and labelled, loaded ONCE.
    #
    #     Both figures the arrears answer is built from come from this one list:
    #     the windowed TOTAL that `arrears_for` measures against, and the
    #     per-period breakdown the member's statement shows. They used to be two
    #     separate queries, and only the total was windowed by date — so a
    #     payment falling outside a member's own dues window counted towards the
    #     statement and not towards the headline, and the page disagreed with
    #     itself. One source, one window, applied per member below.
    dues_rows = list(BenevolentContribution.objects
                     .filter(membership_id__in=mem_ids, kind__in=DUES_KINDS)
                     .filter(_effective_q())
                     .values_list("membership_id", "transaction__date",
                                  "transaction__amount", "period_label"))
    dues_by_member = {}
    for mid, d, amt, label in dues_rows:
        dues_by_member.setdefault(mid, []).append((d, amt or _Dec(0), label))

    # (4) The case scan for missed-levy counting — per SCHEME, shared by all.
    case_scan = list(BenevolentCase.objects
                     .filter(scheme=scheme, event_date__lte=as_of,
                             status__in=[BenevolentCase.Status.APPROVED,
                                         BenevolentCase.Status.PARTLY_PAID,
                                         BenevolentCase.Status.PAID,
                                         BenevolentCase.Status.CLOSED])
                     .order_by("-event_date"))
    scheme._missed_case_scan_cache = case_scan
    scan_case_ids = [c.pk for c in case_scan]
    # a member's OWN settled cases drive the bereaved-member dues waiver.
    # _waived_periods filters by status only (no date bound), so load with the
    # same filter — grouped by membership — rather than reusing the date-bounded
    # scan above, which would drop a future-dated settled case.
    own_cases_by_member = {}
    for c in (BenevolentCase.objects
              .filter(scheme=scheme, membership_id__in=mem_ids,
                      status__in=[BenevolentCase.Status.APPROVED,
                                  BenevolentCase.Status.PARTLY_PAID,
                                  BenevolentCase.Status.PAID,
                                  BenevolentCase.Status.CLOSED])
              .select_related("policy")):
        own_cases_by_member.setdefault(c.membership_id, []).append(c)

    # (5) Levy paid per (member, case) — one grouped query for the whole scheme.
    levy_by_member = {}
    if scan_case_ids:
        for row in (BenevolentContribution.objects
                    .filter(membership_id__in=mem_ids, case_id__in=scan_case_ids)
                    .filter(_effective_q())
                    .values_list("membership_id", "case_id")
                    .annotate(total=Sum("transaction__amount"))):
            mid, cid, total = row
            levy_by_member.setdefault(mid, {})[cid] = total or _Dec(0)

    # (6) Last contribution date per member — one grouped query.
    last_by_member = dict(
        BenevolentContribution.objects
        .filter(membership_id__in=mem_ids).filter(_effective_q())
        .values_list("membership_id")
        .annotate(last=Max("transaction__date")))

    # Window boundary shared by dues windowing: the scheme's earliest policy date.
    first_policy = min((v.effective_from for v in versions), default=None)

    # Warm each membership's caches, then compute facts via the unchanged path.
    out = []
    for m in memberships:
        m._policy_versions = versions
        m._policy_cache_trusted = True
        m._levy_paid_cache = levy_by_member.get(m.pk, {})
        m._last_contribution_date_cache = last_by_member.get(m.pk)  # None if never
        m._own_settled_cases_cache = own_cases_by_member.get(m.pk, [])
        drows = dues_by_member.get(m.pk, ())
        # One window, both figures — mirrors arrears_for's start/end exactly, and
        # the per-period breakdown is now cut to the same dates so the member's
        # statement and their "owing now" cannot disagree.
        total, by_period = _Dec(0), {}
        if first_policy is not None:
            start = max(m.cover_from, first_policy)
            end = min(as_of, m.left_on or as_of)
            for d, amt, label in drows:
                if start <= d <= end:
                    total += amt
                    by_period[label] = by_period.get(label, _Dec(0)) + amt
        # Set unconditionally. `_batched=True` tells facts_for the caches on this
        # instance were warmed for THIS pass, so it does not clear them first —
        # leaving one unset on a scheme with no published policy would hand the
        # next pass a stale figure from the previous one. (A scheme with no
        # versions has no dues rows at all, so both are read as zero anyway.)
        m._dues_paid_total_cache = total
        m._paid_by_period_cache = by_period
        m._last_dues_date_cache = max((d for d, _, _ in drows), default=None)
        out.append((m, facts_for(m, as_of=as_of, _batched=True)))
    return out


def facts_for(membership, policy=None, as_of=None, _batched=False) -> MembershipFacts:
    """THE facts. Computed once; used by standing and by eligibility alike, so the
    register and the claim decision can never disagree about a plain number.

    For MANY members of one scheme, prefer ``facts_for_scheme`` — it pre-loads the
    same data scheme-wide and calls this per member with its caches warmed, for a
    fraction of the queries and the identical result.

    ``_batched`` is set only by ``facts_for_scheme``: it means the per-instance
    caches on this membership were deliberately warmed for THIS pass and may be
    trusted. When it is False (every ordinary call), any per-instance cache left
    over from an earlier call is cleared first — a membership object reused across
    two assessments (e.g. after publishing a new policy version) must resolve
    against the CURRENT policies, never a stale cached version list. This is the
    invariant a cross-version agreement test caught."""
    if not _batched:
        for attr in ("_policy_versions", "_policy_cache_trusted",
                     "_dues_window_start", "_paid_by_period_cache",
                     "_dues_paid_total_cache", "_levy_paid_cache",
                     "_last_contribution_date_cache", "_last_dues_date_cache",
                     "_own_settled_cases_cache"):
            if hasattr(membership, attr):
                delattr(membership, attr)
    from benevolent.services.contributions import arrears_for, dues_schedule

    as_of = as_of or _dt.date.today()
    policy = policy or membership._policy_on_cached(as_of)
    f = MembershipFacts(as_of=as_of, policy=policy)
    if policy is None:
        return f

    f.grace_days = policy.grace_period_days or 0
    f.exemption = live_exemption(membership, as_of)
    f.age = _age(membership.date_of_birth, as_of)

    # `arrears_for` already knows about exemptions — it is the ONE place that knows
    # what a member owes, and an exempt member's excused periods are simply not
    # charged there (see contributions._waived_periods). Re-applying the exemption
    # here would be a second implementation of the same rule, and second
    # implementations drift.
    f.arrears = arrears_for(membership, policy, as_of=as_of)
    f.days_past_due = _days_past_due(membership, policy, as_of)

    # Tenure and payment-record facts, derived from the SAME dues schedule the
    # arrears figure comes from — one read, so a member's "6 periods paid" and
    # their arrears can never disagree. A period counts as PAID only when fully
    # settled (outstanding == 0); a waived period (a bereaved-member rule)
    # counts as paid, because the scheme chose not to charge it.
    rows = dues_schedule(membership, policy, as_of=as_of)
    f.total_periods = len(rows)
    f.paid_periods = sum(1 for r in rows if r["outstanding"] <= 0)
    f.missed_periods = sum(1 for r in rows if r["outstanding"] > 0)
    f.arrears_periods = f.missed_periods
    # paid_up_since: the day the record last became wholly clear. If anything is
    # outstanding now it is None (they are not currently paid up); otherwise it
    # is the end of the last period that HAD been outstanding, or the cover date
    # if they have never missed. Used by the catch-up re-qualification rule.
    if f.missed_periods == 0:
        f.paid_up_since = _last_cleared_date(membership, rows, policy) \
            or membership.cover_from

    f.months_idle = membership.months_since_contribution(as_of=as_of)
    f.missed_cases = missed_case_levies(membership, policy, as_of)
    f.had_something_to_pay = _had_something_to_pay(membership, policy, as_of)
    f.renewal_due_on = membership.renewal_due_on(policy, as_of=as_of)
    f.renewal_overdue = membership.renewal_overdue(policy, as_of=as_of)
    return f


def _age(dob, on):
    if not dob:
        return None
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def _days_past_due(membership, policy, as_of):
    """How long the member has been behind — measured from the END of the earliest
    period they still owe for, because that is the day the money actually became
    late. Measuring from today (as a naive implementation does) would put everyone
    permanently inside their grace period, which would make the grace period a way
    of never being in arrears at all."""
    from benevolent.services.contributions import dues_schedule
    rows = [r for r in dues_schedule(membership, policy, as_of=as_of)
            if r["outstanding"] > 0]
    if not rows:
        return 0
    first = rows[0]["period"]              # dues_schedule is in period order
    end = _period_end(first, policy.contribution_frequency)
    return max(0, (as_of - end).days) if end else 0


def _last_cleared_date(membership, rows, policy):
    """The date of a currently-paid-up member's most recent dues payment — a
    plain fact exposed on MembershipFacts for the register and reports. (The
    catch-up eligibility rule computes its own 'last caught up a late gap' date
    rather than relying on this, because 'last paid' and 'last caught up late'
    are different questions.) None where no dues payment is on record."""
    cached = getattr(membership, "_last_dues_date_cache", "unset")
    if cached != "unset":
        return cached
    from benevolent.services.contributions import DUES_KINDS, contributions_qs
    return (contributions_qs(membership=membership, kinds=DUES_KINDS)
            .order_by("-transaction__date")
            .values_list("transaction__date", flat=True).first())


def _period_end(label, frequency):
    """The last day of a dues period, from its label. The day after this, the money
    is late."""
    try:
        if frequency == SchemePolicy.Frequency.ANNUAL:
            return _dt.date(int(label), 12, 31)
        if frequency == SchemePolicy.Frequency.QUARTERLY:
            year, q = label.split("-Q")
            month = int(q) * 3
            return _last_day(int(year), month)
        year, month = label.split("-")
        return _last_day(int(year), int(month))
    except (ValueError, AttributeError):
        return None


def _last_day(year, month):
    if month == 12:
        return _dt.date(year, 12, 31)
    return _dt.date(year, month + 1, 1) - _dt.timedelta(days=1)


def live_exemption(membership, on=None):
    """The exemption in force on a date, if any. An unapproved one is not in force:
    proposing that a member be excused does not excuse them."""
    on = on or _dt.date.today()
    for ex in membership.exemptions.all():
        if ex.covers(on):
            return ex
    return None


# ---------------------------------------------------------------------------
# The standing itself
# ---------------------------------------------------------------------------

@dataclass
class StandingResult:
    standing: str
    reason: str
    facts: MembershipFacts
    workings: list = field(default_factory=list)

    @property
    def covered(self):
        """Would this standing, on its own, let the member claim?

        Not a simple membership test of a fixed list, and it took a failing test to
        see why. Under a policy whose arrears treatment is IGNORE or DEDUCT — and
        DEDUCT is the commonest real rule — a member in ARREARS is still perfectly
        well covered: the scheme pays them and nets off what they owe. Reporting
        them as "not covered" because the word ARREARS appears on their row would be
        the register telling a treasurer something the eligibility engine flatly
        denies, which is exactly the disagreement this module is built to prevent.

        So this mirrors the eligibility engine's *blocking* rules, which it can do
        safely because the two share their facts. Standing still reports rather than
        decides — but what it reports is true.
        """
        p = self.facts.policy
        if self.standing in (Standing.GOOD, Standing.EXEMPT, Standing.GRACE):
            return True
        if p is None:
            return False
        if self.standing == Standing.ARREARS:
            treatment = p.arrears_treatment
            if p.arrears_block and treatment == SchemePolicy.ArrearsTreatment.IGNORE:
                treatment = SchemePolicy.ArrearsTreatment.BLOCK
            return treatment != SchemePolicy.ArrearsTreatment.BLOCK
        if self.standing == Standing.INACTIVE:
            return p.inactivity_action not in (
                SchemePolicy.InactivityAction.SUSPEND,
                SchemePolicy.InactivityAction.LAPSE,
                SchemePolicy.InactivityAction.EXPEL)
        return False       # suspended, withdrawn, deceased, closed, pending

    def as_dict(self):
        return {"standing": self.standing, "reason": self.reason,
                "covered": self.covered, "workings": list(self.workings),
                "as_of": self.facts.as_of.isoformat()}


def assess(membership, policy=None, as_of=None) -> StandingResult:
    """Where this member stands. A pure function — it writes nothing.

    The order of the tests is the whole design, and it is not arbitrary:

      1. THE LIFECYCLE DOMINATES. A deceased member is not "in arrears"; a
         withdrawn one is not "inactive". Whatever a human decided about this
         membership outranks anything a calculation has to say about it, and
         saying otherwise to a bereaved family would be indefensible.
      2. EXEMPT beats everything derived. Someone excused from contributing
         cannot be behind on contributions — that is what the word means.
      3. GRACE beats ARREARS. A grace period exists so that being a fortnight
         late is not the same as being in default.
      4. INACTIVE beats ARREARS. Owing three months' dues and having vanished for
         three years are different problems, and the second is the one worth
         saying out loud.
      5. Otherwise: arrears, or good standing.
    """
    as_of = as_of or _dt.date.today()
    policy = policy or membership._policy_on_cached(as_of)
    facts = facts_for(membership, policy, as_of)
    w = []

    # --- 1. the lifecycle dominates ---------------------------------------
    lifecycle = {
        SchemeMembership.Status.PENDING: (
            Standing.PENDING, "Awaiting registration."),
        SchemeMembership.Status.SUSPENDED: (
            Standing.SUSPENDED, "Suspended by the scheme."),
        SchemeMembership.Status.WITHDRAWN: (
            Standing.WITHDRAWN, "The member withdrew from the scheme."),
        SchemeMembership.Status.DECEASED: (
            Standing.DECEASED, "The member has died."),
        SchemeMembership.Status.CLOSED: (
            Standing.CLOSED, "The membership is closed."),
    }
    if membership.status in lifecycle:
        standing, reason = lifecycle[membership.status]
        return StandingResult(standing, reason, facts,
                              ["A lifecycle decision outranks any calculation."])

    if policy is None:
        return StandingResult(
            Standing.GOOD, "No policy is in force, so nothing is owed.", facts,
            ["A member cannot be in arrears under rules that do not exist."])

    # --- 2. exempt --------------------------------------------------------
    if facts.exemption and facts.exemption.exempt_dues:
        ex = facts.exemption
        until = f" until {ex.to_date:%d %b %Y}" if ex.to_date else " (no end date)"
        return StandingResult(
            Standing.EXEMPT,
            f"Exempt — {ex.get_kind_display().lower()}{until}.", facts,
            [f"Granted {ex.from_date:%d %b %Y}: {ex.reason[:100]}"])
    if facts.age_exempt:
        return StandingResult(
            Standing.EXEMPT,
            f"Exempt — aged {facts.age}; the policy exempts members from "
            f"{policy.exemption_age}.", facts,
            ["An automatic age exemption; no paperwork is needed."])

    # --- 3. inactive (before arrears: the bigger fact wins) ---------------
    # "Months since the last contribution" only means anything if there was
    # something to contribute to. In a per-case levy scheme there are no monthly
    # dues; money is asked for when somebody is bereaved. A quiet year with no
    # cases would otherwise turn every faithful member in the scheme inactive
    # on the same day, for the offence of not paying a levy nobody raised.
    if (policy.inactivity_months and facts.months_idle >= policy.inactivity_months
            and facts.had_something_to_pay):
        return StandingResult(
            Standing.INACTIVE,
            f"Inactive — {facts.months_idle} month(s) since the last contribution "
            f"(the policy allows {policy.inactivity_months}).", facts,
            [f"Arrears stand at {facts.arrears}, but the larger fact is that this "
             f"member has stopped contributing altogether."])
    if policy.inactivity_missed_cases and \
            facts.missed_cases >= policy.inactivity_missed_cases:
        return StandingResult(
            Standing.INACTIVE,
            f"Inactive — did not contribute to the last {facts.missed_cases} case "
            f"levies (the policy allows {policy.inactivity_missed_cases}).", facts,
            ["A member who does not stand with a bereaved family, and then expects "
             "the family to stand with them."])

    # --- 4. renewal overdue reads as arrears ------------------------------
    if facts.renewal_overdue and policy.lapse_on_non_renewal:
        return StandingResult(
            Standing.ARREARS,
            f"Renewal fell due on {facts.renewal_due_on:%d %b %Y} and the grace "
            f"period has passed.", facts,
            ["An unrenewed subscription is a debt like any other."])

    # --- 5. grace, then arrears, then good --------------------------------
    if facts.in_grace:
        return StandingResult(
            Standing.GRACE,
            f"In grace — {facts.arrears} outstanding, {facts.days_past_due} day(s) "
            f"late of the {facts.grace_days} allowed.", facts,
            ["Still covered: a grace period that did not cover would not be grace."])

    if facts.arrears > 0:
        allowed = policy.max_arrears_allowed or Decimal(0)
        if facts.arrears <= allowed:
            return StandingResult(
                Standing.GOOD,
                f"{facts.arrears} outstanding, within the {allowed} the policy "
                f"tolerates.", facts, [])
        return StandingResult(
            Standing.ARREARS,
            f"In arrears by {facts.arrears}"
            + (f" ({facts.days_past_due} days late)" if facts.days_past_due else "")
            + ".", facts,
            [f"What arrears actually DO to a claim is the policy's decision "
             f"({policy.get_arrears_treatment_display().lower()}) — this is the "
             f"fact, not the verdict."])

    return StandingResult(Standing.GOOD, "Up to date.", facts, [])


# ---------------------------------------------------------------------------
# Caching the answer
# ---------------------------------------------------------------------------

@db_tx.atomic
def refresh(membership, as_of=None, user=None, log=True):
    """Recompute the cached standing. Writes ONLY to the derived axis.

    This is what a nightly job runs. It cannot touch `status`, so it is
    structurally incapable of overriding a treasurer's decision — not because it
    is told not to, but because there is nowhere for it to write.

    A change of standing is logged, because a member has a right to know when the
    scheme's view of them changed, and why.
    """
    result = assess(membership, as_of=as_of)
    before = membership.standing
    changed = before != result.standing

    membership.standing = result.standing
    membership.standing_reason = result.reason[:200]
    membership.standing_as_of = result.facts.as_of
    membership.save(update_fields=["standing", "standing_reason", "standing_as_of"])

    if changed and log:
        MembershipEvent.objects.create(
            membership=membership, kind=MembershipEvent.Kind.STANDING,
            on=result.facts.as_of,
            summary=f"Standing changed from "
                    f"{Standing(before).label if before else '—'} to "
                    f"{Standing(result.standing).label}.",
            reason=result.reason,
            from_value=before or "", to_value=result.standing,
            automated=(user is None), actor=user)
    return result


def refresh_scheme(scheme, as_of=None, user=None):
    """Recompute standing for every membership on a scheme. Returns the changes."""
    changes = []
    for m in scheme.memberships.select_related("member", "scheme"):
        before = m.standing
        result = refresh(m, as_of=as_of, user=user)
        if before != result.standing:
            changes.append({"membership": m, "from": before,
                            "to": result.standing, "reason": result.reason})
    return changes
