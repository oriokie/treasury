"""Loan module views. Thin views; all money movement goes through
loans.services.loans so accounting integrity lives in one place."""
import datetime as dt
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from core.permissions import LoanConvertMixin, LoanManageMixin, LoanViewMixin
from departments.models import Department
from reports.exports import csv_response, xlsx_response

from .forms import (AttachmentForm, InterestForm, LenderForm, LoanForm,
                    PatternForm, ReceiptForm, RepaymentForm, RetireForm)
from .models import Lender, Loan, LoanNarrationPattern, LoanTransaction
from .services import loans as svc


def _site_name():
    try:
        from core.models import SiteConfig
        return SiteConfig.get().church_name or "Church"
    except Exception:  # noqa: BLE001
        return "Church"


# ---- Dashboard --------------------------------------------------------------

class LoanDashboardView(LoanViewMixin, View):
    def get(self, request):
        loans = list(Loan.objects.exclude(status=Loan.Status.DRAFT)
                     .select_related("lender", "fund")
                     .prefetch_related("transactions__receipt_transaction",
                                       "transactions__income_transaction",
                                       "transactions__expense"))
        today = dt.date.today()
        active = [l for l in loans if l.status == Loan.Status.ACTIVE]
        out_p = sum((l.outstanding_principal for l in active), Decimal(0))
        out_i = sum((l.outstanding_interest for l in active), Decimal(0))
        overdue = [l for l in active if l.is_overdue]
        due_soon = [l for l in active if l.maturity_date and not l.is_overdue
                    and l.maturity_date <= today + dt.timedelta(days=60)]
        # by fund
        by_fund = {}
        for l in active:
            row = by_fund.setdefault(l.fund, {"fund": l.fund, "count": 0,
                                              "outstanding": Decimal(0),
                                              "financing": Decimal(0)})
            row["count"] += 1
            row["outstanding"] += l.outstanding_principal
            row["financing"] += l.received_total
        # largest lenders by outstanding
        by_lender = {}
        for l in active:
            by_lender[l.lender] = by_lender.get(l.lender, Decimal(0)) + l.outstanding_principal
        largest = sorted(by_lender.items(), key=lambda kv: -kv[1])[:8]
        recent = (LoanTransaction.objects.select_related("loan__lender", "loan__fund")
                  .order_by("-date", "-id")[:12])
        return render(request, "loans/dashboard.html", {
            "active_count": len(active), "outstanding_principal": out_p,
            "outstanding_interest": out_i, "overdue": overdue,
            "due_soon": due_soon, "by_fund": sorted(
                by_fund.values(), key=lambda r: -r["outstanding"]),
            "largest": largest, "recent": recent,
            "unlinked_lenders": Lender.objects.filter(
                member__isnull=True, merged_into__isnull=True,
                loans__isnull=False).distinct().count(),
        })


# ---- Register / detail ------------------------------------------------------

class LoanRegisterView(LoanViewMixin, View):
    def get(self, request):
        qs = (Loan.objects.select_related("lender", "fund")
              .prefetch_related("transactions__receipt_transaction",
                                "transactions__income_transaction",
                                "transactions__expense"))
        status = request.GET.get("status") or ""
        fund = request.GET.get("fund") or ""
        q = (request.GET.get("q") or "").strip()
        if status:
            qs = qs.filter(status=status)
        if fund:
            qs = qs.filter(fund_id=fund)
        if q:
            qs = qs.filter(Q(number__icontains=q) | Q(lender__name__icontains=q)
                           | Q(purpose__icontains=q) | Q(project__icontains=q))
        loans = list(qs)
        export = request.GET.get("export")
        if export in ("csv", "xlsx"):
            header = ["Loan no", "Lender", "Fund", "Type", "Loan date", "Maturity",
                      "Received", "Repaid", "Converted", "Written off",
                      "Outstanding", "Interest paid", "Status"]
            rows = [[l.number, l.lender.name, l.fund.name, l.get_loan_type_display(),
                     l.loan_date.isoformat(),
                     l.maturity_date.isoformat() if l.maturity_date else "",
                     float(l.received_total), float(l.principal_repaid),
                     float(l.converted_total), float(l.written_off_total),
                     float(l.outstanding_principal), float(l.interest_paid),
                     l.get_status_display()] for l in loans]
            if export == "csv":
                return csv_response("loan_register.csv", header, rows)
            return xlsx_response("loan_register.xlsx", header, rows,
                                 title="Loan register", church=_site_name())
        totals = {
            "received": sum((l.received_total for l in loans), Decimal(0)),
            "outstanding": sum((l.outstanding_principal for l in loans), Decimal(0)),
        }
        page = Paginator(loans, 25).get_page(request.GET.get("page"))
        return render(request, "loans/register.html", {
            "page_obj": page, "loans": page.object_list, "totals": totals,
            "funds": Department.objects.exclude(
                fund_type=Department.FundType.TRUST).order_by("name"),
            "statuses": Loan.Status.choices,
            "f_status": status, "f_fund": fund, "q": q,
        })


class LoanDetailView(LoanViewMixin, View):
    def get(self, request, pk):
        loan = get_object_or_404(
            Loan.objects.select_related("lender", "fund"), pk=pk)
        loan.refresh_status()
        txns = list(loan.transactions.select_related(
            "receipt_transaction", "income_transaction", "expense")
            .order_by("date", "id"))
        # running outstanding for the loan ledger
        bal = Decimal(0)
        rows = []
        for t in txns:
            eff = t.effective
            if eff:
                if t.kind == LoanTransaction.Kind.RECEIPT:
                    bal += t.amount
                elif t.kind != LoanTransaction.Kind.INTEREST:   # interest never
                    bal -= t.amount                              # touches principal
            rows.append({"t": t, "effective": eff, "balance": bal})
        export = request.GET.get("export")
        if export in ("csv", "xlsx"):
            header = ["Date", "Type", "Amount", "Outstanding after", "Effective",
                      "Note", "Document"]
            data = [[r["t"].date.isoformat(), r["t"].get_kind_display(),
                     float(r["t"].amount), float(r["balance"]),
                     "Yes" if r["effective"] else "No", r["t"].note,
                     (r["t"].expense.voucher_no if r["t"].expense_id else "")
                     or (r["t"].receipt_transaction.core_ref
                         if r["t"].receipt_transaction_id else "") or ""]
                    for r in rows]
            fname = f"loan_statement_{loan.number}"
            title = f"Loan statement {loan.number} — {loan.lender.name}"
            if export == "csv":
                return csv_response(fname + ".csv", header, data)
            return xlsx_response(fname + ".xlsx", header, data,
                                 title=title, church=_site_name())
        return render(request, "loans/detail.html", {
            "loan": loan, "rows": rows,
            "attach_form": AttachmentForm(),
            "can_manage": _can_manage(request.user),
            "can_convert": _can_convert(request.user),
        })


def _can_manage(user):
    from core import roles
    return roles.can_manage_loans(user)


def _can_convert(user):
    from core import roles
    return roles.can_convert_loans(user)


class LoanCreateView(LoanManageMixin, View):
    def get(self, request):
        form = LoanForm(initial={"lender": request.GET.get("lender") or None})
        return render(request, "loans/loan_form.html", {"form": form, "loan": None})

    def post(self, request):
        form = LoanForm(request.POST)
        if form.is_valid():
            loan = form.save(commit=False)
            loan.created_by = request.user
            loan.save()
            messages.success(request, f"Loan {loan.number} created.")
            return redirect("loan_detail", loan.pk)
        return render(request, "loans/loan_form.html", {"form": form, "loan": None})


class LoanEditView(LoanManageMixin, View):
    def get(self, request, pk):
        loan = get_object_or_404(Loan, pk=pk)
        if not loan.is_editable:
            messages.error(request, f"{loan.number} is {loan.get_status_display().lower()} "
                                    f"and can no longer be edited.")
            return redirect("loan_detail", loan.pk)
        return render(request, "loans/loan_form.html",
                      {"form": LoanForm(instance=loan), "loan": loan})

    def post(self, request, pk):
        loan = get_object_or_404(Loan, pk=pk)
        if not loan.is_editable:
            messages.error(request, "This loan can no longer be edited.")
            return redirect("loan_detail", loan.pk)
        form = LoanForm(request.POST, instance=loan)
        if form.is_valid():
            form.save()
            messages.success(request, f"Loan {loan.number} updated.")
            return redirect("loan_detail", loan.pk)
        return render(request, "loans/loan_form.html", {"form": form, "loan": loan})


class LoanDeleteView(LoanManageMixin, View):
    def post(self, request, pk):
        loan = get_object_or_404(Loan, pk=pk)
        if loan.transactions.exists():
            messages.error(request, f"{loan.number} has transactions and cannot be "
                                    f"deleted — reverse/reject its documents instead.")
            return redirect("loan_detail", loan.pk)
        number = loan.number
        loan.delete()
        messages.success(request, f"Loan {number} deleted.")
        return redirect("loan_register")


# ---- Money actions ----------------------------------------------------------

class _MoneyActionView(LoanManageMixin, View):
    """Shared GET/POST plumbing for the four money forms."""
    form_class = None
    template = "loans/money_form.html"
    title = ""

    def get(self, request, pk):
        loan = get_object_or_404(Loan.objects.select_related("lender", "fund"), pk=pk)
        return render(request, self.template, {
            "loan": loan, "form": self.form_class(), "title": self.title,
            "action": request.path})

    def post(self, request, pk):
        loan = get_object_or_404(Loan.objects.select_related("lender", "fund"), pk=pk)
        form = self.form_class(request.POST)
        if form.is_valid():
            try:
                self.apply(request, loan, form.cleaned_data)
                messages.success(request, f"{self.title} recorded on {loan.number}.")
                return redirect("loan_detail", loan.pk)
            except ValidationError as e:
                for msg in e.messages:
                    form.add_error(None, msg)
        return render(request, self.template, {
            "loan": loan, "form": form, "title": self.title, "action": request.path})


class LoanReceiptView(_MoneyActionView):
    form_class = ReceiptForm
    title = "Loan receipt"

    def apply(self, request, loan, cd):
        from giving.models import Transaction
        svc.record_receipt(
            loan, date=cd["date"], amount=cd["amount"], user=request.user,
            note=cd.get("note") or "",
            channel=(Transaction.Channel.CASH if cd.get("channel") == "CASH"
                     else Transaction.Channel.BANK),
            core_ref=(cd.get("reference") or "").strip().upper() or None)


class LoanRepaymentView(_MoneyActionView):
    form_class = RepaymentForm
    title = "Principal repayment"

    def apply(self, request, loan, cd):
        bank_txn = _bank_debit(cd.get("bank_transaction_id"))
        svc.record_repayment(
            loan, date=cd["date"], amount=cd["amount"], user=request.user,
            method=cd.get("method"), voucher_no=cd.get("voucher_no") or "",
            note=cd.get("note") or "", bank_transaction=bank_txn)


class LoanInterestView(_MoneyActionView):
    form_class = InterestForm
    title = "Interest payment"

    def apply(self, request, loan, cd):
        bank_txn = _bank_debit(cd.get("bank_transaction_id"))
        svc.record_interest(
            loan, date=cd["date"], amount=cd["amount"], user=request.user,
            method=cd.get("method"), voucher_no=cd.get("voucher_no") or "",
            note=cd.get("note") or "", bank_transaction=bank_txn)


def _bank_debit(txn_id):
    if not txn_id:
        return None
    from giving.models import Transaction
    return Transaction.objects.filter(
        pk=txn_id, direction=Transaction.Direction.DEBIT).first()


class LoanConvertView(LoanConvertMixin, _MoneyActionView):
    form_class = RetireForm
    title = "Convert to donation"

    def apply(self, request, loan, cd):
        svc.convert_to_donation(loan, date=cd["date"], amount=cd["amount"],
                                user=request.user, note=cd.get("note") or "")


class LoanWriteOffView(LoanConvertMixin, _MoneyActionView):
    form_class = RetireForm
    title = "Write off"

    def apply(self, request, loan, cd):
        svc.write_off(loan, date=cd["date"], amount=cd["amount"],
                      user=request.user, note=cd.get("note") or "")


class LoanAttachmentView(LoanManageMixin, View):
    def post(self, request, pk):
        loan = get_object_or_404(Loan, pk=pk)
        form = AttachmentForm(request.POST, request.FILES)
        if form.is_valid():
            att = form.save(commit=False)
            att.loan = loan
            att.uploaded_by = request.user
            att.save()
            messages.success(request, "Attachment added.")
        else:
            messages.error(request, "Could not save the attachment.")
        return redirect("loan_detail", loan.pk)


class LoanReceiptFromTransactionView(LoanManageMixin, View):
    """Record an already-imported bank credit (usually sitting in the review
    queue after a fund-less loan-narration hit) as a loan receipt."""

    def get(self, request, txn_id):
        txn = self._txn(txn_id)
        return render(request, "loans/from_transaction.html", {
            "txn": txn, "form": LoanForm(initial={
                "loan_date": txn.date,
                "purpose": (txn.reference or txn.raw_narration or "")[:200]}),
            "lenders": Lender.objects.filter(merged_into__isnull=True).order_by("name"),
            "loans": Loan.objects.filter(status=Loan.Status.ACTIVE)
                     .select_related("lender", "fund"),
        })

    def post(self, request, txn_id):
        txn = self._txn(txn_id)
        loan_id = request.POST.get("loan_id")
        try:
            if loan_id:                              # attach to an existing loan
                loan = get_object_or_404(Loan, pk=loan_id)
            else:                                    # create lender + loan inline
                fund = get_object_or_404(Department, pk=request.POST.get("fund"))
                if fund.fund_type == Department.FundType.TRUST:
                    raise ValidationError("Loans cannot finance a trust fund.")
                lender, _ = svc.match_or_create_lender(
                    request.POST.get("lender_name") or txn.payer_name,
                    request.POST.get("lender_phone") or txn.payer_phone)
                loan, _ = svc.loan_for_receipt(
                    lender, fund, txn.date, user=request.user,
                    purpose=(txn.reference or "")[:200])
            svc.record_receipt(loan, date=txn.date, amount=txn.amount,
                               user=request.user, existing_transaction=txn)
            messages.success(request,
                             f"Recorded as a loan receipt on {loan.number}.")
            return redirect("loan_detail", loan.pk)
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
            return redirect("loan_from_transaction", txn_id=txn.pk)

    def _txn(self, txn_id):
        from giving.models import Transaction
        txn = get_object_or_404(Transaction, pk=txn_id)
        if txn.direction != Transaction.Direction.CREDIT:
            raise Http404
        return txn


# ---- Lenders ----------------------------------------------------------------

class LenderListView(LoanViewMixin, View):
    def get(self, request):
        qs = Lender.objects.filter(merged_into__isnull=True).select_related("member")
        q = (request.GET.get("q") or "").strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(phone__icontains=q))
        page = Paginator(qs, 30).get_page(request.GET.get("page"))
        return render(request, "loans/lender_list.html",
                      {"page_obj": page, "lenders": page.object_list, "q": q})


class LenderFormView(LoanManageMixin, View):
    def get(self, request, pk=None):
        lender = get_object_or_404(Lender, pk=pk) if pk else None
        return render(request, "loans/lender_form.html",
                      {"form": LenderForm(instance=lender), "lender": lender})

    def post(self, request, pk=None):
        lender = get_object_or_404(Lender, pk=pk) if pk else None
        form = LenderForm(request.POST, instance=lender)
        if form.is_valid():
            lender = form.save()
            messages.success(request, f"Lender {lender.name} saved.")
            return redirect("lender_matching" if not lender.member_id else "lender_list")
        return render(request, "loans/lender_form.html",
                      {"form": form, "lender": lender})


class LenderMatchingView(LoanManageMixin, View):
    """Lenders not yet linked to a member: link, create member, edit, merge."""

    def get(self, request):
        from members.models import Member
        unlinked = (Lender.objects.filter(member__isnull=True,
                                          merged_into__isnull=True)
                    .prefetch_related("loans"))
        rows = []
        for l in unlinked:
            # candidate members by phone then name-key — suggestion only,
            # never auto-linked
            cands = []
            if l.phone:
                cands = list(Member.objects.filter(phone=l.phone)[:3])
            if not cands and l.name_key:
                cands = list(Member.objects.filter(name_key=l.name_key)[:3])
            dups = svc.possible_duplicate_lenders(
                name=l.name, phone=l.phone, national_id=l.national_id,
                exclude_pk=l.pk)
            rows.append({"lender": l, "candidates": cands, "dups": dups})
        return render(request, "loans/lender_matching.html", {"rows": rows})

    def post(self, request):
        action = request.POST.get("action")
        lender = get_object_or_404(Lender, pk=request.POST.get("lender_id"))
        if action == "link":
            from members.models import Member
            member = get_object_or_404(Member, pk=request.POST.get("member_id"))
            lender.member = member
            lender.save()
            messages.success(request, f"{lender.name} linked to member {member.name}.")
        elif action == "create_member":
            from members.models import Member
            member = Member.objects.create(
                name=lender.name, phone=lender.phone or None,
                source=Member.Source.MANUAL)
            lender.member = member
            lender.save()
            messages.success(request,
                             f"Member {member.name} created and linked (details "
                             f"pre-filled from the lender).")
        elif action == "merge":
            absorb = get_object_or_404(Lender, pk=request.POST.get("absorb_id"))
            svc.merge_lenders(lender, absorb, user=request.user)
            messages.success(request, f"{absorb.name} merged into {lender.name}.")
        return redirect("lender_matching")


# ---- Narration patterns -----------------------------------------------------

class PatternListView(LoanManageMixin, View):
    def get(self, request):
        return render(request, "loans/patterns.html", {
            "patterns": LoanNarrationPattern.objects.select_related("fund"),
            "form": PatternForm()})

    def post(self, request):
        if request.POST.get("action") == "toggle":
            p = get_object_or_404(LoanNarrationPattern, pk=request.POST.get("pk"))
            p.active = not p.active
            p.save(update_fields=["active"])
            messages.success(request, f"Pattern “{p.pattern}” "
                                      f"{'activated' if p.active else 'deactivated'}.")
            return redirect("loan_patterns")
        form = PatternForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Pattern added.")
            return redirect("loan_patterns")
        return render(request, "loans/patterns.html", {
            "patterns": LoanNarrationPattern.objects.select_related("fund"),
            "form": form})
