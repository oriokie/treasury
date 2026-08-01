"""Generic Report Engine — a reusable, component-based framework for building
reports out of registered, composable *sections*, each of which draws its
figures from the Semantic Reporting Layer (``ReportContext``) and therefore the
Financial Metrics Registry.

Nothing existing is redesigned by this module. It is *new infrastructure* that
future reports (starting with the Board Report, next phase) will opt into. The
current hand-written report views keep working untouched; this engine simply
gives the next generation of reports a single place to declare structure,
permissions, filters, drill-down and exports without re-deriving accounting
logic or re-implementing rendering/export plumbing.

Concepts
--------
* **Section** — the reusable unit. A section knows how to turn a
  ``ReportContext`` (+ resolved filter values) into a ``SectionData``: an
  ordered list of columns and rows, an optional total row, a plain-language
  note, and optional per-row drill-down links. Sections are meant to be shared
  across reports (e.g. a "Fund balances" section usable by several reports).
* **Report** — a registered composition: a key, a title, a required permission,
  an ordered list of sections, and the filters it accepts. Rendering a report
  builds ONE ``ReportContext`` and feeds it to every section, so shared metrics
  compute once (recommendation #1, generalised to every report on the engine).
* **Filters** — declarative (name, label, kind, default). The engine resolves
  them from the request once and hands the values to sections.
* **Permissions** — each report names a check; the engine enforces it before
  building anything.
* **Exports** — a report renders to HTML (structured data for a template) or to
  CSV / Excel via the existing ``reports.exports`` helpers, with no per-report
  export code.
* **Drill-down** — a column may be marked a drill-down anchor; a section may
  attach a URL per row. The template renders these as links; nothing is
  hard-coded.

The engine is intentionally small and dependency-light: it orchestrates the
Semantic Reporting Layer and the export helpers, and stays out of accounting.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Optional

from core.reporting.context import ReportContext


# ===========================================================================
# Section data model
# ===========================================================================

@dataclass
class Column:
    key: str
    label: str
    numeric: bool = False           # right-align + number formatting in exports
    drilldown: bool = False         # render this cell as a link if row has a url
    #: decimal places for a numeric column. Statements are read to the cent;
    #: a summary of the year's collections is read to the shilling, and the
    #: trailing ".00" on every line is noise a board has to look past. Default
    #: 2 leaves every existing column exactly as it was.
    places: int = 2


@dataclass
class Row:
    cells: dict                     # column key -> value
    url: Optional[str] = None       # drill-down target for this row
    emphasis: bool = False          # e.g. subtotal / highlighted row
    meta: dict = field(default_factory=dict)


@dataclass
class SectionData:
    """The rendered output of a section: columns + rows (+ optional total and
    note). Format-agnostic — the HTML template and the CSV/Excel exporters all
    read the same structure."""
    key: str
    title: str
    columns: list                   # list[Column]
    rows: list                      # list[Row]
    total: Optional[Row] = None
    note: str = ""
    kind: str = "table"             # table | keyvalue | chart | html
    extra: dict = field(default_factory=dict)   # chart json, custom html, …

    def is_empty(self):
        return not self.rows and self.total is None


# ===========================================================================
# Section — the reusable component
# ===========================================================================

class Section:
    """Base class for a reusable report section.

    Subclass and implement ``build(ctx, filters) -> SectionData``. ``ctx`` is
    the shared ``ReportContext`` (draw every figure from it — do not import
    services or write raw aggregates), ``filters`` is a dict of resolved filter
    values. Set ``permission`` to restrict a section beyond its report, or
    leave it None to inherit the report's permission.
    """
    key: str = ""
    title: str = ""
    permission: Optional[Callable] = None   # optional extra gate

    def __init__(self, key=None, title=None):
        if key:
            self.key = key
        if title:
            self.title = title

    def visible_to(self, user):
        return self.permission is None or self.permission(user)

    def build(self, ctx: ReportContext, filters: dict) -> SectionData:  # pragma: no cover
        raise NotImplementedError

    # helpers for subclasses --------------------------------------------------

    @staticmethod
    def table(key, title, columns, rows, total=None, note=""):
        return SectionData(key=key, title=title, columns=columns, rows=rows,
                           total=total, note=note, kind="table")

    @staticmethod
    def keyvalue(key, title, pairs, note=""):
        """A simple label/value section (e.g. an income statement).

        ``pairs`` is a list of ``(label, value)``, ``(label, value, emphasis)``
        or ``(label, value, level)`` tuples, where ``level`` is one of:

        ``"heading"``   a group heading such as ASSETS — no figure of its own
        ``"subtotal"``  a total of the lines above it, e.g. Total liabilities
        ``"grand"``     the figure the statement exists to give, e.g. Net assets

        A bare ``True`` still means "emphasise this" and is treated as a
        subtotal, so every existing caller keeps working unchanged. The levels
        exist because a statement that renders a heading, a subtotal and the
        bottom line identically leaves the reader to work out which is which —
        which is the whole complaint about the statement of financial position.
        """
        cols = [Column("label", "", numeric=False),
                Column("value", "", numeric=True)]
        rows = []
        for pair in pairs:
            label, value = pair[0], pair[1]
            mark = pair[2] if len(pair) > 2 else False
            level = mark if isinstance(mark, str) else ("subtotal" if mark else "")
            rows.append(Row(cells={"label": label, "value": value},
                            emphasis=bool(level),
                            meta={"level": level} if level else {}))
        return SectionData(key=key, title=title, columns=cols, rows=rows,
                           note=note, kind="keyvalue")


class FunctionSection(Section):
    """Wrap a plain ``build(ctx, filters)`` callable as a section, so simple
    sections don't need a subclass."""
    def __init__(self, key, title, fn, permission=None):
        super().__init__(key=key, title=title)
        self._fn = fn
        self.permission = permission

    def build(self, ctx, filters):
        return self._fn(ctx, filters)


# ===========================================================================
# Filters
# ===========================================================================

@dataclass
class Filter:
    name: str
    label: str
    kind: str = "text"              # text | date | month | choice | fund | bool
    default: Any = None
    choices: Optional[list] = None  # for kind="choice": list[(value,label)]

    def resolve(self, request):
        raw = request.GET.get(self.name)
        if raw is None or raw == "":
            return self.default
        if self.kind == "date":
            try:
                return _dt.date.fromisoformat(raw)
            except ValueError:
                return self.default
        if self.kind == "bool":
            return raw.lower() in ("1", "true", "yes", "on")
        return raw


# ===========================================================================
# Report — a registered composition
# ===========================================================================

@dataclass
class Report:
    key: str
    title: str
    sections: list                          # list[Section]
    permission: Callable = lambda u: True   # required to view
    filters: list = field(default_factory=list)     # list[Filter]
    description: str = ""
    category: str = "General"
    period_from_request: bool = True        # build ctx from ?start/?end
    #: optional dedicated HTML/print template. When None the generic
    #: ``reports/engine_report.html`` is used. A report may opt into a richer,
    #: purpose-built presentation (e.g. the Treasurer's board pack) without the
    #: engine or any other report changing — the section data is identical, only
    #: the presentation template differs.
    html_template: Optional[str] = None

    # ---- rendering pipeline ----

    def resolve_filters(self, request):
        return {f.name: f.resolve(request) for f in self.filters}

    def build_context(self, request):
        if self.period_from_request:
            return ReportContext.from_request(request, label=self.title)
        return ReportContext(label=self.title)

    def render(self, request):
        """Run the pipeline: check permission → resolve filters → build ONE
        shared context → build each visible section. Returns a RenderedReport.
        Permission is enforced here; the view is a thin wrapper."""
        if not self.permission(request.user):
            raise PermissionDenied_(self.key)
        filters = self.resolve_filters(request)
        ctx = self.build_context(request)
        sections = []
        for section in self.sections:
            if not section.visible_to(request.user):
                continue
            data = section.build(ctx, filters)
            if data is None:
                continue
            # a section may render as one SectionData or several (a KPI band +
            # chart + table); flatten either into the ordered section list
            if isinstance(data, (list, tuple)):
                sections.extend(d for d in data if d is not None)
            else:
                sections.append(data)
        return RenderedReport(report=self, context=ctx, filters=filters,
                              sections=sections)


class PermissionDenied_(Exception):
    """Raised by the engine when a user lacks a report's permission; the view
    translates it to Django's PermissionDenied (kept separate so the engine has
    no Django-view dependency)."""
    pass


@dataclass
class RenderedReport:
    report: Report
    context: ReportContext
    filters: dict
    sections: list                          # list[SectionData]

    # ---- exports (build on the existing reports.exports helpers) ----

    def _flat_rows(self):
        """Flatten every table/keyvalue section into (section-title, header,
        rows) blocks for CSV/Excel. Charts/html sections are skipped in flat
        exports (they have no tabular form)."""
        blocks = []
        for s in self.sections:
            if s.kind not in ("table", "keyvalue"):
                continue
            header = [c.label for c in s.columns]
            rows = []
            for r in s.rows:
                rows.append([r.cells.get(c.key, "") for c in s.columns])
            if s.total is not None:
                rows.append([s.total.cells.get(c.key, "") for c in s.columns])
            blocks.append((s.title, header, rows))
        return blocks

    def to_csv(self, church=None):
        """One CSV with each section stacked (title row, header, rows, blank)."""
        from reports.exports import csv_response
        header = ["", ""]
        combined = []
        # find the widest section to size the header
        width = max((len(h) for _, h, _ in self._flat_rows()), default=2)
        header = [""] * width
        for title, hdr, rows in self._flat_rows():
            combined.append([title] + [""] * (width - 1))
            combined.append((hdr + [""] * width)[:width])
            for r in rows:
                combined.append((list(r) + [""] * width)[:width])
            combined.append([""] * width)
        return csv_response(f"{self.report.key}.csv", header, combined)

    def to_xlsx(self, church=None):
        """One styled workbook, sections stacked on a single sheet."""
        from reports.exports import xlsx_response
        width = max((len(h) for _, h, _ in self._flat_rows()), default=2)
        header = [""] * width
        combined = []
        for title, hdr, rows in self._flat_rows():
            combined.append([title] + [""] * (width - 1))
            combined.append((hdr + [""] * width)[:width])
            for r in rows:
                combined.append((list(r) + [""] * width)[:width])
            combined.append([""] * width)
        return xlsx_response(f"{self.report.key}.xlsx", header, combined,
                             title=self.report.title, church=church)


# ===========================================================================
# Registry
# ===========================================================================

class ReportRegistry:
    def __init__(self):
        self._reports: dict[str, Report] = {}

    def register(self, report: Report):
        if report.key in self._reports:
            raise ValueError(f"Report '{report.key}' already registered.")
        self._reports[report.key] = report
        return report

    def get(self, key):
        return self._reports.get(key)

    def all(self):
        return sorted(self._reports.values(),
                      key=lambda r: (r.category, r.title))

    def visible_to(self, user):
        return [r for r in self.all() if r.permission(user)]


registry = ReportRegistry()
