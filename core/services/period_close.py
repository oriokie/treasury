"""Accounting period close checklist — everything a treasurer should confirm
before locking a month, run on demand and shown before the lock action is
taken. Nothing here blocks locking outright (a treasurer may have a good
reason to close with an open item — e.g. an advance that genuinely spans
months) but every item is surfaced so closing is a deliberate decision, not
an accident."""
import calendar
import datetime as dt
from decimal import Decimal


def period_close_checklist(year, month):
    """Return a list of {key, label, ok, detail} for the given month."""
    start = dt.date(year, month, 1)
    end = dt.date(year, month, calendar.monthrange(year, month)[1])
    items = []

    # 1. Bank reconciliation complete
    from statements.models import BankReconciliation
    rec = (BankReconciliation.objects.filter(
               statement_date__year=year, statement_date__month=month)
           .order_by("-statement_date").first())
    items.append({
        "key": "bank_reconciliation", "label": "Bank reconciliation complete",
        "ok": bool(rec and rec.is_reconciled),
        "detail": (f"Reconciliation dated {rec.statement_date:%d %b %Y} balances "
                   "(difference is zero)." if rec and rec.is_reconciled else
                   f"Reconciliation dated {rec.statement_date:%d %b %Y} does not "
                   "yet balance." if rec else
                   "No bank reconciliation has been recorded for this month."),
    })

    # 2. Petty cash reconciled
    from cashbook.views import _petty_balance_asof
    petty_now = _petty_balance_asof(end)
    petty_ok, petty_detail = True, (
        f"Petty cash float as of {end:%d %b %Y}: KSh {petty_now:,.2f}.")
    if rec:
        petty_item = rec.items.filter(description__icontains="petty cash").first()
        if petty_item and petty_item.amount != petty_now:
            petty_ok = False
            petty_detail = (f"The reconciliation's petty-cash figure "
                            f"(KSh {petty_item.amount:,.2f}) no longer matches the "
                            f"current float (KSh {petty_now:,.2f}) — refresh the "
                            "reconciliation before relying on it.")
    items.append({"key": "petty_cash", "label": "Petty cash reconciled",
                  "ok": petty_ok, "detail": petty_detail})

    # 3. Advances cleared or explained (advisory — spanning months is normal)
    #
    # Judged as at the month-end, not as at today. This was a fourth, private
    # copy of the "which advances were still open" rule, and it carried the same
    # fault the other three did: `.exclude(status=CLOSED)` reads the status the
    # row has NOW, so closing an advance in August retrospectively cleared it
    # from July's checklist — a month could pass its own close review because of
    # something done after the month ended. It now calls the one shared,
    # date-aware rule, and reads the balance as at the month-end too (`balance`
    # is a property over current totals, so a receipt keyed in later would
    # otherwise settle an advance retrospectively as well).
    from cashbook.models import StaffAdvance
    from cashbook.services.treasury_position import advances_open_asof
    open_advances = [
        a for a in advances_open_asof(
            StaffAdvance.objects.filter(date_issued__lte=end), end)
        if (a.amount - a.settled_asof(end)
            - (a.returned_to_petty or Decimal(0))) > 0]
    items.append({
        "key": "advances", "label": "Advances cleared or explained",
        "ok": not open_advances,
        "detail": (f"{len(open_advances)} advance(s) still carry an outstanding "
                   f"balance as of {end:%d %b %Y}. This is normal if they're still "
                   "in use — just confirm each one is expected to remain open."
                   if open_advances else
                   "No outstanding staff advances as of this month-end."),
        "advisory": True,
    })

    # 4. No pending envelope allocations
    from envelopes.models import EnvelopeLine
    pending_lines = EnvelopeLine.objects.filter(
        envelope__date__gte=start, envelope__date__lte=end, transaction__isnull=True)
    n_pending_env = pending_lines.count()
    items.append({
        "key": "envelope_allocations", "label": "No pending envelope allocations",
        "ok": n_pending_env == 0,
        "detail": (f"{n_pending_env} envelope share(s) this month have not yet "
                   "been posted as income." if n_pending_env else
                   "Every envelope share this month has been posted."),
    })

    # 5. No draft or pending journals (unresolved review-queue / unapproved items)
    from giving.models import Transaction
    from cashbook.models import Expense
    pending_txn = Transaction.objects.filter(
        date__gte=start, date__lte=end,
        allocation_status=Transaction.Status.REVIEW).count()
    pending_exp = Expense.objects.filter(
        date__gte=start, date__lte=end, status=Expense.Status.PENDING).count()
    items.append({
        "key": "pending_entries", "label": "No draft or pending journals",
        "ok": pending_txn == 0 and pending_exp == 0,
        "detail": (f"{pending_txn} unallocated receipt(s) and {pending_exp} "
                   "unapproved expense(s) dated this month are still pending."
                   if (pending_txn or pending_exp) else
                   "No unallocated receipts or unapproved expenses this month."),
    })

    # 6. Trial balance balances
    from ledger.services import posting
    _, tb_totals = posting.trial_balance(start, end)
    tb_ok = tb_totals["debit"] == tb_totals["credit"]
    items.append({
        "key": "trial_balance", "label": "Trial balance balances",
        "ok": tb_ok,
        "detail": (f"Debits {tb_totals['debit']:,.2f} = credits "
                   f"{tb_totals['credit']:,.2f} for the month." if tb_ok else
                   f"Debits {tb_totals['debit']:,.2f} vs credits "
                   f"{tb_totals['credit']:,.2f} — out of balance."),
    })

    # 7. Statement of Fund Balances reconciles
    from ledger.services.health import funds_out_of_balance
    out = funds_out_of_balance()
    items.append({
        "key": "fund_balances", "label": "Statement of Fund Balances reconciles",
        "ok": not out,
        "detail": (f"{len(out)} fund(s) don't tie between the fund report and the "
                   "general ledger." if out else
                   "Every fund's balance ties to the general ledger."),
    })

    # 8. Cash book equals bank plus cash on hand
    items.append({
        "key": "cashbook_equals_bank", "label": "Cash book equals bank plus cash on hand",
        "ok": bool(rec and rec.is_reconciled),
        "detail": (f"The bank reconciliation confirms the cash book agrees with "
                   "the bank statement plus cash/petty cash on hand." if rec and rec.is_reconciled else
                   "This is confirmed by a balanced bank reconciliation (see item 1) "
                   "— none is available yet for this month."),
    })

    return items


def checklist_all_clear(items):
    return all(i["ok"] for i in items if not i.get("advisory"))
