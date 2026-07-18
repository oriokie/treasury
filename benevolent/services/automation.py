"""Automation jobs.

The nightly runner (`schemes.run_automation`) already recomputes standing and
sends due reminders. This module adds the rest of the scheduled work a welfare
scheme needs, split along one line the module holds everywhere:

    * DERIVED / HOUSEKEEPING work — safe to do automatically, because it changes
      nothing a person is answerable for. Detecting duplicates, noticing a member
      has become eligible, archiving a case that has been closed for months.

    * STATUS DECISIONS — suspending a member, closing a membership, dropping a
      dependant's cover. Automation must NOT do these (see registry.suspend's own
      note: a punishment is a person's decision). So instead of acting, the job
      raises a `BenevolentTask` that states what it found and what the policy
      would do, and leaves a human to confirm. The member's status is untouched
      until somebody clicks.

Every job here is idempotent: safe to run nightly, safe to re-run after a
failure. Task-raising is deduplicated on a stable `dedup_key` among OPEN tasks,
so a job that runs every night does not raise the same "suspend Jane" task thirty
times — it finds the open one already there and leaves it.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from django.db import transaction as db_tx
from django.utils import timezone

from benevolent.models import (BenevolentCase, BenevolentScheme, BenevolentTask,
                               SchemeDependant, SchemeMembership, SchemePolicy)


# ---------------------------------------------------------------------------
# The task mechanism (item: create review tasks)
# ---------------------------------------------------------------------------

def raise_task(scheme, *, kind, title, dedup_key, detail="", severity=None,
               membership=None, dependant=None, case=None, recommended_action="",
               by_automation=True):
    """Raise a review task, ONCE. If an open task with the same dedup_key already
    exists, this is a no-op returning it — so a nightly job can call this every
    night without piling up duplicates. Returns (task, created)."""
    severity = severity or BenevolentTask.Severity.MEDIUM
    existing = BenevolentTask.objects.filter(
        dedup_key=dedup_key, status=BenevolentTask.Status.OPEN).first()
    if existing is not None:
        return existing, False
    task = BenevolentTask.objects.create(
        scheme=scheme, kind=kind, title=title[:160], detail=detail,
        severity=severity, membership=membership, dependant=dependant, case=case,
        recommended_action=recommended_action[:40], dedup_key=dedup_key[:120],
        created_by_automation=by_automation)
    return task, True


def resolve_task(task, *, user, action, note=""):
    """Mark a task actioned or dismissed. Does NOT itself change any status — the
    human takes the actual action (following the link), this just records that
    the task has been dealt with."""
    task.status = (BenevolentTask.Status.DONE if action == "done"
                   else BenevolentTask.Status.DISMISSED)
    task.resolved_by = user
    task.resolved_at = timezone.now()
    task.resolution_note = (note or "")[:200]
    task.save(update_fields=["status", "resolved_by", "resolved_at",
                             "resolution_note"])
    return task


# ---------------------------------------------------------------------------
# Job: suspend overdue members (PROPOSE — never acts)
# ---------------------------------------------------------------------------

def _batch_facts(scheme, as_of):
    """Facts for every active member of the scheme in a bounded number of
    queries — the batch loader, so the propose/flag jobs don't fan out to a
    per-member facts_for. Returns [(membership, MembershipFacts), …]."""
    from benevolent.models import SchemeMembership
    from benevolent.services.standing import facts_for_scheme
    actives = list(scheme.memberships
                   .filter(status=SchemeMembership.Status.ACTIVE)
                   .select_related("member")
                   .prefetch_related("exemptions", "adjustments"))
    return facts_for_scheme(scheme, as_of=as_of, memberships=actives)


def propose_suspensions(scheme, *, as_of=None):
    """Where a member is overdue enough that the policy's inactivity rule says
    SUSPEND or LAPSE, raise a task recommending it — but do not suspend. A member
    who has stopped paying is INACTIVE (a fact automation may compute); removing
    their cover is a punishment (a decision a person makes)."""
    from benevolent.services.standing import facts_for
    as_of = as_of or _dt.date.today()
    policy = scheme.policy_on(as_of)
    raised = 0
    if policy is None or policy.inactivity_action not in (
            SchemePolicy.InactivityAction.SUSPEND,
            SchemePolicy.InactivityAction.LAPSE):
        return 0
    for mem, facts in _batch_facts(scheme, as_of):
        if mem.status != SchemeMembership.Status.ACTIVE:
            continue
        over = False
        why = []
        if policy.inactivity_months and facts.months_idle >= policy.inactivity_months:
            over = True
            why.append(f"{facts.months_idle} months since a contribution "
                       f"(limit {policy.inactivity_months})")
        if policy.inactivity_missed_cases and \
                facts.missed_cases >= policy.inactivity_missed_cases:
            over = True
            why.append(f"{facts.missed_cases} consecutive case levies unpaid "
                       f"(limit {policy.inactivity_missed_cases})")
        if not over:
            continue
        action = policy.get_inactivity_action_display()
        _, created = raise_task(
            scheme, kind=BenevolentTask.Kind.SUSPEND_OVERDUE,
            title=f"{mem.member.name} is overdue — policy says {action.lower()}",
            detail=(f"{mem.member.name} ({mem.number}) is inactive: "
                    + "; ".join(why) + f". Policy v{policy.version} would "
                    f"{action.lower()} them. Confirm to apply, or dismiss."),
            dedup_key=f"suspend:{mem.pk}",
            severity=BenevolentTask.Severity.MEDIUM,
            membership=mem, recommended_action=policy.inactivity_action)
        raised += 1 if created else 0
    return raised


# ---------------------------------------------------------------------------
# Job: close inactive memberships (PROPOSE — never acts)
# ---------------------------------------------------------------------------

def propose_closures(scheme, *, as_of=None, idle_months=24):
    """A membership that has been SUSPENDED and idle a long time is a candidate
    for closing off the register — but, again, closing is a person's decision, so
    this proposes it rather than doing it."""
    from benevolent.services.standing import facts_for
    as_of = as_of or _dt.date.today()
    policy = scheme.policy_on(as_of)
    raised = 0
    for mem in scheme.memberships.filter(
            status=SchemeMembership.Status.SUSPENDED).select_related("member"):
        idle = mem.months_since_contribution(as_of=as_of)
        if idle < idle_months:
            continue
        _, created = raise_task(
            scheme, kind=BenevolentTask.Kind.CLOSE_INACTIVE,
            title=f"{mem.member.name} has been suspended and idle {idle} months",
            detail=(f"{mem.member.name} ({mem.number}) has been suspended with no "
                    f"contribution for {idle} months. Consider closing the membership "
                    f"to tidy the register. Confirm to close, or dismiss to keep it."),
            dedup_key=f"close:{mem.pk}",
            severity=BenevolentTask.Severity.LOW,
            membership=mem, recommended_action="CLOSE")
        raised += 1 if created else 0
    return raised


# ---------------------------------------------------------------------------
# Job: expire waiting periods (NOTIFY — a member has become eligible)
# ---------------------------------------------------------------------------

def flag_waiting_period_served(scheme, *, as_of=None, window_days=3):
    """When a member crosses the end of their waiting period, they become
    claim-eligible. This raises a low-severity task so the register reflects it —
    a positive event, no status change, purely a heads-up."""
    as_of = as_of or _dt.date.today()
    policy = scheme.policy_on(as_of)
    raised = 0
    if policy is None or not policy.waiting_period_days:
        return 0
    for mem in scheme.memberships.filter(status=SchemeMembership.Status.ACTIVE
                                         ).select_related("member"):
        served = (as_of - mem.cover_from).days
        # only the ones who crossed the line within the recent window, so this is
        # a one-off notice and not a permanent flag on everyone long past it
        if policy.waiting_period_days <= served < policy.waiting_period_days + window_days:
            _, created = raise_task(
                scheme, kind=BenevolentTask.Kind.WAITING_PERIOD_SERVED,
                title=f"{mem.member.name} has served the waiting period",
                detail=(f"{mem.member.name} ({mem.number}) completed the "
                        f"{policy.waiting_period_days}-day waiting period and is now "
                        f"eligible to claim."),
                dedup_key=f"waiting:{mem.pk}:{mem.cover_from.isoformat()}",
                severity=BenevolentTask.Severity.LOW,
                membership=mem)
            raised += 1 if created else 0
    return raised


# ---------------------------------------------------------------------------
# Job: age dependants (PROPOSE — a child has passed the age limit)
# ---------------------------------------------------------------------------

def flag_aged_out_dependants(scheme, *, as_of=None):
    """A child dependant who passes the policy's age limit loses cover — but
    dropping their cover is a decision (there may be a disability exception, a
    still-in-education rule the church applies by hand). So this raises a task
    rather than silently removing them."""
    as_of = as_of or _dt.date.today()
    policy = scheme.policy_on(as_of)
    raised = 0
    if policy is None or not policy.dependant_age_limit:
        return 0
    deps = SchemeDependant.objects.filter(
        membership__scheme=scheme, active=True,
        relationship=SchemeDependant.Relationship.CHILD,
        date_of_birth__isnull=False).select_related("membership__member")
    for dep in deps:
        b = dep.date_of_birth
        age = as_of.year - b.year - ((as_of.month, as_of.day) < (b.month, b.day))
        if age <= policy.dependant_age_limit:
            continue
        _, created = raise_task(
            scheme, kind=BenevolentTask.Kind.DEPENDANT_AGED_OUT,
            title=f"{dep.display_name} has passed the dependant age limit",
            detail=(f"{dep.display_name}, a child dependant of "
                    f"{dep.membership.member.name}, is {age} — over the policy limit of "
                    f"{policy.dependant_age_limit}. Review whether cover should end "
                    f"(some churches keep a dependant in full-time education, or with a "
                    f"disability). Confirm to remove cover, or dismiss to keep it."),
            dedup_key=f"agedout:{dep.pk}",
            severity=BenevolentTask.Severity.MEDIUM,
            membership=dep.membership, dependant=dep, recommended_action="REMOVE_COVER")
        raised += 1 if created else 0
    return raised


# ---------------------------------------------------------------------------
# Job: detect duplicate memberships (PROPOSE)
# ---------------------------------------------------------------------------

def flag_duplicate_memberships(scheme, *, as_of=None):
    """Two live memberships that look like the same person (same phone, or the
    same name) — a member enrolled twice, which would let them claim twice or
    muddle their contributions. Raised for a human to merge or explain."""
    from django.db.models import Count
    raised = 0
    live = SchemeMembership.objects.filter(
        scheme=scheme, status__in=SchemeMembership.LIVE_STATUSES)

    # by phone
    phone_dupes = (live.exclude(member__phone="")
                   .exclude(member__phone__isnull=True)
                   .values("member__phone")
                   .annotate(n=Count("id")).filter(n__gte=2))
    for row in phone_dupes:
        members = list(live.filter(member__phone=row["member__phone"])
                       .select_related("member"))
        # a shared FAMILY phone is fine; only flag when the NAMES also match
        names = {" ".join(m.member.name.lower().split()) for m in members}
        if len(names) < len(members):
            first = members[0]
            _, created = raise_task(
                scheme, kind=BenevolentTask.Kind.POSSIBLE_DUPLICATE,
                title=f"Possible duplicate membership: {first.member.name}",
                detail=(f"{len(members)} live memberships share phone "
                        f"{row['member__phone']} with matching names. This may be one "
                        f"person enrolled twice — review and merge if so."),
                dedup_key=f"dupe:phone:{row['member__phone']}",
                severity=BenevolentTask.Severity.MEDIUM,
                membership=first)
            raised += 1 if created else 0
    return raised


# ---------------------------------------------------------------------------
# Job: archive completed cases (HOUSEKEEPING — safe)
# ---------------------------------------------------------------------------

def archive_completed_cases(scheme, *, as_of=None, closed_months=6):
    """Mark long-settled cases archived, so the working case list shows what is
    live rather than years of history. Safe to do automatically: archiving is a
    display/housekeeping flag, not a money or status decision, and a CLOSED/PAID
    case is genuinely finished. Reversible by a human at any time.

    Uses the CaseEvent log to record the archival, rather than a new column, so
    it fits the existing audit trail. A case is 'archived' when it has an ARCHIVED
    event and no later re-open; we simply avoid re-logging one that already has."""
    from benevolent.models import CaseEvent
    from benevolent.services.cases import log as case_log
    as_of = as_of or _dt.date.today()
    cutoff = as_of - _dt.timedelta(days=closed_months * 30)
    archived = 0
    finished = BenevolentCase.objects.filter(
        scheme=scheme,
        status__in=[BenevolentCase.Status.PAID, BenevolentCase.Status.CLOSED,
                    BenevolentCase.Status.REJECTED, BenevolentCase.Status.CANCELLED])
    for case in finished.prefetch_related("events"):
        # the date it was finished: latest of closed_at / a decision date
        finished_on = case.closed_at.date() if case.closed_at else None
        if finished_on is None or finished_on > cutoff:
            continue
        if case.events.filter(kind=CaseEvent.Kind.ARCHIVED).exists():
            continue
        case_log(case, CaseEvent.Kind.ARCHIVED,
                 f"Auto-archived: finished on {finished_on:%d %b %Y}, older than "
                 f"{closed_months} months.", automated=True)
        archived += 1
    return archived


# ---------------------------------------------------------------------------
# The aggregate the runner calls
# ---------------------------------------------------------------------------

@db_tx.atomic
def run_jobs(scheme=None, *, as_of=None, cfg=None):
    """Run every automation job for the active schemes, returning a per-job tally.
    Called from schemes.run_automation after standing/reminders. Honours the
    settings toggles where they exist; the propose-only and housekeeping jobs run
    whenever automation is on, because none of them changes a status."""
    from benevolent.models import BenevolentSettings
    as_of = as_of or _dt.date.today()
    cfg = cfg or BenevolentSettings.get()
    schemes = ([scheme] if scheme is not None
               else list(BenevolentScheme.objects.filter(
                   status=BenevolentScheme.Status.ACTIVE)))

    tally = {"suspensions_proposed": 0, "closures_proposed": 0,
             "eligible_flagged": 0, "aged_out_flagged": 0,
             "duplicates_flagged": 0, "cases_archived": 0}
    for sch in schemes:
        if cfg.auto_flag_inactive:
            tally["suspensions_proposed"] += propose_suspensions(sch, as_of=as_of)
            tally["closures_proposed"] += propose_closures(sch, as_of=as_of)
        tally["eligible_flagged"] += flag_waiting_period_served(sch, as_of=as_of)
        tally["aged_out_flagged"] += flag_aged_out_dependants(sch, as_of=as_of)
        tally["duplicates_flagged"] += flag_duplicate_memberships(sch, as_of=as_of)
        if getattr(cfg, "auto_archive_cases", True):
            tally["cases_archived"] += archive_completed_cases(sch, as_of=as_of)
    return tally
