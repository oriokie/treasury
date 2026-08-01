"""Board-pack sections: the tables a church board actually reads.

Three components live here, all of them reusable outside the board pack and all
of them fed exclusively from the Financial Metrics Registry through the shared
``ReportContext``:

* **Collections summary** — collections, trust, local and expenditure per month
  of the reporting period, with the net. The board-pack form of the Collections
  Summary report, and identical to it for identical dates.
* **Trust fund summary** — the same monthly shape, per trust account.
* **Bank reconciliation** — the bank's balance at the period end reconciled to
  the cash book, rendered only when the bank has actually told us a balance for
  that date.

Both monthly tables collapse to a single figure column when the period sits
inside one calendar month, so the same component serves "for the month of July"
and "for January to December" without the report having to choose.
"""
from __future__ import annotations

from decimal import Decimal

from core.reporting.components import ComponentSection
from core.reporting.engine import Column, Row, Section, SectionData


def _n(v):
    return v if v is not None else Decimal(0)


# ===========================================================================
# Collections summary (by month)
# ===========================================================================

class CollectionsSummaryComponent(ComponentSection):
    """Collections, the trust/local split, expenditure and net — per month of
    the period, or as a single line when the period is one month.

    Reads ``collections_summary_monthly``, which is the same credit and expense
    basis as the standalone Collections Summary report: confirmed credits that
    are not excluded from income, and effective expenses other than remittances
    (a remittance settles a trust liability; it is not the church spending).
    """
    key = "collections_summary"
    title = "Collections summary"
    declared_metrics = ("collections_summary_monthly",)

    def render(self, ctx, filters):
        data = ctx.metric("collections_summary_monthly")
        rows_data = data.get("rows") or []
        if not rows_data:
            return None
        multi = data.get("multi_month")
        cols = [Column("period", "Month" if multi else "Period"),
                Column("collections", "Collections", numeric=True, places=0),
                Column("trust", "Trust funds", numeric=True, places=0),
                Column("local", "Local funds", numeric=True, places=0),
                Column("expenditure", "Expenditure", numeric=True, places=0),
                Column("net", "Net (coll − exp)", numeric=True, places=0)]
        rows = [Row(cells={"period": r["label"],
                           "collections": r["collections"], "trust": r["trust"],
                           "local": r["local"], "expenditure": r["expenditure"],
                           "net": r["net"]},
                    meta={"down": r["net"] < 0})
                for r in rows_data]
        total = None
        if multi:
            t = data["totals"]
            total = Row(cells={"period": "TOTAL", **t}, emphasis=True)
        return SectionData(
            key=self.key, title=self.title, columns=cols, rows=rows,
            total=total, kind="table",
            note="Collections are confirmed receipts for the month; trust funds "
                 "belong to the conference and are shown separately from local "
                 "funds. Net is collections less expenditure — it is a cash "
                 "measure, not a surplus.")


# ===========================================================================
# Trust fund summary (by month)
# ===========================================================================

class TrustFundSummaryComponent(ComponentSection):
    """Trust collections per trust account, by month of the period.

    Trust accounts with nothing collected in the period are omitted, and the
    accounts are ordered by what they raised, so the table opens on the funds
    that matter. The grand total ties to the Trust funds column of the
    collections summary above it.
    """
    key = "trust_fund_summary"
    title = "Trust fund summary"
    declared_metrics = ("trust_collections_monthly",)

    def render(self, ctx, filters):
        data = ctx.metric("trust_collections_monthly")
        rows_data = data.get("rows") or []
        months = data.get("months") or []
        if not rows_data:
            return None
        multi = len(months) > 1
        cols = [Column("fund", "Trust fund")]
        if multi:
            cols += [Column(f"m{i}", b["short"], numeric=True, places=0)
                     for i, b in enumerate(months)]
        cols.append(Column("total", "Collected" if not multi else "Total",
                           numeric=True, places=0))

        rows = []
        for r in rows_data:
            cells = {"fund": r["dept"].name, "total": r["total"]}
            if multi:
                for i, v in enumerate(r["cells"]):
                    cells[f"m{i}"] = v or None      # blank, not a row of zeros
            rows.append(Row(cells=cells))
        total_cells = {"fund": "TOTAL TRUST FUNDS", "total": data["grand"]}
        if multi:
            for i, v in enumerate(data["col_totals"]):
                total_cells[f"m{i}"] = v
        return SectionData(
            key=self.key, title=self.title, columns=cols, rows=rows,
            total=Row(cells=total_cells, emphasis=True), kind="table",
            note="Trust funds are collected on behalf of the conference and are "
                 "held as a liability until remitted — they are never church "
                 "income.")


# ===========================================================================
# Bank reconciliation (as at the period end)
# ===========================================================================

class BankReconciliationComponent(ComponentSection):
    """The bank reconciliation as at the period end: the bank's own balance for
    that date, adjusted for items the bank has not yet seen, set against the
    cash book.

    The bank's figure comes from ``bank_position``, which takes whichever of the
    imported register or the live feed is nearer the date asked about. When
    neither has a balance for that date there is nothing to reconcile against,
    so the section prints no figure and says plainly that the account is
    unreconciled — a difference computed against some other date's balance
    reconciles nothing while looking as though it does.
    """
    key = "bank_reconciliation"
    title = "Bank reconciliation"
    declared_metrics = ("bank_position", "unpresented_payments_total",
                        "cash_in_transit")

    def render(self, ctx, filters):
        as_of = ctx.end
        pos = ctx.metric("bank_position", as_of)
        stmt_balance = pos.get("statement_balance")
        stmt_date = pos.get("statement_date")
        system = _n(pos.get("system_balance"))

        if stmt_balance is None:
            # No bank figure for this date means there is nothing to reconcile
            # against, and a lone cash-book balance dressed up as a
            # reconciliation would tell the board the account had been checked
            # when it has not. Say so instead, and print no figure.
            return Section.keyvalue(
                self.key, self.title,
                [("Not reconciled — no bank statement imported for "
                  f"{as_of:%d %b %Y}" if as_of else
                  "Not reconciled — no bank statement imported", None,
                  "heading")],
                note="Import the bank statement covering this period end to "
                     "complete this section.")

        unpresented = _n(ctx.metric("unpresented_payments_total", as_of))
        in_transit = _n(ctx.metric("cash_in_transit", as_of))
        adjusted = _n(stmt_balance) + in_transit - unpresented
        difference = adjusted - system

        pairs = [
            (f"Balance per bank statement at {stmt_date:%d %b %Y}"
             if stmt_date else "Balance per bank statement", _n(stmt_balance)),
            ("Add: deposits in transit", in_transit),
            ("Less: unpresented payments", -unpresented),
            ("Adjusted bank balance", adjusted, "subtotal"),
            ("Balance per cash book", system),
            ("Unreconciled difference", difference, "grand"),
        ]
        stale = pos.get("balance_stale_days")
        note = ("Reconciled — the bank and the cash book agree."
                if not difference else
                "The difference above is unexplained and should be "
                "investigated before this report is adopted.")
        if stale:
            note += (f" The bank's balance is dated {stmt_date:%d %b %Y}, "
                     f"{stale} day(s) before the period end.")
        if not pos.get("opening_configured"):
            note += (" The opening bank balance has not been set in settings, "
                     "so the cash-book figure understates the account by that "
                     "amount.")
        return Section.keyvalue(self.key, self.title, pairs, note=note)


# ===========================================================================
# Registration — so the Report Designer and other reports can compose these
# ===========================================================================

def register_components():
    """Register the board-pack tables so the Report Designer and other reports
    can compose them by key."""
    from core.reporting import component_registry
    if not component_registry.has("collections_summary"):
        component_registry.register(
            "collections_summary", lambda **k: CollectionsSummaryComponent(**k),
            label="Collections summary (by month)", category="Financial")
    if not component_registry.has("trust_fund_summary"):
        component_registry.register(
            "trust_fund_summary", lambda **k: TrustFundSummaryComponent(**k),
            label="Trust fund summary (by month)", category="Trust")
    if not component_registry.has("bank_reconciliation"):
        component_registry.register(
            "bank_reconciliation", lambda **k: BankReconciliationComponent(**k),
            label="Bank reconciliation (period end)", category="Reconciliation")
