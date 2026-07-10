"""Reusable report components + the Component Registry.

A *component* is the reusable building block every report is assembled from — a
KPI band, a fund summary table, a financial statement, a chart, a commentary
panel, a signature block, and so on. Components are ``Section`` subclasses (so
they slot straight into the existing Generic Report Engine and its render
pipeline) enriched with three things this phase adds:

1. **Layout metadata** (``LayoutMeta``) — width/order/priority/visibility/… so a
   future Report Designer can place and configure them.
2. **Dependency tracking** — each component declares the metrics it consumes and
   records what it actually used, feeding the Financial Dependency Map.
3. **Registration** — components register in the ``ComponentRegistry`` so new
   ones (including from future modules like Payroll or Inventory) are added by
   registration, never by editing the engine.

Every component draws its figures **only** from the ``ReportContext`` (the
Semantic Reporting Layer), never from models or raw aggregates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from core.reporting.engine import Column, Row, Section, SectionData
from core.reporting.layout import LayoutMeta


class ComponentSection(Section):
    """Base class for a reusable, layout-aware, dependency-tracking component.

    Subclasses implement ``render(ctx, filters) -> SectionData`` (note: not
    ``build`` — the base ``build`` wraps ``render`` to attach layout metadata
    and record metric provenance automatically). Declare the metrics a component
    consumes in ``declared_metrics`` for static impact analysis; actual usage is
    still captured at render time from the context.
    """
    #: metrics this component is expected to consume (impact analysis)
    declared_metrics: tuple = ()

    def __init__(self, key=None, title=None, layout: Optional[LayoutMeta] = None,
                 permission: Optional[Callable] = None):
        super().__init__(key=key, title=title)
        self.layout = layout or LayoutMeta()
        if permission is not None:
            self.permission = permission

    # subclasses implement this ------------------------------------------------
    def render(self, ctx, filters) -> SectionData:  # pragma: no cover
        raise NotImplementedError

    # the engine calls build(); we wrap render() to add metadata + provenance --
    def build(self, ctx, filters) -> SectionData:
        before = set(ctx.metrics_used())
        data = self.render(ctx, filters)
        if data is None:
            return None
        after = ctx.metrics_used()
        used = [m for m in after if m not in before]
        # merge declared metrics that were used (in case a metric was already
        # in the context cache from an earlier component and so didn't re-appear
        # in the delta) — declared ∩ this-render is the safe union
        for m in self.declared_metrics:
            if m not in used:
                used.append(m)
        data.extra.setdefault("metrics_used", used)
        data.extra.setdefault("layout", self.layout.as_dict())
        return data

    # convenience builders reused by many components ---------------------------
    @staticmethod
    def money_table(key, title, columns, rows, total=None, note=""):
        return SectionData(key=key, title=title, columns=columns, rows=rows,
                           total=total, note=note, kind="table")


class ComponentRegistry:
    """Registry of reusable component *classes* (or factories), keyed by a
    stable component key, so reports can compose by name and future modules can
    contribute components without touching the engine.

    A registered entry is a zero/keyword-arg factory returning a
    ``ComponentSection`` instance, so the same component can be instantiated with
    different layout/permission per report.
    """
    def __init__(self):
        self._factories: dict[str, Callable] = {}
        self._meta: dict[str, dict] = {}

    def register(self, key, factory, *, label="", category="General",
                 description="", designer_safe=True, params_schema=None):
        """Register a component factory.

        ``designer_safe`` (default True): whether the Report Designer's visual
        builder may offer this component. A component whose factory requires a
        Python object that can't round-trip through JSON (e.g. ``ChartComponent``
        needs a ``spec_fn`` callable) must be registered with
        ``designer_safe=False`` — the designer's palette omits it, and
        ``validate_definition`` refuses a saved definition that references it,
        so a callable-only component can never reach the designer's JSON path
        and crash at render time.

        ``params_schema``: an optional list of ``{"name", "label", "kind",
        "required", "source"}`` dicts describing the extra parameters this
        component takes (e.g. the ``narrative`` component's ``narrative_key``),
        so the designer can render real form fields instead of asking an
        administrator to hand-type a JSON ``params`` object. ``kind`` is one of
        ``text``, ``textarea``, ``select`` (options come from ``source``, e.g.
        ``"narratives"``), or ``number``. Components with no schema (the large
        majority — they read straight from ``ReportContext``/filters) simply
        show no params fields.
        """
        if key in self._factories:
            raise ValueError(f"Component '{key}' already registered.")
        self._factories[key] = factory
        self._meta[key] = {"label": label or key, "category": category,
                           "description": description,
                           "designer_safe": designer_safe,
                           "params_schema": params_schema or []}
        return factory

    def create(self, key, **kwargs):
        if key not in self._factories:
            raise KeyError(f"No component '{key}'. Known: "
                           f"{', '.join(sorted(self._factories))}.")
        return self._factories[key](**kwargs)

    def has(self, key):
        return key in self._factories

    def is_designer_safe(self, key):
        meta = self._meta.get(key)
        return bool(meta and meta.get("designer_safe", True))

    def all(self):
        return sorted(self._meta.items(),
                      key=lambda kv: (kv[1]["category"], kv[1]["label"]))

    def by_category(self, designer_safe_only=False):
        out: dict[str, list] = {}
        for key, meta in self.all():
            if designer_safe_only and not meta.get("designer_safe", True):
                continue
            out.setdefault(meta["category"], []).append({"key": key, **meta})
        return out


component_registry = ComponentRegistry()
