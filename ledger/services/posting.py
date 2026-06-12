"""Seed the chart of accounts and post balanced journal entries from the source
documents. The general ledger is a faithful projection of what users record:

  Local receipt      DR Cash            CR Income (by kind)
  Trust receipt      DR Cash            CR Trust funds payable        (a liability)
  Recurrent expense  DR Expense         CR Cash
  Capital expense    DR Fixed assets    CR Cash
  Trust remittance   DR Trust payable   CR Cash
  Opening balances   DR Cash (+assets)  CR Accumulated funds / Trust payable

Inter-fund transfers are equity reclassifications that net to zero for the whole
church, so they carry no general-ledger effect (their per-fund effect is shown in
the fund reports). Bank DEBIT transactions are represented by their expense, so
they are not posted twice.
"""
import datetime as dt
from decimal import Decimal

from django.db import transaction as db_tx

from ledger.models import Account, JournalEntry, JournalLine


# ---- Chart of accounts ----------------------------------------------------
CHART = [
    ("1000", "Cash & bank", "ASSET", "CASH"),
    ("1500", "Fixed assets", "ASSET", "FIXED_ASSETS"),
    ("2000", "Trust funds payable", "LIABILITY", "TRUST_PAYABLE"),
    ("3000", "Accumulated fund balances", "EQUITY", "ACCUM_FUNDS"),
    ("3100", "Capital (asset) fund", "EQUITY", "CAPITAL_FUND"),
    ("4100", "Tithe", "INCOME", "INC_TITHE"),
    ("4200", "Offerings & general income", "INCOME", "INC_OFFERINGS"),
    ("4300", "Development & projects", "INCOME", "INC_DEVELOPMENT"),
    ("4900", "Other income", "INCOME", "INC_OTHER"),
]
# expense accounts are generated per Expense.Category as 5xxx
EXPENSE_BASE = 5100


def ensure_chart():
    """Create the chart of accounts if missing. Idempotent."""
    from cashbook.models import Expense
    for code, name, typ, key in CHART:
        Account.objects.get_or_create(
            system_key=key, defaults={"code": code, "name": name, "type": typ})
    # one expense account per category
    for i, (val, label) in enumerate(Expense.Category.choices):
        Account.objects.get_or_create(
            system_key=f"EXP_{val}",
            defaults={"code": str(EXPENSE_BASE + i), "name": label, "type": "EXPENSE"})


def _acct(key):
    return Account.objects.filter(system_key=key).first()


def chart_ready():
    return Account.objects.filter(system_key="CASH").exists()


def _income_key_for(dept):
    """Map a local fund to an income account by its name."""
    n = (dept.name or "").lower()
    if "tithe" in n:
        return "INC_TITHE"
    if "develop" in n or "project" in n:
        return "INC_DEVELOPMENT"
    if "offering" in n or "budget" in n or "church" in n or "youth" in n or "sabbath" in n:
        return "INC_OFFERINGS"
    return "INC_OFFERINGS"


def _entry(date, memo, source_type, source_id, lines):
    """lines: list of (account, debit, credit[, department]). Balanced JournalEntry."""
    je = JournalEntry.objects.create(date=date, memo=memo[:200],
                                     source_type=source_type, source_id=source_id)
    JournalLine.objects.bulk_create([
        JournalLine(entry=je, account=a, debit=d, credit=c,
                    department=(ln[3] if len(ln) > 3 else None))
        for ln in lines for (a, d, c) in [ln[:3]]])
    return je


def _post_pair(date, memo, stype, sid, debit_acct, credit_acct, amount, dept=None):
    """Post a simple two-line entry; negative amounts flip the sides (reversals).
    Both lines are tagged with the fund the entry concerns."""
    if not debit_acct or not credit_acct or amount == 0:
        return None
    a = abs(amount)
    if amount > 0:
        lines = [(debit_acct, a, Decimal(0), dept), (credit_acct, Decimal(0), a, dept)]
    else:
        lines = [(credit_acct, a, Decimal(0), dept), (debit_acct, Decimal(0), a, dept)]
    return _entry(date, memo, stype, sid, lines)


# ---- Posting individual documents ----------------------------------------
@db_tx.atomic
def post_transaction(txn):
    """Income receipt. Skips bank-debit-direction rows (represented by expenses)."""
    from giving.models import Transaction
    JournalEntry.objects.filter(source_type="transaction", source_id=txn.pk).delete()
    if (txn.direction != Transaction.Direction.CREDIT or not txn.confirmed
            or txn.is_reversed or txn.is_reversal):
        return
    cash = _acct("CASH")
    dept = txn.department
    if dept is not None and dept.is_trust:
        credit = _acct("TRUST_PAYABLE")
    else:
        credit = _acct(_income_key_for(dept)) if dept else _acct("INC_OTHER")
    who = txn.payer_name or txn.reference or "Receipt"
    _post_pair(txn.date, f"Receipt: {who}", "transaction", txn.pk, cash, credit, txn.amount, dept)


@db_tx.atomic
def post_expense(exp):
    from cashbook.models import Expense
    JournalEntry.objects.filter(source_type="expense", source_id=exp.pk).delete()
    if exp.status not in (Expense.Status.APPROVED, Expense.Status.PAID):
        return
    cash = _acct("CASH")
    if exp.category == Expense.Category.REMITTANCE:
        debit = _acct("TRUST_PAYABLE")
    elif exp.expenditure_type == Expense.ExpenditureType.CAPITAL:
        debit = _acct("FIXED_ASSETS")
    else:
        debit = _acct(f"EXP_{exp.category}") or _acct("EXP_OTHER")
    _post_pair(exp.date, f"{exp.get_category_display()}: {exp.description}",
               "expense", exp.pk, debit, cash, exp.amount, exp.department)


@db_tx.atomic
def post_transfer(tr):
    """Inter-fund transfer: an equity reclassification between two funds. Net zero
    for the whole church, but moves the fund-balance claim from source to
    destination (tagged by fund so the ledger reconciles to the fund reports)."""
    from cashbook.models import FundTransfer  # noqa
    JournalEntry.objects.filter(source_type="transfer", source_id=tr.pk).delete()
    accum = _acct("ACCUM_FUNDS")
    if not accum or tr.amount == 0:
        return
    a = abs(tr.amount)
    _entry(tr.date, f"Transfer {tr.source.name} → {tr.destination.name}",
           "transfer", tr.pk,
           [(accum, a, Decimal(0), tr.source),          # DR source fund equity
            (accum, Decimal(0), a, tr.destination)])     # CR destination fund equity


def post_opening():
    """A single dated entry establishing brought-forward balances, with one
    cash line and one equity/liability line per fund (fund-tagged)."""
    from departments.models import Department
    JournalEntry.objects.filter(source_type="opening").delete()
    cash = _acct("CASH"); accum = _acct("ACCUM_FUNDS"); trust = _acct("TRUST_PAYABLE")
    lines = []
    for d in Department.objects.all():
        ob = d.opening_balance or Decimal(0)
        if not ob:
            continue
        lines.append((cash, ob, Decimal(0), d))
        lines.append((trust if d.is_trust else accum, Decimal(0), ob, d))
    if not lines:
        return
    from giving.models import Transaction
    first = Transaction.objects.order_by("date").values_list("date", flat=True).first()
    d0 = dt.date((first.year if first else dt.date.today().year), 1, 1)
    _entry(d0, "Opening balances brought forward", "opening", None, lines)


@db_tx.atomic
def rebuild():
    """Regenerate the entire general ledger from the source documents."""
    from giving.models import Transaction
    from cashbook.models import Expense, FundTransfer
    ensure_chart()
    JournalEntry.objects.exclude(source_type="manual").delete()  # keep manual adjustments
    post_opening()
    for t in Transaction.objects.filter(direction=Transaction.Direction.CREDIT, confirmed=True):
        post_transaction(t)
    for e in Expense.objects.filter(status__in=[Expense.Status.APPROVED, Expense.Status.PAID]):
        post_expense(e)
    for tr in FundTransfer.objects.all():
        post_transfer(tr)
    return JournalEntry.objects.count()


def fund_balance_from_ledger(dept):
    """The fund's balance computed purely from the general ledger, on the same
    basis as the fund reports (so the two tie out exactly — the proof the ledger
    is a complete, authoritative system of record).

    Trust fund: net of its trust-funds-payable lines (collected less remitted).
    Local fund: its net claim on cash (opening + receipts − all payments) plus the
    equity effect of any inter-fund transfers.
    """
    from django.db.models import Sum
    if dept.is_trust:
        agg = (JournalLine.objects.filter(department=dept, account__system_key="TRUST_PAYABLE")
               .aggregate(d=Sum("debit"), c=Sum("credit")))
        return (agg["c"] or Decimal(0)) - (agg["d"] or Decimal(0))
    cash = (JournalLine.objects.filter(department=dept, account__system_key="CASH")
            .aggregate(d=Sum("debit"), c=Sum("credit")))
    cash_net = (cash["d"] or Decimal(0)) - (cash["c"] or Decimal(0))
    tr = (JournalLine.objects.filter(department=dept, account__system_key="ACCUM_FUNDS",
                                     entry__source_type="transfer")
          .aggregate(d=Sum("debit"), c=Sum("credit")))
    transfer_net = (tr["c"] or Decimal(0)) - (tr["d"] or Decimal(0))
    return cash_net + transfer_net


def accounting_equation():
    """Entity-level Assets = Liabilities + Equity (+ retained income − expense)."""
    from django.db.models import Sum
    def _net(types, credit_normal):
        agg = (JournalLine.objects.filter(account__type__in=types)
               .aggregate(d=Sum("debit"), c=Sum("credit")))
        d, c = (agg["d"] or Decimal(0)), (agg["c"] or Decimal(0))
        return (c - d) if credit_normal else (d - c)
    assets = _net(["ASSET"], False)
    liabilities = _net(["LIABILITY"], True)
    equity = _net(["EQUITY"], True)
    income = _net(["INCOME"], True)
    expense = _net(["EXPENSE"], False)
    funds = equity + income - expense          # equity incl. undistributed surplus
    return {"assets": assets, "liabilities": liabilities, "equity": equity,
            "income": income, "expense": expense, "funds": funds,
            "balanced": assets == liabilities + funds}


# ---- Reporting helpers ----------------------------------------------------
def trial_balance(start=None, end=None):
    """Return (rows, totals). Each row: account, debit, credit (net per account)."""
    from django.db.models import Sum, Q
    f = Q()
    if start:
        f &= Q(entry__date__gte=start)
    if end:
        f &= Q(entry__date__lte=end)
    agg = {r["account"]: r for r in JournalLine.objects.filter(f).values("account")
           .annotate(d=Sum("debit"), c=Sum("credit"))}
    rows, td, tc = [], Decimal(0), Decimal(0)
    for acct in Account.objects.order_by("code"):
        a = agg.get(acct.id)
        if not a:
            continue
        net = (a["d"] or 0) - (a["c"] or 0)
        debit = net if net > 0 else Decimal(0)
        credit = -net if net < 0 else Decimal(0)
        if debit == 0 and credit == 0:
            continue
        rows.append({"account": acct, "debit": debit, "credit": credit})
        td += debit; tc += credit
    return rows, {"debit": td, "credit": tc}


def ledger_for(account, start=None, end=None):
    from django.db.models import Q
    f = Q(account=account)
    if start:
        f &= Q(entry__date__gte=start)
    if end:
        f &= Q(entry__date__lte=end)
    lines = (JournalLine.objects.filter(f).select_related("entry")
             .order_by("entry__date", "entry__id"))
    rows, bal = [], Decimal(0)
    sign = 1 if account.is_debit_normal else -1
    for ln in lines:
        bal += sign * (ln.debit - ln.credit)
        rows.append({"date": ln.entry.date, "memo": ln.entry.memo,
                     "debit": ln.debit, "credit": ln.credit, "balance": bal})
    return rows


def fund_variance_detail(dept):
    """Explain why a fund's engine balance differs from its ledger balance by
    finding the specific source records that are missing from, or inconsistent
    with, the general ledger. Returns a list of dicts describing each suspect
    entry. Used by the reconciliation drill-down so a treasurer can see the
    actual transactions/expenses causing a variance rather than just the total.
    """
    from giving.models import Transaction
    from cashbook.models import Expense

    posted_txn_ids = set(JournalEntry.objects.filter(source_type="transaction")
                         .values_list("source_id", flat=True))
    posted_exp_ids = set(JournalEntry.objects.filter(source_type="expense")
                         .values_list("source_id", flat=True))

    issues = []

    # credits/debits the fund engine counts but that have no ledger entry
    txns = Transaction.objects.filter(department=dept).exclude(
        excluded_from_income=True)
    for t in txns:
        if t.is_reversal or t.is_reversed:
            continue
        if t.pk not in posted_txn_ids:
            issues.append({
                "kind": "transaction", "id": t.pk, "date": t.date,
                "desc": (t.payer_name or (t.member.name if t.member_id else "")
                         or t.reference or "transaction"),
                "amount": t.amount if t.direction == "CREDIT" else -t.amount,
                "reason": "Not posted to the ledger",
                "ref": t.mpesa_ref or t.core_ref or t.reference or "",
                "url": f"/transactions/{t.pk}/edit/"})

    # approved/paid expenses the engine counts but with no ledger entry
    exps = Expense.objects.filter(department=dept,
                                  status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
    for x in exps:
        if x.pk not in posted_exp_ids:
            issues.append({
                "kind": "expense", "id": x.pk, "date": x.date,
                "desc": x.description, "amount": -x.amount,
                "reason": "Not posted to the ledger",
                "ref": x.voucher_no or "", "url": "/expenses/"})

    # ledger entries that point at a now-deleted/changed source (orphan postings)
    for e in JournalEntry.objects.filter(source_type="transaction"):
        if e.source_id and not Transaction.objects.filter(pk=e.source_id).exists():
            issues.append({
                "kind": "orphan", "id": e.source_id, "date": e.date,
                "desc": e.memo or "orphaned ledger entry",
                "amount": Decimal(0),
                "reason": "Ledger entry for a transaction that no longer exists",
                "ref": "", "url": ""})

    issues.sort(key=lambda i: (i["date"] or dt.date.min), reverse=True)
    return issues
