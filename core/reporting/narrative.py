"""Financial Narrative Engine.

Generates deterministic, reproducible financial commentary that consumes **only**
the Semantic Reporting Layer (``ReportContext`` → Financial Metrics Registry).
A narrative never contains a hardcoded financial value or its own accounting
logic: it asks the context for registered metrics and renders words around them.
The same figures that appear in a report's tables therefore drive its prose, so
commentary can never contradict the statements.

Design
------
* **Narrative** — the reusable unit. A subclass implements
  ``generate(ctx, cfg) -> NarrativeResult``, reading figures from ``ctx`` and
  returning text plus the metrics it used (provenance) and any *findings*
  (detected conditions: variances, overruns, shortages, …). Narratives are
  registered in the ``narrative_registry`` so reports/dashboards/exports/AI pick
  them by key.
* **NarrativeConfig** — style (executive/treasurer/auditor/committee/detailed/
  concise), tone (informational/analytical/formal/executive_summary) and a
  ``Thresholds`` bundle. Style/tone affect phrasing and verbosity, never the
  numbers. All defaults are fixed, so output is deterministic for a given
  context + config.
* **Thresholds** — configurable trigger levels (variance %, ageing days, low-cash
  floor, large-movement %). Detection reads these; changing a threshold changes
  which findings fire, never the underlying figures.
* **Findings** — a structured record of a detected condition (severity, message,
  metric, value). They power warnings/exceptions/recommendations narratives and
  can be surfaced to a future AI layer as machine-readable signals.

Determinism: every narrative is a pure function of (ReportContext figures,
config). No randomness, no time-of-day dependence beyond the context's period,
no LLM call. Re-running yields byte-identical text.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


# ===========================================================================
# Configuration
# ===========================================================================

class Style:
    EXECUTIVE = "executive"
    TREASURER = "treasurer"
    AUDITOR = "auditor"
    COMMITTEE = "committee"
    DETAILED = "detailed"
    CONCISE = "concise"
    ALL = (EXECUTIVE, TREASURER, AUDITOR, COMMITTEE, DETAILED, CONCISE)


class Tone:
    INFORMATIONAL = "informational"
    ANALYTICAL = "analytical"
    FORMAL = "formal"
    EXECUTIVE_SUMMARY = "executive_summary"
    ALL = (INFORMATIONAL, ANALYTICAL, FORMAL, EXECUTIVE_SUMMARY)


@dataclass
class Thresholds:
    """Configurable trigger levels for condition detection. Defaults are
    deliberately conservative and fixed (deterministic)."""
    variance_pct: float = 10.0          # budget over/under beyond this is flagged
    large_movement_pct: float = 25.0    # period-on-period change beyond this
    ageing_days: int = 90               # items older than this are "ageing"
    low_cash_floor: Decimal = Decimal("0")   # closing cash at/below this = shortage
    material_amount: Decimal = Decimal("1000")   # ignore movements below this
    inactive_no_receipts: bool = True   # a fund with 0 receipts is "inactive"

    def as_dict(self):
        return {"variance_pct": self.variance_pct,
                "large_movement_pct": self.large_movement_pct,
                "ageing_days": self.ageing_days,
                "low_cash_floor": float(self.low_cash_floor),
                "material_amount": float(self.material_amount),
                "inactive_no_receipts": self.inactive_no_receipts}


@dataclass
class NarrativeConfig:
    style: str = Style.TREASURER
    tone: str = Tone.INFORMATIONAL
    thresholds: Thresholds = field(default_factory=Thresholds)

    def verbose(self):
        return self.style in (Style.DETAILED, Style.AUDITOR, Style.COMMITTEE)

    def terse(self):
        return self.style in (Style.CONCISE, Style.EXECUTIVE) \
            or self.tone == Tone.EXECUTIVE_SUMMARY


# ===========================================================================
# Findings & results
# ===========================================================================

class Severity:
    INFO = "info"
    NOTICE = "notice"
    WARNING = "warning"
    CRITICAL = "critical"
    _ORDER = {INFO: 0, NOTICE: 1, WARNING: 2, CRITICAL: 3}


@dataclass
class Finding:
    """A detected condition. Structured so warnings/exceptions/recommendations
    narratives and a future AI layer can consume the same signal."""
    code: str                       # e.g. "budget_overrun"
    severity: str                   # Severity.*
    message: str                    # human sentence
    metric: str = ""                # metric that surfaced it
    value: Decimal = Decimal("0")
    subject: str = ""               # fund / category name, if any

    def as_dict(self):
        return {"code": self.code, "severity": self.severity,
                "message": self.message, "metric": self.metric,
                "value": float(self.value) if self.value is not None else None,
                "subject": self.subject}


@dataclass
class NarrativeResult:
    key: str
    title: str
    text: str
    metrics_used: list = field(default_factory=list)
    findings: list = field(default_factory=list)     # list[Finding]

    def as_dict(self):
        return {"key": self.key, "title": self.title, "text": self.text,
                "metrics_used": self.metrics_used,
                "findings": [f.as_dict() for f in self.findings]}


# ===========================================================================
# Formatting helpers (shared, so phrasing is consistent)
# ===========================================================================

def kes(value):
    """Format a money value as 'KES 12,345'. Deterministic."""
    v = value or 0
    if isinstance(v, Decimal):
        v = float(v)
    return f"KES {v:,.0f}"


def pct(part, whole):
    if not whole:
        return 0.0
    return round(float(part) / float(whole) * 100, 1)


def _period_phrase(ctx):
    if ctx.start and ctx.end:
        return f"the period {ctx.start:%d %b %Y} to {ctx.end:%d %b %Y}"
    return "the period to date"


# ===========================================================================
# Narrative base + registry
# ===========================================================================

class Narrative:
    """Base class for a reusable narrative. Subclasses set ``key``/``title`` and
    implement ``generate(ctx, cfg) -> NarrativeResult``. Figures come only from
    ``ctx``; ``cfg`` chooses style/tone/thresholds."""
    key: str = ""
    title: str = ""

    def generate(self, ctx, cfg) -> NarrativeResult:  # pragma: no cover
        raise NotImplementedError

    # helper for subclasses: assemble a result, capturing provenance
    def result(self, ctx, text, findings=None, before=None):
        used = ctx.metrics_used()
        if before is not None:
            used = [m for m in used if m not in before]
        return NarrativeResult(key=self.key, title=self.title, text=text.strip(),
                               metrics_used=used, findings=findings or [])


class NarrativeRegistry:
    def __init__(self):
        self._narratives: dict[str, Narrative] = {}

    def register(self, narrative: Narrative):
        if narrative.key in self._narratives:
            raise ValueError(f"Narrative '{narrative.key}' already registered.")
        self._narratives[narrative.key] = narrative
        return narrative

    def get(self, key):
        return self._narratives.get(key)

    def all(self):
        return sorted(self._narratives.values(), key=lambda n: n.title)

    def keys(self):
        return sorted(self._narratives)


narrative_registry = NarrativeRegistry()


class NarrativeEngine:
    """Facade for generating narratives. Given a ReportContext and a config,
    produces one or many narratives by key, each deterministic and drawn from
    the registry."""

    def __init__(self, config: NarrativeConfig = None):
        self.config = config or NarrativeConfig()

    def generate(self, key, ctx, config=None) -> NarrativeResult:
        narrative = narrative_registry.get(key)
        if narrative is None:
            raise KeyError(f"No narrative '{key}'. Known: "
                           f"{', '.join(narrative_registry.keys())}.")
        before = list(ctx.metrics_used())
        cfg = config or self.config
        result = narrative.generate(ctx, cfg)
        # ensure provenance is populated even if the subclass didn't slice it
        if not result.metrics_used:
            result.metrics_used = [m for m in ctx.metrics_used()
                                   if m not in before]
        return result

    def generate_many(self, keys, ctx, config=None):
        return [self.generate(k, ctx, config) for k in keys]

    def findings(self, keys, ctx, config=None):
        """Collect all findings across a set of narratives (for a consolidated
        warnings/exceptions panel)."""
        out = []
        for k in keys:
            out.extend(self.generate(k, ctx, config).findings)
        return out
