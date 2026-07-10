# Treasurer's Report + Report-Aware AI Assistant

*The flagship board report, rebuilt on the Generic Report Engine and enriched with
the Financial Intelligence Platform, integrated with the existing AI assistant so
every section is explainable through natural-language questions — while preserving
a single authoritative source of financial truth (the Financial Metrics Registry).*

---

## 1. The existing assistant, extended (not replaced)

The application already had a chatbot: `core/services/assistant.py` (rule-based
answers + an optional provider-agnostic LLM), the `/assistant/` page and
`/assistant/ask/` endpoint. Rather than adding a second chatbot, this phase
extended it to consume the **Financial Knowledge Service** (Phase 9), which itself
reads only the Metrics Registry, Semantic Reporting Layer, Intelligence Engine,
Narrative Engine, snapshots and dependency graph.

New module `core/services/assistant_knowledge.py`:

* `knowledge_context(report_key, period, element)` — a compact, factual context
  block assembled entirely from `knowledge.full_briefing` / `knowledge_for`
  (health score, headline metrics, insights with their explanations, priority
  recommendations). No figure is recomputed.
* `structured_answer(...)` — a deterministic, grounded answer from the Knowledge
  Service (health-score breakdown, risks/insights, recommendations, a concept's
  figures, or the executive briefing). Used when the LLM is off.
* `answer_with_context(question, report_key, period, element)` — the report-aware
  entry point. When the LLM is enabled it is given the grounded context and a
  system prompt that **forbids inventing or recalculating figures**; when off, it
  returns the structured knowledge answer. Provenance is always attached.

**The assistant never calculates a financial figure itself** — it only relays what
the Knowledge Service (i.e. the registry) provides.

## 2. Report-aware AI (persistent context)

The `/assistant/ask/` endpoint now accepts an optional `{report_key, period,
element}` payload; when present it routes to `answer_with_context`, otherwise to
the classic `assistant.answer`. The `/assistant/` page reads `report_key`, `start`,
`end`, `element` and `q` from the URL, shows a **context banner** ("Answering about
<report> · <section> · <period>"), and includes that context in every question it
sends — so the user can ask "why did income decrease?", "explain this chart", "why
is this score low?", "which transactions make up this amount?" without restating
what they are looking at. A "use general mode" control clears the context.

## 3. Ask-AI throughout the report

The Generic Report Engine's HTML template now renders:

* a top **"✨ Ask AI about this report"** button (opens the assistant with the
  report key + period), and
* a per-section **"✨ Ask AI"** link in every section header (adds
  `&element=<section title>`).

Selecting any of these opens the existing assistant already aware of the report,
period and section — verified end-to-end (the banner shows the context and the
grounded answer path is used). Because this lives in the shared engine template,
**every** engine report gains report-aware AI, not just the Treasurer's Report.

## 4. The Treasurer's Report (`reports/treasurer_report.py`)

A comprehensive, board-ready report composed **only** from the Generic Report
Engine, reusable components, the Metrics Registry, ReportContext, the Narrative
Engine and the Intelligence Platform — no accounting calculation is added or
modified. Sections, in order:

1. **AI executive briefing** — `AiBriefingComponent`: an LLM-written board briefing
   when enabled (grounded in the knowledge context, instructed to use only the
   provided figures), else the deterministic executive-summary narrative + top
   recommendations.
2. **Financial health score** — the transparent 9-indicator score with drill-down.
3. **KPI cards** — headline metrics.
4. **Charts** — income by channel, fund balances (screen only).
5. **Income & expenditure** — income & expense summaries, the Income &
   Expenditure statement, income/expense narratives.
6. **Funds & cash** — the Statement of Fund Balances, cash position, fund-
   performance narrative.
7. **Budget** — budget summary + variance narrative.
8. **Trust & compliance** — trust-funds and development-projects narratives.
9. **Reconciliation** — bank-reconciliation summary + outstanding items.
10. **Oversight** — intelligence insights (explained) + board recommendations.
11. **Notes & signatures** — provenance/disclaimer info panel + signature block.

Every section carries an Ask-AI affordance. The report is registered as
`treasurer_report` and linked prominently from the reports index; it renders and
exports to CSV, Excel, PDF and Word (all verified).

## 5. Intelligence report components (`reports/intelligence_components.py`)

Four reusable components register with the component registry (category
"Intelligence"), so the Treasurer's Report — and the Report Designer — can compose
them: `HealthScoreComponent`, `InsightsComponent`, `RecommendationsComponent`,
`AiBriefingComponent`. Each reads only the ReportContext / intelligence layer.

## 6. Output formats

The engine's renderer framework already produces format-appropriate output: the
HTML view is an interactive dashboard (grid layout, per-section Ask-AI, charts,
print button); CSV/Excel are the structured tables; PDF and Word are branded
documents (Phase 8 branding). Charts and the info panel are screen-only
(`export_visible=False` / `print_visible` set via `LayoutMeta`), while the
identical figures flow to every format because they all render the same
`SectionData`. The AI executive briefing appears both in the report (as a section)
and in the assistant (ask "brief the board" / "executive briefing").

## 7. Validation

* **Every figure originates from the Metrics Registry** — components read
  ReportContext metrics; the Treasurer's Report's declared metrics are all
  registered (tested). No new calculation is introduced.
* **No duplicate calculations** — the intelligence components and the assistant
  reuse the registry/knowledge layer; nothing recomputes a balance.
* **Charts match figures** — charts are built by the ChartEngine from the same
  metrics the tables use.
* **Narratives match metrics** — narratives come from the Narrative Engine, which
  is metric-sourced.
* **AI explanations reference the same records** — the assistant answers only from
  the knowledge context (registry metrics + insights), and every insight records
  the metrics/thresholds/services behind it.
* **Recommendations come from the Intelligence Engine** — via
  `recommendations_from_insights`.
* **The assistant answers contextual questions without recalculating** — verified:
  with the LLM off it returns structured knowledge answers; with it on, the system
  prompt forbids inventing figures.

## Files

New: `core/services/assistant_knowledge.py`, `reports/intelligence_components.py`,
`reports/treasurer_report.py`, `core/test_treasurer_report.py`.

Modified: `core/views.py` (AssistantView reads context; AssistantAskView routes to
knowledge-aware answering), `templates/assistant.html` (context banner + payload +
prefill), `templates/reports/engine_report.html` (Ask-AI top button + per-section
links), `templates/reports/index.html` (Treasurer's Report link), `reports/apps.py`
(register intelligence components + treasurer report).

No migration — the phase is additive code + templates only.
