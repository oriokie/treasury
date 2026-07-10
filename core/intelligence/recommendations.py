"""Recommendation Engine.

Turns structured insights into prioritised, actionable recommendations. A
recommendation is derived deterministically from an insight's suggested actions
and severity — it never invents a figure. Recommendations are dismissible with an
audit trail (persisted separately; see reports/models_intelligence or the
workspace views), and each carries the fingerprint of the insight it came from so
its provenance and the metrics behind it remain traceable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.intelligence.engine import Severity


@dataclass
class Recommendation:
    code: str                       # insight code it derives from
    action: str                     # the recommended action text
    priority: int                   # 0..100
    severity: str
    subject: str = ""
    rationale: str = ""             # why (from the insight)
    supporting_metrics: list = field(default_factory=list)
    insight_fingerprint: str = ""

    def as_dict(self):
        return {"code": self.code, "action": self.action,
                "priority": self.priority, "severity": self.severity,
                "subject": self.subject, "rationale": self.rationale,
                "supporting_metrics": list(self.supporting_metrics),
                "insight_fingerprint": self.insight_fingerprint}


def recommendations_from_insights(insights):
    """Derive prioritised recommendations from a list of insights. Each of an
    insight's suggested actions becomes a recommendation carrying the insight's
    priority, severity, subject, rationale and supporting metrics — so a
    recommendation is always explainable back to the metric that triggered it.
    De-duplicated on (code, action, subject)."""
    recs = []
    seen = set()
    for ins in insights:
        actions = ins.suggested_actions or []
        if not actions:
            continue
        for action in actions:
            fp = (ins.code, action, ins.subject)
            if fp in seen:
                continue
            seen.add(fp)
            recs.append(Recommendation(
                code=ins.code, action=action, priority=ins.priority,
                severity=ins.severity, subject=ins.subject,
                rationale=ins.description,
                supporting_metrics=list(ins.supporting_metrics),
                insight_fingerprint=ins.fingerprint))
    recs.sort(key=lambda r: (-r.priority, -Severity.rank(r.severity)))
    return recs
