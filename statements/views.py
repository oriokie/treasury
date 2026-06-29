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
        force_sabbath = form.cleaned_data.get("service_sabbath")
        imp = StatementImport.objects.create(
            uploaded_by=self.request.user, filename=f.name, file=f,
            bank_account=bank_account)
        # synchronous import (swap for a Celery task for very large files)
        imp.file.seek(0)
        content = imp.file.read()
        run_import(imp, content, f.name, bank_account=bank_account,
                   force_sabbath=force_sabbath)
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
    post-import reconciliation refuse to balance: contributions received into the bank that
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


class ReconciliationDeleteView(TreasurerRequiredMixin, View):
    """Remove a reconciliation worksheet (and its items) within a week of
    creation. Reconciliations don't post to the ledger, so deletion is safe."""
    def post(self, request, pk):
        rec = get_object_or_404(BankReconciliation, pk=pk)
        if not rec.can_delete:
            messages.error(request, "This reconciliation is more than a week old "
                                    "and can no longer be deleted.")
            return redirect("reconciliation_detail", pk=rec.pk)
        when = rec.statement_date
        rec.delete()
        messages.success(request, f"Deleted the reconciliation for {when}.")
        return redirect("reconciliation_list")


def _sync_managed_recon_items(rec):
    """Keep the auto-managed reconciling items (petty-cash float and outstanding
    bank-funded staff advances) in step with their current values: upsert when
    non-zero, remove when zero. Both are cash that has left the bank but the cash
    book still holds, so they ADD back to the bank balance to reach the book."""
    from decimal import Decimal
    from cashbook.views import _petty_balance_asof, outstanding_bank_advances_total
    managed = [
        ("Petty cash float (cash on hand)", _petty_balance_asof(rec.statement_date),
         ReconciliationItem.Kind.CASH_AT_HAND),
        ("Staff advances issued (not yet accounted)",
         outstanding_bank_advances_total(rec.statement_date),
         ReconciliationItem.Kind.CASH_AT_HAND),
    ]
    for desc, amount, kind in managed:
        existing = rec.items.filter(description=desc).first()
        if amount and amount > 0:
            if existing:
                if existing.amount != amount or existing.effect != ReconciliationItem.Effect.ADD:
                    existing.amount = amount
                    existing.effect = ReconciliationItem.Effect.ADD
                    existing.save(update_fields=["amount", "effect"])
            else:
                ReconciliationItem.objects.create(
                    reconciliation=rec, kind=kind, description=desc,
                    amount=amount, effect=ReconciliationItem.Effect.ADD,
                    auto=True)
        elif existing and getattr(existing, "auto", False):
            existing.delete()


class ReconciliationDetailView(ReadAccessMixin, View):
    template_name = "statements/reconciliation_detail.html"

    def _export(self, request, rec):
        from reports.exports import csv_response, xlsx_response
        from core.models import SiteConfig
        from statements.models import ReconciliationItem
        adds = rec.items.filter(effect=ReconciliationItem.Effect.ADD)
        subs = rec.items.filter(effect=ReconciliationItem.Effect.SUBTRACT)
        header = ["Line", "Detail", "Amount"]
        rows = [["Balance per bank statement", "", float(rec.bank_balance or 0)]]
        for it in adds:
            rows.append(["Add", it.get_kind_display() + (f" — {it.description}"
                         if it.description else ""), float(it.amount)])
        for it in subs:
            rows.append(["Less", it.get_kind_display() + (f" — {it.description}"
                         if it.description else ""), -float(it.amount)])
        rows.append(["Adjusted bank balance", "", float(rec.adjusted_balance or 0)])
        rows.append(["Balance per cash book", "", float(rec.book_balance or 0)])
        rows.append(["Difference", "", float(rec.difference or 0)])
        fname = f"bank-reconciliation-{rec.statement_date}"
        if request.GET["export"] == "csv":
            return csv_response(fname + ".csv", header, rows)
        return xlsx_response(fname + ".xlsx", header, rows,
            title=f"Bank Reconciliation — {rec.statement_date:%d %b %Y}",
            church=SiteConfig.get().church_name)

    def get(self, request, pk):
        rec = get_object_or_404(BankReconciliation, pk=pk)
        from cashbook.models import ChequeRegister
        from cashbook.views import (unpresented_cheques_total, _petty_balance_asof,
                                    outstanding_bank_advances_total)
        # auto-populate the petty-cash float and bank-funded staff advances as
        # reconciling items, kept in step with their current values
        from core import roles
        if roles.can_enter_data(request.user):
            _sync_managed_recon_items(rec)
        if request.GET.get("export") in ("xlsx", "csv"):
            return self._export(request, rec)
        unpresented = (ChequeRegister.objects.filter(
            status=ChequeRegister.Status.ISSUED,
            date_issued__lte=rec.statement_date).order_by("date_issued"))
        petty_float = _petty_balance_asof(rec.statement_date)
        # is the petty-cash float already entered as a reconciling item?
        petty_listed = rec.items.filter(
            description__icontains="petty cash").exists()
        bank_advances = outstanding_bank_advances_total(rec.statement_date)
        advances_listed = rec.items.filter(
            description__icontains="staff advance").exists()
        return render(request, self.template_name, {
            "rec": rec, "items": rec.items.all(),
            "item_form": ReconciliationItemForm(),
            "suggested_book": _ledger_bank_balance(rec.statement_date),
            "diag": _recon_diagnostic(rec.statement_date),
            "default_effects": ReconciliationItem.DEFAULT_EFFECT,
            "unpresented_cheques": unpresented,
            "unpresented_total": unpresented_cheques_total(rec.statement_date),
            "petty_float": petty_float, "petty_listed": petty_listed,
            "bank_advances": bank_advances, "advances_listed": advances_listed,
            "additions": rec.items.filter(effect=ReconciliationItem.Effect.ADD),
            "subtractions": rec.items.filter(effect=ReconciliationItem.Effect.SUBTRACT),
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
                messages.success(request, "Cash-book balance updated.")
            except Exception:
                from core.utils import log_exception as _lx; _lx('statements/views.py')
                messages.error(request, "Enter a valid book balance.")
        elif action == "recompute_book":
            # refresh the stored cash-book balance from the current ledger, as of
            # this reconciliation's statement date (older worksheets can go stale
            # after later edits/imports — this pulls the up-to-date figure).
            rec.book_balance = _ledger_bank_balance(rec.statement_date)
            rec.save(update_fields=["book_balance"])
            messages.success(request,
                f"Cash-book balance recomputed from the ledger: "
                f"KSh {rec.book_balance:,.2f}.")
        elif action == "add_petty_cash":
            from cashbook.views import _petty_balance_asof
            amt = _petty_balance_asof(rec.statement_date)
            if amt and amt != 0 and not rec.items.filter(
                    description__icontains="petty cash").exists():
                ReconciliationItem.objects.create(
                    reconciliation=rec, kind=ReconciliationItem.Kind.CASH_AT_HAND,
                    description="Petty cash float (cash on hand)",
                    amount=abs(amt),
                    effect=(ReconciliationItem.Effect.ADD if amt > 0
                            else ReconciliationItem.Effect.SUBTRACT))
                messages.success(request, "Petty cash float added as a reconciling item.")
            else:
                messages.info(request, "Petty cash float is already listed or is zero.")
            return redirect("reconciliation_detail", pk=rec.pk)
        elif action == "add_advances":
            from cashbook.views import outstanding_bank_advances_total
            amt = outstanding_bank_advances_total(rec.statement_date)
            if amt and amt > 0 and not rec.items.filter(
                    description__icontains="staff advance").exists():
                ReconciliationItem.objects.create(
                    reconciliation=rec, kind=ReconciliationItem.Kind.CASH_AT_HAND,
                    description="Staff advances issued (not yet accounted)",
                    amount=amt, effect=ReconciliationItem.Effect.ADD)
                messages.success(request, "Outstanding staff advances added as a reconciling item.")
            else:
                messages.info(request, "No bank-funded staff advances to add (or already listed).")
            return redirect("reconciliation_detail", pk=rec.pk)
        elif action == "add_unpresented_cheques":
            from cashbook.models import ChequeRegister
            existing = " ".join(rec.items.values_list("description", flat=True))
            added = 0
            for chq in ChequeRegister.objects.filter(
                    status=ChequeRegister.Status.ISSUED,
                    date_issued__lte=rec.statement_date):
                if chq.cheque_number and chq.cheque_number in existing:
                    continue
                ReconciliationItem.objects.create(
                    reconciliation=rec, kind=ReconciliationItem.Kind.UNPRESENTED,
                    description=f"Cheque {chq.cheque_number}"
                                + (f" — {chq.payee}" if chq.payee else ""),
                    amount=chq.amount, effect=ReconciliationItem.Effect.SUBTRACT)
                added += 1
            if added:
                messages.success(request, f"Added {added} unpresented cheque(s) from the register.")
            else:
                messages.info(request, "No new unpresented cheques to add — they're already listed "
                                       "or all cheques have cleared.")
        return redirect("reconciliation_detail", pk=rec.pk)
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
        from departments.models import Department, split_component_dept_ids
        from django.db.models import Q
        comp_ids = set(split_component_dept_ids())
        used = [t.department_id for t in rows if t.department_id]
        funds = (Department.objects.filter(active=True)
                 .filter(Q(selectable=True) | Q(pk__in=used))
                 .order_by("name"))
        return render(request, self.template_name, {
            "imp": imp, "rows": rows, "funds": funds,
            "comp_ids": comp_ids, "count": rows.count()})

    def post(self, request, pk):
        imp = get_object_or_404(StatementImport, pk=pk)
        from departments.models import Department, split_component_dept_ids
        comp_ids = set(split_component_dept_ids())
        rows = list(Transaction.objects.filter(statement_import=imp, confirmed=False))
        only = set(request.POST.getlist("confirm"))     # ids ticked, if any
        confirm_all = request.POST.get("confirm_all")
        changed = 0
        for t in rows:
            new_dept = request.POST.get(f"dept_{t.id}")
            # never re-point a split component from this screen — it's part of a
            # configured split and must stay put.
            if (new_dept and str(t.department_id) != new_dept
                    and t.department_id not in comp_ids):
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
            from core.utils import log_exception as _lx; _lx('statements/views.py')
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
        import json
        from statements.models import BankEvent
        ctx = super().get_context_data(**kwargs)
        ctx["counts"] = {s.label: BankEvent.objects.filter(status=s.value).count()
                         for s in BankEvent.Status}

        def _find(obj, key):
            """Case-insensitive search for a key anywhere in nested JSON."""
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k.lower() == key.lower() and v not in (None, "", []):
                        return v
                    found = _find(v, key)
                    if found not in (None, ""):
                        return found
            elif isinstance(obj, list):
                for it in obj:
                    found = _find(it, key)
                    if found not in (None, ""):
                        return found
            return None

        # most recent cleared bank balance reported by the feed
        cleared = None
        for e in BankEvent.objects.order_by("-received_at")[:100]:
            if not e.payload:
                continue
            try:
                data = json.loads(e.payload)
            except (ValueError, TypeError):
                continue
            cb = _find(data, "ClearedBalance")
            if cb not in (None, ""):
                cleared = {"balance": cb, "at": e.received_at,
                           "account": e.acct_no or _find(data, "AccountNo") or "",
                           "currency": e.currency or _find(data, "Currency") or ""}
                break
        ctx["cleared"] = cleared

        # pretty-print each event's raw payload for the on-row JSON view
        for e in ctx.get("events", []):
            try:
                e.pretty = json.dumps(json.loads(e.payload), indent=2) if e.payload else ""
            except (ValueError, TypeError):
                e.pretty = e.payload or ""
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
        if not imp.can_purge:
            messages.error(request, "A purge is only allowed within a week of upload "
                                    "— after that, counts and reports may already rely "
                                    "on its entries.")
            return redirect("statement_list")

        txns = Transaction.objects.filter(statement_import=imp)
        n_txn = txns.count()

        # safety rails -------------------------------------------------------
        linked_qs = Expense.objects.filter(bank_transaction__in=txns)
        linked = linked_qs.count()
        if linked and not request.POST.get("unlink_expenses"):
            # offer to auto-unlink rather than just refusing. Unlinking only
            # clears the reconciliation link (bank_transaction → NULL); the
            # expenses themselves are kept — they remain recorded expenses, just
            # no longer matched to a (soon-to-be-removed) bank debit.
            messages.error(request,
                f"Cannot purge yet: {linked} expense(s) are linked to this "
                f"statement's debits. Re-submit with 'unlink and purge' to clear "
                f"those reconciliation links and proceed (the expenses are kept).")
            return redirect("statement_list")
        n_unlinked = 0
        if linked:
            n_unlinked = linked_qs.update(bank_transaction=None)
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
                                  + (f"; unlinked {n_unlinked} expense(s)" if n_unlinked else "")
                                  + ". The file can be re-uploaded cleanly.")
        return redirect("statement_list")
