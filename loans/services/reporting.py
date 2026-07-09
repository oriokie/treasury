"""Loan reporting aggregates and the financial-statement classification
helpers, computed database-side from the same source documents the loan
services already use. Nothing here maintains a stored balance — every figure
derives from LoanTransaction effectiveness and the loans' computed
properties, so these reports can never disagree with the loan pages, the
ledger, or the fund balances.

Two audiences share this module:
  * the loan report catalogue (register/ageing/maturity/etc.);
  * the existing financial statements (Statement of Financial Position,
    Cash Flow), which call the classification helpers here so loan financing
    is presented consistently everywhere.
"""
import datetime as _dt
from decimal import Decimal

from django.db.models import Q

from loans.models import Loan, LoanTransaction


# ---- shared loan set --------------------------------------------------------

def _prefetched(qs=None):
    qs = qs if qs is not None else Loan.objects.all()
    return qs.select_related("lender", "fund").prefetch_related(
        "transactions__receipt_transaction",
        "transactions__income_transaction",
        "transactions__expense")


def filtered_loans(*, start=None, end=None, fund=None, lender=None,
                   status=None, loan_type=None, include_draft=False):
    """Apply the standard report filters. `start`/`end` bound the loan_date."""
    qs = Loan.objects.all()
    if not include_draft:
        qs = qs.exclude(status=Loan.Status.DRAFT)
    if start:
        qs = qs.filter(loan_date__gte=start)
    if end:
        qs = qs.filter(loan_date__lte=end)
    if fund:
        qs = qs.filter(fund_id=fund)
    if lender:
        qs = qs.filter(lender_id=lender)
    if status:
        qs = qs.filter(status=status)
    if loan_type:
        qs = qs.filter(loan_type=loan_type)
    return _prefetched(qs)


# ---- 1. Loan Liability Schedule --------------------------------------------

def liability_schedule(loans=None, as_of=None):
    """The full liability line per loan, as at a date (default today)."""
    as_of = as_of or _dt.date.today()
    loans = loans if loans is not None else _prefetched()
    rows = []
    for l in loans:
        outstanding = l.outstanding_asof(as_of)
        interest_out = l.outstanding_interest if outstanding else Decimal(0)
        days = None
        if l.maturity_date:
            days = (l.maturity_date - as_of).days
        rows.append({
            "loan": l, "number": l.number, "lender": l.lender.name,
            "fund": l.fund.name, "fund_id": l.fund_id,
            "loan_date": l.loan_date, "maturity_date": l.maturity_date,
            "original_principal": l.principal_amount or l.received_total,
            "received": l.received_total,
            "outstanding_principal": outstanding,
            "outstanding_interest": interest_out,
            "total_outstanding": outstanding + interest_out,
            "interest_rate": l.interest_rate,
            "interest_method": l.get_interest_method_display(),
            "status": l.get_status_display(), "status_code": l.status,
            "days_to_maturity": days,
            "overdue": bool(days is not None and days < 0 and outstanding > 0),
        })
    return rows


# ---- 3. Loan Ageing (by outstanding age from loan_date) ---------------------

AGE_BUCKETS = [(0, 30, "0–30 days"), (31, 90, "31–90 days"),
               (91, 180, "91–180 days"), (181, 365, "181–365 days"),
               (366, 10 ** 6, "Over 1 year")]


def ageing(loans=None, as_of=None):
    as_of = as_of or _dt.date.today()
    loans = loans if loans is not None else _prefetched()
    buckets = {label: {"count": 0, "amount": Decimal(0)}
               for _, _, label in AGE_BUCKETS}
    rows = []
    for l in loans:
        outstanding = l.outstanding_asof(as_of)
        if outstanding <= 0:
            continue
        age = (as_of - l.loan_date).days
        label = next((lbl for lo, hi, lbl in AGE_BUCKETS if lo <= age <= hi),
                     AGE_BUCKETS[-1][2])
        buckets[label]["count"] += 1
        buckets[label]["amount"] += outstanding
        rows.append({"loan": l, "age_days": age, "bucket": label,
                     "outstanding": outstanding})
    return {"rows": rows, "buckets": buckets,
            "total": sum((b["amount"] for b in buckets.values()), Decimal(0))}


# ---- 4. Maturity schedule ---------------------------------------------------

def maturity_schedule(loans=None, as_of=None):
    """Active loans with a maturity date, ordered by maturity, split into
    overdue / due-now buckets (current vs long-term at the 12-month line)."""
    as_of = as_of or _dt.date.today()
    loans = loans if loans is not None else _prefetched()
    rows = []
    for l in loans:
        if not l.maturity_date:
            continue
        outstanding = l.outstanding_asof(as_of)
        if outstanding <= 0:
            continue
        days = (l.maturity_date - as_of).days
        rows.append({"loan": l, "maturity_date": l.maturity_date,
                     "outstanding": outstanding, "days": days,
                     "overdue": days < 0,
                     "current": 0 <= days <= 365,
                     "long_term": days > 365})
    rows.sort(key=lambda r: r["maturity_date"])
    return rows


# ---- 5/6. By fund / by lender ----------------------------------------------

def by_fund(loans=None, as_of=None):
    as_of = as_of or _dt.date.today()
    loans = loans if loans is not None else _prefetched()
    agg = {}
    for l in loans:
        row = agg.setdefault(l.fund_id, {
            "fund": l.fund, "count": 0, "received": Decimal(0),
            "repaid": Decimal(0), "outstanding": Decimal(0)})
        row["count"] += 1
        row["received"] += l.received_total
        row["repaid"] += l.principal_repaid
        row["outstanding"] += l.outstanding_asof(as_of)
    return sorted(agg.values(), key=lambda r: -r["outstanding"])


def by_lender(loans=None, as_of=None):
    as_of = as_of or _dt.date.today()
    loans = loans if loans is not None else _prefetched()
    agg = {}
    for l in loans:
        row = agg.setdefault(l.lender_id, {
            "lender": l.lender, "count": 0, "received": Decimal(0),
            "repaid": Decimal(0), "outstanding": Decimal(0),
            "is_member": bool(l.lender.member_id)})
        row["count"] += 1
        row["received"] += l.received_total
        row["repaid"] += l.principal_repaid
        row["outstanding"] += l.outstanding_asof(as_of)
    return sorted(agg.values(), key=lambda r: -r["outstanding"])


# ---- 7/8/9. Transaction-level histories ------------------------------------

def _txn_rows(kind, start=None, end=None, loans=None):
    loans = loans if loans is not None else _prefetched()
    ids = [l.id for l in loans]
    qs = (LoanTransaction.objects.filter(kind=kind, loan_id__in=ids)
          .select_related("loan__lender", "loan__fund", "expense",
                          "receipt_transaction", "income_transaction")
          .order_by("date", "id"))
    if start:
        qs = qs.filter(date__gte=start)
    if end:
        qs = qs.filter(date__lte=end)
    return [t for t in qs if t.effective]


def repayment_history(start=None, end=None, loans=None):
    return _txn_rows(LoanTransaction.Kind.PRINCIPAL, start, end, loans)


def interest_history(start=None, end=None, loans=None):
    return _txn_rows(LoanTransaction.Kind.INTEREST, start, end, loans)


def conversions(start=None, end=None, loans=None):
    return (_txn_rows(LoanTransaction.Kind.CONVERSION, start, end, loans)
            + _txn_rows(LoanTransaction.Kind.WRITE_OFF, start, end, loans))


# ---- 10. Financing activities (cash flow) ----------------------------------

def financing_activity(start=None, end=None):
    """Loan-related cash movements for the Cash Flow Statement's financing
    section: receipts (in), principal repayments (out), interest (out —
    system policy classes interest under financing here). Conversions and
    write-offs move no cash and are excluded."""
    loans = _prefetched()
    receipts = sum((t.amount for t in
                    _txn_rows(LoanTransaction.Kind.RECEIPT, start, end, loans)),
                   Decimal(0))
    repayments = sum((t.amount for t in
                      repayment_history(start, end, loans)), Decimal(0))
    interest = sum((t.amount for t in
                    interest_history(start, end, loans)), Decimal(0))
    return {"receipts": receipts, "repayments": repayments,
            "interest": interest,
            "net_financing": receipts - repayments - interest}


# ---- Financial-statement classification (Statement of Financial Position) --

def outstanding_liability(as_of=None, split_current=True):
    """Total outstanding loan principal as at a date, optionally split into
    current (matures within 12 months, or no maturity date = payable on
    demand = current) vs long-term (matures beyond 12 months). This is the
    Loans payable liability line on the balance sheet, and by construction it
    equals the net credit on the LOANS_PAYABLE ledger account."""
    as_of = as_of or _dt.date.today()
    horizon = as_of + _dt.timedelta(days=365)
    current = long_term = Decimal(0)
    for l in _prefetched().filter(loan_date__lte=as_of):
        bal = l.outstanding_asof(as_of)
        if bal <= 0:
            continue
        if split_current and l.maturity_date and l.maturity_date > horizon:
            long_term += bal
        else:
            current += bal
    return {"current": current, "long_term": long_term,
            "total": current + long_term}


def interest_expense(start=None, end=None):
    """Interest actually paid in the period — the Finance Costs line for the
    Income & Expenditure statement."""
    return sum((t.amount for t in interest_history(start, end)), Decimal(0))


def retirement_income(start=None, end=None):
    """Income recognised from loan conversions / write-offs in the period.
    These are NON-CASH: the liability is reclassified to income with no cash
    movement, so the cash flow statement must exclude this amount from
    operating cash receipts (it is otherwise counted, since the income leg is
    a normal, non-excluded contribution credit)."""
    total = Decimal(0)
    for t in conversions(start, end):
        if t.income_transaction_id and t.effective:
            total += t.amount
    return total
