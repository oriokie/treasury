"""A demonstration report assembled entirely from the reusable component
library, exercising every category of component and the chart engine.

This proves the phase's deliverable — that a report is *composed* from shared
components rendered through the common framework, all fed by the Semantic
Reporting Layer — without redesigning any existing report. It sits alongside
`fund_overview` (the v2.28 demo) and the hand-written reports, all untouched.
"""
from __future__ import annotations

from core import roles
from core.reporting import Filter, LayoutMeta, Report, registry
from core.reporting.charts import ChartEngine
from core.reporting.component_library import (
    BankReconciliationSummaryComponent, BudgetSummaryComponent,
    CashPositionComponent, ChartComponent, CommentaryComponent,
    ExecutiveSummaryComponent, ExpenseSummaryComponent,
    FundSummaryComponent, IncomeSummaryComponent, InfoPanelComponent,
    KpiCardsComponent, OutstandingItemsComponent, SignatureBlockComponent,
    VarianceAnalysisComponent,
)


def _can_view_reports(user):
    from core.rights import has_right
    return roles.is_staff_role(user) or has_right(user, "view_reports")


# A board-pack-style composition. Layout metadata gives each component its
# placement/priority so a future Report Designer has everything it needs; the
# renderers already honour print/export visibility.
registry.register(Report(
    key="board_pack_demo",
    title="Board pack (component demo)",
    description="A board-pack-style report assembled entirely from the reusable "
                "component library and rendered through the common rendering "
                "framework — a demonstration, not a redesign of the Board Report.",
    category="Overview",
    permission=_can_view_reports,
    filters=[
        Filter("consolidated", "Consolidate sub-accounts", kind="bool",
               default=True),
    ],
    sections=[
        ExecutiveSummaryComponent(layout=LayoutMeta(order=10, width=12,
                                                    priority=100, group="Summary")),
        KpiCardsComponent(layout=LayoutMeta(order=20, width=12, priority=95,
                                            group="Summary")),
        ChartComponent(ChartEngine.income_by_channel, key="chart_income_channel",
                       title="Income by channel",
                       layout=LayoutMeta(order=30, width=6, priority=70,
                                         group="Visual", export_visible=False)),
        ChartComponent(ChartEngine.fund_closing_balances, key="chart_fund_balances",
                       title="Fund closing balances",
                       layout=LayoutMeta(order=31, width=6, priority=70,
                                         group="Visual", export_visible=False)),
        FundSummaryComponent(layout=LayoutMeta(order=40, width=12, priority=90,
                                               group="Financial")),
        IncomeSummaryComponent(layout=LayoutMeta(order=50, width=6, priority=80,
                                                 group="Financial")),
        ExpenseSummaryComponent(layout=LayoutMeta(order=51, width=6, priority=80,
                                                  group="Financial")),
        CashPositionComponent(layout=LayoutMeta(order=60, width=6, priority=75,
                                                group="Financial")),
        BudgetSummaryComponent(layout=LayoutMeta(order=61, width=6, priority=60,
                                                 group="Financial")),
        VarianceAnalysisComponent(layout=LayoutMeta(order=70, width=12, priority=55,
                                                    group="Analysis")),
        BankReconciliationSummaryComponent(
            layout=LayoutMeta(order=80, width=6, priority=50,
                              group="Reconciliation")),
        OutstandingItemsComponent(layout=LayoutMeta(order=81, width=6, priority=50,
                                                    group="Reconciliation")),
        InfoPanelComponent(
            "Figures are drawn from the Financial Metrics Registry via the "
            "Semantic Reporting Layer. This report is a demonstration of the "
            "reusable component library and rendering framework.",
            layout=LayoutMeta(order=90, width=12, priority=10, group="Notes",
                              print_visible=False, export_visible=False)),
        SignatureBlockComponent(
            layout=LayoutMeta(order=100, width=12, priority=20, group="Formal",
                              page_break_before=True)),
    ],
))
