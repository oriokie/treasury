"""Point-in-time reporting: the position as it stood on a date.

There are two honest answers to "what was the position on 30 July", and the
difference is not a rounding error:

* **Restated** (the default everywhere) — 30 July as we now understand it. A
  bank credit dated 25 July that was receipted to the Building fund on 1 August
  belongs to Building, so on 30 July Building held it. This is the right basis
  for a comparative figure in a set of accounts.
* **As reported** (this module) — 30 July as it stood on 30 July. Nobody had
  allocated that credit yet, so it sat in suspense: pending receipts carry it,
  the fund does not, and the bank reconciliation shows why money at the bank
  exceeded money in funds. This is the basis a treasurer needs to reproduce a
  balancing done on the day, or to explain a figure the board was given.

Nothing stores *when* a receipt was allocated — there is no ``receipted_at``
column — but ``simple_history`` records every change to a Transaction and an
Expense with a timestamp, so the state at any past moment is reconstructable.
``Model.history.as_of(moment)`` returns exactly that: the rows as they were,
including the ones that did not exist yet.

The basis is carried in a context variable rather than threaded through every
aggregate's signature. That is deliberate. The failure that matters here is a
*mixed* statement — suspense on one basis and fund balances on the other, which
double-counts the same shilling — and a parameter that ten functions have to
remember to pass is a parameter one of them will eventually forget. Entering
the basis once, at the top of a render, means every figure below it moves
together or none does.

Usage::

    with asat.as_reported(date(2026, 7, 30)):
        rows = ctx.fund_summary()          # as it stood on the day
"""
from __future__ import annotations

import contextlib
import contextvars
import datetime as dt

from django.utils import timezone

#: The moment being reported as at, or None for the restated (default) basis.
_AS_REPORTED: "contextvars.ContextVar" = contextvars.ContextVar(
    "treasury_as_reported_at", default=None)


def moment_for(value):
    """The last instant of ``value`` as an aware datetime.

    A position "as at 30 July" includes everything entered up to the end of
    30 July, so the cut is midnight at the close of that day, not its start.
    A datetime is taken as given (already an instant).
    """
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        naive = value
    else:
        naive = dt.datetime.combine(value, dt.time.max)
    if timezone.is_naive(naive):
        return timezone.make_aware(naive, timezone.get_current_timezone())
    return naive


@contextlib.contextmanager
def as_reported(value):
    """Report as the position stood at ``value`` (a date or datetime).

    ``as_reported(None)`` is a no-op, so a caller can pass a flag straight
    through without branching.
    """
    moment = moment_for(value)
    token = _AS_REPORTED.set(moment)
    try:
        yield moment
    finally:
        _AS_REPORTED.reset(token)


@contextlib.contextmanager
def restated():
    """Force the restated basis inside an as-reported block — for the rare
    figure that must not move (a control total being compared against)."""
    token = _AS_REPORTED.set(None)
    try:
        yield
    finally:
        _AS_REPORTED.reset(token)


def active():
    """The moment being reported as at, or None on the default basis."""
    return _AS_REPORTED.get()


def is_active():
    return _AS_REPORTED.get() is not None


def cache_key_part():
    """Distinguishes cached aggregates by basis. Without this a restated figure
    and an as-reported one would share a cache entry and whichever ran first
    would answer for both."""
    moment = _AS_REPORTED.get()
    return "" if moment is None else f":asat={moment.isoformat()}"


# ---------------------------------------------------------------------------
# Base querysets — the single place the basis is applied
# ---------------------------------------------------------------------------

def base(model):
    """The rows of ``model`` as they stood at the reporting moment.

    On the default basis this is simply ``model.objects.all()``, so nothing
    changes and nothing costs anything. Under an as-reported basis it is the
    historical reconstruction, which supports ``.filter()``, ``.values()`` and
    ``.aggregate()`` exactly like an ordinary queryset — so every aggregate
    built on top keeps working unchanged.
    """
    moment = _AS_REPORTED.get()
    if moment is None:
        return model.objects.all()
    history = getattr(model, "history", None)
    if history is None:      # not tracked: it can only answer for today
        return model.objects.all()
    return history.as_of(moment)


def transactions():
    from giving.models import Transaction
    return base(Transaction)


def expenses():
    from cashbook.models import Expense
    return base(Expense)


def credits():
    """Confirmed, non-reversed credits — ``TransactionQuerySet.confirmed_credits``
    expressed as a filter, so it can be applied to the historical base too (a
    historical queryset has the model's fields but not its custom manager)."""
    return transactions().filter(direction="CREDIT", confirmed=True,
                                 is_reversed=False, is_reversal=False)
