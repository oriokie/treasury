"""Bulk SMS to a scheme's own membership — one send path, several audiences.

Everything here reuses `core.services.sms.send_sms` (the same Advanta
integration every other SMS in the app uses, logged the same way to
`SmsLog`) and the SAME arrears/inactivity measures the standing engine
already computes (`contributions.arrears_for`, `standing.missed_case_levies`,
`SchemeMembership.months_since_contribution`) — an audience here can never
disagree with what a member's own standing page already says about them,
because it is computed the identical way.

Round 9 follow-up items 8/9: a treasurer wants to tell everyone a case was
just approved (so the levy roster knows to expect it), warn a defaulter
before they are marked inactive, or just reach a scheme's whole membership —
different audiences, the same underlying action. One versatile page, not
three different half-built ones.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal

from benevolent.models import BenevolentCase, SchemeMembership, SchemePolicy


# ---------------------------------------------------------------------------
# Audiences — each returns a list of (membership, reason) so the sender/
# preview can show WHY someone is included, not just a bare name list.
# ---------------------------------------------------------------------------

def audience_all_active(scheme):
    qs = (SchemeMembership.objects
          .filter(scheme=scheme, status=SchemeMembership.Status.ACTIVE)
          .select_related("member").order_by("member__name"))
    return [(m, "Active member") for m in qs]


def audience_defaulters(scheme, *, as_of=None):
    """Members with something currently outstanding — dues arrears under a
    periodic policy, or an unpaid levy on a recent, decided case under a levy
    policy. Deliberately independent of whether the policy has
    `inactivity_missed_cases` configured at all — "who currently owes
    something" is a broader, more immediately useful question for a
    treasurer reaching for the SMS Center than "who is close to the specific
    threshold that would mark them inactive" (that narrower question is
    `audience_near_inactive`, below)."""
    from benevolent.services import contributions as contrib_svc

    as_of = as_of or _dt.date.today()
    policy = scheme.policy_on(as_of)
    out = []
    if policy is None:
        return out
    qs = (SchemeMembership.objects
          .filter(scheme=scheme, status__in=SchemeMembership.LIVE_STATUSES)
          .select_related("member").order_by("member__name"))
    leviable = (SchemePolicy.ContributionMode.PER_CASE_LEVY,
               SchemePolicy.ContributionMode.HYBRID)
    if policy.contribution_mode in leviable:
        decided_cases = list(BenevolentCase.objects.filter(
            scheme=scheme, event_date__lte=as_of,
            status__in=[BenevolentCase.Status.APPROVED,
                       BenevolentCase.Status.PARTLY_PAID,
                       BenevolentCase.Status.PAID,
                       BenevolentCase.Status.CLOSED]))
        for m in qs:
            unpaid = 0
            for case in decided_cases:
                if case.event_date < m.cover_from or case.membership_id == m.pk:
                    continue
                if contrib_svc.levy_paid_by(m, case) <= 0:
                    unpaid += 1
            if unpaid > 0:
                out.append((m, f"{unpaid} case levy/levies unpaid"))
    else:
        for m in qs:
            owed = contrib_svc.arrears_for(m, policy=policy, as_of=as_of)
            if owed > 0:
                out.append((m, f"{owed:,.2f} in arrears"))
    return out


def audience_near_inactive(scheme, *, as_of=None):
    """One step away from being flagged inactive under the policy currently
    in force — the warning a treasurer would want to send BEFORE it happens,
    not after. Uses the exact same thresholds standing.assess() checks
    against, just one unit short of tripping them."""
    from benevolent.services import standing as standing_svc

    as_of = as_of or _dt.date.today()
    policy = scheme.policy_on(as_of)
    out = []
    if policy is None:
        return out
    qs = (SchemeMembership.objects
          .filter(scheme=scheme, status=SchemeMembership.Status.ACTIVE)
          .select_related("member").order_by("member__name"))
    leviable = (SchemePolicy.ContributionMode.PER_CASE_LEVY,
               SchemePolicy.ContributionMode.HYBRID)
    for m in qs:
        if policy.inactivity_missed_cases and policy.contribution_mode in leviable:
            missed = standing_svc.missed_case_levies(m, policy=policy, as_of=as_of)
            if missed == policy.inactivity_missed_cases - 1:
                out.append((m, f"{missed} of {policy.inactivity_missed_cases} case "
                              f"levies missed — one more and they are inactive"))
        if policy.inactivity_months:
            idle = m.months_since_contribution(as_of=as_of)
            if idle == policy.inactivity_months - 1:
                out.append((m, f"{idle} of {policy.inactivity_months} months idle — "
                              f"one more and they are inactive"))
    return out


def audience_case_roster_unpaid(case, *, amount=None):
    """Everyone still owing their share of one specific case's levy — the
    exact roster raise_case_levy() already computes, filtered to those who
    have not yet paid. Reuses that function outright rather than re-deriving
    who owes what, so this can never disagree with the levy page itself."""
    from benevolent.services import contributions as contrib_svc

    try:
        summary = contrib_svc.raise_case_levy(case, amount=amount)
    except Exception:  # noqa: BLE001 — not a leviable policy, or case not ready
        return []
    return [(row["membership"], f"{row['outstanding']:,.2f} outstanding towards "
                                f"{case.number}")
            for row in summary["rows"] if row["outstanding"] > 0]


AUDIENCES = {
    "ALL_ACTIVE": ("All active members", audience_all_active),
    "DEFAULTERS": ("Defaulters (arrears or missed levies)", audience_defaulters),
    "NEAR_INACTIVE": ("One step from being marked inactive", audience_near_inactive),
    "CASE_ROSTER": ("A specific case's unpaid levy roster", None),
}


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

@dataclass
class BulkSmsResult:
    sent: int = 0
    failed: int = 0
    no_phone: int = 0
    logs: list = field(default_factory=list)

    @property
    def attempted(self):
        return self.sent + self.failed


def _format(template, membership, scheme, extra=None):
    ctx = {
        "name": membership.member.name.split()[0].title() if membership.member.name else "",
        "full_name": membership.member.name,
        "scheme": scheme.name,
        "number": membership.number,
    }
    if extra:
        ctx.update(extra)
    out = template or ""
    for k, v in ctx.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def send_bulk_sms(members_with_reasons, message_template, *, scheme, extra=None):
    """Send one message to each (membership, reason) pair, substituting
    {name}/{full_name}/{scheme}/{number} per recipient. Never raises — a
    failed or missing number for one person does not stop the rest; see
    BulkSmsResult for the tally, and SmsLog (core.models) for the permanent,
    per-recipient record, exactly as every other SMS in the app is logged.
    """
    from core.services.sms import send_sms

    result = BulkSmsResult()
    for membership, _reason in members_with_reasons:
        phone = membership.member.receipt_phone
        if not phone:
            result.no_phone += 1
            continue
        text = _format(message_template, membership, scheme, extra=extra)
        log = send_sms(phone, text)
        result.logs.append(log)
        if log and log.status == log.Status.SENT:
            result.sent += 1
        else:
            result.failed += 1
    return result


# ---------------------------------------------------------------------------
# Message presets — a starting point, always still editable before sending
# ---------------------------------------------------------------------------

PRESETS = {
    "CASE_APPROVED": (
        "A case has been approved",
        "Dear {name}, a benevolent case has been approved by {scheme}. Please "
        "check your levy contribution status. Thank you for standing with the family."),
    "ARREARS_REMINDER": (
        "Arrears reminder",
        "Dear {name}, our records show you have an outstanding balance with "
        "{scheme}. Kindly settle this at your earliest convenience."),
    "INACTIVITY_WARNING": (
        "Inactivity warning",
        "Dear {name}, you are close to being marked inactive on {scheme} due to "
        "missed contributions. Please contribute soon to keep your cover active."),
    "STATUS_CHANGE": (
        "Status change notice",
        "Dear {name}, your membership status with {scheme} has changed. Please "
        "contact the treasury office if you have any questions."),
}
