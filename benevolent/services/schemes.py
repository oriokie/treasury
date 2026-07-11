"""Scheme and policy lifecycle.

Publishing a policy is the one operation with real subtlety, so it lives here
rather than on the model: it must supersede the version it replaces, close that
version's effective window, and do both atomically — otherwise `policy_on(date)`
could momentarily resolve two active versions to the same date and decide a case
under the wrong rules.
"""
from __future__ import annotations

import datetime as _dt

from django.core.exceptions import ValidationError
from django.db import transaction as db_tx
from django.utils import timezone

from benevolent.models import (BenevolentEventType, BenevolentScheme,
                               SchemeBenefitRule, SchemeMembership, SchemePolicy)


# ---------------------------------------------------------------------------
# Schemes
# ---------------------------------------------------------------------------

@db_tx.atomic
def activate_scheme(scheme, user=None, on=None):
    if scheme.status == BenevolentScheme.Status.CLOSED:
        raise ValidationError(f"{scheme.name} is closed and cannot be reopened.")
    if scheme.current_policy is None:
        raise ValidationError(
            f"{scheme.name} has no policy in force. Publish a policy before opening the "
            f"scheme — without one, no case could be assessed.")
    scheme.status = BenevolentScheme.Status.ACTIVE
    scheme.opened_on = scheme.opened_on or (on or _dt.date.today())
    scheme.save(update_fields=["status", "opened_on"])
    return scheme


@db_tx.atomic
def suspend_scheme(scheme, user=None):
    """Stop new cases; keep taking contributions and paying what is already
    approved. Cases already in flight are unaffected — a scheme's suspension is
    not a reason to renege on a benefit already granted."""
    scheme.status = BenevolentScheme.Status.SUSPENDED
    scheme.save(update_fields=["status"])
    return scheme


@db_tx.atomic
def close_scheme(scheme, user=None, on=None):
    from benevolent.models import BenevolentCase
    open_cases = scheme.cases.filter(status__in=BenevolentCase.OPEN_STATUSES).count()
    if open_cases:
        raise ValidationError(
            f"{scheme.name} still has {open_cases} open case(s). Settle or reject them "
            f"before closing the scheme.")
    scheme.status = BenevolentScheme.Status.CLOSED
    scheme.closed_on = on or _dt.date.today()
    scheme.save(update_fields=["status", "closed_on"])
    return scheme


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------

@db_tx.atomic
def publish_policy(policy, user=None):
    """Bring a draft policy into force from its effective date.

    Any version currently active from an EARLIER date is superseded and has its
    window closed the day before this one opens, so exactly one version is in
    force on any given date. Decided cases are untouched: they point at the
    version they were decided under, and `policy_on(their event date)` still
    resolves to it.
    """
    if policy.status != SchemePolicy.Status.DRAFT:
        raise ValidationError(f"{policy} is {policy.get_status_display().lower()}, not a draft.")

    scheme = policy.scheme
    clash = (scheme.policies
             .filter(status=SchemePolicy.Status.ACTIVE, effective_from=policy.effective_from)
             .exclude(pk=policy.pk).first())
    if clash:
        raise ValidationError(
            f"Policy v{clash.version} already takes effect on "
            f"{policy.effective_from:%d %b %Y}. Choose a different effective date.")

    later = (scheme.policies
             .filter(status=SchemePolicy.Status.ACTIVE, effective_from__gt=policy.effective_from)
             .exclude(pk=policy.pk).order_by("effective_from").first())
    if later:
        raise ValidationError(
            f"Policy v{later.version} already takes effect on {later.effective_from:%d %b %Y}, "
            f"which is after this one. Publishing this version now would leave a gap or an "
            f"overlap; supersede in date order instead.")

    prior = (scheme.policies
             .filter(status=SchemePolicy.Status.ACTIVE,
                     effective_from__lt=policy.effective_from)
             .exclude(pk=policy.pk).order_by("-effective_from").first())
    if prior:
        prior.effective_to = policy.effective_from - _dt.timedelta(days=1)
        prior.status = SchemePolicy.Status.SUPERSEDED
        # only the window/status change — the rules are untouched, which the
        # model's immutability guard verifies for us
        prior.save(update_fields=["effective_to", "status"])

    policy.status = SchemePolicy.Status.ACTIVE
    policy.published_by = user
    policy.published_at = timezone.now()
    policy.save(update_fields=["status", "published_by", "published_at"])
    return policy


@db_tx.atomic
def new_version_from(policy, *, effective_from, user=None):
    """Start a new DRAFT version pre-filled from an existing one, benefit
    schedule and all. This is the ONLY way to change a scheme's rules: the old
    version stays exactly as it was, and the new one applies from its own
    effective date forward.
    """
    if effective_from <= policy.effective_from:
        raise ValidationError(
            f"A new version must take effect after v{policy.version} "
            f"({policy.effective_from:%d %b %Y}).")
    draft = SchemePolicy(scheme=policy.scheme, effective_from=effective_from,
                         status=SchemePolicy.Status.DRAFT, created_by=user)
    for f in SchemePolicy.RULE_FIELDS:
        if f == "effective_from":
            continue
        setattr(draft, f, getattr(policy, f))
    draft.notes = policy.notes
    draft.save()
    for rule in policy.benefit_rules.all():
        SchemeBenefitRule.objects.create(
            policy=draft, event_type=rule.event_type, amount=rule.amount,
            percent=rule.percent, cap=rule.cap,
            waiting_period_days=rule.waiting_period_days,
            max_per_year=rule.max_per_year, active=rule.active)
    return draft


@db_tx.atomic
def withdraw_policy(policy, user=None):
    """Retire a draft that was never used. A version that has decided a case can
    never be withdrawn — it is part of the audit record."""
    if policy.is_locked:
        raise ValidationError(
            f"{policy} has decided cases and is part of the permanent record. Publish a "
            f"superseding version instead of withdrawing this one.")
    if policy.status == SchemePolicy.Status.ACTIVE and policy.scheme.is_open:
        raise ValidationError(
            "This is the policy currently in force for an open scheme. Publish a "
            "replacement first, or suspend the scheme.")
    policy.status = SchemePolicy.Status.WITHDRAWN
    policy.save(update_fields=["status"])
    return policy


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------

@db_tx.atomic
def enrol(scheme, member, *, joined_on=None, user=None, notes="", date_of_birth=None,
          household_name=""):
    """Enrol a member.

    Where the policy requires formal registration, the enrolment starts PENDING
    and cover does not begin until someone admits them (`admit`). Where it does
    not, they are active immediately. Which of those happens is the policy's
    decision, not this function's.
    """
    if not scheme.accepts_contributions:
        raise ValidationError(
            f"{scheme.name} is {scheme.get_status_display().lower()} and is not enrolling.")
    joined_on = joined_on or _dt.date.today()
    policy = scheme.policy_on(joined_on)

    needs_admission = bool(
        policy and policy.registration_required
        and policy.registration_approval != SchemePolicy.RegistrationApproval.AUTO)
    start_status = (SchemeMembership.Status.PENDING if needs_admission
                    else SchemeMembership.Status.ACTIVE)

    existing = SchemeMembership.objects.filter(scheme=scheme, member=member).first()
    if existing:
        if existing.is_live:
            raise ValidationError(
                f"{member.name} is already enrolled in {scheme.name} "
                f"({existing.number}, {existing.get_status_display().lower()}).")
        # a returning member: REINSTATE the same membership so their history —
        # contributions, past cases, their number — is never orphaned. Their
        # waiting period restarts from today (see SchemeMembership.cover_from):
        # without that, a member could lapse for years, rejoin the week a relative
        # fell ill, and claim on the strength of a 2019 joining date.
        return reinstate(existing, on=joined_on, user=user)

    m = SchemeMembership.objects.create(
        scheme=scheme, member=member, joined_on=joined_on,
        status=start_status, enrolled_by=user, notes=notes,
        date_of_birth=date_of_birth, household_name=household_name,
        registered_on=(None if needs_admission
                       else (joined_on if (policy and policy.registration_required)
                             else None)))
    if policy and policy.renewal_required:
        m.renewed_until = m.renewal_due_on(policy, as_of=joined_on)
        m.save(update_fields=["renewed_until"])
    return m


@db_tx.atomic
def admit(membership, *, on=None, user=None):
    """Formally admit a member whose registration needed approval. Cover — and so
    the waiting period — runs from THIS date, not from the day their name was
    first typed into a list."""
    if membership.status != SchemeMembership.Status.PENDING:
        raise ValidationError(
            f"{membership.member.name} is {membership.get_status_display().lower()}, "
            f"not awaiting admission.")
    on = on or _dt.date.today()
    membership.registered_on = on
    membership.status = SchemeMembership.Status.ACTIVE
    membership.save(update_fields=["registered_on", "status"])
    return membership


@db_tx.atomic
def reinstate(membership, *, on=None, user=None):
    """Bring a lapsed / inactive / expired member back.

    `reinstated_on` is what makes any reinstatement waiting period run from the
    day they returned rather than the day they originally joined — see
    SchemeMembership.cover_from.
    """
    on = on or _dt.date.today()
    if membership.status in (SchemeMembership.Status.EXPELLED,):
        raise ValidationError(
            f"{membership.member.name} was removed from the scheme and must be enrolled "
            f"afresh by a treasurer, not simply reinstated.")
    membership.status = SchemeMembership.Status.ACTIVE
    membership.left_on = None
    membership.inactive_since = None
    membership.reinstated_on = on
    membership.save(update_fields=["status", "left_on", "inactive_since", "reinstated_on"])
    return membership


@db_tx.atomic
def withdraw_membership(membership, *, on=None, user=None):
    from benevolent.models import BenevolentCase
    open_cases = membership.cases.filter(status__in=BenevolentCase.OPEN_STATUSES).count()
    if open_cases:
        raise ValidationError(
            f"{membership.member.name} has {open_cases} open case(s). Settle them before "
            f"withdrawing the membership.")
    membership.status = SchemeMembership.Status.WITHDRAWN
    membership.left_on = on or _dt.date.today()
    membership.save(update_fields=["status", "left_on"])
    return membership


def refresh_arrears_status(scheme, as_of=None):
    """Backwards-compatible entry point (Phase 1). Delegates to the automation
    engine, running only the arrears rule."""
    result = run_automation(scheme, as_of=as_of, only={"arrears"}, force=True)
    return result["changed"]


# ---------------------------------------------------------------------------
# Automation
# ---------------------------------------------------------------------------

def run_automation(scheme=None, as_of=None, only=None, force=False, user=None):
    """Apply the standing membership rules: arrears, inactivity, renewals.

    Two principles govern everything here, and they are what make an automated
    job safe to point at a church's welfare register:

    1. **It never overrides a human.** Only memberships in
       `AUTOMATABLE_STATUSES` are touched. A membership someone deliberately
       SUSPENDED, WITHDREW or EXPELLED is left exactly alone. An automated job
       quietly reversing a decision a treasurer made on purpose is the fastest
       way to make people stop trusting automation.

    2. **It is reversible and it reports.** Every change is returned, and each
       rule reinstates as readily as it demotes: a member who catches up on their
       arrears goes back to ACTIVE on the next run without anyone intervening.

    Which rules run at all is a SETTING (BenevolentSettings), because none of them
    can change the outcome of a decision already made — they change the state a
    FUTURE claim will be assessed against, which the policy then rules on.
    """
    from benevolent.models import BenevolentScheme, BenevolentSettings
    from benevolent.services.contributions import arrears_for

    as_of = as_of or _dt.date.today()
    cfg = BenevolentSettings.get()
    if not force and not cfg.automation_enabled:
        return {"ran": False, "changed": 0, "changes": [],
                "reason": "Automation is switched off in the benevolent settings."}

    only = only or {"arrears", "inactivity", "renewal"}
    schemes = ([scheme] if scheme is not None
               else list(BenevolentScheme.objects.filter(
                   status=BenevolentScheme.Status.ACTIVE)))

    changes = []
    for sch in schemes:
        policy = sch.policy_on(as_of)
        if policy is None:
            continue
        members = sch.memberships.filter(
            status__in=SchemeMembership.AUTOMATABLE_STATUSES).select_related("member")

        for m in members:
            before = m.status
            want = before
            reason = ""

            # --- renewal: strongest signal, so it is decided first ----------
            if "renewal" in only and (force or cfg.auto_lapse_unrenewed) \
                    and policy.renewal_required and policy.lapse_on_non_renewal:
                if m.renewal_overdue(policy, as_of=as_of):
                    want = SchemeMembership.Status.EXPIRED
                    due = m.renewal_due_on(policy, as_of=as_of)
                    reason = f"renewal was due {due:%d %b %Y} and the grace period has passed"
                elif before == SchemeMembership.Status.EXPIRED:
                    want = SchemeMembership.Status.ACTIVE
                    reason = "renewed"

            # --- inactivity -----------------------------------------------
            if want == before and "inactivity" in only \
                    and (force or cfg.auto_flag_inactive) \
                    and policy.inactivity_months \
                    and policy.inactivity_action != SchemePolicy.InactivityAction.NONE:
                months = m.months_since_contribution(as_of=as_of)
                if months >= policy.inactivity_months:
                    action = policy.inactivity_action
                    mapping = {
                        SchemePolicy.InactivityAction.FLAG: SchemeMembership.Status.INACTIVE,
                        SchemePolicy.InactivityAction.LAPSE: SchemeMembership.Status.LAPSED,
                        # SUSPEND and EXPEL are deliberately NOT automated: they are
                        # punitive, and removing someone from a welfare scheme is a
                        # decision a person should make and be answerable for. The
                        # policy still BLOCKS their claims via the eligibility engine;
                        # automation just declines to be the one who throws them out.
                    }
                    target = mapping.get(action)
                    if target and before != target:
                        want = target
                        reason = (f"{months} month(s) without a contribution "
                                  f"(the policy allows {policy.inactivity_months})")
                elif before == SchemeMembership.Status.INACTIVE:
                    want = SchemeMembership.Status.ACTIVE
                    reason = "contributing again"

            # --- arrears ---------------------------------------------------
            if want == before and "arrears" in only and (force or cfg.auto_refresh_arrears):
                treatment = policy.arrears_treatment
                if policy.arrears_block and treatment == SchemePolicy.ArrearsTreatment.IGNORE:
                    treatment = SchemePolicy.ArrearsTreatment.BLOCK
                if treatment == SchemePolicy.ArrearsTreatment.BLOCK:
                    owed = arrears_for(m, policy, as_of=as_of)
                    allowed = policy.max_arrears_allowed or 0
                    if owed > allowed and before == SchemeMembership.Status.ACTIVE:
                        want = SchemeMembership.Status.LAPSED
                        reason = f"in arrears by {owed}"
                    elif owed <= allowed and before == SchemeMembership.Status.LAPSED:
                        want = SchemeMembership.Status.ACTIVE
                        reason = "arrears cleared"

            if want != before:
                m.status = want
                m.inactive_since = (as_of if want == SchemeMembership.Status.INACTIVE
                                    else None)
                m.save(update_fields=["status", "inactive_since"])
                changes.append({
                    "membership": m, "scheme": sch,
                    "from": before, "to": want, "reason": reason,
                })

    summary = f"{len(changes)} membership(s) updated across {len(schemes)} scheme(s)."
    if not force:
        cfg.automation_last_run = timezone.now()
        cfg.automation_last_summary = summary[:255]
        cfg.save(update_fields=["automation_last_run", "automation_last_summary"])
    return {"ran": True, "changed": len(changes), "changes": changes,
            "summary": summary, "as_of": as_of}
