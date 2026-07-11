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

from benevolent.models import BenevolentCase, BenevolentPayout, SchemePolicy
from benevolent.services.eligibility import evaluate_case


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
    whether it is carried. Read-only and safe from a template."""
    from benevolent.models import CaseApproval
    policy = policy or case.policy or case.scheme.policy_on(case.event_date)
    votes = list(case.committee_approvals.select_related("user"))
    approvals = [v for v in votes if v.decision == CaseApproval.Decision.APPROVE]
    rejections = [v for v in votes if v.decision == CaseApproval.Decision.REJECT]
    quorum = (policy.committee_quorum if policy else 0) or 0
    route = approval_route(case, policy, amount)
    return {
        "required": route == "COMMITTEE",
        "route": route,
        "votes": votes,
        "approvals": approvals,
        "rejections": rejections,
        "quorum": quorum,
        "have": len(approvals),
        "carried": route == "COMMITTEE" and quorum > 0 and len(approvals) >= quorum,
        "blocked": route == "COMMITTEE" and quorum > 0 and len(rejections) >= quorum,
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
    change is on the audit trail."""
    from benevolent.models import CaseApproval
    _require_status(case, [BenevolentCase.Status.ASSESSED], "voted on")
    if approval_route(case) != "COMMITTEE":
        raise ValidationError(
            f"{case.number} does not need a committee decision under the policy in force.")
    vote, _created = CaseApproval.objects.update_or_create(
        case=case, user=user,
        defaults={"decision": decision, "amount": amount, "note": note})
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
    _notify(case, "case_submitted",
            f"Benevolent case {case.number} ({case.scheme.name}) submitted for "
            f"{case.beneficiary_display}.")
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
    return result


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
    _notify(case, "case_approved",
            f"Benevolent case {case.number} approved for {amount} "
            f"({case.beneficiary_display}).")
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
    _notify(case, "case_rejected",
            f"Benevolent case {case.number} rejected — {reason[:120]}")
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
    case.refresh_status()
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
