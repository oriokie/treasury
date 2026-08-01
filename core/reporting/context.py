"""Semantic Reporting Layer — the single interface through which every report,
dashboard, widget, export and AI feature obtains financial data.

The layer sits on top of the Financial Metrics Registry (``core.metrics``,
v2.27): it never recomputes accounting logic, it *orchestrates* the registry.
Its job is threefold:

1. **One doorway.** New code asks a ``ReportContext`` for figures
   (``ctx.metric("tithe")``, ``ctx.fund_summary()``) instead of importing
   services or writing raw aggregates. The registry remains the source of
   truth; this is the semantic surface over it.

2. **Compute-once per render.** A ``ReportContext`` is bound to a period (and
   optional scope) and **memoizes every metric result for its own lifetime**.
   This directly addresses recommendation #1 (the Monthly Treasurer's Report
   recomputing ``department_summary`` and friends separately for each section):
   with a shared context, ``fund_summary`` runs once no matter how many
   sections read it. Unlike ``core.perfcache`` (an *optional, cross-request*
   TTL cache, off by default), request-scoped memoization is always on and
   needs no configuration, so it helps in dev, tests and production alike.

3. **Uniform provenance.** Because every figure flows through one object,
   future features (semantic exports, AI answers, a metrics adoption audit)
   can enumerate exactly which metrics a report consumed.

Design notes
------------
* A context is cheap and short-lived — create one per render, don't cache it
  across requests (period/scope would go stale).
* Memoization keys on the metric name plus the *call arguments*, so
  ``ctx.metric("fund_balance", dept_a)`` and ``ctx.metric("fund_balance",
  dept_b)`` are cached independently.
* The context is deliberately thin: it adds no accounting rules. If a figure
  isn't a registered metric, it isn't available here — which is the point
  (raw aggregates are what we're migrating away from).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from core.metrics import metrics


def _hashable(value):
    """Best-effort stable key part for memoization. Model instances key on pk,
    everything hashable keys on itself, and anything else falls back to id()
    (still correct within one request, where the object identity is stable)."""
    if value is None:
        return None
    pk = getattr(value, "pk", None)
    if pk is not None and hasattr(value, "_meta"):
        return (value.__class__.__name__, pk)
    try:
        hash(value)
        return value
    except TypeError:
        return id(value)


@dataclass
class ReportContext:
    """A period- and scope-bound doorway to the Financial Metrics Registry,
    memoized for the life of one report render.

    Typical use in a view::

        ctx = ReportContext.from_request(request)          # honours ?start/?end
        funds = ctx.fund_summary()                         # computed once
        tithe = ctx.metric("tithe")                        # period applied
        income = ctx.metric("total_income")                # shares the render

    ``start``/``end`` may be None (all-time). ``scope`` is an optional set of
    department ids for leader-scoped reports; metrics that don't accept a scope
    ignore it (the registry functions are unchanged), and scoped filtering is
    applied by the caller — the context simply carries it so every section of a
    report agrees on the same scope.
    """

    start: Optional[Any] = None
    end: Optional[Any] = None
    scope: Optional[frozenset] = None
    label: str = ""
    #: When set, this render is on the "as reported" basis — the position as it
    #: stood at this moment, rather than as it is now understood. Set by the
    #: engine view; carried here so a section or template can say so on the
    #: page. The basis itself is applied by ``reports.services.asat``.
    as_reported_at: Optional[Any] = None
    _cache: dict = field(default_factory=dict, repr=False)
    _used: list = field(default_factory=list, repr=False)

    # ---- construction ----

    @classmethod
    def from_request(cls, request, **kw):
        """Build a context from the request's period (``?start``/``?end`` via
        the shared ``parse_period``), so a report's context matches its URL."""
        from core.utils import parse_period
        start, end = parse_period(request)
        return cls(start=start, end=end, **kw)

    @classmethod
    def for_period(cls, start=None, end=None, **kw):
        return cls(start=start, end=end, **kw)

    # ---- the doorway ----

    def metric(self, name, *args, **kwargs):
        """Return a registry metric's value, computed once per (name, args)
        for this context. Period-aware metrics (those whose registry ``inputs``
        begin with ``start``) automatically receive this context's period, and
        as-at metrics (``inputs`` beginning with ``as_of``) automatically
        receive this context's period end, unless the caller passes explicit
        positional args or period kwargs — so every section of a report reads
        as at the same date without each remembering to pass ``ctx.end``.

        Raises KeyError with a helpful message if the metric isn't registered,
        so typos surface immediately rather than silently returning nothing.
        """
        if name not in metrics.registry:
            raise KeyError(
                f"'{name}' is not a registered metric. Known metrics: "
                f"{', '.join(sorted(metrics.registry))}. "
                f"Add it to core.metrics rather than computing it ad hoc.")

        inputs = metrics.registry[name].inputs
        no_explicit = (not args and "start" not in kwargs
                       and "end" not in kwargs and "as_of" not in kwargs)
        call_args = args
        if no_explicit and inputs.startswith("start"):
            call_args = (self.start, self.end)
        elif no_explicit and inputs.startswith("as_of") and self.end is not None:
            call_args = (self.end,)

        key = (name, tuple(_hashable(a) for a in call_args),
               tuple(sorted((k, _hashable(v)) for k, v in kwargs.items())))
        if key not in self._cache:
            impl = getattr(metrics, name)
            self._cache[key] = impl(*call_args, **kwargs)
            self._used.append(name)
        return self._cache[key]

    # ---- convenience accessors for the most-used metrics ----
    # These read better in views/templates than metric("...") and make the
    # common report sections self-documenting. Each is just a thin call to
    # metric(), so they share the same per-render memoization.

    def fund_summary(self, consolidated=True):
        return self.metric("fund_summary", self.start, self.end, consolidated=consolidated)

    def trust_summary(self):
        return self.metric("trust_summary")

    def tithe(self):
        return self.metric("tithe")

    def total_income(self):
        return self.metric("total_income")

    def income_by_channel(self):
        return self.metric("income_by_channel")

    def receipts_by_department(self):
        return self.metric("receipts_by_department")

    def expenses_by_department(self, **kw):
        return self.metric("expenses_by_department", **kw)

    def operating_expense(self):
        return self.metric("operating_expense")

    def capital_expenditure(self):
        return self.metric("capital_expenditure")

    def trust_to_remit(self):
        return self.metric("trust_to_remit")

    def loans_outstanding(self, as_of=None):
        return self.metric("loans_outstanding", as_of or self.end)

    # ---- introspection (adoption audit / AI provenance) ----

    def metrics_used(self):
        """Distinct metric names this context has served, in first-use order —
        used by the adoption report and future AI provenance."""
        seen = []
        for n in self._used:
            if n not in seen:
                seen.append(n)
        return seen

    def as_dict(self, *names):
        """Materialise a set of metrics into a plain dict, e.g. for a widget or
        an API payload: ``ctx.as_dict("tithe", "total_income")``."""
        return {n: self.metric(n) for n in names}
