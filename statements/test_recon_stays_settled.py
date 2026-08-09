"""A worksheet that balanced must still balance the next time it is opened.

Opening a reconciliation is not a passive act: `ReconciliationDetailView.get`
re-syncs the auto-managed reconciling items for anyone who can enter data. One
of those items — "Receipts pending allocation" — is not a correction to the
bank's side of the sheet at all. It is the cash book's other half: money at the
bank on the statement date and in no fund on the statement date, carried in
suspense precisely so the sheet can balance. `reconciliation_basis` states the
invariant the 3.41.x releases converged on — the money is in the cash book or
in suspense, never both, never neither.

The sync re-read the suspense half from the books as they now stand and left
the stored cash-book half exactly as it was written on the day. So: 40,000
arrives on 31 July unallocated; July's worksheet balances at nil with the
40,000 in suspense; the gift is allocated in August; the treasurer opens July's
sheet to file it and the GET deletes the 40,000 line while the July cash-book
figure stays behind it. A settled reconciliation un-settles itself because
somebody looked at it — and with auto-lock on, July was closed on the strength
of the figure the app then withdrew.

These tests pin the rule at its own level: the sync moves both halves of that
money or neither, and it leaves alone the figures a treasurer typed by hand.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.roles import TREASURER
from departments.models import Department, current_cash_position
from giving.models import Transaction
from statements.models import BankReconciliation, ReconciliationItem
from statements.views import (PENDING_RECEIPTS_ITEM, _sync_managed_recon_items,
                              start_reconciliation)

JUL31 = dt.date(2026, 7, 31)


def _entered_on(obj, *when):
    """Backdate the row's latest history entry — simple_history stamps the wall
    clock, so a fixture built today looks to history as though it was, and
    `balances.receipted_after` reads that history to decide whether a credit had
    been receipted yet on the statement date."""
    from django.utils import timezone
    latest = obj.history.order_by("-history_date", "-history_id").first()
    type(obj).history.filter(history_id=latest.history_id).update(
        history_date=timezone.make_aware(dt.datetime(*when),
                                         timezone.get_current_timezone()))


class SettledWorksheetSurvivesBeingOpenedAgain(TestCase):
    """The reported story, at the level of the sync that causes it."""

    def setUp(self):
        self.user = User.objects.create_user("recon_settled", password="x")
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(name="Building", fund_type="LOCAL")
        # three weeks of ordinary July giving, already in a fund: 215,000
        early = Transaction.objects.create(
            date=dt.date(2026, 7, 3), channel="BANK", direction="CREDIT",
            amount=Decimal("215000"), department=self.fund, confirmed=True,
            allocation_status="AUTO")
        _entered_on(early, 2026, 7, 3, 10, 0)
        # and 40,000 on the 31st that nobody recognised, so it is in no fund
        self.gift = Transaction.objects.create(
            date=JUL31, channel="BANK", direction="CREDIT",
            amount=Decimal("40000"), department=None, confirmed=True,
            allocation_status="REVIEW")
        _entered_on(self.gift, 2026, 7, 31, 16, 0)

    def _prepare(self, book_balance=None):
        """July's worksheet, through the call the create view makes."""
        return start_reconciliation(statement_date=JUL31,
                                    bank_balance=Decimal("255000"),
                                    book_balance=book_balance, user=self.user)

    def _allocate_the_gift(self):
        self.gift.department = self.fund
        self.gift.allocation_status = "MANUAL"
        self.gift.save()
        _entered_on(self.gift, 2026, 8, 3, 9, 0)

    def _suspense(self, rec):
        it = rec.items.filter(description=PENDING_RECEIPTS_ITEM).first()
        return it.amount if it else Decimal(0)

    def test_it_balances_on_the_day_with_the_gift_in_suspense(self):
        """The starting position every later test moves away from."""
        rec = self._prepare()
        self.assertEqual(rec.book_balance, Decimal("215000"))
        self.assertEqual(self._suspense(rec), Decimal("40000"))
        self.assertEqual(rec.difference, Decimal("0"))
        self.assertTrue(rec.is_reconciled)

    def test_allocating_the_gift_and_re_syncing_leaves_it_settled(self):
        """The defect. Nothing was done TO the worksheet — it was opened."""
        rec = self._prepare()
        self._allocate_the_gift()

        _sync_managed_recon_items(rec)
        rec.refresh_from_db()

        self.assertEqual(self._suspense(rec), Decimal("0"),
                         "the gift is in a fund now, so nothing is pending")
        self.assertEqual(
            rec.difference, Decimal("0"),
            f"July balanced at nil and now differs by {rec.difference}: bank "
            f"{rec.bank_balance}, book {rec.book_balance}, adjustments "
            f"{rec.adjustments}")
        self.assertTrue(rec.is_reconciled)

    def test_the_two_halves_of_that_money_move_together(self):
        """The invariant itself, rather than one of its consequences: cash book
        plus suspense is the same total before and after the allocation."""
        rec = self._prepare()
        self.assertEqual(rec.book_balance + self._suspense(rec),
                         Decimal("255000"))
        self._allocate_the_gift()
        _sync_managed_recon_items(rec)
        rec.refresh_from_db()
        self.assertEqual(rec.book_balance + self._suspense(rec),
                         Decimal("255000"))

    def test_the_refreshed_figure_is_the_one_every_other_page_shows(self):
        """It is refreshed FROM THE LEDGER, not nudged by the delta. A sheet
        that balances but disagrees with the dashboard is 3.41.2 again."""
        rec = self._prepare()
        self._allocate_the_gift()
        _sync_managed_recon_items(rec)
        rec.refresh_from_db()
        self.assertEqual(rec.book_balance, current_cash_position(JUL31))

    def test_a_partial_allocation_moves_only_what_was_allocated(self):
        """Half the queue cleared: suspense falls by that half and the cash
        book rises by it, and the sheet still balances."""
        other = Transaction.objects.create(
            date=dt.date(2026, 7, 28), channel="BANK", direction="CREDIT",
            amount=Decimal("10000"), department=None, confirmed=True,
            allocation_status="REVIEW")
        _entered_on(other, 2026, 7, 28, 12, 0)
        rec = start_reconciliation(statement_date=JUL31,
                                   bank_balance=Decimal("265000"),
                                   user=self.user)
        self.assertEqual(self._suspense(rec), Decimal("50000"))
        self.assertEqual(rec.difference, Decimal("0"))

        self._allocate_the_gift()                 # the 40,000 only
        _sync_managed_recon_items(rec)
        rec.refresh_from_db()

        self.assertEqual(self._suspense(rec), Decimal("10000"))
        self.assertEqual(rec.book_balance, Decimal("255000"))
        self.assertEqual(rec.difference, Decimal("0"))

    def test_the_sync_reports_the_figure_it_moved(self):
        """The detail view turns this into a message. A stored balance rewritten
        during a page view with nothing on screen to show for it is how this
        went wrong in the first place."""
        rec = self._prepare()
        self.assertIsNone(_sync_managed_recon_items(rec),
                          "nothing moved, so there is nothing to announce")
        self._allocate_the_gift()
        self.assertEqual(_sync_managed_recon_items(rec), Decimal("255000"))


class TheSyncLeavesHandTypedFiguresAlone(TestCase):
    """The other half of the choice: refreshing the cash-book balance on every
    page view would balance the sheet and destroy "Update book balance", which
    exists so a treasurer can hold a figure the ledger does not yet know about.
    The refresh happens only when the sync itself moves the suspense line."""

    def setUp(self):
        self.user = User.objects.create_user("recon_manual", password="x")
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(name="Welfare", fund_type="LOCAL")
        t = Transaction.objects.create(
            date=dt.date(2026, 7, 4), channel="BANK", direction="CREDIT",
            amount=Decimal("100000"), department=self.fund, confirmed=True,
            allocation_status="AUTO")
        _entered_on(t, 2026, 7, 4, 10, 0)

    def test_a_typed_balance_survives_a_sync_that_moves_no_suspense(self):
        rec = BankReconciliation.objects.create(
            statement_date=JUL31, bank_balance=Decimal("100000"),
            book_balance=Decimal("93750"), created_by=self.user)
        _sync_managed_recon_items(rec)
        rec.refresh_from_db()
        self.assertEqual(rec.book_balance, Decimal("93750"))

    def test_a_typed_balance_survives_the_worksheet_being_created(self):
        """`start_reconciliation` syncs immediately, and the sync CREATES the
        suspense line. A first appearance must not count as the line moving, or
        the figure the treasurer typed into the new-worksheet form is gone a
        millisecond after they typed it."""
        pending = Transaction.objects.create(
            date=dt.date(2026, 7, 20), channel="BANK", direction="CREDIT",
            amount=Decimal("6000"), department=None, confirmed=True,
            allocation_status="REVIEW")
        _entered_on(pending, 2026, 7, 20, 11, 0)
        rec = start_reconciliation(statement_date=JUL31,
                                   bank_balance=Decimal("106000"),
                                   book_balance=Decimal("88888"),
                                   user=self.user)
        rec.refresh_from_db()
        self.assertEqual(rec.book_balance, Decimal("88888"))
        self.assertEqual(
            rec.items.filter(description=PENDING_RECEIPTS_ITEM).first().amount,
            Decimal("6000"))

    def test_a_credit_appearing_late_moves_the_bank_side_only(self):
        """A statement line imported after the worksheet was prepared: the books
        never held that money, so the cash-book balance must not move. The sheet
        was out by 6,000 and the new suspense line is what explains it."""
        rec = start_reconciliation(statement_date=JUL31,
                                   bank_balance=Decimal("106000"),
                                   user=self.user)
        self.assertEqual(rec.difference, Decimal("6000"))
        late = Transaction.objects.create(
            date=dt.date(2026, 7, 20), channel="BANK", direction="CREDIT",
            amount=Decimal("6000"), department=None, confirmed=True,
            allocation_status="REVIEW")
        _entered_on(late, 2026, 7, 20, 11, 0)

        _sync_managed_recon_items(rec)
        rec.refresh_from_db()

        self.assertEqual(rec.book_balance, Decimal("100000"))
        self.assertEqual(rec.difference, Decimal("0"))

    def test_a_worksheet_with_no_cash_book_side_is_not_given_one(self):
        """book_balance is optional, and a worksheet left open has no difference
        at all. Filling it in from a page view would declare it reconciled."""
        pending = Transaction.objects.create(
            date=dt.date(2026, 7, 20), channel="BANK", direction="CREDIT",
            amount=Decimal("6000"), department=None, confirmed=True,
            allocation_status="REVIEW")
        _entered_on(pending, 2026, 7, 20, 11, 0)
        rec = BankReconciliation.objects.create(
            statement_date=JUL31, bank_balance=Decimal("106000"),
            book_balance=None, created_by=self.user)
        _sync_managed_recon_items(rec)          # creates the suspense line
        pending.department = self.fund
        pending.allocation_status = "MANUAL"
        pending.save()
        _entered_on(pending, 2026, 8, 3, 9, 0)

        _sync_managed_recon_items(rec)          # removes it again
        rec.refresh_from_db()

        self.assertIsNone(rec.book_balance)
        self.assertIsNone(rec.difference)


class OpeningTheWorksheetInTheBrowser(TestCase):
    """Through the page, because the GET is what triggers the sync — the
    treasurer's only action in the reported story was to look."""

    def setUp(self):
        self.user = User.objects.create_user("recon_page", password="x")
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.client.force_login(self.user)
        self.fund = Department.objects.create(name="Youth", fund_type="LOCAL")
        early = Transaction.objects.create(
            date=dt.date(2026, 7, 3), channel="BANK", direction="CREDIT",
            amount=Decimal("215000"), department=self.fund, confirmed=True,
            allocation_status="AUTO")
        _entered_on(early, 2026, 7, 3, 10, 0)
        self.gift = Transaction.objects.create(
            date=JUL31, channel="BANK", direction="CREDIT",
            amount=Decimal("40000"), department=None, confirmed=True,
            allocation_status="REVIEW")
        _entered_on(self.gift, 2026, 7, 31, 16, 0)
        self.rec = start_reconciliation(statement_date=JUL31,
                                        bank_balance=Decimal("255000"),
                                        user=self.user)

    def test_re_opening_july_in_august_leaves_it_reconciled(self):
        self.gift.department = self.fund
        self.gift.allocation_status = "MANUAL"
        self.gift.save()
        _entered_on(self.gift, 2026, 8, 3, 9, 0)

        r = self.client.get(f"/reconciliations/{self.rec.pk}/")
        self.assertEqual(r.status_code, 200)
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.difference, Decimal("0"))
        self.assertTrue(self.rec.is_reconciled)

    def test_the_page_says_it_re_read_the_cash_book(self):
        self.gift.department = self.fund
        self.gift.allocation_status = "MANUAL"
        self.gift.save()
        _entered_on(self.gift, 2026, 8, 3, 9, 0)
        r = self.client.get(f"/reconciliations/{self.rec.pk}/")
        said = [str(m) for m in r.context["messages"]]
        self.assertTrue(any("cash-book balance" in m for m in said), said)

    def test_a_quiet_re_open_says_nothing_and_changes_nothing(self):
        r = self.client.get(f"/reconciliations/{self.rec.pk}/")
        self.assertEqual([str(m) for m in r.context["messages"]], [])
        self.rec.refresh_from_db()
        self.assertEqual(self.rec.book_balance, Decimal("215000"))
        self.assertEqual(
            self.rec.items.filter(
                description=PENDING_RECEIPTS_ITEM).first().amount,
            Decimal("40000"))
