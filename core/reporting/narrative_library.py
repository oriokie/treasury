"""The financial narrative library — concrete, registered narratives.

Every narrative draws figures only from the ``ReportContext`` (Semantic Reporting
Layer) and is deterministic. Style/tone adjust phrasing and verbosity; the
numbers are always the registered metrics. Detection uses the configurable
``Thresholds`` to raise ``Finding``s.

Coverage (registered keys): executive_summary, income_analysis, expense_analysis,
budget_performance, budget_variance, fund_performance, cash_position,
bank_reconciliation, outstanding_items, development_projects, restricted_funds,
trust_funds, giving_trends, department_performance, asset_position,
liability_position, loan_position, cash_flow, financial_risks,
financial_highlights, key_changes, exceptions, warnings, recommendations.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from core.reporting.narrative import (Finding, Narrative, NarrativeResult,
                                      Severity, Style, Tone, kes, pct,
                                      narrative_registry, _period_phrase)


def _n(v):
    return v if v is not None else Decimal(0)


# ===========================================================================
# Executive summary & highlights
# ===========================================================================

class ExecutiveSummaryNarrative(Narrative):
    key = "executive_summary"
    title = "Executive summary"

    def generate(self, ctx, cfg):
        before = list(ctx.metrics_used())
        income = _n(ctx.total_income())
        opex = _n(ctx.operating_expense())
        capital = _n(ctx.capital_expenditure())
        surplus = income - opex
        rows = ctx.fund_summary()
        closing = sum((_n(r["closing"]) for r in rows), Decimal(0))
        to_remit = _n(ctx.trust_to_remit())

        parts = [f"Over {_period_phrase(ctx)}, recognised income was "
                 f"{kes(income)} against operating expenditure of {kes(opex)}, "
                 f"{'a surplus' if surplus >= 0 else 'a deficit'} of "
                 f"{kes(abs(surplus))}."]
        if capital and not cfg.terse():
            parts.append(f"Capital expenditure of {kes(capital)} was also "
                         "incurred on assets and development.")
        parts.append(f"Net fund balances stand at {kes(closing)}.")
        if to_remit > 0:
            parts.append(f"{kes(to_remit)} of trust funds remains to be remitted "
                         "to the conference.")
        elif not cfg.terse():
            parts.append("All trust funds due have been remitted.")
        return self.result(ctx, " ".join(parts), before=before)


class FinancialHighlightsNarrative(Narrative):
    key = "financial_highlights"
    title = "Financial highlights"

    def generate(self, ctx, cfg):
        before = list(ctx.metrics_used())
        income = _n(ctx.total_income())
        tithe = _n(ctx.tithe())
        opex = _n(ctx.operating_expense())
        rows = ctx.fund_summary()
        closing = sum((_n(r["closing"]) for r in rows), Decimal(0))
        top = sorted(rows, key=lambda r: _n(r["receipts"]), reverse=True)
        top = [r for r in top if _n(r["receipts"]) > 0][:3]
        bullets = [
            f"Total income {kes(income)} (tithe {kes(tithe)}, "
            f"{pct(tithe, income)}% of income).",
            f"Operating expenditure {kes(opex)}.",
            f"Net fund balances {kes(closing)}.",
        ]
        if top:
            names = ", ".join(f"{r['department'].name} ({kes(r['receipts'])})"
                              for r in top)
            bullets.append(f"Largest receipting funds: {names}.")
        text = " ".join(bullets)
        return self.result(ctx, text, before=before)


# ===========================================================================
# Income / expense / giving
# ===========================================================================

class IncomeAnalysisNarrative(Narrative):
    key = "income_analysis"
    title = "Income analysis"

    def generate(self, ctx, cfg):
        before = list(ctx.metrics_used())
        income = _n(ctx.total_income())
        channels = ctx.income_by_channel()
        parts = [f"Total recognised income for {_period_phrase(ctx)} was "
                 f"{kes(income)}."]
        chan_bits = []
        for r in channels:
            amt = _n(r.get("total"))
            if amt <= 0:
                continue
            label = r.get("channel") or r.get("label") or "other"
            chan_bits.append(f"{label} {kes(amt)} ({pct(amt, income)}%)")
        if chan_bits:
            parts.append("By channel: " + "; ".join(chan_bits) + ".")
        return self.result(ctx, " ".join(parts), before=before)


class ExpenseAnalysisNarrative(Narrative):
    key = "expense_analysis"
    title = "Expense analysis"

    def generate(self, ctx, cfg):
        before = list(ctx.metrics_used())
        opex = _n(ctx.operating_expense())
        capital = _n(ctx.capital_expenditure())
        cats = ctx.metric("expense_by_category", ctx.start, ctx.end,
                          expenditure_type="RECURRENT")
        parts = [f"Operating expenditure was {kes(opex)}"]
        if capital:
            parts[0] += f", with a further {kes(capital)} of capital spend"
        parts[0] += "."
        top = [c for c in cats if _n(c["amount"]) > 0][:3]
        if top:
            bits = ", ".join(f"{c['name']} {kes(c['amount'])}" for c in top)
            parts.append(f"Largest operating categories: {bits}.")
        return self.result(ctx, " ".join(parts), before=before)


class GivingTrendsNarrative(Narrative):
    key = "giving_trends"
    title = "Giving trends"

    def generate(self, ctx, cfg):
        before = list(ctx.metrics_used())
        groups = ctx.metric("giving_by_group", ctx.start, ctx.end)
        total = sum((_n(v) for v in groups.values()), Decimal(0)) \
            if isinstance(groups, dict) else Decimal(0)
        parts = [f"Giving by demographic group totalled {kes(total)}."]
        if isinstance(groups, dict):
            ranked = sorted(groups.items(), key=lambda kv: _n(kv[1]), reverse=True)
            bits = [f"{g or 'Unassigned'} {kes(_n(v))}"
                    for g, v in ranked if _n(v) > 0][:4]
            if bits:
                parts.append("Leading groups: " + "; ".join(bits) + ".")
        return self.result(ctx, " ".join(parts), before=before)


# ===========================================================================
# Budget
# ===========================================================================

def _budget_rows(ctx):
    """(dept, budget, actual, variance, pct) for funds with a budget. Actual =
    period expenses from fund_summary. Metric-sourced."""
    out = []
    for r in ctx.fund_summary(consolidated=False):
        dept = r["department"]
        budget = getattr(dept, "annual_budget", None)
        if not budget:
            continue
        actual = _n(r["expenses"])
        variance = _n(budget) - actual
        out.append((dept, _n(budget), actual, variance,
                    pct(actual, budget)))
    return out


class BudgetPerformanceNarrative(Narrative):
    key = "budget_performance"
    title = "Budget performance"

    def generate(self, ctx, cfg):
        before = list(ctx.metrics_used())
        rows = _budget_rows(ctx)
        if not rows:
            return self.result(ctx, "No annual budgets are configured, so budget "
                               "performance cannot be assessed.", before=before)
        total_budget = sum((b for _, b, _, _, _ in rows), Decimal(0))
        total_actual = sum((a for _, _, a, _, _ in rows), Decimal(0))
        parts = [f"Against total budgets of {kes(total_budget)}, actual "
                 f"expenditure was {kes(total_actual)} "
                 f"({pct(total_actual, total_budget)}% utilised)."]
        return self.result(ctx, " ".join(parts), before=before)


class BudgetVarianceNarrative(Narrative):
    key = "budget_variance"
    title = "Budget variance"

    def generate(self, ctx, cfg):
        before = list(ctx.metrics_used())
        rows = _budget_rows(ctx)
        findings = []
        overruns = []
        for dept, budget, actual, variance, utilised in rows:
            if utilised > 100 + cfg.thresholds.variance_pct:
                overruns.append((dept.name, actual - budget, utilised))
                findings.append(Finding(
                    "budget_overrun", Severity.WARNING,
                    f"{dept.name} is over budget by {kes(actual - budget)} "
                    f"({utilised:.0f}% utilised).",
                    metric="fund_summary", value=actual - budget,
                    subject=dept.name))
        if not rows:
            text = "No budgets configured; variance not assessed."
        elif not overruns:
            text = ("All budgeted funds are within tolerance "
                    f"(±{cfg.thresholds.variance_pct:.0f}%).")
        else:
            bits = "; ".join(f"{n} over by {kes(v)} ({u:.0f}%)"
                             for n, v, u in overruns)
            text = f"Funds over budget: {bits}."
        r = self.result(ctx, text, findings=findings, before=before)
        return r


# ===========================================================================
# Funds
# ===========================================================================

class FundPerformanceNarrative(Narrative):
    key = "fund_performance"
    title = "Fund performance"

    def generate(self, ctx, cfg):
        before = list(ctx.metrics_used())
        rows = ctx.fund_summary()
        findings = []
        negatives = [r for r in rows if _n(r["closing"]) < 0]
        for r in negatives:
            findings.append(Finding(
                "negative_balance", Severity.CRITICAL,
                f"{r['department'].name} has a negative closing balance of "
                f"{kes(r['closing'])}.", metric="fund_summary",
                value=_n(r["closing"]), subject=r["department"].name))
        inactive = []
        if cfg.thresholds.inactive_no_receipts:
            inactive = [r for r in rows
                        if _n(r["receipts"]) == 0 and _n(r["closing"]) != 0]
        closing = sum((_n(r["closing"]) for r in rows), Decimal(0))
        parts = [f"Across {len(rows)} funds, net balances total {kes(closing)}."]
        if negatives:
            parts.append(f"{len(negatives)} fund(s) are overdrawn: "
                         + ", ".join(r["department"].name for r in negatives) + ".")
        if inactive and cfg.verbose():
            parts.append(f"{len(inactive)} fund(s) had no receipts this period.")
        for r in inactive:
            findings.append(Finding(
                "inactive_fund", Severity.NOTICE,
                f"{r['department'].name} received no income this period.",
                metric="fund_summary", subject=r["department"].name))
        return self.result(ctx, " ".join(parts), findings=findings, before=before)


class RestrictedFundsNarrative(Narrative):
    key = "restricted_funds"
    title = "Restricted funds"

    def generate(self, ctx, cfg):
        before = list(ctx.metrics_used())
        rows = ctx.fund_summary()
        # restricted = trust funds + development funds (held for a purpose)
        restricted = [r for r in rows if r.get("is_trust")
                      or getattr(r["department"], "category", "") == "DEVELOPMENT"]
        total = sum((_n(r["closing"]) for r in restricted), Decimal(0))
        parts = [f"Restricted and purpose-held funds carry balances of "
                 f"{kes(total)} across {len(restricted)} funds."]
        return self.result(ctx, " ".join(parts), before=before)


class TrustFundsNarrative(Narrative):
    key = "trust_funds"
    title = "Trust funds"

    def generate(self, ctx, cfg):
        before = list(ctx.metrics_used())
        summary = ctx.trust_summary()
        to_remit = _n(ctx.trust_to_remit())
        collected = sum((_n(r.get("collected")) for r in summary), Decimal(0))
        remitted = sum((_n(r.get("remitted")) for r in summary), Decimal(0))
        findings = []
        parts = [f"Trust funds collected {kes(collected)} and remitted "
                 f"{kes(remitted)}, leaving {kes(to_remit)} still to remit to "
                 "the conference."]
        if to_remit > cfg.thresholds.material_amount:
            findings.append(Finding(
                "trust_to_remit", Severity.NOTICE,
                f"{kes(to_remit)} of trust funds is due for remittance.",
                metric="trust_to_remit", value=to_remit))
        return self.result(ctx, " ".join(parts), findings=findings, before=before)


class DevelopmentProjectsNarrative(Narrative):
    key = "development_projects"
    title = "Development projects"

    def generate(self, ctx, cfg):
        before = list(ctx.metrics_used())
        rows = [r for r in ctx.fund_summary(consolidated=False)
                if getattr(r["department"], "category", "") == "DEVELOPMENT"]
        if not rows:
            return self.result(ctx, "No development project funds are active.",
                               before=before)
        total_bf = sum((_n(r["opening"]) for r in rows), Decimal(0))
        total_rcv = sum((_n(r["receipts"]) for r in rows), Decimal(0))
        total_close = sum((_n(r["closing"]) for r in rows), Decimal(0))
        parts = [f"{len(rows)} development project fund(s): brought forward "
                 f"{kes(total_bf)}, raised {kes(total_rcv)}, closing "
                 f"{kes(total_close)}."]
        return self.result(ctx, " ".join(parts), before=before)


class DepartmentPerformanceNarrative(Narrative):
    key = "department_performance"
    title = "Department performance"

    def generate(self, ctx, cfg):
        before = list(ctx.metrics_used())
        rows = sorted(ctx.fund_summary(), key=lambda r: _n(r["receipts"]),
                      reverse=True)
        active = [r for r in rows if _n(r["receipts"]) > 0]
        parts = [f"{len(active)} of {len(rows)} funds received income this period."]
        if active:
            lead = active[0]
            parts.append(f"{lead['department'].name} led with "
                         f"{kes(lead['receipts'])}.")
        return self.result(ctx, " ".join(parts), before=before)


# ===========================================================================
# Cash / reconciliation / outstanding
# ===========================================================================

class CashPositionNarrative(Narrative):
    key = "cash_position"
    title = "Cash position"

    def generate(self, ctx, cfg):
        before = list(ctx.metrics_used())
        rows = ctx.fund_summary()
        closing = sum((_n(r["closing"]) for r in rows), Decimal(0))
        trust = sum((_n(r["closing"]) for r in rows if r.get("is_trust")),
                    Decimal(0))
        local = closing - trust
        findings = []
        parts = [f"Total funds held at period end were {kes(closing)} "
                 f"({kes(local)} local, {kes(trust)} trust)."]
        if closing <= cfg.thresholds.low_cash_floor:
            findings.append(Finding(
                "cash_shortage", Severity.CRITICAL,
                f"Total cash position {kes(closing)} is at or below the floor.",
                metric="fund_summary", value=closing))
            parts.append("This is a cash shortage requiring attention.")
        return self.result(ctx, " ".join(parts), findings=findings, before=before)


class BankReconciliationNarrative(Narrative):
    key = "bank_reconciliation"
    title = "Bank reconciliation"

    def generate(self, ctx, cfg):
        before = list(ctx.metrics_used())
        unpresented = _n(ctx.metric("unpresented_payments_total", ctx.end))
        findings = []
        if unpresented > 0:
            parts = [f"Unpresented payment instruments total {kes(unpresented)} "
                     "as at the period end; these reconcile the ledger balance to "
                     "the bank statement."]
            findings.append(Finding(
                "unpresented_payments", Severity.INFO,
                f"{kes(unpresented)} of payments remain unpresented.",
                metric="unpresented_payments_total", value=unpresented))
        else:
            parts = ["No payment instruments are outstanding; the ledger and bank "
                     "balances agree."]
        return self.result(ctx, " ".join(parts), findings=findings, before=before)


class OutstandingItemsNarrative(Narrative):
    key = "outstanding_items"
    title = "Outstanding items"

    def generate(self, ctx, cfg):
        before = list(ctx.metrics_used())
        pending = _n(ctx.metric("pending_receipts_total", ctx.end))
        to_remit = _n(ctx.trust_to_remit())
        unpresented = _n(ctx.metric("unpresented_payments_total", ctx.end))
        findings = []
        items = []
        if pending > 0:
            items.append(f"{kes(pending)} of bank receipts await allocation")
            findings.append(Finding("pending_receipts", Severity.NOTICE,
                                    f"{kes(pending)} of receipts unallocated.",
                                    metric="pending_receipts_total", value=pending))
        if to_remit > 0:
            items.append(f"{kes(to_remit)} of trust funds await remittance")
        if unpresented > 0:
            items.append(f"{kes(unpresented)} of payments are unpresented")
        if not items:
            text = "There are no outstanding items."
        else:
            text = "Outstanding items: " + "; ".join(items) + "."
        return self.result(ctx, text, findings=findings, before=before)


# ===========================================================================
# Balance sheet positions
# ===========================================================================

class AssetPositionNarrative(Narrative):
    key = "asset_position"
    title = "Asset position"

    def generate(self, ctx, cfg):
        before = list(ctx.metrics_used())
        rows = ctx.fund_summary()
        cash = sum((_n(r["closing"]) for r in rows), Decimal(0))
        pending = _n(ctx.metric("pending_receipts_total", ctx.end))
        parts = [f"Liquid assets (fund cash balances) total {kes(cash)}"]
        if pending:
            parts[0] += f", with a further {kes(pending)} of receipts pending"
        parts[0] += "."
        return self.result(ctx, " ".join(parts), before=before)


class LiabilityPositionNarrative(Narrative):
    key = "liability_position"
    title = "Liability position"

    def generate(self, ctx, cfg):
        before = list(ctx.metrics_used())
        to_remit = _n(ctx.trust_to_remit())
        loans = ctx.loans_outstanding(ctx.end)
        loan_total = _n(loans.get("total")) if isinstance(loans, dict) else _n(loans)
        total = to_remit + loan_total
        parts = [f"Liabilities comprise {kes(to_remit)} of trust funds payable to "
                 f"the conference and {kes(loan_total)} of outstanding loans — "
                 f"{kes(total)} in total."]
        return self.result(ctx, " ".join(parts), before=before)


class LoanPositionNarrative(Narrative):
    key = "loan_position"
    title = "Loan position"

    def generate(self, ctx, cfg):
        before = list(ctx.metrics_used())
        loans = ctx.loans_outstanding(ctx.end)
        loan_total = _n(loans.get("total")) if isinstance(loans, dict) else _n(loans)
        if loan_total <= 0:
            text = "The church carries no outstanding loan liabilities."
        else:
            text = f"Outstanding loan liabilities total {kes(loan_total)}."
        return self.result(ctx, text, before=before)


class CashFlowNarrative(Narrative):
    key = "cash_flow"
    title = "Cash flow"

    def generate(self, ctx, cfg):
        before = list(ctx.metrics_used())
        rows = ctx.fund_summary()
        opening = sum((_n(r["opening"]) for r in rows), Decimal(0))
        receipts = sum((_n(r["receipts"]) for r in rows), Decimal(0))
        expenses = sum((_n(r["expenses"]) for r in rows), Decimal(0))
        closing = sum((_n(r["closing"]) for r in rows), Decimal(0))
        net = closing - opening
        parts = [f"Cash moved from {kes(opening)} at the start to {kes(closing)} "
                 f"at the end of {_period_phrase(ctx)}, a net "
                 f"{'increase' if net >= 0 else 'decrease'} of {kes(abs(net))} "
                 f"(receipts {kes(receipts)}, payments {kes(expenses)})."]
        return self.result(ctx, " ".join(parts), before=before)


# ===========================================================================
# Cross-cutting: risks, changes, exceptions, warnings, recommendations
# ===========================================================================

def _collect_findings(ctx, cfg):
    """Run the detection-bearing narratives once and gather their findings.
    Deterministic and metric-sourced."""
    findings = []
    for cls in (FundPerformanceNarrative, BudgetVarianceNarrative,
                CashPositionNarrative, TrustFundsNarrative,
                OutstandingItemsNarrative, BankReconciliationNarrative):
        findings.extend(cls().generate(ctx, cfg).findings)
    return findings


class FinancialRisksNarrative(Narrative):
    key = "financial_risks"
    title = "Financial risks"

    def generate(self, ctx, cfg):
        before = list(ctx.metrics_used())
        findings = [f for f in _collect_findings(ctx, cfg)
                    if f.severity in (Severity.WARNING, Severity.CRITICAL)]
        if not findings:
            text = "No material financial risks were detected this period."
        else:
            text = "Risks identified: " + " ".join(f.message for f in findings)
        return self.result(ctx, text, findings=findings, before=before)


class KeyChangesNarrative(Narrative):
    key = "key_changes"
    title = "Key changes"

    def generate(self, ctx, cfg):
        before = list(ctx.metrics_used())
        if not (ctx.start and ctx.end):
            return self.result(ctx, "Period not bounded; period-on-period changes "
                               "are not available.", before=before)
        length = ctx.end - ctx.start
        prev_end = ctx.start - _dt.timedelta(days=1)
        prev_start = prev_end - length
        cur = _n(ctx.total_income())
        prev = _n(ctx.metric("total_income", prev_start, prev_end))
        findings = []
        if prev:
            change = cur - prev
            change_pct = pct(abs(change), prev)
            direction = "up" if change >= 0 else "down"
            text = (f"Income is {direction} {kes(abs(change))} "
                    f"({change_pct}%) versus the prior period ({kes(prev)}).")
            if change_pct >= cfg.thresholds.large_movement_pct:
                findings.append(Finding(
                    "large_movement", Severity.NOTICE,
                    f"Income moved {direction} {change_pct}% versus prior period.",
                    metric="total_income", value=change))
        else:
            text = f"Income for this period was {kes(cur)}; no comparable prior period."
        return self.result(ctx, text, findings=findings, before=before)


class ExceptionsNarrative(Narrative):
    key = "exceptions"
    title = "Exceptions"

    def generate(self, ctx, cfg):
        before = list(ctx.metrics_used())
        findings = _collect_findings(ctx, cfg)
        exceptions = [f for f in findings if f.severity == Severity.CRITICAL]
        if not exceptions:
            text = "No exceptions were identified."
        else:
            text = "Exceptions requiring action: " + \
                   " ".join(f.message for f in exceptions)
        return self.result(ctx, text, findings=exceptions, before=before)


class WarningsNarrative(Narrative):
    key = "warnings"
    title = "Warnings"

    def generate(self, ctx, cfg):
        before = list(ctx.metrics_used())
        findings = [f for f in _collect_findings(ctx, cfg)
                    if f.severity in (Severity.WARNING, Severity.CRITICAL)]
        if not findings:
            text = "No warnings."
        else:
            text = " ".join(f.message for f in findings)
        return self.result(ctx, text, findings=findings, before=before)


class RecommendationsNarrative(Narrative):
    key = "recommendations"
    title = "Recommendations"

    def generate(self, ctx, cfg):
        before = list(ctx.metrics_used())
        findings = _collect_findings(ctx, cfg)
        recs = []
        seen = set()
        for f in findings:
            rec = _recommendation_for(f)
            if rec and rec not in seen:
                recs.append(rec)
                seen.add(rec)
        if not recs:
            text = "No specific actions are recommended; financial controls " \
                   "appear to be operating normally."
        else:
            text = " ".join(f"{i}. {r}" for i, r in enumerate(recs, 1))
        return self.result(ctx, text, findings=findings, before=before)


def _recommendation_for(finding):
    return {
        "budget_overrun": f"Review spending on {finding.subject} and seek "
                          "committee approval for the overrun.",
        "negative_balance": f"Fund {finding.subject} is overdrawn — arrange a "
                            "transfer or curtail spending.",
        "cash_shortage": "Address the cash shortage before committing further "
                         "expenditure.",
        "trust_to_remit": "Remit outstanding trust funds to the conference.",
        "pending_receipts": "Allocate the pending bank receipts to their funds.",
        "unpresented_payments": "Follow up unpresented instruments with payees.",
        "inactive_fund": None,
    }.get(finding.code)


# ===========================================================================
# Registration
# ===========================================================================

def _register_all():
    for cls in (ExecutiveSummaryNarrative, FinancialHighlightsNarrative,
                IncomeAnalysisNarrative, ExpenseAnalysisNarrative,
                GivingTrendsNarrative, BudgetPerformanceNarrative,
                BudgetVarianceNarrative, FundPerformanceNarrative,
                RestrictedFundsNarrative, TrustFundsNarrative,
                DevelopmentProjectsNarrative, DepartmentPerformanceNarrative,
                CashPositionNarrative, BankReconciliationNarrative,
                OutstandingItemsNarrative, AssetPositionNarrative,
                LiabilityPositionNarrative, LoanPositionNarrative,
                CashFlowNarrative, FinancialRisksNarrative, KeyChangesNarrative,
                ExceptionsNarrative, WarningsNarrative,
                RecommendationsNarrative):
        narrative_registry.register(cls())


_register_all()
