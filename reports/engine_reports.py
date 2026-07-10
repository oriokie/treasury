"""Reusable report sections + engine-registered reports.

These prove the Generic Report Engine end-to-end without touching any existing
report. Every figure is drawn from the shared ``ReportContext`` (Semantic
Reporting Layer), so nothing here re-derives accounting logic, and the shared
context means metrics compute once per render.

Sections defined here (``FundBalancesSection``, ``IncomeMixSection``,
``TrustToRemitSection``) are written to be *reused* by future reports (the Board
Report, next phase, is expected to compose several of them).
"""
from __future__ import annotations

from decimal import Decimal

from core import roles
from core.reporting import (Column, Filter, FunctionSection, Report, Row,
                            Section, SectionData, registry)


def _num(v):
    return v if v is not None else Decimal(0)


# ===========================================================================
# Reusable sections
# ===========================================================================

class FundBalancesSection(Section):
    """Per-fund opening / receipts / expenses / transfers / closing — the fund
    summary, sourced from ``ctx.fund_summary()``. Supports drill-down: each row
    links to that fund's ledger. Reused by any report needing fund balances."""
    key = "fund_balances"
    title = "Fund balances"

    def build(self, ctx, filters):
        from django.urls import reverse
        rows_data = ctx.fund_summary(consolidated=filters.get("consolidated", True))
        columns = [
            Column("fund", "Fund", drilldown=True),
            Column("opening", "Opening", numeric=True),
            Column("receipts", "Receipts", numeric=True),
            Column("expenses", "Expenses", numeric=True),
            Column("net_transfer", "Transfers", numeric=True),
            Column("closing", "Closing", numeric=True),
        ]
        rows = []
        tot = {"opening": Decimal(0), "receipts": Decimal(0),
               "expenses": Decimal(0), "net_transfer": Decimal(0),
               "closing": Decimal(0)}
        for r in rows_data:
            dept = r["department"]
            try:
                url = reverse("report_fund", args=[dept.id])
            except Exception:  # noqa: BLE001 — drill-down is best-effort
                url = None
            cells = {
                "fund": dept.name,
                "opening": _num(r["opening"]),
                "receipts": _num(r["receipts"]),
                "expenses": _num(r["expenses"]),
                "net_transfer": _num(r.get("net_transfer")),
                "closing": _num(r["closing"]),
            }
            for k in tot:
                tot[k] += cells[k]
            rows.append(Row(cells=cells, url=url,
                            meta={"is_trust": r.get("is_trust")}))
        total = Row(cells={"fund": "Total", **tot}, emphasis=True)
        return SectionData(key=self.key, title=self.title, columns=columns,
                           rows=rows, total=total, kind="table",
                           note="Closing = opening + receipts − expenses ± "
                                "transfers. Figures from the fund_summary metric.")


class IncomeMixSection(Section):
    """Income split by channel (envelope / cash / bank), from the
    ``income_by_channel`` metric."""
    key = "income_mix"
    title = "Income by channel"

    def build(self, ctx, filters):
        data = ctx.income_by_channel()
        columns = [Column("channel", "Channel"),
                   Column("total", "Total", numeric=True),
                   Column("count", "Entries", numeric=True)]
        rows = []
        grand = Decimal(0)
        # income_by_channel returns an iterable of dict-like rows
        for r in data:
            total = _num(r.get("total"))
            grand += total
            rows.append(Row(cells={
                "channel": r.get("channel") or r.get("label") or "—",
                "total": total, "count": r.get("count") or r.get("n") or ""}))
        total_row = Row(cells={"channel": "Total income", "total": grand,
                               "count": ""}, emphasis=True)
        return SectionData(key=self.key, title=self.title, columns=columns,
                           rows=rows, total=total_row, kind="table",
                           note="Recognised income (excludes loan receipts).")


class TrustToRemitSection(Section):
    """Trust funds and the balance still to remit to the conference, from the
    ``trust_summary`` metric plus the ``trust_to_remit`` total."""
    key = "trust_to_remit"
    title = "Trust funds — still to remit"

    def build(self, ctx, filters):
        rows_data = ctx.trust_summary()
        columns = [Column("fund", "Trust fund"),
                   Column("collected", "Collected", numeric=True),
                   Column("remitted", "Remitted", numeric=True),
                   Column("to_remit", "To remit", numeric=True)]
        rows = []
        for r in rows_data:
            dept = r.get("department")
            rows.append(Row(cells={
                "fund": getattr(dept, "name", "—"),
                "collected": _num(r.get("collected")),
                "remitted": _num(r.get("remitted")),
                "to_remit": _num(r.get("to_remit"))}))
        total = Row(cells={"fund": "Total to remit", "collected": "",
                           "remitted": "", "to_remit": ctx.trust_to_remit()},
                    emphasis=True)
        return SectionData(key=self.key, title=self.title, columns=columns,
                           rows=rows, total=total, kind="table",
                           note="Conference remittance obligation as at period end.")


# ===========================================================================
# Registered reports (demonstrations — do NOT redesign existing reports)
# ===========================================================================

def _can_view_reports(user):
    """Mirror of ReportAccessMixin.test_func so engine reports enforce exactly
    the same permission as the hand-written report views: staff roles, or a
    user granted the view_reports right."""
    from core.rights import has_right
    return roles.is_staff_role(user) or has_right(user, "view_reports")


registry.register(Report(
    key="fund_overview",
    title="Fund overview (engine)",
    description="Fund balances, income mix and trust remittance — a "
                "demonstration report built entirely on the Generic Report "
                "Engine and the Semantic Reporting Layer.",
    category="Overview",
    permission=_can_view_reports,
    filters=[
        Filter("consolidated", "Consolidate sub-accounts", kind="bool",
               default=True),
    ],
    sections=[
        FundBalancesSection(),
        IncomeMixSection(),
        TrustToRemitSection(),
    ],
))
