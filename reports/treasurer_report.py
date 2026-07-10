"""The Treasurer's Report — a comprehensive, board-ready financial report rebuilt
on the Generic Report Engine and enriched with the Financial Intelligence
Platform, presented as a professional board/audit-committee pack.

It composes, top to bottom, from reusable components + narratives + intelligence,
organised into the sections a board, business meeting, conference treasury or
auditor expects:

  1. Executive summary — an indicator band with period-on-period movement, an
     AI executive briefing, a plain-language summary and the financial-health
     score.
  2. Financial statements — the statutory set: Income & Expenditure, Statement
     of Financial Position, Statement of Cash Flows and Statement of Fund
     Balances, which reconcile with one another.
  3. Income & expenditure analysis, with commentary.
  4. Budget performance vs actual, with commentary.
  5. Funds & cash position, with commentary.
  6. Trust funds, remittance and development projects.
  7. Treasury operations — bank reconciliation and outstanding items.
  8. Financial intelligence — insights and recommendations.
  9. Board action summary — the decisions and follow-ups the board must act on.
 10. Notes & signatures.

Every figure is drawn from the Financial Metrics Registry through one shared
``ReportContext`` (the Semantic Reporting Layer); the intelligence, health score
and AI briefing are analytical aids over — never substitutes for — those audited
figures. Each section keeps its "Ask AI" affordance in the HTML view.

The report opts into a dedicated ``treasurer_board_pack.html`` presentation
template (an executive cover, a sticky section navigator, grouped/collapsible
sections and a print-optimised layout) via ``Report.html_template`` — the
section *data* is identical to any other engine report; only the presentation
differs, so the engine, the Report Designer and every other report are
unchanged. Runs alongside the legacy board/monthly reports; nothing existing
changed.
"""
from __future__ import annotations

from core import roles
from core.reporting import Filter, LayoutMeta, Report, registry
from core.reporting.charts import ChartEngine
from core.reporting.component_library import (
    BankReconciliationSummaryComponent, BudgetSummaryComponent,
    CashPositionComponent, ChartComponent, ExecutiveSummaryComponent,
    ExpenseSummaryComponent, FundSummaryComponent, IncomeSummaryComponent,
    InfoPanelComponent, NarrativeComponent, OutstandingItemsComponent,
    SignatureBlockComponent)
from reports.board_pack_components import (BoardActionSummaryComponent,
                                           ExecutiveSnapshotComponent,
                                           FundsAttentionComponent,
                                           TreasuryPositionComponent)
from reports.financial_statements import (CashFlowStatementSection,
                                          FinancialPositionSummarySection,
                                          FundBalancesStatementSection,
                                          IncomeExpenditureStatementSection)
from reports.intelligence_components import (AiBriefingComponent,
                                             HealthScoreComponent,
                                             InsightsComponent,
                                             RecommendationsComponent)

# Section groups (also the sticky-nav order in the board-pack template)
G_SUMMARY = "Executive summary"
G_STATEMENTS = "Financial statements"
G_INCOME = "Income & expenditure"
G_BUDGET = "Budget performance"
G_FUNDS = "Funds & cash"
G_TRUST = "Trust & development"
G_OPS = "Treasury operations"
G_INTEL = "Financial intelligence"
G_ACTIONS = "Board actions"
G_NOTES = "Notes & signatures"


def _can_view_reports(user):
    from core.rights import has_right
    return roles.is_staff_role(user) or has_right(user, "view_reports")


registry.register(Report(
    key="treasurer_report",
    title="Treasurer's Report",
    description="The comprehensive board report — an executive summary, the "
                "statutory financial statements, income, expenditure, budget, "
                "fund, trust and treasury-operations analysis, intelligence "
                "insights and a board action summary, with an AI executive "
                "briefing. Every figure from the Financial Metrics Registry.",
    category="Board",
    permission=_can_view_reports,
    html_template="reports/treasurer_board_pack.html",
    filters=[Filter("consolidated", "Consolidate sub-accounts", kind="bool",
                    default=True)],
    sections=[
        # ============================ 1. EXECUTIVE SUMMARY ==================
        ExecutiveSnapshotComponent(
            layout=LayoutMeta(order=10, width=12, priority=100, group=G_SUMMARY,
                              collapsible=False)),
        AiBriefingComponent(
            layout=LayoutMeta(order=12, width=12, priority=98, group=G_SUMMARY)),
        ExecutiveSummaryComponent(
            layout=LayoutMeta(order=14, width=12, priority=90, group=G_SUMMARY)),
        HealthScoreComponent(
            layout=LayoutMeta(order=16, width=12, priority=88, group=G_SUMMARY)),
        # charts render live (Chart.js) on screen and as server-side PNGs of
        # the same registry figures in the PDF/Word exports (rec #28)
        ChartComponent(ChartEngine.income_by_channel, key="chart_income",
                       title="Income by channel",
                       layout=LayoutMeta(order=20, width=4, priority=70,
                                         group=G_SUMMARY)),
        ChartComponent(ChartEngine.fund_closing_balances, key="chart_funds",
                       title="Largest fund balances",
                       layout=LayoutMeta(order=21, width=4, priority=70,
                                         group=G_SUMMARY)),
        ChartComponent(ChartEngine.local_vs_trust, key="chart_local_trust",
                       title="Local vs trust funds",
                       layout=LayoutMeta(order=22, width=4, priority=70,
                                         group=G_SUMMARY)),

        # ============================ 2. FINANCIAL STATEMENTS ==============
        IncomeExpenditureStatementSection(
            layout=LayoutMeta(order=30, width=12, priority=85,
                              group=G_STATEMENTS, page_break_before=True)),
        FinancialPositionSummarySection(
            layout=LayoutMeta(order=32, width=6, priority=84,
                              group=G_STATEMENTS)),
        CashFlowStatementSection(
            layout=LayoutMeta(order=33, width=6, priority=84,
                              group=G_STATEMENTS)),
        FundBalancesStatementSection(
            layout=LayoutMeta(order=34, width=12, priority=83,
                              group=G_STATEMENTS)),

        # ============================ 3. INCOME & EXPENDITURE ==============
        IncomeSummaryComponent(
            layout=LayoutMeta(order=40, width=6, priority=80, group=G_INCOME,
                              page_break_before=True)),
        ExpenseSummaryComponent(
            layout=LayoutMeta(order=41, width=6, priority=80, group=G_INCOME)),
        NarrativeComponent("income_analysis", title="Income analysis",
                           layout=LayoutMeta(order=42, width=6, priority=55,
                                             group=G_INCOME)),
        NarrativeComponent("expense_analysis", title="Expenditure analysis",
                           layout=LayoutMeta(order=43, width=6, priority=55,
                                             group=G_INCOME)),

        # ============================ 4. BUDGET PERFORMANCE ================
        BudgetSummaryComponent(
            layout=LayoutMeta(order=50, width=12, priority=60, group=G_BUDGET)),
        NarrativeComponent("budget_variance", title="Budget variance",
                           layout=LayoutMeta(order=51, width=12, priority=55,
                                             group=G_BUDGET)),

        # ============================ 5. FUNDS & CASH ======================
        CashPositionComponent(
            layout=LayoutMeta(order=60, width=6, priority=75, group=G_FUNDS,
                              page_break_before=True)),
        NarrativeComponent("fund_performance", title="Fund performance",
                           layout=LayoutMeta(order=61, width=6, priority=55,
                                             group=G_FUNDS)),
        FundSummaryComponent(
            layout=LayoutMeta(order=62, width=12, priority=70, group=G_FUNDS)),
        FundsAttentionComponent(
            layout=LayoutMeta(order=63, width=12, priority=68, group=G_FUNDS)),

        # ============================ 6. TRUST & DEVELOPMENT ===============
        NarrativeComponent("trust_funds", title="Trust funds & remittance",
                           layout=LayoutMeta(order=70, width=6, priority=50,
                                             group=G_TRUST)),
        NarrativeComponent("development_projects", title="Development projects",
                           layout=LayoutMeta(order=71, width=6, priority=50,
                                             group=G_TRUST)),

        # ============================ 7. TREASURY OPERATIONS ===============
        TreasuryPositionComponent(
            layout=LayoutMeta(order=79, width=6, priority=55, group=G_OPS)),
        BankReconciliationSummaryComponent(
            layout=LayoutMeta(order=80, width=6, priority=50, group=G_OPS)),
        OutstandingItemsComponent(
            layout=LayoutMeta(order=81, width=6, priority=50, group=G_OPS)),

        # ============================ 8. FINANCIAL INTELLIGENCE ============
        InsightsComponent(
            layout=LayoutMeta(order=90, width=12, priority=65, group=G_INTEL)),
        RecommendationsComponent(
            layout=LayoutMeta(order=91, width=12, priority=65, group=G_INTEL)),

        # ============================ 9. BOARD ACTIONS =====================
        BoardActionSummaryComponent(
            layout=LayoutMeta(order=100, width=12, priority=95, group=G_ACTIONS,
                              page_break_before=True, collapsible=False)),

        # ============================ 10. NOTES & SIGNATURES ===============
        InfoPanelComponent(
            "All figures are drawn from the Financial Metrics Registry via the "
            "Semantic Reporting Layer, so every statement reconciles with the "
            "others. Insights, the health score and the AI briefing are "
            "analytical aids that support — but do not replace — the audited "
            "accounting figures.",
            layout=LayoutMeta(order=110, width=12, priority=10, group=G_NOTES,
                              print_visible=True, export_visible=False)),
        SignatureBlockComponent(
            layout=LayoutMeta(order=120, width=12, priority=20, group=G_NOTES,
                              page_break_before=True)),
    ],
))
