# Report Engine & Semantic Reporting Layer — Architecture

*Phase deliverable (v2.28). Introduces the Semantic Reporting Layer and the
Generic Report Engine as reusable foundations, without redesigning any existing
report. The Board Report will be the first consumer, in the next phase.*

---

## 1. Layered architecture

```
   Reports / dashboards / widgets / exports / AI
                    │  (consume figures only through…)
                    ▼
   ┌─────────────────────────────────────────────┐
   │ Generic Report Engine (core/reporting/engine)│  structure, filters,
   │  Report · Section · Filter · Registry ·      │  permissions, drill-down,
   │  RenderedReport · render pipeline · exports  │  exports
   └─────────────────────────────────────────────┘
                    │  (draw every figure from…)
                    ▼
   ┌─────────────────────────────────────────────┐
   │ Semantic Reporting Layer (core/reporting/     │  ReportContext:
   │  context.ReportContext)                       │  period+scope bound,
   │                                               │  memoized per render
   └─────────────────────────────────────────────┘
                    │  (which orchestrates…)
                    ▼
   ┌─────────────────────────────────────────────┐
   │ Financial Metrics Registry (core/metrics)     │  the single source of
   │  20 registered metrics, each = one            │  truth for accounting
   │  authoritative implementation                 │  figures
   └─────────────────────────────────────────────┘
                    │  (which forward to…)
                    ▼
   ┌─────────────────────────────────────────────┐
   │ Canonical services & models                   │  balances.py, posting.py,
   │  (unchanged; still the real implementations)  │  loans, assets, pledges…
   └─────────────────────────────────────────────┘
```

The rule the whole stack enforces: **reports never calculate accounting figures
themselves.** They ask the Semantic Reporting Layer, which asks the Metrics
Registry, which owns the one true implementation.

---

## 2. Semantic Reporting Layer — `ReportContext`

`core/reporting/context.py`.

A `ReportContext` is a **period- and scope-bound doorway to the Metrics
Registry, memoized for the life of one report render**. It is the sole interface
new code should use to obtain financial data.

```python
from core.reporting import ReportContext

ctx = ReportContext.from_request(request)      # honours ?start/?end
funds  = ctx.fund_summary()                    # metric, computed once
tithe  = ctx.metric("tithe")                   # period auto-applied
income = ctx.metric("total_income")            # shares this render
```

Key properties:

* **One doorway.** `ctx.metric(name, …)` resolves against `core.metrics`. If a
  name isn't a registered metric it raises `KeyError` — you cannot silently
  invent an ad-hoc figure. Convenience accessors (`fund_summary`,
  `trust_summary`, `tithe`, `total_income`, …) read well in views/templates.
* **Compute-once per render.** Results memoize on `(metric name, call args)` for
  the context's lifetime. A report whose sections each need `fund_summary`
  computes it once. This is the general form of recommendation #1.
* **Period auto-application.** Period-aware metrics (registry `inputs` starting
  with `start`) automatically receive the context's period, so sections don't
  each parse the request.
* **Provenance.** `ctx.metrics_used()` lists the metrics a render consumed —
  used by the adoption audit and available to future AI provenance.

The layer adds **no accounting rules**; it orchestrates the registry. That is
deliberate — raw aggregates are exactly what it replaces.

### Relationship to `core.perfcache`

Two complementary caches, different lifetimes:

| | Request-scoped memo | TTL cache (`perfcache`) |
|---|---|---|
| Lifetime | one request/render | seconds, across requests |
| Default | **always on** | off (opt-in via `DJANGO_DASH_CACHE_TTL`) |
| Purpose | dedupe within a render (rec. #1) | reduce cross-request recompute |
| Invalidation | end of request; cleared on `bump_data_version` | data-version bump + TTL |

The request memo lives in `core.perfcache` (a `contextvars` dict opened by
`RequestScopeMiddleware`) so that **every** function already routing through
`perfcache.cached()` — `department_summary`, `trust_summary`, and more — is
deduped per request with no change to those functions or their callers. A
`ReportContext` additionally memoizes at the metric level for code that goes
through the semantic layer directly.

---

## 3. Generic Report Engine

`core/reporting/engine.py`.

### 3.1 Report lifecycle

```
register(Report)  ──►  request  ──►  Report.render(request)
                                       │
                                       ├─ permission check (report-level)
                                       ├─ resolve filters  (Filter.resolve)
                                       ├─ build ONE ReportContext (shared)
                                       ├─ for each Section visible_to(user):
                                       │      Section.build(ctx, filters) → SectionData
                                       └─ RenderedReport(sections)
                                              │
                       ┌──────────────────────┼───────────────────────┐
                       ▼                       ▼                       ▼
                   HTML template          to_csv()               to_xlsx()
              (engine_report.html)   (reports.exports)      (reports.exports)
```

Because `render()` builds **one** `ReportContext` and passes it to every
section, shared metrics compute once per report — the engine generalises
recommendation #1 to every report built on it.

### 3.2 Component hierarchy

* **`Report`** — a registered composition: `key`, `title`, `permission`,
  ordered `sections`, declared `filters`, `category`. Owns the render pipeline.
* **`Section`** — the reusable unit. `build(ctx, filters) → SectionData`. Draws
  figures only from `ctx`. May carry its own `permission` (section-level
  visibility) on top of the report's. `FunctionSection` wraps a plain callable
  for simple cases.
* **`SectionData`** — format-agnostic output: `columns`, `rows`, optional
  `total`, `note`, and a `kind` (`table` / `keyvalue` / `chart` / `html`). The
  HTML template and the CSV/Excel exporters all read this one structure.
* **`Column` / `Row`** — a column may be a `drilldown` anchor; a row may carry a
  `url` (its drill-down target) and `emphasis` (subtotal styling).
* **`Filter`** — declarative (`name`, `label`, `kind`, `default`, `choices`);
  `resolve(request)` reads and types the value. Kinds: text, date, month,
  choice, fund, bool.
* **`RenderedReport`** — the render result; knows how to `to_csv()` / `to_xlsx()`
  by flattening its sections through the existing `reports.exports` helpers.
* **`ReportRegistry`** (singleton `registry`) — `register` / `get` / `all` /
  `visible_to`.

### 3.3 Rendering pipeline & exports

Rendering is independent of data retrieval: a section produces `SectionData`
(pure data), and the *presentation* (HTML template `engine_report.html`, or the
CSV/Excel exporters) consumes it. Exports reuse the existing
`reports.exports.csv_response` / `xlsx_response` — no new export plumbing, and no
existing export is redesigned. PDF/Word are future presenters over the same
`SectionData` (the structure is format-agnostic by design).

### 3.4 Filters, permissions, drill-down

* **Filters** resolve once per render and are handed to every section.
* **Permissions** are enforced at two levels: the report (before anything is
  built) and, optionally, per section (`Section.permission`). A user sees only
  authorised reports (`registry.visible_to`) and, within a report, only
  authorised sections.
* **Drill-down** is generic: a section marks a column `drilldown=True` and
  attaches a `url` per row; the template renders links. Filters/permissions are
  preserved because a drill-down link points at another report/view that applies
  its own context and permission. Example chain: fund overview → fund ledger →
  transaction → journal entry.

---

## 4. Extension points — adding a report

No framework change is needed to add a report. In an app's `engine_reports.py`
(imported from its `AppConfig.ready`):

```python
from core.reporting import Report, Section, SectionData, Column, Row, registry

class MySection(Section):
    key, title = "my_section", "My section"
    def build(self, ctx, filters):
        value = ctx.metric("total_income")          # from the registry only
        return SectionData("my_section", "My section",
                           [Column("k", "Item"), Column("v", "Value", numeric=True)],
                           [Row(cells={"k": "Income", "v": value})])

registry.register(Report(
    key="my_report", title="My report", category="Custom",
    permission=lambda u: True, sections=[MySection()]))
```

It immediately has a URL (`/reports/r/my_report/`), HTML, CSV/Excel exports,
filters and drill-down. If it needs a figure that isn't a registered metric,
**add the metric to `core/metrics.py`** — do not compute it in the section.

Future modules (payroll, inventory, conference, projects) contribute reports the
same way, with no engine changes.

---

## 5. Migration strategy

* The engine runs **alongside** the existing reporting system. Nothing is
  migrated in this phase. All existing report URLs, views, templates, exports
  and permissions are untouched and keep working.
* Recommendation #1 is addressed **transparently**: the request-scoped memo
  benefits the current hand-written reports (including the Monthly Treasurer's
  Report) without changing their code, because they already call through
  `perfcache.cached()`.
* The Board Report becomes the first report rebuilt on the engine in the next
  phase; others follow opportunistically. Legacy implementations remain until
  their replacement is proven equal.

---

## 6. Backward compatibility

* No existing URL, view, template, export or permission changed.
* One new middleware (`RequestScopeMiddleware`) that only opens/closes a
  per-request dict — safe, cheap, and correctness-preserving (a mid-request
  financial write clears the memo via `bump_data_version`, so no stale figure is
  ever served within a request).
* No database migrations.

---

## 7. Files

New:
* `core/reporting/__init__.py` — public API.
* `core/reporting/context.py` — `ReportContext` (Semantic Reporting Layer).
* `core/reporting/engine.py` — engine (Report, Section, Filter, registry, …).
* `reports/engine_reports.py` — reusable sections + the `fund_overview` demo.
* `templates/reports/engine_report.html` — generic renderer.
* `docs/REPORT_ENGINE.md`, `docs/METRICS_ADOPTION.md` (this phase's docs).

Modified:
* `core/perfcache.py` — request-scoped memo + `RequestScopeMiddleware`.
* `config/settings.py` — register the middleware.
* `reports/views.py` — `EngineReportView` (generic adapter).
* `reports/urls.py` — `/reports/r/<key>/` route.
* `reports/apps.py` — import engine reports at `ready()`.
* `templates/reports/index.html` — links to the metrics registry and the demo.
```
