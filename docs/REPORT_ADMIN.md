# Report Administration Platform (Phase 8)

*A configuration-driven platform: administrators design report layouts, manage
templates, schedule generation, brand output, browse a report library, and
monitor reporting health — without modifying application code. Built entirely on
the existing Generic Report Engine, Component Library, Narrative Engine, Renderer
Framework and Snapshot Foundation; accounting still flows only through the
Financial Metrics Registry via ReportContext.*

---

## 1. Report Designer

**Architecture.** A `ReportDefinition` model persists a report as data: a JSON
`sections` list (each entry names a *registered* component, plus params, title
override, `enabled` flag and a `LayoutMeta` dict), report-level `filters`, page
`page_settings`, permission and category. The compiler
(`reports/services/designer.py`) turns a definition into exactly the same kind of
engine `Report` object the code-defined reports produce — so a designed report
renders through the identical engine, renderers, narratives, dependency map and
snapshot pipeline, with the same permissions and every export format.

**Key guarantee.** A definition can only *arrange registered components*; it can
never introduce a calculation. All figures still come from the Financial Metrics
Registry through ReportContext. The compiler namespaces designed reports as
`def__<key>`, so they can never clash with code-defined reports.

**Validation.** `validate_definition` refuses unknown components, unknown/missing
narrative keys and out-of-range widths *before* a definition can be saved live,
so an administrator cannot persist a configuration that would render incorrectly
(Part 9 security).

**Capabilities.** Create, duplicate, edit, enable/disable, delete; configure
title, description, category, permission, sections (reorder via `order`, group,
width, per-section params, enable/disable), filters, and page settings. The
editor is a JSON-backed section editor with the full component palette and
narrative-key list surfaced; a drag-and-drop canvas can layer on top of this same
persistence later (the model is already complete). Lazy registration: a designed
report is compiled/registered on first request, so no startup database query.

**URLs.** `/reports/designer/` (list), `/reports/designer/new/`,
`/reports/designer/<key>/` (edit), `.../duplicate/`, `.../delete/`. A designed
report renders at `/reports/r/def__<key>/`.

## 2. Component configuration

Every registered component is configurable through a definition section:
visibility (`enabled`), section title/labels, params (e.g. `narrative_key`),
ordering, width, grouping, and — via `LayoutMeta` — collapse state, print
visibility, export visibility and per-format restriction. Components continue to
consume ReportContext exclusively; configuration is presentation only.

## 3. Branding & themes

**Architecture.** A `ReportBranding` model holds church name, conference, region,
contact details, logo URL, primary/accent colours, fonts, header/footer text,
watermark, certification statement, page size/orientation and page numbering. One
branding is active at a time (enforced on save).

**Application.** `core/reporting/renderers.py::resolve_branding` is the single
place branding is read; it resolves the active branding (falling back to
`SiteConfig`'s church name, so behaviour is unchanged until branding is
configured). The PDF and Word renderers stamp the church name, conference/region,
header, certification statement and footer, and the Word renderer applies the
primary colour to headings/table headers. Because every renderer reads the one
helper, branding applies consistently across outputs.

## 4. Report scheduling

**Architecture.** A `ReportSchedule` model (frequency daily/weekly/monthly/
quarterly/yearly/manual, period policy, export formats, recipients, enabled,
next/last run + status) plus a `ScheduleRun` execution history. The execution
service (`reports/services/scheduling.py`) renders a schedule's report **headless**
(no HTTP request) for the policy-derived accounting period, creates an immutable
snapshot (building directly on the Phase 7 Snapshot Foundation), and records the
run. It never raises — failures are captured on the run so a scheduler can retry
(the `attempt` counter supports this).

**Execution.** `execute_schedule` runs one schedule now (used by the "run now"
button); `run_due_schedules(now)` runs every enabled schedule whose `next_run` is
due — the entry point a cron/worker calls. The background worker itself is an
operational step, not application code. Period policies (`prev_month`,
`prev_quarter`, `ytd`, `prev_year`, `all`) resolve to concrete date ranges.

**URLs.** `/reports/schedules/` (list + create), `.../run/`, `.../toggle/`.

## 5. Report distribution

Schedules carry a `recipients` list and a `require_approval` flag. This phase
implements the recipient configuration and ties every generated snapshot to its
schedule run (so a distributed report always references its immutable snapshot).
Actual email/notification sending is a thin operational layer over the existing
messaging (Africa's Talking / email backend) and is left as a deferred wiring
step (#38) — the data model and the snapshot linkage are in place.

## 6. Report Library

`/reports/library/` — the central entry point. Lists every report (code-defined
and designed) grouped by category, with search, tags, favourites (per user),
recently used (per user) and frequently used (all users), plus counts of
designed reports, active schedules and snapshots. Favourites and usage are backed
by `ReportFavourite` and `ReportUsage`; usage (with render time) is recorded by
the engine view on every render, and never breaks a report if it fails.

## 7. Report versioning

Extends the Snapshot Foundation. Each snapshot records app `report_version` and
engine `template_version`; a `ReportDefinition` carries its own
`template_version` (bumped on each edit). `/reports/snapshots/` lists snapshot
history (filterable by report); `/reports/snapshots/compare/<a>/<b>/` diffs two
snapshots section-by-section on their immutable structured payloads (the
deterministic integrity anchor), so a "changed" row reflects a genuine figure or
structure change. Snapshots remain immutable after publication (save raises).

## 8. Feature Adoption Dashboard

`/reports/adoption/` — platform health for developers/administrators: registered
metrics, engine reports, components and narratives; renderer formats; component
reuse across reports; snapshot coverage; report view counts and average render
time; most-used reports; active schedules and failed runs; open (non-addressed)
recommendations parsed from `docs/recommendations.md`; and the remaining legacy
reports.

## 9. Administration & security

* **Permissions.** Editing (designer, schedules) requires `TreasurerRequiredMixin`;
  the library, adoption dashboard and snapshot history are read-only under
  `ReportAccessMixin`. Designed reports carry their own permission
  (reports/treasurer/admin) enforced by the engine at render time, exactly like
  code-defined reports.
* **Configuration validation.** Definitions are validated before saving; an
  invalid configuration is refused with a clear message and never persisted live.
* **Immutability & audit.** Snapshots are immutable; the Django admin exposes
  definitions, schedules, runs, branding, usage and favourites (snapshots and
  runs read-only). Ownership is recorded on definitions and schedules.
* **No calculation surface.** Because a definition only arranges registered
  components, an administrator cannot introduce or alter an accounting
  calculation through the platform.

## 10. Performance

Designed reports reuse the one shared ReportContext and the request-scoped metric
memo, exactly like code reports. Designed reports are compiled lazily (first
request) rather than at startup, avoiding a startup database query. Usage tracking
is a single insert guarded so it can never slow or break a render. Snapshot
generation reuses the deterministic payload checksum from Phase 7.

---

## Files

New: `reports/models_admin.py` (ReportBranding, ReportDefinition, ReportSchedule,
ScheduleRun, ReportUsage, ReportFavourite), `reports/services/designer.py`,
`reports/services/scheduling.py`, `reports/admin_views.py`, templates
(`library.html`, `adoption_dashboard.html`, `designer_list.html`,
`designer_edit.html`, `schedule_list.html`, `snapshot_history.html`,
`snapshot_compare.html`), migration `reports/0002_*`.

Modified: `reports/models.py` (import admin models), `reports/urls.py` (platform
routes), `reports/views.py` (usage tracking + lazy designed-report resolution),
`reports/admin.py` (register admin models), `reports/apps.py` (lazy registration
note), `core/reporting/renderers.py` (branding), `templates/reports/index.html`
(platform links).
