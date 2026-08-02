"""Reconciliation: the closing-balance suggestion, and pending receipts.

Two reported failures:

* A reconciliation for last month left out the credits that were sitting
  unreceipted at that date, so the worksheet refused to balance by exactly the
  amount in the review queue.
* The closing balance had to be typed by hand even though the bank register
  already held the bank's own figure for that date.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from statements.models import BankReconciliation, ReconciliationItem

AS_AT = dt.date(2026, 7, 31)


def _treasurer(username="rec_tr"):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
    return u


def _entered_on(obj, *when):
    from django.utils import timezone
    latest = obj.history.order_by("-history_date", "-history_id").first()
    type(obj).history.filter(history_id=latest.history_id).update(
        history_date=timezone.make_aware(dt.datetime(*when),
                                         timezone.get_current_timezone()))


class PendingReceiptItemTests(TestCase):
    """A credit banked in July and receipted in August is money the bank had
    and the cash book did not — on 31 July."""

    def setUp(self):
        self.user = _treasurer()
        self.fund = Department.objects.create(name="Building", fund_type="LOCAL")
        self.txn = Transaction.objects.create(
            date=dt.date(2026, 7, 25), channel="BANK", direction="CREDIT",
            amount=Decimal("5000"), department=None, confirmed=True,
            allocation_status="REVIEW")
        _entered_on(self.txn, 2026, 7, 25, 10, 0)

    def _make(self):
        from statements.views import start_reconciliation
        return start_reconciliation(statement_date=AS_AT,
                                    bank_balance=Decimal("5000"),
                                    user=self.user)

    def _pending_item(self, rec):
        return rec.items.filter(description__startswith="Receipts pending").first()

    def test_pending_receipts_appear_as_a_reconciling_item(self):
        item = self._pending_item(self._make())
        self.assertIsNotNone(item)
        self.assertEqual(item.amount, Decimal("5000"))
        self.assertEqual(item.effect, ReconciliationItem.Effect.SUBTRACT)

    def test_the_worksheet_balances_with_it(self):
        rec = self._make()
        self.assertEqual(rec.adjusted_balance, Decimal("0"))
        self.assertEqual(rec.difference, Decimal("0"))
        self.assertTrue(rec.is_reconciled)

    def test_allocated_later_it_lands_in_the_cash_book(self):
        """Reconciling July in August, after the credit was given a fund.

        The credit keeps its July date, so the cash book now has it in July and
        suspense does not. That is the cash book being completed after the
        fact, which is what a cash book is for — and the money is still counted
        exactly once, which is the invariant that matters."""
        from statements.views import start_reconciliation
        self.txn.department = self.fund
        self.txn.allocation_status = "MANUAL"
        self.txn.save()
        _entered_on(self.txn, 2026, 8, 3, 9, 0)

        rec = start_reconciliation(statement_date=AS_AT,
                                   bank_balance=Decimal("5000"),
                                   user=self.user)
        pending = getattr(self._pending_item(rec), "amount", Decimal("0"))
        self.assertEqual(rec.book_balance, Decimal("5000"))
        self.assertEqual(pending, Decimal("0"))
        self.assertEqual(rec.book_balance + pending, Decimal("5000"))
        self.assertEqual(rec.difference, Decimal("0"))

    def test_nothing_pending_at_that_date_means_no_such_line(self):
        """A date before the credit arrived has nothing in suspense."""
        from statements.views import start_reconciliation
        rec = start_reconciliation(statement_date=dt.date(2026, 6, 30),
                                   bank_balance=Decimal("0"), user=self.user)
        self.assertIsNone(self._pending_item(rec))


class SuggestedBalanceTests(TestCase):
    def setUp(self):
        self.user = _treasurer("rec_sug")
        self.client.force_login(self.user)

    def test_the_form_offers_a_date_and_asks_the_register(self):
        r = self.client.get(reverse("reconciliation_new"))
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(r.context["form"].initial.get("statement_date"))

    def test_it_defaults_to_the_end_of_last_month(self):
        r = self.client.get(reverse("reconciliation_new"))
        chosen = r.context["form"].initial["statement_date"]
        today = dt.date.today()
        self.assertEqual(chosen, today.replace(day=1) - dt.timedelta(days=1))

    def test_a_requested_date_is_honoured(self):
        r = self.client.get(reverse("reconciliation_new"),
                            {"statement_date": "2026-07-31"})
        self.assertEqual(r.context["form"].initial["statement_date"], AS_AT)

    def test_no_register_balance_leaves_the_field_blank(self):
        """'If not, just blank as is' — a fabricated balance would reconcile
        against itself and hide the gap it was invented to fill."""
        r = self.client.get(reverse("reconciliation_new"))
        self.assertIsNone(r.context["form"].initial.get("bank_balance"))

    def test_it_suggests_the_register_balance_when_there_is_one(self):
        from unittest.mock import patch
        from statements.views import suggested_bank_balance
        reg = {"balance": Decimal("52000"), "as_at": AS_AT, "stale_days": 0,
               "reason": ""}
        none = {"balance": None, "as_at": None, "stale_days": None, "reason": ""}
        with patch("statements.services.register.balance_asof", return_value=reg), \
             patch("statements.services.register.live_balance_asof", return_value=none):
            balance, note = suggested_bank_balance(AS_AT)
        self.assertEqual(balance, Decimal("52000"))
        self.assertIn("31 Jul 2026", note)

    def test_a_stale_suggestion_says_how_stale(self):
        from unittest.mock import patch
        from statements.views import suggested_bank_balance
        reg = {"balance": Decimal("52000"), "as_at": dt.date(2026, 7, 10),
               "stale_days": 21, "reason": ""}
        none = {"balance": None, "as_at": None, "stale_days": None, "reason": ""}
        with patch("statements.services.register.balance_asof", return_value=reg), \
             patch("statements.services.register.live_balance_asof", return_value=none):
            _balance, note = suggested_bank_balance(AS_AT)
        self.assertIn("21 day(s) before", note)

    def test_the_lookup_endpoint_answers_for_a_date(self):
        r = self.client.get(reverse("reconciliation_balance"),
                            {"date": "2026-07-31"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("ok", r.json())

    def test_the_lookup_endpoint_shrugs_at_a_bad_date(self):
        r = self.client.get(reverse("reconciliation_balance"), {"date": "nonsense"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ok"])
