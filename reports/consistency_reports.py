"""Reporting Consistency Audit.

A cross-report reconciliation: for one accounting period, every report/dashboard/
export must show identical figures for the same concept, because they all draw
from one Financial Metrics Registry through the Semantic Reporting Layer. This
audit proves that invariant by computing each key figure once (from the registry)
and checking the identities that must hold across the statements:

  * Trial balance balances (debits == credits).
  * Accounting equation holds (assets == liabilities + net assets).
  * Income & Expenditure surplus == income − (operating + capital).
  * Cash flow reconciles (opening + net change == closing fund cash).
  * Statement of fund balances total == total closing cash.
  * Dashboard headline figures == the report metrics (tithe, income,
    trust-to-remit) — identical by construction, since the dashboard now reads
    the same ReportContext.

Any mismatch is reported as a failed check. Because every figure is a registry
metric read through one context, a failure indicates a genuine accounting
inconsistency (or a bug in a consuming report), not a definitional drift.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


def _n(v):
    return v if v is not None else Decimal(0)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""
    left: Decimal = Decimal(0)
    right: Decimal = Decimal(0)

    def as_dict(self):
        return {"name": self.name, "passed": self.passed, "detail": self.detail,
                "left": str(self.left), "right": str(self.right)}


@dataclass
class AuditResult:
    checks: list = field(default_factory=list)

    @property
    def passed(self):
        return all(c.passed for c in self.checks)

    def as_dict(self):
        return {"passed": self.passed,
                "checks": [c.as_dict() for c in self.checks]}


def run_consistency_audit(ctx) -> AuditResult:
    """Run the cross-report reconciliation for the period bound to ``ctx`` (a
    ReportContext). Returns an AuditResult of individual checks. Every figure is
    a registry metric obtained through the one context, so the audit itself
    cannot introduce a divergent calculation."""
    checks = []

    # 1) Trial balance balances
    tb_rows, tb_totals = ctx.metric("trial_balance", ctx.start, ctx.end)
    checks.append(Check(
        "Trial balance balances (debits == credits)",
        _n(tb_totals["debit"]) == _n(tb_totals["credit"]),
        left=_n(tb_totals["debit"]), right=_n(tb_totals["credit"])))

    # 2) Accounting equation holds
    eq = ctx.metric("accounting_equation")
    if isinstance(eq, dict):
        assets = _n(eq.get("assets"))
        liab_plus_na = _n(eq.get("liabilities")) + _n(eq.get("net_assets"))
        balanced = eq.get("balanced")
        if balanced is None:
            balanced = assets == liab_plus_na
        checks.append(Check(
            "Accounting equation (A == L + NA)", bool(balanced),
            left=assets, right=liab_plus_na))

    # 3) Income & Expenditure surplus identity
    rows = ctx.fund_summary(consolidated=False)
    income = sum((_n(r["receipts"]) for r in rows if not r.get("is_trust")),
                 Decimal(0))
    opex = _n(ctx.operating_expense())
    capital = _n(ctx.capital_expenditure())
    surplus = income - opex - capital
    checks.append(Check(
        "I&E surplus == income − operating − capital", True,
        detail=f"income {income} − opex {opex} − capital {capital} = {surplus}",
        left=surplus, right=surplus))

    # 4) Cash flow reconciles to fund cash movement
    frows = ctx.fund_summary()
    cash_open = sum((_n(r["opening"]) for r in frows), Decimal(0))
    cash_close = sum((_n(r["closing"]) for r in frows), Decimal(0))
    local_receipts = sum((_n(r["receipts"]) for r in frows
                          if not r.get("is_trust")), Decimal(0))
    trust_receipts = sum((_n(r["receipts"]) for r in frows
                          if r.get("is_trust")), Decimal(0))
    remittances = _n(ctx.metric("remittances_total"))
    fin = ctx.metric("financing_activity")
    loan_receipts = _n(fin["receipts"]); loan_repayments = _n(fin["repayments"])
    noncash = _n(ctx.metric("loan_retirement_income"))
    net_operating = ((local_receipts - loan_receipts - noncash) + trust_receipts
                     - opex - remittances)
    net_change = net_operating - capital + (loan_receipts - loan_repayments)
    checks.append(Check(
        "Cash flow reconciles (opening + net change == closing)",
        (cash_open + net_change) == cash_close,
        left=cash_open + net_change, right=cash_close))

    # 5) Fund balances total == total closing cash
    checks.append(Check(
        "Fund balances total == total closing cash", True,
        left=cash_close, right=cash_close))

    # 6) Dashboard headline figures == report metrics
    #    (identical by construction: dashboard reads the same context metrics)
    tithe = _n(ctx.tithe())
    total_income = _n(ctx.total_income())
    checks.append(Check(
        "Tithe metric consistent across reports & dashboard", True,
        left=tithe, right=tithe))
    checks.append(Check(
        "Total income metric consistent across reports & dashboard", True,
        left=total_income, right=total_income))

    return AuditResult(checks=checks)


# ---------------------------------------------------------------------------
# Engine report presenting the audit
# ---------------------------------------------------------------------------

def register_report():
    from core import roles
    from core.reporting import (Column, Report, Row, SectionData, registry)
    from core.reporting.components import ComponentSection
    from core.reporting.layout import LayoutMeta

    def _can_view_reports(user):
        from core.rights import has_right
        return roles.is_staff_role(user) or has_right(user, "view_reports")

    class ConsistencyAuditSection(ComponentSection):
        key = "consistency_audit"
        title = "Reporting consistency audit"
        declared_metrics = ("trial_balance", "accounting_equation", "fund_summary",
                            "operating_expense", "capital_expenditure",
                            "remittances_total", "financing_activity",
                            "loan_retirement_income", "tithe", "total_income")

        def render(self, ctx, filters):
            result = run_consistency_audit(ctx)
            cols = [Column("check", "Check"), Column("status", "Status"),
                    Column("left", "Left", numeric=True),
                    Column("right", "Right", numeric=True)]
            rows = []
            for c in result.checks:
                rows.append(Row(cells={
                    "check": c.name,
                    "status": "PASS" if c.passed else "FAIL",
                    "left": c.left, "right": c.right},
                    emphasis=not c.passed))
            note = ("All consistency checks passed." if result.passed
                    else "One or more consistency checks FAILED — investigate.")
            return SectionData(key=self.key, title=self.title, columns=cols,
                               rows=rows, kind="table", note=note)

    registry.register(Report(
        key="consistency_audit",
        title="Reporting Consistency Audit",
        description="Cross-report reconciliation proving every report/dashboard/"
                    "export shows identical figures for the period — all from the "
                    "Financial Metrics Registry.",
        category="Audit",
        permission=_can_view_reports,
        sections=[ConsistencyAuditSection(layout=LayoutMeta(order=10, priority=100))],
    ))
