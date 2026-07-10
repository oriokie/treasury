"""Report components backed by the Financial Intelligence Platform.

These let the Generic Report Engine compose intelligence into any report:
* HealthScoreComponent — the Financial Health Score with its transparent
  indicator drill-down (from core.intelligence.compute_health_score).
* InsightsComponent — the prioritised intelligence insights with explanations.
* RecommendationsComponent — board recommendations derived from insights.
* AiBriefingComponent — an AI executive narrative when the LLM is enabled,
  otherwise the deterministic narrative/intelligence summary. Never invents a
  figure; every value comes from the metrics registry via ReportContext.

All read only the ReportContext / intelligence layer — no accounting calculation
is duplicated.
"""
from __future__ import annotations

from core.reporting.components import ComponentSection
from core.reporting.engine import Column, Row, SectionData


class HealthScoreComponent(ComponentSection):
    key = "health_score"
    title = "Financial health score"
    declared_metrics = ("fund_summary", "operating_expense", "total_income",
                        "income_by_channel", "trust_to_remit",
                        "unpresented_payments_total", "pending_receipts_total")

    def render(self, ctx, filters):
        from core.intelligence import compute_health_score
        hs = compute_health_score(ctx)
        cols = [Column("indicator", "Indicator"),
                Column("score", "Score", numeric=True),
                Column("weight", "Weight", numeric=True),
                Column("detail", "Basis")]
        rows = [Row(cells={"indicator": i.label, "score": round(i.score, 0),
                           "weight": i.weight, "detail": i.detail})
                for i in hs.indicators]
        total = Row(cells={"indicator": f"OVERALL — {hs.band}",
                           "score": round(hs.overall, 0), "weight": "",
                           "detail": "Weighted average of the indicators above."},
                    emphasis=True)
        return SectionData(key=self.key, title=self.title, columns=cols, rows=rows,
                           total=total, kind="table",
                           note=f"Overall financial health: {hs.overall:.0f}/100 "
                                f"({hs.band}). Every indicator is computed from the "
                                "Financial Metrics Registry.")


class InsightsComponent(ComponentSection):
    key = "insights"
    title = "Intelligence insights"
    declared_metrics = ()

    def __init__(self, key=None, title=None, layout=None, permission=None,
                 min_priority=0, limit=12):
        super().__init__(key=key or "insights", title=title, layout=layout,
                         permission=permission)
        self._min_priority = min_priority
        self._limit = limit

    def render(self, ctx, filters):
        from core.intelligence import IntelligenceEngine
        insights = [i for i in IntelligenceEngine().analyse(ctx)
                    if i.priority >= self._min_priority][:self._limit]
        if not insights:
            return SectionData(key=self.key, title=self.title, columns=[], rows=[],
                               kind="info",
                               extra={"text": "No material insights this period."})
        cols = [Column("severity", "Severity"), Column("insight", "Insight"),
                Column("why", "Why it fired")]
        rows = [Row(cells={"severity": i.severity.upper(), "insight": i.title,
                           "why": i.explanation.reason},
                    emphasis=i.severity in ("critical", "warning"))
                for i in insights]
        return SectionData(key=self.key, title=self.title, columns=cols, rows=rows,
                           kind="table",
                           note="Each insight is explainable and traces to the "
                                "metrics that triggered it.")


class RecommendationsComponent(ComponentSection):
    key = "board_recommendations"
    title = "Recommendations for the board"
    declared_metrics = ()

    def render(self, ctx, filters):
        from core.intelligence import (IntelligenceEngine,
                                       recommendations_from_insights)
        recs = recommendations_from_insights(IntelligenceEngine().analyse(ctx))[:10]
        if not recs:
            return SectionData(key=self.key, title=self.title, columns=[], rows=[],
                               kind="info",
                               extra={"text": "No specific actions are "
                                      "recommended; controls appear to be "
                                      "operating normally."})
        cols = [Column("priority", "Priority", numeric=True),
                Column("action", "Recommended action"),
                Column("rationale", "Rationale")]
        rows = [Row(cells={"priority": r.priority, "action": r.action,
                           "rationale": r.rationale[:120]}) for r in recs]
        return SectionData(key=self.key, title=self.title, columns=cols, rows=rows,
                           kind="table")


class AiBriefingComponent(ComponentSection):
    """An AI-written executive briefing when the LLM is enabled; otherwise the
    deterministic executive-summary narrative + top recommendations. Always
    grounded in the metrics registry — the AI is instructed to use only the
    provided figures."""
    key = "ai_briefing"
    title = "Executive briefing"
    declared_metrics = ("total_income", "operating_expense", "fund_summary",
                        "trust_to_remit")

    def render(self, ctx, filters):
        text = self._briefing(ctx)
        return SectionData(key=self.key, title=self.title, columns=[], rows=[],
                           kind="commentary", extra={"text": text})

    def _briefing(self, ctx):
        from core.models import SiteConfig
        cfg = SiteConfig.get()
        # AI path
        if getattr(cfg, "llm_enabled", False) and getattr(cfg, "llm_api_key", ""):
            try:
                from core.services.assistant_knowledge import knowledge_context
                from core.services import assistant as base
                context = knowledge_context("treasurer_report",
                                            {"start": ctx.start, "end": ctx.end})
                system = (
                    "You are a church treasurer writing the executive briefing for "
                    "the board's financial report. Using ONLY the figures and "
                    "insights in the knowledge context, write a concise briefing "
                    "with these headers on their own lines: 'Overview:', "
                    "'Key insights:', 'Recommendations:'. Cite specific figures; "
                    "never invent a number. Keep under 260 words.")
                text, err = base._llm_call("Write the board executive briefing.",
                                           cfg, context=context, system=system)
                if text:
                    return text.strip()
            except Exception:  # noqa: BLE001
                pass
        # deterministic fallback: narrative + recommendations
        from core.reporting.narrative import NarrativeEngine
        from core.intelligence import (IntelligenceEngine,
                                       recommendations_from_insights)
        summary = NarrativeEngine().generate("executive_summary", ctx).text
        recs = recommendations_from_insights(IntelligenceEngine().analyse(ctx))[:4]
        rec_txt = ""
        if recs:
            rec_txt = "\n\nRecommendations: " + "; ".join(r.action for r in recs)
        return summary + rec_txt


def register_components():
    """Register the intelligence components with the component registry so
    reports (and the designer) can compose them."""
    from core.reporting.components import component_registry as reg
    reg.register("health_score",
                 lambda **k: HealthScoreComponent(**k),
                 label="Financial health score", category="Intelligence")
    reg.register("insights",
                 lambda **k: InsightsComponent(**k),
                 label="Intelligence insights", category="Intelligence")
    reg.register("board_recommendations",
                 lambda **k: RecommendationsComponent(**k),
                 label="Board recommendations", category="Intelligence")
    reg.register("ai_briefing",
                 lambda **k: AiBriefingComponent(**k),
                 label="AI executive briefing", category="Intelligence")
