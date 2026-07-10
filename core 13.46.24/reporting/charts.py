"""Chart Engine — reusable financial visualisations driven exclusively by the
Semantic Reporting Layer / Financial Metrics Registry.

A chart here is a *specification*, not a rendered image: a ``ChartSpec`` is a
small, JSON-serialisable description (type, labels, datasets, options) that the
HTML renderer hands to Chart.js on the page. Because a chart is built from a
``ReportContext`` (and therefore registry metrics), **it never queries the
database directly** — the same rule the whole reporting stack enforces.

Supported types cover the common financial visualisations: line, bar,
stacked bar, pie, doughnut, waterfall, trend (line variant), gauge, and
comparison (grouped bar). KPI "cards" are a component (see components.py), not a
Chart.js chart, but are included in the same visual vocabulary.

The palette matches the app's forest/brass identity so engine charts look native
alongside the hand-written ones.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal

# App identity palette (forest green + brass + supporting tints), reused so
# engine charts match the existing dashboard/executive charts.
FOREST = "#1f5f4f"
BRASS = "#b08d57"
PALETTE = [FOREST, BRASS, "#6b8f7e", "#c8a97e", "#3d7a68", "#d8c19a",
           "#8fae9f", "#9c7b4d"]


def _f(v):
    """Coerce a Decimal/None to a float for JSON/Chart.js."""
    if v is None:
        return 0.0
    if isinstance(v, Decimal):
        return float(v)
    return v


@dataclass
class ChartSpec:
    """A renderer-agnostic chart description. ``to_config()`` yields a Chart.js
    config dict; the HTML renderer serialises it and instantiates the chart."""
    key: str
    chart_type: str                 # line|bar|stackedBar|pie|doughnut|waterfall|gauge|comparison
    labels: list = field(default_factory=list)
    datasets: list = field(default_factory=list)   # list[dict(label, data, ...)]
    title: str = ""
    options: dict = field(default_factory=dict)
    metrics_used: list = field(default_factory=list)   # provenance

    def to_config(self) -> dict:
        """Chart.js config. Waterfall is emulated with a stacked bar (a
        transparent base + visible delta), since core Chart.js has no native
        waterfall — kept here so components don't reimplement it."""
        ctype = self.chart_type
        datasets = [dict(d) for d in self.datasets]
        options = dict(self.options)
        stacked = ctype in ("stackedBar", "waterfall")
        base_type = {"stackedBar": "bar", "waterfall": "bar",
                     "comparison": "bar", "trend": "line",
                     "gauge": "doughnut"}.get(ctype, ctype)

        # assign palette colours where a dataset didn't specify them
        for i, d in enumerate(datasets):
            if base_type in ("pie", "doughnut"):
                d.setdefault("backgroundColor", PALETTE[: len(self.labels)] or PALETTE)
            else:
                d.setdefault("backgroundColor", PALETTE[i % len(PALETTE)])
                d.setdefault("borderColor", PALETTE[i % len(PALETTE)])
                if base_type == "line":
                    d.setdefault("fill", False)
                    d.setdefault("tension", 0.3)

        if stacked:
            options.setdefault("scales", {})
            options["scales"].setdefault("x", {})["stacked"] = True
            options["scales"].setdefault("y", {})["stacked"] = True

        cfg = {
            "type": base_type,
            "data": {"labels": self.labels, "datasets": datasets},
            "options": options,
        }
        return cfg

    def to_json(self) -> str:
        return json.dumps(self.to_config())


class ChartEngine:
    """Factory of ChartSpecs from a ReportContext. Every method reads figures
    from ``ctx`` (registry metrics) and records provenance on the spec."""

    # ---- generic builders ----

    @staticmethod
    def line(key, labels, series, title="", metrics_used=()):
        """series: list of (label, [values]) pairs."""
        datasets = [{"label": lbl, "data": [_f(v) for v in vals]}
                    for lbl, vals in series]
        return ChartSpec(key, "line", list(labels), datasets, title,
                         metrics_used=list(metrics_used))

    @staticmethod
    def bar(key, labels, series, title="", stacked=False, metrics_used=()):
        datasets = [{"label": lbl, "data": [_f(v) for v in vals]}
                    for lbl, vals in series]
        return ChartSpec(key, "stackedBar" if stacked else "bar",
                         list(labels), datasets, title,
                         metrics_used=list(metrics_used))

    @staticmethod
    def doughnut(key, labels, values, title="", metrics_used=()):
        return ChartSpec(key, "doughnut", list(labels),
                         [{"data": [_f(v) for v in values]}], title,
                         options={"plugins": {"legend": {"position": "bottom"}}},
                         metrics_used=list(metrics_used))

    pie = doughnut  # alias — same builder, different type set below

    @staticmethod
    def waterfall(key, labels, deltas, title="", metrics_used=()):
        """A waterfall from a sequence of signed deltas: renders a transparent
        cumulative base plus the visible step, emulated via a stacked bar."""
        base = []
        step = []
        running = 0.0
        for d in deltas:
            d = _f(d)
            if d >= 0:
                base.append(running)
                step.append(d)
            else:
                base.append(running + d)
                step.append(-d)
            running += d
        return ChartSpec(
            key, "waterfall", list(labels),
            [{"label": "", "data": base, "backgroundColor": "rgba(0,0,0,0)",
              "borderColor": "rgba(0,0,0,0)"},
             {"label": title or "Movement", "data": step}],
            title, metrics_used=list(metrics_used))

    @staticmethod
    def comparison(key, labels, series, title="", metrics_used=()):
        """Grouped bar comparing multiple series across the same labels."""
        datasets = [{"label": lbl, "data": [_f(v) for v in vals]}
                    for lbl, vals in series]
        return ChartSpec(key, "comparison", list(labels), datasets, title,
                         metrics_used=list(metrics_used))

    @staticmethod
    def gauge(key, value, target, title="", metrics_used=()):
        """A simple progress gauge (value vs remaining-to-target) as a
        half-doughnut."""
        value = _f(value); target = _f(target) or 0.0
        remaining = max(target - value, 0.0)
        spec = ChartSpec(
            key, "gauge", ["Achieved", "Remaining"],
            [{"data": [value, remaining],
              "backgroundColor": [FOREST, "#e7e2d8"]}],
            title,
            options={"circumference": 180, "rotation": 270,
                     "cutout": "70%", "plugins": {"legend": {"display": False}}},
            metrics_used=list(metrics_used))
        return spec

    # ---- metric-driven convenience builders ----

    @staticmethod
    def income_by_channel(ctx, key="income_channel_chart"):
        """Doughnut of income split by channel, from the income_by_channel
        metric via the context."""
        data = ctx.income_by_channel()
        labels, values = [], []
        for r in data:
            labels.append(r.get("channel") or r.get("label") or "—")
            values.append(r.get("total"))
        spec = ChartEngine.doughnut(key, labels, values, "Income by channel",
                                    metrics_used=["income_by_channel"])
        return spec

    @staticmethod
    def fund_closing_balances(ctx, key="fund_balances_chart", top=8):
        """Bar of the largest fund closing balances, from fund_summary."""
        rows = sorted(ctx.fund_summary(), key=lambda r: r["closing"] or 0,
                      reverse=True)[:top]
        labels = [r["department"].name for r in rows]
        values = [r["closing"] for r in rows]
        spec = ChartEngine.bar(key, labels, [("Closing balance", values)],
                               "Fund closing balances",
                               metrics_used=["fund_summary"])
        return spec

    @staticmethod
    def local_vs_trust(ctx, key="local_trust_chart"):
        """Doughnut splitting closing balances into local vs trust funds."""
        rows = ctx.fund_summary()
        trust = sum((r["closing"] or 0 for r in rows if r.get("is_trust")), 0)
        local = sum((r["closing"] or 0 for r in rows if not r.get("is_trust")), 0)
        spec = ChartEngine.doughnut(key, ["Local funds", "Trust funds"],
                                    [local, trust], "Local vs trust funds",
                                    metrics_used=["fund_summary"])
        return spec
