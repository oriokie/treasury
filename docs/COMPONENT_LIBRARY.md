# Report Component Library, Chart Engine & Rendering Framework

*Phase deliverable (v2.29). Builds a reusable, component-based reporting layer on
top of the Generic Report Engine (v2.28) and the Semantic Reporting Layer /
Metrics Registry. No existing report is redesigned or migrated; this is new
infrastructure the Board Report (next phase) and future reports will consume.*

---

## 1. Where this sits

```
   A report  =  an ordered list of COMPONENTS, each with LAYOUT metadata
                          │
                          ▼
   ┌───────────────────────────────────────────────────────────┐
   │ Component Library (core/reporting/component_library.py)     │  16 reusable
   │  KPI cards · Executive summary · Fund/Income/Expense/Budget │  components,
   │  summary · Cash position · Bank recon · Outstanding items · │  registered
   │  Variance · Chart · Commentary · Signature · Appendix ·     │  in the
   │  Info panel · Financial statement                           │  Component
   └───────────────────────────────────────────────────────────┘  Registry
        │ each is a ComponentSection (extends engine.Section)
        │ draws figures ONLY from…                    produces…
        ▼                                                 ▼
   ReportContext (Semantic Layer)              SectionData (format-agnostic)
        │                                                 │
        │ figures                                         │ consumed by…
        ▼                                                 ▼
   Metrics Registry (single source of truth)   Rendering Framework
                                               (core/reporting/renderers.py)
                                                 HTML · CSV · Excel · PDF ·
                                                 Word · Print  (Renderer Registry)

   Chart Engine (core/reporting/charts.py) — metric-driven ChartSpecs, never
   queries the DB. Dependency Map (core/reporting/dependencies.py) — traces
   components → metrics → accounting services, with reverse impact analysis.
```

The invariant from the whole stack still holds: **components never compute
accounting figures.** They ask a `ReportContext` (the Semantic Reporting Layer),
which resolves registered metrics. If a figure isn't a metric, the metric is
added to `core/metrics.py`; a component never aggregates directly.

---

## 2. Component model

### 2.1 `ComponentSection` (core/reporting/components.py)

A component is a `Section` subclass (so it slots into the existing engine render
pipeline) enriched with:

* **Layout metadata** — a `LayoutMeta` (width on a 12-col grid, order, priority,
  group, collapse, responsive, print/export visibility, page-break). Presentation
  only; it never changes *what* is computed.
* **Dependency tracking** — components implement `render(ctx, filters)` (not
  `build`); the base `build()` wraps it to record, from the context's usage, the
  metrics the component actually consumed (`extra["metrics_used"]`) plus its
  declared metrics, and to attach the layout (`extra["layout"]`). This feeds the
  Financial Dependency Map with zero extra work per component.
* **Registration** — components register in the `ComponentRegistry` (a key →
  factory map), so reports compose by name and new components (including from
  future modules like Payroll or Inventory) are added by registration, never by
  editing the engine.

### 2.2 The library (core/reporting/component_library.py)

Sixteen registered components, by category:

* **Summary** — `kpi_cards` (headline KPI band), `executive_summary`
  (auto-generated plain-language overview).
* **Financial** — `fund_summary` (per-fund movement with drill-down to ledgers),
  `income_summary`, `expense_summary`, `budget_summary` (hides itself when no
  budgets are configured), `cash_position`, `financial_statement` (a generic
  labelled line-item statement built from a metric spec).
* **Reconciliation** — `bank_recon_summary` (unpresented payments as-at),
  `outstanding_items` (pending receipts, trust-to-remit, unpresented, loans).
* **Analysis** — `variance_analysis` (this period vs the prior equal-length
  period, per fund).
* **Visual** — `chart` (wraps any `ChartSpec`).
* **Narrative** — `commentary` (static or callable text), `info_panel`
  (methodology/caveats; export-hidden by default).
* **Formal** — `signature_block` (prepared/reviewed/approved), `appendix`.

Each is small, self-contained, and reusable across many reports.

---

## 3. Chart Engine (core/reporting/charts.py)

A chart is a **specification, not an image**: a `ChartSpec` (type, labels,
datasets, options, provenance) whose `to_config()` yields a Chart.js config the
HTML renderer hands to the browser. Because specs are built from a
`ReportContext`, **charts never query the database** — same rule as everything
else.

* **Generic builders**: `line`, `bar`(+stacked), `doughnut`/`pie`, `waterfall`
  (emulated with a transparent base + visible step over a stacked bar, since core
  Chart.js has no native waterfall), `comparison` (grouped bar), `gauge`
  (half-doughnut progress).
* **Metric-driven builders**: `income_by_channel(ctx)`,
  `fund_closing_balances(ctx)`, `local_vs_trust(ctx)` — each reads registry
  metrics via the context and records provenance on the spec.
* **Palette**: forest/brass identity, matching the existing dashboard charts.

The `ChartComponent` wraps any spec-producing callable, so a report adds a chart
by naming a builder — no bespoke chart code, and the chart's metric provenance
flows into the dependency map. This exercises the engine's `kind="chart"` section
type (recommendation #25).

---

## 4. Rendering Framework (core/reporting/renderers.py)

Components produce `SectionData`; **renderers** turn a whole `RenderedReport`
into a medium. A renderer implements `render(rendered, *, church, request)` and
declares its `fmt`. Six are registered in the `RendererRegistry`:

| fmt | Renderer | Output |
|---|---|---|
| html | HtmlRenderer | template context (engine_report.html) |
| print | PrintRenderer | HTML with print-hidden components dropped |
| csv | CsvRenderer | stacked-section CSV (via `reports.exports`) |
| xlsx | XlsxRenderer | styled workbook (via `reports.exports`) |
| pdf | PdfRenderer | ReportLab PDF (forest header, tables) |
| docx | DocxRenderer | Word-compatible HTML (`application/msword`) — the app's existing Word approach, no new dependency |

Key properties:

* **Format is orthogonal to components.** A new output format is a new renderer;
  components never change. Adding, say, a JSON or XBRL renderer later touches
  only the registry.
* **Per-medium visibility** is honoured uniformly: every renderer consults each
  component's `LayoutMeta.visible_in(fmt)`, so an info panel hidden from exports,
  or a note hidden from print, drops out consistently everywhere.
* **Reuses existing plumbing.** CSV/Excel go through the same
  `reports.exports.csv_response` / `xlsx_response` the rest of the app uses, so
  engine exports match the established style and the existing exports are
  untouched.

The generic `EngineReportView` picks the renderer from `?export=` / `?print=1`,
so every registered report gets all formats for free.

---

## 5. Financial Dependency Map (core/reporting/dependencies.py)

Extends `ReportContext.metrics_used()` into a full graph, **derived from an
actual render** (never hand-maintained):

* `build_dependency_map(rendered)` reads each component's recorded
  `metrics_used` and, via the registry's `authoritative` metadata, resolves the
  underlying accounting services (e.g. `reports.services.balances.department_summary`).
* `DependencyMap` exposes: `all_metrics()`, `all_services()`,
  `metric_to_components()` (reverse index — "who depends on this metric?"), and
  `as_dict()` (machine-readable, served at `?deps=json`).
* `impact_of_metric(metric_key, reports)` gives static cross-report impact
  analysis from components' `declared_metrics` without rendering.

Uses: always-accurate "what feeds this report" documentation; tracing a figure
to its accounting service; impact analysis before changing a service; and a
provenance graph for future AI reasoning.

---

## 6. Layout metadata & the future Report Designer (core/reporting/layout.py)

`LayoutMeta` is a complete, declarative, serialisable placement model —
width/order/priority/group/collapse/responsive/print/export/page-break, plus a
free-form `extra`. Nothing consumes it as editable UI yet; it exists so:

* the renderers can honour placement and visibility now, and
* a future drag-and-drop **Report Designer** can read/write layouts
  (`as_dict()` / `from_dict()`) with no further schema work — a report becomes
  data (a list of component keys + layouts), not code.

This phase deliberately **builds the model, not the designer UI.**

---

## 7. Extension points for future modules

A new module contributes reporting without touching the engine:

1. **New metric** → register in `core/metrics.py` (the only place accounting
   logic lives).
2. **New component** → subclass `ComponentSection`, implement `render(ctx,
   filters)` drawing from the context, register in `component_registry`.
3. **New chart** → add a `ChartEngine` builder returning a `ChartSpec`.
4. **New output format** → implement a `Renderer`, register in
   `renderer_registry`.
5. **New report** → register a `Report` composing components with layouts.

Each step is registration, not modification — the open/closed principle applied
to reporting. A Payroll module, for instance, would add payroll metrics, a
`PayrollSummaryComponent`, and a `payroll_report`, and immediately get HTML,
every export, drill-down and a dependency map.

---

## 8. Backward compatibility

* No existing report, view, template, export, URL or permission changed.
* The generic template gained new section kinds (kpi/chart/commentary/
  signature/info) and a layout-aware grid; the v2.28 `fund_overview` demo still
  renders through it unchanged.
* No database migrations (no models added).
* New middleware from v2.28 is unchanged; this phase adds none.

---

## 9. Files

New:
* `core/reporting/layout.py` — `LayoutMeta`.
* `core/reporting/components.py` — `ComponentSection`, `ComponentRegistry`.
* `core/reporting/component_library.py` — the 16 components.
* `core/reporting/charts.py` — Chart Engine (`ChartSpec`, `ChartEngine`).
* `core/reporting/renderers.py` — rendering framework + 6 renderers.
* `core/reporting/dependencies.py` — Financial Dependency Map.
* `reports/component_demo.py` — the `board_pack_demo` composition.
* `templates/reports/component_catalogue.html` — component library catalogue.

Modified:
* `core/reporting/__init__.py` — export the new public API.
* `reports/views.py` — `EngineReportView` routes all renderers + dependency-map
  endpoint; `ComponentCatalogueView`.
* `reports/urls.py` — `components/` route.
* `reports/apps.py` — register component library + demo at `ready()`.
* `templates/reports/engine_report.html` — render all component kinds + charts.
* `templates/reports/index.html` — links to the catalogue and demos.
* `core/templatetags/treasury_extras.py` — `pct_of_12` grid filter.
```
