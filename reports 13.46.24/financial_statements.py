"""Financial statements migrated onto the Generic Report Engine.

Each report is composed from registry metrics + reusable components + the
Financial Narrative Engine, and is proven (in tests) to produce figures
identical to the legacy view it replaces. These run **alongside** the legacy
views (which are untouched) so existing URLs/exports keep working; the legacy
implementations are removed only once equivalence is established and adopted.

Reports here: income_statement (Statement of Financial Activity), trial_balance,
financial_position (summary). The Board Report is in reports/board_report.py.
"""
from __future__ import annotations

from decimal import Decimal

from core import roles
from core.reporting import (Column, Filter, Report, Row, Section, SectionData,
                            registry)
from core.reporting.components import ComponentSection
from core.reporting.component_library import NarrativeComponent
from core.reporting.layout import LayoutMeta


def _can_view_reports(user):
    from core.rights import has_right
    return roles.is_staff_role(user) or has_right(user, "view_reports")


def _n(v):
    return v if v is not None else Decimal(0)


# ===========================================================================
# Income & Expenditure (Statement of Financial Activity)
# ===========================================================================

class IncomeExpenditureStatementSection(ComponentSection):
    """The full I&E statement, every figure from registry metrics. Mirrors the
    legacy IncomeStatementView exactly: local receipts as revenue; recurrent and
    capital expenditure by category; operating and net surplus; and the fund
    change in net assets that ties back to the funds."""
    key = "income_expenditure"
    title = "Statement of income & expenditure"
    declared_metrics = ("fund_summary", "operating_expense", "capital_expenditure",
                        "expense_by_category")

    def render(self, ctx, filters):
        rows = ctx.fund_summary(consolidated=False)
        # revenue = local (non-trust) receipts per fund
        income = [{"name": r["department"].name, "amount": _n(r["receipts"])}
                  for r in rows if not r.get("is_trust") and _n(r["receipts"])]
        income.sort(key=lambda x: -x["amount"])
        total_income = sum((r["amount"] for r in income), Decimal(0))

        recurrent = ctx.metric("expense_by_category", ctx.start, ctx.end,
                               expenditure_type="RECURRENT")
        capital = ctx.metric("expense_by_category", ctx.start, ctx.end,
                             expenditure_type="CAPITAL")
        total_recurrent = _n(ctx.operating_expense())
        total_capital = _n(ctx.capital_expenditure())
        operating = total_income - total_recurrent
        surplus = operating - total_capital

        na_open = sum((_n(r["opening"]) for r in rows if not r.get("is_trust")),
                      Decimal(0))
        net_transfers = sum((_n(r.get("net_transfer")) for r in rows
                             if not r.get("is_trust")), Decimal(0))
        na_close = na_open + surplus + net_transfers

        cols = [Column("line", "Line"), Column("amount", "Amount", numeric=True)]
        body = []
        for r in income:
            body.append(Row(cells={"line": r["name"], "amount": r["amount"]}))
        body.append(Row(cells={"line": "Total revenue", "amount": total_income},
                        emphasis=True))
        for r in recurrent:
            if _n(r["amount"]):
                body.append(Row(cells={"line": f"  {r['name']}",
                                       "amount": _n(r["amount"])}))
        body.append(Row(cells={"line": "Total recurrent expenditure",
                               "amount": total_recurrent}, emphasis=True))
        body.append(Row(cells={"line": "Operating surplus/(deficit)",
                               "amount": operating}, emphasis=True))
        for r in capital:
            if _n(r["amount"]):
                body.append(Row(cells={"line": f"  {r['name']}",
                                       "amount": _n(r["amount"])}))
        body.append(Row(cells={"line": "Total capital expenditure",
                               "amount": total_capital}, emphasis=True))
        body.append(Row(cells={"line": "Net surplus/(deficit)",
                               "amount": surplus}, emphasis=True))
        body.append(Row(cells={"line": "Net assets brought forward",
                               "amount": na_open}))
        body.append(Row(cells={"line": "Net inter-fund transfers",
                               "amount": net_transfers}))
        body.append(Row(cells={"line": "Net assets carried forward",
                               "amount": na_close}, emphasis=True))
        return SectionData(key=self.key, title=self.title, columns=cols,
                           rows=body, kind="table",
                           note="Local (operating) basis: trust collections and "
                                "remittances excluded.")


registry.register(Report(
    key="income_statement_v2",
    title="Income & Expenditure (engine)",
    description="Statement of Financial Activity, composed from registry metrics "
                "with auto-generated commentary. Figures identical to the legacy "
                "Income Statement.",
    category="Financial statements",
    permission=_can_view_reports,
    sections=[
        NarrativeComponent("executive_summary",
                           layout=LayoutMeta(order=10, priority=90)),
        IncomeExpenditureStatementSection(layout=LayoutMeta(order=20, priority=100)),
        NarrativeComponent("income_analysis", title="Income analysis",
                           layout=LayoutMeta(order=30, priority=60)),
        NarrativeComponent("expense_analysis", title="Expenditure analysis",
                           layout=LayoutMeta(order=40, priority=60)),
    ],
))


# ===========================================================================
# Trial Balance
# ===========================================================================

class TrialBalanceSection(ComponentSection):
    """Trial balance straight from the trial_balance metric (ledger)."""
    key = "trial_balance"
    title = "Trial balance"
    declared_metrics = ("trial_balance",)

    def render(self, ctx, filters):
        rows_data, totals = ctx.metric("trial_balance", ctx.start, ctx.end)
        cols = [Column("code", "Code"), Column("account", "Account"),
                Column("debit", "Debit", numeric=True),
                Column("credit", "Credit", numeric=True)]
        rows = []
        for r in rows_data:
            acct = r["account"]
            rows.append(Row(cells={"code": getattr(acct, "code", ""),
                                   "account": getattr(acct, "name", str(acct)),
                                   "debit": _n(r["debit"]),
                                   "credit": _n(r["credit"])}))
        total = Row(cells={"code": "", "account": "TOTAL",
                           "debit": _n(totals["debit"]),
                           "credit": _n(totals["credit"])}, emphasis=True)
        balanced = _n(totals["debit"]) == _n(totals["credit"])
        return SectionData(key=self.key, title=self.title, columns=cols,
                           rows=rows, total=total, kind="table",
                           note="In balance." if balanced else
                                "WARNING: trial balance does not balance.")


registry.register(Report(
    key="trial_balance_v2",
    title="Trial Balance (engine)",
    description="Trial balance from the general ledger via the trial_balance "
                "metric.",
    category="Financial statements",
    permission=_can_view_reports,
    sections=[TrialBalanceSection(layout=LayoutMeta(order=10, priority=100))],
))


# ===========================================================================
# Financial Position (summary)
# ===========================================================================

class FinancialPositionSummarySection(ComponentSection):
    """A summary statement of financial position: assets (fund cash + pending),
    liabilities (trust payable + loans), and net assets — all metric-sourced.
    A summary (not the full legacy SOFP with NBV/prepayments), suitable for the
    board pack; the legacy detailed view remains for the full statement."""
    key = "financial_position_summary"
    title = "Statement of financial position (summary)"
    declared_metrics = ("fund_summary", "pending_receipts_total",
                        "trust_to_remit", "loans_outstanding")

    def render(self, ctx, filters):
        from core.reporting.engine import Section
        rows = ctx.fund_summary()
        cash = sum((_n(r["closing"]) for r in rows), Decimal(0))
        trust_payable = sum((_n(r["closing"]) for r in rows if r.get("is_trust")),
                            Decimal(0))
        pending = _n(ctx.metric("pending_receipts_total", ctx.end))
        loans = ctx.loans_outstanding(ctx.end)
        loan_total = _n(loans.get("total")) if isinstance(loans, dict) else _n(loans)
        total_assets = cash + pending
        total_liabilities = trust_payable + loan_total + pending
        net_assets = total_assets - total_liabilities
        pairs = [
            ("Fund cash balances", cash),
            ("Receipts pending allocation", pending),
            ("Total assets", total_assets, True),
            ("Trust funds payable", trust_payable),
            ("Outstanding loans", loan_total),
            ("Pending receipts (unallocated)", pending),
            ("Total liabilities", total_liabilities, True),
            ("Net assets", net_assets, True),
        ]
        return Section.keyvalue(self.key, self.title, pairs)


registry.register(Report(
    key="financial_position_v2",
    title="Financial Position summary (engine)",
    description="Summary statement of financial position from registry metrics, "
                "with cash-position and liability commentary.",
    category="Financial statements",
    permission=_can_view_reports,
    sections=[
        FinancialPositionSummarySection(layout=LayoutMeta(order=10, priority=100)),
        NarrativeComponent("asset_position", title="Assets",
                           layout=LayoutMeta(order=20, width=6, priority=60)),
        NarrativeComponent("liability_position", title="Liabilities",
                           layout=LayoutMeta(order=21, width=6, priority=60)),
    ],
))
