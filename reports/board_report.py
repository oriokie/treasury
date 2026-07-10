"""The Board / Treasurer Report, rebuilt entirely from reusable components and
the Financial Narrative Engine.

This does NOT copy the legacy MonthlyTreasurerReportView. It composes the report
from the component library and narrative engine, every figure flowing from the
Financial Metrics Registry through one shared ReportContext. Each section is an
independently configurable component with layout metadata, so the composition
(order, width, visibility, print/export) is data, not code.

The legacy Board/Monthly report and its URL remain untouched; this is a parallel
engine report (`board_report_v2`) that can be adopted once reviewed.
"""
from __future__ import annotations

from core import roles
from core.reporting import Filter, LayoutMeta, Report, registry
from core.reporting.charts import ChartEngine
from core.reporting.component_library import (
    BankReconciliationSummaryComponent, BudgetSummaryComponent,
    CashPositionComponent, ChartComponent, ExpenseSummaryComponent,
    FundSummaryComponent, IncomeSummaryComponent, InfoPanelComponent,
    KpiCardsComponent, NarrativeComponent, OutstandingItemsComponent,
    SignatureBlockComponent, VarianceAnalysisComponent)
from reports.financial_statements import (FinancialPositionSummarySection,
                                          IncomeExpenditureStatementSection)


def _can_view_reports(user):
    from core.rights import has_right
    return roles.is_staff_role(user) or has_right(user, "view_reports")


# The board pack, top to bottom. Each section is a reusable component with its
# own layout; the narrative components generate commentary from the same context.
registry.register(Report(
    key="board_report_v2",
    title="Board / Treasurer's Report (engine)",
    description="The board pack, composed entirely from reusable components and "
                "auto-generated narrative — every figure from the Financial "
                "Metrics Registry through one shared context.",
    category="Board",
    permission=_can_view_reports,
    filters=[
        Filter("consolidated", "Consolidate sub-accounts", kind="bool",
               default=True),
    ],
    sections=[
        # Executive summary & highlights
        NarrativeComponent("executive_summary", title="Executive summary",
                           layout=LayoutMeta(order=10, width=12, priority=100,
                                             group="Summary")),
        NarrativeComponent("financial_highlights", title="Financial highlights",
                           layout=LayoutMeta(order=15, width=12, priority=95,
                                             group="Summary")),
        # KPI dashboard
        KpiCardsComponent(layout=LayoutMeta(order=20, width=12, priority=95,
                                            group="KPIs")),
        # Charts
        ChartComponent(ChartEngine.income_by_channel, key="chart_income",
                       title="Income by channel",
                       layout=LayoutMeta(order=30, width=6, priority=70,
                                         group="Charts", export_visible=False)),
        ChartComponent(ChartEngine.fund_closing_balances, key="chart_funds",
                       title="Fund balances",
                       layout=LayoutMeta(order=31, width=6, priority=70,
                                         group="Charts", export_visible=False)),
        # Cash position
        CashPositionComponent(layout=LayoutMeta(order=40, width=6, priority=85,
                                                group="Position")),
        NarrativeComponent("cash_position", title="Cash position commentary",
                           layout=LayoutMeta(order=41, width=6, priority=60,
                                             group="Position")),
        # Fund summary
        FundSummaryComponent(layout=LayoutMeta(order=50, width=12, priority=90,
                                               group="Funds")),
        NarrativeComponent("fund_performance", title="Fund performance",
                           layout=LayoutMeta(order=51, width=12, priority=55,
                                             group="Funds")),
        # Income & expense
        IncomeSummaryComponent(layout=LayoutMeta(order=60, width=6, priority=80,
                                                 group="Income & expense")),
        ExpenseSummaryComponent(layout=LayoutMeta(order=61, width=6, priority=80,
                                                  group="Income & expense")),
        NarrativeComponent("income_analysis", title="Income analysis",
                           layout=LayoutMeta(order=62, width=6, priority=50,
                                             group="Income & expense")),
        NarrativeComponent("expense_analysis", title="Expenditure analysis",
                           layout=LayoutMeta(order=63, width=6, priority=50,
                                             group="Income & expense")),
        # Budget performance & variance
        BudgetSummaryComponent(layout=LayoutMeta(order=70, width=12, priority=60,
                                                 group="Budget")),
        NarrativeComponent("budget_variance", title="Budget variance",
                           layout=LayoutMeta(order=71, width=12, priority=55,
                                             group="Budget")),
        # Development projects & trust
        NarrativeComponent("development_projects", title="Development projects",
                           layout=LayoutMeta(order=80, width=6, priority=45,
                                             group="Restricted")),
        NarrativeComponent("trust_funds", title="Trust funds",
                           layout=LayoutMeta(order=81, width=6, priority=45,
                                             group="Restricted")),
        # Reconciliation & outstanding
        BankReconciliationSummaryComponent(
            layout=LayoutMeta(order=90, width=6, priority=50,
                              group="Reconciliation")),
        OutstandingItemsComponent(layout=LayoutMeta(order=91, width=6, priority=50,
                                                    group="Reconciliation")),
        # Variance analysis
        VarianceAnalysisComponent(layout=LayoutMeta(order=95, width=12, priority=45,
                                                    group="Analysis")),
        # Financial statements
        IncomeExpenditureStatementSection(
            layout=LayoutMeta(order=100, width=6, priority=70,
                              group="Statements")),
        FinancialPositionSummarySection(
            layout=LayoutMeta(order=101, width=6, priority=70,
                              group="Statements")),
        # Risks, warnings, recommendations
        NarrativeComponent("financial_risks", title="Financial risks",
                           layout=LayoutMeta(order=110, width=12, priority=65,
                                             group="Oversight")),
        NarrativeComponent("recommendations", title="Recommendations",
                           layout=LayoutMeta(order=111, width=12, priority=65,
                                             group="Oversight")),
        # Notes & signatures
        InfoPanelComponent(
            "All figures are drawn from the Financial Metrics Registry via the "
            "Semantic Reporting Layer; commentary is generated from the same "
            "figures and cannot diverge from the statements.",
            layout=LayoutMeta(order=120, width=12, priority=10, group="Notes",
                              print_visible=False, export_visible=False)),
        SignatureBlockComponent(
            layout=LayoutMeta(order=130, width=12, priority=20, group="Formal",
                              page_break_before=True)),
    ],
))
