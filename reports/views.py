from decimal import Decimal

from django.contrib import messages
from django.db.models import Sum, Count, Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from core.permissions import ReadAccessMixin, TreasurerRequiredMixin
from core.utils import parse_period, safe_json
from cashbook.models import Expense
from departments.models import Department
from giving.models import Transaction
from members.models import Member
from .services import balances
from .exports import csv_response


class PeriodMixin(ReadAccessMixin):
    def period(self):
        return parse_period(self.request)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["start"], ctx["end"] = self.period()
        ctx["filters"] = self.request.GET
        return ctx


class ReportIndexView(ReadAccessMixin, TemplateView):
    template_name = "reports/index.html"


class MonthlyReportView(PeriodMixin, TemplateView):
    template_name = "reports/monthly.html"

    def get(self, request, *args, **kwargs):
        start, end = self.period()
        rows = balances.department_summary(start, end)
        if request.GET.get("export") == "csv":
            data = [(r["department"].name,
                     "Trust" if r["is_trust"] else "Local",
                     r["opening"], r["receipts"], r["expenses"], r["closing"])
                    for r in rows]
            return csv_response(
                f"monthly_{start}_{end}.csv",
                ["Fund", "Type", "Opening", "Receipts", "Expenses", "Closing"], data)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        rows = balances.department_summary(ctx["start"], ctx["end"])
        ctx["rows"] = rows
        ctx["totals"] = balances.totals(rows)
        ctx["trust_total"] = sum(r["receipts"] for r in rows if r["is_trust"])
        ctx["local_total"] = sum(r["receipts"] for r in rows if not r["is_trust"])
        return ctx


class OfferingSummaryView(PeriodMixin, TemplateView):
    template_name = "reports/offering_summary.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        data = balances.offering_summary(ctx["start"], ctx["end"])
        ctx["sabbaths"] = data["sabbaths"]
        ctx["rows"] = data["rows"]
        return ctx


class TitheReportView(PeriodMixin, TemplateView):
    template_name = "reports/tithe.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        s, e = ctx["start"], ctx["end"]
        ctx["tithe"] = balances.tithe_total(s, e)
        ctx["count"] = Transaction.objects.filter(
            date__gte=s, date__lte=e, direction=Transaction.Direction.CREDIT,
            department__name__icontains="tithe").count()
        return ctx


class GroupGivingView(PeriodMixin, TemplateView):
    template_name = "reports/by_group.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        data = balances.giving_by_group(ctx["start"], ctx["end"])
        labels = dict(Member.Group.choices)
        ctx["rows"] = [{"group": labels.get(k, k), "total": v}
                       for k, v in sorted(data.items(), key=lambda x: -x[1])]
        ctx["grand_total"] = sum(data.values())
        return ctx


class DevGroupUnassignedView(TreasurerRequiredMixin, TemplateView):
    """Development contributions sitting on the parent Development fund without a specific
    group — list them and reassign to the correct group for accurate per-group
    totals."""
    template_name = "reports/dev_unassigned.html"

    def _qs(self):
        from departments.models import Department
        return (Transaction.objects.active()
                .filter(direction=Transaction.Direction.CREDIT, confirmed=True,
                        department__category=Department.Category.DEVELOPMENT,
                        dev_group__isnull=True)
                .select_related("department", "member").order_by("-date"))

    def get_context_data(self, **kwargs):
        from departments.models import DevelopmentGroup
        ctx = super().get_context_data(**kwargs)
        qs = self._qs()
        ctx["items"] = qs[:500]
        ctx["count"] = qs.count()
        ctx["total"] = qs.aggregate(t=Sum("amount"))["t"] or Decimal(0)
        ctx["groups"] = DevelopmentGroup.objects.filter(active=True).order_by("number")
        return ctx

    def post(self, request):
        from departments.models import DevelopmentGroup
        gid = request.POST.get("dev_group")
        ids = request.POST.getlist("txn")
        if request.POST.get("all"):
            ids = list(self._qs().values_list("id", flat=True))
        grp = DevelopmentGroup.objects.filter(pk=gid).first()
        if not grp or not ids:
            messages.error(request, "Choose a group and at least one contribution to assign.")
            return redirect("dev_unassigned")
        n = (Transaction.objects.filter(id__in=ids, dev_group__isnull=True)
             .update(dev_group=grp))
        # keep the linked envelope lines in step (guarded: tolerate older schemas
        # where EnvelopeLine has no dev_group column)
        try:
            from envelopes.models import EnvelopeLine
            EnvelopeLine.objects.filter(transaction_id__in=ids,
                                        dev_group__isnull=True).update(dev_group=grp)
        except Exception:
            pass
        messages.success(request, f"Assigned {n} development contribution(s) to {grp.label}.")
        return redirect("dev_unassigned")


class DevGroupProgressView(PeriodMixin, TemplateView):
    template_name = "reports/dev_groups.html"

    def get(self, request, *args, **kwargs):
        export = request.GET.get("export")
        if export in ("csv", "xlsx"):
            from .exports import csv_response, xlsx_response
            from core.models import SiteConfig
            s, e = self.period()
            rows = balances.dev_group_progress(s, e)
            header = ["Group", "Collected", "Target", "Balance to target", "% complete"]
            out = [[(r["group"].label if hasattr(r["group"], "label") else r["group"].name),
                    float(r.get("collected") or 0), float(r.get("target") or 0),
                    float(r.get("balance") or 0), float(r.get("pct") or 0)] for r in rows]
            title = f"Development groups {s:%d %b %Y}–{e:%d %b %Y}"
            if export == "xlsx":
                return xlsx_response("dev_groups.xlsx", header, out, title=title,
                                     church=SiteConfig.get().church_name)
            return csv_response("dev_groups.csv", header, out)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["rows"] = balances.dev_group_progress(ctx["start"], ctx["end"])
        from departments.models import Department
        un = (Transaction.objects.active().filter(
                direction=Transaction.Direction.CREDIT, confirmed=True,
                department__category=Department.Category.DEVELOPMENT,
                dev_group__isnull=True))
        ctx["unassigned_count"] = un.count()
        ctx["unassigned_total"] = un.aggregate(t=Sum("amount"))["t"] or Decimal(0)
        return ctx


class ExpenseReportView(PeriodMixin, TemplateView):
    template_name = "reports/expenses.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        s, e = ctx["start"], ctx["end"]
        eff = Q(status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
        base = (Expense.objects.filter(eff, date__gte=s, date__lte=e)
                .exclude(category=Expense.Category.REMITTANCE))
        # consolidate by the top-level fund (sub-account spend rolls into its parent)
        from collections import defaultdict
        from departments.models import Department
        from .services.budget import budget_amount
        parent_of = {}
        budget_of = {}
        for d in Department.objects.select_related("parent"):
            top = d.parent or d
            parent_of[d.id] = top.name
            if top.name not in budget_of:
                budget_of[top.name] = budget_amount(e.year, top)
        agg = defaultdict(Decimal)
        for r in base.values("department_id").annotate(total=Sum("amount")):
            agg[parent_of.get(r["department_id"], "Unallocated")] += r["total"] or Decimal(0)
        ctx["by_dept"] = [{"name": k, "total": v, "budget": budget_of.get(k)}
                          for k, v in sorted(agg.items(), key=lambda x: -x[1])]
        ctx["by_category"] = (base.values("category")
                              .annotate(total=Sum("amount")).order_by("-total"))
        ctx["by_claimant"] = (base.exclude(claimant="").values("claimant")
                              .annotate(total=Sum("amount")).order_by("-total"))
        ctx["outstanding"] = Expense.objects.filter(
            status__in=[Expense.Status.PENDING, Expense.Status.APPROVED]).order_by("date")
        ctx["cat_labels"] = dict(Expense.Category.choices)
        return ctx


class IncomeExpenditureView(PeriodMixin, TemplateView):
    template_name = "reports/income_expenditure.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        s, e = ctx["start"], ctx["end"]
        # Income = the church's own (local) revenue only. Trust funds (tithe,
        # the remitted share of combined/thanksgiving offerings, etc.) are
        # collected on behalf of the field — a liability, not revenue — so they
        # are excluded here and shown separately as a memo.
        income = (Transaction.objects.confirmed_credits().filter(
            date__gte=s, date__lte=e, department__is_trust=False,
            excluded_from_income=False)
            .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        trust_collected = (Transaction.objects.confirmed_credits().filter(
            date__gte=s, date__lte=e, department__is_trust=True,
            excluded_from_income=False)
            .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        # Expenditure excludes remittances (they settle the trust liability) AND
        # capital purchases (an asset, not consumed in the period — only any
        # depreciation belongs in an income & expenditure account). Capital
        # additions are shown separately as a memo.
        paid = [Expense.Status.APPROVED, Expense.Status.PAID]
        expense = (Expense.objects.filter(
            date__gte=s, date__lte=e, status__in=paid)
            .exclude(category=Expense.Category.REMITTANCE)
            .exclude(expenditure_type=Expense.ExpenditureType.CAPITAL)
            .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        capital = (Expense.objects.filter(
            date__gte=s, date__lte=e, status__in=paid,
            expenditure_type=Expense.ExpenditureType.CAPITAL)
            .exclude(category=Expense.Category.REMITTANCE)
            .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        remittances = (Expense.objects.filter(
            date__gte=s, date__lte=e, status__in=paid,
            category=Expense.Category.REMITTANCE)
            .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        ctx["income"] = income
        ctx["expense"] = expense
        # Gain/(loss) on asset disposals in the period — the only part of a disposal
        # that belongs in the income result (the proceeds themselves are a capital
        # receipt, excluded from income above).
        from assets.models import FixedAsset
        disposal_gl = (FixedAsset.objects.filter(
            disposed=True, disposed_on__gte=s, disposed_on__lte=e)
            .aggregate(t=Sum("disposal_gain_loss"))["t"] or Decimal(0))
        ctx["disposal_gain_loss"] = disposal_gl
        ctx["net"] = income - expense + disposal_gl
        ctx["capital"] = capital
        ctx["trust_collected"] = trust_collected
        ctx["remittances"] = remittances
        return ctx


class FundLedgerView(PeriodMixin, TemplateView):
    template_name = "reports/fund_ledger.html"

    def get(self, request, *args, **kwargs):
        if request.GET.get("export") in ("xlsx", "csv", "subgroups", "subgroups-csv"):
            return self._export(request, *args, **kwargs)
        return super().get(request, *args, **kwargs)

    def _export(self, request, *args, **kwargs):
        from reports.exports import csv_response, xlsx_response
        from core.models import SiteConfig
        ctx = self.get_context_data(**kwargs)
        dept = ctx["department"]
        mode = request.GET.get("export")

        # subgroup breakdown export (sub-accounts beneath this fund)
        if mode in ("subgroups", "subgroups-csv"):
            header = ["ID", "Subgroup", "Type", "Receipts", "Payments", "Closing balance"]
            rows = []
            for r in ctx["subgroups"]:
                sub = r["sub"]
                rows.append([sub.id, sub.name,
                             "Trust" if sub.is_trust else "Local",
                             float(r["receipts"]), float(r["payments"]), float(r["closing"])])
            for r in ctx.get("dev_rows", []):
                g = r["group"]
                rows.append([getattr(g, "id", ""), g.name, "Local",
                             float(r["receipts"]), "", float(r["receipts"])])
            rows.append(["", "TOTAL", "", "", "", float(ctx["subgroup_total"])])
            fname = f"fund-{dept.slug or dept.id}-subgroups-{ctx['start']}-{ctx['end']}"
            if mode == "subgroups-csv":
                return csv_response(fname + ".csv", header, rows)
            return xlsx_response(fname + ".xlsx", header, rows,
                                 title=f"{dept.name} — sub-accounts ({ctx['start']} to {ctx['end']})",
                                 church=SiteConfig.get().church_name)

        header = ["ID", "Type", "Date", "Description", "Debit", "Credit", "Balance"]
        rows = [["", "", "", "Opening balance", "", "", float(ctx["opening"])]]
        for en in ctx["entries"]:
            rows.append([en.get("ref_id", ""), en.get("src", ""),
                         en["date"].isoformat(), en["desc"],
                         float(en["debit"]) if en["debit"] else "",
                         float(en["credit"]) if en["credit"] else "",
                         float(en["balance"])])
        rows.append(["", "", "", "Closing balance", "", "", float(ctx["closing"])])
        fname = f"fund-{dept.slug or dept.id}-{ctx['start']}-{ctx['end']}"
        if request.GET["export"] == "csv":
            return csv_response(fname + ".csv", header, rows)
        return xlsx_response(fname + ".xlsx", header, rows,
                             title=f"{dept.name} ledger ({ctx['start']} to {ctx['end']})",
                             church=SiteConfig.get().church_name)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        dept = get_object_or_404(Department, pk=kwargs["pk"])
        s, e = ctx["start"], ctx["end"]
        receipts = (Transaction.objects.confirmed_credits().filter(
            department=dept, date__gte=s, date__lte=e,
            excluded_from_income=False).order_by("date"))
        payments = Expense.objects.filter(
            department=dept, date__gte=s, date__lte=e,
            status__in=[Expense.Status.APPROVED, Expense.Status.PAID]).order_by("date")
        entries = []
        for t in receipts:
            entries.append({"date": t.date, "desc": t.payer_name or t.reference or "Receipt",
                            "credit": t.amount, "debit": None, "src": "Receipt", "ref_id": t.id})
        for x in payments:
            entries.append({"date": x.date, "desc": x.description,
                            "credit": None, "debit": x.amount, "src": "Expense", "ref_id": x.id})
        from cashbook.models import FundTransfer
        for tr in FundTransfer.objects.filter(destination=dept, date__gte=s, date__lte=e):
            entries.append({"date": tr.date, "desc": f"Transfer from {tr.source.name}"
                            + (f" — {tr.reason}" if tr.reason else ""),
                            "credit": tr.amount, "debit": None, "src": "Transfer", "ref_id": tr.id})
        for tr in FundTransfer.objects.filter(source=dept, date__gte=s, date__lte=e):
            entries.append({"date": tr.date, "desc": f"Transfer to {tr.destination.name}"
                            + (f" — {tr.reason}" if tr.reason else ""),
                            "credit": None, "debit": tr.amount, "src": "Transfer", "ref_id": tr.id})
        entries.sort(key=lambda r: r["date"])
        running = dept.opening_balance or Decimal(0)
        for en in entries:
            running += (en["credit"] or 0) - (en["debit"] or 0)
            en["balance"] = running
        ctx["department"] = dept
        ctx["entries"] = entries
        ctx["opening"] = dept.opening_balance or Decimal(0)
        ctx["closing"] = running

        # roll up any sub-accounts beneath this fund (two grouped queries, not 2/sub)
        subs = list(dept.subgroups.all())
        sub_ids = [x.id for x in subs]
        sub_rec = {r["department"]: (r["t"] or Decimal(0)) for r in
                   Transaction.objects.filter(
                       department_id__in=sub_ids,
                       direction=Transaction.Direction.CREDIT,
                       date__gte=s, date__lte=e)
                   .values("department").annotate(t=Sum("amount"))}
        sub_pay = {r["department"]: (r["t"] or Decimal(0)) for r in
                   Expense.objects.filter(
                       department_id__in=sub_ids, date__gte=s, date__lte=e,
                       status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
                   .values("department").annotate(t=Sum("amount"))}
        subs_rows = []
        sub_total = Decimal(0)
        for sub in subs:
            r = sub_rec.get(sub.id, Decimal(0))
            p = sub_pay.get(sub.id, Decimal(0))
            closing = (sub.opening_balance or Decimal(0)) + r - p
            subs_rows.append({"sub": sub, "receipts": r, "payments": p, "closing": closing})
            sub_total += closing
        subs = subs_rows

        # development groups are sub-accounts of the Development fund (one query)
        from departments.models import DevelopmentGroup
        dev_rows = []
        if dept.name.lower() == "development":
            dev_map = {r["dev_group"]: (r["t"] or Decimal(0)) for r in
                       Transaction.objects.filter(
                           dev_group__isnull=False,
                           direction=Transaction.Direction.CREDIT,
                           date__gte=s, date__lte=e)
                       .values("dev_group").annotate(t=Sum("amount"))}
            for grp in DevelopmentGroup.objects.filter(active=True):
                r = dev_map.get(grp.id, Decimal(0))
                dev_rows.append({"group": grp, "receipts": r})
                sub_total += r
        ctx["dev_rows"] = dev_rows
        ctx["subgroups"] = subs
        ctx["subgroup_total"] = sub_total
        ctx["combined_closing"] = running + sub_total
        ctx["parent"] = dept.parent
        return ctx


class TrustFundView(PeriodMixin, TemplateView):
    template_name = "reports/trust.html"

    def get_context_data(self, **kwargs):
        from cashbook.models import RemittanceBatch
        ctx = super().get_context_data(**kwargs)
        ctx["rows"] = balances.trust_summary(ctx["start"], ctx["end"])
        ctx["total_to_remit"] = sum(r["to_remit"] for r in ctx["rows"])
        ctx["total_unreceipted"] = sum((r["unreceipted"] for r in ctx["rows"]), Decimal(0))
        ctx["total_liability"] = sum((r["total_liability"] for r in ctx["rows"]), Decimal(0))
        ctx["batches"] = (RemittanceBatch.objects
                          .order_by("-date", "-id")[:25])
        ctx["remitted_total"] = sum(r["remitted"] for r in ctx["rows"])
        return ctx


class RemittanceView(PeriodMixin, TemplateView):
    template_name = "reports/remittance.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["rows"] = balances.trust_summary(ctx["start"], ctx["end"])
        ctx["total"] = sum(r["to_remit"] for r in ctx["rows"])
        ctx["total_unreceipted"] = sum((r["unreceipted"] for r in ctx["rows"]), Decimal(0))
        return ctx


class RemitTrustView(TreasurerRequiredMixin, View):
    """Raise a remittance expense against each trust fund for the amount still to
    remit in the period — the monthly lump sum sent to the field."""

    def post(self, request):
        import datetime as _dt
        from cashbook.models import Expense
        from core.models import SiteConfig
        from core.utils import sabbath_week_of
        try:
            s = _dt.date.fromisoformat(request.POST["start"])
            e = _dt.date.fromisoformat(request.POST["end"])
        except (KeyError, ValueError):
            messages.error(request, "Pick a valid period to remit.")
            return redirect("report_remittance")
        field = SiteConfig.get().field_name or "the field"
        cheque = (request.POST.get("cheque_no") or "").strip()
        try:
            paid = _dt.date.fromisoformat(request.POST.get("cheque_date")) if request.POST.get("cheque_date") else e
        except ValueError:
            paid = e
        rows = balances.trust_summary(s, e)
        n = 0
        total = Decimal(0)
        for r in rows:
            amt = r["to_remit"]
            if amt and amt > 0:
                Expense.objects.create(
                    date=paid, sabbath_week=sabbath_week_of(paid), department=r["department"],
                    description=f"Remittance to {field} ({s:%d %b}–{e:%d %b %Y})"
                                + (f", cheque {cheque}" if cheque else ""),
                    amount=amt, category=Expense.Category.REMITTANCE,
                    claimant=field, method=Expense.Method.CHEQUE,
                    voucher_no=cheque[:30], status=Expense.Status.PAID, paid_date=paid,
                    recorded_by=request.user, approved_by=request.user)
                n += 1
                total += amt
        if n:
            messages.success(
                request, f"Remitted {n} trust fund(s) totalling KES {total:,.2f} to {field}"
                         + (f" by cheque {cheque}." if cheque else "."))
        else:
            messages.info(request, "Nothing outstanding to remit for this period.")
        return redirect(f"{reverse('report_remittance')}?start={s}&end={e}")


class MemberStatementView(PeriodMixin, TemplateView):
    template_name = "reports/member_statement.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        member = get_object_or_404(Member, pk=kwargs["pk"])
        s, e = ctx["start"], ctx["end"]
        txns = (Transaction.objects.confirmed_credits().filter(
            member=member, date__gte=s, date__lte=e,
            excluded_from_income=False).values("department__name")
            .annotate(total=Sum("amount")).order_by("-total"))
        ctx["member"] = member
        ctx["rows"] = txns
        ctx["total"] = sum(r["total"] for r in txns)
        return ctx


class CashBookView(PeriodMixin, TemplateView):
    template_name = "reports/cashbook.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        s, e = ctx["start"], ctx["end"]
        entries = []
        for t in Transaction.objects.filter(date__gte=s, date__lte=e,
                                             direction=Transaction.Direction.CREDIT):
            entries.append({"date": t.date, "desc": t.payer_name or t.reference or "Receipt",
                            "credit": t.amount, "debit": None})
        for x in Expense.objects.filter(date__gte=s, date__lte=e,
                                        status__in=[Expense.Status.APPROVED, Expense.Status.PAID]):
            entries.append({"date": x.date, "desc": x.description,
                            "credit": None, "debit": x.amount})
        entries.sort(key=lambda r: r["date"])
        running = Decimal(0)
        for en in entries:
            running += (en["credit"] or 0) - (en["debit"] or 0)
            en["balance"] = running
        ctx["entries"] = entries
        ctx["closing"] = running
        return ctx


class ReconciliationView(PeriodMixin, TemplateView):
    template_name = "reports/reconciliation.html"

    def get_context_data(self, **kwargs):
        from statements.models import BankReconciliation
        ctx = super().get_context_data(**kwargs)
        s, e = ctx["start"], ctx["end"]
        bank = Transaction.objects.active().filter(channel=Transaction.Channel.BANK,
                                          date__gte=s, date__lte=e)
        ctx["ledger_credits"] = bank.filter(direction=Transaction.Direction.CREDIT).aggregate(
            t=Sum("amount"))["t"] or Decimal(0)
        ctx["ledger_debits"] = bank.filter(direction=Transaction.Direction.DEBIT).aggregate(
            t=Sum("amount"))["t"] or Decimal(0)
        ctx["unreconciled"] = bank.filter(
            allocation_status=Transaction.Status.REVIEW).order_by("date")

        # Proper bank reconciliation: the bank STATEMENT balance reconciles to the
        # BOOK (cashbook) balance through timing/at-hand adjustments — NOT to the
        # raw bank-credit total, which is why a naive "credits vs bank balance"
        # comparison looks wildly off. Show the latest statement reconciliation.
        rec = (BankReconciliation.objects.order_by("-statement_date")
               .prefetch_related("items").first())
        ctx["rec"] = rec
        if rec:
            adds = [i for i in rec.items.all() if i.effect == "ADD"]
            subs = [i for i in rec.items.all() if i.effect == "SUBTRACT"]
            ctx["rec_adds"] = adds
            ctx["rec_subs"] = subs
            ctx["rec_add_total"] = sum((i.amount for i in adds), Decimal(0))
            ctx["rec_sub_total"] = sum((i.amount for i in subs), Decimal(0))
            ctx["rec_computed_book"] = (rec.bank_balance + ctx["rec_add_total"]
                                        - ctx["rec_sub_total"])
            ctx["rec_variance"] = ctx["rec_computed_book"] - rec.book_balance
        return ctx


class AnnualSummaryView(ReadAccessMixin, TemplateView):
    template_name = "reports/annual.html"

    def get_context_data(self, **kwargs):
        from django.db.models.functions import ExtractYear
        ctx = super().get_context_data(**kwargs)
        income = (Transaction.objects.confirmed_credits()
                  .filter(excluded_from_income=False)
                  .annotate(yr=ExtractYear("date"))
                  .values("yr").annotate(total=Sum("amount")).order_by("yr"))
        expense = (Expense.objects.filter(
            status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
            .exclude(category=Expense.Category.REMITTANCE)
            .annotate(yr=ExtractYear("date"))
            .values("yr").annotate(total=Sum("amount")).order_by("yr"))
        inc = {r["yr"]: r["total"] for r in income}
        exp = {r["yr"]: r["total"] for r in expense}
        years = sorted(set(inc) | set(exp))
        ctx["rows"] = [{"year": y, "income": inc.get(y, 0), "expense": exp.get(y, 0),
                        "net": (inc.get(y, 0) or 0) - (exp.get(y, 0) or 0)} for y in years]
        # historical reference years (collection / trust fund / expenditure)
        from core.models import HistoricalYear, HistoricalMonth
        import json
        ctx["historical"] = list(HistoricalYear.objects.all())
        # seasonality: average collection / trust / expenditure by calendar month
        MN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
              "Oct", "Nov", "Dec"]
        agg = {m: {"c": 0.0, "t": 0.0, "e": 0.0, "n": 0} for m in range(1, 13)}
        for hm in HistoricalMonth.objects.all():
            a = agg[hm.month]
            a["c"] += float(hm.collection); a["t"] += float(hm.trust_fund)
            a["e"] += float(hm.expenditure); a["n"] += 1
        season = {"labels": MN,
                  "collection": [round(agg[m]["c"] / agg[m]["n"], 2) if agg[m]["n"] else 0 for m in range(1, 13)],
                  "trust": [round(agg[m]["t"] / agg[m]["n"], 2) if agg[m]["n"] else 0 for m in range(1, 13)],
                  "expenditure": [round(agg[m]["e"] / agg[m]["n"], 2) if agg[m]["n"] else 0 for m in range(1, 13)]}
        ctx["season_json"] = safe_json(season)
        ctx["has_season"] = HistoricalMonth.objects.exists()
        return ctx


class HistoricalYearManageView(TreasurerRequiredMixin, TemplateView):
    """Add, edit, or remove prior-year comparison figures (e.g. 2025). Closed
    years populate automatically; this is for years entered by hand."""
    template_name = "reports/historical_manage.html"

    def get_context_data(self, **kwargs):
        from core.models import HistoricalYear
        ctx = super().get_context_data(**kwargs)
        ctx["years"] = HistoricalYear.objects.all()
        return ctx

    def post(self, request, *args, **kwargs):
        from decimal import Decimal, InvalidOperation
        from core.models import HistoricalYear
        action = request.POST.get("action")
        if action == "delete":
            HistoricalYear.objects.filter(pk=request.POST.get("pk")).delete()
            messages.success(request, "Historical year removed.")
            return redirect("historical_manage")
        try:
            year = int(request.POST.get("year"))
            def dec(k):
                return Decimal(str(request.POST.get(k) or "0").replace(",", ""))
            HistoricalYear.objects.update_or_create(
                year=year, defaults=dict(collection=dec("collection"),
                    trust_fund=dec("trust_fund"), expenditure=dec("expenditure"),
                    note=(request.POST.get("note") or "Entered manually")[:200]))
            messages.success(request, f"Saved historical figures for {year}.")
        except (TypeError, ValueError, InvalidOperation):
            messages.error(request, "Enter a valid year and numeric amounts.")
        return redirect("historical_manage")


class AuditLogView(ReadAccessMixin, TemplateView):
    template_name = "reports/audit.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        from giving.models import Transaction as T
        from cashbook.models import Expense as X
        from members.models import Member as M
        records = []
        for model in (T, X, M):
            for h in model.history.all().select_related("history_user")[:40]:
                records.append({
                    "model": model.__name__, "when": h.history_date,
                    "user": getattr(h.history_user, "username", "system"),
                    "type": h.get_history_type_display(),
                    "obj": str(h.instance) if hasattr(h, "instance") else str(h),
                })
        records.sort(key=lambda r: r["when"], reverse=True)
        ctx["records"] = records[:100]
        return ctx


# ---- Envelope reports ----
import datetime as dt
from .services import envelope_reports


from core.utils import last_saturday as _last_saturday


class EnvelopeSabbathView(ReadAccessMixin, TemplateView):
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


class EnvelopeSummaryView(ReadAccessMixin, TemplateView):
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


# ---- Monthly account reports ----
from .services import monthly


def _year_from(request):
    try:
        return int(request.GET.get("year", dt.date.today().year))
    except (ValueError, TypeError):
        return dt.date.today().year


class MonthlyAccountsView(ReadAccessMixin, TemplateView):
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


class TrustMonthlyView(ReadAccessMixin, TemplateView):
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


class CollectionsSummaryView(ReadAccessMixin, TemplateView):
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


# ===================== Trust Fund Remittance subsystem =====================
import datetime as _dt
from django.utils import timezone as _tz
from django.shortcuts import render as _render
from cashbook.models import RemittanceBatch
from core.models import SiteConfig
from core.utils import sabbath_week_of


def _days_outstanding(dept):
    """Days since the oldest *unremitted* trust receipt for a fund. Receipts up to
    the last remittance's period are already settled, so we count only from the
    first receipt after it — not the first contribution ever (which made everything look
    months overdue even right after remitting)."""
    from cashbook.models import RemittanceBatch
    last = (RemittanceBatch.objects.filter(
        status=RemittanceBatch.Status.REMITTED, period_end__isnull=False)
        .order_by("-period_end").first())
    q = Transaction.objects.filter(
        department=dept, direction=Transaction.Direction.CREDIT,
        confirmed=True, is_reversed=False, is_reversal=False)
    if last:
        q = q.filter(date__gt=last.period_end)
    first = q.order_by("date").first()
    if not first:
        return 0
    return (_dt.date.today() - first.date).days


def _repost_to_ledger(expenses=None):
    """After a bulk .update() (which bypasses post_save signals), rebuild the
    general ledger so it always reflects batch approve/remit and stays reconciled."""
    try:
        from ledger.services import posting
        if posting.chart_ready():
            posting.rebuild()
    except Exception:
        pass


def remittance_dashboard_rows(start=None, end=None):
    rows = []
    for r in balances.trust_summary(start, end):   # period (or lifetime) collected vs remitted
        out = r["to_remit"]
        rows.append({
            "department": r["department"], "collected": r["collected"],
            "remitted": r["remitted"], "outstanding": out,
            "unreceipted": r["unreceipted"],
            "days": _days_outstanding(r["department"]) if out > 0 else 0,
        })
    return rows


def _remit_period(request):
    """Resolve a remittance period from the request: ?month=YYYY-MM, or
    ?start=&end=, else None (lifetime)."""
    import datetime as _dt, calendar as _cal
    month = request.GET.get("month") or request.POST.get("month")
    if month:
        try:
            y, m = (int(x) for x in month.split("-"))
            last = _cal.monthrange(y, m)[1]
            return _dt.date(y, m, 1), _dt.date(y, m, last), month
        except (ValueError, TypeError):
            pass
    s = request.GET.get("start") or request.POST.get("start")
    e = request.GET.get("end") or request.POST.get("end")
    def pd(v):
        try:
            return _dt.datetime.strptime(v, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None
    return pd(s), pd(e), None


class RemittanceDashboardView(ReadAccessMixin, TemplateView):
    template_name = "reports/remittance_dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        start, end, month = _remit_period(self.request)
        rows = remittance_dashboard_rows(start, end)
        ctx["rows"] = rows
        ctx["period_start"], ctx["period_end"], ctx["period_month"] = start, end, month
        ctx["period_active"] = bool(start or end)
        ctx["total_outstanding"] = sum((r["outstanding"] for r in rows), Decimal(0))
        ctx["total_collected"] = sum((r["collected"] for r in rows), Decimal(0))
        ctx["total_remitted"] = sum((r["remitted"] for r in rows), Decimal(0))
        ctx["total_unreceipted"] = sum((r["unreceipted"] for r in rows), Decimal(0))
        ctx["max_days"] = max([r["days"] for r in rows], default=0)
        # Next remittance: use the configured per-month deadlines if any exist
        # (their dates are set freely per month, not on a fixed day). We count
        # down to the *reporting Sabbath* — the Saturday whose count must be in
        # the remittance. Fall back to the configured due-day only if no
        # deadlines have been entered yet.
        import datetime as _dt, calendar as _cal
        from cashbook.models import RemittanceDeadline
        today = _dt.date.today()
        nxt = (RemittanceDeadline.objects.filter(remitted=False, deadline__gte=today)
               .order_by("deadline").first())
        overdue_dl = (RemittanceDeadline.objects.filter(remitted=False, deadline__lt=today)
                      .order_by("-deadline").first())
        active = nxt or overdue_dl
        if active:
            ctx["next_deadline"] = active
            ctx["due_date"] = active.deadline
            ctx["reporting_sabbath"] = active.reporting_sabbath
            ctx["days_to_deadline"] = (active.deadline - today).days
            ctx["days_to_sabbath"] = (active.reporting_sabbath - today).days
            ctx["deadline_period"] = active.get_period_display()
            ctx["has_deadlines"] = True
        else:
            # legacy fallback: configured day of the following month
            cfg = SiteConfig.get()
            ny, nm = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
            due_day = min(cfg.trust_remit_due_day or 15, _cal.monthrange(ny, nm)[1])
            ctx["due_date"] = _dt.date(ny, nm, due_day)
            ctx["days_to_deadline"] = (ctx["due_date"] - today).days
            ctx["has_deadlines"] = False
        ctx["due_overdue"] = ctx["total_outstanding"] > 0 and ctx.get("days_to_deadline", 99) < 0
        ctx["batches"] = RemittanceBatch.objects.all()[:10]
        ctx["field_name"] = SiteConfig.get().field_name or "the field"
        return ctx


class RemittanceBatchCreateView(TreasurerRequiredMixin, View):
    """Generate a DRAFT batch. POST 'all'=1 to include every outstanding trust
    fund, or one or more 'fund' ids for a per-fund wizard."""
    def post(self, request):
        start, end, month = _remit_period(request)
        rows = {r["department"].id: r for r in remittance_dashboard_rows(start, end)}
        if request.POST.get("all"):
            chosen = [r for r in rows.values() if r["outstanding"] > 0]
        else:
            ids = [int(i) for i in request.POST.getlist("fund") if i.isdigit()]
            chosen = [rows[i] for i in ids if i in rows and rows[i]["outstanding"] > 0]
        if not chosen:
            messages.error(request, "No outstanding trust funds to remit for that period.")
            return redirect("remittance_dashboard")
        field = SiteConfig.get().field_name or "the field"
        batch = RemittanceBatch.create_batch(
            created_by=request.user, status=RemittanceBatch.Status.DRAFT,
            period_start=start, period_end=end)
        today = _dt.date.today()
        # date the remittance expense within the period it covers, so it ties to
        # the right month's trust collection
        rdate = end or today
        plabel = (f" for {month}" if month else
                  f" for {start:%d %b %Y}–{end:%d %b %Y}" if (start and end) else "")
        for r in chosen:
            Expense.objects.create(
                date=rdate, sabbath_week=sabbath_week_of(rdate), department=r["department"],
                description=f"Trust remittance to {field} — {batch.batch_number}{plabel}",
                amount=r["outstanding"], category=Expense.Category.REMITTANCE,
                claimant=field, method=Expense.Method.CHEQUE,
                status=Expense.Status.PENDING, recorded_by=request.user,
                remittance_batch=batch)
        batch.recompute_total()
        batch.save(update_fields=["total_amount"])
        messages.success(request, f"Created remittance batch {batch.batch_number} "
                                  f"covering {len(chosen)} fund(s){plabel}.")
        return redirect("remittance_batch_detail", pk=batch.pk)


class RemittanceBatchDetailView(ReadAccessMixin, TemplateView):
    template_name = "reports/remittance_batch.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        batch = get_object_or_404(RemittanceBatch, pk=kwargs["pk"])
        ctx["batch"] = batch
        ctx["lines"] = batch.expenses.select_related("department").all()
        ctx["field_name"] = SiteConfig.get().field_name or "the field"
        return ctx


class RemittanceBatchApproveView(TreasurerRequiredMixin, View):
    def post(self, request, pk):
        batch = get_object_or_404(RemittanceBatch, pk=pk)
        if batch.status != RemittanceBatch.Status.DRAFT:
            messages.error(request, "Only a draft batch can be approved.")
            return redirect("remittance_batch_detail", pk=pk)
        batch.status = RemittanceBatch.Status.APPROVED
        batch.approved_by = request.user
        batch.save(update_fields=["status", "approved_by"])
        batch.expenses.update(status=Expense.Status.APPROVED, approved_by=request.user)
        _repost_to_ledger(batch.expenses.all())
        messages.success(request, f"Batch {batch.batch_number} approved.")
        return redirect("remittance_batch_detail", pk=pk)


class RemittanceBatchRemitView(TreasurerRequiredMixin, View):
    def post(self, request, pk):
        batch = get_object_or_404(RemittanceBatch, pk=pk)
        if batch.status != RemittanceBatch.Status.APPROVED:
            messages.error(request, "Approve the batch before marking it remitted.")
            return redirect("remittance_batch_detail", pk=pk)
        cheque = (request.POST.get("cheque_no") or "").strip()
        try:
            cdate = _dt.date.fromisoformat(request.POST.get("cheque_date")) if request.POST.get("cheque_date") else _dt.date.today()
        except ValueError:
            cdate = _dt.date.today()
        batch.status = RemittanceBatch.Status.REMITTED
        batch.cheque_no = cheque[:30]
        batch.cheque_date = cdate
        batch.remitted_at = _tz.now()
        batch.save(update_fields=["status", "cheque_no", "cheque_date", "remitted_at"])
        batch.expenses.update(status=Expense.Status.PAID, paid_date=cdate,
                              voucher_no=cheque[:30])
        _repost_to_ledger(batch.expenses.all())
        messages.success(request, f"Batch {batch.batch_number} marked remitted"
                                  + (f" by cheque {cheque}." if cheque else "."))
        return redirect("remittance_batch_detail", pk=pk)


class RemittanceBatchListView(ReadAccessMixin, TemplateView):
    template_name = "reports/remittance_batches.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["batches"] = RemittanceBatch.objects.all()
        return ctx


class RemittanceCalendarView(ReadAccessMixin, TemplateView):
    """Per-year remittance calendar: each period's deadline and the reporting
    Sabbath it maps to (the most recent Saturday on/before the deadline). When a
    deadline doesn't fall on a Sabbath, the previous Sabbath is shown as the
    reporting Sabbath, and due-soon / overdue items are highlighted."""
    template_name = "reports/remittance_calendar.html"

    def get_context_data(self, **kwargs):
        from cashbook.models import RemittanceDeadline, RemittanceBatch
        import calendar as _cal
        ctx = super().get_context_data(**kwargs)
        today = _dt.date.today()
        try:
            year = int(self.request.GET.get("year", today.year))
        except (TypeError, ValueError):
            year = today.year
        deadlines = list(RemittanceDeadline.objects.filter(year=year))

        # auto-mark a period as remitted when a remitted batch covers it. A batch
        # covers a month if its period overlaps that month, or (if it has no
        # period set) if it was remitted within the month. This keeps the
        # calendar honest without the treasurer ticking each box by hand.
        remitted_batches = list(RemittanceBatch.objects.filter(
            status=RemittanceBatch.Status.REMITTED))
        auto_marked = 0
        for d in deadlines:
            if d.remitted:
                continue
            m_start = _dt.date(year, d.period_month, 1)
            m_end = _dt.date(year, d.period_month,
                             _cal.monthrange(year, d.period_month)[1])
            covered = False
            for b in remitted_batches:
                ps, pe = b.period_start, b.period_end
                if ps and pe:
                    if ps <= m_end and pe >= m_start:
                        covered = True; break
                elif b.remitted_at and m_start <= b.remitted_at.date() <= m_end:
                    covered = True; break
            if covered:
                d.remitted = True
                d.save(update_fields=["remitted"])
                auto_marked += 1
        if auto_marked:
            messages.info(self.request,
                f"{auto_marked} period(s) marked remitted automatically from "
                f"completed remittance batches.")

        ctx["year"] = year
        ctx["prev_year"] = year - 1
        ctx["next_year"] = year + 1
        ctx["deadlines"] = deadlines
        ctx["has_any"] = bool(deadlines)
        ctx["due_soon"] = [d for d in deadlines if d.is_due_soon]
        ctx["overdue"] = [d for d in deadlines if d.is_overdue]
        return ctx


class RemittanceCalendarGenerateView(TreasurerRequiredMixin, View):
    """Auto-generate a year's monthly remittance deadlines. The default deadline
    is the 1st of the following month (adjustable afterwards). Existing periods
    are left untouched."""

    def post(self, request):
        from cashbook.models import RemittanceDeadline
        import calendar
        today = _dt.date.today()
        try:
            year = int(request.POST.get("year", today.year))
        except (TypeError, ValueError):
            year = today.year
        try:
            due_day = int(request.POST.get("due_day", 1))
        except (TypeError, ValueError):
            due_day = 1
        due_day = min(max(due_day, 1), 28)
        created = 0
        for m in range(1, 13):
            # deadline falls in the FOLLOWING month by default
            dyear, dmonth = (year + 1, 1) if m == 12 else (year, m + 1)
            _, last_day = calendar.monthrange(dyear, dmonth)
            deadline = _dt.date(dyear, dmonth, min(due_day, last_day))
            _, was_created = RemittanceDeadline.objects.get_or_create(
                year=year, period_month=m,
                defaults={"deadline": deadline,
                          "label": f"{calendar.month_name[m]} remittance"})
            if was_created:
                created += 1
        messages.success(request, f"Generated {created} remittance deadline(s) for "
                                  f"{year}. You can adjust individual dates below.")
        return redirect(f"{reverse('remittance_calendar')}?year={year}")


class RemittanceDeadlineUpdateView(TreasurerRequiredMixin, View):
    """Edit one deadline's date/label/notes, or toggle its remitted flag."""

    def post(self, request, pk):
        from cashbook.models import RemittanceDeadline
        d = get_object_or_404(RemittanceDeadline, pk=pk)
        if request.POST.get("toggle_remitted"):
            d.remitted = not d.remitted
            d.save(update_fields=["remitted"])
            messages.success(request, f"{d.get_period_display()} marked "
                                      f"{'remitted' if d.remitted else 'not remitted'}.")
            return redirect(f"{reverse('remittance_calendar')}?year={d.year}")
        new_date = request.POST.get("deadline")
        if new_date:
            try:
                d.deadline = _dt.date.fromisoformat(new_date)
            except ValueError:
                messages.error(request, "Invalid date.")
                return redirect(f"{reverse('remittance_calendar')}?year={d.year}")
        d.label = (request.POST.get("label") or d.label)[:60]
        d.notes = (request.POST.get("notes") or "")[:200]
        d.save()
        messages.success(request, f"Updated {d.get_period_display()} deadline.")
        return redirect(f"{reverse('remittance_calendar')}?year={d.year}")


# ===================== Budget vs Actual report =====================
from .services import budget as budget_svc


class BudgetVsActualView(ReadAccessMixin, TemplateView):
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


# ===================== Reporting suite (new reports) =====================
from .exports import xlsx_response
from core.utils import sabbath_of
from core.models import SiteConfig


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


class DailySummaryView(ReadAccessMixin, TemplateView):
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


class WeeklySummaryView(ReadAccessMixin, TemplateView):
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


class CashFlowView(ReadAccessMixin, TemplateView):
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


class BoardReportView(PeriodMixin, TemplateView):
    """One-page board summary: position, fund groups, trust, budget, KPIs, plus an
    AI-written narrative (insights, trends, recommendations) with a rule-based
    fallback when the assistant is off or unavailable."""
    template_name = "reports/board_report.html"

    def get_context_data(self, **kwargs):
        from core.services.assistant import board_report_narrative
        from cashbook.views import open_payables_total, open_accruals_total
        from django.db.models.functions import ExtractYear
        ctx = super().get_context_data(**kwargs)
        s, e = ctx["start"], ctx["end"]
        cfg = SiteConfig.get()
        paid = [Expense.Status.APPROVED, Expense.Status.PAID]
        rows = balances.department_summary(s, e)
        ctx["totals"] = balances.totals(rows)
        ctx["trust_rows"] = [r for r in rows if r["is_trust"]]
        ctx["local_rows"] = [r for r in rows if not r["is_trust"]]
        ctx["trust_summary"] = balances.trust_summary(s, e)
        ctx["trust_outstanding"] = sum((r["to_remit"] for r in ctx["trust_summary"]), Decimal(0))
        ctx["trust_unreceipted"] = sum((r["unreceipted"] for r in ctx["trust_summary"]), Decimal(0))
        ctx["by_channel"] = balances.income_by_channel(s, e)
        ctx["church"] = cfg.church_name

        # ---- Income & Expenditure statement ----
        income = (Transaction.objects.confirmed_credits().filter(
            date__gte=s, date__lte=e, department__is_trust=False,
            excluded_from_income=False).aggregate(t=Sum("amount"))["t"] or Decimal(0))
        expense = (Expense.objects.filter(date__gte=s, date__lte=e, status__in=paid)
                   .exclude(category=Expense.Category.REMITTANCE)
                   .exclude(expenditure_type=Expense.ExpenditureType.CAPITAL)
                   .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        capital = (Expense.objects.filter(date__gte=s, date__lte=e, status__in=paid,
                   expenditure_type=Expense.ExpenditureType.CAPITAL)
                   .exclude(category=Expense.Category.REMITTANCE)
                   .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        ie_cats = (Expense.objects.filter(date__gte=s, date__lte=e, status__in=paid)
                   .exclude(category=Expense.Category.REMITTANCE)
                   .exclude(expenditure_type=Expense.ExpenditureType.CAPITAL)
                   .values("category").annotate(t=Sum("amount")).order_by("-t"))
        cat_label = dict(Expense.Category.choices)
        ctx["ie_income"] = income
        ctx["ie_expense"] = expense
        ctx["ie_surplus"] = income - expense
        ctx["ie_capital"] = capital
        ctx["ie_expense_by_cat"] = [{"label": cat_label.get(r["category"], r["category"]),
                                     "total": r["t"]} for r in ie_cats]

        # ---- Statement of financial position (period end) ----
        income_all = (Transaction.objects.confirmed_credits()
                      .filter(excluded_from_income=False, date__lte=e)
                      .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        payments_all = (Expense.objects.filter(status__in=paid, date__lte=e)
                        .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        opening = (cfg.opening_bank_balance + cfg.opening_cash_on_hand
                   - cfg.opening_unremitted_trust)
        cash_bank = opening + income_all - payments_all
        payables = open_payables_total()
        accruals = open_accruals_total()
        asset_nbv = Decimal(0)
        try:
            from assets.models import FixedAsset
            asset_nbv = sum((a.net_book_value(e) for a in FixedAsset.objects.filter(active=True)),
                            Decimal(0))
        except Exception:
            asset_nbv = Decimal(0)
        trust_liab = ctx["trust_outstanding"]
        total_assets = cash_bank + asset_nbv
        total_liab = trust_liab + payables + accruals
        ctx["sofp"] = {
            "cash_bank": cash_bank, "asset_nbv": asset_nbv, "total_assets": total_assets,
            "trust_liab": trust_liab, "payables": payables, "accruals": accruals,
            "total_liab": total_liab, "net_assets": total_assets - total_liab,
        }

        # ---- Multi-year trend (like-for-like, year-to-date) ----
        # Compare each year only up to the SAME point in the year as the current
        # report period reaches, so a partial current year isn't unfairly shown
        # against full prior years. We cap each year's figures at the month/day
        # the current period ends on.
        import datetime as _d
        cutoff_month, cutoff_day = e.month, e.day
        ytd_label = f" (Jan–{e:%b})" if (cutoff_month, cutoff_day) != (12, 31) else ""

        def _ytd_filter(qs):
            # keep rows whose (month, day) falls on/before the current cutoff
            from django.db.models.functions import ExtractMonth, ExtractDay
            return (qs.annotate(_m=ExtractMonth("date"), _dd=ExtractDay("date"))
                    .filter(Q(_m__lt=cutoff_month)
                            | Q(_m=cutoff_month, _dd__lte=cutoff_day)))

        inc_y = {r["yr"]: r["total"] for r in (_ytd_filter(
                 Transaction.objects.confirmed_credits()
                 .filter(excluded_from_income=False))
                 .annotate(yr=ExtractYear("date"))
                 .values("yr").annotate(total=Sum("amount")))}
        exp_y = {r["yr"]: r["total"] for r in (_ytd_filter(
                 Expense.objects.filter(status__in=paid)
                 .exclude(category=Expense.Category.REMITTANCE))
                 .annotate(yr=ExtractYear("date")).values("yr")
                 .annotate(total=Sum("amount")))}
        from core.models import HistoricalYear
        hist = {h.year: h for h in HistoricalYear.objects.all()}
        years = sorted(set(inc_y) | set(exp_y) | set(hist))
        ctx["trend_ytd_label"] = ytd_label
        trend = []
        for y in years:
            coll = inc_y.get(y) or (hist[y].collection if y in hist else 0)
            ex = exp_y.get(y) or (hist[y].expenditure if y in hist else 0)
            trend.append({"year": y, "income": coll, "expense": ex,
                          "net": (coll or 0) - (ex or 0)})
        ctx["trend"] = trend

        # ---- narrative (AI with deterministic fallback) ----
        label = f"{s:%d %b %Y} – {e:%d %b %Y}"
        ctx["board_income"] = income
        ctx["board_expenditure"] = expense
        ctx["board_surplus"] = income - expense
        context_str = self._context_str(ctx, label, income, expense)
        narrative, source, err = None, "fallback", None
        if cfg.llm_enabled and self.request.GET.get("ai") != "0":
            narrative, err = board_report_narrative(context_str, label, cfg)
            if narrative:
                source = "ai"
        if not narrative:
            narrative = self._fallback(ctx, label, income, expense)
        ctx["narrative"] = narrative
        ctx["narrative_source"] = source
        ctx["ai_enabled"] = cfg.llm_enabled
        ctx["ai_error"] = err
        return ctx

    def _context_str(self, ctx, label, income, expenditure):
        def f(v):
            return f"{float(v or 0):,.0f}"
        top = sorted(ctx["local_rows"], key=lambda r: r["receipts"], reverse=True)[:6]
        lines = [
            f"Period: {label}",
            f"Local income (I&E): {f(income)}",
            f"Local expenditure (I&E): {f(expenditure)}",
            f"Net surplus/(deficit): {f(income - expenditure)}",
            f"Cash & bank at period end: {f(ctx['sofp']['cash_bank'])}",
            f"Net assets: {f(ctx['sofp']['net_assets'])}",
            f"Trust outstanding to remit: {f(ctx['trust_outstanding'])}",
            "Top funds by receipts: " + "; ".join(
                f"{r['department'].name} {f(r['receipts'])}" for r in top if r["receipts"]),
        ]
        if len(ctx["trend"]) > 1:
            lines.append("Year trend (income): " + "; ".join(
                f"{t['year']}: {f(t['income'])}" for t in ctx["trend"][-4:]))
        deficits = [r for r in ctx["local_rows"] if r["closing"] < 0]
        if deficits:
            lines.append("Funds in deficit: " + "; ".join(
                f"{r['department'].name} {f(r['closing'])}" for r in deficits[:6]))
        return "\n".join(lines)

    def _fallback(self, ctx, label, income, expenditure):
        def f(v):
            return f"{float(v or 0):,.0f}"
        surplus = income - expenditure
        top = [r for r in sorted(ctx["local_rows"], key=lambda r: r["receipts"],
                                 reverse=True) if r["receipts"]][:3]
        deficits = [r for r in ctx["local_rows"] if r["closing"] < 0]
        p = ["Executive summary:",
             f"For {label}, collections were {f(income)} against expenditure of "
             f"{f(expenditure)}, a {'surplus' if surplus >= 0 else 'deficit'} of "
             f"{f(abs(surplus))}.",
             "\nKey insights:"]
        if top:
            p.append("- Largest funds: " + ", ".join(
                f"{r['department'].name} ({f(r['receipts'])})" for r in top) + ".")
        p.append(f"- Trust outstanding to remit: {f(ctx['trust_outstanding'])}.")
        if deficits:
            p.append("- Funds in deficit: " + ", ".join(
                r["department"].name for r in deficits[:5]) + ".")
        p.append("\nRecommendations:")
        if ctx["trust_outstanding"] > 0:
            p.append(f"- Remit the outstanding trust of {f(ctx['trust_outstanding'])}.")
        if deficits:
            p.append("- Review and rebalance funds carrying a negative balance.")
        p.append("- Confirm the bank reconciliation and file supporting vouchers.")
        return "\n".join(p)


class PastorReportView(PeriodMixin, TemplateView):
    """Pastoral summary: tithe, offerings, giving by group, participation."""
    template_name = "reports/pastor_report.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        s, e = ctx["start"], ctx["end"]
        ctx["tithe"] = balances.tithe_total(s, e)
        ctx["by_group"] = balances.giving_by_group(s, e)
        ctx["by_channel"] = balances.income_by_channel(s, e)
        rows = balances.department_summary(s, e)
        ctx["total_income"] = balances.totals(rows)
        from members.models import Member
        ctx["active_members"] = Member.objects.filter(active=True).count()
        givers = (Transaction.objects.confirmed_credits()
                  .filter(date__gte=s, date__lte=e, member__isnull=False)
                  .values("member").distinct().count())
        ctx["givers"] = givers
        ctx["church"] = SiteConfig.get().church_name
        return ctx


class ConferenceSubmissionView(PeriodMixin, TemplateView):
    """Conference submission: trust collected / remitted / to-remit + batches."""
    template_name = "reports/conference_submission.html"

    def get(self, request, *args, **kwargs):
        ctx = self.get_context_data(**kwargs)
        s, e = ctx["start"], ctx["end"]
        rows = balances.trust_summary(s, e)
        cfg = SiteConfig.get()
        if request.GET.get("export") in ("csv", "xlsx"):
            header = ["Trust fund", "Collected", "Remitted",
                      "Outstanding to remit (receipted)", "Unreceipted (pending)",
                      "Total trust liability"]
            data = [[r["department"].name, r["collected"], r["remitted"], r["to_remit"],
                     r["unreceipted"], r["total_liability"]] for r in rows]
            data.append(["TOTAL",
                         sum((r["collected"] for r in rows), Decimal(0)),
                         sum((r["remitted"] for r in rows), Decimal(0)),
                         sum((r["to_remit"] for r in rows), Decimal(0)),
                         sum((r["unreceipted"] for r in rows), Decimal(0)),
                         sum((r["total_liability"] for r in rows), Decimal(0))])
            ex = _export(request, f"conference_{s}_{e}", header, data, "Conference submission")
            if ex:
                return ex
        ctx["rows"] = rows
        ctx["totals"] = {
            "collected": sum((r["collected"] for r in rows), Decimal(0)),
            "remitted": sum((r["remitted"] for r in rows), Decimal(0)),
            "to_remit": sum((r["to_remit"] for r in rows), Decimal(0)),
            "unreceipted": sum((r["unreceipted"] for r in rows), Decimal(0)),
            "total_liability": sum((r["total_liability"] for r in rows), Decimal(0)),
        }
        ctx["field_name"] = cfg.field_name
        ctx["church"] = cfg.church_name
        return self.render_to_response(ctx)


# ===================== Financial statements =====================
class IncomeStatementView(PeriodMixin, TemplateView):
    """Statement of income & expenditure on a LOCAL (operating) basis: trust
    collections and their remittances are excluded, since trust money is held on
    behalf of the field rather than being the church's own income."""
    template_name = "reports/income_statement.html"

    def get(self, request, *args, **kwargs):
        ctx = self.get_context_data(**kwargs)
        s, e = ctx["start"], ctx["end"]
        rows = balances.department_summary(s, e)
        income = [{"name": r["department"].name, "amount": r["receipts"]}
                  for r in rows if not r["is_trust"] and r["receipts"]]
        income.sort(key=lambda x: -x["amount"])
        total_income = sum((r["amount"] for r in income), Decimal(0))
        trust_collected = sum((r["receipts"] for r in rows if r["is_trust"]), Decimal(0))
        # expenditure by category, excluding trust remittances, split recurrent/capital
        eff = Q(status__in=[Expense.Status.APPROVED, Expense.Status.PAID],
                date__gte=s, date__lte=e)
        cats = dict(Expense.Category.choices)
        base = Expense.objects.filter(eff).exclude(category=Expense.Category.REMITTANCE)

        def _by_cat(qs):
            rows = [{"name": cats.get(r["category"], r["category"]), "amount": r["t"]}
                    for r in qs.values("category").annotate(t=Sum("amount")).order_by("-t")]
            return rows, sum((r["amount"] for r in rows), Decimal(0))

        recurrent, total_recurrent = _by_cat(
            base.filter(expenditure_type=Expense.ExpenditureType.RECURRENT))
        capital, total_capital = _by_cat(
            base.filter(expenditure_type=Expense.ExpenditureType.CAPITAL))
        total_exp = total_recurrent + total_capital
        operating = total_income - total_recurrent
        surplus = operating - total_capital
        # Change in net assets (fund basis) — ties the result back to the funds and
        # to the Statement of Financial Position.
        na_open = sum((r["opening"] for r in rows if not r["is_trust"]), Decimal(0))
        net_transfers = sum((r["net_transfer"] for r in rows if not r["is_trust"]), Decimal(0))
        na_close = na_open + surplus + net_transfers
        if request.GET.get("export") in ("csv", "xlsx"):
            header = ["Section", "Line", "Amount"]
            data = [["Revenue", r["name"], r["amount"]] for r in income]
            data.append(["Revenue", "TOTAL REVENUE", total_income])
            data += [["Operating (recurrent) expenditure", r["name"], r["amount"]] for r in recurrent]
            data.append(["Operating (recurrent) expenditure", "TOTAL RECURRENT", total_recurrent])
            data.append(["Result", "OPERATING SURPLUS/(DEFICIT)", operating])
            data += [["Capital expenditure", r["name"], r["amount"]] for r in capital]
            data.append(["Capital expenditure", "TOTAL CAPITAL", total_capital])
            data.append(["Result", "NET SURPLUS/(DEFICIT)", surplus])
            data += [
                ["Net assets", "Net assets brought forward", na_open],
                ["Net assets", "Net surplus/(deficit) for the period", surplus],
                ["Net assets", "Net inter-fund transfers", net_transfers],
                ["Net assets", "NET ASSETS CARRIED FORWARD", na_close],
            ]
            ex = _export(request, f"income_statement_{s}_{e}", header, data,
                         "Statement of Financial Activity")
            if ex:
                return ex
        ctx.update({"income": income, "total_income": total_income,
                    "recurrent": recurrent, "total_recurrent": total_recurrent,
                    "capital": capital, "total_capital": total_capital,
                    "operating": operating, "total_exp": total_exp,
                    "surplus": surplus, "trust_collected": trust_collected,
                    "na_open": na_open, "net_transfers": net_transfers,
                    "na_close": na_close,
                    "church": SiteConfig.get().church_name})
        return self.render_to_response(ctx)


class FinancialPositionView(ReadAccessMixin, TemplateView):
    """Statement of Financial Position (balance sheet) on a fund-accounting basis,
    as at a date. Assets = cash/bank (fund balances) + fixed assets (NBV);
    financed by trust funds payable, accumulated local funds, and a capital fund
    matching the carrying value of fixed assets."""
    template_name = "reports/financial_position.html"

    def get(self, request, *args, **kwargs):
        from assets.models import FixedAsset
        try:
            as_of = _dt.date.fromisoformat(request.GET.get("as_of", ""))
        except ValueError:
            as_of = _dt.date.today()
        rows = balances.department_summary(None, as_of)
        cash = sum((r["closing"] for r in rows), Decimal(0))
        trust_payable = sum((r["closing"] for r in rows if r["is_trust"]), Decimal(0))
        local_rows = [r for r in rows if not r["is_trust"]]
        local_funds = sum((r["closing"] for r in local_rows), Decimal(0))
        from assets.models import nbv_total
        nbv = nbv_total(as_of)
        # Net assets, classified per the SDA framework: Board-designated funds
        # (development/projects) are "Allocated"; the rest are "Unallocated"; the
        # carrying value of property is held as "Invested in property".
        allocated = sum((r["closing"] for r in local_rows
                         if r["department"].category == "DEVELOPMENT"), Decimal(0))
        unallocated = local_funds - allocated
        # Accrual overlay (memoranda): credit purchases owed, expenses accrued, and
        # amounts prepaid. These adjust the cash-basis position to an accrual view.
        from cashbook.views import (open_payables_total, open_accruals_total,
                                     unexpired_prepayments_total,
                                     outstanding_advances_total)
        payables = open_payables_total(as_of)
        accruals = open_accruals_total(as_of)
        prepaid = unexpired_prepayments_total(as_of)
        # An unspent staff advance is cash that has physically left but not yet been
        # expensed — a receivable. Reclassify it out of cash so each is shown
        # correctly; totals are unchanged (it is still inside the cash figure).
        advances = outstanding_advances_total(as_of)
        cash_on_hand = cash - advances
        # Bank money received but not yet receipted/allocated to a fund — shown as
        # cash held in suspense with a matching "pending allocation" liability, so
        # the statement ties to the bank and the money is never invisible.
        pending = balances.pending_receipts_total(as_of)
        accrual_adj = prepaid - payables - accruals
        net_assets = unallocated + allocated + nbv + accrual_adj
        total_assets = cash_on_hand + advances + pending + nbv + prepaid
        total_liabilities = trust_payable + payables + accruals + pending
        total_liab_and_na = total_liabilities + net_assets
        # committed-but-unpaid vouchers (memorandum)
        unpaid = (Expense.objects.filter(status__in=[Expense.Status.PENDING,
                  Expense.Status.APPROVED], date__lte=as_of)
                  .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        ctx = self.get_context_data(**kwargs)
        _cfg = SiteConfig.get()
        if request.GET.get("export") in ("csv", "xlsx"):
            header = ["Section", "Line", "Amount"]
            data = [
                ["Assets", "Cash & bank (current)", cash_on_hand],
                ["Assets", "Staff advances (receivable)", advances],
                ["Assets", "Bank receipts pending allocation", pending],
                ["Assets", "Prepayments", prepaid],
                ["Assets", "Property, plant & equipment (non-current)", nbv],
                ["Assets", "TOTAL ASSETS", total_assets],
                ["Liabilities", "Trust funds payable to the field", trust_payable],
                ["Liabilities", "Payables", payables],
                ["Liabilities", "Accruals", accruals],
                ["Liabilities", "Receipts pending allocation", pending],
                ["Liabilities", "TOTAL LIABILITIES", total_liabilities],
                ["Net assets", "Unallocated funds", unallocated],
                ["Net assets", "Allocated funds (designated)", allocated],
                ["Net assets", "Invested in property", nbv],
                ["Net assets", "Accrual adjustment", accrual_adj],
                ["Net assets", "TOTAL NET ASSETS", net_assets],
                ["", "TOTAL LIABILITIES & NET ASSETS", total_liab_and_na],
            ]
            ex = _export(request, f"financial_position_{as_of}", header, data,
                         "Statement of Financial Position")
            if ex:
                return ex
        ctx.update({"as_of": as_of, "cash": cash, "nbv": nbv,
                    "cash_on_hand": cash_on_hand, "advances": advances,
                    "pending": pending,
                    "trust_payable": trust_payable, "local_funds": local_funds,
                    "unallocated": unallocated, "allocated": allocated,
                    "net_assets": net_assets, "total_assets": total_assets,
                    "total_liab_and_na": total_liab_and_na,
                    "payables": payables, "accruals": accruals, "prepaid": prepaid,
                    "accrual_adj": accrual_adj, "total_liabilities": total_liabilities,
                    "balanced": total_assets == total_liab_and_na,
                    "unpaid": unpaid, "trust_rows": [r for r in rows if r["is_trust"]],
                    "local_rows": [r for r in local_rows if r["closing"]],
                    "opening_bank": _cfg.opening_bank_balance,
                    "opening_cash_on_hand": _cfg.opening_cash_on_hand,
                    "opening_unremitted_trust": _cfg.opening_unremitted_trust,
                    "opening_total": (_cfg.opening_bank_balance
                                      + _cfg.opening_cash_on_hand
                                      - _cfg.opening_unremitted_trust),
                    "church": SiteConfig.get().church_name})
        return self.render_to_response(ctx)

class ChangesInNetAssetsView(PeriodMixin, TemplateView):
    """Statement of Changes in Net Assets — how each class of net assets moved over
    the period: opening + surplus/(deficit) +/- capital reclassification +/-
    transfers = closing. Classified into Unallocated, Allocated (Board-designated)
    and Invested in property, and ties to the Statement of Financial Position."""
    template_name = "reports/changes_in_net_assets.html"

    def get(self, request, *args, **kwargs):
        from assets.models import FixedAsset
        ctx = self.get_context_data(**kwargs)
        s, e = ctx["start"], ctx["end"]
        rows = balances.department_summary(s, e)
        local = [r for r in rows if not r["is_trust"]]

        def is_alloc(r):
            return r["department"].category == "DEVELOPMENT"

        eff = Q(status__in=[Expense.Status.APPROVED, Expense.Status.PAID],
                date__gte=s, date__lte=e)

        def cap_for(alloc):
            qs = (Expense.objects.filter(eff, expenditure_type=Expense.ExpenditureType.CAPITAL,
                  department__fund_type="LOCAL").exclude(category=Expense.Category.REMITTANCE))
            tot = Decimal(0)
            for x in qs.select_related("department"):
                if (x.department.category == "DEVELOPMENT") == alloc:
                    tot += x.amount
            return tot

        def col(alloc):
            sub = [r for r in local if is_alloc(r) == alloc]
            opening = sum((r["opening"] for r in sub), Decimal(0))
            receipts = sum((r["receipts"] for r in sub), Decimal(0))
            expenses = sum((r["expenses"] for r in sub), Decimal(0))
            transfers = sum((r["net_transfer"] for r in sub), Decimal(0))
            closing = sum((r["closing"] for r in sub), Decimal(0))
            capital = cap_for(alloc)
            op_surplus = receipts - (expenses - capital)   # surplus before capital
            return {"opening": opening, "op_surplus": op_surplus, "capital": capital,
                    "transfers": transfers, "closing": closing}

        un, al = col(False), col(True)
        cap_total = un["capital"] + al["capital"]
        day_before = s - _dt.timedelta(days=1)
        from assets.models import nbv_total
        nbv_open = nbv_total(day_before)
        nbv_close = nbv_total(e)
        depr = nbv_close - nbv_open - cap_total      # balancing: depreciation + disposals
        prop = {"opening": nbv_open, "additions": cap_total, "depr": depr, "closing": nbv_close}

        t_open = un["opening"] + al["opening"] + nbv_open
        t_opsurplus = un["op_surplus"] + al["op_surplus"]
        t_transfers = un["transfers"] + al["transfers"]
        t_close = un["closing"] + al["closing"] + nbv_close

        if request.GET.get("export") in ("csv", "xlsx"):
            header = ["Line", "Unallocated", "Allocated", "Invested in property", "Total"]
            data = [
                ["Net assets, beginning", un["opening"], al["opening"], nbv_open, t_open],
                ["Surplus/(deficit) from operations", un["op_surplus"], al["op_surplus"], 0, t_opsurplus],
                ["Capital expenditure (property acquired)", -un["capital"], -al["capital"], cap_total, 0],
                ["Depreciation & disposals", 0, 0, depr, depr],
                ["Net inter-fund transfers", un["transfers"], al["transfers"], 0, t_transfers],
                ["Net assets, end", un["closing"], al["closing"], nbv_close, t_close],
            ]
            ex = _export(request, f"changes_in_net_assets_{s}_{e}", header, data,
                         "Statement of Changes in Net Assets")
            if ex:
                return ex
        ctx.update({"un": un, "al": al, "prop": prop, "cap_total": cap_total,
                    "t_open": t_open, "t_opsurplus": t_opsurplus, "depr": depr,
                    "t_transfers": t_transfers, "t_close": t_close,
                    "church": SiteConfig.get().church_name})
        return self.render_to_response(ctx)


class StatementOfCashFlowsView(PeriodMixin, TemplateView):
    """Statement of Cash Flows on the SDA three-category basis (operating, investing,
    financing). Reconciles the movement in total cash & bank over the period."""
    template_name = "reports/cash_flows.html"

    def get(self, request, *args, **kwargs):
        ctx = self.get_context_data(**kwargs)
        s, e = ctx["start"], ctx["end"]
        rows = balances.department_summary(s, e)
        cash_open = sum((r["opening"] for r in rows), Decimal(0))
        cash_close = sum((r["closing"] for r in rows), Decimal(0))
        local_receipts = sum((r["receipts"] for r in rows if not r["is_trust"]), Decimal(0))
        trust_receipts = sum((r["receipts"] for r in rows if r["is_trust"]), Decimal(0))
        eff = Q(status__in=[Expense.Status.APPROVED, Expense.Status.PAID],
                date__gte=s, date__lte=e)

        def _sum(qs):
            return qs.aggregate(t=Sum("amount"))["t"] or Decimal(0)

        remittances = _sum(Expense.objects.filter(eff, category=Expense.Category.REMITTANCE))
        nonremit = Expense.objects.filter(eff).exclude(category=Expense.Category.REMITTANCE)
        operating_exp = _sum(nonremit.filter(expenditure_type=Expense.ExpenditureType.RECURRENT))
        capital = _sum(nonremit.filter(expenditure_type=Expense.ExpenditureType.CAPITAL))

        net_operating = local_receipts + trust_receipts - operating_exp - remittances
        net_investing = -capital
        net_financing = Decimal(0)
        net_change = net_operating + net_investing + net_financing

        if request.GET.get("export") in ("csv", "xlsx"):
            header = ["Section", "Line", "Amount"]
            data = [
                ["Operating", "Local offerings & income received", local_receipts],
                ["Operating", "Tithe & trust offerings received (held for the field)", trust_receipts],
                ["Operating", "Operating (recurrent) expenses paid", -operating_exp],
                ["Operating", "Remittances to the field paid", -remittances],
                ["Operating", "Net cash from operating activities", net_operating],
                ["Investing", "Purchase of property & equipment", -capital],
                ["Investing", "Net cash used in investing activities", net_investing],
                ["Financing", "Net cash from financing activities", net_financing],
                ["Summary", "Net increase/(decrease) in cash", net_change],
                ["Summary", "Cash & bank at beginning of period", cash_open],
                ["Summary", "Cash & bank at end of period", cash_open + net_change],
            ]
            ex = _export(request, f"cash_flows_{s}_{e}", header, data,
                         "Statement of Cash Flows")
            if ex:
                return ex
        ctx.update({"local_receipts": local_receipts, "trust_receipts": trust_receipts,
                    "operating_exp": operating_exp, "remittances": remittances,
                    "capital": capital, "net_operating": net_operating,
                    "net_investing": net_investing, "net_financing": net_financing,
                    "net_change": net_change, "cash_open": cash_open,
                    "cash_close": cash_close, "cash_end_calc": cash_open + net_change,
                    "ties": (cash_open + net_change) == cash_close,
                    "church": SiteConfig.get().church_name})
        return self.render_to_response(ctx)


class BudgetBoardReportView(ReadAccessMixin, TemplateView):
    """Board-facing budget summary: per-department budget by source of funds, with
    Local Church Budget exposure (departmental allocations) and prior-year pegging."""
    template_name = "reports/budget_board.html"

    def get(self, request, *args, **kwargs):
        today = _dt.date.today()
        try:
            year = int(request.GET.get("year", today.year))
        except (TypeError, ValueError):
            year = today.year
        data = budget_svc.board_budget(year)
        if request.GET.get("export") in ("csv", "xlsx"):
            header = ["Department", "Trust?", "Own funds", "Local Church Budget",
                      "Other funds", "Total budget", f"{year - 1} total"]
            rows = [[r["dept"].name, "Yes" if r["is_trust"] else "No", r["own"],
                     r["lcb"], r["other"], r["total"], r["prior"]] for r in data["rows"]]
            t = data["totals"]
            rows.append(["TOTAL", "", t["own"], t["lcb"], t["other"], t["budget"], t["prior"]])
            return _export(request, f"board_budget_{year}", header, rows,
                           f"Board Budget Summary {year}")
        ctx = {"year": year, "data": data, "totals": data["totals"],
               "years": range(today.year + 1, today.year - 5, -1)}
        return self.render_to_response(ctx)


class DevGroupMembersView(PeriodMixin, TemplateView):
    """Members and their contributions to one development group, for the group
    leader's reconciliation. Can be emailed to the leader if an email is set."""
    template_name = "reports/dev_group_members.html"

    def get_context_data(self, **kwargs):
        from django.shortcuts import get_object_or_404
        from departments.models import DevelopmentGroup
        ctx = super().get_context_data(**kwargs)
        group = get_object_or_404(DevelopmentGroup, pk=kwargs["pk"])
        data = balances.dev_group_members(group, ctx["start"], ctx["end"])
        ctx.update({"group": group, "rows": data["rows"], "grand_total": data["total"]})
        return ctx

    def get(self, request, *args, **kwargs):
        ctx = self.get_context_data(**kwargs)
        if request.GET.get("export") in ("csv", "xlsx"):
            header = ["Member", "Phone", "Contributions", "Total"]
            rows = [[r["name"], r["phone"], r["count"], r["total"]] for r in ctx["rows"]]
            rows.append(["TOTAL", "", "", ctx["grand_total"]])
            return _export(request, f"devgroup_{ctx['group'].number}_members",
                           header, rows, f"{ctx['group'].label} — member contributions")
        return self.render_to_response(ctx)

    def post(self, request, *args, **kwargs):
        """Email the report to the group leader."""
        ctx = self.get_context_data(**kwargs)
        group = ctx["group"]
        if not group.leader_email:
            messages.error(request, "This group has no leader email set. Add one in the "
                                    "development group settings.")
            return redirect(request.get_full_path())
        from core.services.email import send_email, is_configured
        if not is_configured():
            messages.error(request, "Email isn't configured. Set it up in Settings → Email.")
            return redirect(request.get_full_path())
        lines = [f"{r['name']}: {r['total']:,.2f} ({r['count']} donation(s))" for r in ctx["rows"]]
        body = (f"{group.label} — member contributions\n"
                f"Period: {ctx['start']:%d %b %Y} to {ctx['end']:%d %b %Y}\n\n"
                + ("\n".join(lines) if lines else "No contributions in this period.")
                + f"\n\nTOTAL: {ctx['grand_total']:,.2f}")
        ok, detail = send_email(f"{group.label} contribution report",
                                body, group.leader_email)
        (messages.success if ok else messages.error)(
            request, f"Report to {group.leader_email}: {detail}")
        return redirect(request.get_full_path())


class DevGroupAllExcelView(ReadAccessMixin, View):
    """One workbook for all development groups: a summary sheet plus a per-group
    sheet of member contributions, for detailed offline analysis."""
    def get(self, request):
        import io
        import openpyxl
        from django.http import HttpResponse
        from departments.models import DevelopmentGroup
        start, end = parse_period(request)[:2] if request.GET.get("start") else (None, None)
        wb = openpyxl.Workbook()
        summary = wb.active
        summary.title = "Summary"
        summary.append(["Group", "Leader", "Email", "Collected", "Target",
                        "Balance", "% complete"])
        progress = {p["group"].id: p for p in balances.dev_group_progress()}
        for g in DevelopmentGroup.objects.filter(active=True):
            pr = progress.get(g.id, {})
            summary.append([g.label, g.leader_name, g.leader_email,
                            float(pr.get("collected", 0)),
                            float(pr.get("target", 0) or 0),
                            float(pr.get("balance", 0)), pr.get("pct", 0)])
            data = balances.dev_group_members(g, start, end)
            title = ("G%d" % g.number)[:28]
            ws = wb.create_sheet(title=title)
            ws.append([g.label + (" — " + g.leader_name if g.leader_name else "")])
            ws.append(["Member", "Phone", "Contributions", "Total"])
            for r in data["rows"]:
                ws.append([r["name"], r["phone"], r["count"], float(r["total"])])
            ws.append(["TOTAL", "", "", float(data["total"])])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        resp = HttpResponse(buf.read(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resp["Content-Disposition"] = 'attachment; filename="development_groups_detailed.xlsx"'
        return resp


class DevGroupEmailAllView(TreasurerRequiredMixin, View):
    """Email each group's member report to its leader, spacing sends 30s apart so
    the mail provider doesn't flag a burst as spam. Runs in the background."""
    def post(self, request):
        import threading
        from departments.models import DevelopmentGroup
        from core.services.email import is_configured
        if not is_configured():
            messages.error(request, "Email isn't configured. Set it up in Settings → Email.")
            return redirect("report_dev_groups")
        start, end = (parse_period(request)[:2] if request.GET.get("start")
                      else (None, None))
        groups = list(DevelopmentGroup.objects.filter(active=True).exclude(leader_email=""))
        if not groups:
            messages.info(request, "No groups have a leader email set.")
            return redirect("report_dev_groups")
        ids = [g.id for g in groups]
        threading.Thread(target=self._send_all, args=(ids, start, end), daemon=True).start()
        messages.success(request, f"Queued reports for {len(ids)} group leader(s). "
                                  f"They'll be sent about 30 seconds apart to avoid "
                                  f"spam filters.")
        return redirect("report_dev_groups")

    @staticmethod
    def _send_all(group_ids, start, end):
        import time
        from django.db import connection
        from departments.models import DevelopmentGroup
        from core.services.email import send_email
        from core.models import Notification
        try:
            for i, gid in enumerate(group_ids):
                g = DevelopmentGroup.objects.filter(pk=gid).first()
                if not g or not g.leader_email:
                    continue
                data = balances.dev_group_members(g, start, end)
                lines = [f"{r['name']}: {r['total']:,.2f} ({r['count']} donation(s))"
                         for r in data["rows"]]
                body = (f"{g.label} — member contributions\n\n"
                        + ("\n".join(lines) if lines else "No contributions in the period.")
                        + f"\n\nTOTAL: {data['total']:,.2f}")
                ok, detail = send_email(f"{g.label} contribution report",
                                        body, g.leader_email)
                Notification.objects.create(
                    kind=Notification.Kind.GENERAL,
                    message=f"{g.label} report to {g.leader_email}: "
                            f"{'sent' if ok else 'failed — ' + detail[:80]}")
                if i < len(group_ids) - 1:
                    time.sleep(30)
        finally:
            connection.close()


class BankPositionView(ReadAccessMixin, TemplateView):
    """Bank reconciliation: does the system's bank balance agree with the bank?

    The system's bank position = opening bank balance + every confirmed BANK
    credit − every confirmed BANK debit (expenses paid from the bank appear as
    debit rows). The bank's own figure is the closing running balance of the most
    recent imported statement. If the two differ, an entry is on the statement but
    not in the app (or vice versa) — exactly the un-entered-entry case. We show the
    gap and list the most likely culprits so the treasurer can chase them.
    """
    template_name = "reports/bank_position.html"

    def get_context_data(self, **kwargs):
        from decimal import Decimal
        from statements.models import StatementImport
        ctx = super().get_context_data(**kwargs)
        cfg = SiteConfig.get()
        opening = cfg.opening_bank_balance or Decimal(0)

        # most recent statement with a captured closing balance
        stmt = (StatementImport.objects.exclude(status="PURGED")
                .exclude(stmt_closing_balance__isnull=True)
                .order_by("-stmt_last_date", "-uploaded_at").first())
        ctx["stmt"] = stmt

        # system bank movements up to the statement's last date (or all, if none)
        cutoff = stmt.stmt_last_date if stmt else None
        bank = Transaction.objects.filter(channel=Transaction.Channel.BANK,
                                          confirmed=True, is_reversal=False,
                                          is_reversed=False)
        if cutoff:
            bank = bank.filter(date__lte=cutoff)
        credits = bank.filter(direction=Transaction.Direction.CREDIT).aggregate(
            s=Sum("amount"))["s"] or Decimal(0)
        debits = bank.filter(direction=Transaction.Direction.DEBIT).aggregate(
            s=Sum("amount"))["s"] or Decimal(0)
        system_balance = opening + credits - debits

        ctx["opening"] = opening
        ctx["bank_credits"] = credits
        ctx["bank_debits"] = debits
        ctx["system_balance"] = system_balance
        ctx["statement_balance"] = stmt.stmt_closing_balance if stmt else None
        ctx["difference"] = ((stmt.stmt_closing_balance - system_balance)
                             if stmt else None)

        # if there is a gap, surface candidates: recent bank rows that look
        # suspicious (unallocated, in review, or unconfirmed) which often explain
        # a difference, plus a note about the statement's own integrity check.
        ctx["suspects"] = []
        if stmt and ctx["difference"] and abs(ctx["difference"]) > Decimal("0.01"):
            suspects = (Transaction.objects.filter(
                channel=Transaction.Channel.BANK)
                .filter(Q(confirmed=False) | Q(allocation_status="REVIEW")
                        | Q(department__isnull=True))
                .order_by("-date")[:50])
            ctx["suspects"] = [{
                "date": t.date, "payer": t.payer_name or "—",
                "amount": t.amount if t.direction == "CREDIT" else -t.amount,
                "ref": t.mpesa_ref or t.core_ref or t.reference or "",
                "why": ("not confirmed" if not t.confirmed
                        else "in review" if t.allocation_status == "REVIEW"
                        else "no fund"),
                "id": t.id} for t in suspects]
            ctx["stmt_integrity"] = stmt.balance_check
            ctx["stmt_integrity_detail"] = stmt.balance_detail
        return ctx
