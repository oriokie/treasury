"""Layout metadata for report components.

Every component carries a ``LayoutMeta`` describing how it should be placed and
behave in a report — width, order, grouping, visibility, collapse state,
responsive and print behaviour, and export visibility. None of this is consumed
by a UI yet; it exists so the renderers can honour placement and so a future
drag-and-drop Report Designer has a complete, declarative layout model to read
and write without any further schema work.

The metadata is deliberately presentation-only: it never affects *what* a
component computes (that comes from the Semantic Reporting Layer), only *how*
and *whether* it is shown in a given medium.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LayoutMeta:
    """Placement and behaviour metadata for a component within a report.

    Widths are on a 12-column grid (so ``width=6`` is half-width, ``12`` full).
    ``order`` sorts components within a report; ``priority`` lets a responsive
    or summarised view keep only the most important components (higher = more
    important). ``group`` lets components be visually clustered (e.g. all
    "Financial statements" together). The remaining flags govern per-medium
    behaviour so one component definition renders sensibly everywhere.
    """
    width: int = 12                 # 1..12 grid columns
    order: int = 100                # sort order within a report (asc)
    priority: int = 50              # 0..100; higher survives summarised views
    group: str = ""                 # optional visual/logical cluster
    collapsed: bool = False         # start collapsed (HTML)
    collapsible: bool = True        # may the user collapse it
    hide_if_empty: bool = True      # drop the component when it has no data

    # responsive behaviour (hints for the HTML renderer / future designer)
    responsive: str = "stack"       # stack | scroll | hide-mobile
    # print behaviour
    print_visible: bool = True
    page_break_before: bool = False
    # export behaviour
    export_visible: bool = True     # include in CSV/Excel/PDF/Word exports
    export_formats: tuple = ()      # empty = all; else restrict, e.g. ("pdf",)

    # free-form extension point for the future designer (colours, pinned, …)
    extra: dict = field(default_factory=dict)

    def visible_in(self, medium: str) -> bool:
        """Whether this component should appear in a given render medium.
        ``medium`` is one of: html, print, csv, xlsx, pdf, docx."""
        if medium == "print":
            return self.print_visible
        if medium in ("csv", "xlsx", "pdf", "docx"):
            if not self.export_visible:
                return False
            return not self.export_formats or medium in self.export_formats
        return True   # html always eligible (empty handled separately)

    def as_dict(self) -> dict:
        """Serialisable form — what a future Report Designer would persist."""
        return {
            "width": self.width, "order": self.order, "priority": self.priority,
            "group": self.group, "collapsed": self.collapsed,
            "collapsible": self.collapsible, "hide_if_empty": self.hide_if_empty,
            "responsive": self.responsive, "print_visible": self.print_visible,
            "page_break_before": self.page_break_before,
            "export_visible": self.export_visible,
            "export_formats": list(self.export_formats), "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LayoutMeta":
        data = dict(data or {})
        if "export_formats" in data:
            data["export_formats"] = tuple(data["export_formats"])
        return cls(**data)
