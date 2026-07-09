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

from ledger.models import Account, JournalEntry, JournalLine, JournalEntryArchive


# ---- Chart of accounts ----------------------------------------------------
CHART = [
    # --- Assets (1xxx) ---
    ("1000", "Cash & bank", "ASSET", "CASH"),
    ("1010", "Petty cash on hand", "ASSET", "PETTY_CASH"),
    ("1020", "Mobile money (M-Pesa)", "ASSET", "MOBILE_MONEY"),
    ("1200", "Staff advances (receivable)", "ASSET", "STAFF_ADVANCES"),
    ("1300", "Prepayments", "ASSET", "PREPAYMENTS"),
    ("1400", "Other receivables", "ASSET", "RECEIVABLES"),
    ("1500", "Fixed assets", "ASSET", "FIXED_ASSETS"),
    ("1600", "Accumulated depreciation", "ASSET", "ACCUM_DEPRECIATION"),
    # --- Liabilities (2xxx) ---
    ("2000", "Trust funds payable (to field)", "LIABILITY", "TRUST_PAYABLE"),
    ("2100", "Accruals", "LIABILITY", "ACCRUALS"),
    ("2200", "Accounts payable", "LIABILITY", "PAYABLES"),
    ("2300", "Loans payable (to lenders)", "LIABILITY", "LOANS_PAYABLE"),
    ("2400", "Statutory deductions payable (PAYE/NSSF/NHIF)", "LIABILITY", "STATUTORY_PAYABLE"),
    # --- Equity / fund balances (3xxx) ---
    ("3000", "Accumulated fund balances", "EQUITY", "ACCUM_FUNDS"),
    ("3100", "Capital (asset) fund", "EQUITY", "CAPITAL_FUND"),
    ("3200", "Designated / restricted funds", "EQUITY", "DESIGNATED_FUNDS"),
    ("3900", "Opening balance equity", "EQUITY", "OPENING_EQUITY"),
    # --- Income (4xxx) ---
    ("4100", "Tithe", "INCOME", "INC_TITHE"),
    ("4200", "Offerings & general income", "INCOME", "INC_OFFERINGS"),
    ("4300", "Development & projects", "INCOME", "INC_DEVELOPMENT"),
    ("4400", "Interest & investment income", "INCOME", "INC_INTEREST"),
    ("4500", "Fundraising & camp meeting", "INCOME", "INC_FUNDRAISING"),
    ("4600", "Donations & gifts in kind", "INCOME", "INC_DONATIONS"),
    ("4900", "Other income", "INCOME", "INC_OTHER"),
]
# expense accounts are generated per Expense.Category as 5xxx
EXPENSE_BASE = 5100


def ensure_chart():
    """Create the chart of accounts if missing. Idempotent.

    Each Expense.Category gets its own permanent account code the first time
    it's seen. That code is assigned as "one past the highest account code
    already on record", never from the category's position in
    Expense.Category.choices — a real production incident showed why:
    inserting SALARIES/LEASE in the middle of the choices list shifted every
    later category's positional index, so UTILITIES (which already had code
    5105 on disk from before the insertion) collided with the newly-computed
    code for SALARIES (also 5105, from ITS new position) — a duplicate-key
    IntegrityError on Account.code that made the ledger un-rebuildable.
    Assigning by "next free code" is immune to list reordering: an existing
    category's code, once assigned, never changes and is never reused."""
    from cashbook.models import Expense
    for code, name, typ, key in CHART:
        Account.objects.get_or_create(
            system_key=key, defaults={"code": code, "name": name, "type": typ})
    existing_codes = set(Account.objects.values_list("code", flat=True))
    next_code = EXPENSE_BASE
    for val, label in Expense.Category.choices:
        key = f"EXP_{val}"
        if Account.objects.filter(system_key=key).exists():
            continue
        while str(next_code) in existing_codes:
            next_code += 1
        Account.objects.get_or_create(
            system_key=key, defaults={"code": str(next_code), "name": label, "type": "EXPENSE"})
        existing_codes.add(str(next_code))
        next_code += 1


def _acct(key):
    return Account.objects.filter(system_key=key).first()


def chart_ready():
    return Account.objects.filter(system_key="CASH").exists()


def _income_key_for(dept):
    """Map a local fund to an income account. An explicit override
    (Department.income_account) always wins; otherwise fall back to guessing
    from the fund's name."""
    if getattr(dept, "income_account", ""):
        return dept.income_account
    n = (dept.name or "").lower()
    if "tithe" in n:
        return "INC_TITHE"
    if "develop" in n or "project" in n:
        return "INC_DEVELOPMENT"
    if "offering" in n or "budget" in n or "church" in n or "youth" in n or "sabbath" in n:
        return "INC_OFFERINGS"
    return "INC_OFFERINGS"


class UnbalancedEntryError(Exception):
    """Raised if a journal entry's debits and credits don't sum to the same
    total, or a line carries both a debit and a credit. This should never
    trigger from any current posting path — every one of them builds balanced
    entries by construction — but nothing enforced that at the schema level,
    so this is a defence-in-depth safeguard against a future posting path (or
    a mistake in one) silently corrupting the general ledger."""


def _replace_entries(source_type, source_id=None, *, filter_kwargs=None):
    """Delete the existing journal entry/entries for a source document so a
    fresh one can be posted — first archiving their current detail (if
    SiteConfig.archive_replaced_ledger_entries is on) so a correction never
    silently erases what the ledger said before it, even though the live
    ledger always reflects only the current, correct posting."""
    qs = (JournalEntry.objects.filter(**filter_kwargs) if filter_kwargs is not None
          else JournalEntry.objects.filter(source_type=source_type, source_id=source_id))
    try:
        from core.models import SiteConfig
        archive_on = SiteConfig.get().archive_replaced_ledger_entries
    except Exception:  # noqa: BLE001 — archiving must never block a repost
        archive_on = False
    if archive_on:
        for je in qs.prefetch_related("lines__account", "lines__department"):
            try:
                JournalEntryArchive.objects.create(
                    date=je.date, memo=je.memo, source_type=je.source_type,
                    source_id=je.source_id, original_entry_id=je.id,
                    original_number=je.number,
                    original_created_at=je.created_at,
                    lines=[{"account_code": ln.account.code, "account_name": ln.account.name,
                            "department_id": ln.department_id,
                            "department_name": ln.department.name if ln.department_id else None,
                            "debit": str(ln.debit), "credit": str(ln.credit)}
                           for ln in je.lines.all()])
            except Exception:  # noqa: BLE001 — one bad snapshot must not block the repost
                from core.utils import log_exception as _lx; _lx("ledger archive")
    qs.delete()


def _entry(date, memo, source_type, source_id, lines):
    """lines: list of (account, debit, credit[, department]). Balanced JournalEntry.
    Validates debits == credits before anything is written — see
    UnbalancedEntryError."""
    total_debit = sum((ln[1] for ln in lines), Decimal(0))
    total_credit = sum((ln[2] for ln in lines), Decimal(0))
    if total_debit != total_credit:
        raise UnbalancedEntryError(
            f"Refusing to post an unbalanced entry ({source_type} #{source_id}): "
            f"debits {total_debit} != credits {total_credit}. memo={memo!r}")
    for ln in lines:
        if ln[1] and ln[2]:
            raise UnbalancedEntryError(
                f"Refusing to post a line with both a debit and a credit "
                f"({source_type} #{source_id}): {ln!r}")
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
def _is_trust(dept):
    """Authoritative trust test. Always read the fund_type field, never the
    cacheable is_trust flag, so the ledger classifies a fund exactly as the
    balance engine does and the two can never disagree (which would break the
    reconciliation)."""
    from departments.models import Department
    return dept is not None and dept.fund_type == Department.FundType.TRUST



def _is_loan_receipt(txn):
    """True if this credit is indexed as a loan receipt. Loan receipts are
    always excluded_from_income, so the reverse-relation lookup is only paid
    on that rare subset — ordinary receipts never trigger a query."""
    if not getattr(txn, "excluded_from_income", False):
        return False
    try:
        return txn.loan_receipt is not None
    except Exception:  # noqa: BLE001 — no related LoanTransaction
        return False


def post_transaction(txn):
    """Income receipt. Skips bank-debit-direction rows (represented by expenses)."""
    from giving.models import Transaction
    _replace_entries("transaction", txn.pk)
    if (txn.direction != Transaction.Direction.CREDIT or not txn.confirmed
            or txn.is_reversed or txn.is_reversal):
        return
    cash = _acct("CASH")
    dept = txn.department
    if _is_loan_receipt(txn):
        # loan money in: a liability, never income (mirrors the trust shape)
        credit = _acct("LOANS_PAYABLE") or _acct("PAYABLES")
    elif _is_trust(dept):
        credit = _acct("TRUST_PAYABLE")
    else:
        credit = _acct(_income_key_for(dept)) if dept else _acct("INC_OTHER")
    who = txn.payer_name or txn.reference or "Receipt"
    _post_pair(txn.date, f"Receipt: {who}", "transaction", txn.pk, cash, credit, txn.amount, dept)


@db_tx.atomic
def post_expense(exp):
    from cashbook.models import Expense
    _replace_entries("expense", exp.pk)
    if exp.status not in (Expense.Status.APPROVED, Expense.Status.PAID):
        return
    cash = _acct("CASH")
    if exp.category == Expense.Category.REMITTANCE:
        debit = _acct("TRUST_PAYABLE")
    elif exp.category == Expense.Category.LOAN_REPAYMENT:
        # settling loan principal reduces the liability, not an expense
        debit = _acct("LOANS_PAYABLE") or _acct("PAYABLES")
    elif exp.expenditure_type == Expense.ExpenditureType.CAPITAL:
        debit = _acct("FIXED_ASSETS")
    else:
        debit = _acct(f"EXP_{exp.category}") or _acct("EXP_OTHER")
    _post_pair(exp.date, f"{exp.get_category_display()}: {exp.description}",
               "expense", exp.pk, debit, cash, exp.amount, exp.department)


@db_tx.atomic
def post_refund(ref):
    """An expense refund: cash returned to a fund, reversing part of the original
    expense. Mirrors post_expense with the sides flipped (DR Cash, CR Expense)."""
    from cashbook.models import Expense
    _replace_entries("refund", ref.pk)
    exp = ref.expense
    if exp.status not in (Expense.Status.APPROVED, Expense.Status.PAID):
        return
    cash = _acct("CASH")
    if exp.category == Expense.Category.REMITTANCE:
        exp_acct = _acct("TRUST_PAYABLE")
    elif exp.category == Expense.Category.LOAN_REPAYMENT:
        exp_acct = _acct("LOANS_PAYABLE") or _acct("PAYABLES")
    elif exp.expenditure_type == Expense.ExpenditureType.CAPITAL:
        exp_acct = _acct("FIXED_ASSETS")
    else:
        exp_acct = _acct(f"EXP_{exp.category}") or _acct("EXP_OTHER")
    # negative amount flips the pair: DR Cash, CR Expense
    _post_pair(ref.date, f"Refund: {exp.description}", "refund", ref.pk,
               exp_acct, cash, -ref.amount, exp.department)


@db_tx.atomic
def post_transfer(tr):
    """Inter-fund transfer: an equity reclassification between two funds. Net zero
    for the whole church, but moves the fund-balance claim from source to
    destination (tagged by fund so the ledger reconciles to the fund reports)."""
    from cashbook.models import FundTransfer  # noqa
    _replace_entries("transfer", tr.pk)
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
    _replace_entries(None, filter_kwargs={"source_type": "opening"})
    cash = _acct("CASH"); accum = _acct("ACCUM_FUNDS"); trust = _acct("TRUST_PAYABLE")
    lines = []
    for d in Department.objects.all():
        ob = d.opening_balance or Decimal(0)
        if not ob:
            continue
        lines.append((cash, ob, Decimal(0), d))
        lines.append((trust if _is_trust(d) else accum, Decimal(0), ob, d))
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
    from cashbook.models import Expense, FundTransfer, ExpenseRefund
    ensure_chart()
    JournalEntry.objects.exclude(source_type="manual").delete()  # keep manual adjustments
    post_opening()
    for t in Transaction.objects.filter(direction=Transaction.Direction.CREDIT, confirmed=True):
        post_transaction(t)
    for e in Expense.objects.filter(status__in=[Expense.Status.APPROVED, Expense.Status.PAID]):
        post_expense(e)
    for r in ExpenseRefund.objects.select_related("expense"):
        post_refund(r)
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
    if _is_trust(dept):
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


def fund_balances_from_ledger_bulk(dept_ids):
    """Same computation as fund_balance_from_ledger(), for many departments at
    once. Used by the Health Check / reconciliation report, which need every
    fund's ledger balance together — calling the single-department version in
    a loop turned that into 2 queries per fund (roughly 100+ for a real
    church's fund list) plus one implicit transaction each; this does the
    same computation with a small, constant number of grouped-aggregate
    queries. Returns {department_id: balance}. Never used for posting, only
    for read-only comparison/reporting, so there is no accounting-integrity
    risk in the different code path."""
    from django.db.models import Sum
    from departments.models import Department
    dept_ids = list(dept_ids)
    if not dept_ids:
        return {}
    trust_ids = set(Department.objects.filter(
        id__in=dept_ids, fund_type=Department.FundType.TRUST).values_list("id", flat=True))
    local_ids = [d for d in dept_ids if d not in trust_ids]

    out = {did: Decimal(0) for did in dept_ids}
    if trust_ids:
        rows = (JournalLine.objects.filter(department_id__in=trust_ids,
                    account__system_key="TRUST_PAYABLE")
                .values("department_id").annotate(d=Sum("debit"), c=Sum("credit")))
        for r in rows:
            out[r["department_id"]] = (r["c"] or Decimal(0)) - (r["d"] or Decimal(0))
    if local_ids:
        cash_rows = (JournalLine.objects.filter(department_id__in=local_ids,
                        account__system_key="CASH")
                     .values("department_id").annotate(d=Sum("debit"), c=Sum("credit")))
        cash_map = {r["department_id"]: (r["d"] or Decimal(0)) - (r["c"] or Decimal(0))
                    for r in cash_rows}
        tr_rows = (JournalLine.objects.filter(department_id__in=local_ids,
                        account__system_key="ACCUM_FUNDS", entry__source_type="transfer")
                   .values("department_id").annotate(d=Sum("debit"), c=Sum("credit")))
        tr_map = {r["department_id"]: (r["c"] or Decimal(0)) - (r["d"] or Decimal(0))
                  for r in tr_rows}
        for did in local_ids:
            out[did] = cash_map.get(did, Decimal(0)) + tr_map.get(did, Decimal(0))
    return out


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
            "net": assets - liabilities,
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
                     "number": ln.entry.number,
                     "debit": ln.debit, "credit": ln.credit, "balance": bal})
    return rows


def fund_variance_detail(dept):
    """Explain why a fund's engine balance differs from its ledger balance.

    The engine balance is computed from current source records (transactions,
    expenses) as they are *now*; the ledger balance is what was actually posted.
    A difference usually means a record was changed after it was posted — most
    often re-allocated to a different fund, edited, reversed, or excluded — and
    the ledger wasn't rebuilt. This walks both sides and flags the specific
    records whose engine contribution to THIS fund doesn't match what the ledger
    holds for it. Returns a list of issue dicts; the signed amounts sum to the
    fund's variance (engine − ledger) so the drill-down can show what's explained.
    """
    from giving.models import Transaction
    from cashbook.models import Expense

    issues = []

    # ---- transactions: compare engine contribution vs ledger posting ----
    # What the ledger currently holds per transaction, for THIS fund.
    ledger_txn = {}
    if _is_trust(dept):
        rows = (JournalLine.objects.filter(department=dept,
                entry__source_type="transaction",
                account__system_key="TRUST_PAYABLE")
                .values("entry__source_id", "debit", "credit"))
        for r in rows:
            sid = r["entry__source_id"]
            ledger_txn[sid] = ledger_txn.get(sid, Decimal(0)) \
                + (r["credit"] or Decimal(0)) - (r["debit"] or Decimal(0))
    else:
        rows = (JournalLine.objects.filter(department=dept,
                entry__source_type="transaction", account__system_key="CASH")
                .values("entry__source_id", "debit", "credit"))
        for r in rows:
            sid = r["entry__source_id"]
            ledger_txn[sid] = ledger_txn.get(sid, Decimal(0)) \
                + (r["debit"] or Decimal(0)) - (r["credit"] or Decimal(0))

    # every transaction currently allocated to this fund (engine's view)
    seen = set()
    for t in Transaction.objects.filter(department=dept,
                                        direction=Transaction.Direction.CREDIT):
        seen.add(t.pk)
        excluded = t.excluded_from_income and not _is_loan_receipt(t)
        engine_amt = (Decimal(0) if (t.is_reversal or t.is_reversed
                                     or excluded or not t.confirmed)
                      else t.amount)
        ledger_amt = ledger_txn.get(t.pk, Decimal(0))
        if engine_amt != ledger_amt:
            issues.append({
                "kind": "transaction", "id": t.pk, "date": t.date,
                "desc": (t.payer_name or (t.member.name if t.member_id else "")
                         or t.reference or "transaction"),
                "amount": engine_amt - ledger_amt,
                "reason": _txn_reason(t, engine_amt, ledger_amt),
                "ref": t.mpesa_ref or t.core_ref or t.reference or "",
                "url": f"/transactions/{t.pk}/edit/"})

    # transactions the LEDGER still has under this fund but the engine no longer
    # does (re-allocated away, deleted, or now excluded)
    for src_id, ledger_amt in ledger_txn.items():
        if src_id in seen or ledger_amt == 0:
            continue
        t = Transaction.objects.filter(pk=src_id).first()
        if t and t.department_id == dept.id:
            continue
        moved_to = (t.department.name if t and t.department_id else "—") if t else "(deleted)"
        issues.append({
            "kind": "transaction", "id": src_id,
            "date": t.date if t else None,
            "desc": (t.payer_name if t else "removed transaction") or "transaction",
            "amount": Decimal(0) - ledger_amt,
            "reason": f"Ledger still credits this fund but the contribution is now under "
                      f"{moved_to} — rebuild the ledger to re-post it",
            "ref": (t.mpesa_ref or t.core_ref if t else "") or "",
            "url": f"/transactions/{src_id}/edit/" if t else ""})

    # ---- expenses: engine vs ledger ----
    ledger_exp = {}
    erows = (JournalLine.objects.filter(department=dept,
             entry__source_type="expense", account__system_key="CASH")
             .values("entry__source_id", "debit", "credit"))
    for r in erows:
        sid = r["entry__source_id"]
        ledger_exp[sid] = ledger_exp.get(sid, Decimal(0)) \
            + (r["credit"] or Decimal(0)) - (r["debit"] or Decimal(0))
    seen_e = set()
    for x in Expense.objects.filter(department=dept):
        seen_e.add(x.pk)
        engine_amt = (x.amount if x.status in (Expense.Status.APPROVED,
                                               Expense.Status.PAID) else Decimal(0))
        ledger_amt = ledger_exp.get(x.pk, Decimal(0))
        if engine_amt != ledger_amt:
            issues.append({
                "kind": "expense", "id": x.pk, "date": x.date,
                "desc": x.description, "amount": ledger_amt - engine_amt,
                "reason": ("Not posted to the ledger" if ledger_amt == 0
                           else "Ledger amount differs from the current expense"),
                "ref": x.voucher_no or "", "url": "/expenses/"})

    issues.sort(key=lambda i: (i["date"] or dt.date.min), reverse=True)
    return issues


def _txn_reason(t, engine_amt, ledger_amt):
    if ledger_amt == 0 and engine_amt != 0:
        return "Counted by the fund but not posted to the ledger (rebuild needed)"
    if engine_amt == 0 and ledger_amt != 0:
        if t.excluded_from_income:
            return "Now excluded from income but still in the ledger"
        if t.is_reversed:
            return "Reversed but still in the ledger"
        if not t.confirmed:
            return "Not yet confirmed but already in the ledger"
        return "In the ledger but no longer counted by the fund"
    return "Amount in the ledger differs from the current transaction"
