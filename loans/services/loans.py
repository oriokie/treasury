"""Loan business logic. Every operation creates the loan-side index row AND
its underlying source document(s) atomically, so the ledger, fund balances and
loan balances can never disagree (all three are computed from the same
documents).

Ledger shapes (posted automatically by the existing signals when the
documents are saved — no loan-specific posting path exists):

  record_receipt     giving.Transaction (excluded_from_income, on the fund)
                     -> DR Cash / CR Loans payable
  record_repayment   Expense category=LOAN_REPAYMENT
                     -> DR Loans payable / CR Cash   (excluded from I&E)
  record_interest    Expense category=LOAN_INTEREST
                     -> DR Loan interest expense / CR Cash
  convert/write_off  income Transaction + LOAN_REPAYMENT Expense (same date,
                     same amount) -> nets to DR Loans payable / CR Income,
                     zero cash movement
"""
import datetime as _dt
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction as db_tx
from django.db.models import Q

from members.models import name_key, normalize_phone

from loans.models import Lender, Loan, LoanTransaction


# ---- Lender resolution ------------------------------------------------------

def match_or_create_lender(name, phone, national_id=None):
    """Resolve (or create) the lender for a payment — same conservative shape
    as members.services.matching.match_or_create_member: phone is the trusted
    signal, an unambiguous name match is accepted, otherwise create (a loan
    receipt is never orphaned). NEVER creates a church Member.

    Returns (lender, how) where how is matched_phone|matched_id|matched_name|created.
    """
    ph = normalize_phone(phone)
    key = name_key(name)
    live = Lender.objects.filter(merged_into__isnull=True)

    if national_id:
        m = live.filter(national_id=national_id).first()
        if m:
            return m, "matched_id"
    if ph:
        m = live.filter(phone=ph).first()
        if m:
            return m, "matched_phone"
    if key:
        qs = live.filter(name_key=key)
        if qs.count() == 1:
            m = qs.first()
            if not m.phone and ph:
                m.phone = ph
                m.save()
            if not ph or m.phone == ph:
                return m, "matched_name"
    lender = Lender.objects.create(
        name=(name or "").strip() or "(unknown lender)",
        phone=ph or "", national_id=national_id or "",
        source=Lender.Source.AUTO_BANK)
    return lender, "created"


def merge_lenders(keep, absorb, user=None):
    """Repoint the duplicate's loans onto the kept lender and retire it (kept
    on record with merged_into for the audit trail, like member merges)."""
    if keep.pk == absorb.pk:
        raise ValidationError("Cannot merge a lender into itself.")
    with db_tx.atomic():
        absorb.loans.update(lender=keep)
        for f in ("phone", "email", "national_id", "address"):
            if not getattr(keep, f) and getattr(absorb, f):
                setattr(keep, f, getattr(absorb, f))
        if not keep.member_id and absorb.member_id:
            keep.member_id = absorb.member_id
        keep.save()
        absorb.status = Lender.Status.INACTIVE
        absorb.merged_into = keep
        absorb.save()
    return keep


def possible_duplicate_lenders(name=None, phone=None, national_id=None, exclude_pk=None):
    """Lenders that look like the same person (shared phone / ID / name key)."""
    q = Q()
    ph = normalize_phone(phone)
    if ph:
        q |= Q(phone=ph)
    if national_id:
        q |= Q(national_id=national_id)
    k = name_key(name)
    if k:
        q |= Q(name_key=k)
    if not q:
        return Lender.objects.none()
    qs = Lender.objects.filter(q, merged_into__isnull=True)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    return qs


def loan_for_receipt(lender, fund, date, user=None, purpose=""):
    """The loan a new receipt belongs to: the lender's open loan on the same
    fund if one exists (a further drawdown), otherwise a new loan."""
    loan = (Loan.objects.filter(lender=lender, fund=fund, status=Loan.Status.ACTIVE)
            .order_by("-loan_date").first())
    if loan:
        return loan, False
    loan = Loan.objects.create(
        lender=lender, fund=fund, loan_date=date, purpose=purpose or "",
        status=Loan.Status.ACTIVE, created_by=user)
    return loan, True


# ---- Recording loan money ---------------------------------------------------

def _require_editable(loan):
    if not loan.is_editable:
        raise ValidationError(
            f"{loan.number} is {loan.get_status_display().lower()} and can no "
            f"longer be transacted on.")


@db_tx.atomic
def record_receipt(loan, *, date, amount, user=None, note="",
                   channel=None, core_ref=None, bank_receipt=None,
                   mpesa_ref="", payer_name="", payer_phone="",
                   raw_narration="", bank_account=None, statement_import=None,
                   existing_transaction=None, into_petty_cash=False):
    """Loan money in. Creates (or adopts) the fund credit and indexes it.
    The credit is excluded_from_income: it raises the fund's available cash
    and the bank reconciliation exactly like any bank credit, but never its
    income, and the ledger books it as a liability.

    into_petty_cash: the loan money physically landed in the petty-cash box
    rather than the bank. Reuses the existing petty-cash float mechanism — a
    PettyCashTopUp is created so the float rises by exactly this amount (the
    same way any other cash entering the box does). The ledger posting is
    unchanged (petty cash and bank share the single CASH account); only the
    cash-location control total differs.
    """
    from giving.models import Transaction
    _require_editable(loan)
    amount = Decimal(amount)
    if amount <= 0:
        raise ValidationError("A loan receipt must be a positive amount.")

    if existing_transaction is not None:
        txn = existing_transaction
        if hasattr(txn, "loan_receipt"):
            raise ValidationError("That bank credit is already recorded as a loan receipt.")
        if txn.direction != Transaction.Direction.CREDIT:
            raise ValidationError("Only a bank CREDIT can be a loan receipt.")
        txn.department = loan.fund
        txn.excluded_from_income = True
        txn.allocation_status = Transaction.Status.MANUAL
        txn.member = None            # a lender is not automatically a member
        txn.save()
    else:
        from core.models import service_sabbath_for
        from core.utils import sabbath_week_of
        svc = service_sabbath_for(date)
        txn = Transaction.objects.create(
            date=date, service_sabbath=svc, sabbath_week=sabbath_week_of(svc),
            channel=channel or Transaction.Channel.BANK,
            direction=Transaction.Direction.CREDIT, amount=amount,
            department=loan.fund, excluded_from_income=True,
            allocation_status=Transaction.Status.MANUAL,
            reference=(f"LOAN {loan.number}")[:60],
            payer_name=(payer_name or loan.lender.name)[:120],
            payer_phone=(payer_phone or loan.lender.phone or "")[:12],
            core_ref=core_ref, bank_receipt=bank_receipt,
            mpesa_ref=(mpesa_ref or "")[:30], raw_narration=raw_narration or "",
            bank_account=bank_account, statement_import=statement_import)

    topup = None
    if into_petty_cash:
        # the loan cash entered the petty box: raise the float via the existing
        # petty-cash top-up mechanism (a cash-location movement, not a fund
        # movement — fund balances are unaffected, exactly as for other top-ups)
        from cashbook.models import PettyCashTopUp
        topup = PettyCashTopUp.objects.create(
            date=txn.date, amount=amount,
            note=f"Loan receipt {loan.number} — {loan.lender.name}"[:200],
            recorded_by=user)
        if channel is None:
            txn.channel = Transaction.Channel.CASH
            txn.save(update_fields=["channel"])

    lt = LoanTransaction.objects.create(
        loan=loan, kind=LoanTransaction.Kind.RECEIPT, date=txn.date,
        amount=txn.amount, receipt_transaction=txn, petty_topup=topup,
        note=note, created_by=user)
    # the receipt txn may have been saved before the link existed — re-post so
    # the ledger books it against Loans payable, not income
    _repost(txn)
    loan.refresh_status()
    return lt


def _repost(txn):
    from ledger.services import posting
    if posting.chart_ready():
        posting.post_transaction(txn)


@db_tx.atomic
def record_repayment(loan, *, date, amount, user, method=None, voucher_no="",
                     note="", auto_approve=True, bank_transaction=None,
                     paid_from_petty_cash=False):
    """Principal out. An Expense (category LOAN_REPAYMENT) charged to the
    financed fund — reduces the fund's available cash and posts DR Loans
    payable / CR Cash; excluded from the I&E statement, exactly like a trust
    remittance (a liability settlement is not expenditure)."""
    from cashbook.models import Expense
    _require_editable(loan)
    amount = Decimal(amount)
    if amount <= 0:
        raise ValidationError("A repayment must be a positive amount.")
    if amount > loan.outstanding_principal:
        raise ValidationError(
            f"Repayment of {amount} exceeds the outstanding principal "
            f"({loan.outstanding_principal}) on {loan.number}.")
    exp = Expense.objects.create(
        date=date, department=loan.fund,
        description=f"Loan repayment {loan.number} — {loan.lender.name}"[:200],
        amount=amount, category=Expense.Category.LOAN_REPAYMENT,
        funding_source=Expense.FundingSource.LOAN,
        claimant=loan.lender.name[:120],
        method=method or Expense.Method.BANK, voucher_no=voucher_no,
        paid_from_petty_cash=paid_from_petty_cash,
        status=(Expense.Status.PAID if auto_approve else Expense.Status.PENDING),
        recorded_by=user, approved_by=(user if auto_approve else None),
        paid_date=(date if auto_approve else None),
        bank_transaction=bank_transaction)
    lt = LoanTransaction.objects.create(
        loan=loan, kind=LoanTransaction.Kind.PRINCIPAL, date=date, amount=amount,
        expense=exp, note=note, created_by=user)
    loan.refresh_status()
    return lt


@db_tx.atomic
def record_interest(loan, *, date, amount, user, method=None, voucher_no="",
                    note="", auto_approve=True, bank_transaction=None):
    """Interest out — a genuine expense (in I&E), charged to the financed fund."""
    from cashbook.models import Expense
    _require_editable(loan)
    amount = Decimal(amount)
    if amount <= 0:
        raise ValidationError("An interest payment must be a positive amount.")
    exp = Expense.objects.create(
        date=date, department=loan.fund,
        description=f"Loan interest {loan.number} — {loan.lender.name}"[:200],
        amount=amount, category=Expense.Category.LOAN_INTEREST,
        funding_source=Expense.FundingSource.LOAN,
        claimant=loan.lender.name[:120],
        method=method or Expense.Method.BANK, voucher_no=voucher_no,
        status=(Expense.Status.PAID if auto_approve else Expense.Status.PENDING),
        recorded_by=user, approved_by=(user if auto_approve else None),
        paid_date=(date if auto_approve else None),
        bank_transaction=bank_transaction)
    lt = LoanTransaction.objects.create(
        loan=loan, kind=LoanTransaction.Kind.INTEREST, date=date, amount=amount,
        expense=exp, note=note, created_by=user)
    return lt


def _require_different_approver(loan, user, doing):
    """Segregation of duties on the only two loan actions that turn a
    liability into income without anybody paying anything back.

    Recording a receipt, a repayment or an interest payment all leave a
    counterparty and a bank movement behind; conversion and write-off do not —
    they simply declare that the church keeps the money. Without this guard one
    treasurer could open a loan, receipt the cash against it and then, alone,
    convert the balance to a 'donation' that lands in contribution income. That
    is precisely the sequence expense approval has refused since
    SiteConfig.require_different_approver was introduced (cashbook's approve
    action refuses when exp.recorded_by is the approver), and the envelope
    batch and benevolent-case decisions read the same switch. This is the loan
    module reading that ONE switch rather than growing a rule of its own.

    Which actor it compares against: Loan.created_by, the person who put the
    liability on the books. It is the loan module's analogue of
    Expense.recorded_by — the only actor the loan itself records — and it is
    the one that makes the abusive sequence above impossible. (Deliberately not
    the receipt recorder: receipts are frequently entered by the bank importer
    or a different clerk, so keying off them would block innocent conversions
    while still letting the loan's own author retire it.)

    It stays a no-op when the flag is off (the default — many installs have a
    single active treasurer) and when the loan has no recorded creator, which
    is the normal case for loans opened automatically from a bank narration:
    exactly like the expense check, which never fires on a document with no
    recorded_by.
    """
    from core.models import SiteConfig
    if not SiteConfig.get().require_different_approver:
        return
    if user is not None and loan.created_by_id and loan.created_by_id == user.pk:
        raise ValidationError(
            f"You recorded {loan.number} yourself — {doing} must be done by a "
            f"different treasurer (Settings → require a different approver).")


def _retire(loan, kind, *, date, amount, user, note, as_contribution):
    """Shared shape for conversion and write-off: the lender lets the church
    keep the money, so the liability is retired against income with no cash
    movement — expressed as a contra PAIR of ordinary documents (an income
    credit and a LOAN_REPAYMENT expense of the same amount, same date) whose
    combined posting is exactly DR Loans payable / CR Income. Using the two
    standard document types means /ledger/rebuild/, the fund ledger, I&E and
    the cash book all handle it with no special cases: the fund's cash is
    unchanged (the two halves cancel) while its income rises."""
    from cashbook.models import Expense
    from giving.models import Transaction
    from core.models import service_sabbath_for
    from core.utils import sabbath_week_of
    _require_editable(loan)
    _require_different_approver(
        loan, user,
        "converting it to a donation" if kind == LoanTransaction.Kind.CONVERSION
        else "writing it off")
    amount = Decimal(amount)
    if amount <= 0:
        raise ValidationError("Amount must be positive.")
    if amount > loan.outstanding_principal:
        raise ValidationError(
            f"{amount} exceeds the outstanding principal "
            f"({loan.outstanding_principal}) on {loan.number}.")
    label = ("Loan converted to donation" if kind == LoanTransaction.Kind.CONVERSION
             else "Loan written off")
    with db_tx.atomic():
        svc = service_sabbath_for(date)
        txn = Transaction.objects.create(
            date=date, service_sabbath=svc, sabbath_week=sabbath_week_of(svc),
            channel=Transaction.Channel.CASH, direction=Transaction.Direction.CREDIT,
            amount=amount, department=loan.fund,
            member=(loan.lender.member if as_contribution else None),
            allocation_status=Transaction.Status.MANUAL,
            reference=(f"{label.upper()} {loan.number}")[:60],
            payer_name=loan.lender.name[:120],
            payer_phone=(loan.lender.phone or "")[:12],
            raw_narration=f"{label}: {loan.number} ({note})" if note else f"{label}: {loan.number}")
        exp = Expense.objects.create(
            date=date, department=loan.fund,
            description=f"{label} {loan.number} — liability retired (contra)"[:200],
            amount=amount, category=Expense.Category.LOAN_REPAYMENT,
            funding_source=Expense.FundingSource.OTHER,
            claimant=loan.lender.name[:120], method=Expense.Method.CASH,
            status=Expense.Status.PAID, recorded_by=user, approved_by=user,
            paid_date=date)
        lt = LoanTransaction.objects.create(
            loan=loan, kind=kind, date=date, amount=amount,
            income_transaction=txn, expense=exp, note=note, created_by=user)
        loan.refresh_status()
    return lt


def convert_to_donation(loan, *, date, amount=None, user, note=""):
    """The lender gifts (part of) the loan: liability -> contribution income,
    attributed to the lender's linked member (if any), dated today — so it
    appears in contribution reports and the member's statement."""
    amount = amount if amount is not None else loan.outstanding_principal
    return _retire(loan, LoanTransaction.Kind.CONVERSION, date=date, amount=amount,
                   user=user, note=note, as_contribution=True)


def write_off(loan, *, date, amount=None, user, note=""):
    """The debt is forgiven/irrecoverable: liability -> income, without
    contribution attribution."""
    amount = amount if amount is not None else loan.outstanding_principal
    return _retire(loan, LoanTransaction.Kind.WRITE_OFF, date=date, amount=amount,
                   user=user, note=note, as_contribution=False)


# ---- Fund-side aggregates (for fund dashboards / reports) -------------------

def loan_financing_by_fund(start=None, end=None):
    """Loan money received per fund (effective receipts). This is the cash a
    fund holds that is FINANCED rather than contributed."""
    out = {}
    qs = (LoanTransaction.objects.filter(kind=LoanTransaction.Kind.RECEIPT)
          .select_related("loan", "receipt_transaction"))
    if start:
        qs = qs.filter(date__gte=start)
    if end:
        qs = qs.filter(date__lte=end)
    for t in qs:
        if t.effective:
            out[t.loan.fund_id] = out.get(t.loan.fund_id, Decimal(0)) + t.amount
    return out


def outstanding_by_fund():
    """Outstanding loan principal per fund — a fund's Net Position is its
    available cash less this liability."""
    out = {}
    for loan in (Loan.objects.exclude(status=Loan.Status.DRAFT)
                 .prefetch_related("transactions__receipt_transaction",
                                   "transactions__income_transaction",
                                   "transactions__expense")):
        bal = loan.outstanding_principal
        if bal:
            out[loan.fund_id] = out.get(loan.fund_id, Decimal(0)) + bal
    return out


def fund_loan_summary(dept):
    """The financing block for one fund's dashboard."""
    loans = list(Loan.objects.filter(fund=dept).exclude(status=Loan.Status.DRAFT)
                 .select_related("lender"))
    financing = sum((l.received_total for l in loans), Decimal(0))
    outstanding = sum((l.outstanding_principal for l in loans), Decimal(0))
    return {"loans": loans, "financing": financing, "outstanding": outstanding,
            "has_loans": bool(loans)}


# ---- Bank intake (shared by the file importer and the live webhook) ---------

def intake_bank_receipt(pattern, *, date, amount, reference, phone, name,
                        raw_narration, core_ref=None, bank_receipt=None,
                        mpesa_ref="", bank_account=None, statement_import=None):
    """Full automatic intake of a bank credit recognised as a loan receipt by a
    narration pattern that names the financed fund: resolve (or create) the
    lender — never a church Member — attach to their open loan on that fund
    (or open a new one), and record the receipt. One shared implementation so
    the statement importer and the live webhook can never drift apart.

    Returns the created LoanTransaction."""
    lender, _how = match_or_create_lender(name, phone)
    loan, _created = loan_for_receipt(
        lender, pattern.fund, date,
        purpose=f"Bank narration: {(reference or '')[:60]}")
    return record_receipt(
        loan, date=date, amount=amount,
        payer_name=name or "", payer_phone=phone or "",
        core_ref=core_ref, bank_receipt=bank_receipt, mpesa_ref=mpesa_ref,
        raw_narration=raw_narration or "", bank_account=bank_account,
        statement_import=statement_import)


# ---- Departmental visibility (leaders & department-scoped staff) ------------

def loans_for_departments(dept_ids):
    """Loans linked to any of the given departments/funds. Used to scope a
    department leader (or a department-scoped user) to only their own funds'
    loans — reusing the same id set the rest of the leader area is filtered by
    (departments_led_by / allowed_departments), so security stays consistent."""
    dept_ids = list(dept_ids)
    if not dept_ids:
        return Loan.objects.none()
    return (Loan.objects.filter(fund_id__in=dept_ids)
            .exclude(status=Loan.Status.DRAFT)
            .select_related("lender", "fund")
            .prefetch_related("transactions__receipt_transaction",
                              "transactions__income_transaction",
                              "transactions__expense")
            .order_by("-loan_date", "-id"))


def user_has_accessible_loans(user):
    """Whether a department leader has at least one loan on a fund they can
    access — drives conditional menu visibility so an empty Loans page is
    never shown to a leader with no loans."""
    from leaders.permissions import allowed_departments
    ids = list(allowed_departments(user).values_list("id", flat=True))
    if not ids:
        return False
    return Loan.objects.filter(fund_id__in=ids).exclude(
        status=Loan.Status.DRAFT).exists()
