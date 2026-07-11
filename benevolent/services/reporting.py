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
