"""Advance top-up charge: a bank/M-Pesa sending charge on a top-up is the
church's own cost, booked as an expense but not added to what the holder must
account for; reversing a top-up removes its linked charge too."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from cashbook.models import StaffAdvance, AdvanceTopUp, Expense
from ledger.services.posting import ensure_chart


def _tr():
    u = User.objects.create_user("tr_tuc", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class TopUpChargeTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.tr = _tr()
        self.d = Department.objects.create(name="TUC", fund_type="LOCAL",
            category="MINISTRY")
        self.adv = StaffAdvance.objects.create(staff_name="Charge Test",
            department=self.d, amount=Decimal("3000"), date_issued=dt.date(2026, 6, 1),
            purpose="x", method="MPESA", from_petty_cash=False,
            issued_by=self.tr, status="ISSUED")
        self.c = Client(); self.c.force_login(self.tr)

    def test_topup_with_charge(self):
        self.c.post(f"/advances/{self.adv.id}/topup/",
            {"date": "2026-06-05", "amount": "2000", "charge": "30", "note": "more travel"})
        self.adv.refresh_from_db()
        tu = self.adv.topups.first()
        self.assertEqual(self.adv.amount, Decimal("5000"))
        self.assertEqual(tu.charge, Decimal("30"))
        self.assertIsNotNone(tu.charge_expense)
        self.assertEqual(tu.charge_expense.amount, Decimal("30"))
        self.assertEqual(tu.charge_expense.category, "BANK_CHARGE")
        # the charge doesn't reduce what the holder must account for
        self.assertIsNone(tu.charge_expense.advance_id)

    def test_topup_without_charge_no_expense(self):
        self.c.post(f"/advances/{self.adv.id}/topup/",
            {"date": "2026-06-05", "amount": "1000"})
        tu = self.adv.topups.first()
        self.assertEqual(tu.charge, Decimal("0"))
        self.assertIsNone(tu.charge_expense)

    def test_reversing_topup_removes_charge_expense(self):
        self.c.post(f"/advances/{self.adv.id}/topup/",
            {"date": "2026-06-05", "amount": "2000", "charge": "30"})
        tu = self.adv.topups.first()
        exp_id = tu.charge_expense.id
        self.c.post(f"/advances/{self.adv.id}/topup/{tu.id}/reverse/")
        self.assertFalse(Expense.objects.filter(id=exp_id).exists())
        self.adv.refresh_from_db()
        self.assertEqual(self.adv.amount, Decimal("3000"))

    def test_charge_shown_in_statement(self):
        self.c.post(f"/advances/{self.adv.id}/topup/",
            {"date": "2026-06-05", "amount": "2000", "charge": "30"})
        b = self.c.get(f"/advances/{self.adv.id}/").content.decode()
        self.assertIn("sending charge", b)
