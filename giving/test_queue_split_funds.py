"""The queue's manual split may target a split fund, which sub-divides that part
across its components (#9)."""
from decimal import Decimal
import datetime as dt

from django.test import TestCase, Client
from django.contrib.auth.models import User, Group

from departments.models import Department
from giving.models import Transaction, SplitFund, SplitComponent


class QueueSplitFundsTests(TestCase):
    def setUp(self):
        u = User.objects.create_user("qsf", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(u)
        self.enf = Department.objects.create(name="ENF", fund_type="TRUST", category="TRUST")
        self.lcb = Department.objects.create(name="LCB", fund_type="LOCAL", category="OFFERING")
        self.other = Department.objects.create(name="Tithe", fund_type="TRUST",
                                               category="TRUST", selectable=True)
        self.sf = SplitFund.objects.create(name="Combined")
        SplitComponent.objects.create(split_fund=self.sf, department=self.enf, percent=Decimal("50"))
        SplitComponent.objects.create(split_fund=self.sf, department=self.lcb, percent=Decimal("50"))
        self.rev = Transaction.objects.create(date=dt.date(2026, 6, 2), channel="BANK",
            direction="CREDIT", amount=Decimal("1000"), allocation_status="REVIEW",
            confirmed=True, core_ref="REV", reference="mix")

    def test_split_with_split_fund_row_expands(self):
        self.c.post(f"/queue/{self.rev.id}/claim/", {
            "split": "1",
            "split_dept": [f"sf:{self.sf.id}", str(self.other.id)],
            "split_amount": ["600", "400"], "split_grp": ["", ""]})
        parts = Transaction.objects.filter(reference="mix")
        self.assertEqual(parts.count(), 3)
        self.assertEqual(parts.filter(department=self.enf).first().amount, Decimal("300"))
        self.assertEqual(parts.filter(department=self.lcb).first().amount, Decimal("300"))
        self.assertEqual(parts.filter(department=self.other).first().amount, Decimal("400"))
        self.assertEqual(sum(p.amount for p in parts), Decimal("1000"))
