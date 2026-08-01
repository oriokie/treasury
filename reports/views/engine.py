"""Split from reports/views.py (P1-2). Behaviour identical; the
package __init__ reproduces the original module namespace."""
from django.views.generic import TemplateView
from core.permissions import (ReportAccessMixin, TreasurerRequiredMixin,
                              RightRequiredMixin, ReportAccessMixin)
from core.models import SiteConfig


class MetricsCatalogueView(ReportAccessMixin, TemplateView):
    """The Financial Metrics Registry catalogue — every named financial
    calculation the system recognises, its accounting definition and its
    authoritative implementation. This is the human view of the Semantic
    Reporting Layer: new reports should source figures from these metrics
    rather than re-deriving accounting logic."""
    template_name = "reports/metrics_catalogue.html"

    def get_context_data(self, **kwargs):
        from core.metrics import metrics
        ctx = super().get_context_data(**kwargs)
        ctx["by_category"] = metrics.by_category()
        ctx["count"] = len(metrics.registry)
        return ctx

class EngineReportView(ReportAccessMixin, TemplateView):
    """Generic view for any report registered on the Generic Report Engine.

    It is a thin adapter: it looks the report up by key, runs the engine's
    render pipeline (which enforces the report's own permission, resolves
    filters and builds ONE shared ReportContext), then hands the result to a
    renderer chosen from the Renderer Registry (?export=csv|xlsx|pdf|docx, or
    ?print=1). HTML is the default. New engine reports need only be registered;
    they get a URL, HTML, filters, drill-down, dependency map and every
    registered export format for free — no per-report or per-format code.
    """
    template_name = "reports/engine_report.html"

    def get(self, request, key):
        from django.core.exceptions import PermissionDenied
        from django.http import Http404, JsonResponse
        from core.reporting import (registry, renderer_registry,
                                    PermissionDenied_, build_dependency_map)
        report = registry.get(key)
        if report is None and key.startswith("def__"):
            # a designed report not yet registered this process — compile it now
            report = self._resolve_definition(key)
        if report is None:
            raise Http404("Unknown report")
        import time as _time
        _t0 = _time.monotonic()
        try:
            rendered = report.render(request)
        except PermissionDenied_:
            raise PermissionDenied
        _render_ms = int((_time.monotonic() - _t0) * 1000)
        self._record_usage(request, key, _render_ms)
        self._annotate_narrative(rendered, key)

        # dependency map endpoint (documentation / debugging / impact analysis)
        if request.GET.get("deps") == "json":
            return JsonResponse(build_dependency_map(rendered).as_dict())

        church = SiteConfig.get().church_name
        export = request.GET.get("export")
        fmt = export if export in ("csv", "xlsx", "pdf", "docx") else None
        if request.GET.get("print") == "1":
            fmt = "print"
        if fmt:
            renderer = renderer_registry.get(fmt)
            if renderer is not None:
                out = renderer.render(rendered, church=church, request=request)
                if isinstance(out, dict):        # print renderer returns ctx
                    out["querystring"] = request.GET.urlencode()
                    out["dep_map"] = build_dependency_map(rendered)
                    out["chart_json"] = self._chart_json(rendered)
                    out.update(self._grouped_context(rendered))
                    return self.render_to_response(
                        out, template=self._template_for(report))
                return out

        return self.render_to_response({
            "report": report,
            "rendered": rendered,
            "sections": rendered.sections,
            "filters": rendered.filters,
            "querystring": request.GET.urlencode(),
            "dep_map": build_dependency_map(rendered),
            "chart_json": self._chart_json(rendered),
            "is_favourite": self._is_favourite(request, key),
            **self._grouped_context(rendered),
        }, template=self._template_for(report))

    def render_to_response(self, context, template=None, **kwargs):
        if template:
            self.template_name = template
        return super().render_to_response(context, **kwargs)

    @staticmethod
    def _template_for(report):
        """A report may declare its own presentation template; otherwise the
        generic engine template is used. Section data is identical either way."""
        return getattr(report, "html_template", None) or \
            "reports/engine_report.html"

    @staticmethod
    def _grouped_context(rendered):
        """Group the rendered sections by their ``LayoutMeta.group`` in layout
        order, so a purpose-built template (e.g. the board pack) can render
        grouped, navigable sections. The generic template ignores this and reads
        the flat ``sections`` list, so this is additive and backward-compatible.
        """
        from core.reporting.layout import LayoutMeta
        groups = []
        index = {}
        for s in rendered.sections:
            layout_raw = s.extra.get("layout")
            layout = LayoutMeta.from_dict(layout_raw) if layout_raw else LayoutMeta()
            name = layout.group or "Report"
            if name not in index:
                index[name] = {"name": name, "sections": [], "order": layout.order}
                groups.append(index[name])
            index[name]["sections"].append(s)
            index[name]["order"] = min(index[name]["order"], layout.order)
        groups.sort(key=lambda g: g["order"])
        # a stable anchor id per group for the table of contents
        import re as _re
        break_groups = set()
        for g in groups:
            g["anchor"] = "grp-" + _re.sub(r"[^a-z0-9]+", "-", g["name"].lower()).strip("-")
        # groups whose first section requests a page break -> break before the group
        for s in rendered.sections:
            layout_raw = s.extra.get("layout")
            if not layout_raw:
                continue
            layout = LayoutMeta.from_dict(layout_raw)
            if layout.page_break_before and layout.group:
                break_groups.add(layout.group)
        # drop a break on the very first group (nothing to break from)
        if groups:
            break_groups.discard(groups[0]["name"])

        out = {"section_groups": groups, "break_groups": break_groups}
        if getattr(rendered.report, "html_template", None):
            out.update(EngineReportView._cover_health(rendered))
        return out

    @staticmethod
    def _annotate_narrative(rendered, key):
        """Give every section its explanation — generated from the section's own
        figures, or the treasurer's stored wording where they have edited it.
        Purely additive: a report that ignores the annotation renders exactly as
        it did before, and a failure here never costs the reader the report."""
        try:
            from reports.services import narratives
            narratives.annotate(rendered, key)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _cover_health(rendered):
        """Compute the health-score band shown on the board-pack cover, reusing
        the shared ReportContext (so its metrics are already memoized). Purely
        presentational; never blocks a render if intelligence is unavailable."""
        try:
            from core.intelligence import compute_health_score
            hs = compute_health_score(rendered.context)
            tone = "good" if hs.overall >= 75 else ("warn" if hs.overall >= 55
                                                    else "bad")
            return {"health_overall": hs.overall, "health_band": hs.band,
                    "health_tone": tone}
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def _resolve_definition(key):
        """Compile and register a designed report on demand (lazy) so its URL
        works without a startup DB query."""
        try:
            from reports.models import ReportDefinition
            from reports.services.designer import register_definition, DefinitionError
            d = ReportDefinition.objects.filter(
                key=key[len("def__"):], enabled=True).first()
            if d is None:
                return None
            return register_definition(d)
        except (DefinitionError, Exception):  # noqa: BLE001
            return None

    @staticmethod
    def _is_favourite(request, key):
        try:
            from reports.models import ReportFavourite
            return ReportFavourite.objects.filter(
                report_key=key, user=request.user).exists()
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _record_usage(request, key, render_ms):
        try:
            from reports.models import ReportUsage
            ReportUsage.objects.create(
                report_key=key,
                user=request.user if request.user.is_authenticated else None,
                render_ms=render_ms,
                export_format=request.GET.get("export", ""))
        except Exception:  # noqa: BLE001 — usage tracking must never break a report
            pass

    @staticmethod
    def _chart_json(rendered):
        """Collect chart sections into a {section_key: chartjs_config} JSON blob
        the template feeds to Chart.js — components stay render-agnostic; the
        view marshals their specs for the HTML medium."""
        import json
        charts = {}
        for s in rendered.sections:
            if s.kind == "chart" and s.extra.get("chart"):
                charts[s.key] = s.extra["chart"]
        return json.dumps(charts) if charts else ""

class ComponentCatalogueView(ReportAccessMixin, TemplateView):
    """The report component library catalogue — every reusable component the
    Generic Report Engine offers, grouped by category, plus the registered
    engine reports and the available render formats. The human view of the
    component-based reporting architecture: new reports are composed from these
    components, all fed by the Semantic Reporting Layer."""
    template_name = "reports/component_catalogue.html"

    def get_context_data(self, **kwargs):
        from core.reporting import (component_registry, registry,
                                    renderer_registry)
        ctx = super().get_context_data(**kwargs)
        ctx["by_category"] = component_registry.by_category()
        ctx["component_count"] = len(component_registry._factories)
        ctx["reports"] = registry.all()
        ctx["formats"] = [(r.fmt, r.label) for r in renderer_registry.all()]
        return ctx
