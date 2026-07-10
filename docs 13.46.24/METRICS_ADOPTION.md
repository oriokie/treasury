# Metrics Registry Adoption Report

*Updated Phase 6 (Financial Narrative Engine + statement migration). Tracks
migration of reporting code onto the Financial Metrics Registry
(`core/metrics.py`) via the Semantic Reporting Layer and the component-based
Generic Report Engine (`core/reporting`).*

---

## Phase 6 update

**Registry grew to 23 metrics** — added `operating_expense`,
`capital_expenditure`, `expense_by_category` (recommendation #23), so the Income
Statement and Board Report read these named concepts from the registry instead of
aggregating them inline. Canonical implementations in
`reports/services/balances.py`, proven equal to the legacy Income Statement.

**Financial Narrative Engine** — 24 narratives, each drawing figures *only* from
`ReportContext`; deterministic; provenance-tracked. A `NarrativeComponent` folds
any narrative into a report, and its metrics flow into the dependency map.
Narratives are the only new "consumers" and they are registry-native by
construction.

**Reports migrated to the engine (parallel-run, legacy untouched):**
`income_statement_v2`, `trial_balance_v2`, `financial_position_v2`,
`board_report_v2` — all `ReportContext`-only, registry-metric-only, composed from
reusable components + narratives, rendering in all six formats. Income &
Expenditure figures proven identical to the legacy view; trial balance balances.

**Dashboard migration begun** — `DashboardView` headline figures now come through
a single `ReportContext` (fund summary, trust summary, trust-to-remit, giving by
group, income by channel, tithe). Figures identical (services unchanged; metrics
wrap them), now sharing the reports' memoized metrics (recommendation #24).

---

## 1. Where the registry stands

23 metrics registered, spanning Accounting, Balance, Expense, Income, Loan,
Payment and Trust. Each names one authoritative implementation; the registry is
browsable at `/reports/metrics/`, and the component library at
`/reports/components/`.

The registry is a **facade over already-canonical services** — the real
implementations live in `reports/services/balances.py`,
`ledger/services/posting.py`, `loans/services/*`, `assets/models.py`,
`pledges/models.py`, `cashbook/*`. Adoption therefore means *routing consumers
through the registry / semantic layer*, not rewriting maths.



---

## 2. Components migrated this phase

| Component | Change | Basis |
|---|---|---|
| **Semantic Reporting Layer** (`ReportContext`) | New. Sole interface for reports/dashboards/widgets/exports/AI to obtain figures; every accessor resolves to a registered metric. | Draws 100% from the registry. |
| **Generic Report Engine** | New. Every section draws figures from the shared `ReportContext`; the engine cannot bypass the registry (sections receive a context, not raw models). | Registry-only by construction. |
| **`fund_overview` demo report** | New. Three sections (`FundBalancesSection`, `IncomeMixSection`, `TrustToRemitSection`) built entirely on registry metrics (`fund_summary`, `income_by_channel`, `trust_summary`, `trust_to_remit`). | Registry-only. |
| **Request-scoped memo** (`perfcache`) | New. Makes every consumer of `perfcache.cached()` (which underlies the registry's `fund_summary`/`trust_summary`) dedupe per request — recommendation #1, applied system-wide without touching report views. | Transparent. |

Carried over from v2.27 (already on the registry): the dashboard income-credit
basis and trust-to-remit, and the assistant's tithe/credit-filter, all delegate
to `core.metrics`.

---

## 3. Legacy calculations remaining (not yet on the registry)

These are **intentionally not migrated in this phase** (the brief is "do not
redesign existing reports yet"). They remain correct; they simply don't yet go
through the semantic layer.

| Location | Nature | Migrate when |
|---|---|---|
| `reports/views.py` (~54 raw aggregate sites) | Mostly **report-specific groupings** (by member, category, year, Sabbath) plus a few named concepts (income, op-ex) computed inline in the Monthly/Board reports. | When each report is rebuilt on the engine (Board first, next phase). Named concepts should become registry metrics; groupings can stay report-local or become parameterised metrics. |
| `core/views.py`, `leaders/views.py` | Executive/leader dashboards with inline aggregates. | When dashboards adopt `ReportContext` (a later phase). |
| `reports/services/treasurer.py`, `monthly.py` | Time-series and LCB/trust trend builders reusing `balances`. | Wrap as registry metrics if reused outside their reports; otherwise leave. |
| `cashbook/views.py`, `statements/views.py`, `envelopes/views.py`, `pledges/views.py` | Operational subsystem aggregates (payments, reconciliation, envelopes, pledges). | As each subsystem gains engine reports. |
| Entity model properties (`loans`, `pledges`, `assets`, `cashbook`) | Per-object figures correctly co-located with their model. | Generally stay as-is; exposed via registry metrics where a *portfolio* total is needed (already done for loans/payments). |

None of these are duplicates of a registered metric that bypass it; they are
either report-specific groupings or entity-scoped properties. The inventory
(`docs/CALCULATION_INVENTORY.md`) classifies each.

---

## 4. New metrics added this phase

None. The phase built the *layers above* the registry; the existing 20 metrics
covered every figure the new demo report needed. This is the intended outcome —
if a report needs a figure that isn't a metric, the metric is added; here none
were missing.

---

## 5. Remaining technical debt

* **Named concepts still inline in the Monthly/Board report** (income, operating
  expense, capital) should become registry metrics (e.g. `operating_expense`,
  `capital_expenditure`) when the Board Report is rebuilt, so the report reads
  them from `ctx.metric(...)` rather than aggregating inline. Deferred to that
  phase by design.
* **Dashboards** (`core/views.py`, `leaders/views.py`) still assemble figures
  directly; they should adopt `ReportContext` so a dashboard and a report agree
  by construction.
* **Chart/HTML section kinds** exist in `SectionData` but the demo only exercises
  tables/keyvalue; chart rendering through the engine is a small follow-up.

---

## 6. Recommended next migrations (in order)

1. **Board Report onto the engine** (next phase, already planned). Compose
   existing reusable sections + new ones; add `operating_expense` /
   `capital_expenditure` metrics; prove figures identical to the current report.
2. **Financial statements** (Balance Sheet, Income Statement, Cash Flow) as
   engine reports sharing one `ReportContext` — they currently each rebuild
   overlapping aggregates.
3. **Dashboards adopt `ReportContext`** so headline figures come from the same
   memoized metrics the reports use.
4. **Subsystem reports** (payments, reconciliation, envelopes, pledges) as they
   are touched.

Each migration should follow the v2.27 discipline: prove the new path returns
figures identical to the legacy one before removing the legacy implementation.

---

## 7. Adoption metrics

* Registry metrics: **20** (unchanged across v2.28 and v2.29; none needed
  adding — the existing set covered every figure the layers above required).
* Reusable components: **16** (KPI cards, executive summary, fund/income/expense/
  budget summary, cash position, bank recon, outstanding items, variance, chart,
  commentary, signature, appendix, info panel, financial statement) — all
  registry-native (draw only from `ReportContext`).
* Chart Engine: metric-driven `ChartSpec` builders (line/bar/stacked/doughnut/
  waterfall/gauge/comparison) — charts never query the DB.
* Render formats: **6** (HTML, CSV, Excel, PDF, Word, Print) via a Renderer
  Registry; format is orthogonal to components.
* Reports on the engine: **2** demonstrations (`fund_overview`,
  `board_pack_demo`) — **no existing report migrated**, per scope.
* Dependency map: live, derived per render; traces components → 11 metrics →
  accounting services for the demo, with a reverse impact index.
* Recommendation #25 (exercise chart/HTML section kinds): **addressed** — the
  chart component and two live charts render through the engine.
* Recommendation #1 (v2.28): **addressed** (request-scoped memo; Monthly report
  133 → 120 queries, no figure change).
