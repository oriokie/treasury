"""The Treasurer's Report — a comprehensive, board-ready report rebuilt on the
Generic Report Engine and enriched with the Financial Intelligence Platform.

It composes, top to bottom, from reusable components + narratives + intelligence:
an AI executive briefing, the financial health score, KPI cards, income &
expenditure, fund balances, cash position, budget performance, trust & remittance,
outstanding items, intelligence insights and board recommendations — every figure
from the Financial Metrics Registry through one shared ReportContext. Each section
supports an "Ask AI" affordance in the HTML view (handled by the engine template),
opening the assistant already aware of the report, period and section.

Runs alongside the legacy board/monthly reports; nothing existing changed.
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
    SignatureBlockComponent)
from reports.financial_statements import (FundBalancesStatementSection,
                                          IncomeExpenditureStatementSection)
from reports.intelligence_components import (AiBriefingComponent,
                                             HealthScoreComponent,
                                             InsightsComponent,
                                             RecommendationsComponent)


def _can_view_reports(user):
    from core.rights import has_right
    return roles.is_staff_role(user) or has_right(user, "view_reports")


registry.register(Report(
    key="treasurer_report",
    title="Treasurer's Report",
    description="The comprehensive board report — financial statements, health "
                "score, intelligence insights and recommendations, with an AI "
                "executive briefing. Every figure from the Financial Metrics "
                "Registry; ask the AI about any section.",
    category="Board",
    permission=_can_view_reports,
    filters=[Filter("consolidated", "Consolidate sub-accounts", kind="bool",
                    default=True)],
    sections=[
        # 1. AI executive briefing
        AiBriefingComponent(layout=LayoutMeta(order=10, width=12, priority=100,
                                              group="Summary")),
        # 2. Health score
        HealthScoreComponent(layout=LayoutMeta(order=20, width=12, priority=95,
                                               group="Summary")),
        # 3. KPI cards
        KpiCardsComponent(layout=LayoutMeta(order=30, width=12, priority=95,
                                            group="Summary")),
        # 4. Charts
        ChartComponent(ChartEngine.income_by_channel, key="chart_income",
                       title="Income by channel",
                       layout=LayoutMeta(order=40, width=6, priority=70,
                                         group="Charts", export_visible=False)),
        ChartComponent(ChartEngine.fund_closing_balances, key="chart_funds",
                       title="Fund balances",
                       layout=LayoutMeta(order=41, width=6, priority=70,
                                         group="Charts", export_visible=False)),
        # 5. Income & expenditure
        IncomeSummaryComponent(layout=LayoutMeta(order=50, width=6, priority=85,
                                                 group="Income & expense")),
        ExpenseSummaryComponent(layout=LayoutMeta(order=51, width=6, priority=85,
                                                  group="Income & expense")),
        IncomeExpenditureStatementSection(
            layout=LayoutMeta(order=52, width=12, priority=80,
                              group="Income & expense")),
        NarrativeComponent("income_analysis", title="Income analysis",
                           layout=LayoutMeta(order=53, width=6, priority=55,
                                             group="Income & expense")),
        NarrativeComponent("expense_analysis", title="Expenditure analysis",
                           layout=LayoutMeta(order=54, width=6, priority=55,
                                             group="Income & expense")),
        # 6. Funds & cash
        FundBalancesStatementSection(
            layout=LayoutMeta(order=60, width=12, priority=80, group="Funds")),
        CashPositionComponent(layout=LayoutMeta(order=61, width=6, priority=75,
                                                group="Funds")),
        NarrativeComponent("fund_performance", title="Fund performance",
                           layout=LayoutMeta(order=62, width=6, priority=55,
                                             group="Funds")),
        # 7. Budget
        BudgetSummaryComponent(layout=LayoutMeta(order=70, width=12, priority=60,
                                                 group="Budget")),
        NarrativeComponent("budget_variance", title="Budget variance",
                           layout=LayoutMeta(order=71, width=12, priority=55,
                                             group="Budget")),
        # 8. Trust & remittance
        NarrativeComponent("trust_funds", title="Trust funds & remittance",
                           layout=LayoutMeta(order=80, width=6, priority=50,
                                             group="Trust & compliance")),
        NarrativeComponent("development_projects", title="Development projects",
                           layout=LayoutMeta(order=81, width=6, priority=50,
                                             group="Trust & compliance")),
        # 9. Reconciliation & outstanding
        BankReconciliationSummaryComponent(
            layout=LayoutMeta(order=90, width=6, priority=50,
                              group="Reconciliation")),
        OutstandingItemsComponent(layout=LayoutMeta(order=91, width=6, priority=50,
                                                    group="Reconciliation")),
        # 10. Intelligence
        InsightsComponent(layout=LayoutMeta(order=100, width=12, priority=65,
                                            group="Oversight")),
        RecommendationsComponent(layout=LayoutMeta(order=101, width=12, priority=65,
                                                   group="Oversight")),
        # 11. Notes & signatures
        InfoPanelComponent(
            "All figures are drawn from the Financial Metrics Registry via the "
            "Semantic Reporting Layer. Insights, the health score and the AI "
            "briefing are analytical aids that support — but do not replace — the "
            "audited accounting figures.",
            layout=LayoutMeta(order=110, width=12, priority=10, group="Notes",
                              print_visible=True, export_visible=False)),
        SignatureBlockComponent(
            layout=LayoutMeta(order=120, width=12, priority=20, group="Formal",
                              page_break_before=True)),
    ],
))
