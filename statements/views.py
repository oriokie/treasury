from django.contrib import messages
from django.db.models import Sum
from django.shortcuts import redirect
from django.views.generic import ListView, DetailView, FormView

from core.permissions import (DataEntryRequiredMixin, ReadAccessMixin,
                              TreasurerRequiredMixin)
from .forms import UploadForm
from .models import StatementImport
from .services.importer import run_import


class StatementListView(ReadAccessMixin, ListView):
    model = StatementImport
    template_name = "statements/list.html"

    def get_context_data(self, **kwargs):
        from django.utils import timezone as _tz
        ctx = super().get_context_data(**kwargs)
        ctx["today_str"] = _tz.localdate().isoformat()
        return ctx
    context_object_name = "imports"
    paginate_by = 25


class StatementUploadView(DataEntryRequiredMixin, FormView):
    template_name = "statements/upload.html"
    form_class = UploadForm

    def form_valid(self, form):
        f = form.cleaned_data["file"]
        bank_account = form.cleaned_data.get("bank_account")
        imp = StatementImport.objects.create(
            uploaded_by=self.request.user, filename=f.name, file=f,
            bank_account=bank_account)
        # synchronous import (swap for a Celery task for very large files)
        imp.file.seek(0)
        content = imp.file.read()
        run_import(imp, content, f.name, bank_account=bank_account)
        if imp.status == StatementImport.Status.FAILED:
            messages.error(self.request, f"Import failed: {imp.error_detail}")
        else:
            messages.success(
                self.request,
                f"Imported {imp.imported}, queued {imp.queued_for_review}, "
                f"skipped {imp.duplicates_skipped} duplicate(s).")
        return redirect("statement_detail", pk=imp.pk)


class ImportStatusView(ReadAccessMixin, DetailView):
    model = StatementImport
    template_name = "statements/detail.html"
    context_object_name = "imp"

    def get_context_data(self, **kwargs):
        from giving.models import Transaction as _T
        ctx = super().get_context_data(**kwargs)
        ctx["auto_pending"] = _T.objects.filter(
            statement_import=self.object, confirmed=False).count()
        return ctx


# ---- Bank reconciliation sheet ----
from decimal import Decimal
from django.contrib import messages
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import ListView

from core.permissions import ReadAccessMixin, DataEntryRequiredMixin
from giving.models import Transaction
from .models import BankReconciliation, ReconciliationItem
from .forms import BankReconciliationForm, ReconciliationItemForm


def _ledger_bank_balance(up_to_date):
    """Cash-book balance per the books, to date: opening cash position + confirmed
    income (excluding double-counted lines such as bank M-Pesa already receipted as
    envelopes) − approved/paid expenses (including trust remittances). This is the
    figure the bank statement reconciles *to* via cash-at-hand, unpresented cheques
    and unremitted-trust adjustments — NOT the raw bank-credit total."""
    from core.models import SiteConfig
    from cashbook.models import Expense
    cfg = SiteConfig.get()
    opening = (cfg.opening_bank_balance + cfg.opening_cash_on_hand
               - cfg.opening_unremitted_trust)
    income = (Transaction.objects.confirmed_credits()
              .filter(excluded_from_income=False, date__lte=up_to_date)
              .aggregate(t=Sum("amount"))["t"] or Decimal(0))
    expenses = (Expense.objects.filter(
                    status__in=[Expense.Status.APPROVED, Expense.Status.PAID],
                    date__lte=up_to_date)
                .aggregate(t=Sum("amount"))["t"] or Decimal(0))
    return opening + income - expenses


def _recon_diagnostic(up_to_date):
    """Explain the cash-book balance and surface the things that most often make a
    post-import reconciliation refuse to balance: gifts received into the bank that
    are not (yet) in the book — still in the review queue, awaiting Sabbath
    confirmation, or unconfirmed — and bank money excluded as envelope detail."""
    from core.models import SiteConfig
    from cashbook.models import Expense
    cfg = SiteConfig.get()
    opening = (cfg.opening_bank_balance + cfg.opening_cash_on_hand
               - cfg.opening_unremitted_trust)
    base = Transaction.objects.filter(date__lte=up_to_date, is_reversal=False,
                                      is_reversed=False, direction=Transaction.Direction.CREDIT)
    income = (base.filter(confirmed=True, excluded_from_income=False)
              .aggregate(t=Sum("amount"))["t"] or Decimal(0))
    expenses = (Expense.objects.filter(
        status__in=[Expense.Status.APPROVED, Expense.Status.PAID],
        date__lte=up_to_date).aggregate(t=Sum("amount"))["t"] or Decimal(0))
    # money at the bank but NOT in the book balance
    unconfirmed = (base.filter(confirmed=False)
                   .aggregate(t=Sum("amount"))["t"] or Decimal(0))
    in_review = (base.filter(allocation_status=Transaction.Status.REVIEW)
                 .aggregate(t=Sum("amount"))["t"] or Decimal(0))
    # Sabbath-pending gifts that are NOT also off-book (unconfirmed / in review):
    # these are already in the cash book — only their Sabbath label is unsettled,
    # so they do NOT explain a reconciliation gap. Shown for awareness only.
    sab_pending = (base.filter(sabbath_confirm_pending=True, confirmed=True)
                   .exclude(allocation_status=Transaction.Status.REVIEW)
                   .aggregate(t=Sum("amount"))["t"] or Decimal(0))
    excluded = (base.filter(excluded_from_income=True)
                .aggregate(t=Sum("amount"))["t"] or Decimal(0))
    book = opening + income - expenses
    return {
        "opening": opening, "income": income, "expenses": expenses, "book": book,
        "unconfirmed": unconfirmed, "in_review": in_review,
        "sab_pending": sab_pending, "excluded": excluded,
        # only genuinely off-book money widens a reconciliation gap
        "off_book_total": unconfirmed + in_review,
    }


class ReconciliationListView(ReadAccessMixin, ListView):
    model = BankReconciliation
    template_name = "statements/reconciliation_list.html"
    context_object_name = "reconciliations"


class ReconciliationCreateView(DataEntryRequiredMixin, View):
    template_name = "statements/reconciliation_new.html"

    def get(self, request):
        return render(request, self.template_name, {"form": BankReconciliationForm()})

    def post(self, request):
        form = BankReconciliationForm(request.POST)
        if form.is_valid():
            rec = form.save(commit=False)
            rec.created_by = request.user
            if rec.book_balance is None:
                rec.book_balance = _ledger_bank_balance(rec.statement_date)
            rec.save()
            messages.success(request, "Reconciliation started — add your items below.")
            return redirect("reconciliation_detail", pk=rec.pk)
        return render(request, self.template_name, {"form": form})


class ReconciliationDetailView(ReadAccessMixin, View):
    template_name = "statements/reconciliation_detail.html"

    def get(self, request, pk):
        rec = get_object_or_404(BankReconciliation, pk=pk)
        return render(request, self.template_name, {
            "rec": rec, "items": rec.items.all(),
            "item_form": ReconciliationItemForm(),
            "suggested_book": _ledger_bank_balance(rec.statement_date),
            "diag": _recon_diagnostic(rec.statement_date),
            "default_effects": ReconciliationItem.DEFAULT_EFFECT,
        })

    def post(self, request, pk):
        rec = get_object_or_404(BankReconciliation, pk=pk)
        action = request.POST.get("action")
        if action == "add_item":
            form = ReconciliationItemForm(request.POST)
            if form.is_valid():
                item = form.save(commit=False)
                item.reconciliation = rec
                item.save()
                messages.success(request, "Item added.")
            else:
                messages.error(request, "Could not add item — check the amount.")
        elif action == "delete_item":
            ReconciliationItem.objects.filter(
                pk=request.POST.get("item_id"), reconciliation=rec).delete()
            messages.info(request, "Item removed.")
        elif action == "set_book":
            try:
                rec.book_balance = Decimal(request.POST.get("book_balance"))
                rec.save(update_fields=["book_balance"])
            except Exception:
                messages.error(request, "Enter a valid book balance.")
        return redirect("reconciliation_detail", pk=rec.pk)


# ===================== Automatic reconciliation =====================
from django.shortcuts import render, get_object_or_404
from django.views import View
from core.permissions import TreasurerRequiredMixin
from .models import ReconciliationMatch
from .services import reconcile as recon


class AutoReconcileRunView(TreasurerRequiredMixin, View):
    def post(self, request):
        summary = recon.run_auto_reconcile(request.user)
        if summary["auto"] or summary["review"]:
            messages.success(
                request, f"Auto-reconciliation: {summary['auto']} matched automatically, "
                         f"{summary['review']} need review.")
        else:
            messages.info(request, "No new matches found — everything is up to date.")
        return redirect("auto_reconcile")


class AutoReconcileView(ReadAccessMixin, View):
    template_name = "statements/auto_reconcile.html"

    def get(self, request):
        from giving.models import Transaction
        from cashbook.models import Expense
        open_matches = ReconciliationMatch.objects.filter(
            status__in=[ReconciliationMatch.Status.AUTO, ReconciliationMatch.Status.REVIEW]
        ).select_related("transaction", "expense", "expense__department")
        recent = ReconciliationMatch.objects.filter(
            status=ReconciliationMatch.Status.CONFIRMED).select_related(
            "transaction", "expense")[:15]
        # how many debits remain unmatched (no link, no open suggestion)
        linked = set(Expense.objects.filter(bank_transaction__isnull=False)
                     .values_list("bank_transaction_id", flat=True))
        suggested = set(ReconciliationMatch.objects.filter(
            status__in=[ReconciliationMatch.Status.AUTO, ReconciliationMatch.Status.REVIEW,
                        ReconciliationMatch.Status.CONFIRMED]).values_list("transaction_id", flat=True))
        unmatched = (Transaction.objects.filter(direction=Transaction.Direction.DEBIT,
                     is_reversal=False).exclude(id__in=linked | suggested).count())
        return render(request, self.template_name, {
            "matches": open_matches, "recent": recent, "unmatched": unmatched,
        })


class AutoReconcileConfirmView(TreasurerRequiredMixin, View):
    def post(self, request, pk):
        m = get_object_or_404(ReconciliationMatch, pk=pk)
        recon.confirm(m, request.user)
        messages.success(request, "Match confirmed and linked.")
        return redirect("auto_reconcile")


class AutoReconcileRejectView(TreasurerRequiredMixin, View):
    def post(self, request, pk):
        m = get_object_or_404(ReconciliationMatch, pk=pk)
        recon.reject(m)
        messages.success(request, "Match rejected.")
        return redirect("auto_reconcile")


# ---- Review & confirm auto-allocated imports (held when import confirmation is on) ----
class AutoAllocationReviewView(DataEntryRequiredMixin, View):
    template_name = "statements/auto_review.html"

    def get(self, request, pk):
        imp = get_object_or_404(StatementImport, pk=pk)
        rows = (Transaction.objects.filter(statement_import=imp, confirmed=False)
                .select_related("department", "dev_group").order_by("date", "id"))
        from departments.models import Department
        return render(request, self.template_name, {
            "imp": imp, "rows": rows,
            "funds": Department.objects.filter(active=True, selectable=True).order_by("name"),
            "count": rows.count()})

    def post(self, request, pk):
        imp = get_object_or_404(StatementImport, pk=pk)
        from departments.models import Department
        rows = list(Transaction.objects.filter(statement_import=imp, confirmed=False))
        only = set(request.POST.getlist("confirm"))     # ids ticked, if any
        confirm_all = request.POST.get("confirm_all")
        changed = 0
        for t in rows:
            new_dept = request.POST.get(f"dept_{t.id}")
            if new_dept and str(t.department_id) != new_dept:
                d = Department.objects.filter(pk=new_dept).first()
                if d:
                    t.department = d
                    t.allocation_status = Transaction.Status.MANUAL
            if confirm_all or str(t.id) in only:
                t.confirmed = True
                changed += 1
            t.save()
        # repost the ledger so confirmed rows now affect balances
        try:
            from ledger.services import posting
            if posting.chart_ready():
                posting.rebuild()
        except Exception:
            pass
        messages.success(request, f"Confirmed {changed} auto-allocated entr"
                                  f"{'y' if changed == 1 else 'ies'}. They now affect balances.")
        return redirect("statement_detail", pk=pk)


class AutoAllocationExcelView(DataEntryRequiredMixin, View):
    def get(self, request, pk):
        import io
        import openpyxl
        from django.http import HttpResponse
        imp = get_object_or_404(StatementImport, pk=pk)
        rows = (Transaction.objects.filter(statement_import=imp, confirmed=False)
                .select_related("department").order_by("date", "id"))
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Auto-allocated"
        ws.append(["Date", "Payer", "Phone", "Reference", "Allocated fund",
                   "Amount", "Status", "Bank receipt"])
        for t in rows:
            ws.append([t.date.strftime("%Y-%m-%d"), t.payer_name, t.payer_phone,
                       t.reference, t.department.name if t.department else "—",
                       float(t.amount), t.get_allocation_status_display(),
                       t.bank_receipt or t.core_ref or ""])
        for i, w in enumerate([12, 24, 14, 22, 24, 12, 12, 18], start=1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        resp = HttpResponse(buf.read(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resp["Content-Disposition"] = f'attachment; filename="auto-allocated-import-{pk}.xlsx"'
        return resp


class BankAccountListView(DataEntryRequiredMixin, View):
    template_name = "statements/bank_accounts.html"

    def get(self, request):
        from statements.models import BankAccount
        return render(request, self.template_name, {
            "accounts": BankAccount.objects.all(),
            "kinds": BankAccount.Kind.choices})

    def post(self, request):
        from statements.models import BankAccount
        name = (request.POST.get("name") or "").strip()
        if not name:
            messages.error(request, "Account name is required.")
            return redirect("bank_accounts")
        BankAccount.objects.create(
            name=name, bank_name=(request.POST.get("bank_name") or "").strip(),
            account_number=(request.POST.get("account_number") or "").strip(),
            kind=request.POST.get("kind") or BankAccount.Kind.CURRENT,
            is_default=bool(request.POST.get("is_default")))
        messages.success(request, f"Added bank account “{name}”.")
        return redirect("bank_accounts")


class BankFeedLogView(ReadAccessMixin, ListView):
    """Read-only log of real-time CBS events received from the bank, newest first
    — for monitoring and troubleshooting the live feed."""
    template_name = "statements/bank_feed_log.html"
    context_object_name = "events"
    paginate_by = 50

    def get_queryset(self):
        from statements.models import BankEvent
        return BankEvent.objects.select_related("transaction").all()

    def get_context_data(self, **kwargs):
        from statements.models import BankEvent
        ctx = super().get_context_data(**kwargs)
        ctx["counts"] = {s.label: BankEvent.objects.filter(status=s.value).count()
                         for s in BankEvent.Status}
        return ctx


class StatementPurgeView(TreasurerRequiredMixin, View):
    """Same-day undo of a statement import. Removes every transaction the import
    created (including split siblings, which carry the import link), the bank
    envelopes receipted from them, and members auto-created by this import that
    would be left with no giving — then marks the import PURGED. Refuses when any
    of the money has been linked onward (an expense tied to a debit) or falls in
    a locked period, so a purge can never strand half-reconciled data. The button
    disappears after the day of upload: by then counts and reports may rely on it."""

    def post(self, request, pk):
        import datetime as dt
        from django.utils import timezone
        from core.models import entry_blocked
        from giving.models import Transaction
        from envelopes.models import Envelope, EnvelopeLine
        from cashbook.models import Expense
        from members.models import Member
        imp = get_object_or_404(StatementImport, pk=pk)
        if imp.status == StatementImport.Status.PURGED:
            messages.info(request, "This import has already been purged.")
            return redirect("statement_list")
        if timezone.localtime(imp.uploaded_at).date() != timezone.localdate():
            messages.error(request, "A purge is only allowed on the day the statement "
                                    "was uploaded — after that, counts and reports may "
                                    "already rely on its entries.")
            return redirect("statement_list")

        txns = Transaction.objects.filter(statement_import=imp)
        n_txn = txns.count()

        # safety rails -------------------------------------------------------
        linked = Expense.objects.filter(bank_transaction__in=txns).count()
        if linked:
            messages.error(request, f"Cannot purge: {linked} expense(s) are linked to "
                                    "this statement's debits. Unlink them first.")
            return redirect("statement_list")
        for t in txns:
            why = entry_blocked(t.date)
            if why:
                messages.error(request, f"Cannot purge: {why}")
                return redirect("statement_list")

        # bank envelopes receipted from these gifts come out with them --------
        env_ids = set(Envelope.objects.filter(bank_transaction__in=txns)
                      .values_list("id", flat=True))
        env_ids |= set(EnvelopeLine.objects.filter(transaction__in=txns)
                       .values_list("envelope_id", flat=True))
        n_env = Envelope.objects.filter(id__in=env_ids).count()
        Envelope.objects.filter(id__in=env_ids).delete()   # lines cascade

        member_ids = set(txns.exclude(member__isnull=True)
                         .values_list("member_id", flat=True))
        txns.delete()   # envelope lines / recon matches / reversal records cascade

        # auto-created members left with no giving at all
        n_mem = 0
        for m in Member.objects.filter(id__in=member_ids, source="AUTO_BANK",
                                       created_at__gte=imp.uploaded_at):
            if not Transaction.objects.filter(member=m).exists():
                m.delete()
                n_mem += 1

        imp.status = StatementImport.Status.PURGED
        imp.error_detail = ((imp.error_detail or "") +
                            f"\nPurged by {request.user.username} at "
                            f"{timezone.now():%Y-%m-%d %H:%M}: removed {n_txn} "
                            f"transaction(s), {n_env} envelope(s), {n_mem} "
                            f"auto-created member(s).")[:4000]
        imp.save(update_fields=["status", "error_detail"])
        messages.success(request, f"Import purged — removed {n_txn} transaction(s), "
                                  f"{n_env} bank envelope(s)"
                                  + (f" and {n_mem} auto-created member(s)" if n_mem else "")
                                  + ". The file can be re-uploaded cleanly.")
        return redirect("statement_list")
