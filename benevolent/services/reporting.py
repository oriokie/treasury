"""Scheme reporting.

The single rule this module obeys: **it computes no financial figure itself.**

A scheme's money lives in a fund, and the fund's numbers already have exactly one
authoritative implementation in `reports.services.balances`, surfaced through the
Financial Metrics Registry. So every shilling shown on a benevolent screen is
fetched from the registry — the opening balance, the receipts, the expenditure
and the closing balance are literally the same figures the Board Pack and the
Statement of Financial Position show for that fund, because they are the same
call.

What this module *does* add is the scheme-side, non-financial context the fund
cannot know: how many members are enrolled, how many cases are open, and what
has been approved but not yet paid.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from django.db.models import Count, Q, Sum

from benevolent.models import (BenevolentCase, BenevolentPayout, BenevolentScheme,
                               SchemeMembership)


def _live_schemes():
    return (BenevolentScheme.objects.exclude(status=BenevolentScheme.Status.DRAFT)
            .select_related("fund").order_by("name"))


def scheme_funds():
    """The fund ids every live scheme uses — the join between this module and
    the fund-level metrics."""
    return list(_live_schemes().values_list("fund_id", flat=True))


# ---------------------------------------------------------------------------
# Financial figures — all delegated
# ---------------------------------------------------------------------------

def scheme_balance(scheme):
    """The scheme's cash: its fund's balance, from the registry. Not recomputed."""
    from core.metrics import metrics
    return metrics.fund_balance(scheme.fund) or Decimal(0)


def contributions_total(start=None, end=None, scheme=None):
    """Contributions received. Uses the registry's canonical income-credit
    definition, filtered to the scheme's fund — so it agrees, to the shilling,
    with what the income statement reports for that fund."""
    from core.metrics import income_credits
    funds = [scheme.fund_id] if scheme is not None else scheme_funds()
    if not funds:
        return Decimal(0)
    return (income_credits(start, end, department_id__in=funds)
            .aggregate(t=Sum("amount"))["t"] or Decimal(0))


def payouts_total(start=None, end=None, scheme=None):
    """Benefits paid out. Reuses the authoritative per-fund expense figure
    (which already nets off refunds) rather than aggregating expenses again."""
    from core.metrics import metrics
    funds = [scheme.fund_id] if scheme is not None else scheme_funds()
    if not funds:
        return Decimal(0)
    by_fund = metrics.expenses_by_department(start, end)
    return sum((by_fund.get(f, Decimal(0)) for f in funds), Decimal(0))


def approved_unpaid_total(scheme=None):
    """Benefits approved but with no voucher yet posted — a COMMITMENT, not a
    ledger liability.

    Stated carefully on purpose. The application recognises an expense when the
    voucher is approved, at which point it is already in the ledger and already
    reducing the fund. This figure is everything approved by the *case* decision
    that has no effective voucher behind it yet, so it deliberately does NOT
    appear on the balance sheet and does not contradict it. It is a memorandum
    figure — what the scheme has promised and must still find the cash for.
    """
    qs = BenevolentCase.objects.filter(
        status__in=[BenevolentCase.Status.APPROVED, BenevolentCase.Status.PARTLY_PAID])
    if scheme is not None:
        qs = qs.filter(scheme=scheme)
    return sum((c.outstanding for c in qs.prefetch_related("payouts__expense")), Decimal(0))


# ---------------------------------------------------------------------------
# The summary rows behind the dashboard and the reports
# ---------------------------------------------------------------------------

def scheme_summary(start=None, end=None):
    """One row per live scheme: the fund's own figures (from the registry) plus
    the scheme's operational context. The financial columns are taken from
    `fund_summary`, which is the very table the Board Pack's fund statement is
    built from, so the two can never disagree.
    """
    from core.metrics import metrics

    rows_by_fund = {}
    for row in metrics.fund_summary(start, end, False):   # unconsolidated: one row per fund
        rows_by_fund[row["department"].id] = row

    schemes = list(_live_schemes())
    # counts in two grouped queries, not one per scheme (no N+1)
    members = dict(SchemeMembership.objects
                   .filter(status=SchemeMembership.Status.ACTIVE)
                   .values_list("scheme").annotate(n=Count("id")))
    open_cases = dict(BenevolentCase.objects
                      .filter(status__in=BenevolentCase.OPEN_STATUSES)
                      .values_list("scheme").annotate(n=Count("id")))

    out = []
    for s in schemes:
        f = rows_by_fund.get(s.fund_id, {})
        out.append({
            "scheme": s,
            "fund": s.fund,
            "opening": f.get("opening", Decimal(0)),
            "contributions": f.get("receipts", Decimal(0)),
            "payouts": f.get("expenses", Decimal(0)),
            "net_transfer": f.get("net_transfer", Decimal(0)),
            "closing": f.get("closing", Decimal(0)),
            "members": members.get(s.pk, 0),
            "open_cases": open_cases.get(s.pk, 0),
            "committed": approved_unpaid_total(s),
        })
    return out


def totals(rows):
    keys = ["opening", "contributions", "payouts", "closing", "committed"]
    t = {k: sum((r[k] for r in rows), Decimal(0)) for k in keys}
    t["members"] = sum(r["members"] for r in rows)
    t["open_cases"] = sum(r["open_cases"] for r in rows)
    return t


def case_statistics(start=None, end=None, scheme=None):
    """Operational (not financial) case metrics for the dashboard."""
    qs = BenevolentCase.objects.all()
    if scheme is not None:
        qs = qs.filter(scheme=scheme)
    if start:
        qs = qs.filter(event_date__gte=start)
    if end:
        qs = qs.filter(event_date__lte=end)
    by_status = dict(qs.values_list("status").annotate(n=Count("id")))
    by_event = list(qs.values("event_type__name")
                    .annotate(n=Count("id"))
                    .order_by("-n")[:10])
    return {
        "total": qs.count(),
        "by_status": {k: by_status.get(k, 0) for k, _ in BenevolentCase.Status.choices},
        "by_event": by_event,
        "awaiting_assessment": by_status.get(BenevolentCase.Status.SUBMITTED, 0),
        "awaiting_approval": by_status.get(BenevolentCase.Status.ASSESSED, 0),
        "awaiting_payment": (by_status.get(BenevolentCase.Status.APPROVED, 0)
                             + by_status.get(BenevolentCase.Status.PARTLY_PAID, 0)),
    }


def scheme_standing_snapshot(scheme):
    """How the scheme's active membership currently stands, grouped by
    `standing` — the same field the standing engine already computes and
    caches on every SchemeMembership, just grouped here rather than
    recalculated. For a scheme funded by per-case levies, the levy roster
    (`raise_case_levy`) already gives a sharper, case-specific answer to
    "who has and hasn't paid"; this is the equivalent view for a scheme
    funded by ongoing dues, where "paid towards THIS case" isn't a
    meaningful question but "in good standing right now" is.
    """
    from benevolent.models import SchemeMembership, Standing
    qs = (SchemeMembership.objects
          .filter(scheme=scheme, status=SchemeMembership.Status.ACTIVE)
          .select_related("member"))
    groups = {}
    for m in qs:
        groups.setdefault(m.standing, []).append(m)
    good = groups.get(Standing.GOOD, [])
    arrears = groups.get(Standing.ARREARS, [])
    grace = groups.get(Standing.GRACE, [])
    exempt = groups.get(Standing.EXEMPT, [])
    inactive = groups.get(Standing.INACTIVE, [])
    return {"total": qs.count(), "good": good, "arrears": arrears, "grace": grace,
            "exempt": exempt, "inactive": inactive,
            "not_in_good_standing": arrears + grace + inactive}


def member_statement(membership, start=None, end=None):
    """One member's dealings with a scheme: what they put in, what they took out."""
    from benevolent.services.contributions import (arrears_for, contributions_qs,
                                                   contributions_total, dues_schedule)
    cases = list(membership.cases.select_related("event_type")
                 .prefetch_related("payouts__expense").order_by("-event_date"))
    return {
        "membership": membership,
        "contributions": list(contributions_qs(membership=membership, start=start, end=end)),
        "contributed": contributions_total(membership=membership, start=start, end=end),
        "arrears": arrears_for(membership),
        "dues": dues_schedule(membership),
        "cases": cases,
        "benefits_received": sum((c.paid_total for c in cases), Decimal(0)),
    }


# ---------------------------------------------------------------------------
# Phase 8 — the remaining report categories the brief names, none of them
# recomputing anything: arrears reads the same arrears_for() every screen in
# the module already reads; committee and household read straight off the
# Phase 6/3 rosters; audit reads the CaseEvent/MembershipEvent narratives and
# the override records Phase 6's Overrides & Exceptions screen already
# assembles. Report components wrap these; they do not duplicate them.
# ---------------------------------------------------------------------------

def arrears_analysis(scheme=None, as_of=None):
    """Every active member currently in arrears, oldest-looking first, with a
    simple ageing band. Not a registry FINANCIAL metric on its own — it is a
    breakdown of one, `benevolent_arrears` (the total), by member — so the
    two can never disagree: this function's own total is what the metric
    reports.
    """
    from benevolent.services.contributions import arrears_for
    as_of = as_of or _dt.date.today()
    schemes = [scheme] if scheme is not None else list(_live_schemes())
    rows = []
    for sch in schemes:
        policy = sch.policy_on(as_of)
        if policy is None:
            continue
        for m in (sch.memberships.filter(status=SchemeMembership.Status.ACTIVE)
                 .select_related("member")):
            owed = arrears_for(m, policy, as_of=as_of)
            if owed <= 0:
                continue
            months = None
            if policy.contribution_amount:
                months = int((owed / policy.contribution_amount).to_integral_value())
            rows.append({
                "membership": m, "scheme": sch, "owed": owed,
                "band": ("3+ periods" if months and months >= 3 else
                         "2 periods" if months == 2 else "1 period"),
            })
    rows.sort(key=lambda r: -r["owed"])
    return rows


def arrears_total(scheme=None, as_of=None):
    """The registry-facing total: the sum arrears_analysis() itself reports,
    so the KPI figure and the member-level breakdown are definitionally the
    same number."""
    return sum((r["owed"] for r in arrears_analysis(scheme, as_of)), Decimal(0))


def committee_report(scheme=None):
    """Every scheme's committee roster (Phase 6), with how many decisions
    each seat has actually recorded — an activity check, not just a
    membership list."""
    from benevolent.models import CaseApproval, CommitteeMember
    schemes = [scheme] if scheme is not None else list(_live_schemes())
    rows = []
    for sch in schemes:
        seats = (CommitteeMember.objects.filter(scheme=sch, active=True)
                .select_related("user"))
        votes_by_user = dict(CaseApproval.objects.filter(case__scheme=sch)
                             .values_list("user_id").annotate(n=Count("id")))
        for seat in seats:
            rows.append({
                "scheme": sch, "user": seat.user, "role": seat.get_role_display(),
                "seated_since": seat.added_at, "votes_cast": votes_by_user.get(seat.user_id, 0),
            })
    return rows


def household_report(scheme=None):
    """Every household-mode registration, with its dependants — the
    membership register's household dimension pulled out on its own, so a
    treasurer can see household size and coverage without opening each
    member individually."""
    from benevolent.models import RegistrationType, SchemeDependant
    schemes = [scheme] if scheme is not None else list(_live_schemes())
    rows = []
    for sch in schemes:
        heads = (sch.memberships
                .filter(registration_type=RegistrationType.HOUSEHOLD,
                       status=SchemeMembership.Status.ACTIVE)
                .select_related("member")
                .prefetch_related("dependants"))
        for m in heads:
            deps = [d for d in m.dependants.all() if d.active]
            rows.append({
                "scheme": sch, "membership": m,
                "household_name": m.household_name or m.member.name,
                "dependants": deps, "size": 1 + len(deps),
            })
    return rows


def audit_summary(scheme=None, start=None, end=None):
    """Every exceptional / override-type decision across the module in the
    period — the SAME rows `views_committee.OverridesExceptionsView` already
    assembles for its screen, reused here rather than re-queried with
    different logic, so the two can never silently disagree about what counts
    as an override."""
    from benevolent.models import BenevolentCase, MemberAdjustment, MembershipExemption
    start = start or (_dt.date.today() - _dt.timedelta(days=90))
    end = end or _dt.date.today()

    cases = (BenevolentCase.objects.exclude(override_reason="")
            .filter(approved_at__date__gte=start, approved_at__date__lte=end)
            .select_related("scheme", "approved_by"))
    exemptions = (MembershipExemption.objects
                 .filter(approved_at__isnull=False, from_date__gte=start, from_date__lte=end)
                 .select_related("membership__member", "membership__scheme", "approved_by"))
    adjustments = (MemberAdjustment.objects
                  .filter(approved_at__isnull=False, on__gte=start, on__lte=end)
                  .select_related("membership__member", "membership__scheme", "approved_by"))
    if scheme is not None:
        cases = cases.filter(scheme=scheme)
        exemptions = exemptions.filter(membership__scheme=scheme)
        adjustments = adjustments.filter(membership__scheme=scheme)
    return {"overridden_cases": list(cases), "exemptions": list(exemptions),
            "adjustments": list(adjustments)}
