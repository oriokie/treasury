# Financial Intelligence Platform (Phase 9)

*A transparent, explainable layer that turns accounting figures — via the
Financial Metrics Registry and Semantic Reporting Layer — into structured
insights, recommendations, trends/forecasts, a health score, and a unified
knowledge service for a future AI Treasurer. No accounting calculation is
duplicated; every conclusion is traceable to a registry metric. No chatbot is
built — this is the reusable knowledge backend.*

---

## 1. Financial Intelligence Engine (`core/intelligence/engine.py`)

An **Insight** is a first-class structured object (not free text) carrying the
full Part-1 field set: title, description, severity, category, confidence,
priority, supporting metrics/transactions, affected funds/departments, period,
suggested actions, status, detection time — and an **Explanation** recording *why*
it fired (reason, metrics read, thresholds exceeded, accounting services used,
contributing transactions). This is Part 9: no black boxes.

An **IntelligenceModule** is a reusable detector: it declares its category and
`declared_metrics` and implements `evaluate(ctx, cfg) -> [Insight]`, reading
figures only from the `ReportContext`. The **IntelligenceEngine** runs the
registered modules against one shared context, backfills provenance, applies the
confidence floor, and returns insights sorted by priority then severity.

**Determinism.** An insight is a pure function of (context figures, config).
Re-running yields identical insights (verified by test). Every threshold lives in
`IntelligenceConfig`, so detection is tunable and reproducible.

## 2. Insight categories & modules (`core/intelligence/modules.py`)

15 modules across the seven categories:

* **Financial health** — operating result (deficit), liquidity/reserve adequacy.
* **Income intelligence** — income trend (declining / exceptional), income
  concentration, trust remittances due.
* **Expense intelligence** — budget overruns, rapid spending increases.
* **Fund intelligence** — negative balances, dormant funds, development-target
  progress.
* **Cash intelligence** — cash shortage, unpresented instruments.
* **Operational intelligence** — pending receipts, outstanding approvals.
* **Asset & liability intelligence** — loan position.

Each reads only registry metrics and records a full Explanation. The set is
extensible: a new detector is a registered `IntelligenceModule`, never a change
to the engine.

## 3. Recommendation Engine (`core/intelligence/recommendations.py`)

`recommendations_from_insights` turns each insight's suggested actions into
prioritised, de-duplicated **Recommendation** objects that carry the insight's
priority, severity, subject, rationale, supporting metrics and fingerprint — so a
recommendation is always explainable back to the metric that triggered it.
Recommendations are dismissible with an audit trail via `InsightStatus`.

## 4. Treasurer Workspace (`/workspace/`)

The Treasurer Intelligence Dashboard presents intelligence, not raw reports:
Financial Health Score + band, Risk Score, high-priority insights (with their
explanations and suggested actions), prioritised recommendations, health-indicator
drill-down, category grouping, critical/warning counts, upcoming scheduled reports
and recent snapshots. Each insight can be acknowledged / resolved / dismissed,
recording an audit-trail entry (`InsightStatusHistory`); dismissed insights drop
out of the workspace. Everything drills into the supporting reports.

## 5. Financial Knowledge Service (`core/intelligence/knowledge.py`)

The reusable knowledge layer a future AI Treasurer consumes through one interface
(Part 5 — no chatbot). For any **concept** (income, expenditure, funds, cash,
trust, budget, loans, position) `knowledge_for(concept, ctx)` assembles: the
canonical metric values + definitions, the relevant narrative, the intelligence
insights, the recommendations, the linked reports, the supporting snapshots, and
the metric→service **dependency graph** — all from the existing architecture,
nothing recomputed. `full_briefing(ctx)` returns the health score + all insights +
recommendations + concept list + provenance + a disclaimer — the single call an
AI assistant or executive summary reads.

## 6. Trend & Forecast Engine (`core/intelligence/trends.py`)

Deterministic, metric-sourced trend analysis: `monthly_series`, `trend`
(direction, growth rate, rolling average), `year_on_year`, and `forecast` (a
simple, transparent least-squares linear projection). Forecasts are **clearly
labelled projections** (`is_projection=True`, "(proj.)" point labels) and never
replace accounting figures. Every result records the metric it was built from.

## 7. Financial Health Score (`core/intelligence/health.py`)

An overall 0–100 score from nine transparently-weighted indicators: liquidity,
budget performance, income diversity, expense control, fund health, cash
management, reconciliation discipline, outstanding obligations, operational
completeness. Each indicator exposes its raw figures, its 0–100 score, its weight,
its supporting metrics and a plain-language explanation; the overall is the
weighted average. No black-box scoring — the weighting and every contribution are
visible and drill-downable (workspace + `/api/analytics/health/`).

## 8. Analytics APIs

JSON endpoints for future mobile / AI consumers, all consuming the Semantic
Reporting Layer:

* `/api/analytics/insights/` — structured insights + provenance.
* `/api/analytics/health/` — the health score + indicators.
* `/api/analytics/trend/?metric=&months=&forecast=1` — trend or forecast series.
* `/api/analytics/knowledge/?concept=` — a concept record, or the full briefing.

## 9. Explainability (Part 9)

Every insight answers: **why** it was generated (`explanation.reason`), **which
metrics** triggered it (`explanation.metrics`, all registry keys — verified by
test), **which thresholds** were exceeded (`explanation.thresholds` with
limit+actual), **which accounting services** were used (`explanation.services`),
and **which transactions** contributed (`explanation.transactions`). Health
indicators and recommendations are equally transparent. No conclusion is a black
box; all are reproducible.

## 10. Accounting integrity

The intelligence layer computes **no accounting figure of its own**. Every
insight, health indicator, trend and knowledge record reads registry metrics
through `ReportContext`. Tests assert that every metric an insight names is a
registered metric and that an insight's headline value equals the corresponding
metric (e.g. operating deficit == total_income − operating_expense). Insight
*status* is the only thing persisted, keyed by fingerprint, never a figure — so
nothing can drift from the accounting truth.

## 11. Insight lifecycle

Detected (live, each request) → surfaced in the workspace/API with its explanation
→ optionally acknowledged / resolved / dismissed by a user (recorded in
`InsightStatus` + `InsightStatusHistory`) → dismissed insights are filtered from
the workspace but recomputed each run, so a recurring condition re-surfaces if it
persists after a period changes (the fingerprint includes the period).

## Files

New: `core/intelligence/` (`__init__.py`, `engine.py`, `modules.py`,
`recommendations.py`, `trends.py`, `health.py`, `knowledge.py`),
`core/models_intelligence.py`, `core/intelligence_views.py`,
`templates/intelligence/workspace.html`, migration
`core/0049_insightstatus_insightstatushistory.py`.

Modified: `core/models.py` (import intelligence models), `core/urls.py`
(workspace + analytics routes), `core/admin.py` (register status models),
`templates/base.html` (workspace nav link).
