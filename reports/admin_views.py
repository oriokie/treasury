"""Report administration views — Designer, Library, Feature Adoption Dashboard,
Schedules and Branding. These are the human entry points to the
configuration-driven reporting platform.

Access is gated to staff/admin (TreasurerRequiredMixin for edit actions,
ReportAccessMixin for read-only library). No accounting logic lives here — these
views arrange registered components and read the registries; every figure still
comes from the Financial Metrics Registry via the engine.
"""
from __future__ import annotations

import json

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from core.permissions import ReportAccessMixin, TreasurerRequiredMixin


# ===========================================================================
# Report Library — the central entry point
# ===========================================================================

class ReportLibraryView(ReportAccessMixin, TemplateView):
    """Central catalogue of every report — code-defined and designed — with
    categories, tags, search, favourites, and recently/frequently used."""
    template_name = "reports/library.html"

    def get_context_data(self, **kwargs):
        from core.reporting import registry
        from reports.models import (ReportDefinition, ReportFavourite,
                                    ReportUsage, ReportSchedule, ReportSnapshot)
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        q = (self.request.GET.get("q") or "").strip().lower()

        # ensure enabled designed reports are registered so they appear here
        try:
            from reports.services.designer import register_definition, DefinitionError
            for d in ReportDefinition.objects.filter(enabled=True):
                if registry.get(d.engine_key) is None:
                    try:
                        register_definition(d)
                    except DefinitionError:
                        pass
        except Exception:  # noqa: BLE001
            pass

        reports = registry.visible_to(user)
        # attach designed-report metadata (tags/category already on Report)
        items = []
        fav_keys = set(ReportFavourite.objects.filter(user=user)
                       .values_list("report_key", flat=True))
        for r in reports:
            if q and q not in r.title.lower() and q not in r.key.lower() \
                    and q not in (r.description or "").lower():
                continue
            items.append({
                "key": r.key, "title": r.title, "category": r.category,
                "description": r.description,
                "is_favourite": r.key in fav_keys,
                "designed": r.key.startswith("def__"),
            })
        by_category = {}
        for it in items:
            by_category.setdefault(it["category"], []).append(it)

        # recently used (this user) and frequently used (all)
        recent = list(ReportUsage.objects.filter(user=user)
                      .values_list("report_key", flat=True)[:20])
        seen, recent_unique = set(), []
        for k in recent:
            if k not in seen:
                seen.add(k); recent_unique.append(k)
        from django.db.models import Count
        frequent = list(ReportUsage.objects.values("report_key")
                        .annotate(n=Count("id")).order_by("-n")[:8])

        ctx.update({
            "by_category": by_category,
            "favourites": [it for it in items if it["is_favourite"]],
            "recent": recent_unique[:8],
            "frequent": frequent,
            "q": q,
            "definition_count": ReportDefinition.objects.count(),
            "schedule_count": ReportSchedule.objects.filter(enabled=True).count(),
            "snapshot_count": ReportSnapshot.objects.count(),
        })
        return ctx


class ToggleFavouriteView(ReportAccessMixin, View):
    def post(self, request, key):
        from reports.models import ReportFavourite
        fav, created = ReportFavourite.objects.get_or_create(
            report_key=key, user=request.user)
        if not created:
            fav.delete()
        return redirect(request.META.get("HTTP_REFERER") or reverse("report_library"))


# ===========================================================================
# Feature Adoption Dashboard
# ===========================================================================

class AdoptionDashboardView(ReportAccessMixin, TemplateView):
    """Platform-health dashboard: registry adoption, engine adoption, narrative
    coverage, component reuse, renderer usage, snapshot coverage, remaining
    legacy reports, deferred recommendations, consistency status, and report
    generation stats."""
    template_name = "reports/adoption_dashboard.html"

    def get_context_data(self, **kwargs):
        from core.reporting import registry, renderer_registry
        from core.reporting.components import component_registry
        from core.reporting.narrative import narrative_registry
        from core.metrics import metrics
        from reports.models import ReportSnapshot, ReportUsage, ReportSchedule
        from django.db.models import Count, Avg
        ctx = super().get_context_data(**kwargs)

        engine_reports = list(registry.all())
        # component reuse across engine reports
        comp_use = {}
        for r in engine_reports:
            for s in r.sections:
                ck = getattr(s, "key", "")
                comp_use[ck] = comp_use.get(ck, 0) + 1

        # snapshot coverage: which reports have at least one snapshot
        snap_keys = set(ReportSnapshot.objects.values_list(
            "report_key", flat=True).distinct())

        # generation stats
        usage_stats = ReportUsage.objects.aggregate(
            n=Count("id"), avg_ms=Avg("render_ms"))
        top_reports = list(ReportUsage.objects.values("report_key")
                           .annotate(n=Count("id")).order_by("-n")[:10])

        # deferred recommendations count (parse docs/recommendations.md headings)
        deferred = self._count_deferred()

        ctx.update({
            "metrics_count": len(metrics.registry),
            "engine_report_count": len(engine_reports),
            "component_count": len(component_registry._factories),
            "narrative_count": len(narrative_registry.keys()),
            "renderer_formats": renderer_registry.formats(),
            "component_reuse": sorted(comp_use.items(),
                                      key=lambda kv: -kv[1])[:15],
            "snapshot_report_count": len(snap_keys),
            "snapshot_total": ReportSnapshot.objects.count(),
            "usage_total": usage_stats["n"] or 0,
            "avg_render_ms": round(usage_stats["avg_ms"] or 0, 1),
            "top_reports": top_reports,
            "active_schedules": ReportSchedule.objects.filter(enabled=True).count(),
            "failed_runs": self._failed_runs(),
            "deferred_recommendations": deferred,
            "legacy_remaining": self._legacy_remaining(),
        })
        return ctx

    @staticmethod
    def _count_deferred():
        try:
            import os
            from django.conf import settings
            path = os.path.join(settings.BASE_DIR, "docs", "recommendations.md")
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            # count headings that are not marked ADDRESSED
            import re
            heads = re.findall(r"^## \d+\.(.+)$", text, re.M)
            return sum(1 for h in heads if "ADDRESSED" not in h.upper())
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _failed_runs():
        from reports.models import ScheduleRun
        return ScheduleRun.objects.filter(
            status=ScheduleRun.Status.FAILED).count()

    @staticmethod
    def _legacy_remaining():
        # a static, honest list mirroring docs/REPORT_MIGRATION_STATUS.md
        return [
            "Detailed Statement of Financial Position (NBV/prepayments/advances)",
            "Operational registers (Cash Book, Payment/Receipt/Cheque, Journal, "
            "Ledger, Asset/Depreciation, Envelope, Pledge, Audit)",
            "Member & ministry statements (Member/Contribution/Giving history, "
            "Donor, Ministry, Leader)",
        ]


# ===========================================================================
# Report Designer
# ===========================================================================

class DesignerListView(TreasurerRequiredMixin, TemplateView):
    template_name = "reports/designer_list.html"

    def get_context_data(self, **kwargs):
        from reports.models import ReportDefinition
        ctx = super().get_context_data(**kwargs)
        ctx["definitions"] = ReportDefinition.objects.all()
        return ctx


class DesignerEditView(TreasurerRequiredMixin, View):
    """Create/edit a report definition. The editor is JSON-backed (the section
    list + filters), with the component palette and validation surfaced so an
    administrator arranges registered components without writing code. A full
    drag-and-drop canvas can layer on top of this same persistence later."""
    template_name = "reports/designer_edit.html"

    def _palette(self):
        from core.reporting.components import component_registry
        from core.reporting.narrative import narrative_registry
        return {
            "components": component_registry.by_category(),
            "narratives": narrative_registry.keys(),
        }

    def get(self, request, key=None):
        from reports.models import ReportDefinition
        definition = None
        if key:
            definition = get_object_or_404(ReportDefinition, key=key)
        return render(request, self.template_name, {
            "definition": definition,
            "palette": self._palette(),
            "sections_json": json.dumps(definition.sections if definition else [],
                                        indent=2),
            "filters_json": json.dumps(definition.filters if definition else [],
                                       indent=2),
        })

    def post(self, request, key=None):
        from reports.models import ReportDefinition
        from reports.services.designer import (validate_definition,
                                              register_definition,
                                              DefinitionError)
        from django.utils.text import slugify
        data = request.POST
        try:
            sections = json.loads(data.get("sections") or "[]")
            filters = json.loads(data.get("filters") or "[]")
        except json.JSONDecodeError as e:
            messages.error(request, f"Invalid JSON: {e}")
            return redirect(request.path)

        new_key = key or slugify(data.get("key") or data.get("title") or "")[:100]
        if not new_key:
            messages.error(request, "A key or title is required.")
            return redirect(reverse("designer_new"))

        definition = (ReportDefinition.objects.filter(key=key).first()
                      if key else ReportDefinition(key=new_key))
        if definition is None:
            definition = ReportDefinition(key=new_key)
        definition.title = data.get("title") or definition.title or new_key
        definition.description = data.get("description", "")
        definition.category = data.get("category") or "Custom"
        definition.permission = data.get("permission") or "reports"
        definition.enabled = data.get("enabled") == "on"
        definition.sections = sections
        definition.filters = filters
        if definition.pk:
            definition.bump_version()
        if not definition.owner_id:
            definition.owner = request.user

        # validate before saving so an invalid config can't be persisted-live
        problems = validate_definition(definition)
        if problems:
            messages.error(request, "Cannot save: " + "; ".join(problems))
            return render(request, self.template_name, {
                "definition": definition, "palette": self._palette(),
                "sections_json": data.get("sections") or "[]",
                "filters_json": data.get("filters") or "[]"})
        definition.save()
        if definition.enabled:
            try:
                register_definition(definition)
            except DefinitionError as e:
                messages.warning(request, f"Saved but not live: {e}")
        messages.success(request, f"Report '{definition.title}' saved.")
        return redirect(reverse("designer_edit", args=[definition.key]))


class DesignerDuplicateView(TreasurerRequiredMixin, View):
    def post(self, request, key):
        from reports.models import ReportDefinition
        from django.utils.text import slugify
        src = get_object_or_404(ReportDefinition, key=key)
        new_key = slugify(f"{src.key}-copy")[:100]
        i = 1
        while ReportDefinition.objects.filter(key=new_key).exists():
            i += 1
            new_key = slugify(f"{src.key}-copy-{i}")[:100]
        ReportDefinition.objects.create(
            key=new_key, title=f"{src.title} (copy)", description=src.description,
            category=src.category, sections=src.sections, filters=src.filters,
            page_settings=src.page_settings, permission=src.permission,
            enabled=False, owner=request.user, tags=src.tags)
        messages.success(request, "Report duplicated (disabled until you enable it).")
        return redirect(reverse("designer_edit", args=[new_key]))


class DesignerDeleteView(TreasurerRequiredMixin, View):
    def post(self, request, key):
        from reports.models import ReportDefinition
        from core.reporting import registry
        d = get_object_or_404(ReportDefinition, key=key)
        registry._reports.pop(d.engine_key, None)
        d.delete()
        messages.success(request, "Report definition deleted.")
        return redirect(reverse("designer_list"))


# ===========================================================================
# Schedules
# ===========================================================================

class ScheduleListView(TreasurerRequiredMixin, TemplateView):
    template_name = "reports/schedule_list.html"

    def get_context_data(self, **kwargs):
        from reports.models import ReportSchedule
        from core.reporting import registry
        ctx = super().get_context_data(**kwargs)
        ctx["schedules"] = ReportSchedule.objects.all()
        ctx["reports"] = registry.all()
        ctx["frequencies"] = ReportSchedule.Frequency.choices
        ctx["periods"] = ReportSchedule.PERIOD_CHOICES
        return ctx

    def post(self, request):
        from reports.models import ReportSchedule
        s = ReportSchedule.objects.create(
            name=request.POST.get("name") or "Untitled schedule",
            report_key=request.POST.get("report_key"),
            frequency=request.POST.get("frequency") or "MONTHLY",
            period_policy=request.POST.get("period_policy") or "prev_month",
            formats=[f for f in request.POST.getlist("formats")] or ["csv"],
            recipients=[r.strip() for r in
                        (request.POST.get("recipients") or "").split(",") if r.strip()],
            created_by=request.user)
        s.next_run = s.compute_next_run()
        s.save(update_fields=["next_run"])
        messages.success(request, f"Schedule '{s.name}' created.")
        return redirect(reverse("schedule_list"))


class ScheduleRunView(TreasurerRequiredMixin, View):
    def post(self, request, pk):
        from reports.models import ReportSchedule
        from reports.services.scheduling import execute_schedule
        sched = get_object_or_404(ReportSchedule, pk=pk)
        run = execute_schedule(sched, user=request.user)
        if run.status == "SUCCESS":
            messages.success(request, f"Ran '{sched.name}': {run.detail}")
        else:
            messages.error(request, f"Run failed: {run.detail}")
        return redirect(reverse("schedule_list"))


class ScheduleToggleView(TreasurerRequiredMixin, View):
    def post(self, request, pk):
        from reports.models import ReportSchedule
        sched = get_object_or_404(ReportSchedule, pk=pk)
        sched.enabled = not sched.enabled
        sched.save(update_fields=["enabled"])
        return redirect(reverse("schedule_list"))


# ===========================================================================
# Snapshot history / versioning
# ===========================================================================

class SnapshotHistoryView(ReportAccessMixin, TemplateView):
    template_name = "reports/snapshot_history.html"

    def get_context_data(self, **kwargs):
        from reports.models import ReportSnapshot
        ctx = super().get_context_data(**kwargs)
        key = self.request.GET.get("report_key")
        qs = ReportSnapshot.objects.all()
        if key:
            qs = qs.filter(report_key=key)
        ctx["snapshots"] = qs[:100]
        ctx["report_key"] = key
        ctx["report_keys"] = list(ReportSnapshot.objects.values_list(
            "report_key", flat=True).distinct())
        return ctx


class SnapshotCompareView(ReportAccessMixin, TemplateView):
    """Compare two snapshots' payloads section-by-section — the versioning diff."""
    template_name = "reports/snapshot_compare.html"

    def get_context_data(self, **kwargs):
        from reports.models import ReportSnapshot
        ctx = super().get_context_data(**kwargs)
        a = get_object_or_404(ReportSnapshot, pk=kwargs["a"])
        b = get_object_or_404(ReportSnapshot, pk=kwargs["b"])
        ctx["a"], ctx["b"] = a, b
        ctx["diff"] = self._diff(a, b)
        return ctx

    @staticmethod
    def _diff(a, b):
        """Section-title-keyed comparison of the two payloads' totals/rows."""
        def _index(snap):
            return {s["title"]: s for s in snap.payload.get("sections", [])}
        ia, ib = _index(a), _index(b)
        rows = []
        for title in sorted(set(ia) | set(ib)):
            sa, sb = ia.get(title), ib.get(title)
            status = ("unchanged" if sa == sb else
                      "added" if sa is None else
                      "removed" if sb is None else "changed")
            rows.append({"title": title, "status": status})
        return rows
