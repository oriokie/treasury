"""Money banked on Friday, receipted on Sabbath.

Two receipting routes reach the same place by different roads, and a
reconciliation for the Friday has to be right for both:

  A. ALLOCATED — the bank credit itself is given a fund on the Saturday.
  B. MANUALLY RECEIPTED — the bank credit is marked as receipted on paper, which
     detaches it from any fund and makes it a memo, and the envelope carrying
     the income and the fund is entered separately, dated the Sabbath.

In both, on Friday night the money is at the bank and in no fund. A Friday
reconciliation must say so — whenever it is prepared.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from statements.views import start_reconciliation

FRI = dt.date(2026, 7, 31)      # money hits the bank
SAT = dt.date(2026, 8, 1)       # Sabbath: receipting happens


def _entered_on(obj, *when):
    from django.utils import timezone
    latest = obj.history.order_by("-history_date", "-history_id").first()
    type(obj).history.filter(history_id=latest.history_id).update(
        history_date=timezone.make_aware(dt.datetime(*when),
                                         timezone.get_current_timezone()))


class _Friday(TestCase):
    def setUp(self):
        u = User.objects.create_user("sab", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.user = u
        self.tithe = Department.objects.create(name="Tithe", fund_type="TRUST")
        self.bank = Transaction.objects.create(
            date=FRI, channel="BANK", direction="CREDIT",
            amount=Decimal("100"), department=None, confirmed=True,
            allocation_status="REVIEW")
        _entered_on(self.bank, 2026, 7, 31, 16, 0)

    def _worksheet(self, on=FRI):
        """A reconciliation: the bank says 100. Built through the same call the
        create view makes, so a passing test means the real path works."""
        return start_reconciliation(statement_date=on,
                                    bank_balance=Decimal("100"),
                                    user=self.user)

    def _report(self, rec):
        return (f"\n\n  bank {rec.bank_balance}, book {rec.book_balance}, "
                f"adjustments {rec.adjustments} -> adjusted "
                f"{rec.adjusted_balance}, difference {rec.difference}\n"
                f"  items: {[(i.description, str(i.amount), i.effect) for i in rec.items.all()]}\n")


class RouteA_Allocated(_Friday):
    """The bank credit is given a fund on the Sabbath."""

    def _receipt_on_sabbath(self):
        self.bank.department = self.tithe
        self.bank.allocation_status = "MANUAL"
        self.bank.save()
        _entered_on(self.bank, 2026, 8, 1, 9, 0)

    def test_prepared_on_friday(self):
        rec = self._worksheet()
        self.assertEqual(rec.difference, Decimal("0"), self._report(rec))

    def test_prepared_after_the_sabbath(self):
        self._receipt_on_sabbath()
        rec = self._worksheet()
        self.assertEqual(rec.difference, Decimal("0"), self._report(rec))


class RouteB_ManuallyReceipted(_Friday):
    """The Sabbath route the treasurer actually uses: mark the bank line as
    receipted on paper, then key the envelope in, dated the Sabbath."""

    def _receipt_on_sabbath(self):
        # marking detaches the bank row from any fund and excludes it from
        # income — it becomes a memo for money whose receipt lives elsewhere
        self.bank.mark_manual_receipt(True)
        self.bank.refresh_from_db()
        _entered_on(self.bank, 2026, 8, 1, 9, 0)
        # the envelope carrying the income and the fund, dated the Sabbath
        env = Transaction.objects.create(
            date=SAT, channel="ENVELOPE", direction="CREDIT",
            amount=Decimal("100"), department=self.tithe, confirmed=True,
            allocation_status="MANUAL")
        _entered_on(env, 2026, 8, 1, 9, 5)
        return env

    def test_the_marking_really_does_detach_the_bank_row(self):
        """The premise these tests rest on."""
        self._receipt_on_sabbath()
        self.bank.refresh_from_db()
        self.assertTrue(self.bank.manual_receipt)
        self.assertTrue(self.bank.excluded_from_income)
        self.assertIsNone(self.bank.department_id)

    def test_prepared_on_friday(self):
        rec = self._worksheet()
        self.assertEqual(rec.difference, Decimal("0"), self._report(rec))

    def test_prepared_after_the_sabbath(self):
        """The reported case. On Friday night the money was at the bank and in
        no fund; the envelope that gives it a fund is dated Saturday."""
        self._receipt_on_sabbath()
        rec = self._worksheet()
        self.assertEqual(rec.difference, Decimal("0"), self._report(rec))

    def test_the_money_is_counted_exactly_once(self):
        self._receipt_on_sabbath()
        rec = self._worksheet()
        pending = sum((i.amount for i in rec.items.all()
                       if i.description.startswith("Receipts pending")),
                      Decimal(0))
        self.assertEqual(rec.book_balance + pending, Decimal("100"),
                         self._report(rec))


class SaturdayIsRight(_Friday):
    """A reconciliation for the Sabbath itself, after receipting, needs no
    suspense line at all — by then the fund has the money."""

    def test_saturday_worksheet_has_no_suspense(self):
        self.bank.mark_manual_receipt(True)
        _entered_on(self.bank, 2026, 8, 1, 9, 0)
        env = Transaction.objects.create(
            date=SAT, channel="ENVELOPE", direction="CREDIT",
            amount=Decimal("100"), department=self.tithe, confirmed=True,
            allocation_status="MANUAL")
        _entered_on(env, 2026, 8, 1, 9, 5)

        rec = self._worksheet(on=SAT)
        pending = sum((i.amount for i in rec.items.all()
                       if i.description.startswith("Receipts pending")),
                      Decimal(0))
        self.assertEqual(pending, Decimal("0"))
        self.assertEqual(rec.book_balance, Decimal("100"))
        self.assertEqual(rec.difference, Decimal("0"))
