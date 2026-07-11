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
    """A summary statement of financial position: assets (fund cash +
    prepayments + pending), liabilities (trust payable + loans + payables +
    accruals), and net assets — all metric-sourced. A summary (not the full
    legacy SOFP with NBV — property, plant & equipment stays off this
    board-pack view by design), but the same accrual-basis adjustments
    (payables, accruals, prepayments) the legacy statement has always shown,
    via the payables_outstanding/accruals_outstanding/prepayments_unexpired
    metrics, so the two statements can never silently diverge."""
    key = "financial_position_summary"
    title = "Statement of financial position (summary)"
    declared_metrics = ("fund_summary", "pending_receipts_total",
                        "trust_to_remit", "loans_outstanding",
                        "petty_cash_balance", "staff_advances_outstanding",
                        "payables_outstanding", "accruals_outstanding",
                        "prepayments_unexpired")

    def render(self, ctx, filters):
        from core.reporting.engine import Section
        rows = ctx.fund_summary()
        cash = sum((_n(r["closing"]) for r in rows), Decimal(0))
        trust_payable = sum((_n(r["closing"]) for r in rows if r.get("is_trust")),
                            Decimal(0))
        pending = _n(ctx.metric("pending_receipts_total", ctx.end))
        loans = ctx.loans_outstanding(ctx.end)
        loan_total = _n(loans.get("total")) if isinstance(loans, dict) else _n(loans)
        # petty cash and unspent staff advances are inside the fund cash figure;
        # reclassify them onto their own lines (matching the detailed Statement
        # of Financial Position) — totals are unchanged.
        petty = _n(ctx.metric("petty_cash_balance"))
        advances = _n(ctx.metric("staff_advances_outstanding"))
        cash_at_bank = cash - petty - advances
        # Cash & bank, split local vs trust (unrestricted vs restricted) rather
        # than shown as one lumped figure — trust cash is already a liability
        # below (trust funds payable), so seeing it broken out on the asset
        # side too makes the restriction visible instead of hidden inside a
        # single "Cash & bank" total. is_trust rows may also carry petty/
        # advances in principle, but those are church-wide (unrestricted)
        # float/receivables in practice, so the netting stays on the local side.
        trust_cash = sum((_n(r["closing"]) for r in rows if r.get("is_trust")),
                         Decimal(0))
        local_cash = cash_at_bank - trust_cash
        # Accrual-basis overlay: credit purchases owed, expenses accrued, and
        # amounts prepaid — the SAME adjustments the legacy Statement of
        # Financial Position has always applied (accrual_adj there), now
        # through the registry so both statements move together.
        payables = _n(ctx.metric("payables_outstanding", ctx.end))
        accruals = _n(ctx.metric("accruals_outstanding", ctx.end))
        prepaid = _n(ctx.metric("prepayments_unexpired", ctx.end))
        total_assets = cash + pending + prepaid
        total_liabilities = trust_payable + loan_total + pending + payables + accruals
        net_assets = total_assets - total_liabilities
        pairs = [
            ("Local fund cash (unrestricted)", local_cash),
            ("Trust fund cash (restricted)", trust_cash),
            ("Petty cash float", petty),
            ("Staff advances (receivable)", advances),
            ("Receipts pending allocation", pending),
            ("Prepayments (unexpired)", prepaid),
            ("Total assets", total_assets, True),
            ("Trust funds payable", trust_payable),
            ("Outstanding loans", loan_total),
            ("Accounts payable", payables),
            ("Accrued expenses", accruals),
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


# ===========================================================================
# Statement of Cash Flows
# ===========================================================================

class CashFlowStatementSection(ComponentSection):
    """SDA three-category cash flow (operating, investing, financing) that
    reconciles the movement in total cash & bank. Every figure is a registry
    metric; the classification logic mirrors the legacy
    StatementOfCashFlowsView exactly so figures are identical."""
    key = "cash_flow_statement"
    title = "Statement of cash flows"
    declared_metrics = ("fund_summary", "operating_expense", "capital_expenditure",
                        "remittances_total", "financing_activity",
                        "loan_retirement_income", "receipts_by_department")

    def render(self, ctx, filters):
        rows = ctx.fund_summary()
        cash_open = sum((_n(r["opening"]) for r in rows), Decimal(0))
        cash_close = sum((_n(r["closing"]) for r in rows), Decimal(0))
        local_receipts = sum((_n(r["receipts"]) for r in rows
                              if not r.get("is_trust")), Decimal(0))
        trust_receipts = sum((_n(r["receipts"]) for r in rows
                              if r.get("is_trust")), Decimal(0))

        remittances = _n(ctx.metric("remittances_total"))
        # operating (recurrent) + capital, from the registry
        operating_exp = _n(ctx.operating_expense())
        capital = _n(ctx.capital_expenditure())

        fin = ctx.metric("financing_activity")
        loan_receipts = _n(fin["receipts"])
        loan_repayments = _n(fin["repayments"])
        loan_noncash_income = _n(ctx.metric("loan_retirement_income"))
        local_operating_receipts = (local_receipts - loan_receipts
                                     - loan_noncash_income)

        net_operating = (local_operating_receipts + trust_receipts
                         - operating_exp - remittances)
        net_investing = -capital
        net_financing = loan_receipts - loan_repayments
        net_change = net_operating + net_investing + net_financing

        cols = [Column("line", "Line"), Column("amount", "Amount", numeric=True)]

        def row(label, amount, emph=False):
            return Row(cells={"line": label, "amount": amount}, emphasis=emph)

        body = [
            row("Local offerings & income received", local_operating_receipts),
            row("Tithe & trust offerings received", trust_receipts),
            row("Operating (recurrent) expenses paid", -operating_exp),
            row("Remittances to the field paid", -remittances),
            row("Net cash from operating activities", net_operating, True),
            row("Purchase of property & equipment", -capital),
            row("Net cash used in investing activities", net_investing, True),
            row("Loan receipts (borrowings)", loan_receipts),
            row("Loan principal repayments", -loan_repayments),
            row("Net cash from financing activities", net_financing, True),
            row("Net increase/(decrease) in cash", net_change, True),
            row("Cash & bank at beginning of period", cash_open),
            row("Cash & bank at end of period", cash_open + net_change, True),
        ]
        ties = (cash_open + net_change) == cash_close
        return SectionData(key=self.key, title=self.title, columns=cols,
                           rows=body, kind="table",
                           note="Reconciles to the movement in cash & bank."
                                if ties else
                                "WARNING: cash flow does not reconcile to fund cash.")


registry.register(Report(
    key="cash_flow_v2",
    title="Cash Flow Statement (engine)",
    description="Statement of Cash Flows (operating/investing/financing) from "
                "registry metrics, reconciling the movement in cash & bank.",
    category="Financial statements",
    permission=_can_view_reports,
    sections=[
        CashFlowStatementSection(layout=LayoutMeta(order=10, priority=100)),
        NarrativeComponent("cash_flow", title="Cash flow commentary",
                           layout=LayoutMeta(order=20, priority=60)),
    ],
))


# ===========================================================================
# Statement of Fund Balances
# ===========================================================================

class FundBalancesStatementSection(ComponentSection):
    """Statement of Fund Balances: per fund, opening + movement = closing, split
    into local and trust, with grand totals — the fund_summary metric presented
    as a formal statement. Reused across the board pack and standalone."""
    key = "fund_balances_statement"
    title = "Statement of fund balances"
    declared_metrics = ("fund_summary",)

    def render(self, ctx, filters):
        rows = ctx.fund_summary(consolidated=filters.get("consolidated", True))
        cols = [Column("fund", "Fund"),
                Column("opening", "Opening", numeric=True),
                Column("receipts", "Receipts", numeric=True),
                Column("expenses", "Payments", numeric=True),
                Column("net_transfer", "Transfers", numeric=True),
                Column("closing", "Closing", numeric=True)]

        def _block(block_rows, heading):
            out = [Row(cells={"fund": heading}, emphasis=True)]
            tot = {k: Decimal(0) for k in
                   ("opening", "receipts", "expenses", "net_transfer", "closing")}
            for r in block_rows:
                cells = {"fund": "  " + r["department"].name,
                         "opening": _n(r["opening"]), "receipts": _n(r["receipts"]),
                         "expenses": _n(r["expenses"]),
                         "net_transfer": _n(r.get("net_transfer")),
                         "closing": _n(r["closing"])}
                for k in tot:
                    tot[k] += cells[k]
                out.append(Row(cells=cells))
            out.append(Row(cells={"fund": f"  Total {heading.lower()}", **tot},
                           emphasis=True))
            return out, tot

        local = sorted((r for r in rows if not r.get("is_trust")),
                      key=lambda r: r["department"].name.lower())
        trust = sorted((r for r in rows if r.get("is_trust")),
                      key=lambda r: r["department"].name.lower())
        body = []
        lblock, ltot = _block(local, "Local funds")
        tblock, ttot = _block(trust, "Trust funds")
        body += lblock + tblock
        grand = {k: ltot[k] + ttot[k] for k in ltot}
        total = Row(cells={"fund": "TOTAL ALL FUNDS", **grand}, emphasis=True)
        return SectionData(key=self.key, title=self.title, columns=cols,
                           rows=body, total=total, kind="table",
                           note="Opening + receipts − payments ± transfers = closing.")


registry.register(Report(
    key="fund_balances_v2",
    title="Statement of Fund Balances (engine)",
    description="Per-fund opening, movement and closing balances, split local vs "
                "trust, from the fund_summary metric.",
    category="Financial statements",
    permission=_can_view_reports,
    filters=[Filter("consolidated", "Consolidate sub-accounts", kind="bool",
                    default=True)],
    sections=[
        FundBalancesStatementSection(layout=LayoutMeta(order=10, priority=100)),
        NarrativeComponent("fund_performance", title="Fund performance",
                           layout=LayoutMeta(order=20, priority=60)),
    ],
))


# ===========================================================================
# Budget vs Actual (complete)
# ===========================================================================

class BudgetVsActualSection(ComponentSection):
    """Complete Budget vs Actual per fund with variance and variance %, from the
    canonical budget service (which the legacy view also uses). Year/period
    filters are honoured. Figures identical to the legacy BudgetVsActualView."""
    key = "budget_vs_actual"
    title = "Budget vs actual"
    declared_metrics = ()   # sourced via the budget service (period-keyed, not the ctx period)

    def render(self, ctx, filters):
        from reports.services import budget as budget_svc
        import datetime as _dt
        year = filters.get("year") or (ctx.end or _dt.date.today()).year
        period = (filters.get("period") or "ANNUAL").upper()
        data = budget_svc.budget_vs_actual(year, period,
                                           filters.get("month"),
                                           filters.get("quarter"))
        cols = [Column("fund", "Fund"),
                Column("budget", "Budget", numeric=True),
                Column("actual", "Actual", numeric=True),
                Column("variance", "Variance", numeric=True),
                Column("variance_pct", "Variance %", numeric=True)]
        rows = []
        for r in data["rows"]:
            vp = r["variance_pct"]
            rows.append(Row(cells={
                "fund": r["department"].name, "budget": _n(r["budget"]),
                "actual": _n(r["actual"]), "variance": _n(r["variance"]),
                "variance_pct": round(float(vp), 1) if vp is not None else ""}))
        t = data["totals"]
        total = Row(cells={
            "fund": "TOTAL", "budget": _n(t["budget"]), "actual": _n(t["actual"]),
            "variance": _n(t["variance"]),
            "variance_pct": round(float(t["variance_pct"]), 1)
            if t["variance_pct"] is not None else ""}, emphasis=True)
        return SectionData(key=self.key, title=f"{self.title} — {data['label']}",
                           columns=cols, rows=rows, total=total, kind="table")


registry.register(Report(
    key="budget_vs_actual_v2",
    title="Budget vs Actual (engine)",
    description="Complete budget vs actual per fund with variance, from the "
                "canonical budget service.",
    category="Financial statements",
    permission=_can_view_reports,
    filters=[
        Filter("year", "Year", kind="text"),
        Filter("period", "Period", kind="text", default="ANNUAL"),
        Filter("month", "Month", kind="text"),
        Filter("quarter", "Quarter", kind="text"),
    ],
    sections=[
        BudgetVsActualSection(layout=LayoutMeta(order=10, priority=100)),
        NarrativeComponent("budget_variance", title="Budget variance commentary",
                           layout=LayoutMeta(order=20, priority=60)),
    ],
))


# ===========================================================================
# Combined statutory pack (recommendation #36): every statement in one report,
# under ONE shared ReportContext — the overlapping aggregates (fund_summary,
# income/expense metrics) compute once and every statement reads the same
# memoized figures, so the pack is internally consistent by construction.
# Composes the existing sections only; no new metrics or components.
# ===========================================================================

registry.register(Report(
    key="financial_statements_pack",
    title="Financial Statements (full pack)",
    description="The complete statutory set in one report — Income & "
                "Expenditure, Statement of Financial Position, Statement of "
                "Cash Flows, Statement of Fund Balances and the Trial Balance "
                "— computed from one shared context so every statement "
                "reconciles with the others.",
    category="Financial statements",
    permission=_can_view_reports,
    filters=[Filter("consolidated", "Consolidate sub-accounts", kind="bool",
                    default=True)],
    sections=[
        IncomeExpenditureStatementSection(
            layout=LayoutMeta(order=10, priority=100)),
        FinancialPositionSummarySection(
            layout=LayoutMeta(order=20, priority=95, width=6)),
        CashFlowStatementSection(
            layout=LayoutMeta(order=21, priority=95, width=6)),
        FundBalancesStatementSection(
            layout=LayoutMeta(order=30, priority=90, page_break_before=True)),
        TrialBalanceSection(
            layout=LayoutMeta(order=40, priority=85, page_break_before=True)),
    ],
))
