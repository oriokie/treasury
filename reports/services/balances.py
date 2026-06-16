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


def receipts_by_department(start=None, end=None):
    qs = (Transaction.objects.filter(_credit_filter(start, end))
          .values("department")
          .annotate(total=Sum("amount"), count=Count("id")))
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
        f &= ~Q(category=Expense.Category.REMITTANCE)
    qs = (Expense.objects.filter(f).values("department").annotate(total=Sum("amount")))
    return {r["department"]: (r["total"] or Decimal(0)) for r in qs}


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


def department_summary(start=None, end=None, consolidated=True):
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


def trust_summary(start=None, end=None):
    rows = []
    receipts = receipts_by_department(start, end)
    # Cumulative receipts through the period end — the outstanding remittance is a
    # running liability, not a single month's figure, so a trust collected in one
    # month and remitted the next still reconciles.
    cum_receipts = receipts_by_department(None, end)
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
    for dept in Department.objects.filter(
            fund_type=Department.FundType.TRUST, active=True):
        collected = receipts.get(dept.id, Decimal(0))
        remitted = remitted_map.get(dept.id, Decimal(0))
        # outstanding = opening liability + everything collected to date − everything
        # remitted to date (this is what is genuinely still owed to the conference)
        outstanding = ((dept.opening_balance or Decimal(0))
                       + cum_receipts.get(dept.id, Decimal(0))
                       - cum_remitted_map.get(dept.id, Decimal(0)))
        rows.append({"department": dept, "collected": collected,
                     "remitted": remitted, "to_remit": outstanding})
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


def fund_balance(dept, as_of=None):
    """Closing balance for a SINGLE fund as at a date, computed with targeted
    aggregations (no full-portfolio loop). Mirrors department_summary's basis:
    opening + active receipts − approved/paid expenses + transfers in − out."""
    from decimal import Decimal
    from django.db.models import Sum
    from cashbook.models import Expense, FundTransfer
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

    tin = (FundTransfer.objects.filter(Q(destination_id=dept_id) & end)
           .aggregate(t=Sum("amount"))["t"] or Decimal(0))
    tout = (FundTransfer.objects.filter(Q(source_id=dept_id) & end)
            .aggregate(t=Sum("amount"))["t"] or Decimal(0))

    return opening + receipts - spent + tin - tout


def pending_receipts_total(as_of=None):
    """Bank donations that have been received into the bank but are not yet
    receipted/allocated to a fund — either still unconfirmed (held for review) or
    confirmed but unallocated (no department). They are real money at the bank, so
    they belong on the Statement of Financial Position as cash held in suspense,
    even though they have not yet touched a specific (e.g. trust) fund."""
    f = Q(channel=Transaction.Channel.BANK, direction=Transaction.Direction.CREDIT,
          is_reversed=False, is_reversal=False)
    f &= (Q(confirmed=False) | Q(department__isnull=True))
    if as_of:
        f &= Q(date__lte=as_of)
    return Transaction.objects.filter(f).aggregate(t=Sum("amount"))["t"] or Decimal(0)
