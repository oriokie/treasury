"""Board-pack specific report components used by the redesigned Treasurer's
Report.

Two components live here, both composed only from registered components / the
Financial Metrics Registry via the ``ReportContext`` — they introduce **no new
accounting calculation**, they only aggregate and present registry figures the
same way the existing ``KpiCards``/``CashPosition``/``CashFlow`` components
already do (summing per-fund ``fund_summary`` outputs, reading the operating /
capital / remittance metrics):

* ``ExecutiveSnapshotComponent`` — the one-page executive indicator band the
  board reads first: total receipts, total payments, net surplus/(deficit),
  closing cash position, trust still to remit, pending allocations, active
  funds, and the financial-health score — each with its movement since the
  prior equal-length period so significant changes stand out.

* ``BoardActionSummaryComponent`` — a concise "what does the board need to do"
  panel: the highest-priority recommendations from the Intelligence Engine plus
  the outstanding items still requiring settlement, so the pack closes with a
  clear action list rather than trailing off.

Both register in the component registry so the Report Designer can compose them
and future reports can reuse them.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from core.reporting.components import ComponentSection, component_registry
from core.reporting.engine import Column, Row, SectionData


def _n(v):
    return v if v is not None else Decimal(0)


def _sum(rows, key, predicate=None):
    total = Decimal(0)
    for r in rows:
        if predicate is None or predicate(r):
            total += _n(r.get(key))
    return total


def _pct_change(current, prior):
    """Signed percentage change current-vs-prior, or None when prior is zero
    (so the template shows 'new' rather than a divide-by-zero)."""
    c, p = float(current or 0), float(prior or 0)
    if p == 0:
        return None
    return (c - p) / abs(p) * 100.0


class ExecutiveSnapshotComponent(ComponentSection):
    """The executive indicator band: headline figures + period-on-period
    movement. Every value is a registry metric; the movement compares the
    report's period with the prior equal-length window using the same metrics.
    """
    key = "executive_snapshot"
    title = "Executive summary — key indicators"
    declared_metrics = ("fund_summary", "total_income", "total_payments",
                        "trust_to_remit", "pending_receipts_total",
                        "bank_position", "petty_cash_balance",
                        "staff_advances_outstanding")

    #: which cards carry a "higher is better" sentiment, for movement colouring
    _good_up = {"Total receipts", "Net surplus / (deficit)",
                "Closing cash position", "Total income recognised"}
    _good_down = {"Total payments", "Trust still to remit",
                  "Pending allocations"}

    def render(self, ctx, filters):
        rows = ctx.fund_summary()
        receipts = _sum(rows, "receipts")
        closing = _sum(rows, "closing")
        opening = _sum(rows, "opening")

        payments = _n(ctx.metric("total_payments"))
        net = receipts - payments
        to_remit = _n(ctx.trust_to_remit())
        pending = _n(ctx.metric("pending_receipts_total"))
        bank = ctx.metric("bank_position")
        petty = _n(ctx.metric("petty_cash_balance"))
        advances = _n(ctx.metric("staff_advances_outstanding"))
        active_funds = len([r for r in rows if _n(r.get("closing")) or
                            _n(r.get("receipts")) or _n(r.get("opening"))])

        # prior equal-length period, for movement
        prev = None
        if ctx.start and ctx.end:
            length = ctx.end - ctx.start
            prev_end = ctx.start - _dt.timedelta(days=1)
            prev_start = prev_end - length
            prow = ctx.metric("fund_summary", prev_start, prev_end)
            p_receipts = _sum(prow, "receipts")
            p_payments = _n(ctx.metric("total_payments", prev_start, prev_end))
            prev = {
                "Total receipts": p_receipts,
                "Total payments": p_payments,
                "Net surplus / (deficit)": p_receipts - p_payments,
                "Closing cash position": _sum(prow, "closing"),
            }
            prev_label = f"{prev_start:%d %b}–{prev_end:%d %b %Y}"
        else:
            prev_label = ""

        health = None
        try:
            from core.intelligence import compute_health_score
            hs = compute_health_score(ctx)
            health = (hs.overall, hs.band)
        except Exception:  # noqa: BLE001 — intelligence is analytical, non-blocking
            pass

        cards = [
            ("Total receipts", receipts, "money", "Funds received in the period"),
            ("Total payments", payments, "money",
             "Operating + capital + remittances"),
            ("Net surplus / (deficit)", net, "money", "Receipts less payments"),
            ("Closing cash position", closing, "money",
             "Cash & bank across all funds"),
            ("Bank balance (system)", _n(bank.get("system_balance")), "money",
             "Per the cash book"
             if bank.get("opening_configured")
             else "Opening bank balance not configured"),
            ("Petty cash float", petty, "money", "Cash in the box"),
            ("Staff advances outstanding", advances, "money",
             "Advanced, not yet accounted"),
            ("Trust still to remit", to_remit, "money",
             "Owed to the conference"),
            ("Pending allocations", pending, "money",
             "Receipts awaiting a fund"),
            ("Active funds", active_funds, "count",
             "Funds with activity or balance"),
        ]
        if health is not None:
            cards.append(("Financial health", round(health[0], 0), "score",
                          f"{health[1]} · out of 100"))

        columns = [Column("label", "Indicator"),
                   Column("value", "Value", numeric=True)]
        out_rows = []
        for label, value, fmt, sub in cards:
            meta = {"format": fmt, "sub": sub}
            if prev and label in prev:
                change = _pct_change(value, prev[label])
                meta["delta_abs"] = _n(value) - _n(prev[label])
                meta["delta_pct"] = change
                # sentiment for colouring
                if label in self._good_up:
                    meta["good"] = (meta["delta_abs"] >= 0)
                elif label in self._good_down:
                    meta["good"] = (meta["delta_abs"] <= 0)
            out_rows.append(Row(cells={"label": label, "value": value}, meta=meta))

        note = (f"Movement compares with the prior period ({prev_label}). "
                if prev_label else "")
        note += ("Every figure is drawn from the Financial Metrics Registry via "
                 "the Semantic Reporting Layer.")
        return SectionData(key=self.key, title=self.title, columns=columns,
                           rows=out_rows, kind="kpi", note=note,
                           extra={"snapshot": True})


class BoardActionSummaryComponent(ComponentSection):
    """The closing action list: prioritised board actions (from the
    Intelligence Engine's recommendations) and the outstanding items still to
    settle (from registry metrics). Gives the pack a clear ending.
    """
    key = "board_action_summary"
    title = "Board action summary"
    declared_metrics = ("trust_to_remit", "pending_receipts_total",
                        "unpresented_payments_total", "loans_outstanding",
                        "pending_expense_claims", "staff_advances_outstanding")

    def render(self, ctx, filters):
        from core.intelligence import (IntelligenceEngine,
                                       recommendations_from_insights)
        insights = IntelligenceEngine().analyse(ctx)
        recs = recommendations_from_insights(insights)[:6]

        to_remit = _n(ctx.trust_to_remit())
        pending = _n(ctx.metric("pending_receipts_total"))
        unpresented = _n(ctx.metric("unpresented_payments_total"))
        claims = ctx.metric("pending_expense_claims")
        advances = _n(ctx.metric("staff_advances_outstanding"))

        cols = [Column("kind", "Type"), Column("action", "Action / item"),
                Column("detail", "Detail / rationale")]
        rows = []
        for r in recs:
            rows.append(Row(cells={
                "kind": "Decision" if r.priority >= 4 else "Action",
                "action": r.action,
                "detail": (r.rationale or "")[:140]},
                emphasis=r.priority >= 4,
                meta={"priority": r.priority}))

        follow_ups = []
        if to_remit > 0:
            follow_ups.append(("Remit trust funds",
                               f"KES {to_remit:,.0f} owed to the conference "
                               "remains unremitted."))
        if pending > 0:
            follow_ups.append(("Clear pending allocations",
                               f"KES {pending:,.0f} of receipts await allocation "
                               "to a fund."))
        if claims and claims.get("count"):
            follow_ups.append(("Approve or reject pending expense claims",
                               f"{claims['count']} claim(s) totalling "
                               f"KES {_n(claims.get('total')):,.0f} await "
                               "treasurer approval."))
        if advances > 0:
            follow_ups.append(("Follow up staff advances",
                               f"KES {advances:,.0f} advanced to staff is not "
                               "yet accounted for by receipts."))
        if unpresented > 0:
            follow_ups.append(("Follow up unpresented payments",
                               f"KES {unpresented:,.0f} of instruments issued "
                               "have not yet cleared the bank."))
        for action, detail in follow_ups:
            rows.append(Row(cells={"kind": "Follow-up", "action": action,
                                   "detail": detail}))

        if not rows:
            return SectionData(
                key=self.key, title=self.title, columns=[], rows=[], kind="info",
                extra={"text": "No board decisions or outstanding follow-ups are "
                       "flagged for this period — controls appear to be operating "
                       "normally and obligations are settled."})

        return SectionData(key=self.key, title=self.title, columns=cols, rows=rows,
                           kind="table",
                           note="Decisions and follow-ups for the board's "
                                "attention before the next reporting period. "
                                "Derived from the intelligence insights and "
                                "outstanding-item metrics.")


class TreasuryPositionComponent(ComponentSection):
    """The treasury's cash-location position as at the period end: bank balance
    per the system vs the latest statement, petty-cash float, cash in transit,
    outstanding staff advances and pending expense claims — every figure a
    registry metric. The board's answer to "where, physically, is the money?".
    """
    key = "treasury_position"
    title = "Treasury position"
    declared_metrics = ("bank_position", "petty_cash_balance",
                        "cash_in_transit", "staff_advances_outstanding",
                        "pending_expense_claims")

    def render(self, ctx, filters):
        from core.reporting.engine import Section
        bank = ctx.metric("bank_position")
        petty = _n(ctx.metric("petty_cash_balance"))
        transit = _n(ctx.metric("cash_in_transit"))
        advances = _n(ctx.metric("staff_advances_outstanding"))
        claims = ctx.metric("pending_expense_claims")

        pairs = [
            ("Bank balance per the system", _n(bank.get("system_balance"))),
        ]
        if bank.get("statement_balance") is not None:
            stmt_date = bank.get("statement_date")
            pairs.append((f"Bank balance per statement"
                          + (f" ({stmt_date:%d %b %Y})" if stmt_date else ""),
                          _n(bank.get("statement_balance"))))
            pairs.append(("Difference to investigate",
                          _n(bank.get("difference")), True))
        pairs += [
            ("Petty cash float", petty),
            ("Cash in transit (deposits)", transit),
            ("Outstanding staff advances", advances),
            (f"Pending expense claims ({claims.get('count', 0)})",
             _n(claims.get("total"))),
        ]
        note = ("Cash locations and receivables as at the period end, from the "
                "Financial Metrics Registry.")
        if not bank.get("opening_configured"):
            note += (" NOTE: the opening bank balance has not been configured "
                     "in Financial Setup, so the system bank balance excludes "
                     "the true bank-only opening amount.")
        return Section.keyvalue(self.key, self.title, pairs, note=note)


class FundsAttentionComponent(ComponentSection):
    """Funds requiring the board's attention: overdrawn (negative closing
    balance) and dormant (no movement in the period but money still sitting).
    Canonical selectors over fund_summary — no independent balance maths.
    Hidden when there is nothing to flag.
    """
    key = "funds_attention"
    title = "Funds requiring attention"
    declared_metrics = ("negative_fund_balances", "dormant_funds")

    def render(self, ctx, filters):
        negative = ctx.metric("negative_fund_balances")
        dormant = ctx.metric("dormant_funds")
        if not negative and not dormant:
            return None   # nothing to flag — keep the pack lean

        DORMANT_SHOWN = 12   # overdrawn funds are NEVER truncated
        cols = [Column("flag", "Flag"), Column("fund", "Fund"),
                Column("closing", "Closing balance", numeric=True),
                Column("detail", "Detail")]
        rows = []
        for r in negative:
            rows.append(Row(cells={
                "flag": "Overdrawn", "fund": r["department"].name,
                "closing": _n(r["closing"]),
                "detail": "Closing balance below zero — spending has exceeded "
                          "the fund's resources."},
                emphasis=True, meta={"over": True}))
        shown = sorted(dormant, key=lambda r: abs(_n(r["closing"])),
                       reverse=True)[:DORMANT_SHOWN]
        for r in shown:
            rows.append(Row(cells={
                "flag": "Dormant", "fund": r["department"].name,
                "closing": _n(r["closing"]),
                "detail": "No receipts, expenses or transfers this period."}))
        note = ("Overdrawn funds need corrective action; dormant balances may "
                "warrant reallocation or a board decision.")
        if len(dormant) > DORMANT_SHOWN:
            note = (f"Showing the {DORMANT_SHOWN} largest of {len(dormant)} "
                    "dormant funds by balance; the Fund Balances statement "
                    "above lists every fund. " + note)
        return SectionData(key=self.key, title=self.title, columns=cols,
                           rows=rows, kind="table", note=note)


def register_components():
    """Register the board-pack components so the Report Designer and other
    reports can compose them by key."""
    if not component_registry.has("executive_snapshot"):
        component_registry.register(
            "executive_snapshot", lambda **k: ExecutiveSnapshotComponent(**k),
            label="Executive snapshot (KPIs + movement)", category="Summary")
    if not component_registry.has("board_action_summary"):
        component_registry.register(
            "board_action_summary", lambda **k: BoardActionSummaryComponent(**k),
            label="Board action summary", category="Oversight")
    if not component_registry.has("treasury_position"):
        component_registry.register(
            "treasury_position", lambda **k: TreasuryPositionComponent(**k),
            label="Treasury position (cash locations)", category="Reconciliation")
    if not component_registry.has("funds_attention"):
        component_registry.register(
            "funds_attention", lambda **k: FundsAttentionComponent(**k),
            label="Funds requiring attention", category="Funds")
