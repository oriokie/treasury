"""Financial Dependency Map.

Extends the Semantic Reporting Layer's ``metrics_used()`` into a full dependency
graph: for a report (or a single component), which metrics it consumed, and —
through the Metrics Registry's metadata — which Semantic Reporting services and
underlying accounting implementations those metrics resolve to.

The map is derived, never hand-maintained: it is produced from what actually ran
during a render (the ``ReportContext``'s recorded usage) plus the registry's
declared ``authoritative`` implementation per metric. That makes it useful for:

* **Documentation** — an always-accurate "what feeds this report" listing.
* **Debugging / auditing** — trace a figure back to its accounting service.
* **Impact analysis** — "if I change ``balances.department_summary``, which
  reports/components are affected?" (reverse lookup).
* **Performance** — see which metrics a report actually pulled.
* **Future AI reasoning** — a machine-readable provenance graph.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.metrics import metrics


@dataclass
class ComponentDependency:
    """What one component consumed during a render."""
    component: str
    title: str
    metrics: list = field(default_factory=list)     # metric keys

    def services(self):
        """The authoritative implementations behind this component's metrics,
        read from the registry metadata (e.g. ``reports.services.balances`` for
        several of them)."""
        out = []
        for m in self.metrics:
            meta = metrics.registry.get(m)
            if meta and meta.authoritative not in out:
                out.append(meta.authoritative)
        return out

    def as_dict(self):
        rows = []
        for m in self.metrics:
            meta = metrics.registry.get(m)
            rows.append({
                "metric": m,
                "label": getattr(meta, "label", m),
                "category": getattr(meta, "category", ""),
                "authoritative": getattr(meta, "authoritative", ""),
            })
        return {"component": self.component, "title": self.title,
                "metrics": rows, "services": self.services()}


@dataclass
class DependencyMap:
    """The dependency graph for one rendered report."""
    report_key: str
    report_title: str
    components: list = field(default_factory=list)   # list[ComponentDependency]

    def all_metrics(self):
        seen = []
        for c in self.components:
            for m in c.metrics:
                if m not in seen:
                    seen.append(m)
        return seen

    def all_services(self):
        seen = []
        for c in self.components:
            for s in c.services():
                if s not in seen:
                    seen.append(s)
        return seen

    def metric_to_components(self):
        """Reverse index: metric key -> [component keys that used it]. This is
        the impact-analysis view ("who depends on this metric?")."""
        out: dict[str, list] = {}
        for c in self.components:
            for m in c.metrics:
                out.setdefault(m, [])
                if c.component not in out[m]:
                    out[m].append(c.component)
        return out

    def as_dict(self):
        return {
            "report": self.report_key,
            "title": self.report_title,
            "metrics": self.all_metrics(),
            "services": self.all_services(),
            "components": [c.as_dict() for c in self.components],
            "metric_to_components": self.metric_to_components(),
        }


def build_dependency_map(rendered) -> DependencyMap:
    """Build a DependencyMap from a RenderedReport.

    Each ``SectionData`` records the metrics its component consumed in
    ``extra["metrics_used"]`` (populated by ``ComponentSection``); we assemble
    those into the graph. Sections that predate component instrumentation simply
    contribute no metrics, so the map degrades gracefully.
    """
    dm = DependencyMap(report_key=rendered.report.key,
                       report_title=rendered.report.title)
    for s in rendered.sections:
        used = list(s.extra.get("metrics_used", []))
        dm.components.append(ComponentDependency(
            component=s.key, title=s.title, metrics=used))
    return dm


def impact_of_metric(metric_key, reports):
    """Cross-report impact analysis: given a metric key and an iterable of
    Report objects, return the report/component paths that would be affected if
    that metric (or its underlying service) changed. Rendering is not required —
    this inspects declared component metric dependencies where available.
    """
    hits = []
    for report in reports:
        for section in report.sections:
            declared = getattr(section, "declared_metrics", None)
            if declared and metric_key in declared:
                hits.append((report.key, section.key))
    return hits
