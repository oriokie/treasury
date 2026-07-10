"""Knowledge-aware assistant support.

Extends the existing rule-based/LLM assistant so it can answer questions grounded
in the Financial Knowledge Service (Phase 9) — the Financial Metrics Registry,
Semantic Reporting Layer, Intelligence Engine, Narrative Engine, report snapshots
and dependency graph — WITHOUT ever recalculating a financial figure itself.

The existing ``assistant.answer`` handles the classic keyword questions. This
module adds:

* ``knowledge_context(report_key, period)`` — a compact, factual context block
  built entirely from the Knowledge Service (metrics + insights + recommendations
  + health + narrative), for the LLM to reason over or to return directly.
* ``answer_with_context(question, ctx_spec)`` — report-context-aware answering:
  given the report/period currently open, it resolves the reference ("this
  chart", "why is this score low", "which transactions make up this amount")
  against the Knowledge Service and returns a grounded answer with provenance.

Everything read here comes from the existing services; nothing is computed anew.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal


def _report_context(period):
    from core.reporting import ReportContext
    start = period.get("start") if period else None
    end = period.get("end") if period else None
    if isinstance(start, str):
        start = _dt.date.fromisoformat(start)
    if isinstance(end, str):
        end = _dt.date.fromisoformat(end)
    return ReportContext.for_period(start, end)


def knowledge_context(report_key=None, period=None, element=None):
    """Build a compact, factual context block from the Knowledge Service for the
    currently-open report/period (and optional focused element). Everything here
    is drawn from registered metrics/insights/narratives — no new calculation."""
    from core.intelligence import knowledge, IntelligenceEngine, compute_health_score
    rc = _report_context(period)

    lines = ["FINANCIAL KNOWLEDGE CONTEXT (all figures from the Financial Metrics "
             "Registry; do not recalculate):"]
    if report_key:
        lines.append(f"Report currently open: {report_key}.")
    if rc.start and rc.end:
        lines.append(f"Reporting period: {rc.start:%d %b %Y} to {rc.end:%d %b %Y}.")

    briefing = knowledge.full_briefing(rc)
    hs = briefing["health_score"]
    lines.append(f"Financial health score: {hs['overall']} / 100 ({hs['band']}).")
    lines.append("Health indicators: " + "; ".join(
        f"{i['label']} {i['score']:.0f} ({i['detail']})" for i in hs["indicators"]))

    # headline metrics
    for concept in ("income", "expenditure", "funds", "cash", "trust"):
        try:
            k = knowledge.knowledge_for(concept, rc)
            mvals = ", ".join(f"{mk}={_fmt(v)}" for mk, v in k["metrics"].items()
                              if not isinstance(v, (list, dict)))
            if mvals:
                lines.append(f"{concept.title()}: {mvals}.")
        except Exception:  # noqa: BLE001
            continue

    # insights + recommendations
    insights = briefing["insights"][:8]
    if insights:
        lines.append("Intelligence insights (with why they fired):")
        for i in insights:
            lines.append(f"  - [{i['severity']}] {i['title']}: {i['description']} "
                         f"(why: {i['explanation']['reason']}; "
                         f"metrics: {', '.join(i['explanation']['metrics'])})")
    recs = briefing["recommendations"][:6]
    if recs:
        lines.append("Priority recommendations: "
                     + "; ".join(r["action"] for r in recs))

    if element:
        lines.append(f"The user is asking specifically about: {element}.")
    lines.append("Disclaimer: insights and forecasts support but do not replace "
                 "the audited accounting figures.")
    return "\n".join(lines)


def structured_answer(report_key=None, period=None, element=None, intent=None):
    """Return a structured (non-LLM) answer from the Knowledge Service for a
    report element — used when the LLM is off, or to provide a deterministic
    grounded answer. Returns an assistant answer dict (text/rows/link)."""
    from core.intelligence import (knowledge, IntelligenceEngine,
                                   compute_health_score,
                                   recommendations_from_insights)
    rc = _report_context(period)

    intent = (intent or "").lower()
    # executive briefing (also surfaced in the report's AiBriefing section)
    if intent == "briefing" or "briefing" in (element or "").lower() \
            or "executive summary" in (element or "").lower():
        from reports.intelligence_components import AiBriefingComponent
        text = AiBriefingComponent()._briefing(rc)
        return {"text": text}

    # health score explanation
    if "score" in (element or "").lower() or intent == "health":
        hs = compute_health_score(rc)
        rows = [(i.label, f"{i.score:.0f}/100 — {i.detail}") for i in hs.indicators]
        return {"text": f"The Financial Health Score is {hs.overall:.0f}/100 "
                        f"({hs.band}). It is the weighted average of these "
                        "indicators, each computed from the metrics registry:",
                "rows": rows}

    # risk / insights explanation
    if intent in ("risk", "insights") or "risk" in (element or "").lower():
        insights = IntelligenceEngine().analyse(rc)
        rows = [(f"[{i.severity}] {i.title}", i.explanation.reason)
                for i in insights[:10]]
        return {"text": "These insights were detected, each traceable to the "
                        "metrics that triggered it:", "rows": rows}

    # recommendations
    if intent == "recommendations" or "recommend" in (element or "").lower():
        recs = recommendations_from_insights(IntelligenceEngine().analyse(rc))
        rows = [(f"({r.priority}) {r.action}", r.rationale[:80]) for r in recs[:10]]
        return {"text": "Board recommendations from the Intelligence Engine:",
                "rows": rows}

    # concept drill (income/expenditure/etc.)
    for concept in knowledge.concepts():
        if concept in (element or "").lower() or concept == intent:
            k = knowledge.knowledge_for(concept, rc)
            rows = [(mk, _fmt(v)) for mk, v in k["metrics"].items()
                    if not isinstance(v, (list, dict))]
            narr = (k.get("narrative") or {}).get("text", "")
            return {"text": narr or f"Figures for {concept}:", "rows": rows}

    # default: the full briefing headline
    hs = compute_health_score(rc)
    insights = IntelligenceEngine().analyse(rc)
    return {"text": f"Health score {hs.overall:.0f}/100 ({hs.band}); "
                    f"{len(insights)} active insight(s). Ask about the score, a "
                    "risk, a recommendation, or a specific figure.",
            "rows": [(f"[{i.severity}] {i.title}", i.explanation.reason)
                     for i in insights[:5]]}


def answer_with_context(question, report_key=None, period=None, element=None,
                        user=None):
    """Report-context-aware answering. Resolves the open report + period so the
    user never restates context, grounds the answer in the Knowledge Service, and
    uses the LLM (if enabled) with that grounded context — otherwise returns a
    structured knowledge answer. Provenance is always attached."""
    from core.models import SiteConfig
    from core.services import assistant as base

    cfg = SiteConfig.get()
    element_txt = element or ""

    # If the LLM is available, give it the grounded knowledge context so it can
    # phrase a natural answer WITHOUT inventing figures.
    if cfg.llm_enabled and cfg.llm_api_key:
        ctx_block = knowledge_context(report_key, period, element_txt)
        system = (
            "You are the treasurer's assistant embedded in a church finance "
            "report. Answer the user's question using ONLY the figures and "
            "insights in the KNOWLEDGE CONTEXT. Never invent or recalculate a "
            "number; if a figure isn't in the context, say so. When the user "
            "refers to 'this' (chart/score/figure/section), use the element they "
            "are focused on. Be concise and board-appropriate. Cite the metric "
            "names behind any figure you state.")
        q = question
        if element_txt:
            q = f"[Focused on: {element_txt}] {question}"
        text, err = base._llm_call(q, cfg, context=ctx_block, system=system)
        if text:
            return {"text": text.strip(),
                    "provenance": {"report": report_key, "grounded": True}}
        # fall through to structured answer on LLM error

    # No LLM (or it failed): deterministic structured answer from the Knowledge
    # Service, plus the classic rule engine as a secondary resolver.
    intent = _classify(question)
    ans = structured_answer(report_key, period, element_txt, intent)
    ans.setdefault("provenance", {"report": report_key, "grounded": True})
    return ans


def _classify(question):
    t = (question or "").lower()
    if "briefing" in t or "executive summary" in t or "brief the board" in t:
        return "briefing"
    if "score" in t or "health" in t:
        return "health"
    if "risk" in t or "concern" in t or "why" in t and "risk" in t:
        return "risk"
    if "recommend" in t or "should the board" in t or "action" in t:
        return "recommendations"
    for concept in ("income", "expenditure", "expense", "fund", "cash", "trust",
                    "budget", "loan"):
        if concept in t:
            return {"expense": "expenditure", "fund": "funds",
                    "loan": "loans"}.get(concept, concept)
    return ""


def _fmt(v):
    if isinstance(v, (int, float, Decimal)):
        return f"KES {float(v):,.0f}"
    return str(v)
