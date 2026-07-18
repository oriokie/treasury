"""Fraud detection — red flags a welfare scheme should not miss.

This is NOT statistical anomaly detection, and it is deliberately not dressed up
as such. Fraud in a small church welfare scheme is not a strange number in a
distribution; it is a person doing something a control was meant to stop, or a
pattern across several ordinary-looking events that no single per-transaction
check can see. So this module is a RETROSPECTIVE SCANNER: it walks the record and
surfaces the patterns an auditor would look for by hand, each with the specific
evidence behind it, so a human can judge it.

Every signal it raises is exactly that — a SIGNAL, never a verdict. Real ones
have innocent explanations (a small church where the same elder genuinely does
raise most cases; a member who joined and was widowed a month later). So the scan
never blocks anything and never accuses anyone; it produces a ranked list of
things worth a second look, with the facts stated, and leaves the judgement where
it belongs. Flagging an honest case is a small cost; missing a dishonest one is
not.

The vectors it looks along are the ones specific to this domain:

  Control breaches
    * self_approval        — the raiser also approved (segregation breach)
    * payee_is_actor       — money paid to the person who raised/recorded it
    * override_approved    — approved despite failing eligibility
    * approved_into_deficit— approved when the fund could not afford it

  Membership abuse
    * rapid_claim          — claimed almost immediately after joining, with few
                             contributions (join-to-claim, then vanish)
    * enrol_claim_leave    — joined, claimed, and left in quick succession

  Identity / collusion
    * repeat_beneficiary   — the same person benefits across many cases
    * shared_phone         — one phone number across many members

  Contribution manipulation
    * reversal_after_claim — a contribution reversed soon after a claim was paid
    * reversal_cluster     — many reversals by one user in a short window

None of these are new facts about the data; they are new QUESTIONS asked of facts
the system already records (who raised, who approved, who was paid, when someone
joined, what was reversed and by whom). That is why the scanner needs no new model
fields — the audit trail the rest of the module keeps is what makes it possible.
"""
from __future__ import annotations

import datetime as _dt
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Count

from benevolent.models import (BenevolentCase, BenevolentContribution,
                               BenevolentScheme, SchemeMembership)


# severity ordering, so a mixed list sorts worst-first
_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


@dataclass
class FraudSignal:
    """One red flag. `code` is stable/machine-readable; `severity` ranks it;
    `detail` states the specific evidence; the object refs let the UI link
    straight to the case/member/contribution in question."""
    code: str
    label: str
    severity: str                     # 'high' | 'medium' | 'low'
    detail: str
    scheme_id: int | None = None
    case_id: int | None = None
    membership_id: int | None = None
    contribution_id: int | None = None
    subjects: list = field(default_factory=list)   # free-form ids for grouping

    @property
    def rank(self):
        return _SEVERITY_RANK.get(self.severity, 9)

    def as_dict(self):
        return {"code": self.code, "label": self.label, "severity": self.severity,
                "detail": self.detail, "scheme_id": self.scheme_id,
                "case_id": self.case_id, "membership_id": self.membership_id,
                "contribution_id": self.contribution_id}


# ---------------------------------------------------------------------------
# Control breaches
# ---------------------------------------------------------------------------

_DECIDED = [BenevolentCase.Status.APPROVED, BenevolentCase.Status.PARTLY_PAID,
            BenevolentCase.Status.PAID, BenevolentCase.Status.CLOSED]


def _scan_self_approval(cases):
    out = []
    for c in cases:
        if c.raised_by_id and c.approved_by_id and c.raised_by_id == c.approved_by_id:
            out.append(FraudSignal(
                "self_approval", "Raised and approved by the same person", "high",
                f"{c.number} was both raised and approved by "
                f"{c.approved_by.get_username() if c.approved_by else 'the same user'}. "
                f"A benefit is meant to be approved by someone other than whoever raised "
                f"it — segregation of duties.",
                scheme_id=c.scheme_id, case_id=c.pk))
    return out


def _scan_payee_is_actor(cases):
    """Money paid out to the very person who raised, assessed or recorded the
    case. Matched on name, because the payee is a free-typed name, not a user —
    so this is a heuristic (a genuine namesake will trip it), hence medium."""
    out = []
    for c in cases:
        actors = {}
        if c.raised_by_id and c.raised_by:
            actors[(c.raised_by.get_full_name() or c.raised_by.get_username()).strip().lower()] = "raised"
        if c.approved_by_id and c.approved_by:
            actors[(c.approved_by.get_full_name() or c.approved_by.get_username()).strip().lower()] = "approved"
        for p in c.payouts.all():
            payee = (p.payee_name or "").strip().lower()
            if payee and payee in actors:
                out.append(FraudSignal(
                    "payee_is_actor", "Paid to the person who handled the case", "high",
                    f"{c.number}: a payout was made to '{p.payee_name}', who also "
                    f"{actors[payee]} the case. Money handled and received by one person "
                    f"is a classic control weakness.",
                    scheme_id=c.scheme_id, case_id=c.pk))
    return out


def _scan_override_approved(cases):
    out = []
    for c in cases:
        if (c.override_reason or "").strip() and c.status in _DECIDED:
            snap = c.eligibility_snapshot or {}
            if not snap.get("eligible", True):
                failed = "; ".join(x["label"] for x in c.failed_checks) or "eligibility"
                out.append(FraudSignal(
                    "override_approved", "Approved over a failed eligibility check",
                    "medium",
                    f"{c.number} failed {failed} but was approved with an override. "
                    f"Legitimate, but override approvals are where a scheme's rules get "
                    f"bent, so each is worth review.",
                    scheme_id=c.scheme_id, case_id=c.pk))
    return out


def _scan_approved_into_deficit(schemes):
    """Cases approved while the fund could not afford its own approvals — a payout
    authorised into a hole. Uses the solvency service's position."""
    from benevolent.services import solvency
    out = []
    for s in schemes:
        pos = solvency.fund_position(s)
        if pos.is_depleted:
            out.append(FraudSignal(
                "approved_into_deficit", "Fund approved beyond its balance", "medium",
                f"{s.name}'s approved-but-unpaid benefits exceed its balance by "
                f"{abs(pos.available_after_approved):,.2f}. Not fraud in itself, but a "
                f"fund that keeps approving past its cash is where pressure to cut "
                f"corners builds.",
                scheme_id=s.pk))
    return out


# ---------------------------------------------------------------------------
# Membership abuse
# ---------------------------------------------------------------------------

def _scan_rapid_claim(cases, *, days=60):
    """A member who claimed almost immediately after joining, having paid little
    or nothing in. The oldest trick in mutual-aid: join when trouble is already
    foreseeable, claim, and the scheme carries it."""
    out = []
    for c in cases:
        mem = c.membership
        if mem is None or not mem.cover_from:
            continue
        gap = (c.event_date - mem.cover_from).days
        if 0 <= gap <= days:
            paid = mem.contribution_count
            if paid <= 1:
                out.append(FraudSignal(
                    "rapid_claim", "Claimed soon after joining", "medium",
                    f"{c.number}: {mem.member.name} joined on {mem.cover_from:%d %b %Y} "
                    f"and the event was {gap} day(s) later, with {paid} contribution(s) "
                    f"on record. Worth confirming the membership pre-dated the event in "
                    f"good faith.",
                    scheme_id=c.scheme_id, case_id=c.pk, membership_id=mem.pk))
    return out


def _scan_enrol_claim_leave(cases, *, days=120):
    """Joined, claimed, and left in quick succession — took a benefit out and
    stopped supporting the scheme almost at once."""
    out = []
    for c in cases:
        mem = c.membership
        if mem is None or not mem.left_on or not mem.cover_from:
            continue
        span = (mem.left_on - mem.cover_from).days
        # left within `days` of the claim event
        if c.event_date <= mem.left_on and (mem.left_on - c.event_date).days <= days:
            out.append(FraudSignal(
                "enrol_claim_leave", "Joined, claimed, then left", "medium",
                f"{c.number}: {mem.member.name} was a member for {span} day(s) — joined "
                f"{mem.cover_from:%d %b %Y}, claimed for an event on "
                f"{c.event_date:%d %b %Y}, and left {mem.left_on:%d %b %Y}. A benefit "
                f"taken and support withdrawn soon after is worth a look.",
                scheme_id=c.scheme_id, case_id=c.pk, membership_id=mem.pk))
    return out


# ---------------------------------------------------------------------------
# Identity / collusion
# ---------------------------------------------------------------------------

def _norm_name(s):
    return " ".join((s or "").strip().lower().split())


def _scan_repeat_beneficiary(cases, *, threshold=3):
    """The same person named as beneficiary/payee across several cases under
    different members — one individual benefiting repeatedly through others."""
    by_name = defaultdict(list)
    for c in cases:
        name = _norm_name(c.beneficiary_display)
        if name and name != "—":
            by_name[name].append(c)
    out = []
    for name, group in by_name.items():
        # distinct MEMBERS the beneficiary claimed through
        members = {c.membership_id for c in group if c.membership_id}
        if len(group) >= threshold and len(members) >= 2:
            out.append(FraudSignal(
                "repeat_beneficiary", "One beneficiary across several members", "medium",
                f"'{group[0].beneficiary_display}' is the beneficiary on {len(group)} "
                f"cases across {len(members)} different memberships. A single person "
                f"benefiting through several members can be a family's genuine hardship "
                f"— or a ring.",
                scheme_id=group[0].scheme_id,
                subjects=[c.pk for c in group]))
    return out


def _scan_shared_phone(schemes, *, threshold=4):
    """One phone number registered against many members. Families share a phone,
    so a couple is nothing — but a number on eight members is worth explaining."""
    out = []
    for s in schemes:
        counts = (SchemeMembership.objects
                  .filter(scheme=s).exclude(member__phone="")
                  .exclude(member__phone__isnull=True)
                  .values("member__phone")
                  .annotate(n=Count("id")).filter(n__gte=threshold))
        for row in counts:
            out.append(FraudSignal(
                "shared_phone", "One phone on many members", "low",
                f"{s.name}: phone {row['member__phone']} is registered against "
                f"{row['n']} members. Often a shared family line; occasionally one "
                f"person running several memberships.",
                scheme_id=s.pk))
    return out


# ---------------------------------------------------------------------------
# Contribution manipulation
# ---------------------------------------------------------------------------

def _scan_reversal_after_claim(schemes, *, days=45):
    """A contribution reversed soon after the member's claim was paid — money put
    in to look paid-up long enough to qualify, then pulled back out once the
    benefit was secured."""
    out = []
    for s in schemes:
        paid_cases = list(BenevolentCase.objects.filter(
            scheme=s, status__in=_DECIDED, membership__isnull=False)
            .select_related("membership"))
        by_member = defaultdict(list)
        for c in paid_cases:
            by_member[c.membership_id].append(c)
        reversed_qs = BenevolentContribution.objects.filter(
            scheme=s, reversed_at__isnull=False, membership__isnull=False
        ).select_related("membership", "transaction")
        for contrib in reversed_qs:
            for case in by_member.get(contrib.membership_id, []):
                rev_date = contrib.reversed_at.date()
                if case.approved_at:
                    gap = (rev_date - case.approved_at.date()).days
                    if 0 <= gap <= days:
                        out.append(FraudSignal(
                            "reversal_after_claim",
                            "Contribution reversed just after a claim", "high",
                            f"{contrib.membership.member.name}: a "
                            f"{contrib.amount} contribution was reversed {gap} day(s) "
                            f"after {case.number} was approved. Paying in to qualify and "
                            f"reversing once paid is a deliberate manipulation.",
                            scheme_id=s.pk, case_id=case.pk,
                            membership_id=contrib.membership_id,
                            contribution_id=contrib.pk))
                        break
    return out


def _scan_reversal_cluster(schemes, *, window_days=30, threshold=4):
    """Many contribution reversals by one user in a short window — either a data
    mess being cleaned up, or someone churning receipts."""
    out = []
    for s in schemes:
        revs = list(BenevolentContribution.objects.filter(
            scheme=s, reversed_at__isnull=False)
            .select_related("recorded_by").order_by("reversed_at"))
        by_user = defaultdict(list)
        for r in revs:
            by_user[r.recorded_by_id].append(r.reversed_at.date())
        for uid, dates in by_user.items():
            if uid is None or len(dates) < threshold:
                continue
            dates.sort()
            # sliding window
            for i in range(len(dates)):
                j = i
                while j < len(dates) and (dates[j] - dates[i]).days <= window_days:
                    j += 1
                if j - i >= threshold:
                    out.append(FraudSignal(
                        "reversal_cluster", "Many reversals in a short window", "low",
                        f"{s.name}: {j - i} contribution reversals within "
                        f"{window_days} days. Usually a clean-up; occasionally receipt "
                        f"churning worth a glance.",
                        scheme_id=s.pk))
                    break
    return out


# ---------------------------------------------------------------------------
# The public entry point
# ---------------------------------------------------------------------------

def scan(scheme=None, *, since=None):
    """Run every red-flag check and return the signals, worst first.

    `scheme` limits the scan to one scheme; `since` limits case-based checks to
    events on or after a date (the reversal/identity checks always look at the
    full relevant history, because a pattern does not respect a report window).
    """
    schemes = ([scheme] if scheme is not None
               else list(BenevolentScheme.objects.exclude(
                   status=BenevolentScheme.Status.DRAFT)))
    if not schemes:
        return []

    case_qs = (BenevolentCase.objects
               .filter(scheme__in=schemes)
               .select_related("scheme", "membership__member", "raised_by",
                               "approved_by")
               .prefetch_related("payouts"))
    if since is not None:
        case_qs = case_qs.filter(event_date__gte=since)
    cases = list(case_qs)

    signals = []
    signals += _scan_self_approval(cases)
    signals += _scan_payee_is_actor(cases)
    signals += _scan_override_approved(cases)
    signals += _scan_approved_into_deficit(schemes)
    signals += _scan_rapid_claim(cases)
    signals += _scan_enrol_claim_leave(cases)
    signals += _scan_repeat_beneficiary(cases)
    signals += _scan_shared_phone(schemes)
    signals += _scan_reversal_after_claim(schemes)
    signals += _scan_reversal_cluster(schemes)

    signals.sort(key=lambda s: (s.rank, s.code))
    return signals


def summary(scheme=None, *, since=None):
    """Counts by severity, for a KPI card / badge."""
    signals = scan(scheme, since=since)
    out = {"high": 0, "medium": 0, "low": 0, "total": len(signals)}
    for s in signals:
        out[s.severity] = out.get(s.severity, 0) + 1
    return out
