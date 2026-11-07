"""Petty cash is a cash location: bank deposits and deleting erroneous float
entries must not charge a fund, and linked rows stay protected."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from cashbook.models import PettyCashBankDeposit, PettyCashTopUp
from departments.models import Department


class PettyCashBankDepositUITests(TestCase):
    def setUp(self):
        self.tr = User.objects.create_user("pc_tr", password="x", is_staff=True)
        self.tr.is_superuser = True
        self.tr.save()
        Department.objects.create(name="Local", fund_type="LOCAL", category="MINISTRY")
        self.client.force_login(self.tr)
        PettyCashTopUp.objects.create(
            date=dt.date(2026, 6, 1), amount=Decimal("5000.00"),
            note="seed float", recorded_by=self.tr)

    def test_bank_deposit_reduces_float_without_a_fund(self):
        r = self.client.post(reverse("petty_cash_bank_deposit"), {
            "date": "2026-06-10", "amount": "1500.00", "note": "Excess banked",
        })
        self.assertEqual(r.status_code, 302)
        dep = PettyCashBankDeposit.objects.get()
        self.assertEqual(dep.amount, Decimal("1500.00"))
        self.assertIsNone(dep.bank_transaction_id)
        page = self.client.get(reverse("petty_cash"), {
            "start": "2026-06-01", "end": "2026-06-30"})
        self.assertContains(page, "Deposited to bank")
        self.assertContains(page, "3,500.00")  # 5000 - 1500

    def test_bank_deposit_rejects_more_than_float(self):
        before = PettyCashBankDeposit.objects.count()
        r = self.client.post(reverse("petty_cash_bank_deposit"), {
            "date": "2026-06-10", "amount": "9000.00", "note": "",
        })
        self.assertEqual(r.status_code, 302)
        self.assertEqual(PettyCashBankDeposit.objects.count(), before)

    def test_manual_topup_can_be_deleted(self):
        top = PettyCashTopUp.objects.get()
        r = self.client.post(reverse("petty_cash_topup_delete", args=[top.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertFalse(PettyCashTopUp.objects.filter(pk=top.pk).exists())

    def test_manual_bank_deposit_can_be_deleted(self):
        dep = PettyCashBankDeposit.objects.create(
            date=dt.date(2026, 6, 10), amount=Decimal("500.00"),
            note="typo", recorded_by=self.tr)
        r = self.client.post(reverse("petty_cash_bank_deposit_delete", args=[dep.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertFalse(PettyCashBankDeposit.objects.filter(pk=dep.pk).exists())

    def test_linked_bank_deposit_cannot_be_deleted(self):
        from giving.models import Transaction
        txn = Transaction.objects.create(
            date=dt.date(2026, 6, 10), amount=Decimal("500.00"),
            direction=Transaction.Direction.CREDIT, channel=Transaction.Channel.BANK,
            allocation_status=Transaction.Status.MANUAL, confirmed=True,
            raw_narration="cash deposit")
        dep = PettyCashBankDeposit.objects.create(
            date=dt.date(2026, 6, 10), amount=Decimal("500.00"),
            note="linked", recorded_by=self.tr, bank_transaction=txn)
        r = self.client.post(reverse("petty_cash_bank_deposit_delete", args=[dep.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertTrue(PettyCashBankDeposit.objects.filter(pk=dep.pk).exists())
