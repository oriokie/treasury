"""Asset reports, composed on the Generic Report Engine.

Every figure comes from the Financial Metrics Registry, so these agree with the
Statement of Financial Position and the register↔ledger reconciliation by
construction rather than by coincidence. Registering them on the engine means
they join the report library and inherit its filters, permissions, print layout
and PDF/Word/Excel/CSV exports without any of that being written again here.

Four reports:

* **Fixed Asset Register** — what the church owns, at cost, depreciation to date
  and net book value.
* **Fixed Asset Movement** — opening, additions, donations, depreciation,
  disposals, closing: the note that supports the Statement of Financial Position.
* **Depreciation Schedule** — the charge for the period, per asset.
* **Disposals** — what left, what it was worth, what was received, and the
  resulting gain or loss.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from core import roles
from core.reporting import (Column, Report, Row, Section, SectionData, registry)


def _can_view_reports(user):
    from core.rights import has_right
    return has_right(user, "reports.view") or roles.is_treasurer(user) \
        or roles.is_auditor(user)


def _period(ctx):
    """Asset figures are stated at a date. For a period that has not finished,
    state them at today rather than projecting depreciation no one has charged
    yet — the same rule the statements follow."""
    start = getattr(ctx, "start", None)
    end = getattr(ctx, "end", None) or dt.date.today()
    as_at = min(end, dt.date.today())
    return start, end, as_at


def _rules_and_cfg():
    from assets.models import DepreciationRule
    from core.models import SiteConfig
    return ({r.category: r for r in DepreciationRule.objects.all()}, SiteConfig.get())


class AssetRegisterSection(Section):
    key = "asset_register"
    title = "Fixed asset register"

    def build(self, ctx, filters=None):
        from assets.models import assets_live_at
        _, _, as_at = _period(ctx)
        rules, cfg = _rules_and_cfg()
        cols = [Column("name", "Asset"), Column("category", "Class"),
                Column("fund", "Fund"), Column("acquired", "Acquired"),
                Column("cost", "Cost", numeric=True),
                Column("depreciation", "Depreciation to date", numeric=True),
                Column("nbv", "Net book value", numeric=True)]
        rows, t_cost, t_dep, t_nbv = [], Decimal(0), Decimal(0), Decimal(0)
        for a in (assets_live_at(as_at)
                  .select_related("department", "asset_class").order_by("name")):
            dep = a.accumulated_depreciation(as_at, rules=rules, cfg=cfg)
            nbv = a.net_book_value(as_at, rules=rules, cfg=cfg)
            cost = Decimal(a.cost or 0)
            t_cost += cost; t_dep += dep; t_nbv += nbv
            rows.append(Row(cells={
                "name": a.name,
                "category": (a.asset_class.name if getattr(a, "asset_class", None)
                             else a.get_category_display()),
                "fund": a.department.name if a.department else "—",
                "acquired": a.acquired_on.strftime("%d %b %Y") if a.acquired_on else "—",
                "cost": cost, "depreciation": dep, "nbv": nbv},
                url=f"/{a.pk}/"))
        rows.append(Row(cells={"name": f"{len(rows)} assets", "category": "",
                               "fund": "", "acquired": "",
                               "cost": t_cost, "depreciation": t_dep, "nbv": t_nbv},
                        emphasis=True))
        return SectionData(key=self.key, title=self.title, columns=cols, rows=rows,
                           kind="table",
                           note=f"Held as at {as_at:%d %b %Y}. Net book value here is the "
                                f"figure carried in the Statement of Financial Position.")


class AssetMovementSection(Section):
    key = "asset_movement"
    title = "Movement in fixed assets"

    def build(self, ctx, filters=None):
        from core.metrics import metrics
        start, end, as_at = _period(ctx)
        day_before = (start - dt.timedelta(days=1)) if start else None
        opening = metrics.net_book_value(day_before) if day_before else Decimal(0)
        closing = metrics.net_book_value(as_at)
        additions = metrics.asset_additions_at_cost(start, as_at)
        donated = metrics.donated_assets(start, as_at)
        depr = metrics.depreciation_expense(day_before, as_at) if day_before else Decimal(0)
        disposals = metrics.disposed_carrying_value(start, as_at)
        # additions already include donated assets (they joined the register too)
        purchased = additions - donated
        unexplained = opening + additions - depr - disposals - closing

        cols = [Column("line", "Movement"), Column("amount", "Amount", numeric=True)]
        rows = [
            Row(cells={"line": "Net book value brought forward", "amount": opening}),
            Row(cells={"line": "Assets purchased or built", "amount": purchased}),
            Row(cells={"line": "Assets donated (at fair value)", "amount": donated}),
            Row(cells={"line": "Depreciation for the period", "amount": -depr}),
            Row(cells={"line": "Assets disposed of (carrying value)", "amount": -disposals}),
        ]
        if unexplained:
            rows.append(Row(cells={"line": "Not accounted for — investigate",
                                   "amount": unexplained}))
        rows.append(Row(cells={"line": "Net book value carried forward",
                               "amount": closing}, emphasis=True))
        note = ("Supports the fixed-asset figure in the Statement of Financial "
                "Position. Every line is a recorded figure, so the movement is a "
                "genuine check: if it does not add up, the difference is shown "
                "rather than absorbed.")
        return SectionData(key=self.key, title=self.title, columns=cols, rows=rows,
                           kind="table", note=note)


class DepreciationScheduleSection(Section):
    key = "depreciation_schedule"
    title = "Depreciation for the period"

    def build(self, ctx, filters=None):
        from assets.models import FixedAsset, assets_live_at
        start, end, as_at = _period(ctx)
        rules, cfg = _rules_and_cfg()
        opening_at = (start - dt.timedelta(days=1)) if start else None
        cols = [Column("name", "Asset"), Column("basis", "Basis"),
                Column("cost", "Cost", numeric=True),
                Column("opening", "Depreciation b/f", numeric=True),
                Column("charge", "Charge for period", numeric=True),
                Column("closing", "Depreciation c/f", numeric=True),
                Column("nbv", "Net book value", numeric=True)]
        # assets held now, plus any disposed during the period (they were in use
        # until the day they left, so their charge belongs to this period)
        held = list(assets_live_at(as_at))
        left = FixedAsset.objects.filter(disposed=True, disposed_on__isnull=False,
                                         disposed_on__lte=as_at)
        if start:
            left = left.filter(disposed_on__gte=start)
        rows = []
        t_charge = t_close = Decimal(0)
        for a in sorted(held + list(left), key=lambda x: x.name):
            stop = min(as_at, a.disposed_on) if (a.disposed and a.disposed_on) else as_at
            close = a.accumulated_depreciation(stop, rules=rules, cfg=cfg)
            openv = (a.accumulated_depreciation(opening_at, rules=rules, cfg=cfg)
                     if opening_at else Decimal(0))
            charge = close - openv
            if not charge and not close:
                continue
            t_charge += charge; t_close += close
            method = a.method or (rules.get(a.category).method
                                  if rules.get(a.category) else "")
            rate = a.rate if a.rate is not None else (
                rules.get(a.category).rate if rules.get(a.category) else None)
            basis = f"{method or '—'}{f' {rate}%' if rate else ''}"
            rows.append(Row(cells={
                "name": a.name + (" (disposed)" if a.disposed else ""),
                "basis": basis, "cost": Decimal(a.cost or 0),
                "opening": openv, "charge": charge, "closing": close,
                "nbv": Decimal(a.cost or 0) - close}, url=f"/{a.pk}/"))
        rows.append(Row(cells={"name": "Total", "basis": "", "cost": "",
                               "opening": "", "charge": t_charge,
                               "closing": t_close, "nbv": ""}, emphasis=True))
        return SectionData(key=self.key, title=self.title, columns=cols, rows=rows,
                           kind="table",
                           note="Charged monthly from the date each asset was "
                                "commissioned. Land and heritage assets are not "
                                "depreciated.")


class DisposalsSection(Section):
    key = "asset_disposals"
    title = "Assets disposed of"

    def build(self, ctx, filters=None):
        from assets.models import FixedAsset
        start, end, as_at = _period(ctx)
        qs = FixedAsset.objects.filter(disposed=True, disposed_on__isnull=False,
                                       disposed_on__lte=as_at)
        if start:
            qs = qs.filter(disposed_on__gte=start)
        cols = [Column("name", "Asset"), Column("date", "Date"),
                Column("method", "How"), Column("fund", "Fund"),
                Column("cost", "Cost", numeric=True),
                Column("nbv", "Carrying value", numeric=True),
                Column("proceeds", "Proceeds", numeric=True),
                Column("result", "Gain / (loss)", numeric=True)]
        rows = []
        t_cost = t_nbv = t_proceeds = t_result = Decimal(0)
        for a in qs.select_related("disposal_fund").order_by("disposed_on"):
            cost = Decimal(a.cost or 0)
            nbv = cost - Decimal(a.accumulated_depreciation(a.disposed_on) or 0)
            proceeds = Decimal(a.disposal_proceeds or 0)
            result = Decimal(a.disposal_gain_loss or 0)
            t_cost += cost; t_nbv += nbv; t_proceeds += proceeds; t_result += result
            rows.append(Row(cells={
                "name": a.name, "date": a.disposed_on.strftime("%d %b %Y"),
                "method": a.get_disposal_method_display() if a.disposal_method else "—",
                "fund": a.disposal_fund.name if a.disposal_fund else "—",
                "cost": cost, "nbv": nbv, "proceeds": proceeds, "result": result},
                url=f"/{a.pk}/"))
        if rows:
            rows.append(Row(cells={"name": "Total", "date": "", "method": "", "fund": "",
                                   "cost": t_cost, "nbv": t_nbv,
                                   "proceeds": t_proceeds, "result": t_result},
                            emphasis=True))
        return SectionData(key=self.key, title=self.title, columns=cols, rows=rows,
                           kind="table",
                           note="The gain or loss is the proceeds less what the asset "
                                "was still worth. The proceeds themselves are a capital "
                                "receipt, not income.")


registry.register(Report(
    key="asset_register",
    title="Fixed Asset Register",
    description="Everything the church owns, with cost, depreciation to date and "
                "net book value. Ties to the Statement of Financial Position.",
    category="Assets",
    permission=_can_view_reports,
    sections=[AssetRegisterSection()],
))

registry.register(Report(
    key="asset_movement",
    title="Fixed Asset Movement",
    description="Opening net book value, additions, donations, depreciation and "
                "disposals through to closing — the note supporting the fixed-asset "
                "figure in the Statement of Financial Position.",
    category="Assets",
    permission=_can_view_reports,
    sections=[AssetMovementSection()],
))

registry.register(Report(
    key="depreciation_schedule",
    title="Depreciation Schedule",
    description="The depreciation charge for the period, asset by asset, with the "
                "basis applied and the resulting net book value.",
    category="Assets",
    permission=_can_view_reports,
    sections=[DepreciationScheduleSection()],
))

registry.register(Report(
    key="asset_disposals",
    title="Asset Disposals",
    description="Assets sold, scrapped or written off during the period, with "
                "carrying value, proceeds and the gain or loss recognised.",
    category="Assets",
    permission=_can_view_reports,
    sections=[DisposalsSection()],
))
