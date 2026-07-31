"""Treasurer Intelligence Workspace + Analytics APIs.

The workspace presents intelligence (health score, prioritised insights,
recommendations, alerts) rather than raw reports, with drill-down into the
supporting reports/transactions. The analytics endpoints expose the same
structured intelligence as JSON for future mobile/AI consumers — all consuming
the Semantic Reporting Layer, no duplicated calculation.
"""
from __future__ import annotations

import datetime as _dt

from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from core.permissions import ReportAccessMixin, ExecutiveAccessMixin
from core.utils import parse_period


def _ctx(request):
    from core.reporting import ReportContext
    start, end = parse_period(request)
    return ReportContext.for_period(start, end)


def _apply_statuses(insights):
    """Attach any persisted status to each insight by fingerprint, so dismissed
    ones can be filtered and acknowledged ones marked."""
    from core.models import InsightStatus
    fps = [i.fingerprint for i in insights]
    statuses = {s.fingerprint: s for s in
                InsightStatus.objects.filter(fingerprint__in=fps)}
    live = []
    for i in insights:
        st = statuses.get(i.fingerprint)
        if st:
            i.status = st.state
        if i.status != "dismissed":
            live.append(i)
    return live


class TreasurerWorkspaceView(ExecutiveAccessMixin, TemplateView):
    """The Treasurer Intelligence Dashboard: health & risk scores, high-priority
    insights, outstanding actions and alerts — everything drilling into reports
    and transactions."""
    template_name = "intelligence/workspace.html"

    def get_context_data(self, **kwargs):
        from core.intelligence import (IntelligenceEngine, compute_health_score,
                                       recommendations_from_insights, Severity)
        from reports.models import ReportSnapshot, ReportSchedule
        ctx = super().get_context_data(**kwargs)
        rc = _ctx(self.request)

        insights = _apply_statuses(IntelligenceEngine().analyse(rc))
        recs = recommendations_from_insights(insights)
        health = compute_health_score(rc)

        # risk score: inverse of health, weighted by open critical/warning count
        criticals = [i for i in insights if i.severity == Severity.CRITICAL]
        warnings = [i for i in insights if i.severity == Severity.WARNING]
        risk = min(100, len(criticals) * 25 + len(warnings) * 10)

        # Severity bands, most serious first. The page previously rendered only
        # `high_priority` (12 of them) and nothing else, so the remaining
        # insights the engine had already computed were simply unreachable —
        # `by_category` was built here and never used by the template.
        by_severity = []
        for key, label in ((Severity.CRITICAL, "Critical"),
                           (Severity.WARNING, "Warning"),
                           (Severity.NOTICE, "Notice"),
                           (Severity.INFO, "Information")):
            items = sorted([i for i in insights if i.severity == key],
                           key=lambda i: -i.priority)
            if items:
                by_severity.append({"key": key, "label": label, "items": items,
                                    "count": len(items)})

        ctx.update({
            "start": rc.start, "end": rc.end,
            "health": health,
            "risk_score": risk,
            "risk_band": ("High" if risk >= 60 else
                          "Moderate" if risk >= 30 else "Low"),
            "insights": insights,
            "by_severity": by_severity,
            "high_priority": [i for i in insights if i.priority >= 60][:12],
            "recommendations": recs[:12],
            "critical_count": len(criticals),
            "warning_count": len(warnings),
            "by_category": self._group_by_category(insights),
            "upcoming_schedules": ReportSchedule.objects.filter(
                enabled=True).order_by("next_run")[:5],
            "recent_snapshots": ReportSnapshot.objects.all()[:5],
            "provenance_metrics": rc.metrics_used(),
        })
        return ctx

    @staticmethod
    def _group_by_category(insights):
        groups = {}
        for i in insights:
            groups.setdefault(i.category, []).append(i)
        return groups


class InsightStatusView(ReportAccessMixin, View):
    """Acknowledge/resolve/dismiss an insight, recording an audit-trail entry."""
    def post(self, request):
        from core.models import InsightStatus, InsightStatusHistory
        fp = request.POST.get("fingerprint")
        state = request.POST.get("state", "dismissed")
        code = request.POST.get("code", "")
        subject = request.POST.get("subject", "")
        note = request.POST.get("note", "")
        if not fp:
            return redirect(reverse("treasurer_workspace"))
        st, _ = InsightStatus.objects.get_or_create(
            fingerprint=fp, defaults={"code": code, "subject": subject})
        st.state = state
        st.note = note
        st.updated_by = request.user if request.user.is_authenticated else None
        st.save()
        InsightStatusHistory.objects.create(
            status=st, state=state, note=note,
            changed_by=request.user if request.user.is_authenticated else None)
        return redirect(request.META.get("HTTP_REFERER")
                        or reverse("treasurer_workspace"))


# ===========================================================================
# Analytics APIs (JSON) — for future mobile / AI consumers
# ===========================================================================

class AnalyticsInsightsAPI(ReportAccessMixin, View):
    def get(self, request):
        from core.intelligence import IntelligenceEngine
        rc = _ctx(request)
        insights = IntelligenceEngine().analyse(rc)
        return JsonResponse({
            "period": {"start": rc.start.isoformat() if rc.start else None,
                       "end": rc.end.isoformat() if rc.end else None},
            "insights": [i.as_dict() for i in insights],
            "provenance": {"metrics_used": rc.metrics_used()},
        })


class AnalyticsHealthAPI(ReportAccessMixin, View):
    def get(self, request):
        from core.intelligence import compute_health_score
        rc = _ctx(request)
        return JsonResponse(compute_health_score(rc).as_dict())


class AnalyticsTrendAPI(ReportAccessMixin, View):
    def get(self, request):
        from core.intelligence import trends
        metric = request.GET.get("metric", "total_income")
        months = int(request.GET.get("months", 12))
        do_forecast = request.GET.get("forecast") == "1"
        try:
            if do_forecast:
                result = trends.forecast(metric, history_months=min(months, 12))
            else:
                result = trends.trend(metric, months=months)
            return JsonResponse(result.as_dict())
        except Exception as e:  # noqa: BLE001
            return JsonResponse({"error": str(e)}, status=400)


class AnalyticsKnowledgeAPI(ReportAccessMixin, View):
    def get(self, request):
        from core.intelligence import knowledge
        rc = _ctx(request)
        concept = request.GET.get("concept")
        try:
            if concept:
                return JsonResponse(knowledge.knowledge_for(concept, rc))
            return JsonResponse(knowledge.full_briefing(rc))
        except KeyError as e:
            return JsonResponse({"error": str(e)}, status=404)
