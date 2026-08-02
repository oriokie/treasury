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
from django.urls import reverse
from django.views import View
from django.views.generic import ListView

from core.permissions import ReadAccessMixin, DataEntryRequiredMixin
from giving.models import Transaction
from .models import BankReconciliation, ReconciliationItem
from .forms import BankReconciliationForm, ReconciliationItemForm


import contextlib


@contextlib.contextmanager
def reconciliation_basis(on):
    """Every figure on a reconciliation worksheet is read the same way: dated up
    to the statement date, valued as the books now stand.

    "As the books now stand" is the part that took three attempts to get right.
    A cash book is COMPLETED AFTER THE FACT — July's expenses are keyed in
    during August and belong in July — so rebuilding the balance from what the
    system happened to know on 31 July drops every one of them and leaves the
    cash-book figure too high by exactly that much. That is what 3.41.2 did, and
    why the reconciliation stopped agreeing with the dashboard.

    So the balances are never reconstructed. What IS asked of history is one
    much smaller question, in ``balances.receipted_after``: had this particular
    bank line been receipted yet on that date? That is a fact about one row's
    flags, it is the only thing the Sabbath route needs, and a row whose history
    does not reach back is left out rather than guessed at.

    The invariant the worksheet has to keep, whichever route receipted the money
    and whenever the sheet is prepared: the money is in the cash book or in
    suspense — never both, never neither.
    """
    yield on


def _ledger_bank_balance(up_to_date):
    """Cash-book balance per the books, as of a date — the same figure the
    Statement of Financial Position shows as "cash" for that date, computed
    the same way (reports.services.balances.department_summary), so it can
    never drift from the SOFP. This is the figure the bank statement
    reconciles *to* via cash-at-hand, unpresented cheques and
    unremitted-trust adjustments — NOT the raw bank-credit total."""
    from departments.models import current_cash_position
    return current_cash_position(up_to_date)


def _recon_diagnostic(up_to_date):
    """Explain the cash-book balance and surface the things that most often make a
    post-import reconciliation refuse to balance: contributions received into the bank that
    are not (yet) in the book — still in the review queue, awaiting Sabbath
    confirmation, or unconfirmed — and bank money excluded as envelope detail."""
    from departments.models import total_opening_cash_position, current_cash_position
    from reports.services.balances import (transfers_in_by_department,
                                           transfers_out_by_department)
    from cashbook.models import Expense
    opening = total_opening_cash_position()
    base = Transaction.objects.filter(date__lte=up_to_date, is_reversal=False,
                                      is_reversed=False, direction=Transaction.Direction.CREDIT)
    income = (base.filter(confirmed=True, excluded_from_income=False)
              .aggregate(t=Sum("amount"))["t"] or Decimal(0))
    expenses = (Expense.objects.filter(
        status__in=[Expense.Status.APPROVED, Expense.Status.PAID],
        date__lte=up_to_date).aggregate(t=Sum("amount"))["t"] or Decimal(0))
    # net fund transfers up to the date — zero-sum between two real funds, but
    # shown explicitly so opening + income - expenses + transfers ties exactly
    # to "book" below, the same way department_summary() accounts for them
    transfers = (sum(transfers_in_by_department(None, up_to_date).values(), Decimal(0))
                 - sum(transfers_out_by_department(None, up_to_date).values(), Decimal(0)))
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
    # the true, exact book balance — see current_cash_position()'s docstring
    # for why this (not a hand-rebuilt opening+income-expenses+transfers sum)
    # is the authoritative figure; the components above are shown for
    # transparency and should already tie to it via +transfers
    book = current_cash_position(up_to_date)
    return {
        "opening": opening, "income": income, "expenses": expenses,
        "transfers": transfers, "book": book,
        "unconfirmed": unconfirmed, "in_review": in_review,
        "sab_pending": sab_pending, "excluded": excluded,
        # only genuinely off-book money widens a reconciliation gap
        "off_book_total": unconfirmed + in_review,
    }


class ReconciliationListView(ReadAccessMixin, ListView):
    model = BankReconciliation
    template_name = "statements/reconciliation_list.html"
    context_object_name = "reconciliations"


def suggested_bank_balance(on):
    """What the bank said the account held as at `on`, for pre-filling a new
    reconciliation. Returns ``(balance_or_None, note)``.

    Typing the closing balance by hand is where a reconciliation goes wrong
    before it starts — a transposed digit here sends the treasurer hunting for
    a difference that was never in the books. The register already holds the
    bank's own running balance, so when it can answer the date being
    reconciled, it answers.

    It is a suggestion and not a fact: the field stays editable, and the note
    says which date the figure actually came from, because a balance three
    weeks stale is still the bank's last word but must not be presented as the
    closing position.
    """
    from statements.services import register as register_svc
    try:
        reg = register_svc.balance_asof(on)
        live = register_svc.live_balance_asof(on)
    except Exception:  # noqa: BLE001 — a suggestion must never break the form
        from core.utils import log_exception as _lx
        _lx("recon balance suggestion")
        return None, ""
    # Whichever of the two is nearer the date asked about, matching how the
    # bank position elsewhere chooses between them.
    options = [c for c in (reg, live) if c.get("balance") is not None
               and c.get("as_at") is not None]
    if not options:
        return None, (reg.get("reason") or live.get("reason") or "")
    best = max(options, key=lambda c: c["as_at"])
    note = f"From the bank register, as at {best['as_at']:%d %b %Y}"
    stale = best.get("stale_days") or 0
    if stale:
        note += f" — {stale} day(s) before the date you are reconciling"
    return best["balance"], note + "."


def start_reconciliation(*, statement_date, bank_balance, user,
                         book_balance=None, notes=""):
    """Create a reconciliation worksheet and populate its managed items.

    The single path — the view calls this and so should anything else, because
    the correctness of a worksheet is a property of ALL its figures being read
    from one moment, and that is a property of this function rather than of
    each caller remembering.
    """
    with reconciliation_basis(statement_date):
        if book_balance is None:
            book_balance = _ledger_bank_balance(statement_date)
        rec = BankReconciliation.objects.create(
            statement_date=statement_date, bank_balance=bank_balance,
            book_balance=book_balance, notes=notes, created_by=user)
    _sync_managed_recon_items(rec)
    return rec


class ReconciliationCreateView(DataEntryRequiredMixin, View):
    template_name = "statements/reconciliation_new.html"

    def get(self, request):
        import datetime as _dt
        # Default to the end of last month: a reconciliation is nearly always
        # being prepared for the month just closed, and the date drives the
        # balance suggested below.
        today = _dt.date.today()
        default_date = today.replace(day=1) - _dt.timedelta(days=1)
        raw = request.GET.get("statement_date")
        if raw:
            try:
                default_date = _dt.date.fromisoformat(raw)
            except ValueError:
                pass
        balance, note = suggested_bank_balance(default_date)
        form = BankReconciliationForm(initial={
            "statement_date": default_date,
            "bank_balance": balance,        # None leaves the field blank
        })
        return render(request, self.template_name, {
            "form": form, "balance_note": note,
            "balance_suggested": balance is not None,
            "balance_url": reverse("reconciliation_balance")})

    def post(self, request):
        form = BankReconciliationForm(request.POST)
        if form.is_valid():
            draft = form.save(commit=False)
            rec = start_reconciliation(
                statement_date=draft.statement_date,
                bank_balance=draft.bank_balance,
                book_balance=draft.book_balance,
                notes=draft.notes, user=request.user)
            messages.success(request, "Reconciliation started — add your items below.")
            return redirect("reconciliation_detail", pk=rec.pk)
        return render(request, self.template_name, {"form": form})


class ReconciliationBalanceView(DataEntryRequiredMixin, View):
    """AJAX: what the bank says the account held as at a date.

    The suggested balance depends on the date being reconciled, so changing the
    date on the form has to change the suggestion with it — otherwise the
    convenience becomes a trap, silently offering last month's closing balance
    for this month's worksheet.
    """
    def get(self, request):
        import datetime as _dt
        from django.http import JsonResponse
        try:
            on = _dt.date.fromisoformat(request.GET.get("date", ""))
        except ValueError:
            return JsonResponse({"ok": False, "note": ""})
        balance, note = suggested_bank_balance(on)
        return JsonResponse({
            "ok": balance is not None,
            "balance": str(balance) if balance is not None else "",
            "note": note,
        })


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
    """Keep the auto-managed reconciling items in step with their current values:
    the petty-cash float, outstanding bank-funded staff advances (both cash that
    has left the bank but the cash book still holds — they ADD back), and
    unpresented cheques (issued but not yet cleared — they SUBTRACT from the bank
    balance). Each is upserted when non-zero and removed when zero, so the
    treasurer never has to add them by hand."""
    with reconciliation_basis(rec.statement_date):
        _sync_managed_recon_items_inner(rec)


def _sync_managed_recon_items_inner(rec):
    from decimal import Decimal
    from cashbook.views import (_petty_balance_asof, outstanding_bank_advances_total,
                                outstanding_petty_advances_total,
                                unpresented_cheques_total)
    ADD = ReconciliationItem.Effect.ADD
    SUB = ReconciliationItem.Effect.SUBTRACT

    def _safe(fn):
        try:
            return fn(rec.statement_date) or Decimal(0)
        except Exception:  # noqa: BLE001 — one bad computation must not block the rest
            from core.utils import log_exception as _lx
            _lx("recon managed sync")
            return Decimal(0)

    def _pending_at_the_date():
        """Bank credits that had arrived but are not yet in any fund.

        They are in the bank balance and not in the cash book (which is the sum
        of the fund balances), so they are a reconciling item — without them a
        month with anything sitting in the review queue could not balance at
        all.

        **Read on the same basis as the book balance**, which is the ordinary
        one: what the books say now. That coupling is the whole point, and
        getting it wrong is not a rounding error. This was briefly computed on
        the as-reported basis while the book balance stayed on the current one,
        so tithe banked on 31 July and receipted on 1 August was counted twice
        for a 31 July worksheet — once in the fund it had by then been given to,
        and again in suspense — and the reconciliation was out by exactly that
        amount.

        Reading both sides from the same moment also makes the worksheet
        self-correcting: while the credit is unallocated it sits in suspense and
        the books do not have it; once it is receipted the books have it and
        suspense drops away. Either way the two sides add to the same money, so
        a reconciliation for 31 July balances whether it is prepared on the day,
        the next morning, or in November.
        """
        from reports.services import balances
        try:
            return balances.pending_receipts_total(rec.statement_date) \
                or Decimal(0)
        except Exception:  # noqa: BLE001
            from core.utils import log_exception as _lx
            _lx("recon pending receipts")
            return Decimal(0)

    # Read every managed figure from the worksheet's own moment — see
    # reconciliation_basis for why all of them and not some.
    managed = [
        ("Petty cash float (cash on hand)",
         _safe(_petty_balance_asof),
         ReconciliationItem.Kind.CASH_AT_HAND, ADD),
        ("Receipts pending allocation (banked, not yet in a fund)",
         _pending_at_the_date(),
         ReconciliationItem.Kind.OTHER, SUB),
        ("Staff advances issued (not yet accounted)",
         _safe(outstanding_bank_advances_total),
         ReconciliationItem.Kind.CASH_AT_HAND, ADD),
        ("Staff advances from petty cash (not yet accounted)",
         _safe(outstanding_petty_advances_total),
         ReconciliationItem.Kind.CASH_AT_HAND, ADD),
        ("Unpresented cheques (not yet cleared)",
         _safe(unpresented_cheques_total),
         ReconciliationItem.Kind.UNPRESENTED, SUB),
    ]
    for desc, amount, kind, effect in managed:
        existing = rec.items.filter(description=desc).first()
        if amount and amount > 0:
            if existing:
                if existing.amount != amount or existing.effect != effect:
                    existing.amount = amount
                    existing.effect = effect
                    existing.save(update_fields=["amount", "effect"])
            else:
                ReconciliationItem.objects.create(
                    reconciliation=rec, kind=kind, description=desc,
                    amount=amount, effect=effect, auto=True)
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
        from cashbook.models import PaymentInstrument
        from cashbook.views import (unpresented_cheques_total, _petty_balance_asof,
                                    outstanding_bank_advances_total)
        # auto-populate the petty-cash float and bank-funded staff advances as
        # reconciling items, kept in step with their current values
        from core import roles
        if roles.can_enter_data(request.user):
            _sync_managed_recon_items(rec)
        if request.GET.get("export") in ("xlsx", "csv"):
            return self._export(request, rec)
        # as-at listing: judged on issue/cleared DATES so an instrument that
        # cleared after this reconciliation's date still shows outstanding here
        from cashbook.views import unpresented_payments_qs
        unpresented = (unpresented_payments_qs(rec.statement_date)
                       .select_related("expense__department")
                       .order_by("date_issued"))
        petty_float = _petty_balance_asof(rec.statement_date)
        # is the petty-cash float already entered as a reconciling item?
        petty_listed = rec.items.filter(
            description__icontains="petty cash").exists()
        bank_advances = outstanding_bank_advances_total(rec.statement_date)
        advances_listed = rec.items.filter(
            description__icontains="staff advance").exists()
        with reconciliation_basis(rec.statement_date):
            _suggested_book = _ledger_bank_balance(rec.statement_date)
            _diag = _recon_diagnostic(rec.statement_date)
        return render(request, self.template_name, {
            "rec": rec, "items": rec.items.all(),
            "item_form": ReconciliationItemForm(),
            "suggested_book": _suggested_book,
            "diag": _diag,
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
            with reconciliation_basis(rec.statement_date):
                rec.book_balance = _ledger_bank_balance(rec.statement_date)
            rec.save(update_fields=["book_balance"])
            messages.success(request,
                f"Cash-book balance recomputed from the ledger: "
                f"KSh {rec.book_balance:,.2f}.")
        _maybe_auto_lock_on_reconciled(rec, request)
        return redirect("reconciliation_detail", pk=rec.pk)


def _maybe_auto_lock_on_reconciled(rec, request):
    """When SiteConfig.auto_lock_on_reconciliation is on and this worksheet now
    balances, lock its accounting month so a later edit can't silently
    invalidate the completed reconciliation. Never blocks the reconciliation
    action itself if locking fails for any reason."""
    try:
        from core.models import SiteConfig, PeriodLock
        if not rec.is_reconciled or not SiteConfig.get().auto_lock_on_reconciliation:
            return
        d = rec.statement_date
        if not PeriodLock.objects.filter(year=d.year, month=d.month).exists():
            PeriodLock.objects.create(year=d.year, month=d.month, locked_by=request.user,
                note=f"Auto-locked: bank reconciliation {rec.statement_date} balanced.")
            messages.info(request,
                f"{d:%B %Y} has been automatically locked because this reconciliation "
                "now balances (Settings → auto-lock on reconciliation).")
    except Exception:  # noqa: BLE001 — must never block the reconciliation itself
        from core.utils import log_exception as _lx; _lx("auto-lock on reconciliation")

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
