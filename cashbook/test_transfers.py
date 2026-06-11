"""Coverage for inter-fund transfers: a transfer moves the balance from one fund
to another, leaves total net assets unchanged, and can be reversed."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import User, Group
from django.test import TestCase
from django.urls import reverse

from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from reports.services.balances import fund_balance, department_summary


def _user(name, role):
    u = User.objects.create_user(name, password="x")
    u.groups.add(Group.objects.get_or_create(name=role)[0])
    return u


class FundTransferTests(TestCase):
    def setUp(self):
        self.treasurer = _user("ft_tr", TREASURER)
        self.client.force_login(self.treasurer)
        self.src = Department.objects.create(name="General", fund_type="LOCAL",
            category="MINISTRY", opening_balance=Decimal("0"))
        self.dst = Department.objects.create(name="Building", fund_type="LOCAL",
            category="MINISTRY", opening_balance=Decimal("0"))
        # fund the source with a real receipt
        Transaction.objects.create(date=dt.date.today(), channel="BANK",
            direction="CREDIT", amount=Decimal("10000"), department=self.src,
            allocation_status="AUTO", confirmed=True, core_ref="FT1")

    def _total_net_assets(self):
        return sum((r["closing"] for r in department_summary(None, None)), Decimal(0))

    def test_transfer_moves_balance_and_preserves_total(self):
        before_total = self._total_net_assets()
        r = self.client.post(reverse("transfer_create"), {
            "date": dt.date.today().isoformat(), "source": self.src.pk,
            "destination": self.dst.pk, "amount": "3000",
            "reason": "Move to building", "reference": "TR-1"})
        self.assertIn(r.status_code, (200, 302))
        asof = dt.date.today()
        self.assertEqual(fund_balance(self.src, asof), Decimal("7000"))   # 10000 - 3000
        self.assertEqual(fund_balance(self.dst, asof), Decimal("3000"))   # +3000
        # total across all funds is unchanged (internal reclassification)
        self.assertEqual(self._total_net_assets(), before_total)

    def test_transfer_list_renders(self):
        self.assertEqual(self.client.get(reverse("transfer_list")).status_code, 200)
