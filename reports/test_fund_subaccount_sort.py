"""Fund report sub-accounts sort by receipts (busiest first) and offer a JPEG (#11)."""
import datetime as dt
from decimal import Decimal

from django.test import TestCase, Client
from django.contrib.auth.models import User, Group

from departments.models import Department
from giving.models import Transaction


class FundSubaccountSortTests(TestCase):
    def setUp(self):
        u = User.objects.create_user("fr", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(u)
        self.parent = Department.objects.create(name="Parent", fund_type="LOCAL", category="MINISTRY")
        self.low = Department.objects.create(name="Aaa Low", fund_type="LOCAL",
            category="MINISTRY", parent=self.parent)
        self.high = Department.objects.create(name="Zzz High", fund_type="LOCAL",
            category="MINISTRY", parent=self.parent)
        # 'Aaa Low' would come first alphabetically, but 'Zzz High' has more receipts
        Transaction.objects.create(date=dt.date.today(), channel="CASH", direction="CREDIT",
            amount=Decimal("100"), department=self.low, allocation_status="MANUAL", confirmed=True)
        Transaction.objects.create(date=dt.date.today(), channel="CASH", direction="CREDIT",
            amount=Decimal("9000"), department=self.high, allocation_status="MANUAL", confirmed=True)

    def test_subaccounts_sorted_by_receipts(self):
        from reports.services import balances  # noqa
        r = self.c.get(f"/reports/fund/{self.parent.id}/?start=2020-01-01&end=2030-12-31")
        b = r.content.decode()
        self.assertEqual(r.status_code, 200)
        self.assertIn("tableToPng('subAccountsTable'", b)
        # the higher-receipts sub-account appears before the lower one
        self.assertLess(b.index("Zzz High"), b.index("Aaa Low"))
