"""Financial Metrics Registry — the authoritative, single home for every named
financial calculation in the treasury system (the "Semantic Reporting Layer").

Why a facade, not a rewrite
---------------------------
The inventory (docs/CALCULATION_INVENTORY.md) found that the *authoritative*
implementations of almost every accounting concept already live in a clean
services layer — chiefly reports.services.balances, ledger.services.posting,
loans.services.reporting, and the domain reporting services. The problem was
never that those were wrong; it was that a handful of call sites (dashboards,
the assistant, some report views) recomputed the same concepts inline, and
there was no single place a developer could look to find "the" definition of,
say, tithe or the cash position.

This module is that single place. It does NOT reimplement the maths — it
re-exports the existing canonical functions under stable, well-documented
semantic names, and consolidates the few genuine duplicates the inventory
identified (e.g. tithe, the "income credit" filter) so there is exactly one
implementation of each. Behaviour is unchanged: every function here either IS
the existing canonical callable or calls it directly.

How to use it
-------------
New code should import from here rather than reaching into services or writing
raw aggregates:

    from core.metrics import metrics
    figure = metrics.tithe(start, end)
    funds  = metrics.fund_summary(start, end)

Each metric carries a machine-readable definition (see `registry`) so the
"/metrics/" catalogue page and future semantic reports can enumerate what
exists without duplicating accounting logic.

Compatibility
-------------
Nothing that already works is changed. The old service functions remain and
keep their signatures; this layer sits on top. Migration is incremental: as
call sites are touched, they move to `metrics.*`. The inventory lists the
recommended migration for each site.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Metric metadata — lets the catalogue enumerate every concept with its
# accounting definition and authoritative implementation, so the registry is
# self-documenting and future reports can discover metrics rather than
# hard-coding them.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Metric:
    key: str                      # stable identifier, e.g. "tithe"
    label: str                    # human name
    category: str                 # Income / Expense / Balance / Trust / Loan / …
    definition: str               # the accounting definition, in words
    authoritative: str            # dotted path of the canonical implementation
    unit: str = "KES"             # KES, count, %, days
    inputs: str = "start, end"    # signature summary
    notes: str = ""               # business-rule nuances / why variants exist


class _Registry:
    """Holds the metric catalogue and the callables. Populated at import time
    below. Access metrics as attributes (``metrics.tithe(...)``) or look up
    their metadata via ``metrics.registry[key]``."""

    def __init__(self):
        self.registry: dict[str, Metric] = {}
        self._impl: dict[str, Callable] = {}

    def register(self, metric: Metric, impl: Callable):
        if metric.key in self.registry:
            raise ValueError(f"Metric '{metric.key}' already registered.")
        self.registry[metric.key] = metric
        self._impl[metric.key] = impl
        return impl

    def __getattr__(self, name):
        # attribute access resolves to the registered implementation
        impl = self.__dict__.get("_impl", {}).get(name)
        if impl is None:
            raise AttributeError(f"No metric named '{name}'. "
                                 f"Known: {', '.join(sorted(self._impl))}.")
        return impl

    def has(self, key) -> bool:
        return key in self.registry

    def get(self, key) -> Optional[Metric]:
        """Metadata for a metric, or None — the safe lookup for catalogues and
        tooling (attribute access raises for unknown names by design)."""
        return self.registry.get(key)

    def validate_authoritative(self):
        """Self-check that every metric's documented ``authoritative`` dotted
        path still resolves to a real module (and attribute, where the path is
        importable). Returns a list of (key, path, problem) tuples — empty when
        the registry documentation is sound. Guards against the docs drifting
        from the code as implementations are relocated (e.g. out of view
        god-files into services)."""
        import importlib
        problems = []
        for key, m in self.registry.items():
            path = m.authoritative
            # descriptive (non-importable) entries are allowed but must say so
            if "(" in path or " " in path:
                continue
            mod_path, _, attr = path.rpartition(".")
            if not mod_path:
                problems.append((key, path, "not a dotted path"))
                continue
            obj = None
            # walk from the longest importable module prefix
            parts = mod_path.split(".")
            for i in range(len(parts), 0, -1):
                try:
                    obj = importlib.import_module(".".join(parts[:i]))
                    remainder = parts[i:] + [attr]
                    break
                except ImportError:
                    continue
            else:
                problems.append((key, path, "module does not import"))
                continue
            for name in remainder:
                obj = getattr(obj, name, None)
                if obj is None:
                    problems.append((key, path, f"attribute '{name}' missing"))
                    break
        return problems

    def all(self):
        """(Metric, callable) pairs, ordered by category then label — for the
        catalogue page and documentation."""
        for key in sorted(self.registry,
                          key=lambda k: (self.registry[k].category,
                                         self.registry[k].label)):
            yield self.registry[key], self._impl[key]

    def by_category(self):
        out: dict[str, list[Metric]] = {}
        for m, _ in self.all():
            out.setdefault(m.category, []).append(m)
        return out


metrics = _Registry()


# ===========================================================================
# Canonical shared filters (consolidated from duplicates found in the inventory)
# ===========================================================================

def income_credit_filter(start=None, end=None):
    """THE definition of an "income credit": a confirmed, non-reversed CREDIT
    that is not excluded from income (so loan receipts, which raise fund cash
    but are financing not income, are excluded).

    Consolidates the near-identical inline filters previously duplicated in
    core.services.dashboard._credits and core.services.assistant._credit_filter.
    Note this differs on purpose from balances._credit_filter, which does NOT
    apply excluded_from_income because it also serves cash-position queries
    where loan cash counts. That distinction is intentional and documented in
    the inventory — do not merge the two.
    """
    from django.db.models import Q
    from giving.models import Transaction
    f = Q(direction=Transaction.Direction.CREDIT, confirmed=True,
          is_reversed=False, is_reversal=False, excluded_from_income=False)
    if start:
        f &= Q(date__gte=start)
    if end:
        f &= Q(date__lte=end)
    return f


def income_credits(start=None, end=None, **extra):
    """Queryset of income credits over an optional period (the consolidated
    replacement for the dashboard/assistant ``_credits`` helpers)."""
    from giving.models import Transaction
    return Transaction.objects.filter(income_credit_filter(start, end), **extra)


# ===========================================================================
# Metric registrations — each points at the AUTHORITATIVE implementation.
# The lambdas are thin: they either forward to an existing canonical service
# function (no behaviour change) or, for consolidated concepts, are the single
# implementation everything now shares.
# ===========================================================================

def _b():
    from reports.services import balances
    return balances


# ---- Income & giving ------------------------------------------------------

metrics.register(Metric(
    "total_income", "Total income (period)", "Income",
    "Sum of all income credits (confirmed, non-reversed, not excluded from "
    "income) over the period. Excludes loan receipts.",
    "core.metrics.income_credits", inputs="start, end",
    notes="Consolidated income-credit definition shared by dashboard & assistant."),
    lambda start=None, end=None: (
        income_credits(start, end).aggregate(
            t=__import__("django").db.models.Sum("amount"))["t"] or Decimal(0)))

metrics.register(Metric(
    "tithe", "Tithe (period)", "Income",
    "Income credits on any fund whose name contains 'tithe', over the period. "
    "A key conference remittance figure.",
    "reports.services.balances.tithe_total",
    notes="Consolidates the inline name__icontains='tithe' aggregates that "
          "were duplicated in the assistant service."),
    lambda start=None, end=None: _b().tithe_total(start, end))

metrics.register(Metric(
    "income_by_channel", "Income by channel", "Income",
    "Income totals split by channel (envelope / cash / bank), with counts.",
    "reports.services.balances.income_by_channel"),
    lambda start=None, end=None: _b().income_by_channel(start, end))

metrics.register(Metric(
    "giving_by_group", "Giving by demographic group", "Income",
    "Income totals by member demographic group (Youth/AMM/AWM/…).",
    "reports.services.balances.giving_by_group"),
    lambda start=None, end=None: _b().giving_by_group(start, end))

metrics.register(Metric(
    "offering_summary", "Weekly/Sabbath offering summary", "Income",
    "Receipts by fund for each Sabbath week (1–5) of the period.",
    "reports.services.balances.offering_summary"),
    lambda start=None, end=None: _b().offering_summary(start, end))

metrics.register(Metric(
    "receipts_by_department", "Receipts by fund", "Income",
    "All confirmed fund cash received per department INCLUDING loan receipts "
    "(this is a cash figure, not an income figure — see total_income for the "
    "income-only variant).",
    "reports.services.balances.receipts_by_department",
    notes="Intentionally differs from total_income: includes loan cash."),
    lambda start=None, end=None: _b().receipts_by_department(start, end))


# ---- Fund balances --------------------------------------------------------

metrics.register(Metric(
    "fund_summary", "Fund summary (all funds)", "Balance",
    "Per-fund brought-forward, receipts, expenses, transfers and closing "
    "balance for the period. The master balance table behind most reports.",
    "reports.services.balances.department_summary",
    notes="Request-cached via core.perfcache."),
    lambda start=None, end=None, consolidated=True:
        _b().department_summary(start, end, consolidated))

metrics.register(Metric(
    "fund_balance", "Single fund balance (as-at)", "Balance",
    "Closing balance of one fund as at a date: opening + receipts − expenses "
    "± transfers.",
    "reports.services.balances.fund_balance", inputs="dept, as_of"),
    lambda dept, as_of=None: _b().fund_balance(dept, as_of))

metrics.register(Metric(
    "fund_balance_ledger", "Single fund balance (from GL)", "Balance",
    "Fund balance derived independently from posted journal lines — used to "
    "cross-check the report figure against the general ledger.",
    "ledger.services.posting.fund_balance_from_ledger", inputs="dept",
    notes="Should equal fund_balance; divergence signals a posting problem."),
    lambda dept: __import__("ledger.services.posting", fromlist=["x"])
        .fund_balance_from_ledger(dept))

metrics.register(Metric(
    "opening_cash_position", "Opening cash position (all funds)", "Balance",
    "Sum of every fund's brought-forward opening balance.",
    "departments.models.total_opening_cash_position", inputs="—"),
    lambda: __import__("departments.models", fromlist=["x"])
        .total_opening_cash_position())


# ---- Expenses -------------------------------------------------------------

metrics.register(Metric(
    "expenses_by_department", "Expenses by fund", "Expense",
    "Effective (approved/paid) operational expenses per fund for the period. "
    "Excludes liability settlements (loan repayments, trust remittances) when "
    "only_operating is set.",
    "reports.services.balances.expenses_by_department",
    inputs="start, end, only_effective, include_remittance",
    notes="doc_class-aware: liability vouchers are separable from opex."),
    lambda start=None, end=None, **kw: _b().expenses_by_department(start, end, **kw))

metrics.register(Metric(
    "operating_expense", "Operating (recurrent) expenditure", "Expense",
    "Total recurrent (operating) expenditure for the period: approved/paid, "
    "non-liability expenses of expenditure_type RECURRENT. The 'total recurrent' "
    "figure on the Income & Expenditure statement.",
    "reports.services.balances.operating_expense_total", inputs="start, end",
    notes="Addresses recommendation #23 — was computed inline in the Income "
          "Statement / Board report."),
    lambda start=None, end=None: _b().operating_expense_total(start, end))

metrics.register(Metric(
    "capital_expenditure", "Capital expenditure", "Expense",
    "Total capital expenditure for the period: approved/paid, non-liability "
    "expenses of expenditure_type CAPITAL (assets / development).",
    "reports.services.balances.capital_expenditure_total", inputs="start, end",
    notes="Addresses recommendation #23 — paired with operating_expense so the "
          "Income Statement reads both from the registry."),
    lambda start=None, end=None: _b().capital_expenditure_total(start, end))

metrics.register(Metric(
    "expense_by_category", "Expenditure by category", "Expense",
    "Effective (approved/paid, non-liability) expenditure grouped by category, "
    "optionally restricted to one expenditure_type.",
    "reports.services.balances.expense_by_category",
    inputs="start, end, expenditure_type"),
    lambda start=None, end=None, expenditure_type=None:
        _b().expense_by_category(start, end, expenditure_type))

metrics.register(Metric(
    "remittances_total", "Remittances to the field", "Trust",
    "Total remittances to the conference over the period: effective "
    "(approved/paid) expenses of category REMITTANCE. Ties to the trust-summary "
    "remittance basis and the Cash Flow Statement.",
    "reports.services.balances.remittances_total", inputs="start, end"),
    lambda start=None, end=None: _b().remittances_total(start, end))


# ---- Trust & remittance ---------------------------------------------------

metrics.register(Metric(
    "trust_summary", "Trust fund summary", "Trust",
    "Per trust fund: collected, remitted, and balance still to remit to the "
    "conference.",
    "reports.services.balances.trust_summary",
    notes="Request-cached via core.perfcache."),
    lambda start=None, end=None: _b().trust_summary(start, end))

metrics.register(Metric(
    "trust_to_remit", "Total trust still to remit", "Trust",
    "Sum across trust funds of the balance not yet remitted to the conference.",
    "core.metrics (aggregates trust_summary)",
    notes="Consolidates the sum(r['to_remit']) idiom repeated in dashboards."),
    lambda start=None, end=None: sum(
        (r["to_remit"] for r in _b().trust_summary(start, end)), Decimal(0)))

metrics.register(Metric(
    "pending_receipts_total", "Bank receipts pending allocation", "Balance",
    "Total of confirmed bank credits not yet allocated to a fund (suspense). "
    "Real money at the bank, shown as cash held in suspense on the Statement "
    "of Financial Position.",
    "reports.services.balances.pending_receipts_total", inputs="as_of"),
    lambda as_of=None: _b().pending_receipts_total(as_of))


# ---- Loans ----------------------------------------------------------------

metrics.register(Metric(
    "loans_outstanding", "Outstanding loan liability", "Loan",
    "Total outstanding loan principal as at a date, split current vs "
    "long-term. Ties to the LOANS_PAYABLE ledger account.",
    "loans.services.reporting.outstanding_liability", inputs="as_of, split_current"),
    lambda as_of=None, split_current=True:
        __import__("loans.services.reporting", fromlist=["x"])
        .outstanding_liability(as_of, split_current))

metrics.register(Metric(
    "loan_financing_by_fund", "Loan financing by fund", "Loan",
    "Loan cash received per fund over the period (financing, not income).",
    "loans.services.loans.loan_financing_by_fund"),
    lambda start=None, end=None:
        __import__("loans.services.loans", fromlist=["x"])
        .loan_financing_by_fund(start, end))

metrics.register(Metric(
    "financing_activity", "Loan financing activity (cash flow)", "Loan",
    "Loan-related cash movements for the Cash Flow Statement's financing "
    "section: receipts (in), principal repayments (out), interest, and the net.",
    "loans.services.reporting.financing_activity", inputs="start, end"),
    lambda start=None, end=None:
        __import__("loans.services.reporting", fromlist=["x"])
        .financing_activity(start, end))

metrics.register(Metric(
    "loan_retirement_income", "Loan retirement (non-cash) income", "Loan",
    "Income recognised from loan conversions / write-offs in the period — "
    "non-cash, so excluded from operating cash receipts in the Cash Flow "
    "Statement.",
    "loans.services.reporting.retirement_income", inputs="start, end"),
    lambda start=None, end=None:
        __import__("loans.services.reporting", fromlist=["x"])
        .retirement_income(start, end))


# ---- Accounting integrity -------------------------------------------------

metrics.register(Metric(
    "trial_balance", "Trial balance", "Accounting",
    "Debit/credit totals per account from posted journal lines; the totals "
    "must be equal.",
    "ledger.services.posting.trial_balance", inputs="start, end"),
    lambda start=None, end=None:
        __import__("ledger.services.posting", fromlist=["x"])
        .trial_balance(start, end))

metrics.register(Metric(
    "accounting_equation", "Accounting equation check", "Accounting",
    "Assets = Liabilities + Net assets, from the general ledger. Returns the "
    "three totals and whether they balance.",
    "ledger.services.posting.accounting_equation", inputs="—"),
    lambda: __import__("ledger.services.posting", fromlist=["x"])
        .accounting_equation())


# ---- Payments -------------------------------------------------------------

metrics.register(Metric(
    "payments_outstanding_asof", "Outstanding payment instruments (as-at)",
    "Payment",
    "Payment instruments issued but not yet cleared/cancelled as at a date, "
    "judged on event dates (not current status) so historical reconciliations "
    "are correct.",
    "cashbook.models.PaymentInstrument.outstanding_asof", inputs="as_of"),
    lambda as_of=None: __import__("cashbook.models", fromlist=["x"])
        .PaymentInstrument.outstanding_asof(
            as_of or __import__("datetime").date.today()))

metrics.register(Metric(
    "unpresented_payments_total", "Unpresented payments total (as-at)",
    "Payment",
    "Value of bank-clearing instruments outstanding as at a date — the "
    "reconciling 'less unpresented' figure.",
    "cashbook.services.treasury_position.unpresented_cheques_total",
    inputs="as_of"),
    lambda as_of=None: _tp().unpresented_cheques_total(as_of))


# ===========================================================================
# Treasury position — cash locations & receivables
# (authoritative home: cashbook.services.treasury_position, relocated from the
#  cashbook views god-file; reports.services.balances.bank_position extracted
#  from the Bank Position view so the calculation exists exactly once)
# ===========================================================================

def _tp():
    from cashbook.services import treasury_position
    return treasury_position


metrics.register(Metric(
    "petty_cash_balance", "Petty cash float balance (as-at)", "Balance",
    "The petty-cash float as at a date: top-ups less petty disbursements, plus "
    "cash refunded into the box, less cash out with advance holders. A cash "
    "LOCATION control total (reconciled against the box), not a fund.",
    "cashbook.services.treasury_position.petty_balance_asof", inputs="as_of",
    notes="Consolidates the _petty_balance_asof helper previously imported "
          "from cashbook.views by the assistant, dashboards and period close."),
    lambda as_of=None: _tp().petty_balance_asof(
        as_of or __import__("datetime").date.today()))

metrics.register(Metric(
    "staff_advances_outstanding", "Outstanding staff advances (as-at)",
    "Balance",
    "Money advanced to staff not yet accounted for by approved/paid expense "
    "receipts as at a date — a receivable. Only positive balances count (a "
    "shortfall is owed to staff, not a receivable).",
    "cashbook.services.treasury_position.outstanding_advances_total",
    inputs="as_of",
    notes="Bank- and petty-issued splits exist as service functions "
          "(outstanding_bank_advances_total / outstanding_petty_advances_total) "
          "for the reconciliation worksheet."),
    lambda as_of=None: _tp().outstanding_advances_total(as_of))

metrics.register(Metric(
    "bank_position", "Bank position (system vs statement)", "Balance",
    "The system's bank balance (opening bank balance + confirmed bank credits "
    "− confirmed bank debits − direct bank-paid expenses) compared with the "
    "latest imported statement's closing balance, with the difference. "
    "Returns a dict: opening, opening_configured, bank_credits, bank_debits, "
    "bank_expenses, system_balance, statement_balance, statement_date, "
    "difference.",
    "reports.services.balances.bank_position", inputs="as_of",
    notes="Depends on SiteConfig.opening_bank_balance being configured "
          "(recommendation #9) — opening_configured flags when it is not."),
    lambda as_of=None: _b().bank_position(as_of))

metrics.register(Metric(
    "cash_in_transit", "Cash in transit (as-at)", "Balance",
    "Deposits in transit as at a date: the IN_TRANSIT reconciling items on the "
    "most recent bank-reconciliation worksheet dated on or before the date. "
    "Zero when no worksheet exists.",
    "cashbook.services.treasury_position.cash_in_transit_asof", inputs="as_of"),
    lambda as_of=None: _tp().cash_in_transit_asof(as_of))

metrics.register(Metric(
    "pending_expense_claims", "Pending expense claims", "Expense",
    "Expense claims awaiting treasurer approval: count and total of expenses "
    "in PENDING status dated on or before the date. Returns {count, total}. "
    "Status is the current state — this answers what is pending now, not a "
    "historical reconstruction.",
    "cashbook.services.treasury_position.pending_expense_claims",
    inputs="as_of"),
    lambda as_of=None: _tp().pending_expense_claims(as_of))


# ---- Payments & budget ------------------------------------------------------

metrics.register(Metric(
    "total_payments", "Total payments (period)", "Expense",
    "All payments for the period: operating (recurrent) expenditure + capital "
    "expenditure + remittances to the field. The 'Total payments' headline on "
    "the Treasurer's Report; pairs with total receipts from fund_summary.",
    "core.metrics (operating_expense + capital_expenditure + remittances_total)",
    inputs="start, end",
    notes="A named composition of three registry metrics so the definition "
          "exists once instead of being re-summed per report section."),
    lambda start=None, end=None: (
        _b().operating_expense_total(start, end)
        + _b().capital_expenditure_total(start, end)
        + _b().remittances_total(start, end)))

metrics.register(Metric(
    "budget_vs_actual", "Budget vs actual (formal budgets)", "Expense",
    "Per-fund budgeted vs actual expenditure for a budget year/period, with "
    "variance — from the formal Budget/BudgetLine records (not the legacy "
    "annual_budget attribute).",
    "reports.services.budget.budget_vs_actual",
    inputs="year, period, month, quarter"),
    lambda year, period="ANNUAL", month=None, quarter=None:
        __import__("reports.services.budget", fromlist=["x"])
        .budget_vs_actual(year, period, month, quarter))

metrics.register(Metric(
    "dev_group_progress", "Development-group progress", "Income",
    "Per development group: collected vs target vs balance and % complete "
    "over the period.",
    "reports.services.balances.dev_group_progress"),
    lambda start=None, end=None: _b().dev_group_progress(start, end))


# ---- Fund attention (canonical selectors over fund_summary) ----------------

metrics.register(Metric(
    "negative_fund_balances", "Funds with negative balances", "Balance",
    "Funds whose closing balance for the period is below zero — overdrawn "
    "funds requiring management attention. A canonical selector over "
    "fund_summary (no independent balance calculation).",
    "core.metrics (filters fund_summary)", inputs="start, end"),
    lambda start=None, end=None: [
        r for r in _b().department_summary(start, end, True)
        if (r["closing"] or Decimal(0)) < 0])

metrics.register(Metric(
    "dormant_funds", "Dormant funds", "Balance",
    "Funds with no receipts, expenses or transfers in the period but a "
    "non-zero closing balance — idle money the board may wish to review. A "
    "canonical selector over fund_summary (no independent balance "
    "calculation).",
    "core.metrics (filters fund_summary)", inputs="start, end"),
    lambda start=None, end=None: [
        r for r in _b().department_summary(start, end, True)
        if not (r["receipts"] or 0) and not (r["expenses"] or 0)
        and not (r.get("net_transfer") or 0)
        and (r["closing"] or Decimal(0)) != 0])


# ===========================================================================
# Accrual-basis overlay: payables, accruals, prepayments
# (authoritative home: cashbook.services.treasury_position, relocated from
#  cashbook/views.py — see that module's docstring. The legacy Statement of
#  Financial Position (reports/views.py FinancialPositionView) has always
#  shown these by calling the functions directly; registering them here lets
#  the engine-based Financial Position summary — used by the Treasurer's
#  Report board pack — show the identical accrual-basis adjustments through
#  ctx.metric(), rather than the two statements silently diverging.)
# ===========================================================================

metrics.register(Metric(
    "payables_outstanding", "Accounts payable (as-at)", "Balance",
    "Credit purchases owed as at a date: recorded on/before it and either "
    "still unsettled, or settled only after it — so settling on the 15th "
    "still shows as a liability on a 14th statement.",
    "cashbook.services.treasury_position.open_payables_total", inputs="as_of"),
    lambda as_of=None: _tp().open_payables_total(
        as_of or __import__("datetime").date.today()))

metrics.register(Metric(
    "accruals_outstanding", "Accrued expenses (as-at)", "Balance",
    "Expenses incurred but not yet invoiced or paid, owed as at a date — "
    "same as-of treatment as payables_outstanding.",
    "cashbook.services.treasury_position.open_accruals_total", inputs="as_of"),
    lambda as_of=None: _tp().open_accruals_total(
        as_of or __import__("datetime").date.today()))

metrics.register(Metric(
    "prepayments_unexpired", "Unexpired prepayments (as-at)", "Balance",
    "The unexpired (not-yet-consumed) portion of every recorded prepayment "
    "as at a date — an asset (a future benefit already paid for).",
    "cashbook.services.treasury_position.unexpired_prepayments_total",
    inputs="as_of"),
    lambda as_of=None: _tp().unexpired_prepayments_total(
        as_of or __import__("datetime").date.today()))
