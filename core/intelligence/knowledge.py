"""Financial Knowledge Service — the reusable knowledge layer a future AI
Treasurer (or any analytics consumer) reads from through one interface.

For any financial *concept* it assembles, from the existing architecture and
without duplicating a calculation: the canonical metric value, the relevant
narrative, the intelligence findings, the recommendations, the supporting reports
and transactions, the supporting snapshots, and the metric→service dependency
graph. This is Part 5 — the structured backend, NOT a chatbot. It exposes a
single ``knowledge_for`` / ``full_briefing`` interface so all future features
(mobile, AI assistant, executive summaries) consume identical, explainable data.
"""
from __future__ import annotations

import datetime as _dt


# Map a concept name to the metric(s), narrative and reports that explain it.
# Concepts are stable, human-facing handles over the registry.
CONCEPTS = {
    "income": {
        "metrics": ["total_income", "tithe", "income_by_channel"],
        "narrative": "income_analysis",
        "reports": ["income_statement_v2"],
    },
    "expenditure": {
        "metrics": ["operating_expense", "capital_expenditure", "expense_by_category"],
        "narrative": "expense_analysis",
        "reports": ["income_statement_v2"],
    },
    "funds": {
        "metrics": ["fund_summary"],
        "narrative": "fund_performance",
        "reports": ["fund_balances_v2"],
    },
    "cash": {
        "metrics": ["fund_summary", "unpresented_payments_total"],
        "narrative": "cash_position",
        "reports": ["cash_flow_v2"],
    },
    "trust": {
        "metrics": ["trust_summary", "trust_to_remit", "remittances_total"],
        "narrative": "trust_funds",
        "reports": ["fund_balances_v2"],
    },
    "budget": {
        "metrics": ["fund_summary"],
        "narrative": "budget_variance",
        "reports": ["budget_vs_actual_v2"],
    },
    "loans": {
        "metrics": ["loans_outstanding", "financing_activity"],
        "narrative": "loan_position",
        "reports": [],
    },
    "position": {
        "metrics": ["fund_summary", "loans_outstanding", "trust_to_remit"],
        "narrative": "asset_position",
        "reports": ["financial_position_v2"],
    },
}


def concepts():
    return sorted(CONCEPTS)


def _metric_definition(metric_key):
    from core.metrics import metrics
    m = metrics.registry.get(metric_key)
    return {"key": metric_key, "label": getattr(m, "label", metric_key),
            "definition": getattr(m, "definition", ""),
            "authoritative": getattr(m, "authoritative", "")} if m else None


def knowledge_for(concept, ctx, *, config=None):
    """Assemble the full knowledge record for one concept over ``ctx``'s period.
    Everything is drawn from the registry/narrative/intelligence layers — no new
    calculation. Returns a JSON-safe dict."""
    spec = CONCEPTS.get(concept)
    if spec is None:
        raise KeyError(f"Unknown concept '{concept}'. Known: {', '.join(concepts())}.")

    from core.reporting.narrative import NarrativeEngine
    from core.intelligence.engine import IntelligenceEngine
    from core.intelligence.recommendations import recommendations_from_insights

    # metric values + definitions
    metric_values = {}
    definitions = {}
    for mk in spec["metrics"]:
        try:
            metric_values[mk] = _jsonable(ctx.metric(mk))
        except Exception:  # noqa: BLE001
            metric_values[mk] = None
        definitions[mk] = _metric_definition(mk)

    # narrative
    narrative = None
    if spec.get("narrative"):
        try:
            nr = NarrativeEngine().generate(spec["narrative"], ctx)
            narrative = {"text": nr.text, "metrics_used": nr.metrics_used,
                         "findings": [f.as_dict() for f in nr.findings]}
        except Exception:  # noqa: BLE001
            narrative = None

    # intelligence findings + recommendations relevant to this concept
    insights = IntelligenceEngine(config).analyse(ctx)
    concept_metrics = set(spec["metrics"])
    relevant = [i for i in insights
                if concept_metrics & set(i.supporting_metrics)]
    recs = recommendations_from_insights(relevant)

    # dependency graph (metric -> authoritative service)
    dep_graph = {mk: (definitions[mk] or {}).get("authoritative", "")
                 for mk in spec["metrics"]}

    # supporting snapshots for the linked reports
    snapshots = _snapshots_for(spec.get("reports", []), ctx)

    return {
        "concept": concept,
        "period": {"start": ctx.start.isoformat() if ctx.start else None,
                   "end": ctx.end.isoformat() if ctx.end else None},
        "metrics": metric_values,
        "definitions": definitions,
        "narrative": narrative,
        "insights": [i.as_dict() for i in relevant],
        "recommendations": [r.as_dict() for r in recs],
        "reports": spec.get("reports", []),
        "snapshots": snapshots,
        "dependency_graph": dep_graph,
    }


def full_briefing(ctx, *, config=None):
    """A complete briefing across every concept plus the health score and the
    top insights/recommendations — the single call an AI Treasurer or executive
    summary would consume."""
    from core.intelligence.engine import IntelligenceEngine
    from core.intelligence.recommendations import recommendations_from_insights
    from core.intelligence.health import compute_health_score

    insights = IntelligenceEngine(config).analyse(ctx)
    recs = recommendations_from_insights(insights)
    health = compute_health_score(ctx, config)
    return {
        "period": {"start": ctx.start.isoformat() if ctx.start else None,
                   "end": ctx.end.isoformat() if ctx.end else None},
        "health_score": health.as_dict(),
        "insights": [i.as_dict() for i in insights],
        "recommendations": [r.as_dict() for r in recs],
        "concepts": concepts(),
        "provenance": {"metrics_used": ctx.metrics_used()},
        "generated_at": _dt.datetime.now().isoformat(),
        "disclaimer": "Insights and forecasts are analytical aids derived from "
                      "the Financial Metrics Registry; they support but do not "
                      "replace the audited accounting figures.",
    }


def _snapshots_for(report_keys, ctx):
    try:
        from reports.models import ReportSnapshot
        qs = ReportSnapshot.objects.filter(report_key__in=report_keys)
        if ctx.end:
            qs = qs.filter(period_end=ctx.end)
        return [{"id": s.id, "report_key": s.report_key,
                 "generated_at": s.generated_at.isoformat(),
                 "checksum": s.checksums.get("payload", "")}
                for s in qs[:10]]
    except Exception:  # noqa: BLE001
        return []


def _jsonable(v):
    from decimal import Decimal
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    # fund_summary rows contain model instances — reduce to a name/number view
    if hasattr(v, "name"):
        return str(v)
    return v
