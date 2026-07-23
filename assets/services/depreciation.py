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


def accumulated_depreciation(asset, as_of=None, rules=None, cfg=None):
    if not _is_depreciable(asset, cfg):
        return Decimal(0)
    as_of = as_of or dt.date.today()
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


def charge_for_month(asset, year, month, rules=None, cfg=None):
    """Depreciation to charge for a single calendar month = accumulated at the
    month end minus accumulated at the previous month end. This is what the
    monthly run posts, and it ties to accumulated_depreciation by construction."""
    import calendar
    if asset.disposed and asset.disposed_on and asset.disposed_on < dt.date(year, month, 1):
        return Decimal(0)
    end = dt.date(year, month, calendar.monthrange(year, month)[1])
    prev_end = dt.date(year, month, 1) - dt.timedelta(days=1)
    acc_end = accumulated_depreciation(asset, end, rules, cfg)
    acc_prev = accumulated_depreciation(asset, prev_end, rules, cfg)
    return (acc_end - acc_prev).quantize(TWO)
