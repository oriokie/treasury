"""A reconciliation worksheet is read-only to the roles that are read-only.

`ReconciliationDetailView` is gated by `ReadAccessMixin`, which admits the
Auditor — correctly, because reading a worksheet is precisely what an auditor
is for. But the same class serves the POSTs that CHANGE one: adding and
deleting reconciling items, and overwriting the cash-book balance outright.
Nothing re-checked the role there, so the one account whose whole purpose is
independent verification could quietly alter the thing it was verifying — and,
because a balanced worksheet can auto-lock its accounting month, could close
the month over a discrepancy in the process.

The template had always hidden those controls behind `can_enter_data`, which is
exactly why this went unnoticed: the screen looked right. A hidden button is
not a permission.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import ASSISTANT, AUDITOR, TREASURER
from statements.models import (BankAccount, BankReconciliation,
                               ReconciliationItem)
from statements.models_register import RegisterException

JUL31 = dt.date(2026, 7, 31)


def _user(username, role):
    u = User.objects.create_user(username, password="pw-for-tests-only")
    u.groups.add(Group.objects.get_or_create(name=role)[0])
    return u


class ReconciliationWritesRequireDataEntry(TestCase):
    def setUp(self):
        self.treasurer = _user("recon-treasurer", TREASURER)
        self.auditor = _user("recon-auditor", AUDITOR)
        self.assistant = _user("recon-assistant", ASSISTANT)
        self.rec = BankReconciliation.objects.create(
            statement_date=JUL31, bank_balance=Decimal("100000"),
            book_balance=Decimal("100000"), created_by=self.treasurer)
        self.url = reverse("reconciliation_detail", args=[self.rec.pk])

    # --- the auditor may look, and only look ---------------------------------

    def test_an_auditor_can_still_open_the_worksheet(self):
        """The fix must not cost the auditor the read access they are for."""
        self.client.force_login(self.auditor)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_an_auditor_cannot_overwrite_the_cash_book_balance(self):
        self.client.force_login(self.auditor)
        self.client.post(self.url, {"action": "set_book",
                                    "book_balance": "999999"})
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.book_balance, Decimal("100000"))

    def test_an_auditor_cannot_add_a_reconciling_item(self):
        self.client.force_login(self.auditor)
        self.client.post(self.url, {
            "action": "add_item", "kind": ReconciliationItem.Kind.OTHER,
            "description": "fabricated", "amount": "5000",
            "effect": ReconciliationItem.Effect.ADD})
        self.assertEqual(self.rec.items.count(), 0)

    def test_an_auditor_cannot_delete_a_reconciling_item(self):
        item = ReconciliationItem.objects.create(
            reconciliation=self.rec, kind=ReconciliationItem.Kind.OTHER,
            description="real", amount=Decimal("250"),
            effect=ReconciliationItem.Effect.ADD)
        self.client.force_login(self.auditor)
        self.client.post(self.url, {"action": "delete_item",
                                    "item_id": item.pk})
        self.assertTrue(
            ReconciliationItem.objects.filter(pk=item.pk).exists())

    def test_an_auditor_cannot_recompute_the_cash_book_balance(self):
        """The quietest of the four: it takes no attacker-supplied figure, so it
        reads as harmless — but it still rewrites a stored balance, and can
        still tip a worksheet into 'reconciled' and auto-lock the month."""
        self.client.force_login(self.auditor)
        self.client.post(self.url, {"action": "recompute_book"})
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.book_balance, Decimal("100000"))

    # --- the roles that ARE meant to write, still can ------------------------

    def test_a_treasurer_may_still_set_the_cash_book_balance(self):
        self.client.force_login(self.treasurer)
        self.client.post(self.url, {"action": "set_book",
                                    "book_balance": "123456"})
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.book_balance, Decimal("123456"))

    def test_an_assistant_may_still_add_a_reconciling_item(self):
        """Data entry is Treasurer OR Assistant — the fix must not narrow the
        gate to treasurers and quietly break the assistant's day job."""
        self.client.force_login(self.assistant)
        self.client.post(self.url, {
            "action": "add_item", "kind": ReconciliationItem.Kind.OTHER,
            "description": "cash at hand", "amount": "750",
            "effect": ReconciliationItem.Effect.ADD})
        self.assertEqual(self.rec.items.count(), 1)


class BankRegisterWritesRequireDataEntry(TestCase):
    """The same hole, twice more, found by asking which OTHER read-gated views
    define a write handler. Both live on the bank register — the figures a
    reconciliation is measured against.
    """

    def setUp(self):
        self.treasurer = _user("reg-treasurer", TREASURER)
        self.auditor = _user("reg-auditor", AUDITOR)
        self.account = BankAccount.objects.create(
            name="Current", kind=BankAccount.Kind.CURRENT,
            account_number="0001", is_default=True)

    def test_an_auditor_cannot_set_the_register_opening_balance(self):
        """The running balance is measured from this figure, so rewriting it
        moves every balance the register reports."""
        self.client.force_login(self.auditor)
        self.client.post(f"/bank-register/?account={self.account.pk}",
                         {"register_opening_balance": "5000000",
                          "register_opening_date": "2026-01-01"})
        self.account.refresh_from_db()
        self.assertIsNone(self.account.register_opening_balance)

    def test_a_treasurer_may_still_set_the_register_opening_balance(self):
        self.client.force_login(self.treasurer)
        self.client.post(f"/bank-register/?account={self.account.pk}",
                         {"register_opening_balance": "12000",
                          "register_opening_date": "2026-01-01"})
        self.account.refresh_from_db()
        self.assertEqual(self.account.register_opening_balance,
                         Decimal("12000"))

    def test_an_auditor_cannot_close_a_register_exception(self):
        """Closing an unexplained bank discrepancy is a control action. The
        auditor is the one person who must not be able to make one go away."""
        exc = RegisterException.objects.create(
            account=self.account, kind=RegisterException.Kind.MISSING_IN_LEDGER,
            date=JUL31, amount=Decimal("4000"),
            detail="unexplained credit")
        self.client.force_login(self.auditor)
        self.client.post(f"/bank-register/exceptions/?account={self.account.pk}",
                         {"resolve": exc.pk, "resolution": "nothing to see"})
        exc.refresh_from_db()
        self.assertTrue(exc.is_open, "a read-only auditor closed an exception")
