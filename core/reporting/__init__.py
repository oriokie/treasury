"""Semantic Reporting Layer + Generic Report Engine.

Public API::

    from core.reporting import ReportContext          # the semantic layer
    from core.reporting import (Report, Section, FunctionSection,
                                SectionData, Column, Row, Filter,
                                registry, RenderedReport)

The Semantic Reporting Layer (``ReportContext``) is the sole interface through
which reports, dashboards, widgets, exports and AI features should obtain
financial data — it draws every figure from the Financial Metrics Registry
(``core.metrics``) and memoizes per render. The Generic Report Engine builds
composable, registered reports on top of that layer.
"""
from core.reporting.context import ReportContext
from core.reporting.engine import (
    Column,
    Filter,
    FunctionSection,
    PermissionDenied_,
    RenderedReport,
    Report,
    ReportRegistry,
    Row,
    Section,
    SectionData,
    registry,
)
from core.reporting.layout import LayoutMeta
from core.reporting.components import ComponentSection, component_registry
from core.reporting.charts import ChartEngine, ChartSpec
from core.reporting.renderers import Renderer, renderer_registry
from core.reporting.dependencies import (DependencyMap, ComponentDependency,
                                        build_dependency_map, impact_of_metric)
from core.reporting.narrative import (Narrative, NarrativeEngine,
                                      NarrativeConfig, NarrativeResult,
                                      Thresholds, Finding, Severity, Style, Tone,
                                      narrative_registry)

__all__ = [
    "ReportContext",
    "Report",
    "Section",
    "FunctionSection",
    "SectionData",
    "Column",
    "Row",
    "Filter",
    "RenderedReport",
    "ReportRegistry",
    "PermissionDenied_",
    "registry",
    # component library
    "ComponentSection",
    "component_registry",
    "LayoutMeta",
    # charts
    "ChartEngine",
    "ChartSpec",
    # renderers
    "Renderer",
    "renderer_registry",
    # dependency map
    "DependencyMap",
    "ComponentDependency",
    "build_dependency_map",
    "impact_of_metric",
    # narrative engine
    "Narrative",
    "NarrativeEngine",
    "NarrativeConfig",
    "NarrativeResult",
    "Thresholds",
    "Finding",
    "Severity",
    "Style",
    "Tone",
    "narrative_registry",
]
