"""Bug fix: the Sabbath cash count page's "Cash Disbursed" figure included
expenses paid from the separate petty cash float (Expense.paid_from_petty_cash),
which never came out of the Sabbath offering cash box being counted here.
This silently understated "expected cash on hand" and made the count show a
discrepancy that wasn't real."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase
from django.contrib.auth.models import User
from departments.models import Department
from cashbook.models import Expense
from envelopes.views import CountSessionCreate


class CashDisbursedExcludesPettyCashTests(TestCase):
    def setUp(self):
        self.tr = User.objects.create_user("tr_cashcount", password="x")
        self.d = Department.objects.create(name="CashCountFund", fund_type="LOCAL",
            category="MINISTRY")
        self.sabbath = dt.date(2026, 6, 27)
        self.view = CountSessionCreate()

    def test_petty_cash_expense_excluded_from_disbursed(self):
        Expense.objects.create(date=dt.date(2026, 6, 24), department=self.d,
            description="Normal cash exp", amount=Decimal("500"), category="OTHER",
            method="CASH", status="PAID", recorded_by=self.tr, approved_by=self.tr,
            paid_from_petty_cash=False)
        Expense.objects.create(date=dt.date(2026, 6, 25), department=self.d,
            description="Petty cash exp", amount=Decimal("300"), category="OTHER",
            method="CASH", status="PAID", recorded_by=self.tr, approved_by=self.tr,
            paid_from_petty_cash=True)
        b = self.view._breakdown(self.sabbath)
        self.assertEqual(b["disbursed"], Decimal("500"))

    def test_non_cash_methods_never_counted_regardless_of_petty_flag(self):
        Expense.objects.create(date=dt.date(2026, 6, 24), department=self.d,
            description="Bank exp", amount=Decimal("1000"), category="OTHER",
            method="BANK", status="PAID", recorded_by=self.tr, approved_by=self.tr)
        b = self.view._breakdown(self.sabbath)
        self.assertEqual(b["disbursed"], Decimal("0"))

    def test_pending_expense_not_yet_disbursed(self):
        Expense.objects.create(date=dt.date(2026, 6, 24), department=self.d,
            description="Not yet paid", amount=Decimal("500"), category="OTHER",
            method="CASH", status="PENDING", recorded_by=self.tr)
        b = self.view._breakdown(self.sabbath)
        self.assertEqual(b["disbursed"], Decimal("0"))

    def test_expected_net_reflects_the_fix(self):
        from giving.models import Transaction
        Transaction.objects.create(date=self.sabbath, service_sabbath=self.sabbath,
            amount=Decimal("2000"), direction="CREDIT", confirmed=True,
            channel="CASH", allocation_status="MANUAL", department=self.d)
        Expense.objects.create(date=dt.date(2026, 6, 25), department=self.d,
            description="Petty cash exp", amount=Decimal("300"), category="OTHER",
            method="CASH", status="PAID", recorded_by=self.tr, approved_by=self.tr,
            paid_from_petty_cash=True)
        b = self.view._breakdown(self.sabbath)
        # petty-cash spend must not reduce the expected float
        self.assertEqual(b["net"], Decimal("2000"))
