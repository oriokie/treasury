"""Supporting-document (receipt) helpers — the acceptable-file rule, and the
queue of expenses still missing a receipt.

Pure logic, extracted verbatim from cashbook/views.py. Behaviour is unchanged;
cashbook/views.py re-exports these names (and the two constants) so every
existing call site keeps working — the treasurer and leader expense pages, the
advance detail, the missing-receipts queue, and core/leaders/statements
imports of `missing_receipts_queryset` / `validate_receipt_upload`.
"""
RECEIPT_ALLOWED_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif")
RECEIPT_MAX_BYTES = 1 * 1024 * 1024   # 1 MB — receipts are photos/scans, not archives


def validate_receipt_upload(f):
    """Return an error string if a receipt file is not acceptable, else None.
    Shared by every place a supporting document can be attached (treasurer
    expense page, leader expense page, advance detail, missing-receipts queue)."""
    if not f:
        return None
    if not f.name.lower().endswith(RECEIPT_ALLOWED_EXT):
        return ("Receipts must be a PDF or image file "
                "(.pdf, .jpg, .png, .heic, .webp).")
    if f.size > RECEIPT_MAX_BYTES:
        return ("That file is too large — receipts must be 1 MB or smaller. "
                "Tip: a phone photo at normal quality, or a compressed PDF, "
                "fits easily.")
    return None


def missing_receipts_queryset(start, end, department_ids=None):
    """Expenses in the period that have no supporting document (no attachment
    file, text or link). Charge lines are excluded — they ride on their parent.
    Once any attachment is added, the expense leaves this queue.

    Also excludes a loan CONVERSION/WRITE_OFF's contra expense: that "expense"
    is one half of a same-day, same-amount book-entry pair (see
    loans.services.loans._retire) that retires a liability against income
    with no cash ever changing hands — there is no physical transaction for a
    receipt to document, so it can never leave this queue by any real action
    a treasurer could take. An ordinary loan PRINCIPAL/INTEREST repayment
    (also category=LOAN_REPAYMENT, but a genuine cash disbursement) still
    correctly appears here until its proof of payment is attached."""
    from cashbook.models import Expense
    from loans.models import LoanTransaction
    qs = (Expense.objects.filter(date__gte=start, date__lte=end,
                                 attachments__isnull=True)
          .exclude(category=Expense.Category.BANK_CHARGE)
          .exclude(loan_transaction__kind__in=[LoanTransaction.Kind.CONVERSION,
                                               LoanTransaction.Kind.WRITE_OFF])
          .select_related("department", "recorded_by")
          .order_by("-date", "-id"))
    if department_ids is not None:
        qs = qs.filter(department_id__in=department_ids)
    return qs
