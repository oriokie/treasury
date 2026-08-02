"""The reconciliation must balance whenever it is prepared.

Tithe banked on 31 July, receipted on 1 August. A reconciliation for 31 July is
right if it is prepared on 31 July. Prepared on 1 August — for the same date —
it must still be right, and for the same reason: the bank's side and the cash
book's side have to be read from the same moment as each other.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from statements.models import BankReconciliation
from statements.views import _sync_managed_recon_items

JUL31 = dt.date(2026, 7, 31)


def _entered_on(obj, *when):
    from django.utils import timezone
    latest = obj.history.order_by("-history_date", "-history_id").first()
    type(obj).history.filter(history_id=latest.history_id).update(
        history_date=timezone.make_aware(dt.datetime(*when),
                                         timezone.get_current_timezone()))


class ReconciliationBasisTests(TestCase):
    def setUp(self):
        u = User.objects.create_user("basis", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.user = u
        self.tithe = Department.objects.create(name="Tithe", fund_type="TRUST")
        # banked on 31 July, unallocated at that moment
        self.txn = Transaction.objects.create(
            date=JUL31, channel="BANK", direction="CREDIT",
            amount=Decimal("100"), department=None, confirmed=True,
            allocation_status="REVIEW")
        _entered_on(self.txn, 2026, 7, 31, 16, 0)

    def _receipt_on_1_august(self):
        self.txn.department = self.tithe
        self.txn.allocation_status = "MANUAL"
        self.txn.save()
        _entered_on(self.txn, 2026, 8, 1, 9, 0)

    def _prepare(self):
        """A worksheet for 31 July, built through the production path."""
        from statements.views import start_reconciliation
        return start_reconciliation(statement_date=JUL31,
                                    bank_balance=Decimal("100"),
                                    user=self.user)

    def test_prepared_on_the_day_it_balances(self):
        rec = self._prepare()
        self.assertEqual(rec.book_balance, Decimal("0"))
        self.assertEqual(rec.difference, Decimal("0"), "same-day recon")

    def test_prepared_the_next_day_for_the_same_date_it_still_balances(self):
        """The reported case. Receipting on 1 August must not open a gap in the
        31 July worksheet."""
        self._receipt_on_1_august()
        rec = self._prepare()
        self.assertEqual(
            rec.difference, Decimal("0"),
            f"\\n\\n  bank {rec.bank_balance}, book {rec.book_balance}, "
            f"adjustments {rec.adjustments} -> adjusted {rec.adjusted_balance}\\n"
            f"  items: {[(i.description, str(i.amount), i.effect) for i in rec.items.all()]}\\n")

    def test_prepared_much_later_it_still_balances(self):
        self._receipt_on_1_august()
        self.txn.refresh_from_db()
        rec = self._prepare()
        self.assertEqual(rec.difference, Decimal("0"))

    def test_the_two_sides_are_read_from_the_same_moment(self):
        """Whichever basis is used, suspense and the cash book must agree about
        where the money is. Counting it in neither (or in both) is the bug."""
        self._receipt_on_1_august()
        rec = self._prepare()
        pending = sum((i.amount for i in rec.items.all()
                       if i.description.startswith("Receipts pending")),
                      Decimal(0))
        # the 100 is either in the book balance or in suspense — never both,
        # never neither
        self.assertEqual(rec.book_balance + pending, Decimal("100"))

    def test_an_item_still_unallocated_is_carried_as_suspense(self):
        """The original complaint: a month with anything in the review queue
        could not balance at all."""
        rec = self._prepare()
        pending = rec.items.filter(description__startswith="Receipts pending").first()
        self.assertIsNotNone(pending)
        self.assertEqual(pending.amount, Decimal("100"))
        self.assertEqual(rec.difference, Decimal("0"))
