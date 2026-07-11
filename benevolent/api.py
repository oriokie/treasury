"""Read-only JSON API for the benevolent engine.

The integration surface for anything outside the HTML screens: the case form's
live eligibility preview, the assistant/intelligence layer, dashboards, and any
future external consumer.

Every figure returned here comes from the same services the screens use, which in
turn come from the Financial Metrics Registry — so an API consumer can never see
a number the Board Pack disagrees with. The API is deliberately read-only:
nothing that moves money is exposed over it, and decisions stay behind the
permissioned, audited HTML workflow.
"""
import datetime as dt
from decimal import Decimal

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View

from core.permissions import BenevolentViewMixin

from .models import (BenevolentCase, BenevolentEventType, BenevolentScheme,
                     SchemeMembership)
from .services import reporting as report_svc
from .services.eligibility import evaluate


def _d(v):
    """Money, always as a 2dp string — a stable shape for consumers."""
    return str(Decimal(v if v is not None else 0).quantize(Decimal("0.01")))


def _date(request, key, default=None):
    try:
        return dt.date.fromisoformat(request.GET.get(key) or "")
    except ValueError:
        return default


class SchemeListAPI(BenevolentViewMixin, View):
    """Every live scheme, with its balance and standing."""

    def get(self, request):
        start = _date(request, "start", dt.date(dt.date.today().year, 1, 1))
        end = _date(request, "end", dt.date.today())
        rows = report_svc.scheme_summary(start, end)
        return JsonResponse({
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "schemes": [{
                "id": r["scheme"].pk,
                "code": r["scheme"].code,
                "name": r["scheme"].name,
                "kind": r["scheme"].kind,
                "status": r["scheme"].status,
                "fund": {"id": r["fund"].pk, "name": r["fund"].name},
                "opening": _d(r["opening"]),
                "contributions": _d(r["contributions"]),
                "payouts": _d(r["payouts"]),
                "closing": _d(r["closing"]),
                "committed": _d(r["committed"]),
                "active_members": r["members"],
                "open_cases": r["open_cases"],
                "policy_version": (r["scheme"].current_policy.version
                                   if r["scheme"].current_policy else None),
            } for r in rows],
        })


class SchemeSummaryAPI(BenevolentViewMixin, View):
    """One scheme in full: figures, policy in force, and case statistics."""

    def get(self, request, pk):
        scheme = get_object_or_404(BenevolentScheme, pk=pk)
        start = _date(request, "start", dt.date(dt.date.today().year, 1, 1))
        end = _date(request, "end", dt.date.today())
        policy = scheme.current_policy
        return JsonResponse({
            "scheme": {"id": scheme.pk, "code": scheme.code, "name": scheme.name,
                       "kind": scheme.kind, "status": scheme.status,
                       "fund": scheme.fund.name},
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "financials": {
                "balance": _d(report_svc.scheme_balance(scheme)),
                "contributions": _d(report_svc.contributions_total(start, end, scheme)),
                "payouts": _d(report_svc.payouts_total(start, end, scheme)),
                "committed": _d(report_svc.approved_unpaid_total(scheme)),
                "source": "Financial Metrics Registry (fund_balance / fund_summary)",
            },
            "policy": (policy.terms_snapshot() if policy else None),
            "membership": {
                "active": scheme.memberships.filter(
                    status=SchemeMembership.Status.ACTIVE).count(),
                "total": scheme.memberships.count(),
            },
            "cases": report_svc.case_statistics(start, end, scheme),
        })


class EligibilityAPI(BenevolentViewMixin, View):
    """Ask the policy engine a question without raising a case.

    Powers the live preview on the case form ("would this claim qualify, and what
    is it worth?") and gives any integration the same transparent answer the
    treasurer sees: every check, whether it passed, and the figures compared.
    """

    def get(self, request):
        scheme = get_object_or_404(BenevolentScheme, pk=request.GET.get("scheme"))
        event_date = _date(request, "event_date", dt.date.today())
        reported = _date(request, "reported_date", dt.date.today())
        event_type = BenevolentEventType.objects.filter(
            pk=request.GET.get("event_type"), scheme=scheme).first()
        membership = SchemeMembership.objects.filter(
            pk=request.GET.get("membership"), scheme=scheme).first()
        try:
            claimed = Decimal(request.GET["claimed_amount"]) \
                if request.GET.get("claimed_amount") else None
        except Exception:  # noqa: BLE001
            claimed = None

        result = evaluate(scheme, event_type=event_type, event_date=event_date,
                          membership=membership, reported_date=reported,
                          claimed_amount=claimed)
        return JsonResponse(result.as_dict())


class CaseAPI(BenevolentViewMixin, View):
    """A case as the record shows it — including the frozen decision basis, which
    is the whole point of the audit trail."""

    def get(self, request, pk):
        case = get_object_or_404(
            BenevolentCase.objects.select_related("scheme", "event_type", "policy")
            .prefetch_related("payouts__expense"), pk=pk)
        return JsonResponse({
            "number": case.number,
            "scheme": case.scheme.code,
            "status": case.status,
            "event": {"type": case.event_type.name, "code": case.event_type.code,
                      "date": case.event_date.isoformat(),
                      "reported": case.reported_date.isoformat()},
            "claimant": case.claimant_display,
            "beneficiary": case.beneficiary_display,
            "amounts": {"claimed": _d(case.claimed_amount),
                        "assessed": _d(case.assessed_amount),
                        "approved": _d(case.approved_amount),
                        "paid": _d(case.paid_total),
                        "outstanding": _d(case.outstanding)},
            "policy_version": case.policy.version if case.policy else None,
            "policy_snapshot": case.policy_snapshot,
            "eligibility": case.eligibility_snapshot,
            "override_reason": case.override_reason,
            "payouts": [{"amount": _d(p.amount),
                         "date": p.date.isoformat() if p.date else None,
                         "status": p.status, "effective": p.effective,
                         "payee": p.payee_name,
                         "expense_id": p.expense_id} for p in case.payouts.all()],
        })
