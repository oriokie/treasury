"""Split from reports/views.py (P1-2). Behaviour identical; the
package __init__ reproduces the original module namespace."""
from django.views.generic import TemplateView
from core.permissions import (ReportAccessMixin, TreasurerRequiredMixin,
                              RightRequiredMixin, ReportAccessMixin)
from ..exports import csv_response
import datetime as dt
from ..services import envelope_reports
from core.utils import last_saturday as _last_saturday


class EnvelopeSabbathView(ReportAccessMixin, TemplateView):
    template_name = "reports/envelope_sabbath.html"

    def _date(self, request):
        raw = request.GET.get("date")
        try:
            return dt.date.fromisoformat(raw) if raw else _last_saturday()
        except ValueError:
            return _last_saturday()

    def get(self, request, *args, **kwargs):
        date = self._date(request)
        data = envelope_reports.sabbath_statement(date)
        if request.GET.get("export") == "csv":
            header = ["Receipt", "Contributor"] + [f.name for f in data["funds"]] + ["Total"]
            rows = []
            for r in data["rows"]:
                rows.append([r["envelope"].receipt_no, r["envelope"].contributor_name]
                            + [r["cells"].get(f.id, "") for f in data["funds"]]
                            + [r["total"]])
            rows.append(["", "TOTAL"] + [data["fund_totals"][f.id] for f in data["funds"]]
                        + [data["grand_total"]])
            return csv_response(f"envelopes_{date}.csv", header, rows)
        ctx = self.get_context_data(**kwargs)
        ctx["d"] = data
        ctx["date"] = date
        return self.render_to_response(ctx)

class EnvelopeSummaryView(ReportAccessMixin, TemplateView):
    template_name = "reports/envelope_summary.html"

    def get(self, request, *args, **kwargs):
        today = dt.date.today()
        try:
            year = int(request.GET.get("year", today.year))
            month = int(request.GET.get("month", today.month))
        except ValueError:
            year, month = today.year, today.month
        data = envelope_reports.monthly_summary(year, month)
        if request.GET.get("export") == "csv":
            header = ["Fund"] + [s.strftime("%d %b") for s in data["saturdays"]] + ["Total"]
            rows = [["— TRUST FUNDS —"]]
            for r in data["trust_rows"]:
                rows.append([r["fund"].name] + r["cols"] + [r["total"]])
            rows.append(["TOTAL TRUST FUNDS"] + data["trust_col_totals"] + [data["trust_total"]])
            rows.append(["— LOCAL FUNDS —"])
            for r in data["local_rows"]:
                rows.append([r["fund"].name] + r["cols"] + [r["total"]])
            rows.append(["TOTAL LOCAL FUNDS"] + data["local_col_totals"] + [data["local_total"]])
            return csv_response(f"offering_summary_{year}_{month:02d}.csv", header, rows)
        ctx = self.get_context_data(**kwargs)
        ctx["d"] = data
        ctx["month_label"] = dt.date(year, month, 1).strftime("%B %Y")
        ctx["year"], ctx["month"] = year, month
        return self.render_to_response(ctx)
