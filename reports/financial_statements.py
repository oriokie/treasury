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
        # Honours the report's "Consolidate sub-accounts" filter. This was
        # pinned to False, so a treasurer who asked for consolidation still got
        # every sub-account itemised on the revenue list while the rest of the
        # report rolled them up — the same report disagreeing with itself about
        # what a fund is. Consolidation moves a child's receipts onto its
        # parent's line, so the total is the same either way; only the number
        # of lines changes.
        rows = ctx.fund_summary(consolidated=filters.get("consolidated", True))
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
    filters=[Filter("consolidated", "Consolidate sub-accounts", kind="bool",
                    default=True)],
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
            # An account sits in one column or the other; printing "0.00" in
            # the empty one is the oldest way to make a trial balance hard to
            # read, so the empty side stays blank.
            rows.append(Row(cells={"code": getattr(acct, "code", ""),
                                   "account": getattr(acct, "name", str(acct)),
                                   "debit": _n(r["debit"]) or None,
                                   "credit": _n(r["credit"]) or None}))
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

    def __init__(self, *args, hide_nil_lines=False, **kwargs):
        """``hide_nil_lines`` drops detail lines with no balance at the date.

        Off by default: the standalone statement and the full pack are read as
        accounting documents, where a nil line is a positive statement that the
        church holds none of that thing. The board pack turns it on, because a
        board reads for the shape of the position and every nil line is a line
        it has to look past.
        """
        super().__init__(*args, **kwargs)
        self.hide_nil_lines = hide_nil_lines

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
        # of Financial Position) — totals are unchanged. Once both are shown
        # separately, what remains is genuinely BANK ONLY (there is no more
        # "cash on hand" left uncounted — that's exactly what the petty float
        # line is) — so this line is labelled "Bank", not "Cash & bank".
        petty = _n(ctx.metric("petty_cash_balance"))
        advances = _n(ctx.metric("staff_advances_outstanding"))
        bank = cash - petty - advances
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
        # Grouped under headings, with the two subtotals and the bottom line
        # marked as what they are. The figures are unchanged: previously every
        # line — bank, petty cash, total assets, loans, net assets — arrived in
        # one undifferentiated column, so a reader had to know the statement's
        # shape already to find the three numbers it exists to give.
        detail = [
            ("Assets", None, "heading"),
            ("Bank (funds on hand)", bank),
            ("Petty cash float", petty),
            ("Staff advances (receivable)", advances),
            ("Receipts pending allocation", pending),
            ("Prepayments (unexpired)", prepaid),
            ("Total assets", total_assets, "subtotal"),
            ("Liabilities", None, "heading"),
            ("Trust funds payable", trust_payable),
            ("Outstanding loans", loan_total),
            ("Accounts payable", payables),
            ("Accrued expenses", accruals),
            ("Pending receipts (unallocated)", pending),
            ("Total liabilities", total_liabilities, "subtotal"),
            ("Net assets", net_assets, "grand"),
        ]
        note = ("Net assets is total assets less total liabilities — what the "
                "church would be left holding if every fund were settled today.")
        pairs = detail
        if self.hide_nil_lines:
            # A line the church does not have is not information here. Only
            # detail lines go; the two subtotals and the bottom line always
            # stand, because "total liabilities: nil" is itself the point.
            pairs = [p for p in detail if len(p) > 2 or p[1]]
            if len(pairs) < len(detail):
                note += " Lines with no balance at this date are not shown."
        return Section.keyvalue(self.key, self.title, pairs, note=note)


class FinancialPositionStatementSection(ComponentSection):
    """The full Statement of Financial Position — the statement itself, not a
    précis of it.

    The summary above answers "roughly where do we stand"; a board adopting
    accounts needs the document: current assets separated from fixed, the trust
    liability split into what has been receipted and what has not, borrowings
    split current against long-term, and the funds-employed section showing what
    the net assets actually consist of. Every figure is a registry metric, and
    it is the same set the standalone Statement of Financial Position reads, so
    the two cannot drift apart.
    """
    key = "financial_position_statement"
    title = "Statement of financial position"
    declared_metrics = ("fund_summary", "trust_summary", "pending_receipts_total",
                        "loans_outstanding", "petty_cash_balance",
                        "staff_advances_outstanding", "payables_outstanding",
                        "accruals_outstanding", "prepayments_unexpired",
                        "net_book_value", "fixed_assets_cost",
                        "accumulated_depreciation", "net_assets")

    def __init__(self, *args, hide_nil_lines=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.hide_nil_lines = hide_nil_lines

    def render(self, ctx, filters):
        from core.reporting.engine import Section
        as_of = ctx.end
        rows = ctx.fund_summary()
        cash = sum((_n(r["closing"]) for r in rows), Decimal(0))
        trust_payable = sum((_n(r["closing"]) for r in rows if r.get("is_trust")),
                            Decimal(0))
        local_rows = [r for r in rows if not r.get("is_trust")]
        local_funds = sum((_n(r["closing"]) for r in local_rows), Decimal(0))
        allocated = sum(
            (_n(r["closing"]) for r in local_rows
             if getattr(r["department"], "category", "") == "DEVELOPMENT"),
            Decimal(0))
        unallocated = local_funds - allocated

        # Trust money split by whether a receipt exists for it: what is firmly
        # due to the field, and what is allocated to a trust fund but not yet
        # receipted. The two sum to the trust payable that ties the statement.
        trust_receipted = sum((_n(r.get("to_remit")) for r in ctx.trust_summary()),
                              Decimal(0))
        trust_unreceipted = trust_payable - trust_receipted

        petty = _n(ctx.metric("petty_cash_balance", as_of))
        advances = _n(ctx.metric("staff_advances_outstanding", as_of))
        pending = _n(ctx.metric("pending_receipts_total", as_of))
        prepaid = _n(ctx.metric("prepayments_unexpired", as_of))
        payables = _n(ctx.metric("payables_outstanding", as_of))
        accruals = _n(ctx.metric("accruals_outstanding", as_of))
        bank = cash - petty - advances

        cost = _n(ctx.metric("fixed_assets_cost", as_of))
        depreciation = _n(ctx.metric("accumulated_depreciation", as_of))
        nbv = _n(ctx.metric("net_book_value", as_of))

        loans = ctx.loans_outstanding(as_of)
        if isinstance(loans, dict):
            loans_current = _n(loans.get("current"))
            loans_long = _n(loans.get("long_term"))
            loans_total = _n(loans.get("total"))
        else:
            loans_current, loans_long, loans_total = _n(loans), Decimal(0), _n(loans)

        current_assets = bank + petty + advances + pending + prepaid
        total_assets = current_assets + nbv
        total_liabilities = (trust_payable + payables + accruals + pending
                             + loans_total)
        net_assets = total_assets - total_liabilities
        # The fund balances are cash — what each fund has actually received and
        # paid. The assets and liabilities above carry the accrual overlay:
        # amounts prepaid are an asset the funds have already been charged for,
        # and payables and accruals are costs the funds have not been charged
        # for yet. That difference is real and has to appear, or the funds
        # section is short of net assets by exactly the accrual adjustment and
        # a reader is left to work out why.
        accrual_adj = prepaid - payables - accruals
        funds_employed = unallocated + allocated + nbv + accrual_adj - loans_total

        detail = [
            ("Assets", None, "heading"),
            ("Bank (funds on hand)", bank),
            ("Petty cash float", petty),
            ("Staff advances (receivable)", advances),
            ("Receipts pending allocation", pending),
            ("Prepayments (unexpired)", prepaid),
            ("Total current assets", current_assets, "subtotal"),
            ("Property, plant & equipment at cost", cost),
            ("Less: accumulated depreciation", -depreciation),
            ("Net book value", nbv, "subtotal"),
            ("TOTAL ASSETS", total_assets, "subtotal"),
            ("Liabilities", None, "heading"),
            ("Trust funds payable — receipted", trust_receipted),
            ("Trust funds payable — not yet receipted", trust_unreceipted),
            ("Loans payable — current", loans_current),
            ("Loans payable — long term", loans_long),
            ("Accounts payable", payables),
            ("Accrued expenses", accruals),
            ("Pending receipts (unallocated)", pending),
            ("TOTAL LIABILITIES", total_liabilities, "subtotal"),
            ("NET ASSETS", net_assets, "grand"),
            ("Financed by", None, "heading"),
            ("Unallocated (general) funds", unallocated),
            ("Allocated (board-designated) funds", allocated),
            ("Invested in property", nbv),
            ("Accrual adjustments (prepaid less payables and accruals)",
             accrual_adj),
            ("Less: borrowings to repay", -loans_total),
            ("TOTAL FUNDS", funds_employed, "subtotal"),
        ]
        note = ("Assets less liabilities equals net assets, and the funds "
                "employed below show what those net assets consist of. Trust "
                "money is the conference's and is carried as a liability until "
                "remitted; the church's own worth is the funds section.")
        pairs = detail
        if self.hide_nil_lines:
            pairs = [p for p in detail if len(p) > 2 or p[1]]
            if len(pairs) < len(detail):
                note += " Lines with no balance at this date are not shown."
        if funds_employed != net_assets:
            note = (f"WARNING: funds employed ({funds_employed:,.2f}) do not "
                    f"equal net assets ({net_assets:,.2f}). " + note)
        return Section.keyvalue(self.key, self.title, pairs, note=note)


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
    StatementOfCashFlowsView exactly so figures are identical.

    **Loans converted to donations (and write-offs).** A conversion moves no
    money: the lender's claim is extinguished and income is recognised in its
    place, recorded as a contra pair of ordinary documents. So it belongs in
    *no* cash line of this statement, and it does **not** reduce loan receipts —
    the borrowing was a real cash inflow (often in an earlier period) and
    netting the gift against it would misstate financing, potentially to a
    negative. Both legs are therefore removed: the income leg explicitly, via
    ``loan_retirement_income``; the settlement leg automatically, because it is
    a LOAN_REPAYMENT document and the operating-expense base excludes liability
    documents. What the conversion *does* require is disclosure, so it is shown
    as a non-cash memo below the statement rather than left invisible.
    """
    key = "cash_flow_statement"
    title = "Statement of cash flows"
    declared_metrics = ("fund_summary", "operating_expense", "capital_expenditure",
                        "remittances_total", "financing_activity",
                        "loan_retirement_income", "receipts_by_department")

    def __init__(self, *args, hide_nil_lines=False, **kwargs):
        """``hide_nil_lines`` drops nil detail lines, and any activity with
        neither movement nor a subtotal, so a quiet period reads as a short
        statement rather than a long one of zeros. Off by default (the
        statutory statement keeps its full shape); the board pack turns it on.
        """
        super().__init__(*args, **kwargs)
        self.hide_nil_lines = hide_nil_lines

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

        def row(label, amount, level=""):
            return Row(cells={"line": label, "amount": amount},
                       emphasis=bool(level),
                       meta={"level": level} if level else {})

        blocks = [
            ("Cash flows from operating activities", net_operating,
             "Net cash from operating activities", [
                 ("Local offerings & income received", local_operating_receipts),
                 ("Tithe & trust offerings received", trust_receipts),
                 ("Operating (recurrent) expenses paid", -operating_exp),
                 ("Remittances to the field paid", -remittances)]),
            ("Cash flows from investing activities", net_investing,
             "Net cash used in investing activities", [
                 ("Purchase of property & equipment", -capital)]),
            ("Cash flows from financing activities", net_financing,
             "Net cash from financing activities", [
                 ("Loan receipts (borrowings)", loan_receipts),
                 ("Loan principal repayments", -loan_repayments)]),
        ]
        body = []
        for heading, subtotal, subtotal_label, lines in blocks:
            if self.hide_nil_lines:
                # A church that borrowed nothing does not need two rows to say
                # so. Any activity that did move keeps its heading and its
                # subtotal, so the statement keeps its statutory three-part
                # shape whenever there is a three-part story to tell.
                lines = [(label, amount) for label, amount in lines if amount]
                if not lines and not subtotal:
                    continue
            body.append(row(heading, None, "heading"))
            body += [row(label, amount) for label, amount in lines]
            body.append(row(subtotal_label, subtotal, "subtotal"))
        body += [
            row("Net increase/(decrease) in cash", net_change, "subtotal"),
            row("Cash & bank at beginning of period", cash_open),
            row("Cash & bank at end of period", cash_open + net_change, "grand"),
        ]

        # Non-cash disclosure. A loan turned into a gift changes the church's
        # position without a shilling moving, so a board reading only the cash
        # lines would never learn it happened.
        if loan_noncash_income:
            body.append(row("Non-cash transactions (memo)", None, "heading"))
            body.append(row("Loans converted to donations / written off",
                            loan_noncash_income))

        ties = (cash_open + net_change) == cash_close
        note = ("Reconciles to the movement in cash & bank."
                if ties else
                "WARNING: cash flow does not reconcile to fund cash.")
        if loan_noncash_income:
            note += (" The memo item moved no money: the loan liability was "
                     "retired against income. It is excluded from operating "
                     "receipts and is not netted against loan receipts, which "
                     "remain the cash actually borrowed.")
        return SectionData(key=self.key, title=self.title, columns=cols,
                           rows=body, kind="table", note=note)


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

    #: the money columns, in statement order; "fund" is the label column
    FIGURES = ("opening", "receipts", "expenses", "net_transfer", "closing")

    def render(self, ctx, filters):
        rows = ctx.fund_summary(consolidated=filters.get("consolidated", True))

        def _figures(r):
            return {"opening": _n(r["opening"]), "receipts": _n(r["receipts"]),
                    "expenses": _n(r["expenses"]),
                    "net_transfer": _n(r.get("net_transfer")),
                    "closing": _n(r["closing"])}

        # A dormant fund — nothing brought forward, nothing moved, nothing left
        # — tells the board nothing and pushes the funds that matter onto a
        # second page. Anything with a balance or any movement is kept, so a
        # fund that opened and closed at zero having moved money still appears.
        live = [(r, _figures(r)) for r in rows]
        live = [(r, f) for r, f in live if any(f[k] for k in self.FIGURES)]

        # Transfers are the exception rather than the rule; when nothing moved
        # between funds the column is five rows of zeros and a lost inch of
        # page width, so it stands down entirely.
        has_transfers = any(f["net_transfer"] for _, f in live)
        figures = [k for k in self.FIGURES
                   if k != "net_transfer" or has_transfers]

        labels = {"opening": "Opening", "receipts": "Receipts",
                  "expenses": "Payments", "net_transfer": "Transfers",
                  "closing": "Closing"}
        cols = [Column("fund", "Fund")] + [
            Column(k, labels[k], numeric=True) for k in figures]

        def _block(block, heading):
            out = [Row(cells={"fund": heading}, emphasis=True,
                       meta={"level": "heading"})]
            tot = {k: Decimal(0) for k in figures}
            # Largest fund first: a board reads the top of this table and stops,
            # so the top of the table has to be where the money is.
            for _r, f in sorted(block, key=lambda rf: -rf[1]["closing"]):
                cells = {"fund": "  " + _r["department"].name}
                for k in figures:
                    tot[k] += f[k]
                    # A fund that neither received nor paid anything says so
                    # more clearly with a blank than with "0.00" — the eye then
                    # goes straight to the funds that actually moved. Opening
                    # and closing always print, even at nil.
                    cells[k] = f[k] if (f[k] or k in ("opening", "closing")) \
                        else None
                out.append(Row(cells=cells))
            out.append(Row(cells={"fund": f"  Total {heading.lower()}", **tot},
                           emphasis=True, meta={"level": "subtotal"}))
            return out, tot

        local = [(r, f) for r, f in live if not r.get("is_trust")]
        trust = [(r, f) for r, f in live if r.get("is_trust")]
        body = []
        ltot = {k: Decimal(0) for k in figures}
        ttot = {k: Decimal(0) for k in figures}
        if local:
            lblock, ltot = _block(local, "Local funds")
            body += lblock
        if trust:
            tblock, ttot = _block(trust, "Trust funds")
            body += tblock
        grand = {k: ltot[k] + ttot[k] for k in figures}
        total = Row(cells={"fund": "TOTAL ALL FUNDS", **grand}, emphasis=True,
                    meta={"level": "grand"})
        note = ("Opening + receipts − payments ± transfers = closing."
                if has_transfers else
                "Opening + receipts − payments = closing. No transfers were "
                "made between funds this period.")
        dropped = len(rows) - len(live)
        if dropped:
            note += f" {dropped} dormant fund(s) with no balance or movement " \
                    "are not listed."
        return SectionData(key=self.key, title=self.title, columns=cols,
                           rows=body, total=total, kind="table", note=note)


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
