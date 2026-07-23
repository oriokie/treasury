"""Split from reports/views.py (P1-2). Behaviour identical; the
package __init__ reproduces the original module namespace."""
from django.views.generic import TemplateView
from core.permissions import (ReportAccessMixin, TreasurerRequiredMixin,
                              RightRequiredMixin, ReportAccessMixin)
from ..exports import csv_response
import datetime as dt
from ..services import monthly
from core.models import SiteConfig
from ..exports import xlsx_response
from ._shared import PeriodMixin


def _year_from(request):
    try:
        return int(request.GET.get("year", dt.date.today().year))
    except (ValueError, TypeError):
        return dt.date.today().year

class MonthlyAccountsView(ReportAccessMixin, TemplateView):
    template_name = "reports/monthly_accounts.html"

    def get(self, request, *args, **kwargs):
        year = _year_from(request)
        coll = monthly.collections_by_account(year)
        exp = monthly.expenses_by_account(year)
        if request.GET.get("export") == "csv":
            header = ["Account"] + [lbl for _, lbl in coll["months"]] + ["Total"]
            rows = [["— COLLECTIONS —"]]
            for r in coll["rows"]:
                rows.append([str(r["dept"])] + r["cells"] + [r["total"]])
            rows.append(["Total collections"] + coll["col_totals"] + [coll["grand"]])
            rows.append(["— EXPENSES —"])
            for r in exp["rows"]:
                rows.append([str(r["dept"])] + r["cells"] + [r["total"]])
            rows.append(["Total expenses"] + exp["col_totals"] + [exp["grand"]])
            return csv_response(f"accounts_{year}.csv", header, rows)
        ctx = self.get_context_data(**kwargs)
        ctx.update(year=year, coll=coll, exp=exp,
                   years=range(dt.date.today().year, dt.date.today().year - 6, -1))
        return self.render_to_response(ctx)

class TrustMonthlyView(ReportAccessMixin, TemplateView):
    template_name = "reports/trust_monthly.html"

    def get(self, request, *args, **kwargs):
        year = _year_from(request)
        data = monthly.trust_monthly(year)
        if request.GET.get("export") == "csv":
            header = ["Trust account"] + [lbl for _, lbl in data["months"]] + ["Total"]
            rows = [[str(r["dept"])] + r["cells"] + [r["total"]] for r in data["rows"]]
            rows.append(["TOTAL TRUST FUNDS"] + data["col_totals"] + [data["grand"]])
            return csv_response(f"trust_monthly_{year}.csv", header, rows)
        ctx = self.get_context_data(**kwargs)
        ctx.update(year=year, d=data,
                   years=range(dt.date.today().year, dt.date.today().year - 6, -1))
        return self.render_to_response(ctx)

class CollectionsSummaryView(ReportAccessMixin, TemplateView):
    template_name = "reports/collections_summary.html"

    def get(self, request, *args, **kwargs):
        year = _year_from(request)
        data = monthly.collections_summary(year)
        if request.GET.get("export") == "csv":
            header = ["Month", "Collections", "Trust funds", "Local funds",
                      "Expenditure", "Net"]
            rows = [[r["month"], r["collections"], r["trust"], r["local"],
                     r["expenditure"], r["net"]] for r in data["rows"]]
            rows.append(["TOTAL", data["tot_collections"], data["tot_trust"],
                         data["tot_local"], data["tot_expenditure"], data["tot_net"]])
            return csv_response(f"collections_summary_{year}.csv", header, rows)
        ctx = self.get_context_data(**kwargs)
        ctx.update(year=year, d=data,
                   years=range(dt.date.today().year, dt.date.today().year - 6, -1))
        return self.render_to_response(ctx)

class CollectionsDetailView(PeriodMixin, TemplateView):
    """Detailed collections for any chosen period, broken down by fund. The grand
    total reconciles to the Collections figure on the Collections Summary for the
    same dates. Exports to Excel (.xlsx) and CSV."""
    template_name = "reports/collections_detail.html"

    def get(self, request, *args, **kwargs):
        s, e = self.period()
        data = monthly.collections_detail(s, e)
        export = request.GET.get("export")
        if export in ("xlsx", "csv"):
            header = ["Fund", "Type", "Receipts", "Collected"]
            rows = [[r["fund"], r["type"], r["n"], float(r["amount"])] for r in data["rows"]]
            rows.append(["Trust funds — subtotal", "", "", float(data["tot_trust"])])
            rows.append(["Local funds — subtotal", "", "", float(data["tot_local"])])
            rows.append(["TOTAL COLLECTIONS", "", data["n_receipts"], float(data["tot_collections"])])
            fname = f"collections_detail_{s}_{e}"
            if export == "csv":
                return csv_response(fname + ".csv", header, rows)
            from reports.exports import xlsx_response
            from core.models import SiteConfig
            return xlsx_response(fname + ".xlsx", header, rows,
                                 title=f"Collections detail ({s} to {e})",
                                 church=SiteConfig.get().church_name)
        ctx = self.get_context_data(**kwargs)
        ctx.update(d=data)
        return self.render_to_response(ctx)
