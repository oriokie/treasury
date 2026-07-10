"""The intelligence module library — reusable detectors across the insight
categories. Every module reads figures only from the ReportContext (Semantic
Reporting Layer → Financial Metrics Registry), records a full Explanation (Part
9), and is deterministic given (figures, config).

Coverage (registered): financial health (liquidity, operating surplus, cash
runway, reserve adequacy, income stability), income intelligence (giving trends,
declining income, income concentration, trust remittances), expense intelligence
(budget overruns, department overspending, expense anomalies), fund intelligence
(dormant funds, negative balances, fund depletion risk, development progress,
trust obligations), cash intelligence (cash shortage, outstanding instruments,
pending receipts), operational intelligence (missing allocations, outstanding
approvals, stale reconciliation), asset & liability intelligence (loan position).
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from core.intelligence.engine import (Category, Explanation, IntelligenceModule,
                                      Severity, intelligence_registry)


def _n(v):
    return v if v is not None else Decimal(0)


def _kes(v):
    return f"KES {float(_n(v)):,.0f}"


def _prev_period(ctx):
    """The immediately-preceding period of equal length (for trends)."""
    if not (ctx.start and ctx.end):
        return None, None
    length = ctx.end - ctx.start
    prev_end = ctx.start - _dt.timedelta(days=1)
    prev_start = prev_end - length
    return prev_start, prev_end


# ===========================================================================
# Financial health
# ===========================================================================

class OperatingResultModule(IntelligenceModule):
    key = "operating_result"
    category = Category.HEALTH
    declared_metrics = ("total_income", "operating_expense")

    def evaluate(self, ctx, cfg):
        income = _n(ctx.total_income())
        opex = _n(ctx.operating_expense())
        surplus = income - opex
        out = []
        if surplus < 0:
            out.append(self.insight(
                ctx, code="operating_deficit", severity=Severity.WARNING,
                title="Operating deficit",
                description=f"Operating expenditure ({_kes(opex)}) exceeded income "
                            f"({_kes(income)}), a deficit of {_kes(-surplus)}.",
                value=surplus, subject="Operating result",
                supporting_metrics=["total_income", "operating_expense"],
                suggested_actions=["Review discretionary spending",
                                   "Assess income-raising options"],
                explanation=Explanation(
                    reason="Income minus operating expenditure is negative.",
                    metrics=["total_income", "operating_expense"],
                    thresholds={"surplus_floor": {"limit": 0, "actual": float(surplus)}},
                    services=["reports.services.balances"])))
        return out


class LiquidityModule(IntelligenceModule):
    key = "liquidity"
    category = Category.HEALTH
    declared_metrics = ("fund_summary", "operating_expense")

    def evaluate(self, ctx, cfg):
        rows = ctx.fund_summary()
        cash = sum((_n(r["closing"]) for r in rows), Decimal(0))
        opex = _n(ctx.operating_expense())
        out = []
        # reserve adequacy: months of operating cover held
        if opex > 0 and ctx.start and ctx.end:
            months = max((ctx.end - ctx.start).days / 30.0, 1)
            monthly_opex = opex / Decimal(months)
            reserve_months = float(cash / monthly_opex) if monthly_opex else 0
            if reserve_months < cfg.low_reserve_months:
                out.append(self.insight(
                    ctx, code="low_reserves", severity=Severity.WARNING,
                    title="Low operating reserves",
                    description=f"Reserves cover about {reserve_months:.1f} months "
                                f"of operating expenditure (target "
                                f"{cfg.low_reserve_months:.0f}).",
                    value=cash, subject="Reserves", confidence=0.8,
                    supporting_metrics=["fund_summary", "operating_expense"],
                    suggested_actions=["Build reserves toward the target cover"],
                    explanation=Explanation(
                        reason="Closing cash divided by average monthly operating "
                               "expenditure is below the reserve target.",
                        metrics=["fund_summary", "operating_expense"],
                        thresholds={"reserve_months": {
                            "limit": cfg.low_reserve_months,
                            "actual": round(reserve_months, 2)}},
                        services=["reports.services.balances"])))
        return out


# ===========================================================================
# Income intelligence
# ===========================================================================

class IncomeTrendModule(IntelligenceModule):
    key = "income_trend"
    category = Category.INCOME
    declared_metrics = ("total_income",)

    def evaluate(self, ctx, cfg):
        prev_start, prev_end = _prev_period(ctx)
        if not prev_start:
            return []
        cur = _n(ctx.total_income())
        prev = _n(ctx.metric("total_income", prev_start, prev_end))
        if prev <= 0:
            return []
        change = cur - prev
        change_pct = float(change / prev * 100)
        out = []
        if change_pct <= -cfg.income_decline_pct:
            out.append(self.insight(
                ctx, code="declining_income", severity=Severity.WARNING,
                title="Declining income",
                description=f"Income fell {abs(change_pct):.0f}% versus the prior "
                            f"period ({_kes(prev)} → {_kes(cur)}).",
                value=change, subject="Total income", confidence=0.85,
                supporting_metrics=["total_income"],
                suggested_actions=["Review the income decline with leadership"],
                explanation=Explanation(
                    reason="Period-on-period income change breached the decline "
                           "threshold.",
                    metrics=["total_income"],
                    thresholds={"income_decline_pct": {
                        "limit": -cfg.income_decline_pct,
                        "actual": round(change_pct, 1)}},
                    services=["core.metrics.income_credits"])))
        elif change_pct >= cfg.large_movement_pct:
            out.append(self.insight(
                ctx, code="exceptional_income", severity=Severity.NOTICE,
                title="Exceptional income growth",
                description=f"Income rose {change_pct:.0f}% versus the prior period "
                            f"({_kes(prev)} → {_kes(cur)}).",
                value=change, subject="Total income", confidence=0.7,
                supporting_metrics=["total_income"],
                suggested_actions=["Confirm the increase is recurring, not one-off"],
                explanation=Explanation(
                    reason="Period-on-period income change exceeded the large-"
                           "movement threshold.",
                    metrics=["total_income"],
                    thresholds={"large_movement_pct": {
                        "limit": cfg.large_movement_pct,
                        "actual": round(change_pct, 1)}},
                    services=["core.metrics.income_credits"])))
        return out


class IncomeConcentrationModule(IntelligenceModule):
    key = "income_concentration"
    category = Category.INCOME
    declared_metrics = ("income_by_channel", "total_income")

    def evaluate(self, ctx, cfg):
        income = _n(ctx.total_income())
        if income <= 0:
            return []
        channels = ctx.income_by_channel()
        out = []
        for r in channels:
            amt = _n(r.get("total"))
            share = float(amt / income * 100) if income else 0
            if share >= cfg.income_concentration_pct \
                    and cfg.income_concentration_pct < 100:
                label = r.get("channel") or "one channel"
                out.append(self.insight(
                    ctx, code="income_concentration", severity=Severity.NOTICE,
                    title="Income concentration",
                    description=f"{share:.0f}% of income arrives via {label} — a "
                                "concentration risk if that channel is disrupted.",
                    value=amt, subject=str(label), confidence=0.75,
                    supporting_metrics=["income_by_channel", "total_income"],
                    suggested_actions=["Consider diversifying giving channels"],
                    explanation=Explanation(
                        reason="A single channel's share of income exceeded the "
                               "concentration threshold.",
                        metrics=["income_by_channel", "total_income"],
                        thresholds={"income_concentration_pct": {
                            "limit": cfg.income_concentration_pct,
                            "actual": round(share, 1)}},
                        services=["reports.services.balances"])))
        return out


class TrustRemittanceModule(IntelligenceModule):
    key = "trust_remittance"
    category = Category.INCOME
    declared_metrics = ("trust_to_remit",)

    def evaluate(self, ctx, cfg):
        to_remit = _n(ctx.trust_to_remit())
        if to_remit <= cfg.material_amount:
            return []
        return [self.insight(
            ctx, code="trust_to_remit", severity=Severity.NOTICE,
            title="Trust funds awaiting remittance",
            description=f"{_kes(to_remit)} of trust funds is due for remittance to "
                        "the conference.",
            value=to_remit, subject="Trust funds",
            supporting_metrics=["trust_to_remit"],
            suggested_actions=["Remit outstanding trust funds to the conference"],
            explanation=Explanation(
                reason="Trust collected less remitted exceeds the material amount.",
                metrics=["trust_to_remit"],
                thresholds={"material_amount": {
                    "limit": float(cfg.material_amount),
                    "actual": float(to_remit)}},
                services=["reports.services.balances"]))]


# ===========================================================================
# Expense intelligence
# ===========================================================================

class BudgetOverrunModule(IntelligenceModule):
    key = "budget_overrun"
    category = Category.EXPENSE
    declared_metrics = ("fund_summary",)

    def evaluate(self, ctx, cfg):
        out = []
        for r in ctx.fund_summary(consolidated=False):
            dept = r["department"]
            budget = getattr(dept, "annual_budget", None)
            if not budget:
                continue
            actual = _n(r["expenses"])
            utilised = float(actual / Decimal(budget) * 100) if budget else 0
            if utilised > 100 + cfg.budget_overrun_pct:
                over = actual - Decimal(budget)
                out.append(self.insight(
                    ctx, code="budget_overrun", severity=Severity.WARNING,
                    title=f"Budget overrun: {dept.name}",
                    description=f"{dept.name} has spent {_kes(actual)} against a "
                                f"budget of {_kes(budget)} ({utilised:.0f}%).",
                    value=over, subject=dept.name,
                    affected_departments=[dept.name], affected_funds=[dept.name],
                    supporting_metrics=["fund_summary", "expenses_by_department"],
                    suggested_actions=[f"Review spending on {dept.name}",
                                       "Seek committee approval for the overrun"],
                    explanation=Explanation(
                        reason="Actual expenditure exceeded budget beyond the "
                               "tolerance.",
                        metrics=["fund_summary"],
                        thresholds={"budget_utilisation_pct": {
                            "limit": 100 + cfg.budget_overrun_pct,
                            "actual": round(utilised, 1)}},
                        services=["reports.services.balances",
                                  "reports.services.budget"])))
        return out


class ExpenseSpikeModule(IntelligenceModule):
    key = "expense_spike"
    category = Category.EXPENSE
    declared_metrics = ("operating_expense",)

    def evaluate(self, ctx, cfg):
        prev_start, prev_end = _prev_period(ctx)
        if not prev_start:
            return []
        cur = _n(ctx.operating_expense())
        prev = _n(ctx.metric("operating_expense", prev_start, prev_end))
        if prev <= 0:
            return []
        change_pct = float((cur - prev) / prev * 100)
        if change_pct >= cfg.large_movement_pct and (cur - prev) > cfg.material_amount:
            return [self.insight(
                ctx, code="expense_spike", severity=Severity.WARNING,
                title="Rapid increase in spending",
                description=f"Operating expenditure rose {change_pct:.0f}% versus "
                            f"the prior period ({_kes(prev)} → {_kes(cur)}).",
                value=cur - prev, subject="Operating expenditure", confidence=0.8,
                supporting_metrics=["operating_expense"],
                suggested_actions=["Investigate the driver of the spending increase"],
                explanation=Explanation(
                    reason="Period-on-period operating-expenditure change exceeded "
                           "the large-movement threshold.",
                    metrics=["operating_expense"],
                    thresholds={"large_movement_pct": {
                        "limit": cfg.large_movement_pct,
                        "actual": round(change_pct, 1)}},
                    services=["reports.services.balances"]))]
        return []


# ===========================================================================
# Fund intelligence
# ===========================================================================

class NegativeBalanceModule(IntelligenceModule):
    key = "negative_balance"
    category = Category.FUND
    declared_metrics = ("fund_summary",)

    def evaluate(self, ctx, cfg):
        out = []
        for r in ctx.fund_summary(consolidated=False):
            if _n(r["closing"]) < 0:
                dept = r["department"]
                out.append(self.insight(
                    ctx, code="negative_balance", severity=Severity.CRITICAL,
                    title=f"Overdrawn fund: {dept.name}",
                    description=f"{dept.name} has a negative closing balance of "
                                f"{_kes(r['closing'])}.",
                    value=_n(r["closing"]), subject=dept.name,
                    affected_funds=[dept.name],
                    supporting_metrics=["fund_summary"],
                    suggested_actions=[f"Arrange a transfer into {dept.name} or "
                                       "curtail its spending"],
                    explanation=Explanation(
                        reason="A fund's closing balance is below zero.",
                        metrics=["fund_summary"],
                        thresholds={"balance_floor": {
                            "limit": 0, "actual": float(_n(r["closing"]))}},
                        services=["reports.services.balances"])))
        return out


class DormantFundModule(IntelligenceModule):
    key = "dormant_fund"
    category = Category.FUND
    declared_metrics = ("fund_summary",)

    def evaluate(self, ctx, cfg):
        out = []
        for r in ctx.fund_summary(consolidated=False):
            if _n(r["receipts"]) == 0 and _n(r["closing"]) != 0 \
                    and not getattr(r["department"], "is_trust", False):
                dept = r["department"]
                out.append(self.insight(
                    ctx, code="dormant_fund", severity=Severity.NOTICE,
                    title=f"Dormant fund: {dept.name}",
                    description=f"{dept.name} received no income this period but "
                                f"holds {_kes(r['closing'])}.",
                    value=_n(r["closing"]), subject=dept.name, confidence=0.7,
                    affected_funds=[dept.name],
                    supporting_metrics=["fund_summary"],
                    suggested_actions=[f"Review whether {dept.name} is still active"],
                    explanation=Explanation(
                        reason="A non-trust fund had zero receipts but a non-zero "
                               "balance in the period.",
                        metrics=["fund_summary"],
                        thresholds={"period_receipts": {"limit": ">0", "actual": 0}},
                        services=["reports.services.balances"])))
        return out


class DevelopmentProgressModule(IntelligenceModule):
    key = "development_progress"
    category = Category.FUND
    declared_metrics = ("fund_summary",)

    def evaluate(self, ctx, cfg):
        out = []
        for r in ctx.fund_summary(consolidated=False):
            dept = r["department"]
            target = getattr(dept, "target", None)
            if not target or getattr(dept, "category", "") != "DEVELOPMENT":
                continue
            collected = _n(r["closing"])
            pct = float(collected / Decimal(target) * 100) if target else 0
            if pct >= 100:
                out.append(self.insight(
                    ctx, code="development_target_met", severity=Severity.INFO,
                    title=f"Development target met: {dept.name}",
                    description=f"{dept.name} has reached {pct:.0f}% of its "
                                f"{_kes(target)} target.",
                    value=collected, subject=dept.name, confidence=0.9,
                    affected_funds=[dept.name],
                    supporting_metrics=["fund_summary"],
                    suggested_actions=["Consider commissioning the project"],
                    explanation=Explanation(
                        reason="Development fund balance reached its target.",
                        metrics=["fund_summary"],
                        thresholds={"target_pct": {"limit": 100, "actual": round(pct, 1)}},
                        services=["reports.services.balances"])))
        return out


# ===========================================================================
# Cash intelligence
# ===========================================================================

class CashPositionModule(IntelligenceModule):
    key = "cash_position"
    category = Category.CASH
    declared_metrics = ("fund_summary",)

    def evaluate(self, ctx, cfg):
        rows = ctx.fund_summary()
        closing = sum((_n(r["closing"]) for r in rows), Decimal(0))
        if closing <= 0:
            return [self.insight(
                ctx, code="cash_shortage", severity=Severity.CRITICAL,
                title="Cash shortage",
                description=f"Total funds held are {_kes(closing)} — a cash "
                            "shortage requiring immediate attention.",
                value=closing, subject="Cash position",
                supporting_metrics=["fund_summary"],
                suggested_actions=["Halt non-essential spending",
                                   "Review the cash position urgently"],
                explanation=Explanation(
                    reason="Total closing cash across all funds is at or below zero.",
                    metrics=["fund_summary"],
                    thresholds={"cash_floor": {"limit": 0, "actual": float(closing)}},
                    services=["reports.services.balances"]))]
        return []


class OutstandingInstrumentsModule(IntelligenceModule):
    key = "outstanding_instruments"
    category = Category.CASH
    declared_metrics = ("unpresented_payments_total",)

    def evaluate(self, ctx, cfg):
        unpresented = _n(ctx.metric("unpresented_payments_total", ctx.end))
        if unpresented <= cfg.material_amount:
            return []
        return [self.insight(
            ctx, code="unpresented_payments", severity=Severity.NOTICE,
            title="Unpresented payment instruments",
            description=f"{_kes(unpresented)} of payment instruments remain "
                        "unpresented at period end.",
            value=unpresented, subject="Unpresented payments", confidence=0.8,
            supporting_metrics=["unpresented_payments_total"],
            suggested_actions=["Follow up unpresented instruments with payees"],
            explanation=Explanation(
                reason="Unpresented instruments exceed the material amount.",
                metrics=["unpresented_payments_total"],
                thresholds={"material_amount": {
                    "limit": float(cfg.material_amount),
                    "actual": float(unpresented)}},
                services=["cashbook.services.treasury_position.unpresented_cheques_total"]))]


# ===========================================================================
# Operational intelligence
# ===========================================================================

class PendingReceiptsModule(IntelligenceModule):
    key = "pending_receipts"
    category = Category.OPERATIONAL
    declared_metrics = ("pending_receipts_total",)

    def evaluate(self, ctx, cfg):
        pending = _n(ctx.metric("pending_receipts_total", ctx.end))
        if pending <= cfg.material_amount:
            return []
        return [self.insight(
            ctx, code="pending_receipts", severity=Severity.NOTICE,
            title="Bank receipts awaiting allocation",
            description=f"{_kes(pending)} of bank receipts are pending allocation "
                        "to funds.",
            value=pending, subject="Pending receipts", confidence=0.85,
            supporting_metrics=["pending_receipts_total"],
            suggested_actions=["Allocate the pending bank receipts to their funds"],
            explanation=Explanation(
                reason="Unallocated bank receipts exceed the material amount.",
                metrics=["pending_receipts_total"],
                thresholds={"material_amount": {
                    "limit": float(cfg.material_amount),
                    "actual": float(pending)}},
                services=["reports.services.balances"]))]


class OutstandingApprovalsModule(IntelligenceModule):
    key = "outstanding_approvals"
    category = Category.OPERATIONAL
    declared_metrics = ()

    def evaluate(self, ctx, cfg):
        # pending expense approvals in the period — read via the model, but no
        # new accounting figure (a count of workflow items, not a balance).
        from cashbook.models import Expense
        qs = Expense.objects.filter(status=Expense.Status.PENDING)
        if ctx.start:
            qs = qs.filter(date__gte=ctx.start)
        if ctx.end:
            qs = qs.filter(date__lte=ctx.end)
        from django.db.models import Sum, Count
        agg = qs.aggregate(n=Count("id"), t=Sum("amount"))
        n = agg["n"] or 0
        if n == 0:
            return []
        total = _n(agg["t"])
        return [self.insight(
            ctx, code="outstanding_approvals", severity=Severity.NOTICE,
            title="Expenses awaiting approval",
            description=f"{n} expense(s) totalling {_kes(total)} are pending "
                        "approval.",
            value=total, subject="Approvals", confidence=0.9,
            suggested_actions=["Review and approve (or reject) pending expenses"],
            explanation=Explanation(
                reason="One or more expenses are in the PENDING approval state.",
                metrics=[],
                thresholds={"pending_count": {"limit": 0, "actual": n}},
                services=["cashbook.models.Expense"],
                transactions=list(qs.values_list("id", flat=True)[:50])))]


# ===========================================================================
# Asset & liability intelligence
# ===========================================================================

class LoanPositionModule(IntelligenceModule):
    key = "loan_position"
    category = Category.ASSET_LIABILITY
    declared_metrics = ("loans_outstanding",)

    def evaluate(self, ctx, cfg):
        loans = ctx.loans_outstanding(ctx.end)
        total = _n(loans.get("total")) if isinstance(loans, dict) else _n(loans)
        if total <= 0:
            return []
        return [self.insight(
            ctx, code="loans_outstanding", severity=Severity.INFO,
            title="Outstanding loans",
            description=f"The church carries {_kes(total)} of outstanding loan "
                        "liabilities.",
            value=total, subject="Loans", confidence=0.9,
            supporting_metrics=["loans_outstanding"],
            suggested_actions=["Monitor the loan repayment schedule"],
            explanation=Explanation(
                reason="Outstanding loan principal is greater than zero.",
                metrics=["loans_outstanding"],
                thresholds={"loan_floor": {"limit": 0, "actual": float(total)}},
                services=["loans.services.reporting.outstanding_liability"]))]


# ===========================================================================
# Registration
# ===========================================================================

def _register_all():
    for cls in (OperatingResultModule, LiquidityModule, IncomeTrendModule,
                IncomeConcentrationModule, TrustRemittanceModule,
                BudgetOverrunModule, ExpenseSpikeModule, NegativeBalanceModule,
                DormantFundModule, DevelopmentProgressModule, CashPositionModule,
                OutstandingInstrumentsModule, PendingReceiptsModule,
                OutstandingApprovalsModule, LoanPositionModule):
        intelligence_registry.register(cls())


_register_all()
