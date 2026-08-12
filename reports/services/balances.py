"""Database-side aggregates for reports. No Python loops over ledger rows."""
from decimal import Decimal

from django.db.models import Sum, Q, Count

from cashbook.models import Expense
from departments.models import Department
from giving.models import Transaction


def _txn():
    """The transaction rows this report is entitled to see.

    Ordinarily every row, as it stands now. Inside an ``asat.as_reported``
    block it is the rows as they stood at that moment — so a credit receipted
    after the reporting date is still in suspense, and one entered afterwards
    is not there at all. See ``reports.services.asat``."""
    from reports.services import asat
    return asat.transactions()


def _exp():
    """Expense rows on the same basis as ``_txn`` — a claim approved after the
    reporting date was, on the date, still only a claim."""
    from reports.services import asat
    return asat.expenses()


def _refunds():
    """Expense refunds on the report's basis. A refund is a contra to
    expenditure, so it has to be seen from the same moment the expense is."""
    from cashbook.models import ExpenseRefund
    from reports.services import asat
    return asat.base(ExpenseRefund)


def _transfers():
    """Inter-fund transfers on the report's basis — they move a fund's closing
    balance, so a transfer keyed in later must not appear in an earlier
    position."""
    from cashbook.models import FundTransfer
    from reports.services import asat
    return asat.base(FundTransfer)


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
    qs = _txn().filter(_credit_filter(start, end))
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
    qs = (_exp().filter(f).values("department").annotate(total=Sum("amount")))
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
    qs = (_refunds().filter(f, expense__status__in=[
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
    qs = (_transfers().filter(_transfer_filter(start, end))
          .values("destination").annotate(total=Sum("amount")))
    return {r["destination"]: (r["total"] or Decimal(0)) for r in qs}


def transfers_out_by_department(start=None, end=None):
    from cashbook.models import FundTransfer
    qs = (_transfers().filter(_transfer_filter(start, end))
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

    def _holds_or_moved(dept):
        """Has this fund anything at all to report — a balance on either side
        of the period, or movement inside it? A fund that answers no to every
        one of these would print as a row of zeros."""
        op, rcv, exp, ti, to, exp_op, remit = metrics(dept)
        return any((op, rcv, exp, ti, to, exp_op, remit,
                    op + rcv - exp + ti - to))

    # Which funds get a row. NOT "the open ones". A closed fund does not stop
    # existing, and the close screen promises as much in its own success
    # message: "It stays in historical reports but won't accept new
    # transactions." Filtering on `active` broke that promise at the one seam
    # where a closed fund can still be holding money. The close gate tests the
    # balance only at the moment of closing, and the #63 fix deliberately lets
    # an APPROVED envelope batch post into a fund closed after it was approved
    # — the money was given while the fund was open, and refusing it would
    # lose cash already counted, receipted and sitting in the safe. A fund
    # closed in that window ends the day with a balance and no row anywhere
    # here: one Sabbath posted 23,700 in full, the Collections Summary said
    # 23,700, and this report said 22,950 because the closed fund's 750 had no
    # line at all. A fund missing from a report reads exactly like a fund that
    # received nothing.
    #
    # So a fund earns its row by being open, or by having something to show.
    # That second test is what keeps the register's dead wood off every
    # statement: the ordinary closed fund is at zero with no movement — the
    # close gate saw to that — and stays off the report exactly as before.
    everyone = list(Department.objects.all())
    by_id = {d.id: d for d in everyone}
    included = {d.id for d in everyone if d.active or _holds_or_moved(d)}
    if consolidated:
        # A sub-account is only ever printed inside its parent's row, so a
        # parent left out takes its children's money down with it. Closing a
        # parent closes its sub-accounts too, so the window above lands on a
        # sub just as readily as on a top-level fund — and then the sub holds
        # the money while the parent, judged on its own figures, still has
        # nothing to show. Dropping the parent would hide the sub as
        # completely as filtering it out directly. Ancestors of anything
        # already in are therefore pulled in as the row their children are
        # reported on, however empty they are in themselves. Unconsolidated
        # does not do this: there every fund prints its own row, so an empty
        # parent would be nothing but a line of zeros.
        for did in list(included):
            parent = by_id.get(by_id[did].parent_id)
            while parent is not None and parent.id not in included:
                included.add(parent.id)
                parent = by_id.get(parent.parent_id)
    # rebuilt from `everyone` so the register's own ordering (fund type, then
    # name) survives, rather than the order ancestors happened to be found in
    all_depts = [d for d in everyone if d.id in included]
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
    qs = (_txn().filter(_credit_filter(start, end), excluded_from_income=False)
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
    qs = (_txn().filter(_credit_filter(start, end), member__isnull=False,
                                   excluded_from_income=False)
          .values("member__group")
          .annotate(total=Sum("amount"), count=Count("id")))
    return {r["member__group"] or "UNASSIGNED": (r["total"] or 0) for r in qs}


def income_by_channel(start=None, end=None):
    qs = (_txn().filter(_credit_filter(start, end), excluded_from_income=False)
          .values("channel")
          .annotate(total=Sum("amount"), count=Count("id")))
    return list(qs)


def tithe_total(start=None, end=None):
    return (_txn().filter(
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
    return (_exp().filter(eff)
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
    return (_exp().filter(f).aggregate(t=Sum("amount"))["t"]
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
                     _txn().filter(**f).values("dev_group")
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
                    _exp().filter(remit_f).values("department").annotate(total=Sum("amount"))}
    # cumulative remitted through the period end (for the running balance)
    cum_remit_f = Q(remit_base)
    if end:
        cum_remit_f &= Q(date__lte=end)
    cum_remitted_map = {r["department"]: (r["total"] or Decimal(0)) for r in
                        _exp().filter(cum_remit_f).values("department").annotate(total=Sum("amount"))}
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
    qs = _txn().filter(
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

    receipts = (_txn().filter(
        Q(department_id=dept_id, direction=Transaction.Direction.CREDIT,
          confirmed=True, is_reversed=False, is_reversal=False) & end)
        .aggregate(t=Sum("amount"))["t"] or Decimal(0))

    spent = (_exp().filter(
        Q(department_id=dept_id,
          status__in=[Expense.Status.APPROVED, Expense.Status.PAID]) & end)
        .aggregate(t=Sum("amount"))["t"] or Decimal(0))

    # refunds returned to this fund reduce net expense (restore the balance)
    refunded = (_refunds().filter(
        Q(expense__department_id=dept_id,
          expense__status__in=[Expense.Status.APPROVED, Expense.Status.PAID]) & end)
        .aggregate(t=Sum("amount"))["t"] or Decimal(0))

    tin = (_transfers().filter(Q(destination_id=dept_id) & end)
           .aggregate(t=Sum("amount"))["t"] or Decimal(0))
    tout = (_transfers().filter(Q(source_id=dept_id) & end)
            .aggregate(t=Sum("amount"))["t"] or Decimal(0))

    return {"opening": opening, "receipts": receipts, "spent": spent,
            "refunded": refunded, "transfers_in": tin, "transfers_out": tout,
            "balance": opening + receipts - spent + refunded + tin - tout}


def fund_balance(dept, as_of=None):
    """Closing balance for a SINGLE fund as at a date, computed with targeted
    aggregations (no full-portfolio loop). Mirrors department_summary's basis:
    opening + active receipts − approved/paid expenses + refunds + transfers
    in − out. The sum of ``fund_balance_parts``.

    This is the per-fund figure the general ledger reconciles against
    (``fund_balance_ledger``), so it deliberately does NOT roll sub-accounts
    up. For "what can actually be spent from this fund", see
    ``spendable_balance``.
    """
    parts = fund_balance_parts(dept, as_of)
    return None if parts is None else parts["balance"]


def spendable_balance_parts(dept, as_of=None):
    """What is available to spend FROM this fund: its own balance plus every
    sub-account that collects on its behalf.

    Why this exists separately from ``fund_balance``. Where a parent keeps
    spending at the parent level, giving is allocated to the sub-accounts and
    expenses are charged to the parent — so the parent's own receipts are zero
    and it reads as overdrawn however much the family holds. The Statement of
    Fund Balances never showed that, because ``department_summary`` has always
    consolidated sub-accounts into their parent's row; only the single-fund
    figure did, which is what the expense form and its overdraw guard read.
    This closes that gap without touching ``fund_balance``, which the ledger
    ties to per fund.

    Internal transfers need no special handling: a transfer from child to
    parent is a ``transfers_out`` on one and a ``transfers_in`` on the other,
    so it nets to zero across the family exactly as it should.

    Returns ``fund_balance_parts``' keys (already rolled up) plus:
      ``own_balance`` — the fund's balance excluding sub-accounts
      ``children``    — [{department, balance}] for each fund rolled in
    """
    parts = fund_balance_parts(dept, as_of)
    if parts is None:
        return None
    from departments.models import Department, collection_descendants
    if not isinstance(dept, Department):
        dept = Department.objects.filter(pk=getattr(dept, "id", dept)).first()
        if dept is None:
            return {**parts, "own_balance": parts["balance"], "children": []}
    rolled = {**parts, "own_balance": parts["balance"], "children": []}
    for child in collection_descendants(dept):
        cp = fund_balance_parts(child, as_of)
        for key in ("opening", "receipts", "spent", "refunded",
                    "transfers_in", "transfers_out", "balance"):
            rolled[key] = rolled[key] + cp[key]
        rolled["children"].append({"department": child, "balance": cp["balance"]})
    return rolled


def spendable_balance(dept, as_of=None):
    """The sum of ``spendable_balance_parts`` — the figure the expense form
    shows and its overdraw guard enforces."""
    parts = spendable_balance_parts(dept, as_of)
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
    are excluded here, otherwise they would be double-counted as suspense.

    Money that was at the bank on ``as_of`` and was receipted only afterwards is
    added back by ``receipted_after`` — it is at the bank on the day and in no
    fund on the day, so suspense is the only line that can carry it. Read that
    function before touching this sum: which rows qualify is the whole of the
    difficulty, and getting it wrong reports one credit as two."""
    f = Q(channel=Transaction.Channel.BANK, direction=Transaction.Direction.CREDIT,
          is_reversed=False, is_reversal=False)
    f &= (Q(confirmed=False) | Q(department__isnull=True))
    f &= Q(processed_via_envelope=False, manual_receipt=False, excluded_from_income=False)
    if as_of:
        f &= Q(date__lte=as_of)
    total = _txn().filter(f).aggregate(t=Sum("amount"))["t"] or Decimal(0)
    return total + receipted_after(as_of)


def receipted_after(as_of=None):
    """Bank credits that were awaiting receipt ON ``as_of``, were receipted
    afterwards, and whose money the books still do not carry on that date.

    Money banked on Friday and receipted on Sabbath is the ordinary case here.
    Receipting a bank line BY HAND detaches it from every fund and marks it a
    memo, because the income and the fund move to an envelope — dated the Sabbath.
    So on Friday night that money is at the bank, in no fund, and (once the
    Sabbath comes) excluded from the pending set as well. It would be counted
    nowhere, and a Friday reconciliation would be short by exactly that amount.

    Only the row's OWN flag history is consulted, and only to answer "had this
    been receipted yet". That is deliberately much narrower than rebuilding a
    balance from history: a cash book is completed after the fact — July's
    expenses are keyed in during August and belong in July — so the balances
    themselves must always be read as they now stand. A row whose history does
    not reach back that far is left out rather than guessed at.

    TWO KINDS OF ROW MUST NOT BE ADDED BACK, and both used to be. Each one
    reported a single 5,000 credit as 10,000 — once in a fund, once in
    suspense — which nets out at the bottom of a statement and so hid in the
    one place nobody checks while every line above it was wrong.

    * Not under an as-reported basis. There the queryset behind
      ``pending_receipts_total`` is ALREADY the historical reconstruction, so
      the row is sitting in suspense in it under its own steam. This function
      exists to give the restated (default) basis the one fact it cannot
      reconstruct; on the other basis it is a second helping of the same fact.

    * Not once the row itself sits in a fund. Receipting through an envelope
      (``processed_via_envelope``) leaves the money ON this bank row and merely
      attaches an envelope to it — there is no second posting — so from the
      moment a fund is set, the row's own amount is in that fund's closing
      balance for every date from its own date onwards, ``as_of`` included. It
      is in the book; it cannot also be in suspense. The memo route is the
      exact opposite: ``mark_manual_receipt`` detaches the row from its fund
      and zeroes its cash because the income turns up as a separate envelope
      entry, and THAT is the row this function is for.

    What it still cannot see: nothing links a memo row to the envelope entry
    carrying its income. When that entry is dated on or before ``as_of`` —
    paperwork caught up with weeks later and back-dated to the service Sabbath
    — the book holds it and this function adds it back anyway. Closing that
    gap needs a link between the two rows, not a cleverer guess from here.
    """
    if not as_of:
        return Decimal(0)
    from reports.services import asat
    if asat.is_active():
        # Nothing to add back on this basis. The queryset behind
        # pending_receipts_total is ALREADY the historical reconstruction, so a
        # row that was unreceipted on the day is sitting in its suspense under
        # its own steam. Adding it a second time is one fact served twice — and
        # it was, until this guard: a paper receipting read 10,000 against a
        # true 5,000 on every "as reported" statement.
        return Decimal(0)
    moment = asat.moment_for(as_of)
    rows = Transaction.objects.filter(
        channel=Transaction.Channel.BANK,
        direction=Transaction.Direction.CREDIT,
        is_reversed=False, is_reversal=False, date__lte=as_of,
    ).filter(
        Q(manual_receipt=True) | Q(processed_via_envelope=True)
    ).exclude(
        # already in a fund's closing balance — the same test the fund
        # balances themselves apply (see fund_balance_parts: department set,
        # confirmed, unreversed, dated up to as_of; the date and the reversal
        # flags are in the filter above)
        Q(department__isnull=False) & Q(confirmed=True))
    total = Decimal(0)
    for t in rows:
        was = (t.history.filter(history_date__lte=moment)
               .order_by("-history_date", "-history_id").first())
        if was is None:
            continue          # no record that far back: do not invent one
        if (not was.manual_receipt and not was.processed_via_envelope
                and was.department_id is None and not was.is_reversed):
            total += t.amount
    return total


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
    import datetime as _dt
    from core.models import SiteConfig
    from statements.models import StatementImport
    today = _dt.date.today()
    cfg = SiteConfig.get()
    opening = cfg.opening_bank_balance or Decimal(0)

    stmt = (StatementImport.objects.exclude(status="PURGED")
            .exclude(stmt_closing_balance__isnull=True)
            .order_by("-stmt_last_date", "-uploaded_at").first())
    cutoff = as_of or (stmt.stmt_last_date if stmt else None)

    bank = _txn().filter(channel=Transaction.Channel.BANK,
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
    bank_exp_qs = _exp().filter(
        method=Expense.Method.BANK, status=Expense.Status.PAID,
        bank_transaction__isnull=True)
    if cutoff:
        bank_exp_qs = bank_exp_qs.filter(date__lte=cutoff)
    bank_expenses = bank_exp_qs.aggregate(s=Sum("amount"))["s"] or Decimal(0)
    system_balance = opening + credits - debits - bank_expenses

    # The bank's own figure, as at the date being asked about.
    #
    # This used to be the closing balance of whichever statement was imported
    # most recently, whatever date the report was run for — so a report for 30
    # June carried September's bank balance beside June's movements, and the
    # difference between them was meaningless. The register holds the bank's
    # running balance line by line, so it can answer the question actually
    # asked; the latest import is kept only as the fallback for an account whose
    # statements carry no balance column at all.
    from statements.services import register as register_svc
    # The date to ask the bank about is not the same as the window used to sum
    # our own movements. `cutoff` is None when nothing has been imported, which
    # for movements means "everything" — but asking the bank for its balance as
    # at nothing returns nothing, so a church running the live feed and no
    # statement imports saw no bank balance at all, despite one arriving with
    # every transaction.
    balance_on = as_of or cutoff or today
    reg = register_svc.balance_asof(balance_on)
    live = register_svc.live_balance_asof(balance_on)

    # Both are the bank's own word, arriving by different routes: the register
    # from an imported statement, the live figure pushed with each transaction.
    # Whichever is nearer the date asked about wins — a register balance three
    # weeks old should not beat a same-day one from the bank itself, and the
    # live feed is usually ahead because it does not wait on anybody importing
    # anything. The last import stays as the fallback for an account whose
    # statements carry no balance column and which has no live feed.
    candidates = [(reg["as_at"], reg["balance"], "register", reg),
                  (live["as_at"], live["balance"], "live", live)]
    candidates = [c for c in candidates if c[0] is not None and c[1] is not None]
    if candidates:
        as_at, statement_balance, balance_source, chosen = max(
            candidates, key=lambda c: c[0])
        statement_date = as_at
        balance_stale_days = chosen["stale_days"]
        balance_note = chosen["reason"]
        cleared_balance = live.get("cleared") if balance_source == "live" else None
    else:
        statement_balance = stmt.stmt_closing_balance if stmt else None
        statement_date = stmt.stmt_last_date if stmt else None
        balance_source = "last_import" if stmt else "none"
        balance_stale_days = None
        cleared_balance = None
        balance_note = reg["reason"] or live["reason"]

    return {
        "balance_source": balance_source,
        "balance_stale_days": balance_stale_days,
        "balance_note": balance_note,
        "cleared_balance": cleared_balance,
        "register_balance": reg["balance"],
        "register_as_at": reg["as_at"],
        "live_balance": live["balance"],
        "live_as_at": live["as_at"],
        "opening": opening,
        "opening_configured": bool(cfg.opening_bank_balance),
        "bank_credits": credits,
        "bank_debits": debits,
        "bank_expenses": bank_expenses,
        "system_balance": system_balance,
        "stmt": stmt,
        "statement_balance": statement_balance,
        "statement_date": statement_date,
        "difference": ((statement_balance - system_balance)
                       if statement_balance is not None else None),
    }


# --- cached public wrappers (see core.perfcache; no-op unless a TTL is set) ---
def _k(*parts):
    # The reporting basis is part of the key. Without it a restated figure and
    # an as-reported one for the same period share a cache entry, and whichever
    # was computed first silently answers for both.
    from reports.services import asat
    return ":".join("" if p is None else (p.isoformat() if hasattr(p, "isoformat") else str(p))
                    for p in parts) + asat.cache_key_part()


def department_summary(start=None, end=None, consolidated=True):
    from core.perfcache import cached
    return cached("dept_summary:" + _k(start, end, consolidated),
                  lambda: _department_summary_impl(start, end, consolidated))


def trust_summary(start=None, end=None):
    from core.perfcache import cached
    return cached("trust_summary:" + _k(start, end),
                  lambda: _trust_summary_impl(start, end))
