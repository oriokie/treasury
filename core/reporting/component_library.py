"""The reusable report component library.

Concrete, registered components every report can compose. Each draws figures
only from the ``ReportContext`` (Semantic Reporting Layer → Financial Metrics
Registry), carries layout metadata, and records its metric dependencies.

Components included this phase:
  Executive Summary · KPI Cards · Financial Statement · Fund Summary ·
  Income Summary · Expense Summary · Budget Summary · Cash Position ·
  Bank Reconciliation Summary · Outstanding Items · Variance Analysis ·
  Chart · Table · Commentary · Signature Block · Appendix · Info Panel.

Every component is registered in ``component_registry`` so reports compose by
name and future modules can add their own without touching the engine.
"""
from __future__ import annotations

from decimal import Decimal

from core.reporting.charts import ChartEngine, ChartSpec
from core.reporting.components import ComponentSection, component_registry
from core.reporting.engine import Column, Row, SectionData
from core.reporting.layout import LayoutMeta


def _n(v):
    return v if v is not None else Decimal(0)


# ===========================================================================
# KPI cards & executive summary
# ===========================================================================

class KpiCardsComponent(ComponentSection):
    """A band of headline KPI cards (income, tithe, fund balance, trust to
    remit). Rendered as a ``keyvalue`` kind with card styling in HTML."""
    key = "kpi_cards"
    title = "Key figures"
    declared_metrics = ("total_income", "tithe", "fund_summary", "trust_to_remit")

    def render(self, ctx, filters):
        rows = ctx.fund_summary()
        closing = sum((_n(r["closing"]) for r in rows), Decimal(0))
        cards = [
            ("Total income", ctx.total_income()),
            ("Tithe", ctx.tithe()),
            ("Net fund balance", closing),
            ("Trust still to remit", ctx.trust_to_remit()),
        ]
        columns = [Column("label", "Metric"), Column("value", "Value", numeric=True)]
        data = SectionData(
            key=self.key, title=self.title, columns=columns,
            rows=[Row(cells={"label": l, "value": v}) for l, v in cards],
            kind="kpi")
        return data


class ExecutiveSummaryComponent(ComponentSection):
    """A short plain-language executive summary derived from headline metrics —
    a commentary component whose text is generated, not hand-written."""
    key = "executive_summary"
    title = "Executive summary"
    declared_metrics = ("total_income", "trust_to_remit", "fund_summary")

    def render(self, ctx, filters):
        income = _n(ctx.total_income())
        to_remit = _n(ctx.trust_to_remit())
        rows = ctx.fund_summary()
        closing = sum((_n(r["closing"]) for r in rows), Decimal(0))
        period = ""
        if ctx.start and ctx.end:
            period = f" for {ctx.start:%d %b %Y}–{ctx.end:%d %b %Y}"
        lines = [
            f"Recognised income{period} was KES {income:,.0f}.",
            f"Net fund balances stand at KES {closing:,.0f} across all funds.",
        ]
        if to_remit > 0:
            lines.append(f"KES {to_remit:,.0f} of trust funds remains to be "
                         "remitted to the conference.")
        else:
            lines.append("All trust funds collected have been remitted.")
        return SectionData(key=self.key, title=self.title, columns=[], rows=[],
                           kind="commentary", extra={"text": " ".join(lines)})


# ===========================================================================
# Fund / income / expense / budget summaries
# ===========================================================================

class FundSummaryComponent(ComponentSection):
    """Per-fund opening / receipts / expenses / transfers / closing, with
    drill-down to each fund's ledger."""
    key = "fund_summary"
    title = "Fund balances"
    declared_metrics = ("fund_summary",)

    def render(self, ctx, filters):
        from django.urls import reverse
        rows_data = ctx.fund_summary(
            consolidated=filters.get("consolidated", True))
        rows_data = sorted(rows_data, key=lambda r: r["department"].name.lower())
        columns = [
            Column("fund", "Fund", drilldown=True),
            Column("opening", "Opening", numeric=True),
            Column("receipts", "Receipts", numeric=True),
            Column("expenses", "Expenses", numeric=True),
            Column("net_transfer", "Transfers", numeric=True),
            Column("closing", "Closing", numeric=True),
        ]
        tot = {k: Decimal(0) for k in
               ("opening", "receipts", "expenses", "net_transfer", "closing")}
        rows = []
        for r in rows_data:
            dept = r["department"]
            try:
                url = reverse("report_fund", args=[dept.id])
            except Exception:  # noqa: BLE001
                url = None
            cells = {"fund": dept.name, "opening": _n(r["opening"]),
                     "receipts": _n(r["receipts"]), "expenses": _n(r["expenses"]),
                     "net_transfer": _n(r.get("net_transfer")),
                     "closing": _n(r["closing"])}
            for k in tot:
                tot[k] += cells[k]
            rows.append(Row(cells=cells, url=url,
                            meta={"is_trust": r.get("is_trust")}))
        total = Row(cells={"fund": "Total", **tot}, emphasis=True)
        return SectionData(key=self.key, title=self.title, columns=columns,
                           rows=rows, total=total, kind="table",
                           note="Closing = opening + receipts − expenses ± transfers.")


class IncomeSummaryComponent(ComponentSection):
    """Income by channel with a total — the income-side summary."""
    key = "income_summary"
    title = "Income summary"
    declared_metrics = ("income_by_channel", "total_income")

    def render(self, ctx, filters):
        data = ctx.income_by_channel()
        columns = [Column("channel", "Channel"),
                   Column("total", "Total", numeric=True),
                   Column("count", "Entries", numeric=True)]
        rows = []
        for r in data:
            rows.append(Row(cells={
                "channel": r.get("channel") or r.get("label") or "—",
                "total": _n(r.get("total")),
                "count": r.get("count") or r.get("n") or ""}))
        total = Row(cells={"channel": "Total income",
                           "total": _n(ctx.total_income()), "count": ""},
                    emphasis=True)
        return SectionData(key=self.key, title=self.title, columns=columns,
                           rows=rows, total=total, kind="table",
                           note="Recognised income (excludes loan receipts).")


class ExpenseSummaryComponent(ComponentSection):
    """Operating expenses by fund, from the expenses_by_department metric."""
    key = "expense_summary"
    title = "Expenditure summary"
    declared_metrics = ("expenses_by_department", "fund_summary")

    def render(self, ctx, filters):
        # expenses_by_department returns {dept_id: total}; join to fund names via
        # fund_summary (both from the context, both memoized)
        exp = ctx.expenses_by_department()
        names = {r["department"].id: r["department"].name
                 for r in ctx.fund_summary(consolidated=False)}
        columns = [Column("fund", "Fund"), Column("amount", "Expenditure", numeric=True)]
        rows, grand = [], Decimal(0)
        for dept_id, amount in sorted(exp.items(), key=lambda kv: kv[1] or 0,
                                      reverse=True):
            amt = _n(amount)
            if not amt:
                continue
            grand += amt
            rows.append(Row(cells={"fund": names.get(dept_id, "—"), "amount": amt}))
        total = Row(cells={"fund": "Total expenditure", "amount": grand},
                    emphasis=True)
        return SectionData(key=self.key, title=self.title, columns=columns,
                           rows=rows, total=total, kind="table")


class BudgetSummaryComponent(ComponentSection):
    """Budget vs actual per fund. Reads actuals from fund_summary and budgets
    from the department annual_budget field (a configuration attribute, not an
    accounting figure), computing variance."""
    key = "budget_summary"
    title = "Budget vs actual"
    declared_metrics = ("fund_summary",)

    def render(self, ctx, filters):
        rows_data = ctx.fund_summary(consolidated=False)
        columns = [Column("fund", "Fund"),
                   Column("budget", "Budget", numeric=True),
                   Column("actual", "Actual", numeric=True),
                   Column("variance", "Variance", numeric=True),
                   Column("pct", "% used", numeric=True)]
        rows = []
        any_budget = False
        for r in rows_data:
            dept = r["department"]
            budget = getattr(dept, "annual_budget", None)
            if not budget:
                continue
            any_budget = True
            actual = _n(r["expenses"])
            variance = _n(budget) - actual
            pct = (float(actual) / float(budget) * 100) if budget else 0
            rows.append(Row(cells={
                "fund": dept.name, "budget": _n(budget), "actual": actual,
                "variance": variance, "pct": round(pct, 1)},
                emphasis=False, meta={"over": variance < 0}))
        if not any_budget:
            return None   # hide_if_empty: no budgets configured
        return SectionData(key=self.key, title=self.title, columns=columns,
                           rows=rows, kind="table",
                           note="Variance = budget − actual; negative means over budget.")


# ===========================================================================
# Cash position, reconciliation, outstanding items, variance
# ===========================================================================

class CashPositionComponent(ComponentSection):
    """Cash position: total closing fund balances split into local vs trust,
    plus the opening cash position — a keyvalue panel."""
    key = "cash_position"
    title = "Cash position"
    declared_metrics = ("fund_summary", "opening_cash_position", "trust_to_remit")

    def render(self, ctx, filters):
        from core.reporting.engine import Section
        rows = ctx.fund_summary()
        closing = sum((_n(r["closing"]) for r in rows), Decimal(0))
        trust = sum((_n(r["closing"]) for r in rows if r.get("is_trust")), Decimal(0))
        local = closing - trust
        opening = _n(ctx.metric("opening_cash_position"))
        pairs = [
            ("Opening cash position", opening),
            ("Local funds (closing)", local),
            ("Trust funds (closing)", trust),
            ("Total funds (closing)", closing, True),
            ("Trust still to remit", _n(ctx.trust_to_remit())),
        ]
        return Section.keyvalue(self.key, self.title, pairs)


class BankReconciliationSummaryComponent(ComponentSection):
    """Bank reconciliation summary: unpresented payments still outstanding as at
    the period end, from the payments metric. A concise reconciling panel (not
    the full reconciliation screen)."""
    key = "bank_recon_summary"
    title = "Bank reconciliation summary"
    declared_metrics = ("unpresented_payments_total",)

    def render(self, ctx, filters):
        as_of = ctx.end
        unpresented = _n(ctx.metric("unpresented_payments_total", as_of))
        pairs = [
            (f"Unpresented payments as at {as_of:%d %b %Y}" if as_of
             else "Unpresented payments", unpresented, True),
        ]
        from core.reporting.engine import Section
        data = Section.keyvalue(self.key, self.title, pairs,
                                note="Instruments issued but not yet cleared at "
                                     "the bank — the 'less unpresented' figure.")
        return data


class OutstandingItemsComponent(ComponentSection):
    """Outstanding items across the ledger: pending (unallocated) bank receipts,
    trust still to remit, and unpresented payments — the 'loose ends' panel."""
    key = "outstanding_items"
    title = "Outstanding items"
    declared_metrics = ("pending_receipts_total", "trust_to_remit",
                        "unpresented_payments_total", "loans_outstanding")

    def render(self, ctx, filters):
        as_of = ctx.end
        pending = _n(ctx.metric("pending_receipts_total", as_of))
        to_remit = _n(ctx.trust_to_remit())
        unpresented = _n(ctx.metric("unpresented_payments_total", as_of))
        loans = ctx.loans_outstanding(as_of)
        loan_total = loans.get("total") if isinstance(loans, dict) else _n(loans)
        columns = [Column("item", "Item"), Column("amount", "Amount", numeric=True)]
        rows = [
            Row(cells={"item": "Bank receipts pending allocation", "amount": pending}),
            Row(cells={"item": "Trust funds still to remit", "amount": to_remit}),
            Row(cells={"item": "Unpresented payments", "amount": unpresented}),
            Row(cells={"item": "Outstanding loan liability", "amount": _n(loan_total)}),
        ]
        return SectionData(key=self.key, title=self.title, columns=columns,
                           rows=rows, kind="table",
                           note="Items requiring follow-up or settlement.")


class VarianceAnalysisComponent(ComponentSection):
    """Period-over-period variance of income by fund: this period's receipts vs
    the prior equal-length period, from fund_summary computed for each window."""
    key = "variance_analysis"
    title = "Variance analysis (vs prior period)"
    declared_metrics = ("receipts_by_department", "fund_summary")

    def render(self, ctx, filters):
        import datetime as _dt
        if not (ctx.start and ctx.end):
            return None   # variance needs a bounded period
        length = (ctx.end - ctx.start)
        prev_end = ctx.start - _dt.timedelta(days=1)
        prev_start = prev_end - length
        cur = ctx.receipts_by_department()  # {dept_id: total} for the period
        # prior period via the registry metric directly (different args → its own
        # memo slot on the context)
        prev = ctx.metric("receipts_by_department", prev_start, prev_end)
        names = {r["department"].id: r["department"].name
                 for r in ctx.fund_summary(consolidated=False)}
        columns = [Column("fund", "Fund"),
                   Column("current", "This period", numeric=True),
                   Column("prior", "Prior period", numeric=True),
                   Column("change", "Change", numeric=True)]
        rows = []
        for dept_id in sorted(set(cur) | set(prev),
                              key=lambda d: _n(cur.get(d)) , reverse=True):
            c = _n(cur.get(dept_id)); p = _n(prev.get(dept_id))
            if not c and not p:
                continue
            rows.append(Row(cells={"fund": names.get(dept_id, "—"),
                                   "current": c, "prior": p, "change": c - p},
                            meta={"down": (c - p) < 0}))
        return SectionData(key=self.key, title=self.title, columns=columns,
                           rows=rows, kind="table",
                           note=f"Prior period: {prev_start:%d %b}–{prev_end:%d %b %Y}.")


# ===========================================================================
# Charts, commentary, signature, appendix, info panel
# ===========================================================================

class ChartComponent(ComponentSection):
    """Wrap any ChartSpec-producing callable as a component. The callable
    receives the context and returns a ChartSpec (built via ChartEngine, so it's
    metric-driven)."""
    key = "chart"
    title = "Chart"

    def __init__(self, spec_fn, key=None, title=None, layout=None, permission=None):
        super().__init__(key=key or "chart", title=title or "Chart",
                         layout=layout, permission=permission)
        self._spec_fn = spec_fn

    def render(self, ctx, filters):
        spec = self._spec_fn(ctx)
        if spec is None:
            return None
        return SectionData(key=self.key, title=self.title or spec.title,
                           columns=[], rows=[], kind="chart",
                           extra={"chart": spec.to_config(),
                                  "chart_type": spec.chart_type,
                                  "metrics_used_chart": spec.metrics_used})


class NarrativeComponent(ComponentSection):
    """Render a Financial Narrative Engine narrative as a report section. The
    narrative draws its figures from the same ReportContext the rest of the
    report uses, so its prose can never contradict the tables. ``narrative_key``
    names a registered narrative; ``config`` a NarrativeConfig (style/tone/
    thresholds)."""
    key = "narrative"
    title = "Commentary"

    def __init__(self, narrative_key, config=None, key=None, title=None,
                 layout=None, permission=None):
        super().__init__(key=key or f"narrative_{narrative_key}",
                         title=title, layout=layout, permission=permission)
        self._narrative_key = narrative_key
        self._config = config

    def render(self, ctx, filters):
        from core.reporting.narrative import NarrativeEngine
        engine = NarrativeEngine(self._config)
        result = engine.generate(self._narrative_key, ctx)
        title = self.title or result.title
        if not result.text:
            return None
        data = SectionData(key=self.key, title=title, columns=[], rows=[],
                           kind="commentary",
                           extra={"text": result.text,
                                  "findings": [f.as_dict() for f in result.findings]})
        # record narrative provenance for the dependency map
        data.extra["metrics_used"] = result.metrics_used
        return data


class CommentaryComponent(ComponentSection):
    """A static or callable commentary panel. ``text`` may be a string or a
    ``fn(ctx, filters) -> str``."""
    key = "commentary"
    title = "Commentary"

    def __init__(self, text="", key=None, title=None, layout=None, permission=None):
        super().__init__(key=key or "commentary", title=title or "Commentary",
                         layout=layout, permission=permission)
        self._text = text

    def render(self, ctx, filters):
        text = self._text(ctx, filters) if callable(self._text) else self._text
        if not text:
            return None
        return SectionData(key=self.key, title=self.title, columns=[], rows=[],
                           kind="commentary", extra={"text": text})


class SignatureBlockComponent(ComponentSection):
    """A signature block (prepared by / reviewed by / approved by) for formal
    reports. Presentation-only — no financial data."""
    key = "signature_block"
    title = "Signatures"

    def __init__(self, roles=("Prepared by", "Reviewed by", "Approved by"),
                 key=None, title=None, layout=None, permission=None):
        super().__init__(key=key or "signature_block",
                         title=title or "Signatures", layout=layout,
                         permission=permission)
        self._roles = roles

    def render(self, ctx, filters):
        columns = [Column("role", "Role"), Column("name", "Name"),
                   Column("date", "Date")]
        rows = [Row(cells={"role": r, "name": "", "date": ""}) for r in self._roles]
        data = SectionData(key=self.key, title=self.title, columns=columns,
                           rows=rows, kind="signature")
        data.layout = getattr(self, "layout", None)
        return data


class AppendixComponent(ComponentSection):
    """An appendix panel holding supplementary tabular content supplied by a
    callable ``fn(ctx, filters) -> SectionData`` (still metric-sourced)."""
    key = "appendix"
    title = "Appendix"

    def __init__(self, fn, key=None, title=None, layout=None, permission=None):
        super().__init__(key=key or "appendix", title=title or "Appendix",
                         layout=layout or LayoutMeta(order=900, priority=10),
                         permission=permission)
        self._fn = fn

    def render(self, ctx, filters):
        return self._fn(ctx, filters)


class InfoPanelComponent(ComponentSection):
    """A supporting informational panel (methodology notes, period definition,
    data-source caveats). Presentation-only."""
    key = "info_panel"
    title = "About this report"

    def __init__(self, text, key=None, title=None, layout=None, permission=None):
        super().__init__(key=key or "info_panel",
                         title=title or "About this report",
                         layout=layout or LayoutMeta(order=950, priority=5,
                                                     export_visible=False),
                         permission=permission)
        self._text = text

    def render(self, ctx, filters):
        return SectionData(key=self.key, title=self.title, columns=[], rows=[],
                           kind="info", extra={"text": self._text})


class FinancialStatementComponent(ComponentSection):
    """A generic financial-statement panel: a labelled list of line items with a
    computed total, built from a spec of (label, metric_or_value) rows. Powers
    income-statement / position-style panels without bespoke code."""
    key = "financial_statement"
    title = "Financial statement"

    def __init__(self, lines, key=None, title=None, total_label="Total",
                 layout=None, permission=None):
        super().__init__(key=key or "financial_statement",
                         title=title or "Financial statement",
                         layout=layout, permission=permission)
        self._lines = lines            # list[(label, metric_key_or_callable)]
        self._total_label = total_label

    def render(self, ctx, filters):
        pairs = []
        total = Decimal(0)
        used = []
        for label, source in self._lines:
            if callable(source):
                value = _n(source(ctx))
            else:
                value = _n(ctx.metric(source))
                used.append(source)
            pairs.append((label, value))
            total += value
        pairs.append((self._total_label, total, True))
        from core.reporting.engine import Section
        data = Section.keyvalue(self.key, self.title, pairs)
        data.extra["metrics_used"] = used
        return data


# ===========================================================================
# Registration
# ===========================================================================

def _register_all():
    reg = component_registry
    reg.register("kpi_cards", lambda **k: KpiCardsComponent(**k),
                 label="KPI cards", category="Summary")
    reg.register("executive_summary", lambda **k: ExecutiveSummaryComponent(**k),
                 label="Executive summary", category="Summary")
    reg.register("fund_summary", lambda **k: FundSummaryComponent(**k),
                 label="Fund summary", category="Financial")
    reg.register("income_summary", lambda **k: IncomeSummaryComponent(**k),
                 label="Income summary", category="Financial")
    reg.register("expense_summary", lambda **k: ExpenseSummaryComponent(**k),
                 label="Expenditure summary", category="Financial")
    reg.register("budget_summary", lambda **k: BudgetSummaryComponent(**k),
                 label="Budget vs actual", category="Financial")
    reg.register("cash_position", lambda **k: CashPositionComponent(**k),
                 label="Cash position", category="Financial")
    reg.register("bank_recon_summary",
                 lambda **k: BankReconciliationSummaryComponent(**k),
                 label="Bank reconciliation summary", category="Reconciliation")
    reg.register("outstanding_items", lambda **k: OutstandingItemsComponent(**k),
                 label="Outstanding items", category="Reconciliation")
    reg.register("variance_analysis", lambda **k: VarianceAnalysisComponent(**k),
                 label="Variance analysis", category="Analysis")
    reg.register("chart", lambda spec_fn=None, **k: ChartComponent(spec_fn, **k),
                 label="Chart", category="Visual", designer_safe=False,
                 description="Needs a Python chart-spec function — composed "
                            "in code-defined reports only, not the designer.")
    reg.register("commentary", lambda text="", **k: CommentaryComponent(text, **k),
                 label="Commentary", category="Narrative",
                 params_schema=[{"name": "text", "label": "Text",
                                 "kind": "textarea", "required": True}])
    reg.register("narrative",
                 lambda narrative_key=None, **k: NarrativeComponent(narrative_key, **k),
                 label="Narrative (auto-generated)", category="Narrative",
                 params_schema=[{"name": "narrative_key", "label": "Narrative",
                                 "kind": "select", "source": "narratives",
                                 "required": True}])
    reg.register("signature_block", lambda **k: SignatureBlockComponent(**k),
                 label="Signature block", category="Formal")
    reg.register("appendix", lambda fn=None, **k: AppendixComponent(fn, **k),
                 label="Appendix", category="Formal", designer_safe=False,
                 description="Needs a Python render function — composed in "
                            "code-defined reports only, not the designer.")
    reg.register("info_panel", lambda text="", **k: InfoPanelComponent(text, **k),
                 label="Info panel", category="Narrative",
                 params_schema=[{"name": "text", "label": "Text",
                                 "kind": "textarea", "required": True}])
    reg.register("financial_statement",
                 lambda lines=(), **k: FinancialStatementComponent(lines, **k),
                 label="Financial statement", category="Financial",
                 designer_safe=False,
                 description="Needs a Python list of (label, metric-or-"
                            "callable) lines — composed in code-defined "
                            "reports only, not the designer.")


_register_all()
