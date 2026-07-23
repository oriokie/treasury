"""Why the asset register and the ledger differ, if they ever do.

Register cost is temporal: an asset counts from the day it was acquired until the
day it was disposed of. The ledger matches that — the asset opening entry brings
in only what was owned at the opening date, and everything acquired afterwards
arrives through its own posting (the capital payment that bought it, or a
donation's own journal).

That agreement depends on the data being coherent, and two things can break it,
in opposite directions:

* **Unbacked cost** — an asset acquired after the opening date with no linked
  capital payment and no donation behind it. The ledger is SHORT by the
  difference.
* **A late payment on an opening asset** — a capital payment dated after the
  opening date but linked to an asset the opening balance already brought in.
  The ledger is OVER by that amount, because the payment adds cost that the
  opening had already recognised.

This module names both, per asset, with the remedy in plain words, and totals
them into `predicted_diff` — which is exactly what the `register_vs_ledger` cost
difference reads. Zero means the two agree. Nothing here writes; it is safe to
run at any time, including on production.

It was originally built as a pre-flight before cost was made temporal on the
acquisition date, and it remains the tool for diagnosing any difference.
"""
import datetime as dt
from decimal import Decimal


def _opening_date():
    from ledger.services.posting import _asset_opening_date
    return _asset_opening_date()


def acquisition_coverage(as_of=None):
    """Explain any difference between the register's cost and the ledger's.

    Two things can go wrong, in opposite directions:

    * **Unbacked cost** — an asset acquired after the opening date whose cost is
      not carried by a linked capital payment or a donation. The ledger is SHORT
      by the difference.
    * **Double-counted payment** — a capital payment dated after the opening date
      but linked to an asset the opening balance already brought in. The opening
      recognised its cost already, so the payment adds it a second time and the
      ledger is OVER by that amount.

    `predicted_diff` is register minus ledger, i.e. exactly what the
    `register_vs_ledger` cost difference reads. Zero means the two agree.

    Returns `rows`, `unbacked`, `double_counted`, `totals` and `ready`.
    """
    from assets.models import FixedAsset, Acquisition
    from cashbook.models import Expense

    as_of = as_of or dt.date.today()
    d0 = _opening_date()

    rows, doubles = [], []
    covered_total = shortfall_total = opening_total = double_total = Decimal(0)
    qs = (FixedAsset.objects
          .select_related("department", "acquisition")
          .prefetch_related("source_expenses")
          .order_by("name"))

    for a in qs:
        # an asset already gone before the date under review is irrelevant
        if a.disposed and a.disposed_on and a.disposed_on <= as_of:
            continue
        acquired = a.acquired_on or a.in_service_on
        # not on the register yet at this date, and neither is whatever paid for
        # it — both sides of the reconciliation are as at `as_of`, so it cannot
        # be causing a difference here
        if acquired and acquired > as_of:
            continue
        cost = Decimal(a.cost or 0)
        expenses = [e for e in a.source_expenses.all()
                    if e.expenditure_type == Expense.ExpenditureType.CAPITAL
                    and e.status in (Expense.Status.APPROVED, Expense.Status.PAID)
                    and e.date <= as_of]

        if not acquired or acquired <= d0:
            # brought in by the opening entry. Any capital payment linked to it
            # but dated after the opening date would be added a second time.
            opening_total += cost
            late = sum((Decimal(e.amount or 0) for e in expenses if e.date > d0), Decimal(0))
            if late:
                double_total += late
                doubles.append({
                    "asset": a, "acquired_on": acquired, "cost": cost, "amount": late,
                    "reason": ("Already included in the opening balance, but its capital "
                               "payment is dated after the opening date, so its cost is "
                               "counted twice. Either set the asset's acquisition date to "
                               "when it was actually bought, or unlink the payment if it "
                               "was for something else."),
                })
            continue

        # acquired after the opening date: it must bring its own posting
        covered = sum((Decimal(e.amount or 0) for e in expenses), Decimal(0))
        acq = getattr(a, "acquisition", None)
        if acq and acq.source == Acquisition.Source.DONATION and acq.date <= as_of:
            # a donation posts its own journal at fair value
            covered += Decimal(acq.amount or 0)
        covered = min(covered, cost)
        shortfall = cost - covered

        covered_total += covered
        shortfall_total += shortfall
        rows.append({
            "asset": a,
            "acquired_on": acquired,
            "cost": cost,
            "covered": covered,
            "shortfall": shortfall,
            "expenses": len(expenses),
            "source": acq.get_source_display() if acq else "Not recorded",
            "reason": _reason(a, acq, expenses, shortfall),
        })

    rows.sort(key=lambda r: (-r["shortfall"], r["asset"].name))
    doubles.sort(key=lambda r: -r["amount"])
    unbacked = [r for r in rows if r["shortfall"]]
    return {
        "as_of": as_of,
        "opening_date": d0,
        "rows": rows,
        "unbacked": unbacked,
        "double_counted": doubles,
        "ready": not unbacked and not doubles,
        "totals": {
            "opening": opening_total,
            "post_opening": covered_total + shortfall_total,
            "covered": covered_total,
            "shortfall": shortfall_total,
            "double_counted": double_total,
            # register minus ledger, as register_vs_ledger would report it
            "predicted_diff": shortfall_total - double_total,
            "count": len(rows),
            "unbacked_count": len(unbacked),
            "double_count": len(doubles),
        },
    }


def _reason(asset, acq, expenses, shortfall):
    """Plain words for what would need doing before the switch."""
    if not shortfall:
        return "Backed by the ledger."
    if not acq and not expenses:
        return ("No acquisition recorded and no payment linked — record how it was "
                "acquired, or link the payment that bought it.")
    if acq and acq.source in ("PURCHASE", "CONSTRUCTION") and not expenses:
        return ("Recorded as purchased/built but no payment is linked — link the "
                "capital payment(s) that paid for it.")
    if expenses:
        return ("Cost is higher than the payments linked to it — link the remaining "
                "payment(s), or correct the cost.")
    return "Not backed by a payment or a donation."
