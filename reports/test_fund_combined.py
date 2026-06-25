"""Fund report top cards include sub-accounts (#1)."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from giving.models import Transaction


class FundCombinedCardsTests(TestCase):
    def setUp(self):
        u = User.objects.create_user("fc", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(u)
        self.parent = Department.objects.create(name="Camp Parent", fund_type="LOCAL",
            category="MINISTRY", opening_balance=Decimal("1000"))
        self.sub = Department.objects.create(name="Camp Sub", fund_type="LOCAL",
            category="MINISTRY", parent=self.parent, opening_balance=Decimal("500"))
        Transaction.objects.create(date=dt.date(2026, 6, 1), channel="CASH",
            direction="CREDIT", amount=Decimal("2000"), department=self.parent,
            allocation_status="MANUAL", confirmed=True)
        Transaction.objects.create(date=dt.date(2026, 6, 1), channel="CASH",
            direction="CREDIT", amount=Decimal("3000"), department=self.sub,
            allocation_status="MANUAL", confirmed=True)

    def test_cards_include_subaccounts(self):
        b = self.c.get(f"/reports/fund/{self.parent.id}/?start=2026-01-01&end=2026-12-31").content.decode()
        self.assertIn("incl. sub-accounts", b)
        self.assertIn("1,500.00", b)   # combined opening 1000+500
        self.assertIn("5,000.00", b)   # combined receipts 2000+3000
        self.assertIn("6,500.00", b)   # combined closing 1500+5000
