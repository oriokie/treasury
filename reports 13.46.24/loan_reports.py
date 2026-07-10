"""Loan liability & financing reports, built on the shared reporting service
and the existing report mixins/export helper. Access follows the same
ReportAccessMixin the other financial reports use; every report supports the
standard date/fund/lender/status/type filters and CSV/Excel/print export.
"""
import datetime as _dt
from decimal import Decimal

from django.views.generic import TemplateView

from core.models import SiteConfig
from core.permissions import ReportAccessMixin
from core.utils import parse_period
from departments.models import Department

from .exports import csv_response, xlsx_response


def _export(request, filename, header, rows, title):
    fmt = request.GET.get("export")
    if fmt == "csv":
        return csv_response(filename + ".csv", header, rows)
    if fmt == "xlsx":
        return xlsx_response(filename + ".xlsx", header, rows, title=title,
                             church=SiteConfig.get().church_name)
    return None


class LoanReportMixin(ReportAccessMixin):
    """Shared filter parsing for the loan report catalogue. Resolves the
    standard filters once and exposes both the filtered loan set and the
    filter option lists for the template's filter bar."""

    def loan_filters(self):
        g = self.request.GET
        start, end = parse_period(self.request)
        # loan reports filter on the loan_date only when the user explicitly
        # sets a range; otherwise "as at" reporting uses the full history
        use_period = bool(g.get("start") or g.get("end") or g.get("period"))
        return {
            "start": start if use_period else None,
            "end": end if use_period else None,
            "fund": g.get("fund") or None,
            "lender": g.get("lender") or None,
            "status": g.get("status") or None,
            "loan_type": g.get("loan_type") or None,
        }

    def as_of(self):
        raw = self.request.GET.get("as_of")
        if raw:
            try:
                return _dt.date.fromisoformat(raw)
            except ValueError:
                pass
        return _dt.date.today()

    def filter_context(self):
        from loans.models import Lender, Loan
        return {
            "funds": Department.objects.exclude(
                fund_type=Department.FundType.TRUST).filter(active=True).order_by("name"),
            "lenders": Lender.objects.filter(merged_into__isnull=True).order_by("name"),
            "statuses": Loan.Status.choices,
            "loan_types": Loan.LoanType.choices,
            "f": self.request.GET,
            "as_of_date": self.as_of(),
        }


class LoanReportIndexView(ReportAccessMixin, TemplateView):
    template_name = "reports/loans/index.html"


class LoanLiabilityScheduleView(LoanReportMixin, TemplateView):
    template_name = "reports/loans/liability_schedule.html"

    def get(self, request, *args, **kwargs):
        from loans.services import reporting
        loans = reporting.filtered_loans(**self.loan_filters())
        rows = reporting.liability_schedule(loans, as_of=self.as_of())
        ex = _export(
            request, f"loan_liability_{self.as_of()}",
            ["Loan no", "Lender", "Fund", "Loan date", "Maturity",
             "Original principal", "Outstanding principal", "Outstanding interest",
             "Total outstanding", "Rate %", "Status", "Days to maturity"],
            [[r["number"], r["lender"], r["fund"], r["loan_date"].isoformat(),
              r["maturity_date"].isoformat() if r["maturity_date"] else "",
              float(r["original_principal"]), float(r["outstanding_principal"]),
              float(r["outstanding_interest"]), float(r["total_outstanding"]),
              float(r["interest_rate"]), r["status"],
              r["days_to_maturity"] if r["days_to_maturity"] is not None else ""]
             for r in rows],
            "Loan Liability Schedule")
        if ex:
            return ex
        ctx = self.get_context_data(**kwargs)
        ctx.update(self.filter_context())
        ctx["rows"] = rows
        ctx["total_outstanding"] = sum((r["outstanding_principal"] for r in rows), Decimal(0))
        ctx["total_interest"] = sum((r["outstanding_interest"] for r in rows), Decimal(0))
        ctx["total"] = sum((r["total_outstanding"] for r in rows), Decimal(0))
        return self.render_to_response(ctx)


class OutstandingLoansView(LoanReportMixin, TemplateView):
    template_name = "reports/loans/outstanding.html"

    def get(self, request, *args, **kwargs):
        from loans.services import reporting
        loans = reporting.filtered_loans(**self.loan_filters())
        rows = [r for r in reporting.liability_schedule(loans, as_of=self.as_of())
                if r["outstanding_principal"] > 0]
        ex = _export(
            request, f"outstanding_loans_{self.as_of()}",
            ["Loan no", "Lender", "Fund", "Loan date", "Maturity",
             "Outstanding principal", "Status", "Overdue"],
            [[r["number"], r["lender"], r["fund"], r["loan_date"].isoformat(),
              r["maturity_date"].isoformat() if r["maturity_date"] else "",
              float(r["outstanding_principal"]), r["status"],
              "Yes" if r["overdue"] else ""] for r in rows],
            "Outstanding Loans")
        if ex:
            return ex
        ctx = self.get_context_data(**kwargs)
        ctx.update(self.filter_context())
        ctx["rows"] = rows
        ctx["total"] = sum((r["outstanding_principal"] for r in rows), Decimal(0))
        return self.render_to_response(ctx)


class LoanAgeingView(LoanReportMixin, TemplateView):
    template_name = "reports/loans/ageing.html"

    def get(self, request, *args, **kwargs):
        from loans.services import reporting
        loans = reporting.filtered_loans(**self.loan_filters())
        data = reporting.ageing(loans, as_of=self.as_of())
        ex = _export(
            request, f"loan_ageing_{self.as_of()}",
            ["Loan no", "Lender", "Fund", "Age (days)", "Bucket", "Outstanding"],
            [[r["loan"].number, r["loan"].lender.name, r["loan"].fund.name,
              r["age_days"], r["bucket"], float(r["outstanding"])]
             for r in data["rows"]],
            "Loan Ageing")
        if ex:
            return ex
        ctx = self.get_context_data(**kwargs)
        ctx.update(self.filter_context())
        ctx.update(data)
        return self.render_to_response(ctx)


class LoanMaturityView(LoanReportMixin, TemplateView):
    template_name = "reports/loans/maturity.html"

    def get(self, request, *args, **kwargs):
        from loans.services import reporting
        loans = reporting.filtered_loans(**self.loan_filters())
        rows = reporting.maturity_schedule(loans, as_of=self.as_of())
        ex = _export(
            request, f"loan_maturity_{self.as_of()}",
            ["Loan no", "Lender", "Fund", "Maturity", "Outstanding",
             "Days to maturity", "Classification"],
            [[r["loan"].number, r["loan"].lender.name, r["loan"].fund.name,
              r["maturity_date"].isoformat(), float(r["outstanding"]), r["days"],
              "Overdue" if r["overdue"] else ("Current" if r["current"] else "Long-term")]
             for r in rows],
            "Loan Maturity Schedule")
        if ex:
            return ex
        ctx = self.get_context_data(**kwargs)
        ctx.update(self.filter_context())
        ctx["rows"] = rows
        ctx["overdue_total"] = sum((r["outstanding"] for r in rows if r["overdue"]), Decimal(0))
        ctx["current_total"] = sum((r["outstanding"] for r in rows if r["current"]), Decimal(0))
        ctx["long_term_total"] = sum((r["outstanding"] for r in rows if r["long_term"]), Decimal(0))
        return self.render_to_response(ctx)


class LoansByFundView(LoanReportMixin, TemplateView):
    template_name = "reports/loans/by_fund.html"

    def get(self, request, *args, **kwargs):
        from loans.services import reporting
        loans = reporting.filtered_loans(**self.loan_filters())
        rows = reporting.by_fund(loans, as_of=self.as_of())
        ex = _export(
            request, "loans_by_fund",
            ["Fund", "Loans", "Received", "Repaid", "Outstanding"],
            [[r["fund"].name, r["count"], float(r["received"]),
              float(r["repaid"]), float(r["outstanding"])] for r in rows],
            "Loans by Fund")
        if ex:
            return ex
        ctx = self.get_context_data(**kwargs)
        ctx.update(self.filter_context())
        ctx["rows"] = rows
        ctx["total_outstanding"] = sum((r["outstanding"] for r in rows), Decimal(0))
        return self.render_to_response(ctx)


class LoansByLenderView(LoanReportMixin, TemplateView):
    template_name = "reports/loans/by_lender.html"

    def get(self, request, *args, **kwargs):
        from loans.services import reporting
        loans = reporting.filtered_loans(**self.loan_filters())
        rows = reporting.by_lender(loans, as_of=self.as_of())
        ex = _export(
            request, "loans_by_lender",
            ["Lender", "Member?", "Loans", "Received", "Repaid", "Outstanding"],
            [[r["lender"].name, "Yes" if r["is_member"] else "", r["count"],
              float(r["received"]), float(r["repaid"]), float(r["outstanding"])]
             for r in rows],
            "Loans by Lender")
        if ex:
            return ex
        ctx = self.get_context_data(**kwargs)
        ctx.update(self.filter_context())
        ctx["rows"] = rows
        ctx["total_outstanding"] = sum((r["outstanding"] for r in rows), Decimal(0))
        return self.render_to_response(ctx)


class LoanRepaymentHistoryView(LoanReportMixin, TemplateView):
    template_name = "reports/loans/repayment_history.html"

    def get(self, request, *args, **kwargs):
        from loans.services import reporting
        flt = self.loan_filters()
        loans = reporting.filtered_loans(fund=flt["fund"], lender=flt["lender"],
                                         status=flt["status"], loan_type=flt["loan_type"])
        rows = reporting.repayment_history(flt["start"], flt["end"], loans)
        ex = _export(
            request, "loan_repayment_history",
            ["Date", "Loan no", "Lender", "Fund", "Amount", "Voucher"],
            [[t.date.isoformat(), t.loan.number, t.loan.lender.name,
              t.loan.fund.name, float(t.amount),
              t.expense.voucher_no if t.expense_id else ""] for t in rows],
            "Loan Repayment History")
        if ex:
            return ex
        ctx = self.get_context_data(**kwargs)
        ctx.update(self.filter_context())
        ctx["rows"] = rows
        ctx["total"] = sum((t.amount for t in rows), Decimal(0))
        return self.render_to_response(ctx)


class LoanInterestReportView(LoanReportMixin, TemplateView):
    template_name = "reports/loans/interest.html"

    def get(self, request, *args, **kwargs):
        from loans.services import reporting
        flt = self.loan_filters()
        loans = reporting.filtered_loans(fund=flt["fund"], lender=flt["lender"],
                                         status=flt["status"], loan_type=flt["loan_type"])
        rows = reporting.interest_history(flt["start"], flt["end"], loans)
        ex = _export(
            request, "loan_interest",
            ["Date", "Loan no", "Lender", "Fund", "Interest paid", "Voucher"],
            [[t.date.isoformat(), t.loan.number, t.loan.lender.name,
              t.loan.fund.name, float(t.amount),
              t.expense.voucher_no if t.expense_id else ""] for t in rows],
            "Loan Interest Report")
        if ex:
            return ex
        ctx = self.get_context_data(**kwargs)
        ctx.update(self.filter_context())
        ctx["rows"] = rows
        ctx["total"] = sum((t.amount for t in rows), Decimal(0))
        return self.render_to_response(ctx)


class LoanConversionsView(LoanReportMixin, TemplateView):
    template_name = "reports/loans/conversions.html"

    def get(self, request, *args, **kwargs):
        from loans.services import reporting
        flt = self.loan_filters()
        loans = reporting.filtered_loans(fund=flt["fund"], lender=flt["lender"],
                                         loan_type=flt["loan_type"], include_draft=True)
        rows = reporting.conversions(flt["start"], flt["end"], loans)
        ex = _export(
            request, "loans_converted",
            ["Date", "Type", "Loan no", "Lender", "Fund", "Amount", "Member credited"],
            [[t.date.isoformat(), t.get_kind_display(), t.loan.number,
              t.loan.lender.name, t.loan.fund.name, float(t.amount),
              (t.income_transaction.member.name
               if t.income_transaction_id and t.income_transaction.member_id else "")]
             for t in rows],
            "Loans Converted to Donations")
        if ex:
            return ex
        ctx = self.get_context_data(**kwargs)
        ctx.update(self.filter_context())
        ctx["rows"] = rows
        ctx["converted_total"] = sum(
            (t.amount for t in rows if t.kind == "CONVERSION"), Decimal(0))
        ctx["writeoff_total"] = sum(
            (t.amount for t in rows if t.kind == "WRITE_OFF"), Decimal(0))
        return self.render_to_response(ctx)


class FinancingActivitiesView(LoanReportMixin, TemplateView):
    template_name = "reports/loans/financing.html"

    def get(self, request, *args, **kwargs):
        from loans.services import reporting
        start, end = parse_period(self.request)
        data = reporting.financing_activity(start, end)
        ex = _export(
            request, f"financing_activities_{start}_{end}",
            ["Financing activity", "Amount"],
            [["Loan receipts (cash in)", float(data["receipts"])],
             ["Principal repayments (cash out)", float(-data["repayments"])],
             ["Interest paid (cash out)", float(-data["interest"])],
             ["Net cash from financing", float(data["net_financing"])]],
            "Cash Flow from Financing Activities")
        if ex:
            return ex
        ctx = self.get_context_data(**kwargs)
        ctx.update(self.filter_context())
        ctx["start"], ctx["end"] = start, end
        ctx.update(data)
        return self.render_to_response(ctx)
