"""Excess petty cash deposited to the bank reduces the float (#queue petty→bank)."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import User, Group
from django.test import TestCase, Client

from cashbook.models import PettyCashTopUp, PettyCashBankDeposit
from cashbook.services.treasury_position import petty_balance_asof
from giving.models import Transaction


class PettyCashBankDepositTests(TestCase):
    def setUp(self):
        u = User.objects.create_user("pcb", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.user = u
        self.c = Client()
        self.c.force_login(u)
        # float starts at 10,000
        PettyCashTopUp.objects.create(
            date=dt.date(2026, 6, 1), amount=Decimal("10000"),
            note="Opening float", recorded_by=u)

    def test_balance_subtracts_bank_deposit(self):
        PettyCashBankDeposit.objects.create(
            date=dt.date(2026, 6, 10), amount=Decimal("2500"),
            note="Excess to bank", recorded_by=self.user)
        self.assertEqual(petty_balance_asof(dt.date(2026, 6, 10)),
                         Decimal("7500"))

    def test_queue_petty_to_bank_reduces_float(self):
        t = Transaction.objects.create(
            date=dt.date(2026, 6, 12), channel="BANK", direction="CREDIT",
            amount=Decimal("3000"), allocation_status="REVIEW",
            confirmed=True, core_ref="PCDEP1",
            raw_narration="CASH DEPOSIT PETTY")
        before = petty_balance_asof(t.date)
        self.assertEqual(before, Decimal("10000"))

        r = self.c.post(f"/queue/{t.id}/claim/", {"kind": "petty_to_bank"})
        self.assertEqual(r.status_code, 302)

        t.refresh_from_db()
        self.assertEqual(t.allocation_status, "MANUAL")
        self.assertTrue(t.excluded_from_income)
        self.assertIsNone(t.department_id)

        dep = PettyCashBankDeposit.objects.get(bank_transaction=t)
        self.assertEqual(dep.amount, Decimal("3000"))
        self.assertEqual(petty_balance_asof(t.date), Decimal("7000"))

    def test_queue_rejects_when_float_insufficient(self):
        t = Transaction.objects.create(
            date=dt.date(2026, 6, 12), channel="BANK", direction="CREDIT",
            amount=Decimal("15000"), allocation_status="REVIEW",
            confirmed=True, core_ref="PCDEP2")
        self.c.post(f"/queue/{t.id}/claim/", {"kind": "petty_to_bank"})
        t.refresh_from_db()
        self.assertEqual(t.allocation_status, "REVIEW")  # still queued
        self.assertEqual(PettyCashBankDeposit.objects.count(), 0)
        self.assertEqual(petty_balance_asof(t.date), Decimal("10000"))

    def test_register_lists_bank_deposit_as_outflow(self):
        PettyCashBankDeposit.objects.create(
            date=dt.date(2026, 6, 15), amount=Decimal("1000"),
            note="Surplus notes", recorded_by=self.user)
        r = self.c.get("/petty-cash/?start=2026-06-01&end=2026-06-30")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Deposited to bank")
        self.assertContains(r, "Surplus notes")
