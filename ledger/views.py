import datetime as dt
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.shortcuts import render, redirect, get_object_or_404
from django.views import View

from core.permissions import ReadAccessMixin, TreasurerRequiredMixin
from core.utils import parse_period
from .models import Account, JournalEntry
from .services import posting


class ChartOfAccountsView(ReadAccessMixin, View):
    template_name = "ledger/chart.html"

    def get(self, request):
        groups = []
        for value, label in Account.Type.choices:
            accts = Account.objects.filter(type=value).order_by("code")
            if accts:
                groups.append({"label": label, "type": value, "accounts": accts})
        return render(request, self.template_name,
                      {"groups": groups, "ready": posting.chart_ready()})


class TrialBalanceView(ReadAccessMixin, View):
    template_name = "ledger/trial_balance.html"

    def get(self, request):
        start, end = parse_period(request)
        rows, totals = posting.trial_balance(start, end)
        if request.GET.get("export") in ("csv", "xlsx"):
            from reports.exports import csv_response, xlsx_response
            from core.models import SiteConfig
            header = ["Code", "Account", "Type", "Debit", "Credit"]
            data = [[r["account"].code, r["account"].name, r["account"].get_type_display(),
                     r["debit"], r["credit"]] for r in rows]
            data.append(["", "TOTALS", "", totals["debit"], totals["credit"]])
            fn = f"trial_balance_{end}"
            if request.GET.get("export") == "xlsx":
                return xlsx_response(fn + ".xlsx", header, data, title="Trial Balance",
                                     church=SiteConfig.get().church_name)
            return csv_response(fn + ".csv", header, data)
        return render(request, self.template_name, {
            "rows": rows, "totals": totals, "start": start, "end": end,
            "balanced": totals["debit"] == totals["credit"], "ready": posting.chart_ready()})


class GeneralLedgerView(ReadAccessMixin, View):
    template_name = "ledger/general_ledger.html"

    def get(self, request):
        start, end = parse_period(request)
        accounts = Account.objects.filter(active=True).order_by("code")
        code = request.GET.get("account") or (accounts.first().code if accounts else None)
        account = Account.objects.filter(code=code).first()
        rows = posting.ledger_for(account, start, end) if account else []
        return render(request, self.template_name, {
            "accounts": accounts, "account": account, "rows": rows,
            "start": start, "end": end})


class JournalView(ReadAccessMixin, View):
    template_name = "ledger/journal.html"

    def get(self, request):
        start, end = parse_period(request)
        entries = (JournalEntry.objects.filter(date__gte=start, date__lte=end)
                   .prefetch_related("lines__account").order_by("-date", "-id")[:2000])
        if request.GET.get("export") in ("csv", "xlsx"):
            from reports.exports import csv_response, xlsx_response
            from core.models import SiteConfig
            header = ["Date", "Source", "Account", "Memo", "Debit", "Credit"]
            rows = []
            for en in entries:
                for ln in en.lines.all():
                    rows.append([en.date.isoformat(),
                                 f"{en.source_type}#{en.source_id}" if en.source_id else en.source_type,
                                 ln.account.name if ln.account else "",
                                 en.memo or "", ln.debit or 0, ln.credit or 0])
            fn = f"general_journal_{start}_{end}"
            if request.GET.get("export") == "xlsx":
                return xlsx_response(fn + ".xlsx", header, rows,
                    title=f"General journal {start} to {end}",
                    church=SiteConfig.get().church_name)
            return csv_response(fn + ".csv", header, rows)
        return render(request, self.template_name,
                      {"entries": entries[:200], "start": start, "end": end})


class RebuildLedgerView(TreasurerRequiredMixin, View):
    def post(self, request):
        n = posting.rebuild()
        messages.success(request, f"General ledger rebuilt from source documents "
                                  f"({n} journal entries posted).")
        return redirect(request.META.get("HTTP_REFERER") or "trial_balance")


class ReconciliationReportView(ReadAccessMixin, View):
    """Proves the general ledger ties to the fund reports: per fund, the balance
    per the fund engine vs the balance per the GL, plus the entity equation."""
    template_name = "ledger/reconciliation.html"

    def get(self, request):
        from decimal import Decimal
        from departments.models import Department
        from reports.services import balances
        eng = {r["department"].id: r for r in balances.department_summary(None, None, consolidated=False)}
        rows, diffs = [], Decimal(0)
        for d in Department.objects.filter(active=True).order_by("is_trust", "name"):
            engine_bal = eng.get(d.id, {}).get("closing", Decimal(0))
            gl_bal = posting.fund_balance_from_ledger(d)
            diff = engine_bal - gl_bal
            diffs += abs(diff)
            if engine_bal == 0 and gl_bal == 0:
                continue
            rows.append({"fund": d, "engine": engine_bal, "ledger": gl_bal,
                         "diff": diff, "ok": diff == 0, "is_trust": d.is_trust})
        eq = posting.accounting_equation()
        if request.GET.get("export") in ("csv", "xlsx"):
            from reports.exports import csv_response, xlsx_response
            from core.models import SiteConfig
            header = ["Fund", "Type", "Per fund report", "Per general ledger", "Difference"]
            data = [[r["fund"].name, "Trust" if r["is_trust"] else "Local",
                     r["engine"], r["ledger"], r["diff"]] for r in rows]
            if request.GET.get("export") == "xlsx":
                return xlsx_response("ledger_reconciliation.xlsx", header, data,
                    title="General ledger reconciliation",
                    church=SiteConfig.get().church_name)
            return csv_response("ledger_reconciliation.csv", header, data)
        return render(request, self.template_name, {
            "rows": rows, "all_tie": diffs == 0, "eq": eq,
            "ready": posting.chart_ready()})


class FundVarianceView(ReadAccessMixin, View):
    """Drill-down: list the actual entries causing a fund's engine-vs-ledger
    variance, so the treasurer can fix the specific records."""
    template_name = "ledger/fund_variance.html"

    def get(self, request, pk):
        from decimal import Decimal
        from departments.models import Department
        from reports.services import balances
        dept = get_object_or_404(Department, pk=pk)
        eng = {r["department"].id: r
               for r in balances.department_summary(None, None, consolidated=False)}
        engine_bal = eng.get(dept.id, {}).get("closing", Decimal(0))
        gl_bal = posting.fund_balance_from_ledger(dept)
        issues = posting.fund_variance_detail(dept)
        explained = sum((i["amount"] for i in issues), Decimal(0))
        return render(request, self.template_name, {
            "dept": dept, "engine": engine_bal, "ledger": gl_bal,
            "diff": engine_bal - gl_bal, "issues": issues,
            "explained": explained,
            "unexplained": (engine_bal - gl_bal) - explained})


from django.views.generic import CreateView, UpdateView
from django.urls import reverse_lazy
from .forms import AccountForm


class AccountCreate(TreasurerRequiredMixin, CreateView):
    model = Account
    form_class = AccountForm
    template_name = "ledger/account_form.html"
    success_url = reverse_lazy("chart_of_accounts")

    def form_valid(self, form):
        messages.success(self.request, f"Account {form.instance.code} added.")
        return super().form_valid(form)


class AccountUpdate(TreasurerRequiredMixin, UpdateView):
    model = Account
    form_class = AccountForm
    template_name = "ledger/account_form.html"
    success_url = reverse_lazy("chart_of_accounts")

    def form_valid(self, form):
        messages.success(self.request, f"Account {form.instance.code} updated.")
        return super().form_valid(form)


class AccountDelete(TreasurerRequiredMixin, View):
    def post(self, request, pk):
        acct = get_object_or_404(Account, pk=pk)
        if acct.system_key:
            messages.error(request, f"{acct.code} {acct.name} is a built-in account and "
                                    "can't be deleted. You can deactivate it instead.")
            return redirect("chart_of_accounts")
        if acct.lines.exists():
            acct.active = False
            acct.save(update_fields=["active"])
            messages.info(request, f"{acct.code} {acct.name} has postings, so it was "
                                   "deactivated rather than deleted (keeps the ledger intact).")
            return redirect("chart_of_accounts")
        name = f"{acct.code} {acct.name}"
        acct.delete()
        messages.success(request, f"Account {name} deleted.")
        return redirect("chart_of_accounts")


class ManualJournalCreate(TreasurerRequiredMixin, View):
    """Post a manual, balanced correcting journal entry (audit adjustment, opening
    balance correction, reclassification). Manual entries survive a ledger rebuild."""
    template_name = "ledger/manual_journal.html"

    def _accounts(self):
        return Account.objects.order_by("code", "name")

    def get(self, request):
        from departments.models import Department
        return render(request, self.template_name, {
            "accounts": self._accounts(),
            "departments": Department.objects.filter(active=True).order_by("name"),
            "today": dt.date.today().isoformat()})

    def post(self, request):
        from departments.models import Department
        from core.models import period_locked
        date_s = request.POST.get("date")
        memo = (request.POST.get("memo") or "").strip()
        try:
            date = dt.date.fromisoformat(date_s)
        except (TypeError, ValueError):
            messages.error(request, "Enter a valid date.")
            return redirect("manual_journal")
        if period_locked(date):
            messages.error(request, "That period is locked. Unlock it before posting.")
            return redirect("manual_journal")
        if not memo:
            messages.error(request, "A narration is required for a manual entry.")
            return redirect("manual_journal")
        # gather lines
        accts = request.POST.getlist("account")
        depts = request.POST.getlist("dept")
        debits = request.POST.getlist("debit")
        credits = request.POST.getlist("credit")
        lines, tot_d, tot_c = [], Decimal(0), Decimal(0)
        for i, acc_id in enumerate(accts):
            if not acc_id:
                continue
            acc = Account.objects.filter(pk=acc_id).first()
            if not acc:
                continue
            try:
                d = Decimal(debits[i] or "0")
                c = Decimal(credits[i] or "0")
            except (InvalidOperation, IndexError):
                d = c = Decimal(0)
            if d == 0 and c == 0:
                continue
            dep = None
            if i < len(depts) and depts[i]:
                dep = Department.objects.filter(pk=depts[i]).first()
            lines.append((acc, d, c, dep))
            tot_d += d
            tot_c += c
        if len(lines) < 2:
            messages.error(request, "A journal entry needs at least two lines.")
            return redirect("manual_journal")
        if tot_d != tot_c:
            messages.error(request, f"Entry is not balanced: debits {tot_d:,.2f} ≠ "
                                    f"credits {tot_c:,.2f}.")
            return redirect("manual_journal")
        posting._entry(date, memo, "manual", None, lines)
        messages.success(request, f"Manual journal entry posted ({tot_d:,.2f}).")
        return redirect("journal")
