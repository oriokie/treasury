"""An advance page must say why its figures differ from its own list.

The advance page shows a "settled by receipts" figure and, below it, a list of
every receipt handed in against the advance. The figure counts only approved and
paid receipts; the list shows all of them. When a holder has handed in more than
has been approved, the two disagree — and nothing on the page said why.

A treasurer looking at receipts totalling 59,747 and a card reading 40,000 has no
way to tell whether the figure is broken or the paperwork is simply waiting on
them. That is the fault: not the arithmetic, which is right, but a page that
states two numbers about the same thing and explains neither.

**The filter itself is correct and is deliberately not changed.** An unapproved
receipt has not been accepted as accounting for anything. Counting it would let
an advance appear settled before a single receipt had been read, which is the
whole point of having someone approve them.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase
from django.urls import reverse

from core import roles
from departments.models import Department

from .models import Expense, StaffAdvance


class AdvanceReceiptsAwaitingApprovalTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user("tess-adv2", password="office-pass-1")
        self.user.groups.add(Group.objects.get_or_create(name=roles.TREASURER)[0])
        self.fund = Department.objects.create(
            name="Camp Meeting", slug="camp-adv",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.advance = StaffAdvance.objects.create(
            staff_name="Peter Kamau", purpose="Camp supplies",
            department=self.fund, amount=Decimal("60000"),
            date_issued=dt.date.today(), issued_by=self.user,
            status=StaffAdvance.Status.ISSUED)
        self.client = Client()
        self.client.force_login(self.user)

    def _receipt(self, amount, status):
        return Expense.objects.create(
            date=dt.date.today(), department=self.fund,
            description=f"Receipt {amount}", amount=Decimal(amount),
            category=Expense.Category.MATERIALS, status=status,
            recorded_by=self.user, advance=self.advance)

    def _page(self):
        response = self.client.get(
            reverse("advance_detail", args=[self.advance.pk]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    # -- the figures ---------------------------------------------------------

    def test_only_approved_receipts_settle_the_advance(self):
        """The rule that was right all along and stays."""
        self._receipt("40000", Expense.Status.PAID)
        self._receipt("19747", Expense.Status.PENDING)
        self.assertEqual(self.advance.settled_total, Decimal("40000"))

    def test_the_waiting_amount_is_the_difference(self):
        self._receipt("40000", Expense.Status.PAID)
        self._receipt("19747", Expense.Status.PENDING)
        self.assertEqual(self.advance.awaiting_approval_total, Decimal("19747"))

    def test_the_submitted_total_is_what_the_list_adds_to(self):
        """The number a treasurer gets by adding the rows up themselves."""
        self._receipt("40000", Expense.Status.PAID)
        self._receipt("19747", Expense.Status.PENDING)
        listed = sum(e.amount for e in self.advance.expenses.all())
        self.assertEqual(self.advance.receipts_submitted_total, listed)

    def test_an_approved_receipt_is_not_double_counted(self):
        self._receipt("40000", Expense.Status.APPROVED)
        self.assertEqual(self.advance.awaiting_approval_total, Decimal("0"))
        self.assertEqual(self.advance.receipts_submitted_total, Decimal("40000"))

    def test_the_balance_still_reflects_only_approved_receipts(self):
        """Approving is what settles an advance; handing in is not."""
        self._receipt("40000", Expense.Status.PAID)
        self._receipt("19747", Expense.Status.PENDING)
        self.assertEqual(self.advance.balance, Decimal("20000"))

    def test_approving_the_receipt_closes_the_gap(self):
        """The behaviour that matters: the difference is a backlog, not a bug."""
        self._receipt("40000", Expense.Status.PAID)
        pending = self._receipt("19747", Expense.Status.PENDING)
        pending.status = Expense.Status.APPROVED
        pending.save(update_fields=["status"])
        self.assertEqual(self.advance.awaiting_approval_total, Decimal("0"))
        self.assertEqual(self.advance.settled_total, Decimal("59747"))
        self.assertEqual(self.advance.balance, Decimal("253"))

    # -- and what the page says about them ------------------------------------

    def test_the_page_says_receipts_are_waiting(self):
        self._receipt("40000", Expense.Status.PAID)
        self._receipt("19747", Expense.Status.PENDING)
        body = self._page()
        self.assertIn("waiting for approval", body)

    def test_the_page_shows_both_figures_so_they_can_be_reconciled(self):
        self._receipt("40000", Expense.Status.PAID)
        self._receipt("19747", Expense.Status.PENDING)
        body = self._page()
        self.assertIn("59,747", body)
        self.assertIn("40,000", body)

    def test_a_waiting_receipt_is_marked_in_the_list(self):
        """So the row that explains the difference is identifiable."""
        self._receipt("40000", Expense.Status.PAID)
        self._receipt("19747", Expense.Status.PENDING)
        self.assertIn("not yet counted", self._page())

    def test_nothing_is_said_when_there_is_nothing_waiting(self):
        """A notice that is always there is a notice nobody reads."""
        self._receipt("40000", Expense.Status.PAID)
        body = self._page()
        self.assertNotIn("waiting for approval", body)
        self.assertNotIn("not yet counted", body)

    def test_an_advance_with_no_receipts_still_renders(self):
        self.assertIn("No settling expenses recorded yet", self._page())

    def test_a_rejected_receipt_is_not_counted_as_waiting(self):
        """Rejected is decided, not pending — it will never settle anything."""
        self._receipt("40000", Expense.Status.PAID)
        self._receipt("5000", Expense.Status.REJECTED)
        self.assertEqual(self.advance.awaiting_approval_total, Decimal("0"))
        self.assertNotIn("waiting for approval", self._page())
