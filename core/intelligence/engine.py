"""Financial Intelligence Engine — core.

Elevates raw accounting figures into *structured, explainable insights*. An
``Insight`` is a first-class object (not free text): it carries a severity,
category, confidence, priority, the supporting metrics/funds/period, suggested
actions, and — crucially — an **explanation** of exactly why it fired (which
metrics, which thresholds, which values). Every insight is traceable to the
Financial Metrics Registry via the metrics it read; nothing here computes an
accounting figure of its own.

Design
------
* **Insight** — the structured finding (Part 1's full field set), with an
  ``Explanation`` recording the trigger (Part 9: no black boxes).
* **IntelligenceModule** — a reusable detector. A subclass declares its category
  and ``declared_metrics`` and implements ``evaluate(ctx, cfg) -> list[Insight]``,
  reading figures only from the ``ReportContext`` (Semantic Reporting Layer).
* **IntelligenceEngine** — runs registered modules against one shared context and
  returns all insights, deterministically. It records provenance (metrics/services
  used) so an AI or auditor can see the whole derivation.
* **Thresholds/config** — every trigger level is configurable; detection reads
  these, so an insight is reproducible given (figures, config).

Determinism: an insight is a pure function of (context figures, config). No
randomness, no time dependence beyond the context period. Re-running yields
identical insights.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal


# ===========================================================================
# Vocabulary
# ===========================================================================

class Severity:
    INFO = "info"
    NOTICE = "notice"
    WARNING = "warning"
    CRITICAL = "critical"
    _ORDER = {INFO: 0, NOTICE: 1, WARNING: 2, CRITICAL: 3}

    @classmethod
    def rank(cls, sev):
        return cls._ORDER.get(sev, 0)


class Category:
    HEALTH = "Financial health"
    INCOME = "Income intelligence"
    EXPENSE = "Expense intelligence"
    FUND = "Fund intelligence"
    CASH = "Cash intelligence"
    OPERATIONAL = "Operational intelligence"
    ASSET_LIABILITY = "Asset & liability intelligence"


class Status:
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


# ===========================================================================
# Explanation — Part 9 (no black boxes)
# ===========================================================================

@dataclass
class Explanation:
    """Why an insight fired: the reason, the metrics that triggered it, the
    thresholds exceeded, and the accounting services behind those metrics.
    Everything needed to reproduce and audit the conclusion."""
    reason: str = ""
    metrics: list = field(default_factory=list)       # metric keys read
    thresholds: dict = field(default_factory=dict)    # name -> {limit, actual}
    services: list = field(default_factory=list)      # accounting services used
    transactions: list = field(default_factory=list)  # supporting txn ids

    def as_dict(self):
        return {"reason": self.reason, "metrics": list(self.metrics),
                "thresholds": dict(self.thresholds),
                "services": list(self.services),
                "transactions": list(self.transactions)}


# ===========================================================================
# Insight — Part 1
# ===========================================================================

@dataclass
class Insight:
    code: str                                    # stable id, e.g. "budget_overrun"
    title: str
    description: str
    severity: str = Severity.NOTICE
    category: str = Category.HEALTH
    confidence: float = 1.0                      # 0..1
    priority: int = 50                           # 0..100 (derived if not set)

    supporting_metrics: list = field(default_factory=list)
    supporting_transactions: list = field(default_factory=list)
    affected_funds: list = field(default_factory=list)
    affected_departments: list = field(default_factory=list)

    period_start: _dt.date = None
    period_end: _dt.date = None

    suggested_actions: list = field(default_factory=list)
    explanation: Explanation = field(default_factory=Explanation)

    value: Decimal = Decimal("0")                # the headline figure
    subject: str = ""                            # fund/department/category name

    status: str = Status.OPEN
    detected_at: _dt.datetime = None

    def __post_init__(self):
        # derive a priority from severity + confidence if left at default
        if self.priority == 50:
            self.priority = min(100, int(
                Severity.rank(self.severity) * 25 + self.confidence * 20))
        # keep the explanation's metric list aligned with supporting_metrics
        if not self.explanation.metrics and self.supporting_metrics:
            self.explanation.metrics = list(self.supporting_metrics)

    @property
    def fingerprint(self):
        """Stable identity for de-dup / status tracking across runs: code +
        subject + period."""
        return f"{self.code}:{self.subject}:{self.period_start}:{self.period_end}"

    def as_dict(self):
        return {
            "code": self.code, "title": self.title,
            "description": self.description, "severity": self.severity,
            "category": self.category, "confidence": self.confidence,
            "priority": self.priority,
            "supporting_metrics": list(self.supporting_metrics),
            "supporting_transactions": list(self.supporting_transactions),
            "affected_funds": list(self.affected_funds),
            "affected_departments": list(self.affected_departments),
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "suggested_actions": list(self.suggested_actions),
            "explanation": self.explanation.as_dict(),
            "value": float(self.value) if self.value is not None else None,
            "subject": self.subject, "status": self.status,
            "fingerprint": self.fingerprint,
        }


# ===========================================================================
# Configuration
# ===========================================================================

@dataclass
class IntelligenceConfig:
    """Configurable trigger levels shared across modules. Every threshold that
    governs whether an insight fires lives here, so insights are reproducible
    and tunable without touching detection code."""
    budget_overrun_pct: float = 10.0          # over budget beyond this %
    large_movement_pct: float = 25.0          # period-on-period change
    income_decline_pct: float = 15.0          # income drop that concerns
    low_reserve_months: float = 3.0           # reserve months below this = risk
    cash_runway_months: float = 2.0           # runway below this = warning
    dormant_days: int = 120                   # fund inactive beyond this
    ageing_days: int = 90                     # advances/items older than this
    material_amount: Decimal = Decimal("1000")
    income_concentration_pct: float = 60.0    # one source above this share
    min_confidence: float = 0.0               # drop insights below this

    def as_dict(self):
        return {k: (float(v) if isinstance(v, Decimal) else v)
                for k, v in self.__dict__.items()}


# ===========================================================================
# Module base + registry
# ===========================================================================

class IntelligenceModule:
    """Base class for a reusable intelligence module. Subclasses set ``key``,
    ``category`` and ``declared_metrics``, and implement
    ``evaluate(ctx, cfg) -> list[Insight]``. Figures come only from ``ctx``."""
    key: str = ""
    category: str = Category.HEALTH
    declared_metrics: tuple = ()

    def evaluate(self, ctx, cfg) -> list:  # pragma: no cover
        raise NotImplementedError

    # ---- helpers for subclasses -------------------------------------------
    def insight(self, ctx, **kw):
        """Build an Insight pre-filled with the period and this module's
        category, and record the metrics used since evaluation started."""
        kw.setdefault("category", self.category)
        kw.setdefault("period_start", ctx.start)
        kw.setdefault("period_end", ctx.end)
        return Insight(**kw)


class IntelligenceRegistry:
    def __init__(self):
        self._modules = {}

    def register(self, module):
        if module.key in self._modules:
            raise ValueError(f"Module '{module.key}' already registered.")
        self._modules[module.key] = module
        return module

    def all(self):
        return list(self._modules.values())

    def get(self, key):
        return self._modules.get(key)

    def keys(self):
        return sorted(self._modules)


intelligence_registry = IntelligenceRegistry()


# ===========================================================================
# Engine
# ===========================================================================

class IntelligenceEngine:
    """Runs intelligence modules against one shared ReportContext and returns
    structured insights. Deterministic and fully explainable: each insight
    records the metrics/thresholds/services behind it, and the engine records
    the union across all modules for provenance."""

    def __init__(self, config: IntelligenceConfig = None):
        self.config = config or IntelligenceConfig()

    def analyse(self, ctx, modules=None, config=None):
        """Evaluate every module (or a subset) against ``ctx``. Returns a list
        of Insights sorted by priority (desc) then severity."""
        cfg = config or self.config
        mods = modules if modules is not None else intelligence_registry.all()
        insights = []
        for module in mods:
            before = list(ctx.metrics_used())
            produced = module.evaluate(ctx, cfg) or []
            after = ctx.metrics_used()
            newly = [m for m in after if m not in before]
            for ins in produced:
                # backfill provenance if the module didn't set it
                if not ins.supporting_metrics:
                    ins.supporting_metrics = list(module.declared_metrics or newly)
                if not ins.explanation.metrics:
                    ins.explanation.metrics = list(ins.supporting_metrics)
                if ins.detected_at is None:
                    ins.detected_at = _dt.datetime.now()
                insights.append(ins)
        # confidence floor
        insights = [i for i in insights if i.confidence >= cfg.min_confidence]
        insights.sort(key=lambda i: (-i.priority, -Severity.rank(i.severity)))
        return insights

    def provenance(self, ctx):
        """Metrics and services the analysis touched — for AI/audit."""
        from core.reporting import build_dependency_map  # noqa: F401 (optional)
        return {"metrics": ctx.metrics_used()}
