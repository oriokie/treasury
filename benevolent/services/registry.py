"""The Benevolent Member Registry.

Registration, households, the membership lifecycle, transfers and exemptions.

Three commitments run through the whole module.

**The member registry is `members.Member`, and there is no second one.**
A `SchemeMembership` is an *enrolment*, not a person. A dependant who is on the
church roll is LINKED to their member record, not typed in again. A household is a
registration TYPE, not a parallel person-database with its own names and phone
numbers to drift out of step with the roll. Every scheme in the church draws on the
one register the church already keeps.

**A human owns the lifecycle; a function owns the standing.**
Everything in this module writes to `status` — the administrative axis — and every
one of those writes is a decision somebody made and is answerable for. Standing is
never set here; it is recomputed from `services/standing.py` afterwards, and the
recomputation is free to disagree.

**Nothing happens without a record of it happening.**
Every function here writes a `MembershipEvent`. `django-simple-history` will tell an
auditor what a field was on 3 March; the event log tells a treasurer, a board and a
bereaved family *what happened to this member, and why*. They are different
questions and both deserve an answer.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction as db_tx
from django.utils import timezone

from benevolent.models import (MembershipEvent, MembershipExemption, RegistrationType,
                               SchemeDependant, SchemeMembership, SchemePolicy, Standing)
from benevolent.services import standing as standing_svc


def log(membership, kind, summary, *, user=None, on=None, reason="",
        from_value="", to_value="", automated=False):
    """One line in the membership's narrative. Never optional."""
    return MembershipEvent.objects.create(
        membership=membership, kind=kind, on=on or _dt.date.today(),
        summary=summary[:255], reason=reason or "",
        from_value=from_value or "", to_value=to_value or "",
        automated=automated, actor=user)


def _maybe_open_death_case(*, scheme, event_date, membership=None,
                           dependant=None, user=None):
    """Open a draft case for a recorded death, if the module settings say to.

    Both this module's death-recording functions (record_death,
    record_dependant_death) go through here. The setting's OFF value skips it
    entirely; ON_RECORD and ALWAYS both open one from this path (the register IS
    the record). ALWAYS additionally has a post_save signal for deaths recorded
    OUTSIDE this service — that signal calls the same case service, and the case
    service is idempotent, so a death recorded through the register never opens
    two cases.
    """
    from benevolent.models import BenevolentSettings
    from benevolent.services import cases as cases_svc

    mode = BenevolentSettings.get().auto_open_case_on_death
    if mode == BenevolentSettings.DeathCaseMode.OFF:
        return None
    return cases_svc.open_case_for_death(
        scheme=scheme, membership=membership, dependant=dependant,
        event_date=event_date, user=user)


# NOTE: the notification this module sends to a MEMBER is
# benevolent.services.notify.send() (Phase 7) — a templated message actually
# delivered to the member's phone/email, not the staff in-app alert
# core.services.notifications.notify() produces. An earlier version of this
# function claimed ("Tell the member...") to do the former while actually
# doing the latter, gated by a field that was never wired to anything either
# — a confirmed bug, fixed by removing it rather than leaving two ways to
# notify a member, one of which lies about what it does.


def _notify_status_change(membership, *, status_note=""):
    """The one place every lifecycle transition below calls to tell a member
    their status changed — so the wording, the settings check, and the
    failure-never-breaks-the-transition guarantee live in one place, not
    once per function."""
    from benevolent.services import notify as notify_svc
    from benevolent.models import NotificationEvent
    notify_svc.send(NotificationEvent.MEMBERSHIP_STATUS_CHANGED, membership=membership,
                    extra={"status_note": status_note})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

@db_tx.atomic
def register(scheme, member, *, joined_on=None, user=None,
             registration_type=RegistrationType.INDIVIDUAL, household_name="",
             date_of_birth=None, notes="", spouse=None, dependants=None, notify=True):
    """Register a member — individually, or as a household.

    A HOUSEHOLD registration is one subscription covering a principal member, a
    spouse and their dependants. It is not a different KIND of membership with its
    own code path: it is the same `SchemeMembership` with `registration_type` set,
    a spouse and dependants attached. Everything downstream — dues, standing,
    claims, benefits — treats it identically, which is exactly the property that
    stops household schemes becoming a second, subtly-different system.

    Where the policy requires formal registration, the enrolment starts PENDING and
    cover does not begin until someone admits them. Which of those happens is the
    policy's decision, not this function's.
    """
    if not scheme.accepts_contributions:
        raise ValidationError(
            f"{scheme.name} is {scheme.get_status_display().lower()} and is not enrolling.")

    joined_on = joined_on or _dt.date.today()
    policy = scheme.policy_on(joined_on)

    if registration_type == RegistrationType.HOUSEHOLD and not (household_name or "").strip():
        household_name = f"The {member.name.split()[-1]} household"

    existing = SchemeMembership.objects.filter(scheme=scheme, member=member).first()
    if existing:
        if existing.status in SchemeMembership.LIVE_STATUSES:
            raise ValidationError(
                f"{member.name} is already enrolled in {scheme.name} "
                f"({existing.number}, {existing.get_status_display().lower()}).")
        return reinstate(existing, on=joined_on, user=user)

    needs_admission = bool(
        policy and policy.registration_required
        and policy.registration_approval != SchemePolicy.RegistrationApproval.AUTO)

    m = SchemeMembership.objects.create(
        scheme=scheme, member=member, joined_on=joined_on,
        status=(SchemeMembership.Status.PENDING if needs_admission
                else SchemeMembership.Status.ACTIVE),
        registration_type=registration_type, household_name=household_name,
        date_of_birth=date_of_birth, notes=notes, enrolled_by=user,
        registered_on=(None if needs_admission
                       else (joined_on if (policy and policy.registration_required)
                             else None)))
    if policy and policy.renewal_required:
        m.renewed_until = m.renewal_due_on(policy, as_of=joined_on)
        m.save(update_fields=["renewed_until"])

    log(m, MembershipEvent.Kind.ENROLLED,
        f"{member.name} registered"
        + (f" as {household_name}" if registration_type == RegistrationType.HOUSEHOLD
           else "")
        + (" — awaiting admission." if needs_admission else "."),
        user=user, on=joined_on, to_value=m.status)

    if spouse is not None:
        add_dependant(m, member=spouse if hasattr(spouse, "pk") else None,
                      name=(spouse if isinstance(spouse, str) else ""),
                      relationship=SchemeDependant.Relationship.SPOUSE,
                      registered_on=joined_on, user=user)
    for d in (dependants or []):
        add_dependant(m, user=user, registered_on=joined_on, **d)

    standing_svc.refresh(m, user=user)
    if not needs_admission and notify:
        from benevolent.services import notify as notify_svc
        from benevolent.models import NotificationEvent
        notify_svc.send(NotificationEvent.REGISTRATION_CONFIRMED, membership=m)
    return m


@db_tx.atomic
def admit(membership, *, on=None, user=None, reason="", notify=True):
    """Formally admit a member whose registration needed approval.

    Cover — and therefore any waiting period — runs from THIS date, not from the day
    their name was first typed into a list. A waiting period served by paperwork
    sitting in a drawer is not a waiting period.
    """
    if membership.status != SchemeMembership.Status.PENDING:
        raise ValidationError(
            f"{membership.member.name} is {membership.get_status_display().lower()}, "
            f"not awaiting admission.")
    on = on or _dt.date.today()
    membership.registered_on = on
    membership.status = SchemeMembership.Status.ACTIVE
    membership.save(update_fields=["registered_on", "status"])
    log(membership, MembershipEvent.Kind.ADMITTED,
        f"Admitted to {membership.scheme.name}. Cover runs from {on:%d %b %Y}.",
        user=user, on=on, reason=reason,
        from_value=SchemeMembership.Status.PENDING, to_value=membership.status)
    standing_svc.refresh(membership, user=user)
    if notify:
        from benevolent.services import notify as notify_svc
        from benevolent.models import NotificationEvent
        notify_svc.send(NotificationEvent.REGISTRATION_CONFIRMED, membership=membership)
    return membership


@db_tx.atomic
def refuse(membership, *, user=None, reason=""):
    """Refuse a pending registration. Requires a reason: telling someone they may
    not join a welfare scheme without saying why is not a thing a church should be
    able to do by accident."""
    if membership.status != SchemeMembership.Status.PENDING:
        raise ValidationError("Only a pending registration can be refused.")
    if not (reason or "").strip():
        raise ValidationError("Refusing a registration must record a reason.")
    membership.status = SchemeMembership.Status.CLOSED
    membership.left_on = _dt.date.today()
    membership.save(update_fields=["status", "left_on"])
    log(membership, MembershipEvent.Kind.REJECTED,
        f"Registration refused.", user=user, reason=reason,
        from_value=SchemeMembership.Status.PENDING, to_value=membership.status)
    standing_svc.refresh(membership, user=user)
    return membership


# ---------------------------------------------------------------------------
# The lifecycle — every one of these is a human's decision
# ---------------------------------------------------------------------------

@db_tx.atomic
def suspend(membership, *, user=None, reason="", on=None):
    """Suspend a membership. A decision, never an inference.

    Suspension is deliberately NOT something automation can do. A member who has
    stopped paying is INACTIVE — that is a fact, and a job may compute it. Removing
    their cover is a punishment, and a person should decide it and answer for it.
    """
    if not (reason or "").strip():
        raise ValidationError("A suspension must record a reason.")
    if membership.status in SchemeMembership.ENDED_STATUSES:
        raise ValidationError(
            f"{membership.member.name} is {membership.get_status_display().lower()}; "
            f"there is nothing to suspend.")
    before = membership.status
    membership.status = SchemeMembership.Status.SUSPENDED
    membership.save(update_fields=["status"])
    log(membership, MembershipEvent.Kind.SUSPENDED, "Suspended.",
        user=user, on=on, reason=reason, from_value=before, to_value=membership.status)
    standing_svc.refresh(membership, user=user)
    _notify_status_change(membership, status_note=reason)
    return membership


@db_tx.atomic
def reinstate(membership, *, on=None, user=None, reason=""):
    """Bring a suspended, withdrawn or closed member back.

    `reinstated_on` is what makes any reinstatement waiting period run from the day
    they returned rather than the day they originally joined (see
    `SchemeMembership.cover_from`). Without it, a member could lapse for years,
    rejoin the week a relative fell ill, and claim immediately on the strength of a
    joining date from 2019.

    Where the policy in force charges a reinstatement fee, it is raised
    automatically as a charge against the member (see
    `services.eligibility.evaluate_reinstatement` and
    `services.engine.charge_policy_fee`) — a rule the church published, applied
    the moment it is triggered, not a fee that depends on a treasurer
    remembering to type it in by hand.
    """
    from benevolent.services.eligibility import evaluate_reinstatement
    on = on or _dt.date.today()
    if membership.status == SchemeMembership.Status.DECEASED:
        raise ValidationError(
            "A deceased member cannot be reinstated. If their membership is to pass "
            "to a survivor, transfer it.")
    before = membership.status
    membership.status = SchemeMembership.Status.ACTIVE
    membership.left_on = None
    membership.inactive_since = None
    membership.reinstated_on = on
    membership.save(update_fields=["status", "left_on", "inactive_since",
                                   "reinstated_on"])

    checks = evaluate_reinstatement(membership, on=on)
    consequences = "; ".join(c.detail for c in checks)
    log(membership, MembershipEvent.Kind.REINSTATED,
        f"Reinstated. Any waiting period runs again from {on:%d %b %Y}.",
        user=user, on=on, reason=(reason + (f" ({consequences})" if consequences else ""))
        .strip(), from_value=before, to_value=membership.status)

    fee_check = next((c for c in checks if c.code == "reinstatement_fee"), None)
    if fee_check is not None and not fee_check.passed:
        policy = membership.scheme.policy_on(on)
        from benevolent.services import engine as engine_svc
        engine_svc.charge_policy_fee(
            membership, amount=policy.reinstatement_fee,
            reason=f"Reinstatement fee under policy v{policy.version}, "
                  f"charged automatically on reinstatement ({on:%d %b %Y}).",
            on=on, user=user)

    standing_svc.refresh(membership, user=user)
    _notify_status_change(membership, status_note="Welcome back.")
    return membership


@db_tx.atomic
def withdraw(membership, *, on=None, user=None, reason=""):
    """The member has chosen to leave."""
    if membership.status in SchemeMembership.ENDED_STATUSES:
        raise ValidationError(
            f"{membership.member.name} is already "
            f"{membership.get_status_display().lower()}.")
    on = on or _dt.date.today()
    before = membership.status
    membership.status = SchemeMembership.Status.WITHDRAWN
    membership.left_on = on
    membership.save(update_fields=["status", "left_on"])
    log(membership, MembershipEvent.Kind.WITHDRAWN, "Withdrew from the scheme.",
        user=user, on=on, reason=reason, from_value=before, to_value=membership.status)
    standing_svc.refresh(membership, user=user)
    _notify_status_change(membership, status_note="This confirms your withdrawal.")
    return membership


@db_tx.atomic
def record_death(membership, *, died_on, user=None, reason=""):
    """Record that the member has died.

    Separate from `withdraw` for reasons that are not administrative tidiness. A
    deceased member's own death is very often the LAST CLAIM on the scheme — the
    thing they paid in for. So this does not close the membership out; it marks it
    deceased, leaving the claim to be raised, assessed and paid under the policy, and
    leaving the membership available to be TRANSFERRED to a survivor if the
    constitution allows it.

    A scheme that simply deleted the membership at this point would be discarding a
    family's entitlement at the exact moment it fell due.
    """
    if membership.status == SchemeMembership.Status.DECEASED:
        raise ValidationError(f"{membership.member.name} is already recorded as deceased.")
    before = membership.status
    membership.status = SchemeMembership.Status.DECEASED
    membership.died_on = died_on
    membership.save(update_fields=["status", "died_on"])
    log(membership, MembershipEvent.Kind.DECEASED,
        f"Recorded as deceased on {died_on:%d %b %Y}.",
        user=user, on=died_on, reason=reason,
        from_value=before, to_value=membership.status)
    standing_svc.refresh(membership, user=user)

    policy = membership.scheme.policy_on(died_on)
    successor = membership.nominees.filter(active=True, is_successor=True).first()
    if policy and policy.transfer_membership_on_death and successor:
        log(membership, MembershipEvent.Kind.NOTE,
            f"{successor.name} is recorded as the successor to this membership. "
            f"Transfer it to them to keep the joining date of "
            f"{membership.joined_on:%d %b %Y}.",
            user=user, on=died_on)

    _maybe_open_death_case(scheme=membership.scheme, membership=membership,
                           event_date=died_on, user=user)
    return membership


@db_tx.atomic
def close(membership, *, user=None, reason="", on=None):
    """Close a membership out — removal, or the end of a scheme."""
    if not (reason or "").strip():
        raise ValidationError("Closing a membership must record a reason.")
    on = on or _dt.date.today()
    before = membership.status
    membership.status = SchemeMembership.Status.CLOSED
    membership.left_on = on
    membership.save(update_fields=["status", "left_on"])
    log(membership, MembershipEvent.Kind.CLOSED, "Membership closed.",
        user=user, on=on, reason=reason, from_value=before, to_value=membership.status)
    standing_svc.refresh(membership, user=user)
    return membership


# ---------------------------------------------------------------------------
# Transfer & inheritance
# ---------------------------------------------------------------------------

@db_tx.atomic
def transfer(membership, to_member, *, on=None, user=None, reason=""):
    """Pass a membership to a successor — almost always the surviving spouse.

    The point of this, and the reason it is not just "enrol the widow", is the
    joining date. A woman whose husband paid into the scheme for eleven years and
    then died should not be told she is a new member with a ninety-day wait before
    the scheme will help her. The transfer KEEPS the original `joined_on`, so the
    years the household paid in belong to the household, not to the man who died.

    Deliberately does NOT set `reinstated_on`, which would restart the waiting
    period — that field exists to stop a lapsed member gaming the scheme, and a
    grieving widow is not a lapsed member gaming the scheme.

    The old membership is closed and points at the new one, so the trail is intact
    in both directions and neither record has to be edited to tell the truth.
    """
    on = on or _dt.date.today()
    policy = membership.scheme.policy_on(on)
    if policy is not None and not policy.allow_transfers:
        raise ValidationError(
            f"Policy v{policy.version} does not permit a membership to be transferred. "
            f"The successor would have to register afresh.")
    if membership.transferred_to_id:
        raise ValidationError(
            f"{membership.number} has already been transferred to "
            f"{membership.transferred_to.number}.")
    if SchemeMembership.objects.filter(
            scheme=membership.scheme, member=to_member,
            status__in=SchemeMembership.LIVE_STATUSES).exists():
        raise ValidationError(
            f"{to_member.name} already has a live membership in "
            f"{membership.scheme.name}. A person cannot hold two.")

    new = SchemeMembership.objects.create(
        scheme=membership.scheme, member=to_member,
        # THE point of the whole function
        joined_on=membership.joined_on,
        registered_on=membership.registered_on,
        renewed_until=membership.renewed_until,
        registration_type=membership.registration_type,
        household_name=membership.household_name,
        status=SchemeMembership.Status.ACTIVE,
        succeeded_from=membership.member,
        enrolled_by=user,
        notes=f"Inherited from {membership.member.name} ({membership.number}).")

    # the household comes with the membership: the dependants were the household's,
    # not the deceased's personally
    for d in membership.dependants.filter(active=True):
        if d.member_id and d.member_id == to_member.pk:
            continue        # the successor cannot be their own dependant
        SchemeDependant.objects.create(
            membership=new, member=d.member, name=d.name,
            relationship=d.relationship, date_of_birth=d.date_of_birth,
            registered_on=d.registered_on,      # keep the original registration date
            notes=d.notes)

    if membership.status != SchemeMembership.Status.DECEASED:
        membership.status = SchemeMembership.Status.CLOSED
    membership.left_on = on
    membership.transferred_to = new
    membership.save(update_fields=["status", "left_on", "transferred_to"])

    log(membership, MembershipEvent.Kind.TRANSFERRED_OUT,
        f"Membership transferred to {to_member.name} ({new.number}).",
        user=user, on=on, reason=reason, to_value=new.number)
    log(new, MembershipEvent.Kind.TRANSFERRED_IN,
        f"Membership taken over from {membership.member.name} "
        f"({membership.number}), keeping the joining date of "
        f"{membership.joined_on:%d %b %Y} — the years already paid in are not lost.",
        user=user, on=on, reason=reason, from_value=membership.number)

    standing_svc.refresh(membership, user=user)
    standing_svc.refresh(new, user=user)
    _notify_status_change(
        new, status_note=f"Membership taken over from {membership.member.name}, "
                         f"keeping the original joining date.")
    return new


# ---------------------------------------------------------------------------
# Exemptions
# ---------------------------------------------------------------------------

@db_tx.atomic
def grant_policy_exemption(membership, *, kind, reason, from_date=None, to_date=None,
                           exempt_dues=True, exempt_levies=False, user=None):
    """Grant AND approve an exemption in one step, because the constitution
    already decided this, not a person deciding it in the moment.

    Every other exemption in this system is a discretionary human judgement —
    someone proposes it, a second person approves it, because it relieves a
    member of an obligation everyone else is carrying and that is a call two
    people should make together. A policy-computed waiver (the automatic
    bereavement dues waiver this exists for) is a different thing: the church
    already wrote the rule down and published it, so applying it is not a new
    decision that needs a second signature — it is the SAME decision, applied.
    Requiring a human "approver" here would only ever produce a rubber stamp,
    and a rubber-stamp requirement teaches people to stop reading what they
    are approving.

    It is still fully auditable — the exemption row and its MembershipEvent are
    identical in shape to a hand-granted one, and both are marked automated so
    a member (or an auditor) can always see that a policy did this, not a
    person, and can always see why.
    """
    fd = from_date or _dt.date.today()
    ex = MembershipExemption(
        membership=membership, kind=kind, reason=reason,
        from_date=fd, to_date=to_date,
        exempt_dues=exempt_dues, exempt_levies=exempt_levies,
        granted_by=user, approved_by=user, approved_at=timezone.now(),
        policy=membership.scheme.policy_on(fd))
    ex.full_clean()
    ex.save()
    log(membership, MembershipEvent.Kind.EXEMPTED,
        f"Exemption granted automatically — {ex.get_kind_display().lower()}"
        + (f" until {ex.to_date:%d %b %Y}." if ex.to_date else ", with no end date.")
        + " Applied under a published policy, not a discretionary decision.",
        user=user, on=ex.from_date, reason=reason, automated=True)
    standing_svc.refresh(membership, user=user)
    _notify_status_change(
        membership,
        status_note=f"You have been excused from contributions "
                   f"({ex.get_kind_display().lower()})"
                   + (f" until {ex.to_date:%d %b %Y}." if ex.to_date else "."))
    return ex


@db_tx.atomic
def grant_exemption(membership, *, kind, reason, from_date=None, to_date=None,
                    exempt_dues=True, exempt_levies=False, user=None, comments=""):
    """Propose that a member be excused from contributing.

    Note what this does NOT do: it does not excuse them. An exemption relieves
    someone of a financial obligation that everyone else is carrying, so it is a
    money decision, and it takes a second person to approve it — the same rule the
    module applies to a benefit. An unapproved exemption covers nothing.
    """
    policy = membership.scheme.policy_on(from_date or _dt.date.today())
    if policy is not None and not policy.allow_exemptions:
        raise ValidationError(
            f"Policy v{policy.version} does not permit exemptions.")
    if not (reason or "").strip():
        raise ValidationError(
            "An exemption must record why. An exemption without a recorded reason is "
            "indistinguishable from favouritism.")

    fd = from_date or _dt.date.today()
    ex = MembershipExemption(
        membership=membership, kind=kind, reason=reason, comments=comments or "",
        from_date=fd, to_date=to_date,
        exempt_dues=exempt_dues, exempt_levies=exempt_levies, granted_by=user,
        policy=membership.scheme.policy_on(fd))
    ex.full_clean(exclude=["approved_by"])
    ex.save()
    log(membership, MembershipEvent.Kind.EXEMPTED,
        f"Exemption proposed — {ex.get_kind_display().lower()}. It does not take "
        f"effect until it is approved.",
        user=user, on=ex.from_date, reason=reason)
    return ex


@db_tx.atomic
def approve_exemption(exemption, *, user):
    """A second person approves. Not the one who proposed it."""
    if exemption.granted_by_id and user is not None \
            and exemption.granted_by_id == user.pk:
        raise ValidationError(
            "An exemption must be approved by someone other than the person who "
            "proposed it. It relieves a member of an obligation everyone else is "
            "carrying.")
    exemption.approved_by = user
    exemption.approved_at = timezone.now()
    exemption.save(update_fields=["approved_by", "approved_at"])
    log(exemption.membership, MembershipEvent.Kind.EXEMPTED,
        f"Exemption approved — {exemption.get_kind_display().lower()}"
        + (f" until {exemption.to_date:%d %b %Y}." if exemption.to_date
           else ", with no end date."),
        user=user, on=exemption.from_date, reason=exemption.reason)
    standing_svc.refresh(exemption.membership, user=user)
    return exemption


@db_tx.atomic
def revoke_exemption(exemption, *, user=None, reason="", on=None):
    on = on or _dt.date.today()
    if not (reason or "").strip():
        raise ValidationError("Revoking an exemption must record a reason.")
    exemption.revoked_on = on
    exemption.revoked_reason = reason[:200]
    exemption.save(update_fields=["revoked_on", "revoked_reason"])
    log(exemption.membership, MembershipEvent.Kind.EXEMPT_ENDED,
        f"Exemption ended — {exemption.get_kind_display().lower()}.",
        user=user, on=on, reason=reason)
    standing_svc.refresh(exemption.membership, user=user)
    return exemption


# ---------------------------------------------------------------------------
# Household members
# ---------------------------------------------------------------------------

@db_tx.atomic
def add_dependant(membership, *, relationship, member=None, name="", phone="",
                  date_of_birth=None, registered_on=None, notes="", user=None):
    """Add a spouse or dependant to a registration.

    `member` links them to the church roll where they are on it; `name` carries
    them where they are not (a young child, an elderly parent in the village).
    Linking is strongly preferred and the form leads with it — a spouse who is a
    church member should have ONE record, whose name and phone cannot drift between
    the roll and the scheme.

    `phone` is independent of `member`: even a dependant who IS a linked church
    member may pay dues from a different, personal number than the one on their
    Member record, and the allocator matches on whatever number actually shows up
    in a narration — see SchemeDependant.phone's own docstring.
    """
    registered_on = registered_on or _dt.date.today()
    policy = membership.scheme.policy_on(registered_on)

    if relationship == SchemeDependant.Relationship.SPOUSE:
        if membership.dependants.filter(
                relationship=SchemeDependant.Relationship.SPOUSE, active=True).exists():
            raise ValidationError("This membership already has a spouse registered.")

    if policy is not None and policy.max_household_size:
        # the principal member counts towards the household
        size = 1 + membership.dependants.filter(active=True).count()
        if size >= policy.max_household_size:
            raise ValidationError(
                f"A household registration covers at most {policy.max_household_size} "
                f"people under policy v{policy.version}, and this one already covers "
                f"{size}.")

    d = SchemeDependant(
        membership=membership, member=member, name=name or "", phone=phone or "",
        relationship=relationship, date_of_birth=date_of_birth,
        registered_on=registered_on, notes=notes)
    d.full_clean(exclude=["membership"])
    d.save()
    log(membership, MembershipEvent.Kind.DEPENDANT_ADDED,
        f"{d.display_name} registered as {d.get_relationship_display().lower()}.",
        user=user, on=registered_on)
    return d


@db_tx.atomic
def update_dependant(dependant, *, member=None, name="", phone="",
                     relationship=None, date_of_birth=None, notes="", user=None):
    """Correct a dependant's own details — a typo in their name, a phone
    number added after the fact, a relationship mis-recorded at registration.

    Deliberately does NOT touch `registered_on`: that is a coverage date with
    real eligibility consequences (a dependant is covered from the day they
    were registered, never retrospectively — see add_dependant's own
    docstring), so backdating it here would be too easy a way to quietly
    extend cover. A genuine correction to WHEN someone was registered should
    go through remove + re-add, which leaves an honest trail of both dates,
    not a silent overwrite of the one that matters for a claim.
    """
    before = (dependant.member_id, dependant.name, dependant.phone,
             dependant.relationship, dependant.date_of_birth, dependant.notes)

    dependant.member = member
    dependant.name = name or ""
    dependant.phone = phone or ""
    if relationship:
        dependant.relationship = relationship
    dependant.date_of_birth = date_of_birth
    dependant.notes = notes or ""
    dependant.full_clean(exclude=["membership"])
    dependant.save()

    after = (dependant.member_id, dependant.name, dependant.phone,
            dependant.relationship, dependant.date_of_birth, dependant.notes)
    if before != after:
        log(dependant.membership, MembershipEvent.Kind.NOTE,
            f"{dependant.display_name}'s details corrected.", user=user)
    return dependant


def remove_dependant(dependant, *, on=None, user=None, reason=""):
    """Remove a dependant from cover.

    Never deleted — `removed_on` is set. A dependant who was covered when an event
    happened is still covered for that event, however the household has changed
    since; deleting the row would quietly destroy a family's entitlement to a claim
    they had already earned.
    """
    on = on or _dt.date.today()
    dependant.active = False
    dependant.removed_on = on
    dependant.save(update_fields=["active", "removed_on"])
    log(dependant.membership, MembershipEvent.Kind.DEPENDANT_REMOVED,
        f"{dependant.display_name} removed from cover.",
        user=user, on=on, reason=reason)
    return dependant


def record_dependant_death(dependant, *, died_on, user=None, reason=""):
    """Record that a dependant has died — separate from remove_dependant()
    for the same reason record_death() (above) is separate from withdraw():
    a dependant's death is very often the event a case gets raised FOR, not
    an administrative change to tidy up. `removed_on` is ALSO set (they are
    no longer an active dependant going forward) but `died_on` records
    specifically why, distinct from moving away, ageing out, or a
    correction — a treasurer or auditor reading the record later should
    never have to guess which one this was.

    The dependant record itself is never deleted. `BenevolentCase.dependant`
    keeps pointing at it regardless of `active`, so a case already raised —
    or one raised after this call — still correctly shows who it was for.
    """
    if dependant.died_on:
        raise ValidationError(f"{dependant.display_name} is already recorded as deceased.")
    dependant.active = False
    dependant.removed_on = died_on
    dependant.died_on = died_on
    dependant.save(update_fields=["active", "removed_on", "died_on"])
    log(dependant.membership, MembershipEvent.Kind.DEPENDANT_DECEASED,
        f"{dependant.display_name} recorded as deceased on {died_on:%d %b %Y}.",
        user=user, on=died_on, reason=reason)
    _maybe_open_death_case(scheme=dependant.membership.scheme,
                           dependant=dependant, event_date=died_on, user=user)
    return dependant


def household_members(membership):
    """Everyone one registration covers, principal first — plus anyone
    recently recorded as deceased, who should stay visible (with that
    status shown) rather than silently vanish the way a dependant removed
    for any OTHER reason correctly does."""
    from django.db.models import Q
    rows = [{"person": membership.member, "role": "Principal member",
             "member": membership.member, "since": membership.cover_from,
             "dependant": None}]
    living_or_deceased = Q(active=True) | Q(died_on__isnull=False)
    for d in membership.dependants.filter(living_or_deceased).order_by(
            "relationship", "name"):
        rows.append({"person": d.member or d, "role": d.get_relationship_display(),
                     "member": d.member, "since": d.registered_on, "dependant": d})
    return rows


def registry(scheme=None, standing=None, status=None, q=""):
    """The register itself, with everything a treasurer looks at in one query.

    Deliberately does NOT recompute standing on read: it reads the cached column,
    because a register of four hundred members is looked at far more often than it
    changes, and recomputing on every page load would make the page slow enough that
    people stopped looking at it. The cache is refreshed by the automation job and
    by every write in this module.
    """
    qs = (SchemeMembership.objects
          .select_related("scheme", "member")
          .prefetch_related("dependants", "exemptions")
          .order_by("scheme__name", "member__name"))
    if scheme is not None:
        qs = qs.filter(scheme=scheme)
    if standing:
        qs = qs.filter(standing=standing)
    if status:
        qs = qs.filter(status=status)
    if q:
        from django.db.models import Q
        qs = qs.filter(Q(member__name__icontains=q) | Q(number__icontains=q)
                       | Q(household_name__icontains=q))
    return qs


def standing_counts(scheme=None):
    """How the scheme's register breaks down. The number a board asks for."""
    from django.db.models import Count
    qs = SchemeMembership.objects.all()
    if scheme is not None:
        qs = qs.filter(scheme=scheme)
    counts = dict(qs.values_list("standing").annotate(n=Count("id")))
    return [{"standing": s, "label": Standing(s).label, "count": counts.get(s, 0)}
            for s, _ in Standing.choices]
