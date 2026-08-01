"""Phase 7 — sending the templates.

Three jobs, kept separate:

    send(event, ...)          render a template and attempt delivery, once,
                              logging a BenevolentNotification whatever
                              happens.
    retry_failed(...)         re-attempt FAILED dispatches, bounded.
    send_due_reminders(...)   decide WHO is due a reminder right now (arrears,
                              renewal) and call send() for each — the piece
                              that closes a gap that survived three phases:
                              arrears_reminder_days and renewal_reminder_days
                              existed since Phase 2 and were never acted on.

Never raises into a caller. A financial decision (approving a case, charging
a fee) must complete whether or not the SMS afterwards succeeds — the same
rule `services/cases.py::_notify` already followed for staff notices.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from django.utils import timezone

from benevolent.models import (BenevolentNotification, BenevolentSettings,
                               NotificationEvent, NotificationTemplate,
                               SchemeMembership)

# ---------------------------------------------------------------------------
# Default templates — installed once, then freely editable. Every placeholder
# used below is guaranteed present in the context `send()` builds for that
# event; see _context().
# ---------------------------------------------------------------------------

_DEFAULTS = {
    (NotificationEvent.REGISTRATION_CONFIRMED, "SMS"): (
        "", "{church}: welcome to {scheme}, {member_name}. Your membership number "
            "is {membership_number}. God bless."),
    (NotificationEvent.REGISTRATION_CONFIRMED, "EMAIL"): (
        "Welcome to {scheme}",
        "Dear {member_name},\n\nYou are now registered with {scheme} at {church}, "
        "membership number {membership_number}, effective {joined_on}.\n\n"
        "Thank you for standing with your church family."),
    (NotificationEvent.RENEWAL_REMINDER, "SMS"): (
        "", "{church}: your {scheme} membership ({membership_number}) is due for "
            "renewal on {renewal_due}. Please see the treasurer."),
    (NotificationEvent.RENEWAL_REMINDER, "EMAIL"): (
        "Your {scheme} renewal is due soon",
        "Dear {member_name},\n\nYour {scheme} membership ({membership_number}) is due "
        "for renewal on {renewal_due}. Please arrange payment with the treasurer to "
        "keep your cover in force."),
    (NotificationEvent.RENEWAL_CONFIRMED, "SMS"): (
        "", "{church}: thank you, {member_name}. Your {scheme} membership is renewed "
            "until {renewed_until}."),
    (NotificationEvent.RENEWAL_CONFIRMED, "EMAIL"): (
        "Your {scheme} renewal is confirmed",
        "Dear {member_name},\n\nYour {scheme} renewal has been received. Your "
        "membership is now current until {renewed_until}. Thank you."),
    (NotificationEvent.ARREARS_REMINDER, "SMS"): (
        "", "{church}: {member_name}, your {scheme} contributions are {amount} in "
            "arrears. Kindly settle at your earliest convenience."),
    (NotificationEvent.ARREARS_REMINDER, "EMAIL"): (
        "Your {scheme} contributions",
        "Dear {member_name},\n\nOur records show {amount} outstanding on your {scheme} "
        "membership. Please arrange settlement with the treasurer."),
    (NotificationEvent.CASE_RECEIVED, "SMS"): (
        "", "{church}: we have received your {scheme} claim {case_number}. It is "
            "being assessed and we will let you know the outcome."),
    (NotificationEvent.CASE_RECEIVED, "EMAIL"): (
        "Your {scheme} claim has been received",
        "Dear {member_name},\n\nWe have received your claim {case_number} under "
        "{scheme}. It is being assessed against the current policy, and we will "
        "let you know the outcome."),
    (NotificationEvent.CASE_DECIDED, "SMS"): (
        "", "{church}: your {scheme} claim {case_number} has been {decision}."
            "{amount_clause}"),
    (NotificationEvent.CASE_DECIDED, "EMAIL"): (
        "Your {scheme} claim — {decision}",
        "Dear {member_name},\n\nYour claim {case_number} under {scheme} has been "
        "{decision}.{amount_clause}\n\nPlease contact the treasurer with any "
        "questions."),
    (NotificationEvent.PAYOUT_MADE, "SMS"): (
        "", "{church}: a payment of {amount} for {scheme} claim {case_number} has "
            "been made."),
    (NotificationEvent.PAYOUT_MADE, "EMAIL"): (
        "Payment made — {scheme}",
        "Dear {member_name},\n\nA payment of {amount} has been made against your "
        "claim {case_number} under {scheme}.\n\nMay God comfort and strengthen you."),
    (NotificationEvent.MEMBERSHIP_STATUS_CHANGED, "SMS"): (
        "", "{church}: your {scheme} membership status is now {status}. "
            "{status_note}"),
    (NotificationEvent.MEMBERSHIP_STATUS_CHANGED, "EMAIL"): (
        "Your {scheme} membership status has changed",
        "Dear {member_name},\n\nYour {scheme} membership ({membership_number}) status "
        "is now {status}. {status_note}\n\nPlease contact the treasurer with any "
        "questions."),
    (NotificationEvent.COMMITTEE_VOTE_NEEDED, "SMS"): (
        "", "{church}: {scheme} claim {case_number} needs your committee decision. "
            "Please review and vote."),
    (NotificationEvent.COMMITTEE_VOTE_NEEDED, "EMAIL"): (
        "Committee decision needed — {scheme}",
        "Dear {user_name},\n\n{scheme} claim {case_number} ({beneficiary}) has been "
        "assessed and routed to the committee. Please review and record your "
        "decision."),

    # --- member self-service portal ---------------------------------------
    (NotificationEvent.PORTAL_REQUEST_UPDATED, "SMS"): (
        "",
        "{church}: your {kind} ({reference}) {phrase}. Sign in to the member "
        "portal to see the details."),
    (NotificationEvent.PORTAL_REQUEST_UPDATED, "EMAIL"): (
        "Your request {reference} — {scheme}",
        "Dear {member_name},\n\nYour {kind} \u201c{subject}\u201d (reference "
        "{reference}) {phrase}.\n\nSign in to the member portal to read the "
        "full details and to reply if anything further is needed.\n\n{church}"),
    (NotificationEvent.PORTAL_INVITED, "SMS"): (
        "",
        "{church}: you can now see your {scheme} contributions, standing and "
        "requests online. Sign in as {username} and use \"forgot password\" to "
        "set your password."),
    (NotificationEvent.PORTAL_INVITED, "EMAIL"): (
        "Your member portal access — {church}",
        "Dear {member_name},\n\nYou can now see your {scheme} contributions, "
        "standing, household and requests online.\n\nYour username is "
        "{username}. Use the \"forgot password\" link on the sign-in page to set "
        "a password for the first time.\n\n{church}"),
}


def install_default_templates(force=False):
    """Seed every event × channel with a sensible default, idempotently — a
    treasurer edits from here, rather than starting from a blank box. Mirrors
    `services.profiles.install_builtins()`'s pattern: safe to call repeatedly,
    never overwrites an edit unless explicitly forced."""
    created = 0
    for (event, channel), (subject, body) in _DEFAULTS.items():
        tpl, was_created = NotificationTemplate.objects.get_or_create(
            event=event, channel=channel, defaults={"subject": subject, "body": body})
        if not was_created and force:
            tpl.subject, tpl.body = subject, body
            tpl.save(update_fields=["subject", "body"])
        created += int(was_created)
    return created


# ---------------------------------------------------------------------------
# Context building — one place assembles the placeholders for each event, so
# a template and the code that renders it can never quietly drift apart.
# ---------------------------------------------------------------------------

def _context(event, *, membership=None, case=None, user=None, extra=None):
    from core.models import SiteConfig
    ctx = {"church": SiteConfig.get().church_name or "Church"}
    if membership is not None:
        ctx.update({
            "member_name": membership.member.name,
            "scheme": membership.scheme.name,
            "membership_number": membership.number,
            "joined_on": membership.joined_on.strftime("%d %b %Y"),
            "renewal_due": (membership.renewed_until.strftime("%d %b %Y")
                            if membership.renewed_until else "—"),
            "renewed_until": (membership.renewed_until.strftime("%d %b %Y")
                              if membership.renewed_until else "—"),
            "status": membership.get_status_display(),
        })
    if case is not None:
        ctx.update({
            "case_number": case.number,
            "scheme": case.scheme.name,
            "beneficiary": case.beneficiary_display,
            "member_name": ctx.get("member_name") or case.claimant_display,
        })
    if user is not None:
        ctx["user_name"] = user.get_full_name() or user.username
    ctx.update(extra or {})
    return ctx


def _recipient(channel, *, membership=None, user=None):
    if user is not None:
        return user.email if channel == "EMAIL" else ""   # staff/committee: email only
    if membership is not None:
        if channel == "SMS":
            return membership.member.receipt_phone or ""
        return membership.email or ""
    return ""


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

# Which BenevolentSettings boolean gates each event. Explicit, not derived by
# string-mangling the event key, because the two naming schemes (event keys
# describe WHAT happened; settings fields describe WHO wants to hear about
# it) were never going to line up by mechanical lowercasing, and a silent
# mismatch here is exactly how a toggle stops doing anything — which is the
# bug this whole phase exists to find and fix.
_TOGGLE_FIELD = {
    NotificationEvent.REGISTRATION_CONFIRMED: "notify_member_registration",
    NotificationEvent.RENEWAL_REMINDER: "notify_member_renewal_reminder",
    NotificationEvent.RENEWAL_CONFIRMED: "notify_member_renewal_confirmed",
    NotificationEvent.ARREARS_REMINDER: "notify_member_arrears_reminder",
    NotificationEvent.CASE_RECEIVED: "notify_member_case_received",
    NotificationEvent.CASE_DECIDED: "notify_member_case_decided",
    NotificationEvent.PAYOUT_MADE: "notify_member_payout",
    NotificationEvent.MEMBERSHIP_STATUS_CHANGED: "notify_member_status_change",
    NotificationEvent.COMMITTEE_VOTE_NEEDED: "notify_committee_vote_needed",
}


def send(event, *, membership=None, case=None, user=None, extra=None):
    """Render and attempt delivery on every enabled channel for this event.

    `membership` and `user` are mutually exclusive recipients, not additive:
    pass `membership` for a message TO that member (their phone/email),
    `user` for a message to a staff member or committee member (their
    email — a `User`, unlike a `Member`, always has one). Passing both is a
    caller error waiting to happen (whose address wins?) and no call site in
    this module does it.

    Returns the list of BenevolentNotification rows created (one per channel
    actually attempted — a channel with no template, no recipient, or turned
    off in settings is skipped and never gets a row at all, since there is
    nothing to have a delivery history OF).

    Never raises. A church that wants silence gets silence, and a church with
    email misconfigured still gets its SMS.
    """
    cfg = BenevolentSettings.get()
    field = _TOGGLE_FIELD.get(event)
    if field is not None and not getattr(cfg, field, False):
        return []
    results = []
    for channel in ("SMS", "EMAIL"):
        try:
            n = _send_one(event, channel, cfg, membership=membership, case=case,
                         user=user, extra=extra)
            if n is not None:
                results.append(n)
        except Exception:  # noqa: BLE001 — never break the caller's workflow
            pass
    return results


def _send_one(event, channel, cfg, *, membership, case, user, extra):
    template = NotificationTemplate.objects.filter(
        event=event, channel=channel, active=True).first()
    if template is None:
        return None

    recipient = _recipient(channel, membership=membership, user=user)
    if not recipient:
        return None

    ctx = _context(event, membership=membership, case=case, user=user, extra=extra)
    subject, body = template.render(ctx)

    notif = BenevolentNotification.objects.create(
        event=event, channel=channel, membership=membership, case=case, user=user,
        recipient=recipient, subject=subject, body=body,
        status=BenevolentNotification.Status.QUEUED)
    _attempt(notif, cfg)
    return notif


def _attempt(notif, cfg=None):
    """Make one delivery attempt against an existing (QUEUED or FAILED) row,
    updating it in place. Shared by send() (attempt 1) and retry_failed()
    (attempts 2+), so there is exactly one place that knows how to actually
    talk to send_sms/send_email."""
    from core.models import SiteConfig
    cfg = cfg or BenevolentSettings.get()
    notif.attempts += 1

    if notif.channel == "SMS":
        from core.services.sms import send_sms
        log = send_sms(notif.recipient, notif.body, SiteConfig.get())
        notif.sms_log = log
        if log is not None and log.status == log.Status.SENT:
            notif.status = BenevolentNotification.Status.SENT
            notif.sent_at = timezone.now()
        elif log is not None and log.status == log.Status.DISABLED:
            notif.status = BenevolentNotification.Status.SKIPPED
            notif.last_error = log.response
        else:
            notif.status = BenevolentNotification.Status.FAILED
            notif.last_error = (log.response if log else "SMS engine returned nothing.")
    else:
        from core.services.email import send_email, is_configured
        if not is_configured():
            notif.status = BenevolentNotification.Status.SKIPPED
            notif.last_error = "Email is not configured."
        else:
            ok, detail = send_email(notif.subject, notif.body, notif.recipient)
            if ok:
                notif.status = BenevolentNotification.Status.SENT
                notif.sent_at = timezone.now()
            else:
                notif.status = BenevolentNotification.Status.FAILED
                notif.last_error = detail

    notif.save(update_fields=["attempts", "status", "last_error", "sent_at", "sms_log"])
    return notif


def retry_failed(max_attempts=3, limit=200):
    """Re-attempt FAILED dispatches, bounded both per-row (max_attempts) and
    per-call (limit, so one automation run cannot spend an unbounded amount
    of time retrying a genuinely broken configuration). Called from the
    existing benevolent_automation job — a new schedule was not needed for
    this, the one that already runs nightly does the job."""
    cfg = BenevolentSettings.get()
    qs = (BenevolentNotification.objects
          .filter(status=BenevolentNotification.Status.FAILED, attempts__lt=max_attempts)
          .order_by("created_at")[:limit])
    retried = 0
    for notif in qs:
        _attempt(notif, cfg)
        retried += 1
    return retried


# ---------------------------------------------------------------------------
# Due reminders — closes a gap that survived three phases: arrears_reminder_
# days and renewal_reminder_days existed since Phase 2 and were never acted
# on (docs/recommendations.md #62c, raised to HIGH).
# ---------------------------------------------------------------------------

def _last_reminder(membership, event):
    """The most recent attempt to send this reminder — whatever the outcome.
    Used purely to throttle: a SKIPPED attempt (SMS disabled, no recipient)
    still means "we already asked for this cycle" just as much as a SENT one
    did. Only excludes nothing — this is deliberately every row, not just
    the successful ones, or a misconfigured channel would defeat the
    throttle and re-attempt every single time send_due_reminders runs."""
    return (BenevolentNotification.objects
            .filter(membership=membership, event=event)
            .order_by("-created_at").first())


def send_due_reminders(scheme=None, as_of=None):
    """Send an arrears reminder to every member currently in arrears, and a
    renewal reminder to every member whose renewal falls due within the
    configured window — each throttled to at most one per
    `reminder_min_gap_days`, so a nightly job does not become a nightly text
    message. Returns {"arrears": n, "renewal": n}.

    Interpretation, stated plainly because the field's own wording ("remind a
    member N days after they fall into arrears") could be read as a one-off
    trigger: without a reliably tracked "date arrears began" to trigger from
    (standing is a recomputed cache, not an event log of when a threshold was
    first crossed), a recurring reminder — at most one every N days, for as
    long as the condition holds — delivers the same practical outcome a
    treasurer actually wants (the member keeps hearing about it until it's
    fixed) without inventing a fragile date to trigger from once.
    """
    from benevolent.models import BenevolentScheme
    from benevolent.services import contributions as contrib_svc

    as_of = as_of or _dt.date.today()
    cfg = BenevolentSettings.get()
    schemes = ([scheme] if scheme is not None
               else list(BenevolentScheme.objects.filter(
                   status=BenevolentScheme.Status.ACTIVE)))
    gap = _dt.timedelta(days=cfg.reminder_min_gap_days or 0)
    sent = {"arrears": 0, "renewal": 0}

    for sch in schemes:
        policy = sch.policy_on(as_of)
        if policy is None:
            continue
        memberships = sch.memberships.filter(
            status=SchemeMembership.Status.ACTIVE).select_related("member")

        if cfg.notify_member_arrears_reminder and (policy.arrears_treatment !=
                policy.ArrearsTreatment.IGNORE):
            for m in memberships:
                owed = contrib_svc.arrears_for(m, policy, as_of=as_of)
                if owed <= 0:
                    continue
                last = _last_reminder(m, NotificationEvent.ARREARS_REMINDER)
                if last is not None and (timezone.now() - last.created_at) < gap:
                    continue
                send(NotificationEvent.ARREARS_REMINDER, membership=m,
                    extra={"amount": f"{owed:,.2f}"})
                sent["arrears"] += 1

        if cfg.notify_member_renewal_reminder and policy.renewal_required:
            window = cfg.renewal_reminder_days or 0
            if window > 0:
                for m in memberships:
                    due = m.renewal_due_on(policy, as_of=as_of)
                    if due is None or not (as_of <= due <= as_of + _dt.timedelta(days=window)):
                        continue
                    last = _last_reminder(m, NotificationEvent.RENEWAL_REMINDER)
                    if last is not None and (timezone.now() - last.created_at) < gap:
                        continue
                    send(NotificationEvent.RENEWAL_REMINDER, membership=m)
                    sent["renewal"] += 1
    return sent
