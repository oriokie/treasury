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
# Headline figures
# ===========================================================================

class BoardKpiComponent(ComponentSection):
    """The four figures a board wants before it reads anything else: what came
    in, how much of it was never ours, what went out, and what is still owed to
    the conference.

    Drawn from ``collections_summary_monthly`` — the same metric the collections
    table below is built from — so the headline and the first table cannot
    disagree. The generic ``KpiCardsComponent`` leads with income and tithe,
    which suits a giving report; a board is being asked to approve spending, so
    expenditure belongs in the top four and the tithe line does not.
    """
    key = "kpi_cards"
    title = "Key figures"
    declared_metrics = ("collections_summary_monthly", "trust_to_remit")

    def render(self, ctx, filters):
        totals = (ctx.metric("collections_summary_monthly") or {}).get("totals") or {}
        cards = [
            ("Total receipts", _n(totals.get("collections"))),
            ("Total trust funds", _n(totals.get("trust"))),
            ("Total expenditure", _n(totals.get("expenditure"))),
            ("Trust still to remit", _n(ctx.trust_to_remit())),
        ]
        columns = [Column("label", "Metric"), Column("value", "Value",
                                                     numeric=True, places=0)]
        return SectionData(
            key=self.key, title=self.title, columns=columns,
            rows=[Row(cells={"label": label, "value": value})
                  for label, value in cards],
            kind="kpi",
            note="Receipts and expenditure are cash for the period; trust funds "
                 "are the share of receipts belonging to the conference.")


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
    """The bank reconciliation the treasurer actually prepared.

    This reads the worksheet from Banking → Bank reconciliation — the one that
    was worked through and signed off, with its own reconciling items — rather
    than computing a second reconciliation of its own from metrics. Two
    reconciliations of the same account for the same date, reaching different
    differences because one knew about a cash-at-hand item the other did not,
    is worse than showing none: the board would be reading a check nobody
    performed, and would have no way to tell which figure the treasurer stood
    behind.

    The newest worksheet dated on or before the period end is used. Where none
    exists the section says the account is unreconciled and prints no figure,
    rather than implying the work was done.
    """
    key = "bank_reconciliation"
    title = "Bank reconciliation"
    declared_metrics = ()      # the worksheet is the source, not a metric

    def render(self, ctx, filters):
        from statements.models import BankReconciliation, ReconciliationItem
        as_of = ctx.end
        qs = BankReconciliation.objects.prefetch_related("items")
        if as_of:
            qs = qs.filter(statement_date__lte=as_of)
        rec = qs.order_by("-statement_date", "-id").first()

        if rec is None:
            return Section.keyvalue(
                self.key, self.title,
                [("Not reconciled — no reconciliation has been prepared"
                  + (f" for {as_of:%d %b %Y} or earlier" if as_of else ""),
                  None, "heading")],
                note="Prepare one under Banking \u2192 Bank reconciliation. "
                     "Reconciling the bank is the strongest single control over "
                     "church cash, and the board is entitled to see it before "
                     "adopting these accounts.")

        pairs = [(f"Balance per bank statement at {rec.statement_date:%d %b %Y}",
                  _n(rec.bank_balance))]
        items = list(rec.items.all())
        for item in [i for i in items
                     if i.effect == ReconciliationItem.Effect.ADD]:
            pairs.append((f"  Add: {item.description or item.get_kind_display()}",
                          _n(item.amount)))
        for item in [i for i in items
                     if i.effect == ReconciliationItem.Effect.SUBTRACT]:
            pairs.append((f"  Less: {item.description or item.get_kind_display()}",
                          -_n(item.amount)))
        pairs.append(("Adjusted bank balance", _n(rec.adjusted_balance),
                      "subtotal"))
        if rec.book_balance is not None:
            pairs.append(("Balance per cash book", _n(rec.book_balance)))
            pairs.append(("Unreconciled difference", _n(rec.difference),
                          "grand"))

        if rec.book_balance is None:
            note = ("No cash-book balance was entered on this worksheet, so it "
                    "shows the adjusted bank balance with nothing to compare "
                    "it against.")
        elif rec.is_reconciled:
            note = ("Reconciled \u2014 the bank and the cash book agree at "
                    f"{rec.statement_date:%d %b %Y}.")
        else:
            note = ("The difference above is unexplained and should be resolved "
                    "before these accounts are adopted.")
        if as_of and rec.statement_date < as_of:
            gap = (as_of - rec.statement_date).days
            note += (f" This is the most recent worksheet and it is dated {gap} "
                     f"day(s) before the period end, so the closing bank "
                     f"balance shown elsewhere in this pack has not itself "
                     f"been reconciled.")
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
