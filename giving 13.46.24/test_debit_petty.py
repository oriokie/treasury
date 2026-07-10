"""A bank debit can be allocated to petty cash, topping up the float (#2)."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from giving.models import Transaction
from cashbook.models import PettyCashTopUp


class DebitPettyCashTests(TestCase):
    def setUp(self):
        u = User.objects.create_user("dp", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(u)

    def test_allocate_to_petty_cash(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 5), channel="BANK",
            direction="DEBIT", amount=Decimal("5000"), allocation_status="REVIEW",
            confirmed=True, core_ref="DBT1", raw_narration="ATM")
        before = PettyCashTopUp.objects.count()
        self.c.post(f"/debits/{t.id}/resolve/",
                    {"kind": "petty_cash", "description": "Top up"})
        self.assertEqual(PettyCashTopUp.objects.count(), before + 1)
        tp = PettyCashTopUp.objects.latest("id")
        self.assertEqual(tp.amount, Decimal("5000"))
        t.refresh_from_db()
        self.assertEqual(t.allocation_status, "MANUAL")
