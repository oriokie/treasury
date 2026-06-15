import datetime as dt

from django.conf import settings
from django.contrib import messages
from django.db import transaction as db_tx
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views.generic import ListView, CreateView, UpdateView, View

from django.views import View

def _block_if_locked(request, d):
    """Return True (and flash an error) if date d is in a locked period. No one —
    including superusers — may post into a locked period; it must be unlocked first."""
    from core.models import period_locked
    lock = period_locked(d)
    if lock:
        from django.contrib import messages as _m
        _m.error(request, f"{lock} is locked. Unlock the period (Controls) before posting or editing entries in it.")
        return True
    return False
from core.permissions import DataEntryRequiredMixin, ReadAccessMixin, TreasurerRequiredMixin
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


class ExpenseListView(ReadAccessMixin, ListView):
    model = Expense
    template_name = "cashbook/list.html"
    context_object_name = "expenses"
    paginate_by = 50

    def get_queryset(self):
        import datetime as dt
        qs = Expense.objects.select_related("department", "recorded_by").order_by("-date")
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
            header = ["Date", "Description", "Fund", "Category", "Type", "Status",
                      "Claimant", "Voucher", "Amount"]
            rows = [[x.date.isoformat(), x.description,
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
        auto = not SiteConfig.get().require_expense_approval
        if auto:
            exp.status = Expense.Status.APPROVED
            exp.approved_by = self.request.user
        exp.save()
        # optional M-Pesa / bank transaction charge -> separate bank-charge expense
        charge = form.cleaned_data.get("charge")
        if charge and charge > 0:
            Expense.objects.create(
                date=exp.date, sabbath_week=exp.sabbath_week, department=exp.department,
                description=f"Transaction charge — {exp.description}"[:200],
                amount=charge, category=Expense.Category.BANK_CHARGE,
                method=exp.method, recorded_by=self.request.user,
                status=exp.status,
                approved_by=self.request.user if auto else None)
        if exp.status == Expense.Status.PENDING:
            from core.services.notifications import notify
            from django.urls import reverse
            notify("APPROVAL",
                   f"Expense awaiting approval: {exp.description} — "
                   f"{exp.amount:,.2f} ({exp.department.name})",
                   link=reverse("expense_list"))
        messages.success(self.request, "Expense recorded.")
        return redirect(self.success_url)


class ExpenseUpdate(DataEntryRequiredMixin, UpdateView):
    model = Expense
    form_class = ExpenseForm
    template_name = "cashbook/form.html"
    success_url = reverse_lazy("expense_list")

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
            exp.approved_by = request.user
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
    topups = (PettyTopUp.objects.filter(date__lte=on)
              .aggregate(t=Sum("amount"))["t"] or Decimal(0))
    disb = (Expense.objects.filter(paid_from_petty_cash=True, date__lte=on,
            status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
            .aggregate(t=Sum("amount"))["t"] or Decimal(0))
    return topups - disb


class PettyCashView(ReadAccessMixin, TemplateView):
    template_name = "cashbook/petty_cash.html"

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
                if cd["amount"] > bal:
                    messages.error(request,
                        f"Insufficient petty cash float — {bal:,.2f} on hand, "
                        f"{cd['amount']:,.2f} requested. Top up the float first.")
                    return redirect("petty_cash")
            Expense.objects.create(
                date=cd["date"], sabbath_week=sabbath_week_of(cd["date"]),
                department=cd["department"], description=cd["description"], amount=cd["amount"],
                category=cd["category"], claimant=cd["claimant"], method=Expense.Method.CASH,
                paid_from_petty_cash=True, status=Expense.Status.PAID, paid_date=cd["date"],
                recorded_by=request.user, approved_by=request.user)
            messages.success(request, f"Petty cash disbursement of {cd['amount']} charged to "
                                      f"{cd['department'].name}.")
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
        return ctx


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
    from django.db.models import Sum
    qs = Payable.objects.filter(settled=False)
    if as_of:
        qs = qs.filter(date__lte=as_of)
    return qs.aggregate(t=Sum("amount"))["t"] or Decimal(0)


def open_accruals_total(as_of=None):
    from decimal import Decimal
    from django.db.models import Sum
    qs = Accrual.objects.filter(settled=False)
    if as_of:
        qs = qs.filter(date__lte=as_of)
    return qs.aggregate(t=Sum("amount"))["t"] or Decimal(0)


def unexpired_prepayments_total(as_of=None):
    from decimal import Decimal
    import datetime as _dt
    as_of = as_of or _dt.date.today()
    return sum((p.unexpired(as_of) for p in Prepayment.objects.all()), Decimal(0))


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


class AdvanceCreate(DataEntryRequiredMixin, View):
    template_name = "cashbook/advance_form.html"

    def get(self, request):
        return render(request, self.template_name, {
            "departments": Department.objects.filter(active=True).order_by("name"),
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
        adv = StaffAdvance.objects.create(
            staff_name=name, department=dept, amount=amount, date_issued=issued,
            purpose=(request.POST.get("purpose") or "")[:200],
            method=request.POST.get("method") or "CASH",
            reference=(request.POST.get("reference") or "")[:40],
            issued_by=request.user)
        messages.success(request, "Advance recorded.")
        return redirect("advance_detail", pk=adv.pk)


class AdvanceDetail(ReadAccessMixin, View):
    template_name = "cashbook/advance_detail.html"

    def get(self, request, pk):
        from .models import StaffAdvance
        adv = get_object_or_404(StaffAdvance, pk=pk)
        return render(request, self.template_name, {
            "adv": adv, "expenses": adv.expenses.all(),
            "categories": Expense.Category.choices, "today": dt.date.today().isoformat()})


class AdvanceAddExpense(DataEntryRequiredMixin, View):
    """Record a receipt/expense that settles part of an advance."""
    def post(self, request, pk):
        from decimal import Decimal, InvalidOperation
        from core.models import SiteConfig
        from .models import StaffAdvance
        adv = get_object_or_404(StaffAdvance, pk=pk)
        try:
            amount = Decimal(request.POST.get("amount") or "0")
        except InvalidOperation:
            amount = Decimal(0)
        desc = (request.POST.get("description") or "").strip()
        if not (desc and amount > 0):
            messages.error(request, "A description and positive amount are required.")
            return redirect("advance_detail", pk=pk)
        try:
            d = dt.date.fromisoformat(request.POST.get("date"))
        except (TypeError, ValueError):
            d = dt.date.today()
        cfg = SiteConfig.get()
        status = (Expense.Status.PENDING if cfg.require_expense_approval
                  else Expense.Status.APPROVED)
        Expense.objects.create(
            date=d, sabbath_week=sabbath_week_of(d), department=adv.department,
            description=desc, amount=amount,
            category=request.POST.get("category") or Expense.Category.OTHER,
            claimant=adv.staff_name, method=adv.method, status=status,
            recorded_by=request.user, advance=adv,
            approved_by=(request.user if status == Expense.Status.APPROVED else None))
        adv.status = (StaffAdvance.Status.SETTLED if adv.balance == 0
                      else StaffAdvance.Status.PARTLY)
        adv.save(update_fields=["status"])
        messages.success(request, "Settling expense recorded against the advance.")
        return redirect("advance_detail", pk=pk)


class AdvanceClose(TreasurerRequiredMixin, View):
    def post(self, request, pk):
        from .models import StaffAdvance
        adv = get_object_or_404(StaffAdvance, pk=pk)
        adv.status = StaffAdvance.Status.CLOSED
        adv.settled_on = dt.date.today()
        adv.note = (request.POST.get("note") or adv.note)[:1000]
        adv.save(update_fields=["status", "settled_on", "note"])
        messages.success(request, "Advance closed.")
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
    """Download all expenses as a spreadsheet for offline re-categorising, then
    re-import to update ONLY the category column. Every other field is read-only
    in this flow — the round-trip is keyed on the expense ID, and any change to
    amounts, dates, descriptions etc. in the file is ignored. This lets a
    treasurer fix many mis-categorised expenses quickly without risking the rest
    of the ledger."""
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
                "Current category", "New category (edit this)"]
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
        for x in (Expense.objects.select_related("department")
                  .order_by("-date", "-id")):
            ws.append([x.id, x.date.isoformat(), x.description,
                       x.department.name if x.department_id else "",
                       float(x.amount), x.get_category_display(),
                       x.get_category_display()])
        ws.column_dimensions["C"].width = 34
        ws.column_dimensions["F"].width = 20
        ws.column_dimensions["G"].width = 22
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
            messages.error(request, "Could not read that file — is it the .xlsx you downloaded?")
            return redirect("expense_recategorize")
        ws = wb["Expenses"] if "Expenses" in wb.sheetnames else wb.active

        # build label -> code and code -> code lookups so the file can carry
        # either the human label ("Transport") or the raw code ("TRANSPORT")
        by_label = {lbl.lower(): code for code, lbl in Expense.Category.choices}
        by_code = {code.upper(): code for code, _ in Expense.Category.choices}

        header = [str(c.value).strip().lower() if c.value else "" for c in ws[1]]
        try:
            id_col = header.index("id")
            new_col = header.index("new category (edit this)")
        except ValueError:
            messages.error(request, "That doesn't look like the recategorise template "
                                    "(missing the ID or New category column).")
            return redirect("expense_recategorize")

        updated = unchanged = bad = missing = 0
        bad_rows = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            if id_col >= len(row) or row[id_col] in (None, ""):
                continue
            try:
                eid = int(row[id_col])
            except (TypeError, ValueError):
                continue
            raw = row[new_col] if new_col < len(row) else None
            if raw in (None, ""):
                continue
            key = str(raw).strip()
            code = by_code.get(key.upper()) or by_label.get(key.lower())
            if not code:
                bad += 1
                if len(bad_rows) < 10:
                    bad_rows.append(f"#{eid}: “{key}”")
                continue
            exp = Expense.objects.filter(pk=eid).first()
            if not exp:
                missing += 1
                continue
            if exp.category == code:
                unchanged += 1
                continue
            exp.category = code
            exp.save(update_fields=["category"])
            updated += 1

        parts = [f"{updated} re-categorised"]
        if unchanged:
            parts.append(f"{unchanged} unchanged")
        if missing:
            parts.append(f"{missing} not found")
        if bad:
            parts.append(f"{bad} with an unrecognised category")
        msg = ", ".join(parts) + "."
        if bad_rows:
            msg += " Unrecognised: " + "; ".join(bad_rows)
        (messages.success if updated else messages.warning)(request, msg)
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
                "Claimant", "Voucher no"]
        ws.append(head)
        for c in range(1, len(head) + 1):
            ws.cell(1, c).font = Font(bold=True, color="FFFFFF")
            ws.cell(1, c).fill = PatternFill("solid", fgColor="1F5F4F")
        ws.append(["2026-06-06", "LCB", "Pulpit microphone", 4500, "Materials",
                   "Cash", "J. Mwangi", "V-001"])
        ws.append(["2026-06-07", "YOUTH", "Bus fare for rally", 2000, "Transport",
                   "M-Pesa", "S. Achieng", "V-002"])
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
            plan.append({
                "date": d, "fund_raw": fund_raw,
                "fund_id": fund.id if fund else None,
                "fund_name": fund.name if fund else None,
                "description": desc[:200], "amount": amt,
                "category": cat, "method": method,
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
            exp = Expense(
                date=d, department=fund, description=p["description"],
                amount=D(str(p["amount"])), category=p["category"],
                method=p["method"], claimant=p["claimant"], voucher_no=p["voucher"],
                recorded_by=request.user,
                sabbath_week=sabbath_week_of(d))
            if auto:
                exp.status = Expense.Status.APPROVED
                exp.approved_by = request.user
            exp.save()
            created += 1
        request.session.pop("expense_import_plan", None)
        state = "approved" if auto else "pending approval"
        parts = [f"{created} expense(s) imported ({state})"]
        if skipped:
            parts.append(f"{skipped} row(s) skipped (missing date, fund, description or amount)")
        messages.success(request, ", ".join(parts) + ".")
        return redirect("expense_list")
