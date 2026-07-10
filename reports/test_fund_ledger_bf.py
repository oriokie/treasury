"""Fund Ledger: opening balance must be brought-forward (founding opening_balance
+ prior-period net movement), not the raw founding field alone; and the
sub-accounts Payments column must hide when every fund shown is collection-only."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from giving.models import Transaction
from cashbook.models import Expense
from ledger.services.posting import ensure_chart


def _tr():
    u = User.objects.create_user("tr_bf", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class FundLedgerBroughtForwardTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.tr = _tr()
        self.d = Department.objects.create(name="BFFund", fund_type="LOCAL",
            category="MINISTRY", opening_balance=Decimal("0"))
        # prior-period (May) activity: 5000 in, 1000 out -> true opening for June = 4000
        Transaction.objects.create(date=dt.date(2026, 5, 10), amount=Decimal("5000"),
            direction="CREDIT", confirmed=True, channel="CASH",
            allocation_status="MANUAL", department=self.d)
        Expense.objects.create(date=dt.date(2026, 5, 15), department=self.d,
            description="May exp", amount=Decimal("1000"), category="OTHER",
            status="PAID", recorded_by=self.tr, approved_by=self.tr)
        # June activity
        Transaction.objects.create(date=dt.date(2026, 6, 5), amount=Decimal("2000"),
            direction="CREDIT", confirmed=True, channel="CASH",
            allocation_status="MANUAL", department=self.d)

    def test_brought_forward_helper(self):
        from reports.services.balances import brought_forward
        self.assertEqual(brought_forward(self.d, dt.date(2026, 6, 1)), Decimal("4000"))

    def test_fund_ledger_shows_true_opening_not_zero(self):
        c = Client(); c.force_login(self.tr)
        r = c.get(f"/reports/fund/{self.d.id}/?start=2026-06-01&end=2026-06-30")
        self.assertEqual(r.status_code, 200)
        b = r.content.decode()
        self.assertIn("4,000.00", b)  # opening, not 0.00

    def test_closing_carries_from_true_opening(self):
        c = Client(); c.force_login(self.tr)
        r = c.get(f"/reports/fund/{self.d.id}/?start=2026-06-01&end=2026-06-30")
        b = r.content.decode()
        # 4000 opening + 2000 June receipts = 6000 closing
        self.assertIn("6,000.00", b)

    def test_matches_dashboard_opening(self):
        from reports.services import balances
        rows = balances.department_summary(dt.date(2026, 6, 1), dt.date(2026, 6, 30),
                                            consolidated=False)
        row = next(r for r in rows if r["department"].id == self.d.id)
        from reports.services.balances import brought_forward
        self.assertEqual(row["opening"], brought_forward(self.d, dt.date(2026, 6, 1)))


class SubAccountBroughtForwardAndCollectionOnlyTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.tr = _tr()
        self.parent = Department.objects.create(name="ParentF", fund_type="LOCAL",
            category="DEVELOPMENT", opening_balance=Decimal("0"))
        self.sub = Department.objects.create(name="SubF", fund_type="LOCAL",
            category="DEVELOPMENT", parent=self.parent, opening_balance=Decimal("500"))
        # prior-period activity on the sub-account
        Transaction.objects.create(date=dt.date(2026, 5, 12), amount=Decimal("1000"),
            direction="CREDIT", confirmed=True, channel="CASH",
            allocation_status="MANUAL", department=self.sub)

    def test_subaccount_opening_is_brought_forward(self):
        c = Client(); c.force_login(self.tr)
        r = c.get(f"/reports/fund/{self.parent.id}/?start=2026-06-01&end=2026-06-30")
        b = r.content.decode()
        # sub opening = 500 founding + 1000 prior receipts = 1500
        self.assertIn("1,500.00", b)


class CollectionOnlyPaymentsColumnTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.tr = _tr()
        self.parent = Department.objects.create(name="CollP", fund_type="LOCAL",
            category="DEVELOPMENT", collection_only=True)
        self.sub = Department.objects.create(name="CollS", fund_type="LOCAL",
            category="DEVELOPMENT", parent=self.parent, collection_only=True,
            opening_balance=Decimal("100"))
        Transaction.objects.create(date=dt.date(2026, 6, 5), amount=Decimal("500"),
            direction="CREDIT", confirmed=True, channel="CASH",
            allocation_status="MANUAL", department=self.sub)

    def test_payments_column_hidden_when_all_collection_only(self):
        c = Client(); c.force_login(self.tr)
        r = c.get(f"/reports/fund/{self.parent.id}/?start=2026-06-01&end=2026-06-30")
        b = r.content.decode()
        self.assertNotIn(">Payments</th>", b)

    def test_payments_column_shown_when_mixed(self):
        Department.objects.create(name="NormalSub", fund_type="LOCAL",
            category="DEVELOPMENT", parent=self.parent, collection_only=False)
        c = Client(); c.force_login(self.tr)
        r = c.get(f"/reports/fund/{self.parent.id}/?start=2026-06-01&end=2026-06-30")
        b = r.content.decode()
        self.assertIn(">Payments</th>", b)

    def test_xlsx_export_respects_flag(self):
        c = Client(); c.force_login(self.tr)
        r = c.get(f"/reports/fund/{self.parent.id}/?export=subgroups&"
                  "start=2026-06-01&end=2026-06-30")
        self.assertEqual(r.status_code, 200)
        self.assertIn("spreadsheet", r["Content-Type"])
