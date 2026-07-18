"""Staff-advance helpers shared beyond the advance views themselves.

`advance_detail_ctx` builds the running-balance statement shown on both the
treasurer and leader advance pages; `record_advance_expense` books an
APPROVED+PAID expense (optionally with a linked transaction charge) against an
advance and refreshes its status. Both are imported by leaders/views.py as well
as cashbook/views.py, so they belong in a service module, not a view file.

Extracted verbatim from cashbook/views.py — behaviour is unchanged.
cashbook/views.py re-exports both under their original private names
(`_advance_detail_ctx`, `_record_advance_expense`) so every existing call site
and `from cashbook.views import ...` keeps working exactly as before.
"""
import datetime as dt
from decimal import Decimal

from core.utils import sabbath_week_of
from cashbook.models import Expense


def advance_detail_ctx(adv, *, leader_mode=False, user=None):
    """Shared context for the advance statement (issued + top-ups → expense
    lines → balance), used by both the treasurer and leader detail pages."""
    # build a dated time-line of issues (base + top-ups) and expense lines
    events = [{"date": adv.date_issued, "kind": "issue",
               "label": f"Advance issued — {adv.purpose}", "amount": adv.base_amount}]
    for t in adv.topups.all():
        events.append({"date": t.date, "kind": "topup",
                       "label": "Top-up issued" + (f" — {t.note}" if t.note else "")
                                + (f" (KSh {t.charge:,.2f} sending charge)" if t.charge else ""),
                       "amount": t.amount, "topup_id": t.id})
    for e in adv.expenses.filter(
            status__in=[Expense.Status.APPROVED, Expense.Status.PAID]).order_by("date", "id"):
        events.append({"date": e.date, "kind": "expense", "label": e.description,
                       "amount": e.amount, "expense": e})
    if adv.returned_to_petty:
        events.append({"date": adv.settled_on or adv.date_issued, "kind": "return",
                       "label": "Unspent cash returned to petty cash",
                       "amount": adv.returned_to_petty})
    events.sort(key=lambda x: (x["date"], 0 if x["kind"] in ("issue", "topup") else 1))
    rows, running = [], Decimal(0)
    can_edit_line = bool(leader_mode and user and adv.status != adv.Status.CLOSED)
    for ev in events:
        if ev["kind"] in ("issue", "topup"):
            running += ev["amount"]
            rows.append({"date": ev["date"], "label": ev["label"],
                         "out": ev["amount"], "back": None, "running": running,
                         "topup_id": ev.get("topup_id")})
        else:
            running -= ev["amount"]
            e = ev.get("expense")
            mine = bool(e and user and e.recorded_by_id == getattr(user, "id", None))
            is_charge = bool(e and e.category == Expense.Category.BANK_CHARGE)
            rows.append({"date": ev["date"], "label": ev["label"], "out": None,
                         "back": ev["amount"], "running": running, "expense": e,
                         "editable": can_edit_line and mine and not is_charge,
                         "deletable": can_edit_line and mine,
                         "attachable": bool(leader_mode and e and adv.status != adv.Status.CLOSED),
                         "is_charge": is_charge,
                         "attachments": list(e.attachments.all()) if e else []})
    from core.roles import is_treasurer as _is_tr
    return {
        "adv": adv, "expenses": adv.expenses.all(), "statement": rows,
        "to_account": running,   # >0 still to account; <0 reimburse staff
        "categories": Expense.Category.choices,
        "today": dt.date.today().isoformat(), "leader_mode": leader_mode,
        "is_treasurer": bool(user and _is_tr(user)) and not leader_mode,
    }


def record_advance_expense(adv, *, date, desc, amount, category, user, claimant=None,
                           charge=None):
    """Create an APPROVED+PAID expense that accounts for part of a staff advance,
    and refresh the advance's status. Optionally also book a transaction `charge`
    the holder incurred on that payment (M-Pesa/bank fee) as a linked BANK_CHARGE
    line — that, too, is met out of the advance and reduces the balance.

    Enforces that the total accounted (expense + its charge) cannot exceed the
    advance's remaining balance (#4): you can't account for more than was advanced.
    Returns (expense, error_message); expense is None when blocked."""
    from cashbook.models import StaffAdvance
    charge = charge or Decimal(0)
    needed = amount + charge
    if needed > adv.balance:
        return None, (f"This would account for KSh {needed:,.2f}, but only "
                      f"KSh {adv.balance:,.2f} is left on the advance. Reduce the "
                      f"amount, or ask the treasurer to top up the advance first.")
    exp = Expense.objects.create(
        date=date, sabbath_week=sabbath_week_of(date), department=adv.department,
        description=desc, amount=amount,
        category=category or Expense.Category.OTHER,
        claimant=(claimant or adv.staff_name), method=adv.method,
        status=Expense.Status.PAID, paid_date=date,
        paid_from_petty_cash=False,   # the petty box lost the cash at issuance
        recorded_by=user, advance=adv, approved_by=user)
    if charge and charge > 0:
        Expense.objects.create(
            date=date, sabbath_week=sabbath_week_of(date), department=adv.department,
            description=f"Transaction charge — {desc}", amount=charge,
            category=Expense.Category.BANK_CHARGE,
            claimant=(claimant or adv.staff_name), method=adv.method,
            status=Expense.Status.PAID, paid_date=date, paid_from_petty_cash=False,
            recorded_by=user, advance=adv, charge_for=exp, approved_by=user)
    bal = adv.balance
    if bal == 0:
        adv.status = StaffAdvance.Status.SETTLED
    elif adv.settled_total > 0:
        adv.status = StaffAdvance.Status.PARTLY
    adv.save(update_fields=["status"])
    return exp, None
