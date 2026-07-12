"""The case workflow: raise → submit → assess → approve → pay → close.

Internal controls, deliberately
-------------------------------
The money leaves the church through the EXISTING expense machinery, not a
parallel path this module invented:

    approve_case()   records the DECISION. It moves no money and posts nothing.
    record_payout()  creates a cashbook.Expense (category=BENEVOLENCE) on the
                     scheme's fund, in PENDING status, exactly as any other
                     claim is raised.

The voucher then runs the ordinary expense route — treasurer approval, the dual
approval threshold on high values, period locks, the payment register, the
ledger posting (DR Benevolence / CR Cash) — none of which is bypassed or
weakened. A benevolent payout is therefore no easier to get out of the bank than
any other payment, which is exactly the point.

The case's payment status is DERIVED from those vouchers (refresh_status), so
rejecting or reversing a voucher flows straight back through to the case with no
second correction to remember.

Segregation of duties is enforced where it matters: the person who raised a case
cannot be the person who approves it (unless a treasurer explicitly overrides,
which is recorded).
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction as db_tx
from django.utils import timezone

from benevolent.models import BenevolentCase, BenevolentPayout, CaseEvent, SchemePolicy
from benevolent.services.eligibility import evaluate_case


# ---------------------------------------------------------------------------
# The case's own narrative (Phase 5) — mirrors services/registry.py's log()
# ---------------------------------------------------------------------------

def log(case, kind, summary, *, user=None, on=None, reason="", automated=False):
    """One line in the case's narrative. Every workflow function below writes
    one; no view or service moves a case through its lifecycle without it."""
    return CaseEvent.objects.create(
        case=case, kind=kind, on=on or _dt.date.today(),
        summary=summary[:255], reason=reason or "", actor=user,
        automated=automated or user is None)


@db_tx.atomic
def create_case(scheme, *, event_type, event_date, membership=None, dependant=None,
                beneficiary_name="", reported_date=None, description="",
                claimed_amount=None, funding_target=None, user=None):
    """Raise a case. The one place a `BenevolentCase` is created, so the first
    line of its history — that it was raised, by whom, for whom — is never
    missed the way a direct `.objects.create()` in a view would miss it."""
    case = BenevolentCase(
        scheme=scheme, membership=membership, event_type=event_type,
        dependant=dependant, beneficiary_name=beneficiary_name,
        event_date=event_date, reported_date=reported_date or _dt.date.today(),
        description=description, claimed_amount=claimed_amount,
        status=BenevolentCase.Status.DRAFT, raised_by=user)
    case.full_clean(exclude=["number", "policy_snapshot", "eligibility_snapshot"])
    case.save()
    log(case, CaseEvent.Kind.RAISED,
        f"Case raised for {case.beneficiary_display} ({case.event_type.name}, "
        f"{event_date:%d %b %Y}).", user=user, on=event_date)
    if funding_target:
        set_funding_target(case, amount=funding_target, user=user)
    return case


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def _require_status(case, allowed, action):
    if case.status not in allowed:
        raise ValidationError(
            f"{case.number} is {case.get_status_display().lower()}; it cannot be {action}.")


def _require_open_period(date):
    from core.models import period_locked
    lock = period_locked(date)
    if lock:
        raise ValidationError(f"{lock} is a closed accounting period; it cannot be posted to.")


# ---------------------------------------------------------------------------
# Committee approval — a decision made by a body, not a person
# ---------------------------------------------------------------------------

def approval_route(case, policy=None, amount=None):
    """WHO has to approve this case: a treasurer, or the committee?

    One function answers it, so the workflow, the screen and the guard can never
    disagree about whether a given benefit needs a committee — which is exactly
    the kind of drift that lets a large payment slip out on one signature.
    """
    policy = policy or case.policy or case.scheme.policy_on(case.event_date)
    if policy is None:
        return "TREASURER"
    mode = policy.approval_mode
    if mode == SchemePolicy.ApprovalMode.COMMITTEE:
        return "COMMITTEE"
    if mode == SchemePolicy.ApprovalMode.TWO_STAGE:
        amt = amount if amount is not None else (
            case.approved_amount or case.assessed_amount or Decimal(0))
        threshold = policy.committee_threshold or Decimal(0)
        return "COMMITTEE" if Decimal(amt) >= threshold else "TREASURER"
    return "TREASURER"


def committee_state(case, policy=None, amount=None):
    """Where the committee has got to on this case: the votes, the quorum, and
    whether it is carried. Read-only and safe from a template.

    Roster-aware (Phase 6): where the scheme has configured a committee roster
    (`services.committee`), `eligible_voters` lists exactly who may vote and
    `chair_approved` tracks whether the Chair specifically has weighed in —
    both None where no roster is configured, meaning "anyone with the general
    right may vote" as before. `carried` factors in
    `policy.committee_requires_chair`: a quorum of ordinary members is not
    enough if the policy says the Chair's own approval is required.
    """
    from benevolent.models import CaseApproval
    from benevolent.services import committee as committee_svc
    policy = policy or case.policy or case.scheme.policy_on(case.event_date)
    votes = list(case.committee_approvals.select_related("user"))
    approvals = [v for v in votes if v.decision == CaseApproval.Decision.APPROVE]
    rejections = [v for v in votes if v.decision == CaseApproval.Decision.REJECT]
    quorum = (policy.committee_quorum if policy else 0) or 0
    route = approval_route(case, policy, amount)

    has_roster = committee_svc.has_roster(case.scheme)
    eligible_voters = list(committee_svc.roster(case.scheme)) if has_roster else None
    chair_seat = committee_svc.chair(case.scheme) if has_roster else None
    chair_approved = (any(v.user_id == chair_seat.user_id for v in approvals)
                      if chair_seat is not None else None)
    requires_chair = bool(policy and policy.committee_requires_chair and chair_seat)

    quorum_met = quorum > 0 and len(approvals) >= quorum
    carried = (route == "COMMITTEE" and quorum_met
              and (chair_approved if requires_chair else True))
    return {
        "required": route == "COMMITTEE",
        "route": route,
        "votes": votes,
        "approvals": approvals,
        "rejections": rejections,
        "quorum": quorum,
        "have": len(approvals),
        "carried": carried,
        "blocked": route == "COMMITTEE" and quorum > 0 and len(rejections) >= quorum,
        "has_roster": has_roster,
        "eligible_voters": eligible_voters,
        "chair_seat": chair_seat,
        "chair_approved": chair_approved,
        "requires_chair": requires_chair,
        "waiting_on_chair": bool(quorum_met and requires_chair and not chair_approved),
        # where the committee members differed on the figure, the LOWEST approved
        # amount is what carries: a quorum has only truly agreed on the largest
        # sum every one of them was willing to authorise.
        "agreed_amount": (min((v.amount for v in approvals if v.amount is not None),
                              default=None)),
    }


@db_tx.atomic
def record_vote(case, *, user, decision, amount=None, note=""):
    """One committee member's decision. Re-voting replaces that member's own
    earlier vote (people change their minds after hearing the discussion), and the
    change is on the audit trail.

    Where the scheme has configured a committee roster, only a seated, active
    member of THAT roster may vote — holding the general benevolent-committee
    right is necessary (enforced at the view) but no longer sufficient once a
    church has actually named who sits on this specific scheme's committee.
    """
    from benevolent.models import CaseApproval
    from benevolent.services import committee as committee_svc
    _require_status(case, [BenevolentCase.Status.ASSESSED], "voted on")
    if approval_route(case) != "COMMITTEE":
        raise ValidationError(
            f"{case.number} does not need a committee decision under the policy in force.")
    if committee_svc.has_roster(case.scheme) and not committee_svc.is_seated(case.scheme, user):
        raise ValidationError(
            f"{user} is not seated on {case.scheme.name}'s committee. Ask whoever "
            f"manages the roster to add them first.")
    vote, created = CaseApproval.objects.update_or_create(
        case=case, user=user,
        defaults={"decision": decision, "amount": amount, "note": note})
    log(case, CaseEvent.Kind.COMMITTEE_VOTE,
        f"{user} voted {vote.get_decision_display().lower()}"
        + (f" ({amount})" if amount is not None else "") + ".",
        user=user, reason=note)
    return vote


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

@db_tx.atomic
def submit_case(case, user=None):
    """Hand a drafted case to the treasury for assessment."""
    _require_status(case, [BenevolentCase.Status.DRAFT], "submitted")
    case.status = BenevolentCase.Status.SUBMITTED
    case.submitted_at = timezone.now()
    case.save(update_fields=["status", "submitted_at"])
    log(case, CaseEvent.Kind.SUBMITTED, "Submitted for assessment.", user=user)
    _notify(case, "case_submitted",
            f"Benevolent case {case.number} ({case.scheme.name}) submitted for "
            f"{case.beneficiary_display}.")
    if case.membership_id:
        from benevolent.services import notify as notify_svc
        from benevolent.models import NotificationEvent
        notify_svc.send(NotificationEvent.CASE_RECEIVED, case=case,
                        membership=case.membership)
    return case


@db_tx.atomic
def assess_case(case, user=None):
    """Run the policy engine and FREEZE the result onto the case.

    This is the moment the case's decision basis becomes permanent: the policy
    version, its full terms, and every eligibility check. Re-assessing a case
    that has not yet been approved simply re-freezes a fresh evaluation (useful
    when a missing document is supplied); once approved, the basis is fixed.
    """
    _require_status(case, [BenevolentCase.Status.SUBMITTED, BenevolentCase.Status.ASSESSED],
                    "assessed")
    was_already_assessed = case.status == BenevolentCase.Status.ASSESSED
    result = evaluate_case(case)
    if result.policy is None:
        raise ValidationError(
            f"No policy was in force on {case.event_date:%d %b %Y}. Publish a policy "
            f"effective on or before that date before assessing this case.")

    case.policy = result.policy
    case.policy_snapshot = result.policy.terms_snapshot()
    case.eligibility_snapshot = result.as_dict()
    case.assessed_amount = result.entitlement.amount
    case.assessed_by = user
    case.assessed_at = timezone.now()
    case.status = BenevolentCase.Status.ASSESSED
    case.save(update_fields=["policy", "policy_snapshot", "eligibility_snapshot",
                             "assessed_amount", "assessed_by", "assessed_at", "status"])
    log(case, CaseEvent.Kind.ASSESSED,
        f"Assessed under policy v{result.policy.version}: "
        + ("eligible, " if result.eligible else "NOT eligible, ")
        + f"entitlement {result.entitlement.amount}.", user=user)

    # Tell the committee ONCE, the first time this case is routed to them —
    # not on every re-assessment, which would otherwise text a committee
    # member every time a treasurer attaches a missing document. Two
    # separate audiences, two separate toggles: _notify_committee tells the
    # COMMITTEE MEMBERS it is their turn to vote (Phase 7, templated,
    # per-member); _notify(..., "committee_pending", ...) tells TREASURY
    # STAFF a case is now waiting on the committee at all (Phase 2's
    # intent, only actually wired in Phase 10 — see notify_on_committee_
    # pending's own help text for why it never fired before).
    if not was_already_assessed and approval_route(case, result.policy) == "COMMITTEE":
        _notify_committee(case)
        _notify(case, "committee_pending",
                f"Benevolent case {case.number} ({case.scheme.name}) is now waiting on "
                f"the committee.")
    return result


def log_document_added(case, label, *, user=None):
    """Called from the attachment-upload view. Kept as a one-line wrapper
    rather than folding upload into this module, so the view still owns the
    file-handling and this module still owns the one place a CaseEvent for a
    case is written."""
    return log(case, CaseEvent.Kind.DOCUMENT_ADDED,
              f"Document attached: {label}." if label else "Document attached.",
              user=user)


def _apply_bereavement_exemption(case, *, user=None):
    """Where the policy says the bereaved member is automatically EXEMPT and
    also waives their ordinary dues for a period, GRANT that waiver as a real,
    visible `MembershipExemption` — not the silent arithmetic adjustment this
    used to be.

    Phase 3 established, for every other exemption in this system, that "an
    exemption without a recorded reason is indistinguishable from favouritism"
    and that a member has a right to see why they owe nothing. Waiving a
    bereaved member's dues without a record was exactly that gap: correct
    arithmetic, invisible reasoning. This closes it by routing through the
    SAME exemption machinery every other exemption uses — the standing engine,
    the member's exemptions panel and the membership event log all pick it up
    automatically, because they already know how to read one.

    Auto-approved, not merely proposed: this is a policy the church already
    wrote down and published, not a discretionary favour someone is granting
    in the moment, so it does not wait on a second person the way a
    hand-proposed exemption does.
    """
    if case.membership_id is None:
        return None
    policy = case.policy
    if policy is None or \
            policy.bereaved_contribution_policy != SchemePolicy.BereavedContributionPolicy.EXEMPT:
        return None
    if not policy.bereaved_dues_waiver_months:
        return None

    from benevolent.services import registry as reg_svc
    end = case.event_date
    for _ in range(policy.bereaved_dues_waiver_months):
        end = (end.replace(day=28) + _dt.timedelta(days=7)).replace(day=1)
    exemption = reg_svc.grant_policy_exemption(
        case.membership,
        kind="BEREAVEMENT", from_date=case.event_date, to_date=end,
        reason=f"Automatic {policy.bereaved_dues_waiver_months}-month dues waiver "
               f"following {case.number} ({case.event_type.name}), per policy "
               f"v{policy.version}.",
        exempt_dues=True, exempt_levies=False, user=user)
    log(case, CaseEvent.Kind.EXEMPTION_GRANTED,
        f"{policy.bereaved_dues_waiver_months}-month dues waiver granted to "
        f"{case.membership.member.name}, automatically, under policy v{policy.version}.",
        user=user, automated=True)
    return exemption


@db_tx.atomic
def set_funding_target(case, *, amount, user=None):
    """Set (or change) what this case is aiming to raise. Purely a fundraising
    goal — the policy alone still decides what is actually owed — so it can be
    set, changed, or left blank at any point in the case's life, by whoever is
    running the collection."""
    amount = Decimal(amount)
    if amount <= 0:
        raise ValidationError("A funding target must be a positive amount.")
    before = case.funding_target
    case.funding_target = amount
    case.funding_target_set_by = user
    case.funding_target_set_at = timezone.now()
    case.save(update_fields=["funding_target", "funding_target_set_by",
                             "funding_target_set_at"])
    log(case, CaseEvent.Kind.FUNDING_TARGET,
        f"Funding target {'changed to' if before else 'set at'} {amount}"
        + (f" (was {before})" if before and before != amount else "") + ".",
        user=user)
    return case


@db_tx.atomic
def decide_bereaved_contribution(case, *, waived, reason, user):
    """The committee's ruling on whether the bereaved member contributes to
    their own case, under a COMMITTEE_DECIDES policy. Only meaningful once —
    and only ever needed — under that specific policy; anything else has
    already been decided by the constitution itself and needs no committee."""
    policy = case.policy or case.scheme.policy_on(case.event_date)
    if policy is None or policy.bereaved_contribution_policy != \
            SchemePolicy.BereavedContributionPolicy.COMMITTEE_DECIDES:
        raise ValidationError(
            "This policy does not leave the bereaved member's own contribution to "
            "the committee — there is nothing to decide.")
    if not (reason or "").strip():
        raise ValidationError("Record why the committee decided this way.")
    case.bereaved_levy_waived = bool(waived)
    case.bereaved_levy_decision_reason = reason
    case.bereaved_levy_decided_by = user
    case.bereaved_levy_decided_at = timezone.now()
    case.save(update_fields=["bereaved_levy_waived", "bereaved_levy_decision_reason",
                             "bereaved_levy_decided_by", "bereaved_levy_decided_at"])
    log(case, CaseEvent.Kind.BEREAVED_DECISION,
        f"The committee decided the bereaved member "
        + ("is not required to contribute." if waived else "does contribute, as normal."),
        user=user, reason=reason)
    return case


@db_tx.atomic
def approve_case(case, *, amount=None, user, override_reason="", allow_self_approval=False):
    """Authorise a benefit. Records a DECISION only — no money moves here.

    Where the policy routes this case to the committee, this is the act of
    RECORDING the committee's decision, and it will not proceed unless the quorum
    has actually been reached. An individual — even a treasurer — cannot approve
    past a committee: that is the point of having one.

    An ineligible case can still be approved where the policy permits an override,
    but only with a written reason, which becomes part of the permanent record and
    appears on the audit trail.
    """
    _require_status(case, [BenevolentCase.Status.ASSESSED], "approved")
    if not case.policy_id:
        raise ValidationError("This case has not been assessed against a policy.")

    amount = Decimal(amount) if amount is not None else (case.assessed_amount or Decimal(0))
    if amount <= 0:
        raise ValidationError("The approved benefit must be a positive amount.")

    # ---- who is allowed to authorise this? ------------------------------
    state = committee_state(case, case.policy, amount)
    if state["required"]:
        if state["blocked"]:
            raise ValidationError(
                f"The committee has rejected {case.number} "
                f"({len(state['rejections'])} of {state['quorum']} needed to refuse).")
        if not state["carried"]:
            if state.get("waiting_on_chair"):
                raise ValidationError(
                    f"{case.number} has its quorum of {state['quorum']} approvals, but "
                    f"policy v{case.policy.version} requires the Chair's approval "
                    f"specifically, and {state['chair_seat'].user} has not yet voted.")
            raise ValidationError(
                f"{case.number} needs a committee decision under policy "
                f"v{case.policy.version}: {state['have']} of {state['quorum']} approvals "
                f"recorded. A benefit routed to the committee cannot be authorised by one "
                f"person, whatever their role.")
        agreed = state["agreed_amount"]
        if agreed is not None and amount > agreed:
            raise ValidationError(
                f"The committee's quorum agreed on at most {agreed}. Approving {amount} "
                f"would authorise more than every member of the quorum was willing to.")
    else:
        # segregation of duties: the raiser is not the approver
        if (not allow_self_approval and user is not None and case.raised_by_id
                and case.raised_by_id == user.pk):
            raise ValidationError(
                f"{case.number} was raised by you. A benefit must be approved by someone "
                f"other than the person who raised the case.")

    snap = case.eligibility_snapshot or {}
    eligible = bool(snap.get("eligible"))
    if not eligible:
        if not case.policy.allow_override:
            failed = "; ".join(c["label"] for c in case.failed_checks) or "eligibility"
            raise ValidationError(
                f"{case.number} fails {failed}, and policy v{case.policy.version} does not "
                f"permit an override.")
        if not (override_reason or "").strip():
            raise ValidationError(
                "This case does not meet the policy's conditions. To approve it anyway you "
                "must record why — the reason is kept on the permanent record.")

    cap = case.policy.benefit_cap
    rule = case.policy.rule_for(case.event_type)
    if rule is not None and rule.cap is not None:
        cap = rule.cap
    if cap is not None and amount > cap and not (override_reason or "").strip():
        raise ValidationError(
            f"{amount} exceeds the policy cap of {cap} for this event. Approve at or below "
            f"the cap, or record an override reason.")

    case.approved_amount = amount
    case.override_reason = override_reason or ""
    case.approved_by = user
    case.approved_at = timezone.now()
    case.status = BenevolentCase.Status.APPROVED
    case.save(update_fields=["approved_amount", "override_reason", "approved_by",
                             "approved_at", "status"])
    log(case, CaseEvent.Kind.APPROVED,
        f"Approved for {amount}."
        + (f" Override: {override_reason}" if override_reason else ""),
        user=user, reason=override_reason)
    _notify(case, "case_approved",
            f"Benevolent case {case.number} approved for {amount} "
            f"({case.beneficiary_display}).")
    if case.membership_id:
        from benevolent.services import notify as notify_svc
        from benevolent.models import NotificationEvent
        notify_svc.send(NotificationEvent.CASE_DECIDED, case=case,
                        membership=case.membership,
                        extra={"decision": "approved",
                              "amount_clause": f" The approved benefit is {amount}."})

    _apply_bereavement_exemption(case, user=user)
    return case


@db_tx.atomic
def reject_case(case, *, reason, user=None):
    _require_status(
        case, [BenevolentCase.Status.SUBMITTED, BenevolentCase.Status.ASSESSED], "rejected")
    if not (reason or "").strip():
        raise ValidationError("A rejection must record a reason.")
    case.status = BenevolentCase.Status.REJECTED
    case.rejection_reason = reason
    case.rejected_by = user
    case.closed_at = timezone.now()
    case.save(update_fields=["status", "rejection_reason", "rejected_by", "closed_at"])
    log(case, CaseEvent.Kind.REJECTED, "Rejected.", user=user, reason=reason)
    _notify(case, "case_rejected",
            f"Benevolent case {case.number} rejected — {reason[:120]}")
    if case.membership_id:
        from benevolent.services import notify as notify_svc
        from benevolent.models import NotificationEvent
        notify_svc.send(NotificationEvent.CASE_DECIDED, case=case,
                        membership=case.membership,
                        extra={"decision": "not approved", "amount_clause": ""})
    return case


@db_tx.atomic
def cancel_case(case, *, user=None, reason=""):
    """Withdraw a case that should never have been raised. Only possible while
    nothing has been paid — once money has moved, the vouchers must be reversed
    through the cash book first, so the ledger and the case stay in step."""
    if case.paid_total > 0:
        raise ValidationError(
            f"{case.number} has payments against it. Reverse or reject the payment "
            f"voucher(s) in the cash book first; the case will follow automatically.")
    if case.status in (BenevolentCase.Status.PAID, BenevolentCase.Status.CLOSED):
        raise ValidationError(f"{case.number} is {case.get_status_display().lower()}.")
    case.status = BenevolentCase.Status.CANCELLED
    case.rejection_reason = reason or case.rejection_reason
    case.closed_at = timezone.now()
    case.save(update_fields=["status", "rejection_reason", "closed_at"])
    log(case, CaseEvent.Kind.CANCELLED, "Cancelled.", user=user, reason=reason)
    return case


# ---------------------------------------------------------------------------
# Paying the benefit — through the ordinary expense route
# ---------------------------------------------------------------------------

@db_tx.atomic
def record_payout(case, *, amount, date=None, user, payee_name="", method=None,
                  voucher_no="", note="", paid_from_petty_cash=False):
    """Raise the payment voucher for (part of) an approved benefit.

    Creates a cashbook.Expense in PENDING status on the scheme's fund with
    category BENEVOLENCE. It is then approved and paid through the normal
    expense workflow — this module does not, and must not, approve its own
    payments.
    """
    from cashbook.models import Expense

    _require_status(case, [BenevolentCase.Status.APPROVED, BenevolentCase.Status.PARTLY_PAID],
                    "paid")
    date = date or _dt.date.today()
    _require_open_period(date)

    amount = Decimal(amount or 0)
    if amount <= 0:
        raise ValidationError("A payout must be a positive amount.")

    # Never authorise more than was approved. `available_to_voucher` nets off
    # BOTH what has been paid and what is already sitting on a live-but-pending
    # voucher — otherwise several pending vouchers could each be raised for the
    # full amount and the case would overpay the moment they were all approved.
    # A rejected voucher releases its amount again, automatically.
    if amount > case.available_to_voucher:
        raise ValidationError(
            f"{amount} exceeds the {case.available_to_voucher} still available on "
            f"{case.number} (approved {case.approved_amount}, paid {case.paid_total}, "
            f"already on a pending voucher {case.committed_total}).")

    payee = payee_name or case.beneficiary_display
    expense = Expense.objects.create(
        date=date, department=case.scheme.fund,
        description=f"{case.scheme.name} benefit {case.number} — {payee}"[:200],
        amount=amount,
        category=Expense.Category.BENEVOLENCE,
        funding_source=Expense.FundingSource.CONTRIBUTION,
        expenditure_type=Expense.ExpenditureType.RECURRENT,
        claimant=payee[:120],
        method=method or Expense.Method.CASH,
        voucher_no=voucher_no,
        paid_from_petty_cash=paid_from_petty_cash,
        status=Expense.Status.PENDING,        # the treasurer approves it, not us
        recorded_by=user)

    payout = BenevolentPayout.objects.create(
        case=case, expense=expense, payee_name=payee[:120], note=note, created_by=user)
    case.refresh_status()
    log(case, CaseEvent.Kind.PAYOUT_RAISED,
        f"Payment voucher of {amount} raised, payable to {payee}.", user=user)
    _notify(case, "payout_raised",
            f"Payment voucher of {amount} raised on benevolent case {case.number} "
            f"— awaiting approval.")
    return payout


@db_tx.atomic
def close_case(case, *, user=None, note=""):
    """Close a fully-settled case. Cases are closed, never deleted."""
    if case.status not in (BenevolentCase.Status.PAID, BenevolentCase.Status.PARTLY_PAID,
                           BenevolentCase.Status.APPROVED):
        raise ValidationError(
            f"{case.number} is {case.get_status_display().lower()} and cannot be closed.")
    case.status = BenevolentCase.Status.CLOSED
    case.closed_at = timezone.now()
    case.save(update_fields=["status", "closed_at"])
    log(case, CaseEvent.Kind.CLOSED, "Closed." + (f" {note}" if note else ""),
        user=user, reason=note)
    return case


def sync_case_from_expense(expense):
    """Called when an expense changes status: pull the case back into line.

    This is what makes the vouchers authoritative. A treasurer rejecting a
    benevolent payment voucher in the ordinary expense screen — with no idea a
    case exists behind it — correctly returns the case to APPROVED with the
    money un-paid, and no one has to remember to do anything here.
    """
    payout = getattr(expense, "benevolent_payout", None)
    if payout is None:
        return None
    case = payout.case
    before = case.status
    case.refresh_status()
    if case.status != before:
        from cashbook.models import Expense
        if expense.status in (Expense.Status.APPROVED, Expense.Status.PAID):
            log(case, CaseEvent.Kind.PAYOUT_PAID,
                f"Voucher for {payout.amount} cleared ({expense.get_status_display().lower()}) "
                f"— case is now {case.get_status_display().lower()}.",
                automated=True)
            if case.membership_id:
                from benevolent.services import notify as notify_svc
                from benevolent.models import NotificationEvent
                notify_svc.send(NotificationEvent.PAYOUT_MADE, case=case,
                                membership=case.membership,
                                extra={"amount": f"{payout.amount:,.2f}"})
        else:
            log(case, CaseEvent.Kind.PAYOUT_REVERSED,
                f"Voucher for {payout.amount} {expense.get_status_display().lower()} in the "
                f"expense screen — case reverted to {case.get_status_display().lower()}.",
                automated=True)
    return case


# ---------------------------------------------------------------------------
# Notifications (reuses the app's existing channel — never raises into a
# workflow; a failed notification must not fail a financial decision)
# ---------------------------------------------------------------------------

def _notify(case, event, message):
    """Tell whoever the SETTINGS say should be told.

    Which events notify at all, and over which channels, is configuration
    (BenevolentSettings) — not a rule, because whether an email goes out cannot
    change whether a claim qualified. A church that wants silence gets silence.

    Never raises into the caller: a failed notification must not fail a financial
    decision.
    """
    try:
        from benevolent.models import BenevolentSettings
        from core.services.notifications import notify
        from django.urls import reverse
        cfg = BenevolentSettings.get()
        if not cfg.wants(event):
            return
        notify(f"BENEVOLENT_{event.upper()}", message,
               link=reverse("benevolent_case_detail", args=[case.pk]),
               email=cfg.staff_email())
    except Exception:  # noqa: BLE001
        pass


def _notify_committee(case):
    """Tell every seated, active committee member a decision is needed —
    where the scheme has a roster (Phase 6). Where it does not, there is no
    concrete list of who "the committee" even is beyond "anyone holding the
    general right", and texting every such person church-wide on every case
    would be worse than texting nobody — so this quietly does nothing until a
    roster exists, exactly the same additive-only rule Phase 6 established
    for voting itself."""
    from benevolent.models import NotificationEvent
    from benevolent.services import committee as committee_svc
    from benevolent.services import notify as notify_svc
    for seat in committee_svc.roster(case.scheme):
        notify_svc.send(NotificationEvent.COMMITTEE_VOTE_NEEDED, case=case, user=seat.user)
