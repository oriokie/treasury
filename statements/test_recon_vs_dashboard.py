"""The reconciliation's cash book is the dashboard's closing balance.

Not "close to", not "computed similarly" — the same figure, from the same
service, for the same date. This is the check that would have caught 3.41.2 the
moment it was written: it rebuilt the cash-book balance from history, dropped
every expense keyed in after the statement date, and the reconciliation quietly
became the only page in the system disagreeing with the dashboard.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase

from cashbook.models import Expense
from core.reporting import ReportContext
from core.roles import TREASURER
from departments.models import Department, current_cash_position
from giving.models import Transaction
from reports.services import balances
from statements.views import _ledger_bank_balance

JUL31 = dt.date(2026, 7, 31)


def _entered_on(obj, *when):
    """Backdate the row's latest history entry — simple_history stamps the wall
    clock, so a fixture built today looks to history as though it was."""
    from django.utils import timezone
    latest = obj.history.order_by("-history_date", "-history_id").first()
    type(obj).history.filter(history_id=latest.history_id).update(
        history_date=timezone.make_aware(dt.datetime(*when),
                                         timezone.get_current_timezone()))


class CashBookAgreesWithDashboard(TestCase):
    def setUp(self):
        u = User.objects.create_user("agree", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.user = u
        self.fund = Department.objects.create(name="Building", fund_type="LOCAL")
        Transaction.objects.create(
            date=dt.date(2026, 7, 5), channel="BANK", direction="CREDIT",
            amount=Decimal("500000"), department=self.fund, confirmed=True,
            allocation_status="AUTO")

    def _dashboard_closing(self, on):
        """What the dashboard totals — fund_summary through the shared context,
        exactly as core.views.DashboardView builds it."""
        rows = ReportContext.for_period(None, on).fund_summary(consolidated=True)
        return balances.totals(rows)["closing"]

    def _assert_all_agree(self, on, expected=None):
        book = _ledger_bank_balance(on)
        dash = self._dashboard_closing(on)
        cash = current_cash_position(on)
        self.assertEqual(book, dash, "reconciliation vs dashboard")
        self.assertEqual(book, cash, "reconciliation vs cash position")
        if expected is not None:
            self.assertEqual(book, expected)
        return book

    def test_they_agree_on_a_quiet_month(self):
        self._assert_all_agree(JUL31, Decimal("500000"))

    def test_they_agree_when_an_expense_was_entered_late(self):
        """The 3.41.2 failure. A July expense keyed in during August belongs in
        July — a cash book is completed after the fact — so both sides must
        count it, and neither may rebuild the balance from what was known on
        the 31st."""
        e = Expense.objects.create(
            date=dt.date(2026, 7, 20), department=self.fund,
            description="Roofing", amount=Decimal("120000"),
            category="MAINTENANCE", status="PAID", recorded_by=self.user)
        _entered_on(e, 2026, 8, 14, 10, 0)          # keyed in a fortnight later
        self._assert_all_agree(JUL31, Decimal("380000"))

    def test_they_agree_when_a_receipt_was_allocated_late(self):
        t = Transaction.objects.create(
            date=dt.date(2026, 7, 28), channel="BANK", direction="CREDIT",
            amount=Decimal("9000"), department=None, confirmed=True,
            allocation_status="REVIEW")
        _entered_on(t, 2026, 7, 28, 12, 0)
        t.department = self.fund
        t.allocation_status = "MANUAL"
        t.save()
        _entered_on(t, 2026, 8, 3, 9, 0)            # allocated in August
        self._assert_all_agree(JUL31, Decimal("509000"))

    def test_they_agree_after_a_sabbath_receipting(self):
        """The bank memo route: the row is detached from every fund, and the
        envelope that carries the fund is dated the Sabbath. Neither side may
        count it in July — but the reconciliation must still explain it."""
        t = Transaction.objects.create(
            date=JUL31, channel="BANK", direction="CREDIT",
            amount=Decimal("7000"), department=None, confirmed=True,
            allocation_status="REVIEW")
        _entered_on(t, 2026, 7, 31, 16, 0)
        t.mark_manual_receipt(True)
        _entered_on(t, 2026, 8, 1, 9, 0)
        env = Transaction.objects.create(
            date=dt.date(2026, 8, 1), channel="ENVELOPE", direction="CREDIT",
            amount=Decimal("7000"), department=self.fund, confirmed=True,
            allocation_status="MANUAL")
        _entered_on(env, 2026, 8, 1, 9, 5)

        book = self._assert_all_agree(JUL31, Decimal("500000"))
        # ... and the 7,000 is carried as awaiting receipt, so the worksheet
        # still accounts for every shilling the bank held on 31 July
        self.assertEqual(balances.pending_receipts_total(JUL31), Decimal("7000"))
        self.assertEqual(book + balances.pending_receipts_total(JUL31),
                         Decimal("507000"))

    def test_the_worksheet_itself_lands_on_the_dashboard_figure(self):
        """End to end, through the call the create view makes."""
        from statements.views import start_reconciliation
        e = Expense.objects.create(
            date=dt.date(2026, 7, 20), department=self.fund,
            description="Roofing", amount=Decimal("120000"),
            category="MAINTENANCE", status="PAID", recorded_by=self.user)
        _entered_on(e, 2026, 8, 14, 10, 0)
        rec = start_reconciliation(statement_date=JUL31,
                                   bank_balance=Decimal("380000"),
                                   user=self.user)
        self.assertEqual(rec.book_balance, self._dashboard_closing(JUL31))
        self.assertEqual(rec.difference, Decimal("0"))
