"""Trend & Forecast Engine.

Reusable, deterministic trend analysis and projection services that consume only
the Financial Metrics Registry via ReportContext. Forecasts are clearly labelled
projections and never replace actual accounting figures — they are computed from
the metric history and returned as separate, marked values.

Supports month-on-month / quarter-on-quarter / year-on-year comparisons, rolling
averages, growth rates, trend direction, and simple, transparent forecast
projections (linear on the recent series). Every result records the metric it was
built from, so it is explainable.
"""
from __future__ import annotations

import calendar
import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal


def _n(v):
    return v if v is not None else Decimal(0)


def _month_bounds(year, month):
    start = _dt.date(year, month, 1)
    end = _dt.date(year, month, calendar.monthrange(year, month)[1])
    return start, end


def _add_months(year, month, delta):
    idx = (year * 12 + (month - 1)) + delta
    return idx // 12, idx % 12 + 1


@dataclass
class TrendPoint:
    label: str
    start: _dt.date
    end: _dt.date
    value: Decimal


@dataclass
class TrendResult:
    metric: str
    points: list = field(default_factory=list)      # list[TrendPoint]
    direction: str = "flat"                          # rising | falling | flat
    growth_pct: float = 0.0                          # last vs first
    rolling_average: Decimal = Decimal(0)
    is_projection: bool = False

    def as_dict(self):
        return {"metric": self.metric, "direction": self.direction,
                "growth_pct": self.growth_pct,
                "rolling_average": float(self.rolling_average),
                "points": [{"label": p.label, "start": p.start.isoformat(),
                            "end": p.end.isoformat(), "value": float(p.value)}
                           for p in self.points]}


def monthly_series(metric_name, end_date=None, months=12):
    """Value of a period-aware metric for each of the last ``months`` calendar
    months, oldest first. Deterministic and metric-sourced."""
    from core.reporting import ReportContext
    from core.metrics import metrics
    end_date = end_date or _dt.date.today()
    points = []
    y, m = end_date.year, end_date.month
    seq = []
    for i in range(months - 1, -1, -1):
        yy, mm = _add_months(y, m, -i)
        seq.append((yy, mm))
    for yy, mm in seq:
        s, e = _month_bounds(yy, mm)
        val = _n(metrics.registry and __metric_value(metric_name, s, e))
        points.append(TrendPoint(label=f"{calendar.month_abbr[mm]} {yy}",
                                  start=s, end=e, value=val))
    return points


def __metric_value(metric_name, start, end):
    from core.metrics import metrics
    impl = getattr(metrics, metric_name)
    try:
        return impl(start, end)
    except TypeError:
        return impl()


def trend(metric_name, end_date=None, months=12, window=3):
    """Build a TrendResult for a metric over a monthly series: direction, growth
    rate (last vs first), and a rolling average over the last ``window`` points."""
    points = monthly_series(metric_name, end_date, months)
    values = [p.value for p in points]
    first = values[0] if values else Decimal(0)
    last = values[-1] if values else Decimal(0)
    growth = float((last - first) / first * 100) if first else 0.0
    direction = ("rising" if last > first else
                 "falling" if last < first else "flat")
    tail = values[-window:] if len(values) >= window else values
    rolling = sum(tail, Decimal(0)) / Decimal(len(tail)) if tail else Decimal(0)
    return TrendResult(metric=metric_name, points=points, direction=direction,
                       growth_pct=round(growth, 1), rolling_average=rolling)


def year_on_year(metric_name, end_date=None):
    """This month vs the same month a year ago."""
    end_date = end_date or _dt.date.today()
    cur_s, cur_e = _month_bounds(end_date.year, end_date.month)
    prev_s, prev_e = _month_bounds(end_date.year - 1, end_date.month)
    cur = _n(__metric_value(metric_name, cur_s, cur_e))
    prev = _n(__metric_value(metric_name, prev_s, prev_e))
    change_pct = float((cur - prev) / prev * 100) if prev else 0.0
    return {"metric": metric_name, "current": float(cur), "prior_year": float(prev),
            "change_pct": round(change_pct, 1),
            "direction": "up" if cur > prev else "down" if cur < prev else "flat"}


def forecast(metric_name, end_date=None, history_months=6, horizon_months=3):
    """Project the next ``horizon_months`` from a simple linear fit on the recent
    history. CLEARLY a projection (``is_projection=True``); never an accounting
    figure. Uses least-squares slope on the monthly series."""
    hist = monthly_series(metric_name, end_date, history_months)
    ys = [float(p.value) for p in hist]
    n = len(ys)
    if n < 2:
        return TrendResult(metric=metric_name, points=list(hist),
                           is_projection=True)
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs) or 1
    slope = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n)) / denom
    intercept = mean_y - slope * mean_x

    proj_points = list(hist)
    last = hist[-1]
    y, m = last.end.year, last.end.month
    for h in range(1, horizon_months + 1):
        yy, mm = _add_months(y, m, h)
        s, e = _month_bounds(yy, mm)
        projected = max(0.0, intercept + slope * (n - 1 + h))
        proj_points.append(TrendPoint(
            label=f"{calendar.month_abbr[mm]} {yy} (proj.)",
            start=s, end=e, value=Decimal(str(round(projected, 2)))))
    growth = round(slope, 2)
    return TrendResult(metric=metric_name, points=proj_points,
                       direction="rising" if slope > 0 else
                       "falling" if slope < 0 else "flat",
                       growth_pct=growth, is_projection=True)
