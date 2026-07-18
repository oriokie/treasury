"""Fund solvency, commitments and cash forecasting.

The accounting integration already recognises expenditure correctly: a benefit
voucher, once approved, is an Expense in the scheme's fund, in the ledger, on the
income statement and reducing the fund balance — exactly like any other payment.
This module does not re-do any of that. It asks the questions that sit ON TOP of
a correct ledger, the ones a committee needs answered BEFORE it commits money:

    can this fund afford this payout?          (fund depletion / negative balance)
    what has it already promised?               (reserved commitments)
    what is approved but not yet paid?          (pending approved payouts)
    what does it owe in total?                  (outstanding liabilities, memo)
    what will it look like in three months?     (cash forecasting / sustainability)

Every cash figure it uses comes from the Financial Metrics Registry via
`benevolent.services.reporting` — `scheme_balance`, `approved_unpaid_total`,
`contributions_total`. Nothing here recomputes a balance or an expense total; it
projects the registry's figures forward and compares them, which is the one thing
the point-in-time fund tables cannot do for themselves.

Two distinctions the code keeps straight, because conflating them is how a fund
report starts lying:

* A COMMITMENT is not a LEDGER LIABILITY. Expenditure is recognised at voucher
  approval; a case approved but not yet vouchered is a promise, a memorandum
  figure, deliberately absent from the balance sheet (see
  reporting.approved_unpaid_total's own note). This module treats it as a claim
  on FUTURE cash, never as a past expense.

* AVAILABLE is not the same as BALANCE. The balance is what is in the fund now;
  available is what is left once the promises already made are honoured. A fund
  can have a healthy balance and be unable to afford one more case.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal

from benevolent.models import (BenevolentCase, BenevolentScheme, SchemeMembership,
                               SchemePolicy)


def _money(v) -> Decimal:
    return Decimal(v or 0)


# ---------------------------------------------------------------------------
# The position — where a scheme's fund stands, right now
# ---------------------------------------------------------------------------

@dataclass
class FundPosition:
    """A scheme fund's cash position, layered from what is certain to what is
    projected. Each layer is the previous one less one more class of claim, so a
    reader can stop at the level of caution they want."""
    scheme: object
    as_of: _dt.date
    balance: Decimal = Decimal(0)              # what is in the fund now (registry)
    approved_unpaid: Decimal = Decimal(0)      # vouchered decisions not yet paid
    committed_unapproved: Decimal = Decimal(0) # vouchers raised, awaiting approval
    reserved_open_cases: Decimal = Decimal(0)  # open cases likely to be approved

    @property
    def available_after_approved(self):
        """Balance less what has been approved but not yet paid — the money the
        fund must still find for decisions already made."""
        return self.balance - self.approved_unpaid

    @property
    def available_after_committed(self):
        """Also less vouchers raised and awaiting approval — the tightest view of
        cash that is genuinely uncommitted today."""
        return self.available_after_approved - self.committed_unapproved

    @property
    def available_after_reserved(self):
        """Also less a prudent reserve for open cases still working through the
        pipeline — the forward-looking 'what could I safely commit to a NEW case'
        figure."""
        return self.available_after_committed - self.reserved_open_cases

    @property
    def is_depleted(self):
        """The fund cannot cover what it has already approved but not paid."""
        return self.available_after_approved < 0

    @property
    def is_negative(self):
        return self.balance < 0

    @property
    def is_overcommitted(self):
        """Approvals plus live vouchers exceed the balance — solvent on paper,
        but every promise cannot be kept without more coming in."""
        return self.available_after_committed < 0

    def as_dict(self):
        return {
            "balance": str(_money(self.balance)),
            "approved_unpaid": str(_money(self.approved_unpaid)),
            "committed_unapproved": str(_money(self.committed_unapproved)),
            "reserved_open_cases": str(_money(self.reserved_open_cases)),
            "available_after_approved": str(_money(self.available_after_approved)),
            "available_after_committed": str(_money(self.available_after_committed)),
            "available_after_reserved": str(_money(self.available_after_reserved)),
            "is_depleted": self.is_depleted,
            "is_negative": self.is_negative,
            "is_overcommitted": self.is_overcommitted,
        }


def _reserved_for_open_cases(scheme, as_of):
    """A prudent reserve against cases still in the pipeline (draft, submitted,
    assessed) that have not yet been approved. Each is valued at its assessed
    amount where it has one, else its claimed amount, else the policy's fixed
    benefit — the best estimate of what it will cost if approved. This is the
    'reserved commitments' figure: money it would be imprudent to treat as free,
    because it is already spoken for by cases the scheme is committed to
    considering."""
    pipeline = [BenevolentCase.Status.DRAFT, BenevolentCase.Status.SUBMITTED,
                BenevolentCase.Status.ASSESSED]
    qs = BenevolentCase.objects.filter(scheme=scheme, status__in=pipeline)
    total = Decimal(0)
    for case in qs.select_related("scheme"):
        est = case.assessed_amount or case.claimed_amount
        if not est:
            policy = scheme.policy_on(case.event_date) or scheme.current_policy
            est = policy.benefit_amount if policy else Decimal(0)
        total += _money(est)
    return total


def fund_position(scheme, *, as_of=None) -> FundPosition:
    """Where the scheme's fund stands: balance, then each layer of claim taken
    off it. Cash figures come from the registry via reporting; commitments come
    from the case pipeline."""
    from benevolent.services import reporting
    as_of = as_of or _dt.date.today()

    pos = FundPosition(scheme=scheme, as_of=as_of)
    pos.balance = _money(reporting.scheme_balance(scheme))
    pos.approved_unpaid = _money(reporting.approved_unpaid_total(scheme))

    # vouchers raised and still awaiting approval (committed, not yet in ledger)
    committed = Decimal(0)
    for case in BenevolentCase.objects.filter(
            scheme=scheme,
            status__in=[BenevolentCase.Status.APPROVED,
                        BenevolentCase.Status.PARTLY_PAID]
    ).prefetch_related("payouts__expense"):
        committed += _money(case.committed_total)
    pos.committed_unapproved = committed

    from benevolent.models import BenevolentSettings
    if getattr(BenevolentSettings.get(), "reserve_open_cases", True):
        pos.reserved_open_cases = _reserved_for_open_cases(scheme, as_of)
    return pos


# ---------------------------------------------------------------------------
# Affordability — the guard the approval/payout flow consults
# ---------------------------------------------------------------------------

@dataclass
class AffordabilityCheck:
    ok: bool
    level: str                 # 'ok' | 'warn' | 'block'
    detail: str
    available: Decimal = Decimal(0)
    shortfall: Decimal = Decimal(0)


def can_fund_payout(scheme, amount, *, as_of=None, cfg=None):
    """Can the fund afford one more payout of `amount`, right now?

    Deliberately advisory by default — a church may legitimately approve a
    payout it intends to fund from a levy still being collected, or from money it
    knows is coming. So this WARNS when a payout would push the fund past its
    available cash, and only BLOCKS when a scheme's settings say it must (a hard
    overdraft guard a treasurer can switch on for a fund that must never go
    negative). Silently refusing would be worse than a warned-through payout the
    committee genuinely means.
    """
    from benevolent.models import BenevolentSettings
    amount = _money(amount)
    cfg = cfg or BenevolentSettings.get()
    pos = fund_position(scheme, as_of=as_of)
    available = pos.available_after_approved

    if amount <= available:
        return AffordabilityCheck(
            True, "ok",
            f"{scheme.fund.name} has {available:,.2f} available after existing "
            f"approvals; this {amount:,.2f} payout fits.",
            available=available)

    shortfall = amount - available
    block = bool(getattr(cfg, "block_overdrawn_payouts", False))
    detail = (f"This {amount:,.2f} payout exceeds the {available:,.2f} available in "
              f"{scheme.fund.name} after existing approvals — a shortfall of "
              f"{shortfall:,.2f}.")
    if block:
        detail += (" This scheme's settings block a payout that would overdraw the "
                   "fund; collect a levy or move money in first.")
        return AffordabilityCheck(False, "block", detail,
                                  available=available, shortfall=shortfall)
    detail += (" It can still be approved — the fund will show the commitment — but "
               "the cash must be found (a levy, or a transfer in).")
    return AffordabilityCheck(False, "warn", detail,
                              available=available, shortfall=shortfall)


# ---------------------------------------------------------------------------
# Cash forecasting / sustainability
# ---------------------------------------------------------------------------

@dataclass
class ForecastMonth:
    month: str                 # 'YYYY-MM'
    opening: Decimal
    inflow: Decimal            # projected dues + expected levy
    outflow: Decimal           # projected benefit payouts
    closing: Decimal

    @property
    def negative(self):
        return self.closing < 0


@dataclass
class Forecast:
    scheme: object
    months: list = field(default_factory=list)   # list[ForecastMonth]
    basis: str = ""

    @property
    def runs_dry(self):
        return any(m.negative for m in self.months)

    @property
    def first_dry_month(self):
        for m in self.months:
            if m.negative:
                return m.month
        return None


def _avg_monthly(scheme, *, kind, months_back=6, as_of=None):
    """A simple trailing average of a monthly cash flow, used as the forecast's
    run-rate. `kind` is 'inflow' (contributions received) or 'outflow' (benefits
    paid). Uses the registry figures so the run-rate is built on the same numbers
    the reports show, not a parallel count."""
    from benevolent.services import reporting
    as_of = as_of or _dt.date.today()
    start = (as_of.replace(day=1) - _dt.timedelta(days=1))
    # step back months_back whole months
    y, m = as_of.year, as_of.month
    m -= months_back
    while m <= 0:
        m += 12
        y -= 1
    window_start = _dt.date(y, m, 1)
    if kind == "inflow":
        total = _money(reporting.contributions_total(window_start, as_of, scheme))
    else:
        total = _money(reporting.payouts_total(window_start, as_of, scheme))
    return total / Decimal(max(1, months_back))


def forecast_scheme(scheme, *, months=6, as_of=None):
    """Project the fund forward month by month at its recent run-rate, so a
    committee can see whether it is sustainable — and if not, roughly when it
    runs dry. Deliberately simple and transparent: a trailing average of real
    inflows and outflows, applied forward, with the known approved-but-unpaid
    commitments landing in the first month rather than being smeared across the
    average. A forecast a treasurer cannot follow by hand is one they cannot
    trust with a family's welfare fund."""
    from benevolent.services import reporting
    as_of = as_of or _dt.date.today()

    inflow_rate = _avg_monthly(scheme, kind="inflow", as_of=as_of)
    outflow_rate = _avg_monthly(scheme, kind="outflow", as_of=as_of)
    approved_unpaid = _money(reporting.approved_unpaid_total(scheme))

    fc = Forecast(
        scheme=scheme,
        basis=(f"Projected at the trailing 6-month average: {inflow_rate:,.0f} in and "
               f"{outflow_rate:,.0f} out per month, with {approved_unpaid:,.0f} of "
               f"approved-but-unpaid benefits landing in the first month."))
    balance = _money(reporting.scheme_balance(scheme))

    y, m = as_of.year, as_of.month
    for i in range(months):
        opening = balance
        inflow = inflow_rate
        outflow = outflow_rate + (approved_unpaid if i == 0 else Decimal(0))
        closing = opening + inflow - outflow
        fc.months.append(ForecastMonth(
            month=f"{y}-{m:02d}", opening=opening, inflow=inflow,
            outflow=outflow, closing=closing))
        balance = closing
        m += 1
        if m > 12:
            m = 1
            y += 1
    return fc
