"""Negative petty float on reconciliation = refund due (personal funds used)."""
import datetime as dt
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.roles import TREASURER
from statements.models import BankReconciliation, ReconciliationItem
from statements.views import _sync_managed_recon_items


class NegativePettyRefundReconTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("petty_ref", password="x")
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.rec = BankReconciliation.objects.create(
            statement_date=dt.date(2026, 6, 30),
            bank_balance=Decimal("10000"),
            book_balance=Decimal("10000"),
            created_by=self.user)

    @patch("cashbook.views._petty_balance_asof", return_value=Decimal("-1500"))
    @patch("cashbook.views.outstanding_bank_advances_total", return_value=Decimal("0"))
    @patch("cashbook.views.outstanding_petty_advances_total", return_value=Decimal("0"))
    @patch("cashbook.views.unpresented_cheques_total", return_value=Decimal("0"))
    def test_negative_petty_becomes_refund_due_line(self, *_mocks):
        _sync_managed_recon_items(self.rec)
        items = list(self.rec.items.all())
        refund = [i for i in items if "refund due" in (i.description or "").lower()]
        self.assertEqual(len(refund), 1)
        self.assertEqual(refund[0].amount, Decimal("1500"))
        self.assertEqual(refund[0].effect, ReconciliationItem.Effect.SUBTRACT)
        self.assertFalse(self.rec.items.filter(
            description="Petty cash float (cash on hand)").exists())

    @patch("cashbook.views._petty_balance_asof", return_value=Decimal("2000"))
    @patch("cashbook.views.outstanding_bank_advances_total", return_value=Decimal("0"))
    @patch("cashbook.views.outstanding_petty_advances_total", return_value=Decimal("0"))
    @patch("cashbook.views.unpresented_cheques_total", return_value=Decimal("0"))
    def test_positive_petty_is_cash_on_hand(self, *_mocks):
        _sync_managed_recon_items(self.rec)
        float_line = self.rec.items.get(description="Petty cash float (cash on hand)")
        self.assertEqual(float_line.amount, Decimal("2000"))
        self.assertEqual(float_line.effect, ReconciliationItem.Effect.ADD)
