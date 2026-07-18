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

# ---------------------------------------------------------------------------
# Membership — Phase 3 moved the substance to services/registry.py
# ---------------------------------------------------------------------------
#
# These remain as the names the rest of the app already calls, and delegate. The
# registry is now the one place that writes the membership lifecycle, so that
# every such write is logged as a MembershipEvent without any caller having to
# remember to do it.

def enrol(scheme, member, *, joined_on=None, user=None, notes="", date_of_birth=None,
          household_name="", **kw):
    from benevolent.services import registry
    return registry.register(
        scheme, member, joined_on=joined_on, user=user, notes=notes,
        date_of_birth=date_of_birth, household_name=household_name, **kw)


def admit(membership, *, on=None, user=None, reason="", notify=True):
    from benevolent.services import registry
    return registry.admit(membership, on=on, user=user, reason=reason, notify=notify)


def reinstate(membership, *, on=None, user=None, reason=""):
    from benevolent.services import registry
    return registry.reinstate(membership, on=on, user=user, reason=reason)


def withdraw_membership(membership, *, on=None, user=None, reason=""):
    from benevolent.services import registry
    return registry.withdraw(membership, on=on, user=user, reason=reason)


# ---------------------------------------------------------------------------
# Automation
# ---------------------------------------------------------------------------

# NOTE: `refresh_arrears_status(scheme, as_of)` was removed here. It was a
# backwards-compatibility shim around `standing.refresh_scheme()` from the
# Phase 3 rewrite — but nothing has ever called it, in this module or outside
# it. A compatibility shim with no callers is not compatibility; it is just an
# extra name for the same thing, and a second place a future reader might
# reasonably (and wrongly) go looking for the arrears logic.


def run_automation(scheme=None, as_of=None, only=None, force=False, user=None):
    """Recompute where every member stands.

    Phase 3 rewrote this, and the rewrite is the point of the phase.

    Phase 2's version mutated `SchemeMembership.status` — the same column a
    treasurer writes to. It was kept safe by an allowlist of statuses it was
    permitted to touch, which worked, but was a rule someone had to remember and
    could one day forget.

    It now writes ONLY to `standing`, which is a cache of a pure function of the
    policy and the facts. It is therefore *structurally* incapable of overriding a
    human's decision — not because it is told not to, but because `status` is a
    different column and this code does not write to it. Suspension, withdrawal
    and closure remain what they always should have been: decisions a person makes
    and answers for.

    Recomputing a cache is also idempotent and free of consequence, which means
    this job can be run as often as you like, in any order, and re-run after a
    failure, with no thought at all.
    """
    from benevolent.models import BenevolentScheme, BenevolentSettings
    from benevolent.services import standing as standing_svc

    as_of = as_of or _dt.date.today()
    cfg = BenevolentSettings.get()
    if not force and not cfg.automation_enabled:
        return {"ran": False, "changed": 0, "changes": [],
                "reason": "Automation is switched off in the benevolent settings."}

    schemes = ([scheme] if scheme is not None
               else list(BenevolentScheme.objects.filter(
                   status=BenevolentScheme.Status.ACTIVE)))

    changes = []
    for sch in schemes:
        for c in standing_svc.refresh_scheme(sch, as_of=as_of, user=user):
            c["scheme"] = sch
            changes.append(c)

    # Phase 7: the same run that recomputes standing also sends any reminders
    # that are due, and retries anything that failed to send last time — the
    # existing nightly cadence does both jobs; no second schedule was needed.
    from benevolent.services import notify as notify_svc
    reminders = notify_svc.send_due_reminders(scheme=scheme, as_of=as_of)
    retried = notify_svc.retry_failed()

    # item 7: the scheduled jobs — proposing status changes as review tasks
    # (never acting on them), flagging eligibility/aged-out/duplicates, and
    # archiving long-settled cases. All idempotent; safe to run nightly.
    from benevolent.services import automation as automation_svc
    jobs = automation_svc.run_jobs(scheme=scheme, as_of=as_of, cfg=cfg)

    tasks_raised = (jobs["suspensions_proposed"] + jobs["closures_proposed"]
                    + jobs["eligible_flagged"] + jobs["aged_out_flagged"]
                    + jobs["duplicates_flagged"])
    summary = (f"{len(changes)} membership standing(s) recomputed across "
               f"{len(schemes)} scheme(s); {reminders['arrears']} arrears and "
               f"{reminders['renewal']} renewal reminder(s) sent; "
               f"{retried} failed notification(s) retried; "
               f"{tasks_raised} review task(s) raised; "
               f"{jobs['cases_archived']} case(s) archived.")
    if not force:
        cfg.automation_last_run = timezone.now()
        cfg.automation_last_summary = summary[:255]
        cfg.save(update_fields=["automation_last_run", "automation_last_summary"])
    return {"ran": True, "changed": len(changes), "changes": changes,
            "summary": summary, "as_of": as_of, "reminders": reminders,
            "retried": retried, "jobs": jobs}
