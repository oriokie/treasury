import datetime as dt

from django.conf import settings
from django.contrib import messages
from django.db import transaction as db_tx
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views.generic import ListView, CreateView, UpdateView, View

from django.views import View

from core.utils import block_if_locked as _block_if_locked, PrefPaginationMixin
from core.permissions import DataEntryRequiredMixin, ReadAccessMixin, TreasurerRequiredMixin, AdvanceAccessMixin
from core.utils import sabbath_week_of
from departments.models import Department
from .forms import ExpenseForm, FundTransferForm, RecurringExpenseForm
from .models import Expense



def _fund_available(dept_id, as_of):
    """A fund's available (closing) balance as at a date, or None if unknown.
    Targeted single-fund aggregation (no full-portfolio loop)."""
    if not dept_id:
        return None
    from reports.services.balances import fund_balance
    return fund_balance(dept_id, as_of)


class ExpenseListView(PrefPaginationMixin, ReadAccessMixin, ListView):
    model = Expense
    template_name = "cashbook/list.html"
    context_object_name = "expenses"
    paginate_by = 50

    def get_queryset(self):
        import datetime as dt
        qs = Expense.objects.select_related("department", "recorded_by").order_by("-id")
        from django.db.models import Count
        qs = qs.annotate(n_attachments=Count("attachments"))
        g = self.request.GET
        if g.get("status"):
            qs = qs.filter(status=g["status"])
        if g.get("department"):
            qs = qs.filter(department_id=g["department"])
        if g.get("category"):
            qs = qs.filter(category=g["category"])
        if g.get("q"):
            from django.db.models import Q
            term = g["q"].strip()
            qs = qs.filter(Q(description__icontains=term)
                           | Q(claimant__icontains=term)
                           | Q(voucher_no__icontains=term))
        for key, lookup in (("start", "date__gte"), ("end", "date__lte")):
            if g.get(key):
                try:
                    qs = qs.filter(**{lookup: dt.date.fromisoformat(g[key])})
                except ValueError:
                    pass
        return qs

    def get(self, request, *args, **kwargs):
        export = request.GET.get("export")
        if export in ("csv", "xlsx"):
            from reports.exports import csv_response, xlsx_response
            from core.models import SiteConfig
            qs = self.get_queryset()
            header = ["ID", "Date", "Description", "Fund", "Category", "Type", "Status",
                      "Claimant", "Voucher", "Amount"]
            rows = [[x.id, x.date.isoformat(), x.description,
                     x.department.name if x.department_id else "",
                     x.get_category_display(), x.get_expenditure_type_display(),
                     x.get_status_display(), x.claimant or "", x.voucher_no or "",
                     float(x.amount)] for x in qs]
            if export == "xlsx":
                return xlsx_response("expenses.xlsx", header, rows, title="Expenses",
                                     church=SiteConfig.get().church_name)
            return csv_response("expenses.csv", header, rows)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        from django.db.models import Sum, Count
        ctx = super().get_context_data(**kwargs)
        ctx["statuses"] = Expense.Status.choices
        ctx["exp_types"] = Expense.ExpenditureType.choices
        ctx["categories"] = Expense.Category.choices
        ctx["departments"] = Department.objects.filter(active=True)
        ctx["filters"] = self.request.GET
        ctx["filtered_total"] = self.get_queryset().aggregate(t=Sum("amount"))["t"] or 0
        # status money-bar: totals within the other active filters, ignoring the
        # status filter itself, so the chips always show the whole picture
        g = self.request.GET.copy()
        g.pop("status", None)
        base = Expense.objects.all()
        if g.get("department"):
            base = base.filter(department_id=g["department"])
        if g.get("category"):
            base = base.filter(category=g["category"])
        if g.get("q"):
            from django.db.models import Q
            term = g["q"].strip()
            base = base.filter(Q(description__icontains=term)
                               | Q(claimant__icontains=term)
                               | Q(voucher_no__icontains=term))
        import datetime as _dt
        for key, lookup in (("start", "date__gte"), ("end", "date__lte")):
            if g.get(key):
                try:
                    base = base.filter(**{lookup: _dt.date.fromisoformat(g[key])})
                except ValueError:
                    pass
        agg = {r["status"]: r for r in base.values("status")
               .annotate(n=Count("id"), t=Sum("amount"))}
        ctx["status_bar"] = [
            {"code": code, "label": label,
             "n": agg.get(code, {}).get("n", 0),
             "t": agg.get(code, {}).get("t", 0) or 0,
             "active": self.request.GET.get("status") == code}
            for code, label in Expense.Status.choices]
        ctx["status_qs"] = g.urlencode()
        # split the filtered total into operating vs trust-remittance, so the
        # page can show a true operating-expense figure (remittances are a
        # liability settlement, not expenditure)
        from django.db.models import Q as _Q
        qs = self.get_queryset()
        remit_total = (qs.filter(category=Expense.Category.REMITTANCE)
                       .aggregate(t=Sum("amount"))["t"] or 0)
        ctx["filtered_remittances"] = remit_total
        ctx["filtered_operating"] = (ctx["filtered_total"] or 0) - remit_total
        from core.models import SiteConfig
        ctx["dual_threshold"] = SiteConfig.get().dual_approval_threshold or 0
        return ctx


class ExpenseCreate(DataEntryRequiredMixin, CreateView):
    model = Expense
    form_class = ExpenseForm
    template_name = "cashbook/form.html"
    success_url = reverse_lazy("expense_list")

    def _settle_target(self):
        """Resolve a ?settle=payable:5 / accrual:3 marker to the obligation being
        paid, so settling opens this form pre-filled and editable (method,
        claimant, charges) instead of silently posting a fixed expense."""
        from .models import Payable, Accrual
        raw = self.request.GET.get("settle") or self.request.POST.get("settle") or ""
        kind, _, sid = raw.partition(":")
        if not sid.isdigit():
            return None, None
        if kind == "payable":
            return "payable", Payable.objects.filter(pk=sid, settled=False).first()
        if kind == "accrual":
            return "accrual", Accrual.objects.filter(pk=sid, settled=False).first()
        return None, None

    def get_initial(self):
        initial = super().get_initial()
        kind, obj = self._settle_target()
        if obj:
            initial.update({
                "date": dt.date.today(), "department": obj.department_id,
                "description": getattr(obj, "description", "")[:200],
                "amount": obj.amount, "category": obj.category,
                "method": Expense.Method.BANK})
        return initial

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        kind, obj = self._settle_target()
        if obj:
            ctx["settle"] = f"{kind}:{obj.pk}"
            ctx["settle_label"] = (f"Settling {kind}: {getattr(obj, 'vendor', '') or ''} "
                                   f"{obj.description}".strip())
        from core.models import SiteConfig
        from core.roles import is_treasurer
        from statements.models import BankAccount
        from .models import PaymentInstrument
        ctx["bank_accounts"] = BankAccount.objects.all()
        ctx["payment_methods"] = PaymentInstrument.Method.choices
        # issuing a payment at entry only makes sense once the expense is approved;
        # when approval is required, this expense will start PENDING, so offer it
        # to a treasurer only (who can approve immediately after) — otherwise hide it.
        cfg = SiteConfig.get()
        ctx["can_issue_payment_now"] = (not cfg.require_expense_approval) or is_treasurer(self.request.user)
        ctx["require_expense_approval"] = cfg.require_expense_approval
        return ctx

    def form_valid(self, form):
        from core.models import SiteConfig
        from core.roles import is_treasurer
        exp = form.save(commit=False)
        if _block_if_locked(self.request, exp.date):
            return redirect("expense_create")
        # H4: block an expense that would overdraw the fund. Treasurers may override
        # with an explicit, logged confirmation; assistants are always blocked.
        cfg = SiteConfig.get()
        if cfg.enforce_fund_balance:
            available = _fund_available(exp.department_id, exp.date)
            if available is not None and exp.amount > available:
                override = is_treasurer(self.request.user) and \
                    self.request.POST.get("override_balance")
                if not override:
                    messages.error(self.request,
                        f"{exp.department.name} has {available:,.2f} available — not enough for "
                        f"this expense of {exp.amount:,.2f}." +
                        (" Tick \u201coverride\u201d to record it anyway." if is_treasurer(self.request.user)
                         else " A treasurer must record an over-budget expense."))
                    ctx = self.get_context_data(form=form)
                    ctx["overspend_warning"] = is_treasurer(self.request.user)
                    return self.render_to_response(ctx)
                messages.warning(self.request,
                    f"Override: {exp.department.name} taken below zero by "
                    f"{exp.recorded_by if False else self.request.user.username}.")
        exp.recorded_by = self.request.user
        exp.sabbath_week = sabbath_week_of(exp.date)
        issue_now = bool(self.request.POST.get("issue_payment"))
        auto = not SiteConfig.get().require_expense_approval
        # a treasurer issuing a payment at entry is implicitly approving it too —
        # matches the existing detail-page pattern where "Mark paid" also approves.
        if auto or (issue_now and is_treasurer(self.request.user)):
            exp.status = Expense.Status.APPROVED
            exp.approved_by = self.request.user
        exp.save()
        # optional M-Pesa / bank transaction charge -> separate bank-charge expense
        charge = form.cleaned_data.get("charge")
        if charge and charge > 0:
            ref = exp.voucher_no or f"exp #{exp.id}"
            Expense.objects.create(
                date=exp.date, sabbath_week=exp.sabbath_week, department=exp.department,
                description=f"Transaction charge — {exp.description} [for {ref}]"[:200],
                amount=charge, category=Expense.Category.BANK_CHARGE,
                method=exp.method, recorded_by=self.request.user,
                voucher_no=exp.voucher_no, paid_from_petty_cash=exp.paid_from_petty_cash,
                status=exp.status, charge_for=exp,
                approved_by=self.request.user if auto else None)
        payment_note = ""
        if issue_now and exp.status == Expense.Status.APPROVED:
            from .models import PaymentInstrument
            method = self.request.POST.get("payment_method") or "CHEQUE"
            if method not in dict(PaymentInstrument.Method.choices):
                method = "CHEQUE"
            ref = (self.request.POST.get("payment_reference") or "").strip()[:40]
            try:
                issued = (dt.date.fromisoformat(self.request.POST.get("payment_date"))
                          if self.request.POST.get("payment_date") else exp.date)
            except ValueError:
                issued = exp.date
            bank_id = self.request.POST.get("payment_bank_account") or ""
            inst = PaymentInstrument(
                method=method, instrument_number=ref,
                payee=(exp.claimant or exp.department.name)[:160],
                amount=exp.amount, date_issued=issued,
                status=PaymentInstrument.Status.ISSUED,
                source_kind=PaymentInstrument.SourceKind.EXPENSE,
                expense=exp, recorded_by=self.request.user)
            if bank_id.isdigit():
                from statements.models import BankAccount
                inst.bank_account = BankAccount.objects.filter(pk=bank_id).first()
            inst.save()
            payment_note = (f" {inst.get_method_display()} "
                            f"{ref or '(no reference)'} issued to settle it.")
        if exp.status == Expense.Status.PENDING:
            from core.services.notifications import notify
            from django.urls import reverse
            notify("APPROVAL",
                   f"Expense awaiting approval: {exp.description} — "
                   f"{exp.amount:,.2f} ({exp.department.name})",
                   link=reverse("expense_list"))
        # if this expense settles a payable/accrual, link and close it
        kind, obj = self._settle_target()
        if obj and not obj.settled:
            obj.settled = True
            obj.settled_on = exp.date
            obj.settled_expense = exp
            obj.save()
            messages.success(self.request,
                f"{kind.title()} settled and recorded as an expense.")
            return redirect("accruals")
        messages.success(self.request, f"Expense recorded.{payment_note}")
        return redirect(self.success_url)


class ExpenseUpdate(DataEntryRequiredMixin, UpdateView):
    model = Expense
    form_class = ExpenseForm
    template_name = "cashbook/form.html"
    success_url = reverse_lazy("expense_list")

    def get_initial(self):
        initial = super().get_initial()
        charge = self.object.charges.first() if self.object else None
        if charge:
            initial["charge"] = charge.amount
        return initial

    def form_valid(self, form):
        from core.roles import is_treasurer
        exp = form.save(commit=False)
        # #1 — respect period locks on edits, just like creation
        if _block_if_locked(self.request, exp.date):
            return redirect(self.success_url)
        original = Expense.objects.get(pk=exp.pk) if exp.pk else None
        # also block if the expense is being moved OUT of a locked period
        if original and _block_if_locked(self.request, original.date):
            return redirect(self.success_url)
        if original and original.status != Expense.Status.PENDING:
            # #2a — only a treasurer may edit an expense that is past PENDING
            if not is_treasurer(self.request.user):
                messages.error(self.request,
                    "This expense has been approved/paid — only a treasurer can edit "
                    "it. Ask a treasurer, or it must be reversed first.")
                return redirect(self.success_url)
            # #2b — a material change (amount or fund) voids the prior approval
            if (original.amount != exp.amount
                    or original.department_id != exp.department_id):
                exp.status = Expense.Status.PENDING
                exp.approved_by = None
                exp.second_approved_by = None
                exp.paid_date = None
                messages.warning(self.request,
                    "Amount or fund changed — the expense was returned to pending "
                    "approval and must be re-approved.")
        exp.sabbath_week = sabbath_week_of(exp.date)
        exp.save()
        # Sync the linked transaction-charge entry (M-Pesa/bank charge). Editing an
        # expense must create the charge if newly added, update it in place if it
        # already exists (never duplicate), and remove it if the charge is cleared.
        charge = form.cleaned_data.get("charge")
        existing = exp.charges.first()
        if charge and charge > 0:
            ref = exp.voucher_no or f"exp #{exp.id}"
            desc = f"Transaction charge — {exp.description} [for {ref}]"[:200]
            if existing:
                existing.date = exp.date
                existing.sabbath_week = exp.sabbath_week
                existing.department = exp.department
                existing.description = desc
                existing.amount = charge
                existing.method = exp.method
                existing.voucher_no = exp.voucher_no
                existing.paid_from_petty_cash = exp.paid_from_petty_cash
                existing.status = exp.status
                existing.save()
                # remove any accidental extra charge rows to prevent double-counting
                exp.charges.exclude(pk=existing.pk).delete()
            else:
                Expense.objects.create(
                    date=exp.date, sabbath_week=exp.sabbath_week,
                    department=exp.department, description=desc, amount=charge,
                    category=Expense.Category.BANK_CHARGE, method=exp.method,
                    recorded_by=self.request.user, voucher_no=exp.voucher_no,
                    paid_from_petty_cash=exp.paid_from_petty_cash,
                    status=exp.status, charge_for=exp,
                    approved_by=(exp.approved_by if exp.status != Expense.Status.PENDING
                                 else None))
        elif existing:
            # charge removed on edit — delete the linked charge entry
            exp.charges.all().delete()
        messages.success(self.request, "Expense updated.")
        return redirect(self.success_url)


class ExpenseApprove(TreasurerRequiredMixin, View):
    def post(self, request, pk):
        from core.models import SiteConfig
        exp = get_object_or_404(Expense, pk=pk)
        action = request.POST.get("action")
        cfg = SiteConfig.get()
        threshold = cfg.dual_approval_threshold or 0
        needs_two = threshold and exp.amount >= threshold
        if action == "approve":
            exp.status = Expense.Status.APPROVED
            exp.approved_by = request.user
        elif action == "second_approve":
            if exp.status != Expense.Status.APPROVED or not exp.approved_by_id:
                messages.error(request, "This expense must receive its first approval "
                                        "before a second treasurer can co-approve it.")
                return redirect("expense_list")
            if exp.approved_by_id == request.user.id:
                messages.error(request, "A second, different treasurer must co-approve this "
                                        "high-value expense.")
                return redirect("expense_list")
            exp.second_approved_by = request.user
            messages.success(request, "Second approval recorded.")
            exp.save()
            return redirect("expense_list")
        elif action == "reject":
            exp.status = Expense.Status.REJECTED
            exp.rejected_by = request.user
            # do NOT set approved_by on a rejection — it corrupts the audit trail
            # (an auditor would otherwise read "approved by X" on a rejected claim).
            exp.save()
            from core.services.notifications import notify
            reason = (request.POST.get("note") or "").strip()
            if exp.recorded_by_id and exp.recorded_by_id != request.user.id:
                notify("REJECTION",
                       f"Your expense “{exp.description}” ({exp.amount:,.2f}) was rejected"
                       + (f": {reason}" if reason else "."),
                       link="/expenses/", recipients=[exp.recorded_by])
            messages.success(request, "Expense rejected and the submitter notified.")
            return redirect("expense_list")
        elif action == "pay":
            # M1: a high-value expense needs two distinct treasurer approvals before pay
            if needs_two and (not exp.approved_by_id or not exp.second_approved_by_id):
                messages.error(request,
                    f"Expenses of {threshold:,.2f} or more need two treasurers' approval "
                    f"before payment. Have a second treasurer co-approve first.")
                return redirect("expense_list")
            exp.status = Expense.Status.PAID
            exp.paid_date = dt.date.today()
            if not exp.approved_by:
                exp.approved_by = request.user
        exp.save()
        messages.success(request, f"Expense marked {exp.get_status_display()}.")
        return redirect("expense_list")


class ExpenseDeleteView(TreasurerRequiredMixin, View):
    """Delete an expense (treasurer only; kept in the audit log)."""
    def post(self, request, pk):
        from django.shortcuts import get_object_or_404, redirect, render
        exp = get_object_or_404(Expense, pk=pk)
        if _block_if_locked(request, exp.date):
            return redirect("expense_list")
        exp.delete()
        messages.success(request, "Expense deleted.")
        return redirect(request.META.get("HTTP_REFERER") or "expense_list")


# ===================== Inter-fund transfers =====================
class TransferListView(ReadAccessMixin, ListView):
    template_name = "cashbook/transfer_list.html"
    context_object_name = "transfers"
    paginate_by = 50

    def get_queryset(self):
        from .models import FundTransfer
        return FundTransfer.objects.select_related(
            "source", "destination", "recorded_by").all()


class TransferCreate(DataEntryRequiredMixin, CreateView):
    from .models import FundTransfer
    model = FundTransfer
    form_class = FundTransferForm
    template_name = "cashbook/transfer_form.html"
    success_url = reverse_lazy("transfer_list")

    def form_valid(self, form):
        if _block_if_locked(self.request, form.cleaned_data.get("date")):
            return redirect("transfer_create")
        form.instance.recorded_by = self.request.user
        messages.success(self.request,
                         "Transfer recorded — the balance has moved between the funds.")
        return super().form_valid(form)


class TransferEdit(DataEntryRequiredMixin, UpdateView):
    from .models import FundTransfer
    model = FundTransfer
    form_class = FundTransferForm
    template_name = "cashbook/transfer_form.html"
    success_url = reverse_lazy("transfer_list")

    def dispatch(self, request, *args, **kwargs):
        self.object = self.get_object()
        if self.object.is_locked:
            messages.error(request, "This transfer can't be edited — it has been "
                "reversed, is a reversal entry, or falls in a locked period.")
            return redirect("transfer_list")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        # block if either the original date or the new date is locked
        if _block_if_locked(self.request, self.object.date) or \
           _block_if_locked(self.request, form.cleaned_data.get("date")):
            return redirect("transfer_edit", pk=self.object.pk)
        messages.success(self.request, "Transfer updated — balances, the journal and "
            "the audit trail were adjusted to match.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["editing"] = True
        return ctx


class TransferReverseView(TreasurerRequiredMixin, View):
    def post(self, request, pk):
        from .models import FundTransfer
        tr = get_object_or_404(FundTransfer, pk=pk)
        if _block_if_locked(request, tr.date):
            return redirect("transfer_list")
        try:
            tr.reverse(request.user)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("transfer_list")
        messages.success(request, "Transfer reversed — a mirror entry was posted and "
                                  "both remain on record.")
        return redirect("transfer_list")


# ===================== Recurring (scheduled) expenses =====================
class RecurringListView(ReadAccessMixin, ListView):
    template_name = "cashbook/recurring_list.html"
    context_object_name = "schedules"

    def get_queryset(self):
        from .models import RecurringExpense
        return RecurringExpense.objects.select_related("department").all()

    def get_context_data(self, **kwargs):
        from .services.recurring import next_due
        ctx = super().get_context_data(**kwargs)
        ctx["rows"] = [{"s": s, "next": next_due(s) if s.active else None}
                       for s in ctx["schedules"]]
        return ctx


class RecurringCreate(DataEntryRequiredMixin, CreateView):
    from .models import RecurringExpense
    model = RecurringExpense
    form_class = RecurringExpenseForm
    template_name = "cashbook/recurring_form.html"
    success_url = reverse_lazy("recurring_list")

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        messages.success(self.request, "Recurring expense saved. Use “Generate due” to "
                                       "create the entries up to today.")
        return super().form_valid(form)


class RecurringUpdate(DataEntryRequiredMixin, UpdateView):
    from .models import RecurringExpense
    model = RecurringExpense
    form_class = RecurringExpenseForm
    template_name = "cashbook/recurring_form.html"
    success_url = reverse_lazy("recurring_list")


class RecurringToggle(DataEntryRequiredMixin, View):
    def post(self, request, pk):
        from .models import RecurringExpense
        s = get_object_or_404(RecurringExpense, pk=pk)
        s.active = not s.active
        s.save(update_fields=["active"])
        messages.success(request, f"“{s.description}” is now {'active' if s.active else 'paused'}.")
        return redirect("recurring_list")


class RecurringDelete(DataEntryRequiredMixin, View):
    """Delete a recurring schedule. Expenses it already generated are kept (they
    are real ledger entries); only the future schedule is removed."""
    def post(self, request, pk):
        from .models import RecurringExpense
        s = get_object_or_404(RecurringExpense, pk=pk)
        desc = s.description
        s.delete()
        messages.success(request, f"Recurring schedule “{desc}” deleted. "
                                  f"Any expenses it already created remain in the ledger.")
        return redirect("recurring_list")


class RecurringGenerate(DataEntryRequiredMixin, View):
    def post(self, request):
        from .services.recurring import generate_due, generate_schedule
        from .models import RecurringExpense
        pk = request.POST.get("schedule")
        if pk:
            s = get_object_or_404(RecurringExpense, pk=pk)
            n = generate_schedule(s, user=request.user)
        else:
            n = generate_due(user=request.user)
        if n:
            messages.success(request, f"Generated {n} expense entr{'y' if n == 1 else 'ies'} "
                                      f"from the recurring schedule(s), up to today.")
        else:
            messages.info(request, "Nothing due — all recurring expenses are already up to date.")
        return redirect("recurring_list")


# ============================ Petty cash ============================
# Petty cash is a CASH LOCATION (a physical float), not a fund. Top-ups put cash
# into the float; disbursements are real expenses charged to the relevant ministry
# fund and flagged as paid from petty cash. The float balance is a control total
# (top-ups less petty disbursements) reconciled against the cash in the box; the
# ministry funds carry the actual cost, so fund balances stay correct.
from django.views.generic import TemplateView
from .forms import PettyCashTopUpForm, PettyCashDisbursementForm
from .models import PettyCashTopUp as PettyTopUp


def _petty_balance_asof(on):
    from decimal import Decimal
    from django.db.models import Sum
    from .models import StaffAdvance, ExpenseRefund
    topups = (PettyTopUp.objects.filter(date__lte=on)
              .aggregate(t=Sum("amount"))["t"] or Decimal(0))
    disb = (Expense.objects.filter(paid_from_petty_cash=True, date__lte=on,
            status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
            .aggregate(t=Sum("amount"))["t"] or Decimal(0))
    # cash refunded back into the petty box tops the float up again
    refunds_in = (ExpenseRefund.objects.filter(to_petty_cash=True, date__lte=on)
                  .aggregate(t=Sum("amount"))["t"] or Decimal(0))
    # advances issued out of the petty box, still unaccounted, are also "out"
    adv_out = Decimal(0)
    for adv in StaffAdvance.objects.filter(from_petty_cash=True, date_issued__lte=on):
        adv_out += adv.petty_outstanding_asof(on)
    return topups - disb + refunds_in - adv_out


class PettyCashView(ReadAccessMixin, TemplateView):
    template_name = "cashbook/petty_cash.html"

    def get(self, request, *args, **kwargs):
        if request.GET.get("export") in ("csv", "xlsx"):
            return self._export(request)
        return super().get(request, *args, **kwargs)

    def _export(self, request):
        from core.utils import parse_period
        from reports.exports import csv_response, xlsx_response
        from core.models import SiteConfig
        ctx = self.get_context_data()
        header = ["Date", "Description", "Fund", "In", "Out", "Balance"]
        rows = [["", "Opening balance", "", "", "", ctx["opening"]]]
        for m in ctx["movements"]:
            rows.append([m["date"].isoformat(), m["desc"], m.get("fund") or "",
                         m["in"] or "", m["out"] or "", m["balance"]])
        rows.append(["", "Closing balance", "", "", "", ctx["closing"]])
        start, end = parse_period(request)
        fn = f"petty_cash_{start}_{end}"
        if request.GET.get("export") == "xlsx":
            return xlsx_response(fn + ".xlsx", header, rows,
                title=f"Petty cash register {start} to {end}",
                church=SiteConfig.get().church_name)
        return csv_response(fn + ".csv", header, rows)

    def get_context_data(self, **kwargs):
        from decimal import Decimal
        from core.models import SiteConfig
        from core.utils import parse_period
        ctx = super().get_context_data(**kwargs)
        start, end = parse_period(self.request)
        opening = _petty_balance_asof(start - dt.timedelta(days=1))
        movements = []
        for t in PettyTopUp.objects.filter(date__gte=start, date__lte=end):
            movements.append({"date": t.date, "desc": "Top-up" + (f" — {t.note}" if t.note else ""),
                              "in": t.amount, "out": None, "fund": None})
        disb = (Expense.objects.filter(paid_from_petty_cash=True, date__gte=start, date__lte=end,
                status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
                .select_related("department"))
        for x in disb:
            movements.append({"date": x.date, "desc": x.description
                              + (f" · {x.claimant}" if x.claimant else ""), "in": None,
                              "out": x.amount, "fund": x.department.name, "cat": x.get_category_display()})
        # petty-cash-funded staff advances: the float drops when issued and rises
        # again only if unspent cash is returned
        from .models import StaffAdvance, AdvanceTopUp
        for adv in StaffAdvance.objects.filter(from_petty_cash=True,
                date_issued__gte=start, date_issued__lte=end).select_related("department"):
            movements.append({"date": adv.date_issued,
                "desc": f"Advance to {adv.staff_name} — {adv.purpose}",
                "in": None, "out": adv.base_amount, "fund": adv.department.name,
                "cat": "Staff advance"})
        # each top-up onto a petty-funded advance is a further outflow on its own date
        for tu in AdvanceTopUp.objects.filter(advance__from_petty_cash=True,
                date__gte=start, date__lte=end).select_related("advance", "advance__department"):
            movements.append({"date": tu.date,
                "desc": f"Advance top-up — {tu.advance.staff_name}"
                        + (f" · {tu.note}" if tu.note else ""),
                "in": None, "out": tu.amount,
                "fund": tu.advance.department.name, "cat": "Advance top-up"})
        for adv in StaffAdvance.objects.filter(from_petty_cash=True,
                returned_to_petty__gt=0, settled_on__gte=start,
                settled_on__lte=end).select_related("department"):
            movements.append({"date": adv.settled_on,
                "desc": f"Unspent advance returned — {adv.staff_name}",
                "in": adv.returned_to_petty, "out": None,
                "fund": adv.department.name, "cat": "Advance return"})
        movements.sort(key=lambda m: m["date"])
        running = opening
        for m in movements:
            running += (m["in"] or Decimal(0)) - (m["out"] or Decimal(0))
            m["balance"] = running
        cfg = SiteConfig.get()
        balance_now = _petty_balance_asof(dt.date.today())
        ctx.update({
            "start": start, "end": end, "opening": opening, "movements": movements,
            "closing": running, "balance_now": balance_now,
            "float_target": cfg.petty_cash_float,
            "to_topup": max(cfg.petty_cash_float - balance_now, Decimal(0)) if cfg.petty_cash_float else Decimal(0),
            "topup_form": PettyCashTopUpForm(), "disb_form": PettyCashDisbursementForm(),
        })
        return ctx


class PettyCashTopUp(DataEntryRequiredMixin, View):
    def post(self, request):
        form = PettyCashTopUpForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            if _block_if_locked(request, cd["date"]):
                return redirect("petty_cash")
            PettyTopUp.objects.create(date=cd["date"], amount=cd["amount"],
                                      note=cd["note"], recorded_by=request.user)
            messages.success(request, f"Petty cash float increased by {cd['amount']}.")
        else:
            messages.error(request, "Could not record the top-up: " + form.errors.as_text())
        return redirect("petty_cash")


class PettyCashDisburse(DataEntryRequiredMixin, View):
    def post(self, request):
        form = PettyCashDisbursementForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            if _block_if_locked(request, cd["date"]):
                return redirect("petty_cash")
            from core.models import SiteConfig
            if SiteConfig.get().enforce_petty_float:
                bal = _petty_balance_asof(cd["date"])
                need = cd["amount"] + (cd.get("charge") or 0)
                if need > bal:
                    messages.error(request,
                        f"Insufficient petty cash float — {bal:,.2f} on hand, "
                        f"{need:,.2f} requested. Top up the float first.")
                    return redirect("petty_cash")
            method = cd.get("method") or Expense.Method.CASH
            exp = Expense.objects.create(
                date=cd["date"], sabbath_week=sabbath_week_of(cd["date"]),
                department=cd["department"], description=cd["description"], amount=cd["amount"],
                category=cd["category"], claimant=cd["claimant"], method=method,
                voucher_no=cd.get("voucher_no") or "",
                paid_from_petty_cash=True, status=Expense.Status.PAID, paid_date=cd["date"],
                recorded_by=request.user, approved_by=request.user)
            # optional M-Pesa / bank charge (float held on M-Pesa/bank) -> linked,
            # also paid from petty cash so it reduces the float too
            charge = cd.get("charge")
            if charge and charge > 0:
                ref = exp.voucher_no or f"exp #{exp.id}"
                Expense.objects.create(
                    date=cd["date"], sabbath_week=exp.sabbath_week, department=cd["department"],
                    description=f"Transaction charge — {exp.description} [for {ref}]"[:200],
                    amount=charge, category=Expense.Category.BANK_CHARGE, method=method,
                    voucher_no=exp.voucher_no, paid_from_petty_cash=True, charge_for=exp,
                    status=Expense.Status.PAID, paid_date=cd["date"],
                    recorded_by=request.user, approved_by=request.user)
            extra = f" (+ {charge:,.2f} charge)" if charge and charge > 0 else ""
            messages.success(request, f"Petty cash disbursement of {cd['amount']:,.2f}{extra} "
                                      f"charged to {cd['department'].name}.")
        else:
            messages.error(request, "Could not record the disbursement: " + form.errors.as_text())
        return redirect("petty_cash")


# ==================== Expense detail + receipt attachments ====================
from django.views.generic import DetailView
from .models import ExpenseAttachment


class ExpenseDetailView(ReadAccessMixin, DetailView):
    model = Expense
    template_name = "cashbook/expense_detail.html"
    context_object_name = "expense"

    def get_context_data(self, **kwargs):
        from core.models import SiteConfig
        ctx = super().get_context_data(**kwargs)
        # same source as the list, so the dual-approval gate behaves identically
        ctx["dual_threshold"] = SiteConfig.get().dual_approval_threshold or 0
        ctx["refunds"] = self.object.refunds.select_related("recorded_by")
        ctx["refundable"] = self.object.refundable_balance
        import datetime as _dt
        ctx["today_iso"] = _dt.date.today().isoformat()
        return ctx


class ExpenseRefundCreate(DataEntryRequiredMixin, View):
    """Record cash returned to the fund against an expense (e.g. unspent change
    from an over-issued purchase). The original expense is never altered."""
    def post(self, request, pk):
        from .models import ExpenseRefund
        from django.core.exceptions import ValidationError
        from decimal import Decimal
        exp = get_object_or_404(Expense, pk=pk)
        if _block_if_locked(request, exp.date):
            return redirect("expense_detail", pk=pk)
        import datetime as _dt
        raw_date = request.POST.get("date") or ""
        try:
            d = _dt.date.fromisoformat(raw_date) if raw_date else _dt.date.today()
        except ValueError:
            d = _dt.date.today()
        try:
            amount = Decimal(request.POST.get("amount") or "0")
        except Exception:  # noqa: BLE001
            amount = Decimal(0)
        ref = ExpenseRefund(expense=exp, date=d, amount=amount,
            method=request.POST.get("method") or Expense.Method.CASH,
            to_petty_cash=bool(request.POST.get("to_petty_cash")),
            reference=request.POST.get("reference", "")[:40],
            note=request.POST.get("note", "")[:200], recorded_by=request.user)
        try:
            ref.full_clean(exclude=["recorded_by"])
        except ValidationError as exc:
            messages.error(request, "; ".join(
                m for msgs in exc.message_dict.values() for m in msgs))
            return redirect("expense_detail", pk=pk)
        ref.save()
        messages.success(request, f"Refund of {amount:,.2f} recorded — the fund "
            f"balance has been restored by this amount.")
        return redirect("expense_detail", pk=pk)


class ExpenseRefundDelete(TreasurerRequiredMixin, View):
    def post(self, request, pk, ref):
        from .models import ExpenseRefund
        r = get_object_or_404(ExpenseRefund, pk=ref, expense_id=pk)
        if _block_if_locked(request, r.date):
            return redirect("expense_detail", pk=pk)
        r.delete()
        messages.success(request, "Refund removed.")
        return redirect("expense_detail", pk=pk)


class ExpenseAttachmentUpload(DataEntryRequiredMixin, View):
    ALLOWED_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif")
    MAX_BYTES = 10 * 1024 * 1024

    def post(self, request, pk):
        exp = get_object_or_404(Expense, pk=pk)
        f = request.FILES.get("file")
        text = (request.POST.get("text") or "").strip()
        link = (request.POST.get("link") or "").strip()
        if f:
            if not f.name.lower().endswith(self.ALLOWED_EXT):
                messages.error(request, "Receipts must be a PDF or image file "
                                        "(.pdf, .jpg, .png, .heic, .webp).")
                return redirect("expense_detail", pk=pk)
            if f.size > self.MAX_BYTES:
                messages.error(request, "That file is too large — receipts must be "
                                        "10 MB or smaller.")
                return redirect("expense_detail", pk=pk)
        if f or text or link:
            ExpenseAttachment.objects.create(expense=exp, file=f or None,
                text=text, link=link, label=request.POST.get("label", "")[:120],
                uploaded_by=request.user)
            messages.success(request, "Receipt added.")
        else:
            messages.error(request, "Add a file, paste a text receipt, or enter a link.")
        return redirect("expense_detail", pk=pk)


class ExpenseAttachmentDelete(DataEntryRequiredMixin, View):
    def post(self, request, pk, att):
        a = get_object_or_404(ExpenseAttachment, pk=att, expense_id=pk)
        a.file.delete(save=False)
        a.delete()
        messages.success(request, "Attachment removed.")
        return redirect("expense_detail", pk=pk)


# ============== Payables, accruals & prepayments (accrual overlay) ==============
# This app keeps spendable fund balances on a cash basis. Payables, accruals and
# prepayments are an accrual overlay: they are tracked here and shown as memoranda
# on the Statement of Financial Position so the treasurer can see obligations and
# prepaid amounts. Settling a payable/accrual records the actual payment (an
# Expense in the fund), so the cash books recognise it at payment date.
from .models import Payable, Accrual, Prepayment
from .forms import PayableForm, AccrualForm, PrepaymentForm


def open_payables_total(as_of=None):
    from decimal import Decimal
    from django.db.models import Sum, Q
    if as_of:
        # outstanding *as at* a date: incurred on/before it and either not settled
        # or only settled after it (so settling on the 15th still shows as a
        # liability on a 14th statement of financial position).
        qs = Payable.objects.filter(date__lte=as_of).filter(
            Q(settled=False) | Q(settled_on__gt=as_of) | Q(settled_on__isnull=True))
    else:
        qs = Payable.objects.filter(settled=False)
    return qs.aggregate(t=Sum("amount"))["t"] or Decimal(0)


def open_accruals_total(as_of=None):
    from decimal import Decimal
    from django.db.models import Sum, Q
    if as_of:
        qs = Accrual.objects.filter(date__lte=as_of).filter(
            Q(settled=False) | Q(settled_on__gt=as_of) | Q(settled_on__isnull=True))
    else:
        qs = Accrual.objects.filter(settled=False)
    return qs.aggregate(t=Sum("amount"))["t"] or Decimal(0)


def unexpired_prepayments_total(as_of=None):
    from decimal import Decimal
    import datetime as _dt
    as_of = as_of or _dt.date.today()
    return sum((p.unexpired(as_of) for p in Prepayment.objects.all()), Decimal(0))


def outstanding_bank_advances_total(as_of=None):
    """Outstanding advances issued from the BANK (not petty cash). These reduce
    the bank statement balance at issuance but are not yet an expense in the cash
    book, so until accounted for they are a reconciling item between bank and book.
    Petty-funded advances are excluded — those sit in the petty-cash float."""
    from decimal import Decimal
    import datetime as _dt
    from django.db.models import Sum
    from cashbook.models import StaffAdvance, Expense
    as_of = as_of or _dt.date.today()
    total = Decimal(0)
    for adv in StaffAdvance.objects.filter(date_issued__lte=as_of, from_petty_cash=False
            ).exclude(status=StaffAdvance.Status.CLOSED):
        settled = (adv.expenses.filter(
            status__in=[Expense.Status.APPROVED, Expense.Status.PAID],
            date__lte=as_of).aggregate(t=Sum("amount"))["t"] or Decimal(0))
        bal = adv.amount - settled - (adv.returned_to_petty or Decimal(0))
        if bal > 0:
            total += bal
    return total


def outstanding_advances_total(as_of=None):
    """Money advanced to staff that has not yet been accounted for by receipts —
    i.e. a receivable. Computed as (amount issued − expenses settled up to the
    date) for advances issued on/before `as_of` that are not yet closed. Only
    positive balances count (a shortfall is owed to staff, not a receivable)."""
    from decimal import Decimal
    import datetime as _dt
    from django.db.models import Sum
    from cashbook.models import StaffAdvance, Expense
    as_of = as_of or _dt.date.today()
    total = Decimal(0)
    for adv in StaffAdvance.objects.filter(date_issued__lte=as_of).exclude(
            status=StaffAdvance.Status.CLOSED):
        settled = (adv.expenses.filter(
            status__in=[Expense.Status.APPROVED, Expense.Status.PAID],
            date__lte=as_of).aggregate(t=Sum("amount"))["t"] or Decimal(0))
        bal = adv.amount - settled
        if bal > 0:
            total += bal
    return total


class AccrualsView(ReadAccessMixin, TemplateView):
    template_name = "cashbook/accruals.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        today = dt.date.today()
        payables = list(Payable.objects.select_related("department"))
        accruals = list(Accrual.objects.select_related("department"))
        prepayments = list(Prepayment.objects.select_related("department"))
        for p in prepayments:
            p.unexpired_now = p.unexpired(today)
        ctx.update({
            "payables": payables, "accruals": accruals, "prepayments": prepayments,
            "open_payables": open_payables_total(), "open_accruals": open_accruals_total(),
            "unexpired_prepay": unexpired_prepayments_total(),
            "payable_form": PayableForm(), "accrual_form": AccrualForm(),
            "prepayment_form": PrepaymentForm(), "today": today,
        })
        return ctx


class PayableCreate(DataEntryRequiredMixin, View):
    def post(self, request):
        form = PayableForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            if _block_if_locked(request, cd["date"]):
                return redirect("accruals")
            Payable.objects.create(date=cd["date"], vendor=cd["vendor"],
                description=cd["description"], amount=cd["amount"], department=cd["department"],
                category=cd["category"], due_date=cd.get("due_date"), recorded_by=request.user)
            messages.success(request, f"Payable to {cd['vendor']} recorded.")
        else:
            messages.error(request, "Could not record payable: " + form.errors.as_text())
        return redirect("accruals")


class AccrualCreate(DataEntryRequiredMixin, View):
    def post(self, request):
        form = AccrualForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            if _block_if_locked(request, cd["date"]):
                return redirect("accruals")
            Accrual.objects.create(date=cd["date"], description=cd["description"],
                amount=cd["amount"], department=cd["department"], category=cd["category"],
                recorded_by=request.user)
            messages.success(request, "Accrual recorded.")
        else:
            messages.error(request, "Could not record accrual: " + form.errors.as_text())
        return redirect("accruals")


class PrepaymentCreate(DataEntryRequiredMixin, View):
    def post(self, request):
        form = PrepaymentForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            # the cash leaves now: record the payment as a PAID expense in the fund
            exp = Expense.objects.create(date=cd["date"], sabbath_week=sabbath_week_of(cd["date"]),
                department=cd["department"], description=f"Prepayment: {cd['description']}",
                amount=cd["amount"], category=cd["category"], method=Expense.Method.BANK,
                status=Expense.Status.PAID, paid_date=cd["date"], recorded_by=request.user,
                approved_by=request.user)
            Prepayment.objects.create(date=cd["date"], description=cd["description"],
                amount=cd["amount"], department=cd["department"], category=cd["category"],
                months=cd["months"], start_date=cd["start_date"], source_expense=exp,
                recorded_by=request.user)
            messages.success(request, "Prepayment recorded; the unexpired portion is shown as a "
                                      "prepaid asset.")
        else:
            messages.error(request, "Could not record prepayment: " + form.errors.as_text())
        return redirect("accruals")


@db_tx.atomic
def _settle(obj, request):
    """Create the actual payment expense in the fund and mark the item settled."""
    if obj.settled:
        return
    exp = Expense.objects.create(date=dt.date.today(), sabbath_week=sabbath_week_of(dt.date.today()),
        department=obj.department, description=f"Settle: {obj.description}"[:200],
        amount=obj.amount, category=obj.category, method=Expense.Method.BANK,
        status=Expense.Status.PAID, paid_date=dt.date.today(), recorded_by=request.user,
        approved_by=request.user)
    obj.settled = True
    obj.settled_on = dt.date.today()
    obj.settled_expense = exp
    obj.save()


class PayableSettle(DataEntryRequiredMixin, View):
    def post(self, request, pk):
        obj = get_object_or_404(Payable, pk=pk)
        _settle(obj, request)
        messages.success(request, f"Payable to {obj.vendor} settled and charged to "
                                  f"{obj.department.name}.")
        return redirect("accruals")


class AccrualSettle(DataEntryRequiredMixin, View):
    def post(self, request, pk):
        obj = get_object_or_404(Accrual, pk=pk)
        _settle(obj, request)
        messages.success(request, "Accrual settled and recorded as an expense.")
        return redirect("accruals")


# ============================ Staff advances / imprest ============================
class AdvanceListView(ReadAccessMixin, ListView):
    template_name = "cashbook/advance_list.html"
    context_object_name = "advances"
    paginate_by = 40

    def get_queryset(self):
        from .models import StaffAdvance
        return StaffAdvance.objects.select_related("department").all()

    def get_context_data(self, **kwargs):
        from decimal import Decimal
        ctx = super().get_context_data(**kwargs)
        today = dt.date.today()
        bank_out = outstanding_bank_advances_total(today)
        all_out = outstanding_advances_total(today)
        petty_out = max(all_out - bank_out, Decimal(0))
        ctx.update({
            "total_outstanding": all_out,
            "bank_outstanding": bank_out,
            "petty_outstanding": petty_out,
        })
        return ctx


class AdvanceCreate(AdvanceAccessMixin, View):
    template_name = "cashbook/advance_form.html"

    def get(self, request):
        return render(request, self.template_name, {
            "departments": Department.objects.filter(active=True).order_by("name"),
            "petty_balance": _petty_balance_asof(dt.date.today()),
            "today": dt.date.today().isoformat()})

    def post(self, request):
        from decimal import Decimal, InvalidOperation
        from .models import StaffAdvance
        dept = Department.objects.filter(pk=request.POST.get("department")).first()
        try:
            amount = Decimal(request.POST.get("amount") or "0")
        except InvalidOperation:
            amount = Decimal(0)
        name = (request.POST.get("staff_name") or "").strip()
        if not (dept and name and amount > 0):
            messages.error(request, "Staff name, fund and a positive amount are required.")
            return redirect("advance_new")
        try:
            issued = dt.date.fromisoformat(request.POST.get("date_issued"))
        except (TypeError, ValueError):
            issued = dt.date.today()
        if _block_if_locked(request, issued):
            return redirect("advance_new")
        from_petty = request.POST.get("from_petty_cash") in ("1", "on", "true")
        method = request.POST.get("method") or "CASH"
        try:
            charge = Decimal(request.POST.get("bank_charge") or "0")
        except InvalidOperation:
            charge = Decimal(0)
        if charge < 0:
            charge = Decimal(0)
        if from_petty:
            method = "CASH"   # petty cash is, by definition, cash
            avail = _petty_balance_asof(issued)
            if amount > avail:
                messages.error(request,
                    f"The petty-cash float is only KSh {avail:,.2f} on {issued:%d %b %Y}; "
                    f"top it up before issuing an advance of KSh {amount:,.2f}.")
                return redirect("advance_new")
        adv = StaffAdvance.objects.create(
            staff_name=name, department=dept, amount=amount, date_issued=issued,
            purpose=(request.POST.get("purpose") or "")[:200],
            method=method, from_petty_cash=from_petty, bank_charge=charge,
            reference=(request.POST.get("reference") or "")[:40],
            issued_by=request.user)
        _sync_advance_charge(adv, request.user)
        messages.success(request, "Advance recorded." + (
            " Petty-cash float reduced accordingly." if from_petty else "")
            + (f" Bank/M-Pesa charge of KSh {charge:,.2f} recorded." if charge else ""))
        return redirect("advance_detail", pk=adv.pk)


def apply_advance_edit(adv, post, user, *, allow_petty_toggle=True):
    """Apply an edit to an advance from POST data and keep everything in step
    (petty float recomputes from the live fields; the bank-charge expense is
    re-synced). Returns (ok, error_message)."""
    from decimal import Decimal, InvalidOperation
    from .models import StaffAdvance
    name = (post.get("staff_name") or adv.staff_name).strip()
    dept = Department.objects.filter(pk=post.get("department")).first() or adv.department
    try:
        amount = Decimal(post.get("amount") or adv.amount)
    except InvalidOperation:
        amount = adv.amount
    try:
        charge = Decimal(post.get("bank_charge") or "0")
    except InvalidOperation:
        charge = Decimal(0)
    if amount <= 0:
        return False, "Amount must be positive."
    if charge < 0:
        charge = Decimal(0)
    try:
        issued = dt.date.fromisoformat(post.get("date_issued"))
    except (TypeError, ValueError):
        issued = adv.date_issued
    from_petty = adv.from_petty_cash
    if allow_petty_toggle:
        from_petty = post.get("from_petty_cash") in ("1", "on", "true")
    method = "CASH" if from_petty else (post.get("method") or adv.method)
    # validate petty float can still cover a petty advance (excluding this advance's
    # own current contribution so an unchanged edit doesn't false-trip)
    if from_petty:
        avail = _petty_balance_asof(issued) + adv.petty_outstanding_asof(issued)
        if amount - adv.settled_asof(issued) - (adv.returned_to_petty or Decimal(0)) > avail:
            return False, (f"The petty-cash float can't cover that amount on "
                           f"{issued:%d %b %Y}.")
    adv.staff_name = name[:120]
    adv.department = dept
    adv.amount = amount
    adv.date_issued = issued
    adv.method = method
    adv.from_petty_cash = from_petty
    adv.bank_charge = charge
    adv.purpose = (post.get("purpose") or adv.purpose)[:200]
    adv.reference = (post.get("reference") or adv.reference)[:40]
    adv.save()
    _sync_advance_charge(adv, user)
    # refresh status against the new amount
    bal = adv.balance
    if bal == 0 and adv.settled_total > 0:
        adv.status = StaffAdvance.Status.SETTLED
    elif adv.settled_total > 0:
        adv.status = StaffAdvance.Status.PARTLY
    elif adv.status in (StaffAdvance.Status.SETTLED, StaffAdvance.Status.PARTLY):
        adv.status = StaffAdvance.Status.ISSUED
    adv.save(update_fields=["status"])
    return True, None


class AdvanceEdit(AdvanceAccessMixin, View):
    """Edit an advance. Treasurers/assistants may edit any advance; a closed
    advance can only be amended by a treasurer (enforced below)."""
    template_name = "cashbook/advance_form.html"

    def _get(self, pk):
        from .models import StaffAdvance
        return get_object_or_404(StaffAdvance, pk=pk)

    def get(self, request, pk):
        adv = self._get(pk)
        return render(request, self.template_name, {
            "adv": adv, "editing": True,
            "departments": Department.objects.filter(active=True).order_by("name"),
            "petty_balance": _petty_balance_asof(dt.date.today()),
            "today": adv.date_issued.isoformat()})

    def post(self, request, pk):
        from core import roles
        adv = self._get(pk)
        if adv.status == "CLOSED" and not roles.is_treasurer(request.user):
            messages.error(request, "A closed advance can only be amended by a treasurer.")
            return redirect("advance_detail", pk=pk)
        if _block_if_locked(request, adv.date_issued):
            return redirect("advance_detail", pk=pk)
        ok, err = apply_advance_edit(adv, request.POST, request.user)
        if not ok:
            messages.error(request, err)
            return redirect("advance_edit", pk=pk)
        messages.success(request, "Advance updated.")
        return redirect("advance_detail", pk=pk)


class AdvanceDelete(TreasurerRequiredMixin, View):
    """Delete an advance end-to-end: its settling expenses and bank-charge expense
    go with it, and the petty float recomputes automatically."""
    def post(self, request, pk):
        from .models import StaffAdvance
        adv = get_object_or_404(StaffAdvance, pk=pk)
        if _block_if_locked(request, adv.date_issued):
            return redirect("advance_detail", pk=pk)
        # an advance can only be deleted if nothing has been accounted against it
        if adv.expenses.exists():
            messages.error(request, "This advance has expenses recorded against it. "
                "Remove those expenses first, then delete the advance.")
            return redirect("advance_detail", pk=pk)
        # detach and remove the church's sending-charge expense (not an accounting
        # line against the advance), then delete the advance
        if adv.charge_expense_id:
            ce = adv.charge_expense
            adv.charge_expense = None
            adv.save(update_fields=["charge_expense"])
            ce.delete()
        adv.delete()
        messages.success(request, "Advance deleted.")
        return redirect("advance_list")


class AdvanceDetail(ReadAccessMixin, View):
    template_name = "cashbook/advance_detail.html"

    def get(self, request, pk):
        from .models import StaffAdvance
        adv = get_object_or_404(StaffAdvance, pk=pk)
        return render(request, self.template_name, _advance_detail_ctx(adv, user=request.user))


def _advance_detail_ctx(adv, *, leader_mode=False, user=None):
    """Shared context for the advance statement (issued + top-ups → expense lines →
    balance), used by both the treasurer and leader detail pages."""
    from decimal import Decimal
    # build a dated time-line of issues (base + top-ups) and expense lines
    events = [{"date": adv.date_issued, "kind": "issue",
               "label": f"Advance issued — {adv.purpose}", "amount": adv.base_amount}]
    for t in adv.topups.all():
        events.append({"date": t.date, "kind": "topup",
                       "label": "Top-up issued" + (f" — {t.note}" if t.note else ""),
                       "amount": t.amount, "topup_id": t.id})
    for e in adv.expenses.filter(
            status__in=[Expense.Status.APPROVED, Expense.Status.PAID]).order_by("date", "id"):
        events.append({"date": e.date, "kind": "expense", "label": e.description,
                       "amount": e.amount, "expense": e})
    if adv.returned_to_petty:
        events.append({"date": adv.settled_on or adv.date_issued, "kind": "return",
                       "label": "Unspent cash returned to petty cash",
                       "amount": adv.returned_to_petty})
    events.sort(key=lambda x: (x["date"], 0 if x["kind"] in ("issue", "topup") else 1))
    rows, running = [], Decimal(0)
    can_edit_line = bool(leader_mode and user and adv.status != adv.Status.CLOSED)
    for ev in events:
        if ev["kind"] in ("issue", "topup"):
            running += ev["amount"]
            rows.append({"date": ev["date"], "label": ev["label"],
                         "out": ev["amount"], "back": None, "running": running,
                         "topup_id": ev.get("topup_id")})
        else:
            running -= ev["amount"]
            e = ev.get("expense")
            mine = bool(e and user and e.recorded_by_id == getattr(user, "id", None))
            is_charge = bool(e and e.category == Expense.Category.BANK_CHARGE)
            rows.append({"date": ev["date"], "label": ev["label"], "out": None,
                         "back": ev["amount"], "running": running, "expense": e,
                         "editable": can_edit_line and mine and not is_charge,
                         "attachable": bool(leader_mode and e and adv.status != adv.Status.CLOSED),
                         "is_charge": is_charge,
                         "attachments": list(e.attachments.all()) if e else []})
    from core.roles import is_treasurer as _is_tr
    return {
        "adv": adv, "expenses": adv.expenses.all(), "statement": rows,
        "to_account": running,   # >0 still to account; <0 reimburse staff
        "categories": Expense.Category.choices,
        "today": dt.date.today().isoformat(), "leader_mode": leader_mode,
        "is_treasurer": bool(user and _is_tr(user)) and not leader_mode,
    }


def _sync_advance_charge(adv, user):
    """Create / update / remove the BANK_CHARGE expense for the charge the CHURCH
    incurs when sending an advance to the holder (adv.bank_charge). This is the
    church's cost, so it is booked against the fund but does NOT reduce the advance
    the holder must account for — it is not linked via the `advance` FK. The
    charge_expense one-to-one keeps a handle on it for edits/removal."""
    from decimal import Decimal
    from .models import Expense
    charge = adv.bank_charge or Decimal(0)
    exp = adv.charge_expense
    if charge and charge > 0:
        if exp:
            exp.date = adv.date_issued
            exp.amount = charge
            exp.department = adv.department
            exp.method = adv.method
            exp.claimant = adv.staff_name
            exp.paid_from_petty_cash = adv.from_petty_cash
            exp.description = f"Bank/M-Pesa charge — sending advance to {adv.staff_name}"
            exp.save(update_fields=["date", "amount", "department", "method",
                                    "claimant", "paid_from_petty_cash", "description"])
        else:
            exp = Expense.objects.create(
                date=adv.date_issued, sabbath_week=sabbath_week_of(adv.date_issued),
                department=adv.department,
                description=f"Bank/M-Pesa charge — sending advance to {adv.staff_name}",
                amount=charge, category=Expense.Category.BANK_CHARGE,
                claimant=adv.staff_name, method=adv.method,
                paid_from_petty_cash=adv.from_petty_cash,
                status=Expense.Status.PAID, paid_date=adv.date_issued,
                recorded_by=user, approved_by=user)
            adv.charge_expense = exp
            adv.save(update_fields=["charge_expense"])
    elif exp:
        # charge removed — delete the linked expense
        adv.charge_expense = None
        adv.save(update_fields=["charge_expense"])
        exp.delete()


def _record_advance_expense(adv, *, date, desc, amount, category, user, claimant=None,
                            charge=None):
    """Create an APPROVED+PAID expense that accounts for part of a staff advance,
    and refresh the advance's status. Optionally also book a transaction `charge`
    the holder incurred on that payment (M-Pesa/bank fee) as a linked BANK_CHARGE
    line — that, too, is met out of the advance and reduces the balance.

    Enforces that the total accounted (expense + its charge) cannot exceed the
    advance's remaining balance (#4): you can't account for more than was advanced.
    Returns (expense, error_message); expense is None when blocked."""
    from decimal import Decimal
    from .models import StaffAdvance, Expense
    charge = charge or Decimal(0)
    needed = amount + charge
    if needed > adv.balance:
        return None, (f"This would account for KSh {needed:,.2f}, but only "
                      f"KSh {adv.balance:,.2f} is left on the advance. Reduce the "
                      f"amount, or ask the treasurer to top up the advance first.")
    exp = Expense.objects.create(
        date=date, sabbath_week=sabbath_week_of(date), department=adv.department,
        description=desc, amount=amount,
        category=category or Expense.Category.OTHER,
        claimant=(claimant or adv.staff_name), method=adv.method,
        status=Expense.Status.PAID, paid_date=date,
        paid_from_petty_cash=False,   # the petty box lost the cash at issuance
        recorded_by=user, advance=adv, approved_by=user)
    if charge and charge > 0:
        Expense.objects.create(
            date=date, sabbath_week=sabbath_week_of(date), department=adv.department,
            description=f"Transaction charge — {desc}", amount=charge,
            category=Expense.Category.BANK_CHARGE,
            claimant=(claimant or adv.staff_name), method=adv.method,
            status=Expense.Status.PAID, paid_date=date, paid_from_petty_cash=False,
            recorded_by=user, advance=adv, approved_by=user)
    bal = adv.balance
    if bal == 0:
        adv.status = StaffAdvance.Status.SETTLED
    elif adv.settled_total > 0:
        adv.status = StaffAdvance.Status.PARTLY
    adv.save(update_fields=["status"])
    return exp, None


class AdvanceAddExpense(AdvanceAccessMixin, View):
    """Record a receipt/expense that settles part of an advance."""
    def post(self, request, pk):
        from decimal import Decimal, InvalidOperation
        from .models import StaffAdvance
        adv = get_object_or_404(StaffAdvance, pk=pk)
        try:
            amount = Decimal(request.POST.get("amount") or "0")
        except InvalidOperation:
            amount = Decimal(0)
        try:
            charge = Decimal(request.POST.get("charge") or "0")
        except InvalidOperation:
            charge = Decimal(0)
        if charge < 0:
            charge = Decimal(0)
        desc = (request.POST.get("description") or "").strip()
        if not (desc and amount > 0):
            messages.error(request, "A description and positive amount are required.")
            return redirect("advance_detail", pk=pk)
        try:
            d = dt.date.fromisoformat(request.POST.get("date"))
        except (TypeError, ValueError):
            d = dt.date.today()
        _exp, err = _record_advance_expense(adv, date=d, desc=desc, amount=amount,
            category=request.POST.get("category"), user=request.user, charge=charge)
        if err:
            messages.error(request, err)
        else:
            messages.success(request, "Expense recorded against the advance."
                + (f" Transaction charge of KSh {charge:,.2f} added." if charge else ""))
        return redirect("advance_detail", pk=pk)


class AdvanceTopUpView(AdvanceAccessMixin, View):
    """Issue more cash onto an open advance (carry a small leftover forward into a
    larger working advance instead of retiring and re-issuing)."""
    def post(self, request, pk):
        from decimal import Decimal, InvalidOperation
        from .models import StaffAdvance, AdvanceTopUp
        adv = get_object_or_404(StaffAdvance, pk=pk)
        if adv.status == StaffAdvance.Status.CLOSED:
            messages.error(request, "This advance is closed; issue a new one instead.")
            return redirect("advance_detail", pk=pk)
        try:
            amount = Decimal(request.POST.get("amount") or "0")
        except InvalidOperation:
            amount = Decimal(0)
        if amount <= 0:
            messages.error(request, "Enter a positive top-up amount.")
            return redirect("advance_detail", pk=pk)
        try:
            d = dt.date.fromisoformat(request.POST.get("date"))
        except (TypeError, ValueError):
            d = dt.date.today()
        if _block_if_locked(request, d):
            return redirect("advance_detail", pk=pk)
        if adv.from_petty_cash:
            avail = _petty_balance_asof(d)
            if amount > avail:
                messages.error(request, f"The petty-cash float is only KSh {avail:,.2f} "
                    f"on {d:%d %b %Y}; top it up before issuing more.")
                return redirect("advance_detail", pk=pk)
        AdvanceTopUp.objects.create(advance=adv, date=d, amount=amount,
            note=(request.POST.get("note") or "")[:200], issued_by=request.user)
        adv.amount = (adv.amount or Decimal(0)) + amount
        if adv.status == StaffAdvance.Status.SETTLED:
            adv.status = StaffAdvance.Status.PARTLY   # reopened by fresh cash
        adv.save(update_fields=["amount", "status"])
        messages.success(request, f"Topped up by KSh {amount:,.2f}. New advance total "
            f"KSh {adv.amount:,.2f}.")
        return redirect("advance_detail", pk=pk)


class AdvanceTopUpReverseView(TreasurerRequiredMixin, View):
    """Reverse a top-up on an advance: remove the top-up, decrement the advance
    total, and restore the source (the petty-cash float recovers automatically
    once the top-up no longer counts as outstanding)."""
    def post(self, request, pk, topup_id):
        from decimal import Decimal
        from .models import StaffAdvance, AdvanceTopUp
        adv = get_object_or_404(StaffAdvance, pk=pk)
        tu = get_object_or_404(AdvanceTopUp, pk=topup_id, advance=adv)
        if _block_if_locked(request, tu.date):
            return redirect("advance_detail", pk=pk)
        amount = tu.amount
        adv.amount = max((adv.amount or Decimal(0)) - amount, Decimal(0))
        adv.save(update_fields=["amount"])
        tu.delete()
        messages.success(request, f"Top-up of KSh {amount:,.2f} reversed. "
            f"New advance total KSh {adv.amount:,.2f}"
            + (" — the petty-cash float has been restored." if adv.from_petty_cash
               else "."))
        return redirect("advance_detail", pk=pk)


class AdvanceClose(TreasurerRequiredMixin, View):
    def post(self, request, pk):
        from decimal import Decimal, InvalidOperation
        from .models import StaffAdvance
        adv = get_object_or_404(StaffAdvance, pk=pk)
        if adv.from_petty_cash:
            try:
                ret = Decimal(request.POST.get("returned_to_petty") or "0")
            except InvalidOperation:
                ret = Decimal(0)
            if ret > 0:
                adv.returned_to_petty = ret
        adv.status = StaffAdvance.Status.CLOSED
        adv.settled_on = dt.date.today()
        adv.note = (request.POST.get("note") or adv.note)[:1000]
        adv.save(update_fields=["status", "settled_on", "note", "returned_to_petty"])
        messages.success(request, "Advance closed." + (
            " Returned cash credited back to petty cash." if adv.from_petty_cash
            and adv.returned_to_petty else ""))
        return redirect("advance_detail", pk=pk)


class ExpenseCategoryList(TreasurerRequiredMixin, View):
    template_name = "cashbook/categories.html"

    def get(self, request):
        from .models import ExpenseCategory, Expense
        return render(request, self.template_name, {
            "builtin": Expense.Category.choices,
            "custom": ExpenseCategory.objects.all()})

    def post(self, request):
        from .models import ExpenseCategory
        action = request.POST.get("action")
        if action == "add":
            label = (request.POST.get("label") or "").strip()
            code = (request.POST.get("code") or "").strip().upper().replace(" ", "_")[:20]
            if label and code:
                ExpenseCategory.objects.get_or_create(code=code, defaults={"label": label})
                messages.success(request, f"Added category “{label}”.")
            else:
                messages.error(request, "Both a code and a label are required.")
        elif action == "toggle":
            ec = ExpenseCategory.objects.filter(pk=request.POST.get("id")).first()
            if ec:
                ec.active = not ec.active
                ec.save(update_fields=["active"])
                messages.success(request, f"“{ec.label}” is now {'active' if ec.active else 'inactive'}.")
        return redirect("expense_categories")


class ExpenseRecategorizeView(TreasurerRequiredMixin, View):
    """Download all expenses as a spreadsheet for offline editing, then re-import
    to update the category and/or the expenditure type (capital vs recurrent).
    The round-trip is keyed on the expense ID; amounts, dates, descriptions and
    other fields in the file are ignored, so a treasurer can fix many
    mis-categorised expenses quickly without risking the rest of the ledger."""
    template_name = "cashbook/recategorize.html"

    def get(self, request):
        if request.GET.get("download"):
            return self._download(request)
        # show the upload form + the valid category list
        return render(request, self.template_name, {
            "categories": Expense.Category.choices})

    def _download(self, request):
        import io
        import openpyxl
        from openpyxl.styles import Font, PatternFill
        from django.http import HttpResponse
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Expenses"
        head = ["ID", "Date", "Description", "Fund", "Amount",
                "Current category", "New category (edit this)",
                "Current type", "New type (capital/recurrent)"]
        ws.append(head)
        for c in range(1, len(head) + 1):
            ws.cell(1, c).font = Font(bold=True, color="FFFFFF")
            ws.cell(1, c).fill = PatternFill("solid", fgColor="1F5F4F")
        # a reference sheet of valid category codes
        ref = wb.create_sheet("Valid categories")
        ref.append(["Code", "Label"])
        ref.cell(1, 1).font = Font(bold=True); ref.cell(1, 2).font = Font(bold=True)
        for code, label in Expense.Category.choices:
            ref.append([code, label])
        ref.append([])
        ref.append(["Expenditure type", ""])
        for code, label in Expense.ExpenditureType.choices:
            ref.append([code, label])
        for x in (Expense.objects.select_related("department")
                  .order_by("-date", "-id")):
            ws.append([x.id, x.date.isoformat(), x.description,
                       x.department.name if x.department_id else "",
                       float(x.amount), x.get_category_display(),
                       x.get_category_display(),
                       x.get_expenditure_type_display() if x.expenditure_type else "",
                       x.get_expenditure_type_display() if x.expenditure_type else ""])
        ws.column_dimensions["C"].width = 34
        ws.column_dimensions["F"].width = 20
        ws.column_dimensions["G"].width = 22
        ws.column_dimensions["I"].width = 24
        buf = io.BytesIO(); wb.save(buf)
        resp = HttpResponse(buf.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resp["Content-Disposition"] = 'attachment; filename="expenses_to_recategorize.xlsx"'
        return resp

    @db_tx.atomic
    def post(self, request):
        import openpyxl
        f = request.FILES.get("file")
        if not f:
            messages.error(request, "Choose the edited spreadsheet to upload.")
            return redirect("expense_recategorize")
        try:
            wb = openpyxl.load_workbook(f, data_only=True)
        except Exception:
            from core.utils import log_exception as _lx; _lx('cashbook/views.py')
            messages.error(request, "Could not read that file — is it the .xlsx you downloaded?")
            return redirect("expense_recategorize")
        ws = wb["Expenses"] if "Expenses" in wb.sheetnames else wb.active

        # build label -> code and code -> code lookups so the file can carry
        # either the human label ("Transport") or the raw code ("TRANSPORT")
        by_label = {lbl.lower(): code for code, lbl in Expense.Category.choices}
        by_code = {code.upper(): code for code, _ in Expense.Category.choices}
        type_by_label = {lbl.lower(): code for code, lbl in Expense.ExpenditureType.choices}
        type_by_code = {code.upper(): code for code, _ in Expense.ExpenditureType.choices}

        header = [str(c.value).strip().lower() if c.value else "" for c in ws[1]]
        try:
            id_col = header.index("id")
            new_col = header.index("new category (edit this)")
        except ValueError:
            messages.error(request, "That doesn't look like the recategorise template "
                                    "(missing the ID or New category column).")
            return redirect("expense_recategorize")
        # optional type column (older templates won't have it)
        type_col = next((i for i, h in enumerate(header)
                         if h.startswith("new type")), None)

        updated = unchanged = bad = missing = typed = 0
        bad_rows = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            if id_col >= len(row) or row[id_col] in (None, ""):
                continue
            try:
                eid = int(row[id_col])
            except (TypeError, ValueError):
                continue
            exp = Expense.objects.filter(pk=eid).first()
            if not exp:
                missing += 1
                continue
            dirty = []
            # category
            raw = row[new_col] if new_col < len(row) else None
            if raw not in (None, ""):
                key = str(raw).strip()
                code = by_code.get(key.upper()) or by_label.get(key.lower())
                if not code:
                    bad += 1
                    if len(bad_rows) < 10:
                        bad_rows.append(f"#{eid}: “{key}”")
                elif exp.category != code:
                    exp.category = code
                    dirty.append("category")
            # expenditure type (capital / recurrent)
            if type_col is not None and type_col < len(row):
                traw = row[type_col]
                if traw not in (None, ""):
                    tkey = str(traw).strip()
                    tcode = type_by_code.get(tkey.upper()) or type_by_label.get(tkey.lower())
                    if not tcode:
                        bad += 1
                        if len(bad_rows) < 10:
                            bad_rows.append(f"#{eid}: type “{tkey}”")
                    elif exp.expenditure_type != tcode:
                        exp.expenditure_type = tcode
                        dirty.append("expenditure_type")
            if dirty:
                exp.save(update_fields=dirty)
                updated += 1 if "category" in dirty else 0
                typed += 1 if "expenditure_type" in dirty else 0
            else:
                unchanged += 1

        parts = []
        if updated:
            parts.append(f"{updated} re-categorised")
        if typed:
            parts.append(f"{typed} type changed")
        if not parts:
            parts.append("0 changed")
        if unchanged:
            parts.append(f"{unchanged} unchanged")
        if missing:
            parts.append(f"{missing} not found")
        if bad:
            parts.append(f"{bad} with an unrecognised value")
        msg = ", ".join(parts) + "."
        if bad_rows:
            msg += " Unrecognised: " + "; ".join(bad_rows)
        (messages.success if (updated or typed) else messages.warning)(request, msg)
        return redirect("expense_list")


# ===========================================================================
# Bulk expense import (item 5)
# ===========================================================================
class ExpenseImportView(DataEntryRequiredMixin, View):
    """Bulk-load expenses from a spreadsheet: date, fund, description, amount,
    category, method, claimant, voucher. Honours the approval setting (auto-approve
    when approval isn't required, otherwise lands as pending)."""
    template_name = "cashbook/import.html"

    CATEGORY_LABELS = {c.label.upper(): c.value for c in Expense.Category}
    CATEGORY_LABELS.update({c.value: c.value for c in Expense.Category})
    # friendly aliases
    CATEGORY_LABELS.update({
        "ALLOWANCE": "ALLOWANCE", "HONORARIA": "ALLOWANCE", "TRANSPORT": "TRANSPORT",
        "FARE": "TRANSPORT", "REFRESHMENTS": "REFRESHMENTS", "CATERING": "REFRESHMENTS",
        "FOOD": "REFRESHMENTS", "MATERIALS": "MATERIALS", "SUPPLIES": "MATERIALS",
        "STATIONERY": "STATIONERY", "PRINTING": "STATIONERY", "UTILITIES": "UTILITIES",
        "POWER": "UTILITIES", "WATER": "UTILITIES", "MAINTENANCE": "MAINTENANCE",
        "REPAIRS": "MAINTENANCE", "CONSTRUCTION": "CONSTRUCTION", "EVANGELISM": "EVANGELISM",
        "MISSION": "EVANGELISM", "BENEVOLENCE": "BENEVOLENCE", "WELFARE": "BENEVOLENCE",
        "BANK CHARGE": "BANK_CHARGE", "BANK CHARGES": "BANK_CHARGE", "CHARGES": "BANK_CHARGE",
        "REMITTANCE": "REMITTANCE", "OTHER": "OTHER",
    })
    METHOD_LABELS = {"CASH": "CASH", "BANK": "BANK", "CHEQUE": "CHEQUE", "CHECK": "CHEQUE",
                     "MPESA": "MPESA", "M-PESA": "MPESA", "MOBILE": "MPESA"}

    def get(self, request):
        if request.GET.get("download"):
            return self._download()
        return render(request, self.template_name, {"stage": "upload"})

    def post(self, request):
        if request.POST.get("apply"):
            return self._apply(request)
        return self._parse(request)

    def _download(self):
        import io, openpyxl
        from openpyxl.styles import Font, PatternFill
        from openpyxl.worksheet.datavalidation import DataValidation
        from django.http import HttpResponse
        from departments.models import Department
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Expenses"
        head = ["Date", "Fund", "Description", "Amount", "Category", "Method",
                "Claimant", "Voucher no", "M-Pesa charge", "Paid from petty cash"]
        ws.append(head)
        for c in range(1, len(head) + 1):
            ws.cell(1, c).font = Font(bold=True, color="FFFFFF")
            ws.cell(1, c).fill = PatternFill("solid", fgColor="1F5F4F")
        ws.append(["2026-06-06", "LCB", "Pulpit microphone", 4500, "Materials",
                   "Cash", "J. Mwangi", "V-001", "", ""])
        ws.append(["2026-06-07", "YOUTH", "Bus fare for rally", 2000, "Transport",
                   "M-Pesa", "S. Achieng", "V-002", 30, "Yes"])
        ref = wb.create_sheet("Lists")
        ref["A1"] = "Funds"; ref["A1"].font = Font(bold=True)
        funds = list(Department.objects.filter(active=True, selectable=True).order_by("name"))
        for i, d in enumerate(funds, start=2):
            ref.cell(i, 1, d.name)
        ref["B1"] = "Category"; ref["B1"].font = Font(bold=True)
        cats = [c.label for c in Expense.Category]
        for i, c in enumerate(cats, start=2):
            ref.cell(i, 2, c)
        ref["C1"] = "Method"; ref["C1"].font = Font(bold=True)
        for i, m in enumerate([mm.label for mm in Expense.Method], start=2):
            ref.cell(i, 3, m)
        nrows = 500
        if funds:
            dv = DataValidation(type="list", formula1=f"=Lists!$A$2:$A${len(funds)+1}", allow_blank=True)
            ws.add_data_validation(dv); dv.add(f"B2:B{nrows}")
        dvc = DataValidation(type="list", formula1=f"=Lists!$B$2:$B${len(cats)+1}", allow_blank=True)
        ws.add_data_validation(dvc); dvc.add(f"E2:E{nrows}")
        dvm = DataValidation(type="list", formula1="=Lists!$C$2:$C$5", allow_blank=True)
        ws.add_data_validation(dvm); dvm.add(f"F2:F{nrows}")
        ws.column_dimensions["C"].width = 30
        ws.column_dimensions["B"].width = 18
        info = wb.create_sheet("How to fill this in")
        for i, line in enumerate([
            "Expense import",
            "",
            "One row per expense.",
            "  - Date — YYYY-MM-DD (required).",
            "  - Fund — the fund charged (pick from the list, required).",
            "  - Description — what it was for (required).",
            "  - Amount — required, > 0.",
            "  - Category — Allowance / Transport / Materials / ... (defaults to Other).",
            "  - Method — Cash / Bank / Cheque / M-Pesa (defaults to Cash).",
            "  - Claimant — who was paid (optional).",
            "  - Voucher no — optional reference.",
            "  - M-Pesa charge — optional. The transaction/withdrawal charge for this",
            "    payment; it's recorded as a separate bank-charge expense on the same",
            "    fund and linked back to this expense.",
            "  - Paid from petty cash — optional Yes/No. If Yes, the expense (and any",
            "    charge) is paid from the petty cash float and reduces it.",
            "",
            "If approval is required, imported expenses arrive as Pending for you to",
            "approve. If it isn't, they are approved automatically.",
        ], start=1):
            info.cell(i, 1, line)
        info.column_dimensions["A"].width = 74
        buf = io.BytesIO(); wb.save(buf)
        resp = HttpResponse(buf.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resp["Content-Disposition"] = 'attachment; filename="expense_import_template.xlsx"'
        return resp

    def _parse(self, request):
        import openpyxl, datetime as _dt
        from departments.models import Department
        f = request.FILES.get("file")
        if not f:
            messages.error(request, "Choose a spreadsheet to upload.")
            return redirect("expense_import")
        try:
            wb = openpyxl.load_workbook(f, data_only=True)
        except Exception:
            from core.utils import log_exception as _lx; _lx('cashbook/views.py')
            messages.error(request, "Could not read that file — please upload a .xlsx.")
            return redirect("expense_import")
        ws = wb["Expenses"] if "Expenses" in wb.sheetnames else wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            messages.error(request, "The sheet is empty.")
            return redirect("expense_import")
        header = [str(c).strip().lower() if c is not None else "" for c in rows[0]]

        def col(*names):
            for n in names:
                if n in header:
                    return header.index(n)
            return None
        c_date = col("date")
        c_fund = col("fund", "department")
        c_desc = col("description", "details", "expense")
        c_amt = col("amount", "value")
        c_cat = col("category")
        c_method = col("method", "paid by", "payment")
        c_claim = col("claimant", "payee", "paid to")
        c_vouch = col("voucher no", "voucher", "ref")
        c_charge = col("m-pesa charge", "mpesa charge", "charge", "transaction charge")
        c_petty = col("paid from petty cash", "petty cash", "petty")
        if c_date is None or c_amt is None or c_desc is None:
            messages.error(request, "Need at least Date, Description and Amount columns "
                                    "— please use the template.")
            return redirect("expense_import")

        funds = {d.name.strip().lower(): d for d in Department.objects.all()}

        def cell(r, idx):
            if idx is None or idx >= len(r) or r[idx] in (None, ""):
                return ""
            return str(r[idx]).strip()

        def pdate(v):
            if isinstance(v, _dt.datetime):
                return v.date().isoformat()
            if isinstance(v, _dt.date):
                return v.isoformat()
            for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%m/%d/%Y"):
                try:
                    return _dt.datetime.strptime(str(v).strip(), fmt).date().isoformat()
                except (ValueError, TypeError):
                    continue
            return None

        plan = []
        for r in rows[1:]:
            desc = cell(r, c_desc)
            d = pdate(r[c_date]) if c_date < len(r) else None
            try:
                amt = float(r[c_amt]) if c_amt < len(r) and r[c_amt] not in (None, "") else 0.0
            except (TypeError, ValueError):
                amt = 0.0
            if not desc and not d and amt <= 0:
                continue
            fund_raw = cell(r, c_fund)
            fund = funds.get(fund_raw.lower()) if fund_raw else None
            cat = self.CATEGORY_LABELS.get(cell(r, c_cat).upper(), "OTHER")
            method = self.METHOD_LABELS.get(cell(r, c_method).upper(), "CASH")
            try:
                charge = (float(r[c_charge]) if c_charge is not None and c_charge < len(r)
                          and r[c_charge] not in (None, "") else 0.0)
            except (TypeError, ValueError):
                charge = 0.0
            petty_raw = cell(r, c_petty) if c_petty is not None else ""
            petty = str(petty_raw).strip().lower() in ("yes", "y", "true", "1", "x")
            plan.append({
                "date": d, "fund_raw": fund_raw,
                "fund_id": fund.id if fund else None,
                "fund_name": fund.name if fund else None,
                "description": desc[:200], "amount": amt,
                "category": cat, "method": method, "charge": round(charge, 2),
                "petty": petty,
                "claimant": cell(r, c_claim)[:120], "voucher": cell(r, c_vouch)[:30],
                "ok": bool(d and desc and amt > 0 and fund),
            })
        if not plan:
            messages.error(request, "No expense rows were found.")
            return redirect("expense_import")
        request.session["expense_import_plan"] = plan
        from core.models import SiteConfig
        return render(request, self.template_name, {
            "stage": "review", "plan": plan,
            "ready": sum(1 for p in plan if p["ok"]),
            "problems": sum(1 for p in plan if not p["ok"]),
            "total": sum(p["amount"] for p in plan if p["ok"]),
            "auto_approve": not SiteConfig.get().require_expense_approval,
        })

    @db_tx.atomic
    def _apply(self, request):
        from departments.models import Department
        from core.models import SiteConfig
        from core.utils import sabbath_of
        plan = request.session.get("expense_import_plan")
        if not plan:
            messages.error(request, "Your import session expired — please upload again.")
            return redirect("expense_import")
        auto = not SiteConfig.get().require_expense_approval
        created = skipped = 0
        for p in plan:
            if not p["ok"]:
                skipped += 1
                continue
            fund = Department.objects.filter(pk=p["fund_id"]).first()
            if not fund:
                skipped += 1
                continue
            from decimal import Decimal as D
            import datetime as _dt
            d = _dt.date.fromisoformat(p["date"])
            petty = bool(p.get("petty"))
            exp = Expense(
                date=d, department=fund, description=p["description"],
                amount=D(str(p["amount"])), category=p["category"],
                method=p["method"], claimant=p["claimant"], voucher_no=p["voucher"],
                paid_from_petty_cash=petty, recorded_by=request.user,
                sabbath_week=sabbath_week_of(d))
            if petty:
                # paid out of the float already — record as paid
                exp.status = Expense.Status.PAID
                exp.paid_date = d
                exp.approved_by = request.user
            elif auto:
                exp.status = Expense.Status.APPROVED
                exp.approved_by = request.user
            exp.save()
            created += 1
            # optional M-Pesa / bank charge -> separate bank-charge expense, linked
            charge = D(str(p.get("charge") or 0))
            if charge > 0:
                ref = exp.voucher_no or f"exp #{exp.id}"
                Expense.objects.create(
                    date=d, sabbath_week=exp.sabbath_week, department=fund,
                    description=f"Transaction charge — {exp.description} [for {ref}]"[:200],
                    amount=charge, category=Expense.Category.BANK_CHARGE,
                    method=exp.method, claimant=exp.claimant, voucher_no=exp.voucher_no,
                    paid_from_petty_cash=petty, recorded_by=request.user, charge_for=exp,
                    status=exp.status, paid_date=exp.paid_date,
                    approved_by=exp.approved_by)
                created += 1
        request.session.pop("expense_import_plan", None)
        state = "approved" if auto else "pending approval"
        parts = [f"{created} expense(s) imported ({state})"]
        if skipped:
            parts.append(f"{skipped} row(s) skipped (missing date, fund, description or amount)")
        messages.success(request, ", ".join(parts) + ".")
        return redirect("expense_list")


class ExpenseBulkActionView(TreasurerRequiredMixin, View):
    """Apply one action (approve / reject / pay / delete) to several selected
    expenses at once. Each item is checked against the same guards as the single
    action, and ones that don't qualify are skipped (and counted) rather than
    erroring the whole batch."""
    def post(self, request):
        import datetime as dt
        from django.shortcuts import redirect
        from core.models import SiteConfig
        ids = request.POST.getlist("ids")
        action = request.POST.get("action")
        if not ids:
            messages.info(request, "No expenses were selected.")
            return redirect("expense_list")
        threshold = SiteConfig.get().dual_approval_threshold or 0
        from core.models import period_locked
        done = skipped = 0
        S = Expense.Status
        for exp in Expense.objects.filter(pk__in=ids):
            if period_locked(exp.date):
                skipped += 1
                continue
            if action == "approve" and exp.status == S.PENDING:
                exp.status = S.APPROVED
                exp.approved_by = request.user
                exp.save()
                done += 1
            elif action == "reject" and exp.status in (S.PENDING, S.APPROVED):
                exp.status = S.REJECTED
                exp.approved_by = request.user
                exp.save()
                done += 1
            elif action == "pay" and exp.status == S.APPROVED:
                needs_two = threshold and exp.amount >= threshold
                if needs_two and not (exp.approved_by_id and exp.second_approved_by_id):
                    skipped += 1
                    continue
                exp.status = S.PAID
                exp.paid_date = dt.date.today()
                exp.save()
                done += 1
            elif action == "delete":
                exp.delete()
                done += 1
            else:
                skipped += 1
        verb = {"approve": "approved", "reject": "rejected", "pay": "marked paid",
                "delete": "deleted"}.get(action, "updated")
        msg = f"{done} expense(s) {verb}."
        if skipped:
            msg += f" {skipped} skipped (locked period or not eligible)."
        (messages.success if done else messages.info)(request, msg)
        return redirect("expense_list")


class FundBudgetView(TreasurerRequiredMixin, View):
    """Budget & goals for a fund (e.g. Camp Meeting): per-category budget vs actual
    spend for a year, plus the contribution goal (Department.target) and the
    yearly goal (Department.annual_budget) tracked against what's been collected."""
    template_name = "cashbook/fund_budget.html"

    def _ctx(self, request, dept):
        import datetime as dt
        from decimal import Decimal
        from django.db.models import Sum
        from giving.models import Transaction
        from .models import BudgetLine, Expense
        year = int(request.GET.get("year") or dt.date.today().year)

        lines = list(BudgetLine.objects.filter(department=dept, year=year))
        # actual spend per budget item, from expenses tagged to that item
        spend = {r["budget_line"]: r["t"] for r in (Expense.objects.filter(
            budget_line__in=lines,
            status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
            .values("budget_line").annotate(t=Sum("amount")))}
        # spend on this fund not tagged to any item (so nothing is hidden)
        untagged = (Expense.objects.filter(department=dept, date__year=year,
            budget_line__isnull=True,
            status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
            .aggregate(t=Sum("amount"))["t"] or Decimal(0))
        rows, tot_budget, tot_actual = [], Decimal(0), Decimal(0)
        for b in lines:
            actual = spend.get(b.id, Decimal(0))
            tot_budget += b.amount; tot_actual += actual
            rows.append({"id": b.id, "name": b.name,
                         "category": b.get_category_display() if b.category else "",
                         "budget": b.amount, "actual": actual,
                         "variance": b.amount - actual,
                         "pct": int(min(actual / b.amount * 100, 999)) if b.amount else 0,
                         "note": b.note})

        # --- collections aggregated across this fund AND its sub-accounts ---
        def _fund_ids(d):
            ids = [d.id]
            for sub in d.subgroups.all():
                ids.extend(_fund_ids(sub))
            return ids

        def _collected(fund, year_):
            if fund is None:
                return Decimal(0)
            return (Transaction.objects.filter(
                department_id__in=_fund_ids(fund),
                direction=Transaction.Direction.CREDIT, confirmed=True,
                is_reversal=False, is_reversed=False, excluded_from_income=False,
                date__year=year_).aggregate(t=Sum("amount"))["t"] or Decimal(0))

        # Camp Meeting EXPENSE goal: collected across the expense fund + subgroups
        expense_collected = _collected(dept, year)

        def _goal(g, collected):
            g = g or Decimal(0)
            return {"goal": g, "collected": collected,
                    "pct": int(min(collected / g * 100, 100)) if g else 0,
                    "short": max(g - collected, Decimal(0)),
                    "variance": collected - g}

        # Group Contribution goal: one goal set on the parent, progress from each
        # sub-account's own collection (shown per group, plus the aggregate)
        group_rows = []
        for sub in dept.subgroups.all():
            c = _collected(sub, year)
            g = sub.contribution_goal or Decimal(0)
            group_rows.append({"name": sub.name, "id": sub.id, "collected": c,
                "goal": g, "pct": int(min(c / g * 100, 100)) if g else 0,
                "short": max(g - c, Decimal(0))})
        group_rows.sort(key=lambda r: r["collected"], reverse=True)
        contribution_collected = sum((r["collected"] for r in group_rows), Decimal(0))
        contribution_target = sum((r["goal"] for r in group_rows), Decimal(0))

        # Camp Meeting OFFERING goal: a SEPARATE trust fund, never merged here
        offering = dept.offering_fund
        offering_collected = _collected(offering, year)

        return {
            "dept": dept, "year": year,
            "years": range(dt.date.today().year + 1, dt.date.today().year - 4, -1),
            "rows": rows, "tot_budget": tot_budget, "tot_actual": tot_actual,
            "tot_variance": tot_budget - tot_actual, "untagged": untagged,
            "expense_goal": _goal(dept.year_goal, expense_collected),
            "contribution_goal": _goal(contribution_target, contribution_collected),
            "group_rows": group_rows,
            "offering": offering,
            "offering_goal": _goal(dept.offering_goal, offering_collected),
            "categories": Expense.Category.choices,
            "is_camp_expense": dept.goal_type == "CAMP_EXPENSE",
            "goal_type": dept.goal_type,
            "all_funds": Department.objects.filter(active=True, is_trust=True)
                         .exclude(pk=dept.pk).order_by("name"),
        }

    def get(self, request, pk):
        from departments.models import Department
        dept = get_object_or_404(Department, pk=pk)
        return render(request, self.template_name, self._ctx(request, dept))

    def post(self, request, pk):
        import datetime as dt
        from decimal import Decimal, InvalidOperation
        from departments.models import Department
        from .models import BudgetLine
        dept = get_object_or_404(Department, pk=pk)
        year = int(request.POST.get("year") or dt.date.today().year)
        # update the fund's goals
        if "save_goals" in request.POST:
            def _dec(name):
                try:
                    v = request.POST.get(name, "").strip()
                    return Decimal(v) if v else None
                except InvalidOperation:
                    return None
            dept.year_goal = _dec("expense_goal")          # Camp Meeting Expense goal
            gt = request.POST.get("goal_type") or "NONE"
            dept.goal_type = gt if gt in ("NONE", "CAMP_EXPENSE") else "NONE"
            # the offering goal applies only to a Camp Meeting Expense fund
            if dept.goal_type == "CAMP_EXPENSE":
                dept.offering_goal = _dec("offering_goal")
                of_id = request.POST.get("offering_fund") or ""
                dept.offering_fund = (Department.objects.filter(pk=of_id).first()
                                      if of_id.isdigit() else None)
            else:
                dept.offering_goal = None
                dept.offering_fund = None
            dept.save(update_fields=["year_goal", "offering_goal",
                                     "offering_fund", "goal_type"])
            # each development group has its own contribution goal
            for sub in dept.subgroups.all():
                val = _dec(f"group_goal_{sub.id}")
                if sub.contribution_goal != val:
                    sub.contribution_goal = val
                    sub.save(update_fields=["contribution_goal"])
            messages.success(request, "Goals updated.")
            return redirect(f"{request.path}?year={year}")
        # add / update a named budget item
        name = (request.POST.get("name") or "").strip()
        try:
            amount = Decimal(request.POST.get("amount") or "0")
        except InvalidOperation:
            amount = Decimal(0)
        if name:
            BudgetLine.objects.update_or_create(
                department=dept, year=year, name=name,
                defaults={"amount": amount, "category": request.POST.get("category", ""),
                          "note": request.POST.get("note", "")[:120]})
            messages.success(request, "Budget item saved.")
        return redirect(f"{request.path}?year={year}")


class BudgetItemsJSONView(DataEntryRequiredMixin, View):
    """Budget items for a fund + year, for the expense form's 'Budget item'
    picker. Returns [] for funds that have no budget set."""
    def get(self, request):
        import datetime as dt
        from django.http import JsonResponse
        from .models import BudgetLine
        try:
            dept_id = int(request.GET.get("dept") or 0)
        except ValueError:
            dept_id = 0
        year = request.GET.get("year")
        try:
            year = int(year) if year else dt.date.today().year
        except ValueError:
            year = dt.date.today().year
        items = (BudgetLine.objects.filter(department_id=dept_id, year=year)
                 .order_by("name"))
        return JsonResponse({"items": [
            {"id": b.id, "name": b.name, "category": b.category,
             "amount": float(b.amount)} for b in items]})


# --- Payables / accruals / prepayments: edit, delete, settle-against-expense ---

class _ObligationEditView(DataEntryRequiredMixin, View):
    """Edit a payable/accrual/prepayment. Settled items are read-only (safe)."""
    model = None
    form_class = None
    title = ""
    template_name = "cashbook/obligation_edit.html"

    def _get(self, pk):
        return get_object_or_404(self.model, pk=pk)

    def get(self, request, pk):
        obj = self._get(pk)
        if getattr(obj, "settled", False):
            messages.error(request, "This item is settled and can no longer be edited.")
            return redirect("accruals")
        fields = self.form_class().fields
        initial = {f: getattr(obj, f) for f in fields if hasattr(obj, f)}
        return render(request, self.template_name,
                      {"form": self.form_class(initial=initial), "title": self.title, "obj": obj})

    def post(self, request, pk):
        obj = self._get(pk)
        if getattr(obj, "settled", False):
            messages.error(request, "This item is settled and can no longer be edited.")
            return redirect("accruals")
        form = self.form_class(request.POST)
        if form.is_valid():
            if _block_if_locked(request, form.cleaned_data.get("date", dt.date.today())):
                return redirect("accruals")
            for f, v in form.cleaned_data.items():
                if hasattr(obj, f):
                    setattr(obj, f, v)
            obj.save()
            messages.success(request, "Saved.")
            return redirect("accruals")
        return render(request, self.template_name,
                      {"form": form, "title": self.title, "obj": obj})


class PayableEdit(_ObligationEditView):
    model = Payable; form_class = PayableForm; title = "Edit payable"

class AccrualEdit(_ObligationEditView):
    model = Accrual; form_class = AccrualForm; title = "Edit accrual"

class PrepaymentEdit(_ObligationEditView):
    model = Prepayment; form_class = PrepaymentForm; title = "Edit prepayment"


class _ObligationDeleteView(DataEntryRequiredMixin, View):
    model = None
    label = "item"
    def post(self, request, pk):
        obj = get_object_or_404(self.model, pk=pk)
        if getattr(obj, "settled", False):
            messages.error(request, f"A settled {self.label} can't be deleted. "
                                    f"Reverse the settlement first if it was a mistake.")
        else:
            obj.delete()
            messages.success(request, f"{self.label.capitalize()} deleted.")
        return redirect("accruals")


class PayableDelete(_ObligationDeleteView):
    model = Payable; label = "payable"

class AccrualDelete(_ObligationDeleteView):
    model = Accrual; label = "accrual"

class PrepaymentDelete(_ObligationDeleteView):
    model = Prepayment; label = "prepayment"


class SettleAgainstExpenseView(DataEntryRequiredMixin, View):
    """Settle a payable/accrual by linking an expense that was already entered
    (e.g. the treasurer keyed the payment as an expense by mistake), instead of
    creating a second one. Lists unlinked expenses on the same fund to choose from."""
    template_name = "cashbook/settle_existing.html"

    def _obj(self, kind, pk):
        model = {"payable": Payable, "accrual": Accrual}.get(kind)
        return model.objects.filter(pk=pk).first() if model else None

    def get(self, request, kind, pk):
        obj = self._obj(kind, pk)
        if not obj or obj.settled:
            messages.error(request, "That item can't be settled.")
            return redirect("accruals")
        cands = (Expense.objects.filter(department=obj.department,
                    payable__isnull=True, accrual__isnull=True)
                 .exclude(category=Expense.Category.BANK_CHARGE)
                 .order_by("-date")[:100])
        return render(request, self.template_name,
                      {"obj": obj, "kind": kind, "candidates": cands})

    def post(self, request, kind, pk):
        obj = self._obj(kind, pk)
        if not obj or obj.settled:
            messages.error(request, "That item can't be settled.")
            return redirect("accruals")
        exp = get_object_or_404(Expense, pk=request.POST.get("expense"))
        obj.settled = True
        obj.settled_on = exp.date
        obj.settled_expense = exp
        obj.save()
        messages.success(request, f"{kind.title()} settled against the existing expense "
                                  f"“{exp.description}”.")
        return redirect("accruals")


def unpresented_cheques_total(as_of=None):
    """Total of cheques issued but not yet cleared (still unpresented at the bank)."""
    from decimal import Decimal
    from django.db.models import Sum
    from .models import PaymentInstrument
    qs = PaymentInstrument.objects.filter(
        method=PaymentInstrument.Method.CHEQUE,
        status__in=PaymentInstrument.OUTSTANDING_STATES)
    if as_of:
        qs = qs.filter(date_issued__lte=as_of)
    return qs.aggregate(t=Sum("amount"))["t"] or Decimal(0)


class ChequeRegisterView(ReadAccessMixin, View):
    """Payment register — cheques today, extensible to EFT/RTGS/M-Pesa. Lists
    payment instruments, filterable by method and status, and drives the full
    lifecycle (draft -> approved -> issued -> cleared, plus void/stop)."""
    template_name = "cashbook/payment_register.html"

    def get(self, request):
        from .models import PaymentInstrument
        from core.roles import is_treasurer
        status = request.GET.get("status", "")
        method = request.GET.get("method", "")
        qs = PaymentInstrument.objects.select_related(
            "expense", "remittance_batch", "refund", "transfer", "bank_account")
        if status:
            qs = qs.filter(status=status)
        if method:
            qs = qs.filter(method=method)
        ctx = {
            "cheques": qs[:500],
            "status": status, "method": method,
            "statuses": PaymentInstrument.Status.choices,
            "methods": PaymentInstrument.Method.choices,
            "source_kinds": PaymentInstrument.SourceKind.choices,
            "unpresented_total": unpresented_cheques_total(),
            "can_enter_data": is_treasurer(request.user)
                              or getattr(request.user, "is_superuser", False),
            "is_treasurer": is_treasurer(request.user)
                            or getattr(request.user, "is_superuser", False),
        }
        # deep-link prefill: e.g. from an expense's "Issue a payment" link
        for_kind = request.GET.get("for_kind", "")
        for_id = request.GET.get("for_id", "")
        if for_kind == "EXPENSE" and for_id.isdigit():
            exp = Expense.objects.filter(pk=for_id).first()
            if exp:
                ctx["prefill_kind"] = "EXPENSE"
                ctx["prefill_id"] = exp.id
                ctx["prefill_amount"] = exp.amount
                ctx["prefill_payee"] = exp.claimant or exp.department.name
        return render(request, self.template_name, ctx)

    def post(self, request):
        from decimal import Decimal, InvalidOperation
        import datetime as dt
        from django.core.exceptions import ValidationError
        from core.roles import is_treasurer
        from .models import (PaymentInstrument, Expense, RemittanceBatch,
                             ExpenseRefund, FundTransfer)
        action = request.POST.get("action")
        treas = is_treasurer(request.user) or getattr(request.user, "is_superuser", False)
        if not treas:
            messages.error(request, "You do not have permission to manage payments.")
            return redirect("payment_register")

        if action == "add":
            try:
                amount = Decimal(request.POST.get("amount") or "0")
            except InvalidOperation:
                amount = Decimal(0)
            try:
                issued = dt.date.fromisoformat(request.POST.get("date_issued"))
            except (TypeError, ValueError):
                issued = None
            kind = request.POST.get("source_kind") or "EXPENSE"
            src_id = (request.POST.get("source_id") or "").strip()
            inst = PaymentInstrument(
                method=request.POST.get("method") or "CHEQUE",
                instrument_number=(request.POST.get("instrument_number") or "")[:40],
                payee=(request.POST.get("payee") or "")[:160],
                amount=amount, date_issued=issued, source_kind=kind,
                signatory_1=(request.POST.get("signatory_1") or "")[:120],
                signatory_2=(request.POST.get("signatory_2") or "")[:120],
                note=(request.POST.get("note") or "")[:200],
                recorded_by=request.user, status="DRAFT")
            # attach the referenced source
            if kind == "EXPENSE" and src_id.isdigit():
                inst.expense = Expense.objects.filter(pk=src_id).first()
            elif kind == "REMITTANCE" and src_id.isdigit():
                inst.remittance_batch = RemittanceBatch.objects.filter(pk=src_id).first()
            elif kind == "REFUND" and src_id.isdigit():
                inst.refund = ExpenseRefund.objects.filter(pk=src_id).first()
            elif kind == "TRANSFER" and src_id.isdigit():
                inst.transfer = FundTransfer.objects.filter(pk=src_id).first()
            # manual / supplier payments need the standalone permission
            if kind in ("MANUAL", "SUPPLIER") and not treas:
                messages.error(request, "Standalone payments require treasurer rights.")
                return redirect("payment_register")
            try:
                inst.full_clean(exclude=["amount"] if amount <= 0 else None)
            except ValidationError as exc:
                msg = "; ".join(m for v in getattr(exc, "message_dict", {}).values()
                                for m in v) or "; ".join(exc.messages)
                messages.error(request, msg)
                return redirect("payment_register")
            inst.save()
            messages.success(request, f"Payment {inst.instrument_number or '(draft)'} recorded.")

        elif action in ("approve", "issue", "clear", "void", "stop"):
            inst = get_object_or_404(PaymentInstrument, pk=request.POST.get("pk"))
            if inst.is_locked and action != "clear":
                messages.error(request, "A cleared payment cannot be changed — void "
                                        "or reverse it instead.")
                return redirect("payment_register")
            if action == "approve":
                inst.approve(request.user)
            elif action == "issue":
                inst.issue()
            elif action == "clear":
                inst.clear()
            elif action == "void":
                inst.void()
            elif action == "stop":
                inst.stop()
            messages.success(request, f"Payment marked {inst.get_status_display()}.")

        elif action == "delete":
            inst = get_object_or_404(PaymentInstrument, pk=request.POST.get("pk"))
            if inst.is_locked:
                messages.error(request, "A cleared payment cannot be deleted — void it instead.")
            else:
                inst.delete()
                messages.success(request, "Payment removed.")

        elif action == "sync":
            created = self._sync_from_records(request.user)
            messages.success(request, f"Imported {created} payment(s) from existing "
                                      f"cheque expenses and remittances.")
        return redirect("payment_register")

    def _sync_from_records(self, user):
        """Create instrument entries for cheque-method expenses and cheque
        remittance batches that aren't in the register yet."""
        from .models import PaymentInstrument, Expense, RemittanceBatch
        have = set(PaymentInstrument.objects.exclude(instrument_number="")
                   .values_list("instrument_number", flat=True))
        created = 0
        for e in Expense.objects.filter(method=Expense.Method.CHEQUE).exclude(voucher_no=""):
            if e.voucher_no in have:
                continue
            PaymentInstrument.objects.create(
                method="CHEQUE", instrument_number=e.voucher_no[:40],
                payee=e.claimant or e.description[:160], amount=e.amount,
                date_issued=e.paid_date or e.date, status="OUTSTANDING",
                source_kind="EXPENSE", expense=e, recorded_by=user)
            have.add(e.voucher_no)
            created += 1
        for b in RemittanceBatch.objects.filter(payment__isnull=True).exclude(cheque_no=""):
            if b.cheque_no in have:
                continue
            inst = PaymentInstrument.objects.create(
                method="CHEQUE", instrument_number=b.cheque_no[:40],
                payee="Conference remittance", amount=b.total_amount,
                date_issued=b.cheque_date or b.date, status="OUTSTANDING",
                source_kind="REMITTANCE", remittance_batch=b, recorded_by=user)
            b.payment = inst
            b.save(update_fields=["payment"])
            have.add(b.cheque_no)
            created += 1
        return created



class ReceiptArchiveView(ReadAccessMixin, TemplateView):
    """Print-friendly archive of expense receipts for a period, grouped by month,
    plus a one-click ZIP download — so a year's supporting documents can be
    printed or filed together for audit."""
    template_name = "cashbook/receipt_archive.html"

    def get(self, request, *args, **kwargs):
        if request.GET.get("download") == "zip":
            return self._zip(request)
        return super().get(request, *args, **kwargs)

    def _attachments(self, request):
        from core.utils import parse_period
        from .models import ExpenseAttachment
        start, end = parse_period(request)
        qs = (ExpenseAttachment.objects.filter(
                  expense__date__gte=start, expense__date__lte=end)
              .select_related("expense", "expense__department")
              .order_by("expense__date"))
        return start, end, qs

    def get_context_data(self, **kwargs):
        from collections import OrderedDict
        ctx = super().get_context_data(**kwargs)
        start, end, qs = self._attachments(self.request)
        groups = OrderedDict()
        n_files = 0
        for a in qs:
            key = a.expense.date.strftime("%B %Y")
            groups.setdefault(key, []).append(a)
            if a.file:
                n_files += 1
        ctx.update({"start": start, "end": end, "groups": groups,
                    "count": qs.count(), "file_count": n_files})
        return ctx

    def _zip(self, request):
        import io
        import zipfile
        from django.http import HttpResponse
        start, end, qs = self._attachments(request)
        buf = io.BytesIO()
        added = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for a in qs:
                if not a.file:
                    continue
                try:
                    d = a.expense.date
                    base = a.file.name.split("/")[-1]
                    arc = f"{d:%Y}/{d:%m}/exp{a.expense_id}_{base}"
                    a.file.open("rb")
                    zf.writestr(arc, a.file.read())
                    a.file.close()
                    added += 1
                except Exception:  # noqa: BLE001
                    from core.utils import log_exception as _lx; _lx("receipt zip")
            # an index of text/links too
            lines = ["Expense receipts index", f"Period: {start} to {end}", ""]
            for a in qs:
                lines.append(f"{a.expense.date}  {a.expense.description}  "
                             f"{a.expense.amount}  "
                             + (a.file.name if a.file else (a.link or a.text or "")))
            zf.writestr("index.txt", "\n".join(lines))
        if not added:
            messages.info(request, "No uploaded receipt files in that period to download.")
            return redirect("receipt_archive")
        resp = HttpResponse(buf.getvalue(), content_type="application/zip")
        resp["Content-Disposition"] = (
            f'attachment; filename="receipts_{start}_{end}.zip"')
        return resp


def _amount_in_words(amount):
    """Render a KES amount in words for cheque printing."""
    from decimal import Decimal
    ones = ["", "one", "two", "three", "four", "five", "six", "seven", "eight",
            "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
            "sixteen", "seventeen", "eighteen", "nineteen"]
    tens = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
            "eighty", "ninety"]

    def under_1000(n):
        w = []
        if n >= 100:
            w.append(ones[n // 100] + " hundred")
            n %= 100
        if n >= 20:
            w.append(tens[n // 10])
            n %= 10
        if n:
            w.append(ones[n])
        return " ".join(w)

    amount = Decimal(amount)
    shillings = int(amount)
    cents = int((amount - shillings) * 100)
    if shillings == 0:
        words = "zero"
    else:
        parts, scale = [], [(1_000_000, "million"), (1_000, "thousand"), (1, "")]
        for div, name in scale:
            if shillings >= div:
                chunk = shillings // div
                parts.append(under_1000(chunk) + (f" {name}" if name else ""))
                shillings %= div
        words = " ".join(p for p in parts if p.strip())
    words = words.strip().capitalize() + " shillings"
    if cents:
        words += f" and {cents} cents"
    return words + " only"


class ChequePrintView(ReadAccessMixin, View):
    """Print-friendly cheque: payee, amount in figures and words, date,
    signatories. Method-agnostic but most useful for cheques."""
    def get(self, request, pk):
        from .models import PaymentInstrument
        inst = get_object_or_404(PaymentInstrument, pk=pk)
        return render(request, "cashbook/payment_print.html", {
            "c": inst, "amount_words": _amount_in_words(inst.amount)})


class ChequeOutstandingView(ReadAccessMixin, View):
    """Outstanding (unpresented) payments report with Excel/CSV export."""
    def get(self, request):
        from .models import PaymentInstrument
        from reports.exports import csv_response, xlsx_response
        qs = (PaymentInstrument.objects.filter(
                status__in=PaymentInstrument.OUTSTANDING_STATES)
              .select_related("expense", "remittance_batch")
              .order_by("date_issued"))
        method = request.GET.get("method", "")
        if method:
            qs = qs.filter(method=method)
        header = ["Method", "Number", "Payee", "Source", "Amount", "Date issued"]
        rows = [[c.get_method_display(), c.instrument_number, c.payee,
                 c.source_label, c.amount, c.date_issued] for c in qs]
        export = request.GET.get("export")
        if export == "csv":
            return csv_response("outstanding_payments", header, rows)
        if export == "xlsx":
            return xlsx_response("outstanding_payments", header, rows,
                                 title="Outstanding payments")
        total = sum((c.amount for c in qs), __import__("decimal").Decimal(0))
        return render(request, "cashbook/payment_outstanding.html", {
            "rows": qs, "total": total, "method": method,
            "methods": PaymentInstrument.Method.choices})
