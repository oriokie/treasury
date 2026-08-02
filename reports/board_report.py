"""The Board / Treasurer's Report — the pack a church board is handed.

Composed entirely from reusable components and the Financial Narrative Engine,
every figure flowing from the Financial Metrics Registry through one shared
ReportContext. Each section is an independently configurable component with
layout metadata, so the composition (order, width, visibility, print/export) is
data, not code.

The pack is deliberately short. A board meets for an hour and reads what is put
in front of it, so this is the shortest set of statements that still answers the
questions a board is accountable for:

* what came in and what went out (collections summary);
* what of it was never ours (trust funds);
* what each fund is holding (statement of fund balances);
* what the church owns and owes (statement of financial position);
* where the cash actually moved (statement of cash flows);
* whether the books balance (trial balance);
* whether the bank agrees (bank reconciliation).

Every section carries an explanation written from its own figures, which the
treasurer can edit — or have the AI assistant draft — before the meeting.
Charts, budget variance, department analysis and the wider commentary set live
on the fuller Treasurer's board pack; putting them here would cost the board
the plot.
"""
from __future__ import annotations

from core import roles
from core.reporting import Filter, LayoutMeta, Report, registry
from core.reporting.component_library import (NarrativeComponent,
                                              SignatureBlockComponent)
from reports.board_sections import (BankReconciliationComponent,
                                    BoardKpiComponent,
                                    CollectionsSummaryComponent,
                                    TrustFundSummaryComponent)
from reports.financial_statements import (CashFlowStatementSection,
                                          FinancialPositionStatementSection,
                                          FundBalancesStatementSection,
                                          TrialBalanceSection)


def _can_view_reports(user):
    from core.rights import has_right
    return roles.is_staff_role(user) or has_right(user, "view_reports")


registry.register(Report(
    key="board_report_v2",
    title="Board / Treasurer's Report",
    description="The statements a church board is accountable for, with "
                "commentary generated from the same figures.",
    category="Board",
    permission=_can_view_reports,
    html_template="reports/board_pack_min.html",
    filters=[
        Filter("consolidated", "Consolidate sub-accounts", kind="bool",
               default=True),
        # The position as it stood on the closing date, rather than as it is
        # now understood: a credit banked before the date but receipted after
        # it stays in suspense, which is what a treasurer balancing on the day
        # actually saw — and what makes the bank reconciliation explain itself.
        Filter("as_reported", "Position as it stood on the date", kind="bool",
               default=False),
    ],
    sections=[
        # ---- 1. Where the church stands ---------------------------------
        BoardKpiComponent(layout=LayoutMeta(order=10, width=12, priority=100,
                                            group="Overview")),
        NarrativeComponent("executive_summary", title="Executive summary",
                           layout=LayoutMeta(order=11, width=12, priority=95,
                                             group="Overview")),

        # ---- 2. What came in --------------------------------------------
        CollectionsSummaryComponent(
            layout=LayoutMeta(order=20, width=12, priority=90,
                              group="Collections")),
        TrustFundSummaryComponent(
            layout=LayoutMeta(order=21, width=12, priority=88,
                              group="Collections")),

        # ---- 3. The statements ------------------------------------------
        FundBalancesStatementSection(
            layout=LayoutMeta(order=30, width=12, priority=86,
                              group="Statements", page_break_before=True)),
        # The statement in full, not a précis of it. A board adopting accounts
        # needs the document — current against fixed assets, the trust
        # liability split by whether it has been receipted, borrowings split
        # current against long-term, and what the net assets consist of. Full
        # width, because that is a statement and not a panel.
        FinancialPositionStatementSection(
            hide_nil_lines=True,
            layout=LayoutMeta(order=31, width=12, priority=84,
                              group="Statements")),
        CashFlowStatementSection(
            hide_nil_lines=True,
            layout=LayoutMeta(order=32, width=12, priority=84,
                              group="Statements")),

        # ---- 4. Whether the books stand up ------------------------------
        TrialBalanceSection(
            layout=LayoutMeta(order=40, width=12, priority=80,
                              group="Verification", page_break_before=True)),
        BankReconciliationComponent(
            layout=LayoutMeta(order=41, width=6, priority=80,
                              group="Verification")),
        NarrativeComponent("recommendations", title="Matters for the board",
                           layout=LayoutMeta(order=42, width=6, priority=78,
                                             group="Verification")),

        # ---- 5. Adoption -------------------------------------------------
        SignatureBlockComponent(
            layout=LayoutMeta(order=50, width=12, priority=20, group="Adoption",
                              page_break_before=True)),
    ],
))
