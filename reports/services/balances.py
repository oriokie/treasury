"""Database-side aggregates for reports. No Python loops over ledger rows."""
from decimal import Decimal

from django.db.models import Sum, Q, Count

from cashbook.models import Expense
from departments.models import Department
from giving.models import Transaction


def _credit_filter(start=None, end=None):
    f = Q(direction=Transaction.Direction.CREDIT, confirmed=True,
          is_reversed=False, is_reversal=False)
    if start:
        f &= Q(date__gte=start)
    if end:
        f &= Q(date__lte=end)
    return f


def _receipted_q():
    """A trust/giving credit counts as RECEIPTED once a formal receipt exists:
    it came through the envelope/receipt flow, or it was flagged as receipted
    manually on paper. Everything else is confirmed-but-not-yet-receipted."""
    return (Q(channel=Transaction.Channel.ENVELOPE) | Q(manual_receipt=True)
            | Q(processed_via_envelope=True))


def receipts_by_department(start=None, end=None, receipted=None):
    qs = Transaction.objects.filter(_credit_filter(start, end))
    if receipted is True:
        qs = qs.filter(_receipted_q())
    elif receipted is False:
        qs = qs.exclude(_receipted_q())
    qs = qs.values("department").annotate(total=Sum("amount"), count=Count("id"))
    return {r["department"]: (r["total"] or Decimal(0)) for r in qs}


def expenses_by_department(start=None, end=None, only_effective=True,
                           include_remittance=True):
    """Approved/paid expenses per department.

    By default includes everything (so a trust fund's ledger shows its
    remittances as outflows and its closing balance is right). Pass
    include_remittance=False for the Income & Expenditure / dashboard expense
    total, where trust remittances are a liability settlement, not expenditure,
    and must be excluded to avoid overstating expenses."""
    f = Q()
    if start:
        f &= Q(date__gte=start)
    if end:
        f &= Q(date__lte=end)
    if only_effective:
        f &= Q(status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
    if not include_remittance:
        # liability settlements, not expenditure: anything classed LIABILITY
        # (trust remittances, loan repayments, and any future liability
        # category) is kept out of the operating-expense view
        f &= ~Q(doc_class=Expense.DocClass.LIABILITY)
    qs = (Expense.objects.filter(f).values("department").annotate(total=Sum("amount")))
    out = {r["department"]: (r["total"] or Decimal(0)) for r in qs}
    # refunds are contra-entries: money returned to the fund reduces the net
    # expense (and restores the fund balance). Date the reduction when received.
    for dept_id, total in refunds_by_department(start, end).items():
        out[dept_id] = out.get(dept_id, Decimal(0)) - total
    return out


def refunds_by_department(start=None, end=None):
    """Expense refunds (cash returned to a fund) per department, by refund date."""
    from cashbook.models import ExpenseRefund
    f = Q()
    if start:
        f &= Q(date__gte=start)
    if end:
        f &= Q(date__lte=end)
    qs = (ExpenseRefund.objects.filter(f, expense__status__in=[
              Expense.Status.APPROVED, Expense.Status.PAID])
          .values("expense__department").annotate(total=Sum("amount")))
    return {r["expense__department"]: (r["total"] or Decimal(0)) for r in qs}


def _transfer_filter(start=None, end=None):
    f = Q()
    if start:
        f &= Q(date__gte=start)
    if end:
        f &= Q(date__lte=end)
    return f


def transfers_in_by_department(start=None, end=None):
    from cashbook.models import FundTransfer
    qs = (FundTransfer.objects.filter(_transfer_filter(start, end))
          .values("destination").annotate(total=Sum("amount")))
    return {r["destination"]: (r["total"] or Decimal(0)) for r in qs}


def transfers_out_by_department(start=None, end=None):
    from cashbook.models import FundTransfer
    qs = (FundTransfer.objects.filter(_transfer_filter(start, end))
          .values("source").annotate(total=Sum("amount")))
    return {r["source"]: (r["total"] or Decimal(0)) for r in qs}


def brought_forward_map(dept_ids, start):
    """Opening (brought-forward) balance for a set of departments as of `start`:
    each fund's founding opening_balance plus all net movement (receipts −
    expenses + transfers in − out) strictly before `start`. With start=None this
    is just the founding opening_balance. Used anywhere a fund's balance is shown
    for a specific period (e.g. the Fund Ledger) so a fund with real prior-period
    activity doesn't wrongly show a zero opening just because its founding
    opening_balance field is zero."""
    from decimal import Decimal
    import datetime as _dt
    dept_ids = list(dept_ids)
    depts = {d.id: d for d in Department.objects.filter(id__in=dept_ids)}
    if start:
        before = start - _dt.timedelta(days=1)
        p_rcv = receipts_by_department(None, before)
        p_exp = expenses_by_department(None, before)
        p_tin = transfers_in_by_department(None, before)
        p_tout = transfers_out_by_department(None, before)
    else:
        p_rcv = p_exp = p_tin = p_tout = {}
    out = {}
    for did in dept_ids:
        d = depts.get(did)
        founding = (d.opening_balance or Decimal(0)) if d else Decimal(0)
        out[did] = (founding + p_rcv.get(did, Decimal(0)) - p_exp.get(did, Decimal(0))
                    + p_tin.get(did, Decimal(0)) - p_tout.get(did, Decimal(0)))
    return out


def brought_forward(dept, start):
    """Opening (brought-forward) balance for a single department as of `start`."""
    return brought_forward_map([dept.id], start).get(
        dept.id, dept.opening_balance or __import__("decimal").Decimal(0))


def _department_summary_impl(start=None, end=None, consolidated=True):
    """Per-fund: opening, receipts, expenses, closing. The master report.

    When consolidated (default), sub-accounts roll up into their parent fund, so
    e.g. the Local Church Budget shows as a single line; each row keeps a
    ``children`` list for optional drill-down.
    """
    from collections import defaultdict
    import datetime as _dt
    receipts = receipts_by_department(start, end)
    expenses = expenses_by_department(start, end)
    expenses_op = expenses_by_department(start, end, include_remittance=False)
    tin = transfers_in_by_department(start, end)
    tout = transfers_out_by_department(start, end)

    # Brought-forward into the period: the founding opening balance plus all net
    # movement strictly before `start`. With start=None this is just the founding
    # opening. This makes year-on-year carry-forward automatic: a year's opening
    # equals the prior year's closing.
    if start:
        before = start - _dt.timedelta(days=1)
        p_rcv = receipts_by_department(None, before)
        p_exp = expenses_by_department(None, before)
        p_tin = transfers_in_by_department(None, before)
        p_tout = transfers_out_by_department(None, before)
    else:
        p_rcv = p_exp = p_tin = p_tout = {}

    def _bf(dept):
        return ((dept.opening_balance or Decimal(0))
                + p_rcv.get(dept.id, Decimal(0)) - p_exp.get(dept.id, Decimal(0))
                + p_tin.get(dept.id, Decimal(0)) - p_tout.get(dept.id, Decimal(0)))

    def metrics(dept):
        full = expenses.get(dept.id, Decimal(0))
        op_only = expenses_op.get(dept.id, Decimal(0))
        return (_bf(dept),
                receipts.get(dept.id, Decimal(0)),
                full,
                tin.get(dept.id, Decimal(0)),
                tout.get(dept.id, Decimal(0)),
                op_only,                       # operating expenses (excl remittance)
                full - op_only)                # remittances

    all_depts = list(Department.objects.filter(active=True))
    if not consolidated:
        rows = []
        for dept in all_depts:
            op, rcv, exp, ti, to, exp_op, remit = metrics(dept)
            rows.append({"department": dept, "opening": op, "receipts": rcv,
                         "expenses": exp, "expenses_operating": exp_op,
                         "remittances": remit,
                         "transfers_in": ti, "transfers_out": to,
                         "net_transfer": ti - to,
                         "closing": op + rcv - exp + ti - to,
                         "is_trust": dept.fund_type == Department.FundType.TRUST, "children": []})
        return rows

    kids = defaultdict(list)
    tops = []
    for d in all_depts:
        (kids[d.parent_id].append(d) if d.parent_id else tops.append(d))
    rows = []
    for dept in tops:
        op, rcv, exp, ti, to, exp_op, remit = metrics(dept)
        children = []
        for c in sorted(kids.get(dept.id, []), key=lambda x: x.name):
            cop, crcv, cexp, cti, cto, cexp_op, cremit = metrics(c)
            op += cop; rcv += crcv; exp += cexp; ti += cti; to += cto
            exp_op += cexp_op; remit += cremit
            children.append({"department": c, "opening": cop, "receipts": crcv,
                             "expenses": cexp, "expenses_operating": cexp_op,
                             "remittances": cremit,
                             "transfers_in": cti, "transfers_out": cto,
                             "closing": cop + crcv - cexp + cti - cto})
        rows.append({"department": dept, "opening": op, "receipts": rcv,
                     "expenses": exp, "expenses_operating": exp_op,
                     "remittances": remit,
                     "transfers_in": ti, "transfers_out": to,
                     "net_transfer": ti - to,
                     "closing": op + rcv - exp + ti - to,
                     "is_trust": dept.fund_type == Department.FundType.TRUST, "children": children})
    return rows


def totals(rows):
    return {
        "opening": sum(r["opening"] for r in rows),
        "receipts": sum(r["receipts"] for r in rows),
        "expenses": sum(r["expenses"] for r in rows),
        # operating expenses exclude trust remittances (a liability settlement):
        # this is the figure the dashboard / I&E "Expenses" should report
        "expenses_operating": sum(r.get("expenses_operating", r["expenses"]) for r in rows),
        "remittances": sum(r.get("remittances", Decimal(0)) for r in rows),
        "closing": sum(r["closing"] for r in rows),
    }


def offering_summary(start=None, end=None):
    """Receipts by department x Sabbath date. Columns are the actual Sabbaths in
    the selected range (not month-ordinals 1..5), so a multi-month range shows
    every Sabbath separately instead of merging all 'week 1' Sabbaths together."""
    from core.utils import sabbath_of
    qs = (Transaction.objects.filter(_credit_filter(start, end), excluded_from_income=False)
          .values("department__name", "service_sabbath", "date").annotate(total=Sum("amount")))
    sabbaths = set()
    rows = {}
    for r in qs:
        name = r["department__name"] or "Unallocated"
        sab = r["service_sabbath"] or sabbath_of(r["date"])
        sabbaths.add(sab)
        row = rows.setdefault(name, {"cells": {}, "total": Decimal(0)})
        amt = r["total"] or Decimal(0)
        row["cells"][sab] = row["cells"].get(sab, Decimal(0)) + amt
        row["total"] += amt
    return {"sabbaths": sorted(sabbaths), "rows": rows}


def giving_by_group(start=None, end=None):
    qs = (Transaction.objects.filter(_credit_filter(start, end), member__isnull=False,
                                   excluded_from_income=False)
          .values("member__group")
          .annotate(total=Sum("amount"), count=Count("id")))
    return {r["member__group"] or "UNASSIGNED": (r["total"] or 0) for r in qs}


def income_by_channel(start=None, end=None):
    qs = (Transaction.objects.filter(_credit_filter(start, end), excluded_from_income=False)
          .values("channel")
          .annotate(total=Sum("amount"), count=Count("id")))
    return list(qs)


def tithe_total(start=None, end=None):
    return (Transaction.objects.filter(
        _credit_filter(start, end), excluded_from_income=False,
        department__name__icontains="tithe",
    ).aggregate(total=Sum("amount"))["total"] or Decimal(0))


def _effective_expense_qs(start=None, end=None):
    """Approved/paid, non-liability expenses in the period — the base every
    income-statement expenditure figure is drawn from. Single definition so the
    Income Statement, Board Report and the operating/capital metrics agree."""
    from cashbook.models import Expense
    from django.db.models import Q
    eff = Q(status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
    if start:
        eff &= Q(date__gte=start)
    if end:
        eff &= Q(date__lte=end)
    return (Expense.objects.filter(eff)
            .exclude(doc_class=Expense.DocClass.LIABILITY))


def operating_expense_total(start=None, end=None):
    """Total recurrent (operating) expenditure over the period: approved/paid,
    non-liability expenses of expenditure_type RECURRENT. Matches the Income
    Statement's 'total recurrent'."""
    from cashbook.models import Expense
    return (_effective_expense_qs(start, end)
            .filter(expenditure_type=Expense.ExpenditureType.RECURRENT)
            .aggregate(t=Sum("amount"))["t"] or Decimal(0))


def capital_expenditure_total(start=None, end=None):
    """Total capital expenditure over the period: approved/paid, non-liability
    expenses of expenditure_type CAPITAL. Matches the Income Statement's 'total
    capital'."""
    from cashbook.models import Expense
    return (_effective_expense_qs(start, end)
            .filter(expenditure_type=Expense.ExpenditureType.CAPITAL)
            .aggregate(t=Sum("amount"))["t"] or Decimal(0))


def remittances_total(start=None, end=None):
    """Total remittances to the field (conference) over the period: effective
    (approved/paid) expenses of category REMITTANCE. Matches the Cash Flow
    Statement's remittances figure and the trust-summary remittance basis."""
    from cashbook.models import Expense
    from django.db.models import Q
    f = Q(category=Expense.Category.REMITTANCE,
          status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
    if start:
        f &= Q(date__gte=start)
    if end:
        f &= Q(date__lte=end)
    return (Expense.objects.filter(f).aggregate(t=Sum("amount"))["t"]
            or Decimal(0))


def expense_by_category(start=None, end=None, expenditure_type=None):
    """Effective expenditure grouped by category (display name → amount),
    optionally restricted to one expenditure_type. Used by the Income Statement
    and the expense-analysis narrative."""
    from cashbook.models import Expense
    qs = _effective_expense_qs(start, end)
    if expenditure_type:
        qs = qs.filter(expenditure_type=expenditure_type)
    cats = dict(Expense.Category.choices)
    rows = [{"name": cats.get(r["category"], r["category"]), "amount": r["t"] or Decimal(0)}
            for r in qs.values("category").annotate(t=Sum("amount")).order_by("-t")]
    return rows


def dev_group_progress(start=None, end=None):
    from departments.models import DevelopmentGroup
    rows = []
    f = dict(direction=Transaction.Direction.CREDIT, confirmed=True,
             is_reversed=False, is_reversal=False, excluded_from_income=False,
             dev_group__isnull=False)
    if start:
        f["date__gte"] = start
    if end:
        f["date__lte"] = end
    # one grouped query for every group, instead of an aggregate per group
    collected_map = {r["dev_group"]: (r["t"] or Decimal(0)) for r in
                     Transaction.objects.filter(**f).values("dev_group")
                     .annotate(t=Sum("amount"))}
    for grp in DevelopmentGroup.objects.filter(active=True):
        collected = collected_map.get(grp.id, Decimal(0))
        target = grp.target or Decimal(0)
        pct = round(float(collected) / float(target) * 100, 1) if target else 0
        rows.append({"group": grp, "collected": collected,
                     "target": target, "pct": pct,
                     "balance": target - collected})
    return rows


def _trust_summary_impl(start=None, end=None):
    rows = []
    receipts = receipts_by_department(start, end)
    # A remittance reduces the trust liability once it is APPROVED or PAID — the
    # same basis the fund reports and general ledger use, so all three agree.
    remit_base = Q(category=Expense.Category.REMITTANCE,
                   status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
    remit_f = Q(remit_base)
    if start:
        remit_f &= Q(date__gte=start)
    if end:
        remit_f &= Q(date__lte=end)
    remitted_map = {r["department"]: (r["total"] or Decimal(0)) for r in
                    Expense.objects.filter(remit_f).values("department").annotate(total=Sum("amount"))}
    # cumulative remitted through the period end (for the running balance)
    cum_remit_f = Q(remit_base)
    if end:
        cum_remit_f &= Q(date__lte=end)
    cum_remitted_map = {r["department"]: (r["total"] or Decimal(0)) for r in
                        Expense.objects.filter(cum_remit_f).values("department").annotate(total=Sum("amount"))}
    # cumulative receipts split by whether a formal receipt has been issued.
    # Only RECEIPTED trust money is a firm liability to remit; unreceipted trust
    # money is still owed but shown on its own "pending receipting" line.
    cum_receipted = receipts_by_department(None, end, receipted=True)
    cum_unreceipted = receipts_by_department(None, end, receipted=False)
    period_unreceipted = receipts_by_department(start, end, receipted=False)
    for dept in Department.objects.filter(
            fund_type=Department.FundType.TRUST, active=True):
        collected = receipts.get(dept.id, Decimal(0))
        remitted = remitted_map.get(dept.id, Decimal(0))
        # outstanding-to-remit = opening liability + RECEIPTED collected to date
        # − everything remitted to date. This is what is genuinely due to the field.
        to_remit = ((dept.opening_balance or Decimal(0))
                    + cum_receipted.get(dept.id, Decimal(0))
                    - cum_remitted_map.get(dept.id, Decimal(0)))
        unreceipted = cum_unreceipted.get(dept.id, Decimal(0))
        rows.append({"department": dept, "collected": collected,
                     "remitted": remitted, "to_remit": to_remit,
                     "unreceipted": unreceipted,
                     "unreceipted_period": period_unreceipted.get(dept.id, Decimal(0)),
                     "total_liability": to_remit + unreceipted})
    return rows


def dev_group_members(group, start=None, end=None):
    """Per-member contributions to a development group in a period, for the leader's
    reconciliation. Returns {'rows': [{member_name, phone, count, total}], 'total'}."""
    from collections import defaultdict
    qs = Transaction.objects.filter(
        dev_group=group, direction=Transaction.Direction.CREDIT, confirmed=True,
        is_reversed=False, is_reversal=False)
    if start:
        qs = qs.filter(date__gte=start)
    if end:
        qs = qs.filter(date__lte=end)
    agg = defaultdict(lambda: {"name": "", "phone": "", "count": 0, "total": Decimal(0)})
    for t in qs.select_related("member"):
        if t.member_id:
            key = ("m", t.member_id)
            name = t.member.name
            phone = t.member.phone or ""
        else:
            key = ("n", (t.payer_name or "Unattributed").strip().upper())
            name = (t.payer_name or "Unattributed").strip() or "Unattributed"
            phone = t.payer_phone or ""
        row = agg[key]
        row["name"] = name
        row["phone"] = row["phone"] or phone
        row["count"] += 1
        row["total"] += t.amount
    rows = sorted(agg.values(), key=lambda r: -r["total"])
    return {"rows": rows, "total": sum((r["total"] for r in rows), Decimal(0))}


def fund_balance_parts(dept, as_of=None):
    """The single-fund closing balance broken into its constituent parts, as a
    dict: opening, receipts, spent, refunded, transfers_in, transfers_out and
    balance. This is THE single implementation — ``fund_balance`` sums it, and
    the expense form's available-balance endpoint displays it — so the guard,
    the form and the reports can never disagree again (the form previously
    duplicated this inline with a stale filter that still counted reversed
    credits).

    Mirrors department_summary's basis exactly: receipts are confirmed,
    non-reversed, non-reversal credits (INCLUDING loan/financing cash — this
    is a cash figure); spent is every approved/paid expense; refunds restore
    the balance; transfers move it.
    """
    from decimal import Decimal
    from django.db.models import Sum
    from cashbook.models import Expense, FundTransfer, ExpenseRefund
    if dept is None:
        return None
    dept_id = getattr(dept, "id", dept)
    end = Q(date__lte=as_of) if as_of else Q()

    opening = getattr(dept, "opening_balance", None)
    if opening is None:
        from departments.models import Department
        opening = (Department.objects.filter(pk=dept_id)
                   .values_list("opening_balance", flat=True).first() or Decimal(0))

    receipts = (Transaction.objects.filter(
        Q(department_id=dept_id, direction=Transaction.Direction.CREDIT,
          confirmed=True, is_reversed=False, is_reversal=False) & end)
        .aggregate(t=Sum("amount"))["t"] or Decimal(0))

    spent = (Expense.objects.filter(
        Q(department_id=dept_id,
          status__in=[Expense.Status.APPROVED, Expense.Status.PAID]) & end)
        .aggregate(t=Sum("amount"))["t"] or Decimal(0))

    # refunds returned to this fund reduce net expense (restore the balance)
    refunded = (ExpenseRefund.objects.filter(
        Q(expense__department_id=dept_id,
          expense__status__in=[Expense.Status.APPROVED, Expense.Status.PAID]) & end)
        .aggregate(t=Sum("amount"))["t"] or Decimal(0))

    tin = (FundTransfer.objects.filter(Q(destination_id=dept_id) & end)
           .aggregate(t=Sum("amount"))["t"] or Decimal(0))
    tout = (FundTransfer.objects.filter(Q(source_id=dept_id) & end)
            .aggregate(t=Sum("amount"))["t"] or Decimal(0))

    return {"opening": opening, "receipts": receipts, "spent": spent,
            "refunded": refunded, "transfers_in": tin, "transfers_out": tout,
            "balance": opening + receipts - spent + refunded + tin - tout}


def fund_balance(dept, as_of=None):
    """Closing balance for a SINGLE fund as at a date, computed with targeted
    aggregations (no full-portfolio loop). Mirrors department_summary's basis:
    opening + active receipts − approved/paid expenses + refunds + transfers
    in − out. The sum of ``fund_balance_parts``."""
    parts = fund_balance_parts(dept, as_of)
    return None if parts is None else parts["balance"]


def pending_receipts_total(as_of=None):
    """Bank donations that have been received into the bank but are not yet
    receipted/allocated to a fund — either still unconfirmed (held for review) or
    confirmed but unallocated (no department). They are real money at the bank, so
    they belong on the Statement of Financial Position as cash held in suspense,
    even though they have not yet touched a specific (e.g. trust) fund.

    A bank credit that has already been receipted through an envelope is NOT
    pending: its income and fund live on the envelope's own record, so the bank
    line is just a memo. Such credits (processed_via_envelope / manual_receipt)
    are excluded here, otherwise they would be double-counted as suspense."""
    f = Q(channel=Transaction.Channel.BANK, direction=Transaction.Direction.CREDIT,
          is_reversed=False, is_reversal=False)
    f &= (Q(confirmed=False) | Q(department__isnull=True))
    f &= Q(processed_via_envelope=False, manual_receipt=False, excluded_from_income=False)
    if as_of:
        f &= Q(date__lte=as_of)
    return Transaction.objects.filter(f).aggregate(t=Sum("amount"))["t"] or Decimal(0)


def bank_position(as_of=None):
    """The system's bank position vs the bank's own figure — extracted VERBATIM
    from the Bank Position report view so the calculation exists exactly once
    (it was previously inline in reports.views.BankPositionView; the view now
    consumes this function, and the ``bank_position`` registry metric points
    here).

    System bank balance = SiteConfig.opening_bank_balance + every confirmed
    BANK credit − every confirmed BANK debit − bank-paid expenses not already
    represented by a bank DEBIT row. The bank's figure is the closing running
    balance of the most recent imported statement.

    ``as_of`` bounds the movement window; when None, the most recent
    statement's last date is used (the view's original behaviour), or all
    movements if no statement exists.

    NOTE (recommendation #9): the figure depends on
    ``SiteConfig.opening_bank_balance`` being configured; while it is at its
    default of zero the system balance understates by the true bank-only
    opening balance. The dict includes ``opening_configured`` so callers can
    surface that.
    """
    from core.models import SiteConfig
    from statements.models import StatementImport
    cfg = SiteConfig.get()
    opening = cfg.opening_bank_balance or Decimal(0)

    stmt = (StatementImport.objects.exclude(status="PURGED")
            .exclude(stmt_closing_balance__isnull=True)
            .order_by("-stmt_last_date", "-uploaded_at").first())
    cutoff = as_of or (stmt.stmt_last_date if stmt else None)

    bank = Transaction.objects.filter(channel=Transaction.Channel.BANK,
                                      confirmed=True, is_reversal=False,
                                      is_reversed=False)
    if cutoff:
        bank = bank.filter(date__lte=cutoff)
    credits = bank.filter(direction=Transaction.Direction.CREDIT).aggregate(
        s=Sum("amount"))["s"] or Decimal(0)
    debits = bank.filter(direction=Transaction.Direction.DEBIT).aggregate(
        s=Sum("amount"))["s"] or Decimal(0)
    # Bank-paid expenses that AREN'T already represented by a bank DEBIT row
    # (i.e. entered directly with method=Bank, not resolved from the debit
    # queue). These are real outflows from the bank account and must reduce the
    # system bank balance, otherwise it overstates the cash at bank. Expenses
    # linked to a bank_transaction are excluded — they're already in `debits`.
    from cashbook.models import Expense
    bank_exp_qs = Expense.objects.filter(
        method=Expense.Method.BANK, status=Expense.Status.PAID,
        bank_transaction__isnull=True)
    if cutoff:
        bank_exp_qs = bank_exp_qs.filter(date__lte=cutoff)
    bank_expenses = bank_exp_qs.aggregate(s=Sum("amount"))["s"] or Decimal(0)
    system_balance = opening + credits - debits - bank_expenses

    statement_balance = stmt.stmt_closing_balance if stmt else None
    return {
        "opening": opening,
        "opening_configured": bool(cfg.opening_bank_balance),
        "bank_credits": credits,
        "bank_debits": debits,
        "bank_expenses": bank_expenses,
        "system_balance": system_balance,
        "stmt": stmt,
        "statement_balance": statement_balance,
        "statement_date": stmt.stmt_last_date if stmt else None,
        "difference": ((statement_balance - system_balance)
                       if statement_balance is not None else None),
    }


# --- cached public wrappers (see core.perfcache; no-op unless a TTL is set) ---
def _k(*parts):
    return ":".join("" if p is None else (p.isoformat() if hasattr(p, "isoformat") else str(p))
                    for p in parts)


def department_summary(start=None, end=None, consolidated=True):
    from core.perfcache import cached
    return cached("dept_summary:" + _k(start, end, consolidated),
                  lambda: _department_summary_impl(start, end, consolidated))


def trust_summary(start=None, end=None):
    from core.perfcache import cached
    return cached("trust_summary:" + _k(start, end),
                  lambda: _trust_summary_impl(start, end))
