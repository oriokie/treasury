"""Monthly depreciation engine (EAM Phase 1).

Depreciation is charged on a MONTHLY basis (one-twelfth of the annual charge for
straight-line; monthly-compounded for reducing balance), starting in the month
the asset is placed in service (`in_service_on`, falling back to `acquired_on`).
A whole month is charged for the month of commissioning (a simple, defensible
convention).

The same month-count logic drives both `accumulated_depreciation(as_of)` (the
register/subsidiary figure) and the monthly depreciation *run* (what posts to the
ledger), so the register and the general ledger agree by construction.

Non-depreciable classes (land, heritage) and CWIP do not depreciate; the charge
is capped so book value never falls below salvage.

One exception to "computed on demand": once an asset has been DISPOSED of, its
accumulated depreciation is a recorded fact rather than a calculation — see
`accumulated_at_disposal` below.
"""
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP

TWO = Decimal("0.01")


def _start_date(asset):
    return asset.in_service_on or asset.acquired_on


def _is_depreciable(asset, cfg=None):
    """A class-aware replacement for the old hardcoded LAND/CONSTRUCTION check.
    Falls back to the category rule when no AssetClass is set."""
    ac = getattr(asset, "asset_class", None)
    if ac is not None:
        if not ac.depreciable or ac.is_cwip:
            return False
        return True
    # legacy category fallback
    return asset.category not in (asset.Category.CONSTRUCTION, asset.Category.LAND)


def months_between(start, as_of):
    """Whole months charged from `start` through `as_of`, inclusive of the
    commissioning month. 0 before the asset is in service."""
    if not start or as_of < start:
        return 0
    return (as_of.year - start.year) * 12 + (as_of.month - start.month) + 1


def monthly_charge(asset, rules=None, cfg=None):
    """The straight-line monthly charge (annual / 12). Reducing balance is
    computed period-by-period in `accumulated_depreciation`, so this returns
    None for reducing balance."""
    method, rate = asset._policy(rules, cfg)
    rate = Decimal(rate or 0)
    if not _is_depreciable(asset, cfg) or method == "NONE" or rate <= 0:
        return Decimal(0)
    if method == "STRAIGHT":
        annual = (Decimal(asset.cost) - Decimal(asset.salvage_value or 0)) * rate / Decimal(100)
        return (annual / 12).quantize(TWO, rounding=ROUND_HALF_UP)
    return None  # reducing balance: see accumulated_depreciation


def accumulated_at_disposal(asset):
    """The accumulated depreciation FROZEN at the moment the disposal was
    recorded, or None if this asset carries no such snapshot.

    A disposal is a document, not a calculation. When one is recorded the
    register stores the proceeds and the resulting gain/(loss) — and that stored
    gain/(loss) is what the Income & Expenditure statement reports (the
    `disposal_gain_loss` metric reads the column verbatim). The carrying value
    it was struck against is therefore already pinned down: it is
    `proceeds - gain_loss`, and the accumulated depreciation behind it is
    `cost - (proceeds - gain_loss)`.

    Everything else used to re-derive that figure instead, by running the
    depreciation engine again at the disposal date — which reads whatever
    DepreciationRule is in the database WHEN IT IS ASKED, not when the disposal
    happened. So a treasurer doing an ordinary thing (raising a category's rate
    from /assets/depreciation-rules/) between recording a disposal and the next
    ledger rebuild made the ledger and the statement report different figures
    for the same disposal, and could flip its sign: a 300,000 desk sold for
    150,000 was a 40,000 LOSS on the statement and, after the rate went from 10%
    to 20%, a 70,000 GAIN in the ledger. Nothing caught it — the register↔ledger
    control only compares the FIXED_ASSETS and ACCUM_DEPRECIATION totals, and
    the gain/loss accounts are not among them.

    Reading the snapshot back here rather than at each call site is what keeps
    the ledger's disposal journal (which asks for
    `asset.accumulated_depreciation(asset.disposed_on)`), the disposals report
    and the `disposed_carrying_value` metric all quoting ONE set of numbers.

    Returns None — meaning "no snapshot, compute it" — for a row disposed
    before the register carried a gain/loss, or written directly by a fixture or
    a data fix, so those keep the old behaviour rather than silently reading
    zero.
    """
    if not (asset.disposed and asset.disposed_on and asset.disposal_gain_loss is not None):
        return None
    nbv = Decimal(asset.disposal_proceeds or 0) - Decimal(asset.disposal_gain_loss)
    return (Decimal(asset.cost or 0) - nbv).quantize(TWO, rounding=ROUND_HALF_UP)


def accumulated_depreciation(asset, as_of=None, rules=None, cfg=None):
    """What the asset has accumulated by `as_of` — the recorded figure once it
    has been disposed of, and the engine's calculation until then."""
    as_of = as_of or dt.date.today()
    # A disposed asset stopped depreciating on its disposal date, and what it had
    # accumulated by then is a recorded figure, not one to recompute — so from
    # that date onwards the snapshot is the answer, whatever the rules say now.
    frozen = accumulated_at_disposal(asset)
    if frozen is not None and as_of >= asset.disposed_on:
        return frozen
    return _engine_accumulated(asset, as_of, rules, cfg)


def _engine_accumulated(asset, as_of, rules=None, cfg=None):
    """The depreciation engine's own answer: what the rules in force RIGHT NOW
    say has accumulated by `as_of`, disposal or no disposal.

    Kept separate from `accumulated_depreciation` for the two questions that
    must not read a disposal snapshot: the monthly run's charge (which posts the
    movement the register has been charging month by month, and must not be
    disturbed by a disposal's frozen total), and the recording of a disposal
    itself (which is where the frozen total comes from in the first place).
    """
    if not _is_depreciable(asset, cfg):
        return Decimal(0)
    start = _start_date(asset)
    n = months_between(start, as_of)
    if n <= 0:
        return Decimal(0)
    method, rate = asset._policy(rules, cfg)
    rate = Decimal(rate or 0)
    cost = Decimal(asset.cost)
    salvage = Decimal(asset.salvage_value or 0)
    if method == "NONE" or rate <= 0:
        return Decimal(0)
    if method == "STRAIGHT":
        acc = monthly_charge(asset, rules, cfg) * n
        return min(acc, cost - salvage).quantize(TWO, rounding=ROUND_HALF_UP)
    # reducing balance, monthly compounded
    monthly_rate = rate / Decimal(100) / 12
    book = cost
    acc = Decimal(0)
    for _ in range(n):
        charge = (book * monthly_rate).quantize(TWO, rounding=ROUND_HALF_UP)
        if book - charge < salvage:
            charge = max(book - salvage, Decimal(0))
        acc += charge
        book -= charge
        if book <= salvage:
            break
    return acc.quantize(TWO, rounding=ROUND_HALF_UP)


def net_book_value(asset, as_of=None, rules=None, cfg=None):
    if asset.disposed and asset.disposed_on and (as_of or dt.date.today()) >= asset.disposed_on:
        return Decimal(0)
    return (Decimal(asset.cost) - accumulated_depreciation(asset, as_of, rules, cfg)).quantize(TWO)


def carrying_value_at_disposal(asset, on, rules=None, cfg=None):
    """The carrying amount a disposal is struck against: cost less what the
    engine says had accumulated by the disposal date.

    Do NOT reach for `net_book_value()` to answer this. That function reports
    ZERO from the disposal date onwards — correctly, because a disposed asset
    carries nothing — so asking it after setting `disposed = True` values every
    disposal at nil and turns the whole proceeds into a "gain". Three of this
    project's own test fixtures did exactly that and quietly recorded, for
    instance, a 130,000 gain on a van worth 100,000 sold for 130,000. It went
    unnoticed for as long as the ledger recomputed the figure independently
    instead of believing what the register had recorded.
    """
    return (Decimal(asset.cost or 0)
            - _engine_accumulated(asset, on, rules, cfg)).quantize(TWO)


def gain_or_loss_on_disposal(asset, on, proceeds, rules=None, cfg=None):
    """Proceeds less carrying value: gain positive, loss negative.

    THE definition of a disposal's result. It is a derived figure — there is no
    field on any form for it — so it is computed in one place and every writer
    goes through here (the disposal view for the message it shows, and the
    pre-save guard in assets/signals.py for everything else), which is what
    makes it safe for the ledger and the statements to read the stored figure
    back rather than each work it out again.
    """
    return (Decimal(proceeds or 0)
            - carrying_value_at_disposal(asset, on, rules, cfg)).quantize(TWO)


def charge_for_month(asset, year, month, rules=None, cfg=None):
    """Depreciation to charge for a single calendar month = accumulated at the
    month end minus accumulated at the previous month end. This is what the
    monthly run posts, and it ties to accumulated_depreciation by construction.

    Deliberately on the engine basis at both ends, disposal snapshot or not. The
    run charges the month's movement in depreciation; the frozen total of a
    disposal reaches the ledger through the DISPOSAL journal, which debits it
    back out of accumulated depreciation. Reading the snapshot here as well
    would post the same difference twice — as a depreciation charge in the month
    of disposal (of up to the asset's whole cost) and again in the disposal
    entry."""
    import calendar
    if asset.disposed and asset.disposed_on and asset.disposed_on < dt.date(year, month, 1):
        return Decimal(0)
    end = dt.date(year, month, calendar.monthrange(year, month)[1])
    prev_end = dt.date(year, month, 1) - dt.timedelta(days=1)
    acc_end = _engine_accumulated(asset, end, rules, cfg)
    acc_prev = _engine_accumulated(asset, prev_end, rules, cfg)
    return (acc_end - acc_prev).quantize(TWO)
