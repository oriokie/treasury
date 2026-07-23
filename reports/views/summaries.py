"""Split from reports/views.py (P1-2). Behaviour identical; the
package __init__ reproduces the original module namespace."""
from decimal import Decimal
from django.db.models import Sum, Count, Q
from django.views.generic import TemplateView
from core.permissions import (ReportAccessMixin, TreasurerRequiredMixin,
                              RightRequiredMixin, ReportAccessMixin)
from core.utils import parse_period, safe_json
from cashbook.models import Expense
from giving.models import Transaction
from ..exports import csv_response
import datetime as _dt
from core.models import SiteConfig
from ..services import budget as budget_svc
from ..exports import xlsx_response
from core.utils import sabbath_of


class BudgetVsActualView(ReportAccessMixin, TemplateView):
    template_name = "reports/budget_vs_actual.html"

    def get(self, request, *args, **kwargs):
        today = _dt.date.today()
        try:
            year = int(request.GET.get("year", today.year))
        except (TypeError, ValueError):
            year = today.year
        period = (request.GET.get("period") or "ANNUAL").upper()
        month = request.GET.get("month")
        quarter = request.GET.get("quarter")
        data = budget_svc.budget_vs_actual(year, period, month, quarter)
        if request.GET.get("export") in ("csv", "xlsx"):
            header = ["Fund", "Budget", "Actual", "Variance", "Variance %"]
            rows = [[r["department"].name, r["budget"], r["actual"], r["variance"],
                     (round(float(r['variance_pct']), 1) if r["variance_pct"] is not None else "")]
                    for r in data["rows"]]
            t = data["totals"]
            rows.append(["TOTAL", t["budget"], t["actual"], t["variance"],
                         (round(float(t['variance_pct']), 1) if t["variance_pct"] is not None else "")])
            fname = f"budget_vs_actual_{data['label']}".replace(" ", "_")
            if request.GET.get("export") == "xlsx":
                return xlsx_response(fname + ".xlsx", header, rows,
                                     title=f"Budget vs Actual — {data['label']}",
                                     church=SiteConfig.get().church_name)
            return csv_response(fname + ".csv", header, rows)
        ctx = self.get_context_data(**kwargs)
        ctx.update({"d": data, "year": year, "period": period,
                    "month": int(month) if month else today.month,
                    "quarter": int(quarter) if quarter else ((today.month - 1) // 3 + 1),
                    "years": range(today.year + 1, today.year - 5, -1),
                    "months": [(m, _dt.date(2000, m, 1).strftime("%B")) for m in range(1, 13)]})
        return self.render_to_response(ctx)

def _export(request, filename, header, rows, title):
    fmt = request.GET.get("export")
    if fmt == "csv":
        return csv_response(filename + ".csv", header, rows)
    if fmt == "xlsx":
        return xlsx_response(filename + ".xlsx", header, rows, title=title,
                             church=SiteConfig.get().church_name)
    return None

def _day_income_expense(start, end):
    inc = (Transaction.objects.filter(direction=Transaction.Direction.CREDIT, is_reversal=False,
           date__gte=start, date__lte=end).values("department__name")
           .annotate(t=Sum("amount")).order_by("-t"))
    eff = Q(status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
    exp = (Expense.objects.filter(eff, date__gte=start, date__lte=end)
           .values("department__name").annotate(t=Sum("amount")).order_by("-t"))
    return inc, exp

class DailySummaryView(ReportAccessMixin, TemplateView):
    template_name = "reports/daily_summary.html"

    def get(self, request, *args, **kwargs):
        try:
            day = _dt.date.fromisoformat(request.GET.get("date", ""))
        except ValueError:
            day = _dt.date.today()
        inc, exp = _day_income_expense(day, day)
        inc = list(inc); exp = list(exp)
        ti = sum((r["t"] or Decimal(0) for r in inc), Decimal(0))
        te = sum((r["t"] or Decimal(0) for r in exp), Decimal(0))
        ex = _export(request, f"daily_{day}",
                     ["Type", "Fund", "Amount"],
                     [["Income", r["department__name"] or "—", r["t"]] for r in inc]
                     + [["Expense", r["department__name"] or "—", r["t"]] for r in exp],
                     f"Daily summary {day:%d %b %Y}")
        if ex:
            return ex
        ctx = self.get_context_data(**kwargs)
        ctx.update({"day": day, "income": inc, "expense": exp,
                    "total_income": ti, "total_expense": te, "net": ti - te})
        return self.render_to_response(ctx)

class WeeklySummaryView(ReportAccessMixin, TemplateView):
    template_name = "reports/weekly_summary.html"

    def get(self, request, *args, **kwargs):
        try:
            anchor = _dt.date.fromisoformat(request.GET.get("date", ""))
        except ValueError:
            anchor = _dt.date.today()
        sab = sabbath_of(anchor)               # the Sabbath of that week
        start = sab - _dt.timedelta(days=6)     # Sun..Sat window
        inc, exp = _day_income_expense(start, sab)
        inc = list(inc); exp = list(exp)
        ti = sum((r["t"] or Decimal(0) for r in inc), Decimal(0))
        te = sum((r["t"] or Decimal(0) for r in exp), Decimal(0))
        ex = _export(request, f"week_to_{sab}",
                     ["Type", "Fund", "Amount"],
                     [["Income", r["department__name"] or "—", r["t"]] for r in inc]
                     + [["Expense", r["department__name"] or "—", r["t"]] for r in exp],
                     f"Week to Sabbath {sab:%d %b %Y}")
        if ex:
            return ex
        ctx = self.get_context_data(**kwargs)
        ctx.update({"sabbath": sab, "start": start, "income": inc, "expense": exp,
                    "total_income": ti, "total_expense": te, "net": ti - te})
        return self.render_to_response(ctx)

class CashFlowView(ReportAccessMixin, TemplateView):
    template_name = "reports/cash_flow.html"

    def get(self, request, *args, **kwargs):
        try:
            year = int(request.GET.get("year", _dt.date.today().year))
        except (TypeError, ValueError):
            year = _dt.date.today().year
        eff = Q(status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
        rows, running = [], Decimal(0)
        for m in range(1, 13):
            import calendar as _cal
            last = _cal.monthrange(year, m)[1]
            s, e = _dt.date(year, m, 1), _dt.date(year, m, last)
            inflow = (Transaction.objects.confirmed_credits()
                      .filter(date__gte=s, date__lte=e, excluded_from_income=False)
                      .aggregate(t=Sum("amount"))["t"] or Decimal(0))
            outflow = (Expense.objects.filter(eff, date__gte=s, date__lte=e)
                       .aggregate(t=Sum("amount"))["t"] or Decimal(0))
            net = inflow - outflow
            running += net
            rows.append({"month": _dt.date(year, m, 1).strftime("%B"),
                         "inflow": inflow, "outflow": outflow, "net": net, "running": running})
        ex = _export(request, f"cash_flow_{year}",
                     ["Month", "Inflow", "Outflow", "Net", "Running"],
                     [[r["month"], r["inflow"], r["outflow"], r["net"], r["running"]] for r in rows],
                     f"Cash flow {year}")
        if ex:
            return ex
        ctx = self.get_context_data(**kwargs)
        ctx.update({"year": year, "rows": rows,
                    "years": range(_dt.date.today().year, _dt.date.today().year - 6, -1),
                    "tot_in": sum((r["inflow"] for r in rows), Decimal(0)),
                    "tot_out": sum((r["outflow"] for r in rows), Decimal(0))})
        return self.render_to_response(ctx)

class CashFlowForecastView(ReportAccessMixin, TemplateView):
    """Forward-looking cash projection over 30 days / quarter / year."""
    template_name = "reports/cashflow_forecast.html"

    def get_context_data(self, **kwargs):
        from core.services import forecast
        ctx = super().get_context_data(**kwargs)
        h = forecast.horizons()
        ctx["horizons"] = h
        # a small bar/line dataset: projected position at each horizon
        ctx["chart_json"] = safe_json({
            "labels": ["Now", "30 days", "Quarter", "Year"],
            "values": [float(forecast.cash_now()),
                       float(h["30 days"]["projected"]),
                       float(h["Quarter"]["projected"]),
                       float(h["Year"]["projected"])],
        })
        return ctx
