"""Recording an expense — one implementation, used by every entry path.

There are four ways an expense reaches this system: the form, the spreadsheet
import, the batch entry screen, and a recurring schedule. Before this module
three of them carried their own copy of the same two rules — what status a new
expense takes, and what to do with a transaction charge — and the copies had
already drifted:

* the form set `approved_by` on the charge row only when auto-approving;
* the import set `paid_date` on it but never `claimant` consistently;
* one copy omitted `payee` from the charge row entirely.

None of that is visible in a test that checks the parent expense. It shows up
in a bank reconciliation months later, when a charge row is missing the payee
that would have matched it. So the rules live here now, once.

**The transaction charge is a separate expense, not a bigger one.** A 2,000
transfer costing 30 in M-Pesa fees is 2,000 of transport and 30 of bank charge —
adding them together would misstate both categories and quietly inflate what the
church believes it spent on transport. The charge row is linked back through
`charge_for`, so the pair can always be found again.
"""
from decimal import Decimal

from django.db import transaction as db_tx

from core.utils import sabbath_week_of

from ..models import Expense


def new_expense_status(*, paid_from_petty_cash=False, auto_approve=False, user=None):
    """What state a newly recorded expense starts in.

    Three cases, and the first is the one people forget: money already out of
    the petty cash tin is *paid*, not pending. Recording it as awaiting approval
    would leave the float disagreeing with the drawer until someone approved a
    payment that had already happened.
    """
    if paid_from_petty_cash:
        return Expense.Status.PAID, user, True          # status, approver, is_paid
    if auto_approve:
        return Expense.Status.APPROVED, user, False
    return Expense.Status.PENDING, None, False


@db_tx.atomic
def record(*, date, department, description, amount, user,
           category=Expense.Category.OTHER, method=Expense.Method.CASH,
           claimant="", payee="", voucher_no="", vendor=None,
           expenditure_type=None, budget_line=None,
           paid_from_petty_cash=False, auto_approve=False,
           charge=None, extra=None):
    """Create one expense, and its transaction charge if there is one.

    Returns ``(expense, charge_expense_or_None)``. Every caller gets the same
    status rules, the same sabbath week, and the same charge treatment.
    """
    status, approver, is_paid = new_expense_status(
        paid_from_petty_cash=paid_from_petty_cash,
        auto_approve=auto_approve, user=user)

    fields = dict(
        date=date, sabbath_week=sabbath_week_of(date), department=department,
        description=(description or "")[:200], amount=Decimal(str(amount)),
        category=category, method=method,
        claimant=(claimant or "")[:120], payee=(payee or "")[:160],
        voucher_no=(voucher_no or "")[:30], vendor=vendor,
        paid_from_petty_cash=bool(paid_from_petty_cash),
        recorded_by=user, status=status, approved_by=approver,
        paid_date=date if is_paid else None)
    if expenditure_type:
        fields["expenditure_type"] = expenditure_type
    if budget_line is not None:
        fields["budget_line"] = budget_line
    if extra:
        fields.update(extra)

    expense = Expense.objects.create(**fields)

    charge_expense = None
    charge = Decimal(str(charge or 0))
    if charge > 0:
        charge_expense = _record_charge(expense, charge, user)
    return expense, charge_expense


def _record_charge(expense, amount, user):
    """The bank/M-Pesa fee, as its own bank-charge expense on the same fund.

    It inherits the parent's status, payee, claimant and voucher deliberately:
    a charge that is pending while its expense is approved would sit in the
    approval queue forever, and one with no payee cannot be matched on the bank
    statement. `charge_for` keeps the two findable as a pair.
    """
    reference = expense.voucher_no or f"exp #{expense.id}"
    return Expense.objects.create(
        date=expense.date, sabbath_week=expense.sabbath_week,
        department=expense.department,
        description=f"Transaction charge — {expense.description} [for {reference}]"[:200],
        amount=Decimal(str(amount)), category=Expense.Category.BANK_CHARGE,
        method=expense.method, claimant=expense.claimant, payee=expense.payee,
        voucher_no=expense.voucher_no, vendor_id=expense.vendor_id,
        paid_from_petty_cash=expense.paid_from_petty_cash,
        recorded_by=user, charge_for=expense,
        status=expense.status, paid_date=expense.paid_date,
        approved_by=expense.approved_by)


@db_tx.atomic
def record_batch_charge(*, header, expenses, amount, user, auto_approve=False):
    """One transaction charge covering a whole batch.

    When a stack of receipts is settled with a single transfer, the bank takes
    one fee for that transfer, not one per receipt. Splitting it across the
    lines would invent charges that were never levied; charging it to each line
    would multiply it.

    So it is recorded once, as its own bank-charge expense on the batch's fund.

    **It is deliberately left unlinked** (`charge_for` is null). That field means
    "the charge levied on *this* expense", and the batch fee belongs to no
    single line — attaching it to the first one would both misstate that line
    and expose the fee to being deleted when that line's own charge is edited,
    since `ExpenseUpdate` replaces an expense's linked charges wholesale. The
    description and the shared voucher number are what tie it to the batch.
    """
    amount = Decimal(str(amount or 0))
    if amount <= 0 or not expenses:
        return None

    status, approver, is_paid = new_expense_status(
        paid_from_petty_cash=header.get("paid_from_petty_cash", False),
        auto_approve=auto_approve, user=user)
    date = header["date"]
    reference = header.get("voucher_no") or f"{date:%Y-%m-%d}"
    total = sum((e.amount for e in expenses), Decimal("0"))

    return Expense.objects.create(
        date=date, sabbath_week=sabbath_week_of(date),
        department=header["department"],
        description=(f"Transaction charge — one payment covering "
                     f"{len(expenses)} item(s) totalling {total:,.2f} "
                     f"[for {reference}]")[:200],
        amount=amount, category=Expense.Category.BANK_CHARGE,
        method=header.get("method") or Expense.Method.CASH,
        claimant=(header.get("claimant") or "")[:120],
        payee=(header.get("payee") or "")[:160],
        voucher_no=(header.get("voucher_no") or "")[:30],
        vendor=header.get("vendor"),
        paid_from_petty_cash=bool(header.get("paid_from_petty_cash")),
        recorded_by=user, status=status, approved_by=approver,
        paid_date=date if is_paid else None)


def _line_or_header(line, header, key, *, blank_as_default=True):
    """Prefer a per-line value; fall back to the shared header default.

    Blank strings (and missing keys) mean "use the header" so a treasurer can
    override only the fields that actually differ on that receipt.
    """
    if key not in line:
        return header.get(key)
    value = line.get(key)
    if blank_as_default and (value is None or value == ""):
        return header.get(key)
    return value


@db_tx.atomic
def record_batch(*, header, lines, user, auto_approve=False,
                 shared_charge=None):
    """Several expenses that usually share a date, fund, claimant and method.

    The common case this exists for: a treasurer settling a stack of receipts
    that mostly share those facts. Re-entering them for every line is the slow
    part, and the part that produces inconsistent data when someone mistypes
    the fund on line seven.

    `header` supplies the defaults; each line supplies description, amount, an
    optional category override, an optional transaction charge, and optional
    overrides for department, claimant, payee, vendor, method, date and
    voucher. Blank line fields keep the header default. Atomic: either the
    whole stack is recorded or none of it, because a half-entered batch is
    worse than none.

    `shared_charge` covers the case a per-line charge cannot express: the whole
    stack settled with one transfer, attracting one fee. The two are added
    rather than exclusive — a treasurer may pay most of a stack in one transfer
    and one item separately.
    """
    created = []
    principals = []
    for line in lines:
        description = (line.get("description") or "").strip()
        amount = line.get("amount")
        if not description or not amount or Decimal(str(amount)) <= 0:
            continue          # blank rows are how a variable-length form ends
        expense, charge_expense = record(
            date=_line_or_header(line, header, "date") or header["date"],
            department=(_line_or_header(line, header, "department")
                        or header["department"]),
            description=description, amount=amount, user=user,
            category=line.get("category") or header.get("category")
            or Expense.Category.OTHER,
            method=(_line_or_header(line, header, "method")
                    or Expense.Method.CASH),
            claimant=_line_or_header(line, header, "claimant") or "",
            payee=_line_or_header(line, header, "payee") or "",
            vendor=_line_or_header(line, header, "vendor"),
            voucher_no=_line_or_header(line, header, "voucher_no") or "",
            expenditure_type=header.get("expenditure_type"),
            budget_line=header.get("budget_line"),
            paid_from_petty_cash=header.get("paid_from_petty_cash", False),
            auto_approve=auto_approve, charge=line.get("charge"))
        created.append(expense)
        principals.append(expense)
        if charge_expense is not None:
            created.append(charge_expense)

    batch_charge = record_batch_charge(
        header=header, expenses=principals, amount=shared_charge,
        user=user, auto_approve=auto_approve)
    if batch_charge is not None:
        created.append(batch_charge)
    return created
