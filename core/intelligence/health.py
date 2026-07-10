"""Financial Health Scoring.

Produces an overall Financial Health Score (0–100) from transparently-weighted
indicators, each computed from the Financial Metrics Registry via ReportContext.
Every indicator exposes its raw figures, the score it contributed, its weight and
a plain-language explanation — there is no black-box scoring (Part 9). The overall
score is the weighted average of the indicator scores.

Indicators: liquidity, budget performance, income diversity, expense control,
fund health, cash management, reconciliation discipline, outstanding obligations,
operational completeness.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


def _n(v):
    return v if v is not None else Decimal(0)


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


@dataclass
class Indicator:
    key: str
    label: str
    score: float                    # 0..100
    weight: float                   # relative weight
    detail: str                     # plain-language explanation
    metrics: list = field(default_factory=list)

    def as_dict(self):
        return {"key": self.key, "label": self.label, "score": round(self.score, 1),
                "weight": self.weight, "detail": self.detail,
                "metrics": list(self.metrics)}


@dataclass
class HealthScore:
    overall: float
    band: str
    indicators: list = field(default_factory=list)

    def as_dict(self):
        return {"overall": round(self.overall, 1), "band": self.band,
                "indicators": [i.as_dict() for i in self.indicators]}


def _band(score):
    if score >= 80:
        return "Strong"
    if score >= 60:
        return "Sound"
    if score >= 40:
        return "Watch"
    return "At risk"


def compute_health_score(ctx, config=None):
    """Compute the Financial Health Score for the period bound to ``ctx``.
    Transparent: returns each indicator's score, weight, figures and explanation,
    then the weighted overall. All figures are registry metrics."""
    from core.intelligence.engine import IntelligenceConfig
    cfg = config or IntelligenceConfig()
    rows = ctx.fund_summary(consolidated=False)
    income = _n(ctx.total_income())
    opex = _n(ctx.operating_expense())
    closing = sum((_n(r["closing"]) for r in rows), Decimal(0))

    indicators = []

    # 1) Liquidity — reserve months vs target
    if opex > 0 and ctx.start and ctx.end:
        months = max((ctx.end - ctx.start).days / 30.0, 1)
        monthly_opex = float(opex) / months
        reserve_months = (float(closing) / monthly_opex) if monthly_opex else 0
        score = _clamp(reserve_months / cfg.low_reserve_months * 100)
        detail = (f"{reserve_months:.1f} months of operating cover held "
                  f"(target {cfg.low_reserve_months:.0f}).")
    else:
        score, detail = 60.0, "No operating expenditure to assess liquidity against."
    indicators.append(Indicator("liquidity", "Liquidity", score, 2.0, detail,
                                ["fund_summary", "operating_expense"]))

    # 2) Budget performance — share of budgeted funds within tolerance
    budgeted = [r for r in rows if getattr(r["department"], "annual_budget", None)]
    if budgeted:
        within = 0
        for r in budgeted:
            b = _n(getattr(r["department"], "annual_budget", 0))
            util = float(_n(r["expenses"]) / b * 100) if b else 0
            if util <= 100 + cfg.budget_overrun_pct:
                within += 1
        score = within / len(budgeted) * 100
        detail = f"{within}/{len(budgeted)} budgeted funds within tolerance."
    else:
        score, detail = 70.0, "No budgets configured to assess performance."
    indicators.append(Indicator("budget", "Budget performance", score, 1.5,
                                detail, ["fund_summary"]))

    # 3) Income diversity — 100 minus the largest channel's share
    channels = ctx.income_by_channel()
    if income > 0 and channels:
        top_share = max((float(_n(c.get("total")) / income * 100)
                         for c in channels), default=0)
        score = _clamp(100 - (top_share - 33))    # even 3-way split ≈ full marks
        detail = f"Largest channel is {top_share:.0f}% of income."
    else:
        score, detail = 50.0, "No income to assess diversity."
    indicators.append(Indicator("income_diversity", "Income diversity", score,
                                1.0, detail, ["income_by_channel", "total_income"]))

    # 4) Expense control — operating expense as a share of income
    if income > 0:
        ratio = float(opex / income * 100)
        score = _clamp(100 - max(0, ratio - 70))  # <70% opex ratio = full marks
        detail = f"Operating expenditure is {ratio:.0f}% of income."
    else:
        score, detail = 40.0, "No income to assess expense control."
    indicators.append(Indicator("expense_control", "Expense control", score, 1.5,
                                detail, ["operating_expense", "total_income"]))

    # 5) Fund health — share of funds with a non-negative balance
    if rows:
        healthy = sum(1 for r in rows if _n(r["closing"]) >= 0)
        score = healthy / len(rows) * 100
        detail = f"{healthy}/{len(rows)} funds non-negative."
    else:
        score, detail = 100.0, "No funds."
    indicators.append(Indicator("fund_health", "Fund health", score, 1.5, detail,
                                ["fund_summary"]))

    # 6) Cash management — positive total cash
    score = 100.0 if closing > 0 else 0.0
    detail = f"Total cash held is KES {float(closing):,.0f}."
    indicators.append(Indicator("cash", "Cash management", score, 1.0, detail,
                                ["fund_summary"]))

    # 7) Reconciliation discipline — unpresented instruments modest vs cash
    unpresented = _n(ctx.metric("unpresented_payments_total", ctx.end))
    if closing > 0:
        ratio = float(unpresented / closing * 100)
        score = _clamp(100 - ratio)
        detail = f"Unpresented instruments are {ratio:.0f}% of cash."
    else:
        score, detail = 60.0, "No cash to assess reconciliation against."
    indicators.append(Indicator("reconciliation", "Reconciliation discipline",
                                score, 1.0, detail, ["unpresented_payments_total",
                                                     "fund_summary"]))

    # 8) Outstanding obligations — trust-to-remit vs cash
    to_remit = _n(ctx.trust_to_remit())
    if closing > 0:
        ratio = float(to_remit / closing * 100)
        score = _clamp(100 - ratio)
        detail = f"Trust funds to remit are {ratio:.0f}% of cash."
    else:
        score, detail = 70.0, "No cash to assess obligations against."
    indicators.append(Indicator("obligations", "Outstanding obligations", score,
                                1.0, detail, ["trust_to_remit", "fund_summary"]))

    # 9) Operational completeness — pending receipts modest vs income
    pending = _n(ctx.metric("pending_receipts_total", ctx.end))
    if income > 0:
        ratio = float(pending / income * 100)
        score = _clamp(100 - ratio)
        detail = f"Unallocated receipts are {ratio:.0f}% of income."
    else:
        score, detail = 80.0, "No income to assess allocation completeness."
    indicators.append(Indicator("operational", "Operational completeness", score,
                                1.0, detail, ["pending_receipts_total",
                                              "total_income"]))

    total_weight = sum(i.weight for i in indicators)
    overall = sum(i.score * i.weight for i in indicators) / total_weight \
        if total_weight else 0
    return HealthScore(overall=overall, band=_band(overall), indicators=indicators)
