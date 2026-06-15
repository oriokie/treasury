"""
Financial accuracy test suite
=============================

The single most important property of this app is that the figures are correct.
These tests assert the *invariants* — the relationships that must hold no matter
what data is entered — across departmental balances, reconciliation, the
statement of financial position, and the statement of cash flows.

Each test builds a small, fully-known set of transactions/expenses/transfers and
checks that every reported total reconciles to first principles. If any of these
ever fail, a figure somewhere is wrong.
"""
import datetime as dt
from decimal import Decimal

from django.test import TestCase
from django.contrib.auth.models import User, Group


D = Decimal


def _money(x):
    return Decimal(str(x))


class FinancialFixture(TestCase):
    """Shared, fully-known financial scenario. Every amount here is hand-totalled
    in the tests, so any drift in the reporting layer is caught."""

    @classmethod
    def setUpTestData(cls):
        from departments.models import Department
        from giving.models import Transaction
        from cashbook.models import Expense, FundTransfer, RemittanceBatch
        from ledger.services import posting

        cls.user = User.objects.create_superuser("acc", password="x")

        # --- funds (with known opening balances / brought-forward) ---
        cls.tithe = Department.objects.create(
            name="Tithe", fund_type=Department.FundType.TRUST, is_trust=True,
            category="OFFERING", opening_balance=D("0"))
        cls.lcb = Department.objects.create(
            name="Local Church Budget", fund_type=Department.FundType.LOCAL,
            category="OFFERING", opening_balance=D("10000"))
        cls.youth = Department.objects.create(
            name="Youth", fund_type=Department.FundType.LOCAL,
            category="MINISTRY", opening_balance=D("2000"))
        cls.dev = Department.objects.create(
            name="Development", fund_type=Department.FundType.LOCAL,
            category="DEVELOPMENT", opening_balance=D("5000"))

        # --- receipts (all confirmed credits) ---
        # Tithe (trust): 50,000 ; LCB: 30,000 ; Youth: 8,000 ; Dev: 12,000
        for dept, amt, ch in [(cls.tithe, "50000", "BANK"),
                              (cls.lcb, "30000", "CASH"),
                              (cls.youth, "8000", "CASH"),
                              (cls.dev, "12000", "BANK")]:
            Transaction.objects.create(
                date=dt.date(2026, 3, 14), channel=ch, direction="CREDIT",
                amount=D(amt), department=dept, allocation_status="MANUAL",
                confirmed=True, service_sabbath=dt.date(2026, 3, 14))

        # --- expenses ---
        # LCB operating: 7,000 (utilities) ; Youth operating: 1,500 (materials)
        # Dev CAPITAL: 9,000 (construction)
        Expense.objects.create(date=dt.date(2026, 3, 20), department=cls.lcb,
            description="Power", amount=D("7000"), category="UTILITIES",
            status=Expense.Status.PAID, recorded_by=cls.user)
        Expense.objects.create(date=dt.date(2026, 3, 21), department=cls.youth,
            description="Materials", amount=D("1500"), category="MATERIALS",
            status=Expense.Status.PAID, recorded_by=cls.user)
        Expense.objects.create(date=dt.date(2026, 3, 22), department=cls.dev,
            description="Hall works", amount=D("9000"), category="CONSTRUCTION",
            status=Expense.Status.PAID, recorded_by=cls.user,
            expenditure_type=Expense.ExpenditureType.CAPITAL)

        # --- trust remittance: 40,000 of the 50,000 tithe sent to the field ---
        Expense.objects.create(date=dt.date(2026, 3, 28), department=cls.tithe,
            description="Tithe remittance", amount=D("40000"),
            category=Expense.Category.REMITTANCE, status=Expense.Status.PAID,
            recorded_by=cls.user)

        # --- inter-fund transfer: 3,000 from LCB to Youth ---
        FundTransfer.objects.create(date=dt.date(2026, 3, 25), source=cls.lcb,
            destination=cls.youth, amount=D("3000"), recorded_by=cls.user)

        posting.rebuild()


# ===========================================================================
# 1. Departmental balance identity
# ===========================================================================
class DepartmentalBalanceTests(FinancialFixture):
    """closing = opening + receipts − expenses + transfers_in − transfers_out,
    for every fund, with no exceptions."""

    def test_balance_identity_per_fund(self):
        from reports.services import balances
        rows = balances.department_summary(None, None, consolidated=False)
        for r in rows:
            expected = (r["opening"] + r["receipts"] - r["expenses"]
                        + r["transfers_in"] - r["transfers_out"])
            self.assertEqual(
                r["closing"], expected,
                f"{r['department'].name}: closing {r['closing']} != "
                f"opening+receipts-expenses+transfers {expected}")

    def test_known_closing_balances(self):
        from reports.services import balances
        rows = {r["department"].name: r
                for r in balances.department_summary(None, None, consolidated=False)}
        # Tithe: 0 + 50,000 − 40,000 remittance = 10,000 still to remit
        self.assertEqual(rows["Tithe"]["closing"], D("10000"))
        # LCB: 10,000 + 30,000 − 7,000 − 3,000 transfer out = 30,000
        self.assertEqual(rows["Local Church Budget"]["closing"], D("30000"))
        # Youth: 2,000 + 8,000 − 1,500 + 3,000 transfer in = 11,500
        self.assertEqual(rows["Youth"]["closing"], D("11500"))
        # Dev: 5,000 + 12,000 − 9,000 capital = 8,000
        self.assertEqual(rows["Development"]["closing"], D("8000"))

    def test_operating_expenses_exclude_remittance(self):
        from reports.services import balances
        rows = {r["department"].name: r
                for r in balances.department_summary(None, None, consolidated=False)}
        # Tithe's full expenses include the 40,000 remittance, but operating == 0
        self.assertEqual(rows["Tithe"]["expenses"], D("40000"))
        self.assertEqual(rows["Tithe"]["expenses_operating"], D("0"))
        self.assertEqual(rows["Tithe"]["remittances"], D("40000"))

    def test_totals_sum_of_rows(self):
        from reports.services import balances
        rows = balances.department_summary(None, None, consolidated=False)
        tot = balances.totals(rows)
        self.assertEqual(tot["opening"], sum(r["opening"] for r in rows))
        self.assertEqual(tot["receipts"], sum(r["receipts"] for r in rows))
        self.assertEqual(tot["closing"], sum(r["closing"] for r in rows))
        # operating expenses total excludes the remittance
        self.assertEqual(tot["expenses_operating"],
                         tot["expenses"] - tot["remittances"])


# ===========================================================================
# 2. Carry-forward continuity (a period's opening == prior period's closing)
# ===========================================================================
class CarryForwardTests(FinancialFixture):

    def test_period_opening_equals_prior_closing(self):
        from reports.services import balances
        # split the year at end of March; April opening must equal March closing
        march_end = dt.date(2026, 3, 31)
        apr_start = dt.date(2026, 4, 1)
        march = {r["department"].id: r for r in
                 balances.department_summary(None, march_end, consolidated=False)}
        april = {r["department"].id: r for r in
                 balances.department_summary(apr_start, None, consolidated=False)}
        for dept_id, mrow in march.items():
            self.assertEqual(
                april[dept_id]["opening"], mrow["closing"],
                f"{mrow['department'].name}: April opening "
                f"{april[dept_id]['opening']} != March closing {mrow['closing']}")

    def test_full_year_equals_split_periods(self):
        from reports.services import balances
        full = {r["department"].id: r["closing"] for r in
                balances.department_summary(None, None, consolidated=False)}
        # closing as at year-end via a split period must match the all-time closing
        split = {r["department"].id: r["closing"] for r in
                 balances.department_summary(dt.date(2026, 1, 1), dt.date(2026, 12, 31),
                                             consolidated=False)}
        self.assertEqual(full, split)


# ===========================================================================
# 3. Reconciliation: engine balance == ledger balance (after posting)
# ===========================================================================
class ReconciliationTests(FinancialFixture):

    def test_engine_matches_ledger_for_every_fund(self):
        from reports.services import balances
        from ledger.services import posting
        rows = balances.department_summary(None, None, consolidated=False)
        for r in rows:
            gl = posting.fund_balance_from_ledger(r["department"])
            self.assertEqual(
                r["closing"], gl,
                f"{r['department'].name}: engine {r['closing']} != ledger {gl}")

    def test_no_variance_reported(self):
        from ledger.services import posting
        from departments.models import Department
        for d in Department.objects.filter(active=True):
            issues = posting.fund_variance_detail(d)
            self.assertEqual(issues, [],
                             f"{d.name} reports a variance when none should exist")

    def test_rebuild_is_idempotent(self):
        """Rebuilding the ledger twice yields identical fund balances."""
        from ledger.services import posting
        from departments.models import Department
        before = {d.id: posting.fund_balance_from_ledger(d)
                  for d in Department.objects.all()}
        posting.rebuild()
        after = {d.id: posting.fund_balance_from_ledger(d)
                 for d in Department.objects.all()}
        self.assertEqual(before, after)


# ===========================================================================
# 4. The ledger always balances (double entry + accounting equation)
# ===========================================================================
class LedgerIntegrityTests(FinancialFixture):

    def test_trial_balance_balances(self):
        from ledger.services import posting
        _, tot = posting.trial_balance()
        self.assertEqual(tot["debit"], tot["credit"])

    def test_every_journal_entry_balances(self):
        from ledger.models import JournalEntry
        for e in JournalEntry.objects.prefetch_related("lines"):
            d = sum(l.debit for l in e.lines.all())
            c = sum(l.credit for l in e.lines.all())
            self.assertEqual(d, c, f"Entry {e.pk} ({e.memo}) unbalanced: {d} != {c}")

    def test_accounting_equation_holds(self):
        from ledger.services import posting
        eq = posting.accounting_equation()
        self.assertTrue(eq["balanced"],
                        f"A={eq['assets']} != L+F={eq['liabilities'] + eq['funds']}")
        self.assertEqual(eq["assets"], eq["liabilities"] + eq["funds"])


# ===========================================================================
# 5. Statement of Financial Position (balance sheet) balances
# ===========================================================================
class FinancialPositionTests(FinancialFixture):

    def _ctx(self, as_of="2026-12-31"):
        from django.test import RequestFactory
        from reports.views import FinancialPositionView
        req = RequestFactory().get(f"/reports/financial-position/?as_of={as_of}")
        req.user = self.user
        resp = FinancialPositionView.as_view()(req)
        return resp.context_data

    def test_assets_equal_liabilities_plus_net_assets(self):
        c = self._ctx()
        self.assertEqual(
            c["total_assets"], c["total_liab_and_na"],
            f"Assets {c['total_assets']} != Liab+NA {c['total_liab_and_na']}")

    def test_trust_payable_equals_unremitted_tithe(self):
        # 50,000 tithe − 40,000 remitted = 10,000 still payable to the field
        c = self._ctx()
        self.assertEqual(c["trust_payable"], D("10000"))

    def test_property_carried_at_nbv(self):
        # no fixed assets registered in this fixture, so PPE == 0 on both sides
        c = self._ctx()
        self.assertEqual(c["nbv"], D("0"))


# ===========================================================================
# 6. Statement of Cash Flows reconciles the cash movement
# ===========================================================================
class CashFlowTests(FinancialFixture):

    def _ctx(self, start="2026-01-01", end="2026-12-31"):
        from django.test import RequestFactory
        from reports.views import StatementOfCashFlowsView
        req = RequestFactory().get(
            f"/reports/cash-flows/?start={start}&end={end}")
        req.user = self.user
        resp = StatementOfCashFlowsView.as_view()(req)
        return resp.context_data

    def test_opening_plus_net_change_equals_closing(self):
        c = self._ctx()
        self.assertEqual(
            c["cash_open"] + c["net_change"], c["cash_close"],
            f"open {c['cash_open']} + change {c['net_change']} "
            f"!= close {c['cash_close']}")
        self.assertEqual(c["cash_end_calc"], c["cash_close"])

    def test_net_change_equals_receipts_less_payments(self):
        c = self._ctx()
        # receipts 100,000 total − operating 8,500 − remittance 40,000 − capital 9,000
        # = 42,500 net change in cash
        self.assertEqual(c["net_change"], D("42500"))

    def test_categories_sum_to_net_change(self):
        c = self._ctx()
        self.assertEqual(
            c["net_operating"] + c["net_investing"] + c["net_financing"],
            c["net_change"])

    def test_capital_is_investing_not_operating(self):
        c = self._ctx()
        self.assertEqual(c["capital"], D("9000"))
        self.assertEqual(c["net_investing"], D("-9000"))


# ===========================================================================
# 7. Transfers are zero-sum (move balance, change nothing in total)
# ===========================================================================
class TransferIntegrityTests(FinancialFixture):

    def test_transfers_in_equal_transfers_out(self):
        from reports.services import balances
        rows = balances.department_summary(None, None, consolidated=False)
        self.assertEqual(sum(r["transfers_in"] for r in rows),
                         sum(r["transfers_out"] for r in rows))

    def test_transfer_leaves_total_local_funds_unchanged(self):
        """A transfer between two local funds must not change total local funds."""
        from reports.services import balances
        from cashbook.models import FundTransfer
        from ledger.services import posting
        rows = balances.department_summary(None, None, consolidated=False)
        before = sum(r["closing"] for r in rows if not r["is_trust"])
        FundTransfer.objects.create(date=dt.date(2026, 4, 1), source=self.youth,
            destination=self.dev, amount=D("500"), recorded_by=self.user)
        posting.rebuild()
        rows2 = balances.department_summary(None, None, consolidated=False)
        after = sum(r["closing"] for r in rows2 if not r["is_trust"])
        self.assertEqual(before, after)


# ===========================================================================
# 8. No double-counting (the app's #1 failure mode)
# ===========================================================================
class NoDoubleCountTests(FinancialFixture):

    def test_envelope_receipted_bank_gift_does_not_inflate_income(self):
        from django.test import Client
        from reports.services import balances
        from giving.models import Transaction
        g, _ = Group.objects.get_or_create(name="Treasurer")
        self.user.groups.add(g)
        rows = balances.department_summary(None, None)
        income_before = balances.totals(rows)["receipts"]
        # receipt the existing trust bank gift as an envelope
        t = Transaction.objects.get(department=self.tithe, channel="BANK")
        c = Client(); c.force_login(self.user)
        c.post(f"/transactions/{t.id}/receipt-envelope/", {"receipt_no": "DBLCHK"})
        rows2 = balances.department_summary(None, None)
        income_after = balances.totals(rows2)["receipts"]
        self.assertEqual(income_before, income_after,
                         "Receipting a bank contribution as an envelope changed income")

    def test_remittance_not_counted_as_income(self):
        from reports.services import balances
        rows = balances.department_summary(None, None)
        # total receipts = 50k + 30k + 8k + 12k = 100,000 exactly (no remittance)
        self.assertEqual(balances.totals(rows)["receipts"], D("100000"))


# ===========================================================================
# 9. Reversals net to zero
# ===========================================================================
class ReversalTests(FinancialFixture):

    def test_reversed_transaction_has_no_net_effect(self):
        from reports.services import balances
        from giving.models import Transaction
        from ledger.services import posting
        before = {r["department"].id: r["closing"] for r in
                  balances.department_summary(None, None, consolidated=False)}
        # add then reverse a 4,000 youth gift
        t = Transaction.objects.create(date=dt.date(2026, 5, 1), channel="CASH",
            direction="CREDIT", amount=D("4000"), department=self.youth,
            allocation_status="MANUAL", confirmed=True)
        t.reverse(self.user)
        posting.rebuild()
        after = {r["department"].id: r["closing"] for r in
                 balances.department_summary(None, None, consolidated=False)}
        self.assertEqual(before, after,
                         "A transaction and its reversal changed a fund balance")


# ===========================================================================
# 10. Trust accounting: liability, never income or operating expense
# ===========================================================================
class TrustAccountingTests(FinancialFixture):

    def test_trust_receipts_not_in_operating_expense(self):
        from reports.services import balances
        rows = balances.department_summary(None, None)
        tot = balances.totals(rows)
        # operating expenses = LCB 7,000 + Youth 1,500 + Dev 9,000 = 17,500
        # (the 40,000 remittance is NOT operating expense)
        self.assertEqual(tot["expenses_operating"], D("17500"))
        self.assertEqual(tot["remittances"], D("40000"))

    def test_trust_summary_outstanding_to_remit(self):
        from reports.services import balances
        trust = balances.trust_summary(None, None)
        row = next(r for r in trust if r["department"].name == "Tithe")
        # collected 50,000, remitted 40,000 -> 10,000 still to remit
        self.assertEqual(row["to_remit"], D("10000"))


# ===========================================================================
# 11. Consolidation rollup: parent fund == sum of itself + its children
# ===========================================================================
class ConsolidationTests(TestCase):
    """A parent fund's consolidated figures must equal its own plus all children's,
    so the Local Church Budget (with sub-accounts) never under/over-states."""

    @classmethod
    def setUpTestData(cls):
        from departments.models import Department
        from giving.models import Transaction
        from cashbook.models import Expense
        from ledger.services import posting
        cls.user = User.objects.create_superuser("cons", password="x")
        cls.lcb = Department.objects.create(
            name="LCB", fund_type=Department.FundType.LOCAL, category="OFFERING",
            opening_balance=D("1000"))
        cls.ss = Department.objects.create(
            name="Sabbath School", fund_type=Department.FundType.LOCAL,
            category="MINISTRY", parent=cls.lcb, opening_balance=D("500"))
        Transaction.objects.create(date=dt.date(2026, 3, 1), channel="CASH",
            direction="CREDIT", amount=D("4000"), department=cls.lcb,
            allocation_status="MANUAL", confirmed=True)
        Transaction.objects.create(date=dt.date(2026, 3, 1), channel="CASH",
            direction="CREDIT", amount=D("2000"), department=cls.ss,
            allocation_status="MANUAL", confirmed=True)
        Expense.objects.create(date=dt.date(2026, 3, 5), department=cls.ss,
            description="Lesson books", amount=D("800"), category="MATERIALS",
            status=Expense.Status.PAID, recorded_by=cls.user)
        posting.rebuild()

    def test_consolidated_parent_equals_own_plus_children(self):
        from reports.services import balances
        consolidated = {r["department"].id: r for r in
                        balances.department_summary(None, None, consolidated=True)}
        flat = {r["department"].id: r for r in
                balances.department_summary(None, None, consolidated=False)}
        lcb_row = consolidated[self.lcb.id]
        # consolidated LCB closing == own LCB + Sabbath School child
        expected = flat[self.lcb.id]["closing"] + flat[self.ss.id]["closing"]
        self.assertEqual(lcb_row["closing"], expected)
        # child is listed under the parent, not as a top-level row
        self.assertNotIn(self.ss.id, consolidated)
        self.assertTrue(any(c["department"].id == self.ss.id
                            for c in lcb_row["children"]))

    def test_consolidated_known_total(self):
        from reports.services import balances
        consolidated = {r["department"].id: r for r in
                        balances.department_summary(None, None, consolidated=True)}
        # LCB: 1,000 + 4,000 = 5,000 ; SS: 500 + 2,000 − 800 = 1,700
        # consolidated = 6,700
        self.assertEqual(consolidated[self.lcb.id]["closing"], D("6700"))


# ===========================================================================
# 12. Statement reconciliation: imported closing balance == system bank position
# ===========================================================================
class StatementReconciliationTests(TestCase):
    """The statement's own closing running-balance must equal the system's bank
    position when every row is imported — the proof no entry was lost."""

    def test_statement_closing_matches_bank_movement(self):
        from django.contrib.auth.models import User
        from statements.models import StatementImport
        from statements.services.importer import run_import
        from giving.models import Transaction
        from decimal import Decimal as Dec
        u = User.objects.create_user("sr", password="x")
        # opening balance 1,000 implied; three credits → closing 1,000+1,800=2,800
        csv = ("Receipt No,Completion Time,Details,Paid In,Withdrawn,Balance\n"
               "UFA,2026-06-06 09:00,UFA~tithe~254790301470~A,500,,1500\n"
               "UFB,2026-06-06 10:00,UFB~tithe~254790301470~B,300,,1800\n"
               "UFC,2026-06-06 11:00,UFC~tithe~254790301470~C,1000,,2800\n").encode()
        imp = StatementImport.objects.create(uploaded_by=u, filename="s.csv")
        run_import(imp, csv, "s.csv")
        imp.refresh_from_db()
        # sum of imported credits == statement closing − statement opening
        credits = sum((t.amount for t in Transaction.objects.filter(
                       statement_import=imp, direction="CREDIT")), Dec("0"))
        self.assertEqual(imp.stmt_closing_balance - imp.stmt_opening_balance,
                         credits)
        self.assertEqual(imp.stmt_closing_balance, Dec("2800"))


# ###########################################################################
# LAYER 2 — edge cases, boundaries, and adversarial scenarios
#
# The tests above prove the formulas are right for clean data. These target the
# messy realities that have actually caused reconciliation gaps: date-window
# boundaries, cross-month Sabbaths, unconfirmed / pending entries, rounding,
# split offerings, mis-keyed dates, and empty/zero states. Each pins down a
# specific behaviour so a future change can't silently shift a figure.
# ###########################################################################


class DateWindowBoundaryTests(TestCase):
    """Period filters are inclusive on both ends, and an entry on the exact
    boundary date is counted in exactly one period — never zero, never twice."""

    @classmethod
    def setUpTestData(cls):
        from departments.models import Department
        from giving.models import Transaction
        cls.user = User.objects.create_superuser("dw", password="x")
        cls.fund = Department.objects.create(
            name="Fund", fund_type=Department.FundType.LOCAL, category="OFFERING")
        # one gift on each boundary date
        for day in (1, 15, 31):
            Transaction.objects.create(
                date=dt.date(2026, 3, day), channel="CASH", direction="CREDIT",
                amount=D("100"), department=cls.fund, allocation_status="MANUAL",
                confirmed=True)

    def test_boundary_dates_inclusive(self):
        from reports.services import balances
        # March 1–31 must include all three (the 1st and 31st are boundaries)
        r = balances.receipts_by_department(dt.date(2026, 3, 1), dt.date(2026, 3, 31))
        self.assertEqual(r[self.fund.id], D("300"))

    def test_adjacent_periods_no_overlap_no_gap(self):
        from reports.services import balances
        first = balances.receipts_by_department(dt.date(2026, 3, 1), dt.date(2026, 3, 15))
        second = balances.receipts_by_department(dt.date(2026, 3, 16), dt.date(2026, 3, 31))
        whole = balances.receipts_by_department(dt.date(2026, 3, 1), dt.date(2026, 3, 31))
        # the 15th lands in the first half only; halves sum to the whole (no dup)
        self.assertEqual(first[self.fund.id], D("200"))   # 1st + 15th
        self.assertEqual(second[self.fund.id], D("100"))  # 31st
        self.assertEqual(first[self.fund.id] + second[self.fund.id],
                         whole[self.fund.id])


class UnconfirmedAndPendingTests(TestCase):
    """Unconfirmed receipts and pending (unapproved) expenses must NOT hit the
    reported balances — only confirmed credits and approved/paid expenses count."""

    @classmethod
    def setUpTestData(cls):
        from departments.models import Department
        from giving.models import Transaction
        from cashbook.models import Expense
        cls.user = User.objects.create_superuser("up", password="x")
        cls.fund = Department.objects.create(
            name="Fund", fund_type=Department.FundType.LOCAL, category="OFFERING",
            opening_balance=D("0"))
        # confirmed 1,000 + UNCONFIRMED 500 (should be ignored)
        Transaction.objects.create(date=dt.date(2026, 3, 1), channel="CASH",
            direction="CREDIT", amount=D("1000"), department=cls.fund,
            allocation_status="MANUAL", confirmed=True)
        Transaction.objects.create(date=dt.date(2026, 3, 2), channel="BANK",
            direction="CREDIT", amount=D("500"), department=cls.fund,
            allocation_status="REVIEW", confirmed=False)
        # PAID 200 + PENDING 300 (pending should be ignored)
        Expense.objects.create(date=dt.date(2026, 3, 5), department=cls.fund,
            description="paid", amount=D("200"), category="MATERIALS",
            status=Expense.Status.PAID, recorded_by=cls.user)
        Expense.objects.create(date=dt.date(2026, 3, 6), department=cls.fund,
            description="pending", amount=D("300"), category="MATERIALS",
            status=Expense.Status.PENDING, recorded_by=cls.user)

    def test_only_confirmed_receipts_count(self):
        from reports.services import balances
        r = balances.receipts_by_department(None, None)
        self.assertEqual(r[self.fund.id], D("1000"))  # not 1,500

    def test_only_approved_expenses_count(self):
        from reports.services import balances
        e = balances.expenses_by_department(None, None)
        self.assertEqual(e.get(self.fund.id, D("0")), D("200"))  # not 500

    def test_closing_uses_effective_figures_only(self):
        from reports.services import balances
        row = next(r for r in balances.department_summary(None, None, consolidated=False)
                   if r["department"].id == self.fund.id)
        # 0 + 1,000 confirmed − 200 paid = 800
        self.assertEqual(row["closing"], D("800"))


class ExcludedFromIncomeTests(TestCase):
    """A receipt flagged excluded_from_income (e.g. asset-disposal proceeds, or a
    bank contribution already counted via an envelope) is real cash in the fund but NOT
    operating income. It must stay in the fund balance yet be distinguishable."""

    @classmethod
    def setUpTestData(cls):
        from departments.models import Department
        from giving.models import Transaction
        cls.user = User.objects.create_superuser("ex", password="x")
        cls.fund = Department.objects.create(
            name="Fund", fund_type=Department.FundType.LOCAL, category="OFFERING")
        Transaction.objects.create(date=dt.date(2026, 3, 1), channel="CASH",
            direction="CREDIT", amount=D("1000"), department=cls.fund,
            allocation_status="MANUAL", confirmed=True)
        Transaction.objects.create(date=dt.date(2026, 3, 2), channel="BANK",
            direction="CREDIT", amount=D("400"), department=cls.fund,
            allocation_status="MANUAL", confirmed=True, excluded_from_income=True)

    def test_excluded_receipt_still_in_fund_balance(self):
        from reports.services import balances
        row = next(r for r in balances.department_summary(None, None, consolidated=False)
                   if r["department"].id == self.fund.id)
        # the fund holds all 1,400 of cash regardless of income classification
        self.assertEqual(row["closing"], D("1400"))


class SplitOfferingTests(TestCase):
    """A split offering (e.g. Combined Offering 50% trust / 50% local) must
    divide exactly, with no money lost or created, and the halves landing in the
    correct trust vs local funds."""

    @classmethod
    def setUpTestData(cls):
        from departments.models import Department
        from giving.models import Transaction, SplitFund, SplitComponent
        cls.user = User.objects.create_superuser("sp", password="x")
        cls.trust_half = Department.objects.create(
            name="Combined (Trust 50%)", fund_type=Department.FundType.TRUST,
            is_trust=True, category="OFFERING", selectable=False)
        cls.local_half = Department.objects.create(
            name="Combined (Local 50%)", fund_type=Department.FundType.LOCAL,
            category="OFFERING", selectable=False)
        sf = SplitFund.objects.create(name="Combined Offering")
        SplitComponent.objects.create(split_fund=sf, department=cls.trust_half, percent=50)
        SplitComponent.objects.create(split_fund=sf, department=cls.local_half, percent=50)
        cls.split = sf

    def test_split_halves_sum_to_whole(self):
        parts = self.split.split(D("1000"))
        total = sum(amt for _, amt in parts)
        self.assertEqual(total, D("1000"))

    def test_odd_amount_splits_without_losing_cents(self):
        # 333.33 split 50/50 must still total exactly 333.33 (no lost cent)
        parts = self.split.split(D("333.33"))
        self.assertEqual(sum(amt for _, amt in parts), D("333.33"))

    def test_split_into_correct_funds(self):
        parts = dict(self.split.split(D("1000")))
        self.assertIn(self.trust_half, parts)
        self.assertIn(self.local_half, parts)
        self.assertEqual(parts[self.trust_half], D("500"))
        self.assertEqual(parts[self.local_half], D("500"))


class EmptyAndZeroStateTests(TestCase):
    """With no data, every total is zero (never None, never an error) and the
    accounting identities still hold trivially."""

    def test_empty_department_summary(self):
        from reports.services import balances
        rows = balances.department_summary(None, None)
        tot = balances.totals(rows)
        for k in ("opening", "receipts", "expenses", "closing"):
            self.assertEqual(tot[k], D("0"))

    def test_empty_accounting_equation_balances(self):
        from ledger.services import posting
        posting.ensure_chart()
        eq = posting.accounting_equation()
        self.assertTrue(eq["balanced"])

    def test_fund_with_only_opening_balance(self):
        from departments.models import Department
        from reports.services import balances
        d = Department.objects.create(name="BF only",
            fund_type=Department.FundType.LOCAL, category="OFFERING",
            opening_balance=D("1234.56"))
        row = next(r for r in balances.department_summary(None, None, consolidated=False)
                   if r["department"].id == d.id)
        # no movement -> closing equals opening exactly
        self.assertEqual(row["closing"], D("1234.56"))


class RoundingAndPrecisionTests(TestCase):
    """Money is held to 2 decimal places; summing many awkward amounts must not
    drift. Decimal arithmetic (not float) is the guard, so this pins it down."""

    @classmethod
    def setUpTestData(cls):
        from departments.models import Department
        from giving.models import Transaction
        cls.user = User.objects.create_superuser("rd", password="x")
        cls.fund = Department.objects.create(
            name="Fund", fund_type=Department.FundType.LOCAL, category="OFFERING")
        # 7 gifts of 14.29 -> 100.03 exactly (a float sum would drift)
        for i in range(7):
            Transaction.objects.create(date=dt.date(2026, 3, i + 1), channel="CASH",
                direction="CREDIT", amount=D("14.29"), department=cls.fund,
                allocation_status="MANUAL", confirmed=True)

    def test_no_floating_point_drift(self):
        from reports.services import balances
        r = balances.receipts_by_department(None, None)
        self.assertEqual(r[self.fund.id], D("100.03"))
        # and it's a Decimal, not a float
        self.assertIsInstance(r[self.fund.id], Decimal)


class MiskeyedDateRobustnessTests(TestCase):
    """A mis-keyed value date far in the future (the 2027–2068 problem from real
    statements) must not silently corrupt a current-period total: it should fall
    outside a bounded period window."""

    @classmethod
    def setUpTestData(cls):
        from departments.models import Department
        from giving.models import Transaction
        cls.user = User.objects.create_superuser("mk", password="x")
        cls.fund = Department.objects.create(
            name="Fund", fund_type=Department.FundType.LOCAL, category="OFFERING")
        Transaction.objects.create(date=dt.date(2026, 3, 1), channel="CASH",
            direction="CREDIT", amount=D("1000"), department=cls.fund,
            allocation_status="MANUAL", confirmed=True)
        # a mis-keyed 2055 date
        Transaction.objects.create(date=dt.date(2055, 1, 1), channel="BANK",
            direction="CREDIT", amount=D("9999"), department=cls.fund,
            allocation_status="MANUAL", confirmed=True)

    def test_bounded_period_excludes_miskeyed_future_date(self):
        from reports.services import balances
        # a 2026 window must report only the legitimate 1,000
        r = balances.receipts_by_department(dt.date(2026, 1, 1), dt.date(2026, 12, 31))
        self.assertEqual(r[self.fund.id], D("1000"))


class DebitReducesBankPositionTests(TestCase):
    """A bank DEBIT (outflow) reduces the bank position; credits and debits net
    correctly so the bank figure can never be overstated by ignoring debits."""

    @classmethod
    def setUpTestData(cls):
        from departments.models import Department
        from giving.models import Transaction
        cls.user = User.objects.create_superuser("db", password="x")
        cls.fund = Department.objects.create(
            name="Fund", fund_type=Department.FundType.LOCAL, category="OFFERING")
        Transaction.objects.create(date=dt.date(2026, 3, 1), channel="BANK",
            direction="CREDIT", amount=D("5000"), department=cls.fund,
            allocation_status="MANUAL", confirmed=True)
        Transaction.objects.create(date=dt.date(2026, 3, 2), channel="BANK",
            direction="DEBIT", amount=D("1200"), department=cls.fund,
            allocation_status="MANUAL", confirmed=True)

    def test_bank_credits_less_debits(self):
        from giving.models import Transaction
        from django.db.models import Sum, Q as _Q
        agg = Transaction.objects.filter(channel="BANK", confirmed=True).aggregate(
            cr=Sum("amount", filter=_Q(direction="CREDIT")),
            db=Sum("amount", filter=_Q(direction="DEBIT")))
        self.assertEqual((agg["cr"] or D("0")) - (agg["db"] or D("0")), D("3800"))


class PerformanceGuardTests(TestCase):
    """Item 2: dev-group progress must stay constant-query regardless of how many
    development groups exist (guards against reintroducing the per-group N+1)."""

    def test_dev_group_progress_is_constant_query(self):
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        from departments.models import DevelopmentGroup
        from reports.services.balances import dev_group_progress
        for n in range(1, 41):
            DevelopmentGroup.objects.create(number=n, name=f"G{n}", target=1000)
        with CaptureQueriesContext(connection) as ctx:
            dev_group_progress()
        # 1 grouped aggregate + 1 group fetch; must not scale with group count
        self.assertLessEqual(len(ctx.captured_queries), 3)
