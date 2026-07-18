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


def reserved_commitments_total(scheme=None, as_of=None):
    """A prudent reserve against open cases still in the pipeline (draft,
    submitted, assessed) — money it would be imprudent to treat as free. Summed
    across live schemes when no scheme is given. Delegates to the solvency
    service so the figure is computed in exactly one place."""
    from benevolent.services import solvency
    schemes = [scheme] if scheme is not None else list(_live_schemes())
    return sum((solvency.fund_position(s, as_of=as_of).reserved_open_cases
                for s in schemes), Decimal(0))


def available_after_commitments(scheme, as_of=None):
    """A scheme fund's cash left once approvals and live vouchers are honoured —
    the 'genuinely uncommitted' view. Delegates to the solvency service."""
    from benevolent.services import solvency
    return solvency.fund_position(scheme, as_of=as_of).available_after_committed


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
    from benevolent.services.standing import facts_for_scheme
    as_of = as_of or _dt.date.today()
    schemes = [scheme] if scheme is not None else list(_live_schemes())
    rows = []
    for sch in schemes:
        policy = sch.policy_on(as_of)
        if policy is None:
            continue
        actives = list(sch.memberships
                       .filter(status=SchemeMembership.Status.ACTIVE)
                       .select_related("member")
                       .prefetch_related("exemptions", "adjustments"))
        for m, f in facts_for_scheme(sch, as_of=as_of, memberships=actives):
            owed = f.arrears           # the same arrears_for value, batch-computed
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


# ===========================================================================
# Analytics for the reporting-gap reports (item 6). Each is a BREAKDOWN or an
# operational metric, not a new financial figure: money totals still come from
# the registry (scheme_summary/contributions_total/payouts_total), and these add
# the operational dimensions the fund tables cannot know (compliance %, turnaround
# days, demographics, rejection reasons, missing documents).
# ===========================================================================

def contribution_compliance(scheme=None, as_of=None):
    """Per active member of a dues scheme: how many dues periods have fallen due,
    how many were paid in full, and the resulting compliance %. Reuses the
    standing engine's own period counters (`facts_for`), so 'X of Y periods paid'
    here and the member's arrears can never disagree."""
    from benevolent.models import SchemeMembership, SchemePolicy
    from benevolent.services.standing import facts_for_scheme
    as_of = as_of or _dt.date.today()
    schemes = ([scheme] if scheme is not None
               else list(_live_schemes()))
    rows = []
    periodic = (SchemePolicy.ContributionMode.FIXED_PERIODIC,
                SchemePolicy.ContributionMode.HYBRID)
    for sch in schemes:
        policy = sch.policy_on(as_of)
        if policy is None or policy.contribution_mode not in periodic:
            continue     # only dues schemes have a compliance % to speak of
        actives = list(sch.memberships
                       .filter(status=SchemeMembership.Status.ACTIVE)
                       .select_related("member")
                       .prefetch_related("exemptions", "adjustments"))
        # ONE batch pass for the whole scheme instead of facts_for per member
        for m, f in facts_for_scheme(sch, as_of=as_of, memberships=actives):
            due = f.total_periods
            paid = f.paid_periods
            pct = (Decimal(paid) / Decimal(due) * 100) if due else Decimal(100)
            rows.append({
                "membership": m, "scheme": sch, "due": due, "paid": paid,
                "missed": f.missed_periods, "compliance": pct.quantize(Decimal("1")),
            })
    rows.sort(key=lambda r: r["compliance"])   # worst compliance first
    return rows


def case_turnaround(scheme=None, start=None, end=None):
    """How long cases take at each stage, in days, from the timestamps the case
    lifecycle already records (raised → submitted → assessed → approved → paid).
    Operational, not financial."""
    qs = (BenevolentCase.objects.select_related("scheme")
          .filter(approved_at__isnull=False))
    if scheme is not None:
        qs = qs.filter(scheme=scheme)
    if start:
        qs = qs.filter(event_date__gte=start)
    if end:
        qs = qs.filter(event_date__lte=end)

    def _days(a, b):
        if not a or not b:
            return None
        return (b - a).total_seconds() / 86400.0

    rows = []
    submit_to_assess, assess_to_approve, report_to_pay = [], [], []
    for c in qs:
        first_pay = min((p.expense.date for p in c.payouts.select_related("expense")
                         if p.effective), default=None)
        d_sub_ass = _days(c.submitted_at, c.assessed_at)
        d_ass_app = _days(c.assessed_at, c.approved_at)
        if d_sub_ass is not None:
            submit_to_assess.append(d_sub_ass)
        if d_ass_app is not None:
            assess_to_approve.append(d_ass_app)
        pay_days = None
        if first_pay and c.reported_date:
            pay_days = (first_pay - c.reported_date).days
            report_to_pay.append(pay_days)
        rows.append({
            "case": c, "scheme": c.scheme,
            "submit_to_assess": d_sub_ass, "assess_to_approve": d_ass_app,
            "report_to_pay": pay_days,
        })

    def _avg(xs):
        return (Decimal(sum(xs)) / Decimal(len(xs))).quantize(Decimal("0.1")) if xs else None
    return {
        "rows": rows,
        "avg_submit_to_assess": _avg(submit_to_assess),
        "avg_assess_to_approve": _avg(assess_to_approve),
        "avg_report_to_pay": _avg(report_to_pay),
        "n": len(rows),
    }


def rejected_reasons(scheme=None, start=None, end=None):
    """Rejected cases and why, plus a tally of the reasons — the report a
    committee uses to see whether it is refusing claims consistently."""
    qs = (BenevolentCase.objects.filter(status=BenevolentCase.Status.REJECTED)
          .select_related("scheme", "membership__member", "rejected_by", "event_type")
          .order_by("-event_date"))
    if scheme is not None:
        qs = qs.filter(scheme=scheme)
    if start:
        qs = qs.filter(event_date__gte=start)
    if end:
        qs = qs.filter(event_date__lte=end)
    return list(qs)


def benefit_utilisation(start=None, end=None):
    """Per scheme: contributions in, benefits out, and the utilisation ratio
    (benefits paid ÷ contributions received) — how much of what members put in is
    flowing back out as benefits. Money figures are the registry's."""
    rows = scheme_summary(start, end)
    out = []
    for r in rows:
        contrib = r["contributions"] or Decimal(0)
        paid = r["payouts"] or Decimal(0)
        ratio = (paid / contrib * 100) if contrib else Decimal(0)
        out.append({
            "scheme": r["scheme"], "contributions": contrib, "payouts": paid,
            "utilisation": ratio.quantize(Decimal("1")),
            "net": contrib - paid,
        })
    return out


def dependant_demographics(scheme=None):
    """Dependants by relationship and by age band — the demographic picture of
    who the scheme actually covers."""
    from benevolent.models import SchemeDependant
    qs = SchemeDependant.objects.filter(active=True)
    if scheme is not None:
        qs = qs.filter(membership__scheme=scheme)
    today = _dt.date.today()
    by_rel = {}
    bands = {"0–5": 0, "6–17": 0, "18–35": 0, "36–59": 0, "60+": 0, "unknown": 0}
    total = 0
    for d in qs.only("relationship", "date_of_birth"):
        total += 1
        rel = d.get_relationship_display()
        by_rel[rel] = by_rel.get(rel, 0) + 1
        if not d.date_of_birth:
            bands["unknown"] += 1
            continue
        age = today.year - d.date_of_birth.year - (
            (today.month, today.day) < (d.date_of_birth.month, d.date_of_birth.day))
        if age <= 5:
            bands["0–5"] += 1
        elif age <= 17:
            bands["6–17"] += 1
        elif age <= 35:
            bands["18–35"] += 1
        elif age <= 59:
            bands["36–59"] += 1
        else:
            bands["60+"] += 1
    return {"total": total, "by_relationship": by_rel, "by_age_band": bands}


def household_statistics(scheme=None):
    """Aggregate household stats: how many households, average size, and the
    distribution of sizes. Built on the same household_report the household
    listing uses."""
    rows = household_report(scheme)
    n = len(rows)
    sizes = [r["size"] for r in rows]
    dist = {}
    for s in sizes:
        key = "1" if s == 1 else "2–3" if s <= 3 else "4–5" if s <= 5 else "6+"
        dist[key] = dist.get(key, 0) + 1
    avg = (Decimal(sum(sizes)) / Decimal(n)).quantize(Decimal("0.1")) if n else Decimal(0)
    total_deps = sum(len(r["dependants"]) for r in rows)
    return {"households": n, "avg_size": avg, "total_dependants": total_deps,
            "size_distribution": dist}


def missing_documents(scheme=None, start=None, end=None):
    """Open cases whose required supporting documents are not all attached — the
    'document expiry / outstanding paperwork' report. Reuses the eligibility
    engine's own `missing_required_documents`, so it agrees exactly with what
    blocks a case at approval."""
    from benevolent.services.eligibility import missing_required_documents
    qs = (BenevolentCase.objects
          .filter(status__in=BenevolentCase.OPEN_STATUSES)
          .select_related("scheme", "event_type", "membership__member")
          .prefetch_related("attachments"))
    if scheme is not None:
        qs = qs.filter(scheme=scheme)
    if start:
        qs = qs.filter(event_date__gte=start)
    if end:
        qs = qs.filter(event_date__lte=end)
    rows = []
    for c in qs:
        missing = missing_required_documents(c.event_type, c)
        if missing:
            rows.append({"case": c, "scheme": c.scheme, "missing": missing})
    return rows


def pending_approvals(scheme=None):
    """Cases awaiting a decision or a payment — the action list a treasurer works
    from. Not period-bound: a pending case is pending regardless of when it was
    raised."""
    qs = (BenevolentCase.objects
          .filter(status__in=[BenevolentCase.Status.SUBMITTED,
                              BenevolentCase.Status.ASSESSED,
                              BenevolentCase.Status.APPROVED,
                              BenevolentCase.Status.PARTLY_PAID])
          .select_related("scheme", "membership__member", "event_type")
          .order_by("event_date"))
    if scheme is not None:
        qs = qs.filter(scheme=scheme)
    return list(qs)
