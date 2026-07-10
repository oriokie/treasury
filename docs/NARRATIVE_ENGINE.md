# Financial Narrative Engine & Report Migration (Phase 6)

*Builds a reusable Financial Narrative Engine and migrates financial statements
and the Board Report onto the Generic Report Engine, so every report is an
assembly of components + narratives powered by the Financial Metrics Registry,
with identical figures across every output.*

---

## 1. Financial Narrative Engine

### 1.1 Principle

A narrative is generated commentary that consumes **only** the Semantic Reporting
Layer (`ReportContext` → Metrics Registry). It contains no hardcoded value and no
accounting logic: it asks the context for registered metrics and renders words
around them. The same figures that fill a report's tables drive its prose, so
**commentary can never contradict the statements.**

Narratives are **deterministic**: a pure function of (context figures, config).
No randomness, no LLM, no time dependence beyond the context's period — re-running
yields byte-identical text. (Verified by test.)

### 1.2 Architecture

```
   NarrativeEngine(config)
        │  generate(key, ctx) / generate_many / findings
        ▼
   narrative_registry  →  Narrative (subclass)
                             │  generate(ctx, cfg) -> NarrativeResult
                             ▼
                     reads ReportContext metrics only
                             │
                     NarrativeResult(text, metrics_used, findings)
```

* **`Narrative`** — the reusable unit. Subclass sets `key`/`title` and implements
  `generate(ctx, cfg)`. Registered in `narrative_registry`.
* **`NarrativeConfig`** — `style` (executive/treasurer/auditor/committee/detailed/
  concise), `tone` (informational/analytical/formal/executive_summary), and a
  `Thresholds` bundle. Style/tone change phrasing and verbosity, never numbers.
* **`Thresholds`** — configurable trigger levels (variance %, large-movement %,
  ageing days, low-cash floor, material amount). Detection reads these; changing
  a threshold changes which *findings* fire, never the figures.
* **`Finding`** — a structured detected condition (code, severity, message,
  metric, value, subject). Powers the warnings/exceptions/recommendations
  narratives and is machine-readable for a future AI layer.
* **`NarrativeResult`** — text + `metrics_used` (provenance) + `findings`.

### 1.3 Coverage (24 narratives)

Executive summary, financial highlights, income analysis, expense analysis,
giving trends, budget performance, budget variance, fund performance, restricted
funds, trust funds, development projects, department performance, cash position,
bank reconciliation, outstanding items, asset position, liability position, loan
position, cash flow, financial risks, key changes, exceptions, warnings,
recommendations.

### 1.4 Detection

The detection-bearing narratives raise `Finding`s for: significant/over-threshold
**budget variances** and **overruns**, **negative fund balances**, **inactive
funds** (no receipts), **cash shortages** (closing ≤ floor), **trust funds due**,
**pending (unallocated) receipts**, **unpresented payment instruments**, and
**large period-on-period movements**. `financial_risks`, `warnings`, `exceptions`
and `recommendations` aggregate these; `recommendations` maps each finding code
to a suggested action. All thresholds are configurable and deterministic.

### 1.5 Narrative lifecycle

1. A report (or dashboard, export, API, AI feature) builds one `ReportContext`.
2. A `NarrativeComponent` (or direct `NarrativeEngine` call) names a narrative.
3. The narrative reads registry metrics from the context (shared/memoized).
4. It returns text + provenance + findings; the component renders it as a
   `kind="commentary"` section, and its metrics flow into the dependency map.

Because the narrative and the report tables read the *same* context, their
figures are identical by construction.

---

## 2. Report migration

### 2.1 Strategy

Migrated reports are **new engine reports registered alongside the legacy views**,
which stay untouched so existing URLs/exports/permissions keep working. Each
migrated report is proven (by test) to produce figures identical to the legacy
view. Legacy implementations are removed only after equivalence is established
and the engine version adopted — an adapter-free parallel-run migration.

### 2.2 Migrated this phase

| Report | Engine key | Basis / equivalence |
|---|---|---|
| Income & Expenditure | `income_statement_v2` | recurrent/capital/operating/surplus proven equal to legacy `IncomeStatementView` |
| Trial Balance | `trial_balance_v2` | `trial_balance` metric (ledger); balances by construction |
| Financial Position (summary) | `financial_position_v2` | assets/liabilities/net from registry metrics |
| Board / Treasurer's Report | `board_report_v2` | rebuilt from components + narratives (not copied) |

Each uses `ReportContext` exclusively, only registry metrics, reusable
components, reusable layouts, reusable renderers, and the narrative engine, and
renders in HTML/CSV/Excel/PDF/Word/Print.

### 2.3 New metrics (recommendation #23)

`operating_expense` (recurrent) and `capital_expenditure`, plus a helper
`expense_by_category`, added so the Income Statement and Board Report read these
named concepts from the registry instead of aggregating inline. Their canonical
implementations live in `reports/services/balances.py`
(`operating_expense_total`, `capital_expenditure_total`, `expense_by_category`),
matching the legacy Income Statement filters exactly. Registry now has 23 metrics.

---

## 3. Board Report composition

`board_report_v2` is composed top-to-bottom from reusable components + narratives,
each an independently-configurable section with layout metadata:

Executive summary (narrative) · Financial highlights (narrative) · KPI cards ·
Income/Fund charts · Cash position (component + narrative) · Fund summary
(component + narrative) · Income & expense summaries (+ narratives) · Budget
summary + variance narrative · Development projects + trust narratives · Bank
reconciliation + outstanding items · Variance analysis · Income & Expenditure and
Financial Position statements · Financial risks + recommendations narratives ·
Info panel · Signature block.

Nothing is copied from the legacy `MonthlyTreasurerReportView`; the report is
data (a list of components + layouts), so its structure is configurable through
the engine.

---

## 4. Dashboard migration (begun)

The main `DashboardView` now obtains its headline figures (fund summary, trust
summary, trust-to-remit, giving-by-group, income-by-channel, tithe) through a
single `ReportContext` rather than calling services ad hoc. The underlying
services are unchanged (the metrics wrap them), so figures are identical — but
the dashboard now shares the exact metrics and request-scoped memoization the
reports use, guaranteeing a dashboard figure equals the corresponding report
figure (recommendation #24). Verified by reconciliation test. The executive
dashboard's blended live+historical trend remains on its bespoke path (a larger,
separate migration).

---

## 5. Accounting validation

* **Income & Expenditure** — migrated recurrent/capital/operating/net-surplus
  equal the legacy figures (test `IncomeStatementEquivalenceTests`).
* **Trial Balance** — debits equal credits (`trial_balance` metric).
* **Dashboard** — tithe and trust-to-remit equal the registry metrics (reconciles
  with reports).
* **Narrative** — figures embedded in prose equal the context's metric values.
* No migrated report introduces a new accounting calculation; all read existing
  registry metrics.

---

## 6. Component reuse map (Board Report)

* `KpiCardsComponent`, `FundSummaryComponent`, `IncomeSummaryComponent`,
  `ExpenseSummaryComponent`, `CashPositionComponent`, `BudgetSummaryComponent`,
  `VarianceAnalysisComponent`, `BankReconciliationSummaryComponent`,
  `OutstandingItemsComponent`, `ChartComponent`, `InfoPanelComponent`,
  `SignatureBlockComponent` — all reused from the v2.29 library.
* `NarrativeComponent` (new) — renders any of the 24 narratives.
* `IncomeExpenditureStatementSection`, `FinancialPositionSummarySection` — reused
  by both the statement reports and the Board Report.

---

## 7. Remaining legacy reports

Not yet migrated (out of scope this phase; the machinery now exists): Cash Flow
Statement, Statement of Fund Balances, Budget vs Actual (full), the detailed
Financial Position (with NBV/prepayments/advances), and the operational registers
(Cash Book, Payment/Receipt Register, Asset/Liability/Loan/Envelope/Pledge
reports, Member/Contribution statements). Department and Giving reports likewise.
Each can now be composed from existing components + narratives; see the migration
strategy in §2.1.

---

## 8. Files

New:
* `core/reporting/narrative.py` — engine, config, thresholds, findings, registry.
* `core/reporting/narrative_library.py` — the 24 narratives.
* `reports/financial_statements.py` — migrated statements.
* `reports/board_report.py` — the rebuilt Board Report.

Modified:
* `core/metrics.py` — `operating_expense`, `capital_expenditure`,
  `expense_by_category`.
* `reports/services/balances.py` — their canonical implementations.
* `core/reporting/context.py` — operating/capital accessors.
* `core/reporting/component_library.py` — `NarrativeComponent`.
* `core/reporting/__init__.py` — export narrative API.
* `core/views.py` — `DashboardView` via `ReportContext`.
* `reports/apps.py` — register narratives, statements, board report.
* `reports/exports.py` — sanitise xlsx sheet titles.
```
