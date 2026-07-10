"""The Liability Transactions register — the central place for every movement
on a liability: loan receipts/repayments/conversions/write-offs, trust fund
receipts and releases, and any voucher whose category is classed LIABILITY
(including custom categories flagged is_liability, so future types — deposit
refunds, advance settlements, deferred income — appear here with no code
change).

This is a REGISTER over existing documents, not a new accounting path: rows
are drawn from the same Expense vouchers, giving Transactions and
LoanTransactions the posting engine already books, so the ledger, the audit
history and every balance stay exactly as they were. Each row links back to
its source document (voucher detail for approval, loan page, transaction).

Row shape (normalised across sources):
  date · ref · txn_type · liability_type · description · fund · party ·
  amount · direction (+ raises the liability / − settles it) · status ·
  created_by · url
"""
import datetime as dt
from decimal import Decimal

from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.shortcuts import render
from django.urls import reverse
from django.views import View

from core import roles
from core.models import SiteConfig
from core.permissions import LiabilityViewMixin
from core.utils import parse_period
from departments.models import Department

from .models import Expense


# ---- row builders -----------------------------------------------------------

def _expense_rows(start, end, dept_ids=None, q=None, status=None):
    """Liability-class vouchers that are NOT the settlement leg of a loan
    transaction (loan legs are represented once, by their LoanTransaction row,
    which carries the loan number and lender)."""
    qs = (Expense.objects.filter(doc_class=Expense.DocClass.LIABILITY,
                                 date__gte=start, date__lte=end,
                                 loan_transaction__isnull=True)
          .select_related("department", "recorded_by"))
    if dept_ids is not None:
        qs = qs.filter(department_id__in=dept_ids)
    if status:
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(Q(description__icontains=q) | Q(claimant__icontains=q)
                       | Q(voucher_no__icontains=q))
    rows = []
    for e in qs:
        if e.category == Expense.Category.REMITTANCE:
            ttype, ltype = "Trust fund release", "Trust"
        else:
            ttype, ltype = e.category_display, "Other"
        rows.append({
            "date": e.date, "ref": e.voucher_no or f"EXP-{e.pk}",
            "txn_type": ttype, "liability_type": ltype,
            "description": e.description,
            "fund": e.department.name if e.department_id else "",
            "party": e.claimant or "", "amount": e.amount,
            "direction": -1,                       # settles / releases
            "status": e.get_status_display(),
            "created_by": getattr(e.recorded_by, "username", ""),
            "url": reverse("expense_detail", args=[e.pk]),
        })
    return rows


_LOAN_KIND = {  # (label, liability direction)
    "RECEIPT": ("Loan receipt", +1),
    "PRINCIPAL": ("Loan repayment", -1),
    "INTEREST": ("Loan interest", 0),          # expense, shown for the trail
    "CONVERSION": ("Loan conversion to donation", -1),
    "WRITE_OFF": ("Loan write-off", -1),
}


def _loan_rows(start, end, dept_ids=None, q=None):
    from loans.models import LoanTransaction
    qs = (LoanTransaction.objects.filter(date__gte=start, date__lte=end)
          .select_related("loan__lender", "loan__fund", "expense",
                          "receipt_transaction", "income_transaction",
                          "created_by"))
    if dept_ids is not None:
        qs = qs.filter(loan__fund_id__in=dept_ids)
    if q:
        qs = qs.filter(Q(loan__number__icontains=q)
                       | Q(loan__lender__name__icontains=q)
                       | Q(note__icontains=q))
    rows = []
    for t in qs:
        if not t.effective:
            continue
        label, sign = _LOAN_KIND.get(t.kind, (t.get_kind_display(), 0))
        rows.append({
            "date": t.date, "ref": t.loan.number,
            "txn_type": label, "liability_type": "Loan",
            "description": t.note or f"{label} — {t.loan.lender.name}",
            "fund": t.loan.fund.name, "party": t.loan.lender.name,
            "amount": t.amount, "direction": sign,
            "status": "Effective",
            "created_by": getattr(t.created_by, "username", ""),
            "url": reverse("loan_detail", args=[t.loan_id]),
        })
    return rows


def _trust_receipt_rows(start, end, dept_ids=None, q=None):
    """Trust fund receipts: confirmed credits on trust funds — money held on
    the field's behalf (CR Trust payable), a liability increase."""
    from giving.models import Transaction
    qs = (Transaction.objects.filter(
            direction=Transaction.Direction.CREDIT, confirmed=True,
            is_reversed=False, is_reversal=False,
            department__fund_type=Department.FundType.TRUST,
            date__gte=start, date__lte=end)
          .select_related("department", "member"))
    if dept_ids is not None:
        qs = qs.filter(department_id__in=dept_ids)
    if q:
        qs = qs.filter(Q(payer_name__icontains=q) | Q(reference__icontains=q)
                       | Q(core_ref__icontains=q))
    rows = []
    for t in qs:
        rows.append({
            "date": t.date, "ref": t.core_ref or t.bank_receipt or f"TXN-{t.pk}",
            "txn_type": "Trust fund receipt", "liability_type": "Trust",
            "description": t.reference or t.payer_name or "Trust receipt",
            "fund": t.department.name if t.department_id else "",
            "party": t.payer_name or "", "amount": t.amount,
            "direction": +1, "status": "Confirmed",
            "created_by": "", "url": reverse("transaction_edit", args=[t.pk]),
        })
    return rows


TYPE_FILTERS = {
    "": None,                                   # all
    "loan": {"loan"},
    "trust": {"trust"},
    "trust_receipts": {"trust_receipts"},
    "other": {"other"},
}


def liability_rows(*, start, end, dept_ids=None, q=None, ltype="",
                   status=None, include_trust_receipts=None):
    """The merged register. Trust receipts are high-volume routine rows, so
    they are included when the Trust filters are chosen or explicitly asked
    for, and summarised (not listed) otherwise — the header always shows the
    outstanding trust liability either way."""
    if include_trust_receipts is None:
        include_trust_receipts = ltype in ("trust", "trust_receipts")
    rows = []
    if ltype in ("", "other", "trust"):
        rows += _expense_rows(start, end, dept_ids, q, status)
    if ltype in ("", "loan"):
        rows += _loan_rows(start, end, dept_ids, q)
    if include_trust_receipts:
        rows += _trust_receipt_rows(start, end, dept_ids, q)
    if ltype == "trust":
        rows = [r for r in rows if r["liability_type"] == "Trust"]
    elif ltype == "trust_receipts":
        rows = [r for r in rows if r["txn_type"] == "Trust fund receipt"]
    elif ltype == "other":
        rows = [r for r in rows if r["liability_type"] == "Other"]
    rows.sort(key=lambda r: (r["date"], r["ref"]), reverse=True)
    return rows


# ---- the register view ------------------------------------------------------

class LiabilityRegisterView(LiabilityViewMixin, View):
    """Finance → Liability Transactions: dashboard header + filterable,
    exportable register. Staff see everything; a department leader is scoped
    to exactly the funds the rest of the leader area allows."""

    def _scope(self, request):
        """None = unrestricted; otherwise the department ids the user may see."""
        if roles.can_view_liabilities(request.user):
            return None
        from leaders.permissions import allowed_departments
        return list(allowed_departments(request.user).values_list("id", flat=True))

    def get(self, request):
        start, end = parse_period(request)
        g = request.GET
        ltype = g.get("type") or ""
        status = g.get("status") or None
        q = (g.get("q") or "").strip() or None
        fund = g.get("fund") or None
        dept_ids = self._scope(request)
        if fund:
            fund_ids = [int(fund)]
            dept_ids = (fund_ids if dept_ids is None
                        else [i for i in fund_ids if i in dept_ids])
        rows = liability_rows(start=start, end=end, dept_ids=dept_ids,
                              q=q, ltype=ltype, status=status)

        export = g.get("export")
        if export in ("csv", "xlsx"):
            from reports.exports import csv_response, xlsx_response
            header = ["Date", "Reference", "Transaction type", "Liability type",
                      "Description", "Fund", "Lender / beneficiary / trust",
                      "Amount", "Effect on liability", "Status", "Created by"]
            data = [[r["date"].isoformat(), r["ref"], r["txn_type"],
                     r["liability_type"], r["description"], r["fund"],
                     r["party"], float(r["amount"]),
                     ("Increase" if r["direction"] > 0
                      else "Settle" if r["direction"] < 0 else "—"),
                     r["status"], r["created_by"]] for r in rows]
            fn = f"liability_register_{start}_{end}"
            if export == "xlsx":
                return xlsx_response(fn + ".xlsx", header, data,
                                     title="Liability Transactions Register",
                                     church=SiteConfig.get().church_name)
            return csv_response(fn + ".csv", header, data)

        # ---- dashboard header (as-at figures, not period-bound) ----
        stats = self._stats(dept_ids, start, end, rows)
        page = Paginator(rows, 40).get_page(g.get("page"))
        funds = Department.objects.filter(active=True).order_by("name")
        if dept_ids is not None:
            funds = funds.filter(id__in=dept_ids)
        return render(request, "cashbook/liability_register.html", {
            "page_obj": page, "rows": page.object_list, "stats": stats,
            "start": start, "end": end, "f": g, "ltype": ltype,
            "funds": funds, "statuses": Expense.Status.choices,
            "scoped": dept_ids is not None,
            "can_manage": roles.can_manage_liabilities(request.user),
        })

    def _stats(self, dept_ids, start, end, rows):
        from loans.services import reporting as loan_rep
        from reports.services import balances
        from cashbook.views import outstanding_advances_total
        today = dt.date.today()
        # outstanding loans (scoped if needed)
        if dept_ids is None:
            loans_out = loan_rep.outstanding_liability()["total"]
        else:
            from loans.services.loans import outstanding_by_fund
            per = outstanding_by_fund()
            loans_out = sum((v for k, v in per.items() if k in dept_ids), Decimal(0))
        # outstanding trust (the balance-sheet figure: trust fund closings)
        trust_out = Decimal(0)
        try:
            for r in balances.department_summary():
                if r.get("is_trust") and (dept_ids is None
                                          or r["department"].id in dept_ids):
                    trust_out += r["closing"]
        except Exception:  # noqa: BLE001
            pass
        advances_out = (outstanding_advances_total(today)
                        if dept_ids is None else None)
        month_start = today.replace(day=1)
        month_rows = [r for r in rows if r["date"] >= month_start] \
            if start <= month_start <= end else None
        if month_rows is None:
            month_rows = liability_rows(start=month_start, end=today,
                                        dept_ids=dept_ids)
        return {
            "loans_out": loans_out, "trust_out": trust_out,
            "advances_out": advances_out,
            "month_count": len(month_rows),
            "month_total": sum((r["amount"] for r in month_rows), Decimal(0)),
            "recent": rows[:8],
        }
