"""Paying a payable a bit at a time.

The behaviour is easy; the accounting is the part worth pinning. A payable that
is half paid must be a liability for the other half, from the day the money left
— not the full invoice until the last instalment arrives. If only one test here
survives, it should be
``PayableLiabilityTests.test_a_part_payment_reduces_the_liability_on_the_day_it_is_paid``.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from core.roles import ASSISTANT, TREASURER
from departments.models import Department

from .models import Expense, Payable
from .services import payables as payable_svc
from .services.treasury_position import open_payables_total

TODAY = dt.date.today()


class PayableTestBase(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("tess", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(
            name="Building Fund", slug="building-fund",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.payable = Payable.objects.create(
            date=TODAY - dt.timedelta(days=30), vendor="Mwangi Hardware",
            description="Cement and steel", amount=Decimal("100000.00"),
            department=self.fund, recorded_by=self.treasurer,
            due_date=TODAY + dt.timedelta(days=30))


class PayablePartialSettlementTests(PayableTestBase):

    def test_a_new_payable_owes_everything(self):
        self.assertEqual(self.payable.paid_total, Decimal("0"))
        self.assertEqual(self.payable.balance, Decimal("100000.00"))
        self.assertFalse(self.payable.settled)
        self.assertEqual(self.payable.status_label, "Outstanding")

    def test_an_instalment_leaves_the_rest_owing(self):
        payable_svc.settle(self.payable, amount=Decimal("30000"),
                           user=self.treasurer)
        self.payable.refresh_from_db()

        self.assertEqual(self.payable.paid_total, Decimal("30000.00"))
        self.assertEqual(self.payable.balance, Decimal("70000.00"))
        self.assertFalse(self.payable.settled,
                         "A part-paid payable must not read as settled.")
        self.assertTrue(self.payable.is_part_paid)
        self.assertEqual(self.payable.status_label, "Part paid")
        self.assertEqual(self.payable.percent_paid, 30)

    def test_instalments_accumulate_until_the_balance_clears(self):
        for part in ("30000", "45000", "25000"):
            payable_svc.settle(self.payable, amount=Decimal(part),
                               user=self.treasurer)
            self.payable.refresh_from_db()

        self.assertEqual(self.payable.paid_total, Decimal("100000.00"))
        self.assertEqual(self.payable.balance, Decimal("0"))
        self.assertTrue(self.payable.settled)
        self.assertEqual(self.payable.payments.count(), 3)

    def test_settled_on_is_the_date_the_LAST_instalment_cleared_it(self):
        """Not the first payment. The debt survives until the last shilling, and
        a balance sheet dated between the two must still show it."""
        first = TODAY - dt.timedelta(days=10)
        last = TODAY - dt.timedelta(days=2)
        payable_svc.settle(self.payable, amount=Decimal("60000"),
                           user=self.treasurer, on=first)
        payable_svc.settle(self.payable, amount=Decimal("40000"),
                           user=self.treasurer, on=last)
        self.payable.refresh_from_db()
        self.assertEqual(self.payable.settled_on, last)

    def test_no_amount_means_pay_the_remaining_balance(self):
        payable_svc.settle(self.payable, amount=Decimal("40000"),
                           user=self.treasurer)
        expense = payable_svc.settle(self.payable, user=self.treasurer)
        self.payable.refresh_from_db()

        self.assertEqual(expense.amount, Decimal("60000.00"))
        self.assertTrue(self.payable.settled)

    def test_paying_more_than_is_owed_is_refused(self):
        """Refused, not silently capped: an overpayment is either a typo or a
        credit the vendor now holds, and a human has to say which."""
        with self.assertRaises(ValidationError):
            payable_svc.settle(self.payable, amount=Decimal("120000"),
                               user=self.treasurer)
        with self.assertRaises(ValidationError):
            payable_svc.settle(self.payable, amount=Decimal("0"),
                               user=self.treasurer)

    def test_a_fully_settled_payable_cannot_be_paid_again(self):
        payable_svc.settle(self.payable, user=self.treasurer)
        self.payable.refresh_from_db()
        with self.assertRaises(ValidationError):
            payable_svc.settle(self.payable, amount=Decimal("1"),
                               user=self.treasurer)

    def test_a_payment_cannot_predate_the_invoice(self):
        with self.assertRaises(ValidationError):
            payable_svc.settle(
                self.payable, amount=Decimal("100"), user=self.treasurer,
                on=self.payable.date - dt.timedelta(days=1))

    def test_each_instalment_is_a_real_expense_in_the_fund(self):
        """The payment reaches the cash book, the fund balance and the ledger by
        the ordinary route. Nothing about partial settlement is a side channel."""
        payable_svc.settle(self.payable, amount=Decimal("30000"),
                           user=self.treasurer)
        expense = self.payable.payments.get()

        self.assertEqual(expense.department, self.fund)
        self.assertEqual(expense.amount, Decimal("30000.00"))
        self.assertEqual(expense.status, Expense.Status.PAID)
        self.assertEqual(expense.category, self.payable.category)
        self.assertIn("Part payment", expense.description)
        self.assertEqual(expense.payee, "Mwangi Hardware")

    def test_a_pending_claim_does_not_reduce_the_balance(self):
        """Only APPROVED/PAID payments count — the same rule advances use. An
        unapproved claim must not discharge a debt before anyone authorised it."""
        Expense.objects.create(
            date=TODAY, department=self.fund, description="Unapproved",
            amount=Decimal("50000"), status=Expense.Status.PENDING,
            recorded_by=self.treasurer, payable=self.payable)
        payable_svc.refresh_settlement(self.payable)
        self.payable.refresh_from_db()

        self.assertEqual(self.payable.paid_total, Decimal("0"))
        self.assertEqual(self.payable.balance, Decimal("100000.00"))
        self.assertFalse(self.payable.settled)


class PayableLiabilityTests(PayableTestBase):
    """What the balance sheet says we owe."""

    def test_an_unpaid_payable_is_a_liability_in_full(self):
        self.assertEqual(open_payables_total(), Decimal("100000.00"))
        self.assertEqual(open_payables_total(TODAY), Decimal("100000.00"))

    def test_a_part_payment_reduces_the_liability_on_the_day_it_is_paid(self):
        """The heart of it. Before partial settlement existed, this payable
        stayed on the balance sheet at its full 100,000 until the final
        instalment — so the church reported owing money it had already paid."""
        paid_on = TODAY - dt.timedelta(days=5)
        payable_svc.settle(self.payable, amount=Decimal("30000"),
                           user=self.treasurer, on=paid_on)

        # the day before the payment: still owed in full
        self.assertEqual(
            open_payables_total(paid_on - dt.timedelta(days=1)),
            Decimal("100000.00"))
        # from the day of the payment: only the rest
        self.assertEqual(open_payables_total(paid_on), Decimal("70000.00"))
        self.assertEqual(open_payables_total(TODAY), Decimal("70000.00"))
        self.assertEqual(open_payables_total(), Decimal("70000.00"))

    def test_a_fully_settled_payable_leaves_the_liability(self):
        payable_svc.settle(self.payable, user=self.treasurer, on=TODAY)
        self.assertEqual(open_payables_total(), Decimal("0"))
        self.assertEqual(open_payables_total(TODAY), Decimal("0"))
        # ...but was still owed the day before it was paid
        self.assertEqual(open_payables_total(TODAY - dt.timedelta(days=1)),
                         Decimal("100000.00"))

    def test_one_vendors_overpayment_cannot_cancel_another_vendors_debt(self):
        """Netted per payable, never in one lump. A balance can't go negative."""
        other = Payable.objects.create(
            date=TODAY - dt.timedelta(days=10), vendor="Other Supplier",
            description="Paint", amount=Decimal("5000"),
            department=self.fund, recorded_by=self.treasurer)
        # an expense larger than the invoice, linked directly (bypassing the
        # service's own guard) to prove the metric is defensive too
        Expense.objects.create(
            date=TODAY, department=self.fund, description="Overpaid",
            amount=Decimal("9000"), status=Expense.Status.PAID,
            recorded_by=self.treasurer, payable=other)
        payable_svc.refresh_settlement(other)

        self.assertEqual(open_payables_total(TODAY), Decimal("100000.00"),
                         "An overpayment on one payable reduced another's debt.")

    def test_the_liability_metric_does_not_query_per_payable(self):
        """The balance sheet must not issue a query per invoice."""
        for i in range(12):
            Payable.objects.create(
                date=TODAY - dt.timedelta(days=5), vendor=f"V{i}",
                description="d", amount=Decimal("1000"),
                department=self.fund, recorded_by=self.treasurer)
        with self.assertNumQueries(1):
            open_payables_total(TODAY)


class PayableUnlinkTests(PayableTestBase):
    def test_detaching_a_payment_keeps_the_expense_and_restores_the_balance(self):
        payable_svc.settle(self.payable, amount=Decimal("30000"),
                           user=self.treasurer)
        expense = self.payable.payments.get()

        payable_svc.unlink_payment(expense)
        self.payable.refresh_from_db()
        expense.refresh_from_db()

        self.assertEqual(self.payable.balance, Decimal("100000.00"))
        self.assertIsNone(expense.payable_id)
        self.assertTrue(Expense.objects.filter(pk=expense.pk).exists(),
                        "The money left the bank; the expense must survive.")

    def test_detaching_reopens_a_settled_payable(self):
        payable_svc.settle(self.payable, user=self.treasurer)
        self.payable.refresh_from_db()
        self.assertTrue(self.payable.settled)

        payable_svc.unlink_payment(self.payable.payments.get())
        self.payable.refresh_from_db()
        self.assertFalse(self.payable.settled)
        self.assertIsNone(self.payable.settled_on)


class PayableViewTests(PayableTestBase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username="tess", password="x")

    def test_the_payables_page_shows_paid_and_balance(self):
        payable_svc.settle(self.payable, amount=Decimal("30000"),
                           user=self.treasurer)
        response = self.client.get(reverse("accruals"))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Part paid", body)
        self.assertIn("70,000", body)

    def test_posting_an_instalment_through_the_view(self):
        response = self.client.post(
            reverse("payable_settle", args=[self.payable.pk]),
            {"amount": "25000"})
        self.assertEqual(response.status_code, 302)
        self.payable.refresh_from_db()
        self.assertEqual(self.payable.balance, Decimal("75000.00"))
        self.assertFalse(self.payable.settled)

    def test_posting_with_no_amount_pays_the_balance(self):
        self.client.post(reverse("payable_settle", args=[self.payable.pk]), {})
        self.payable.refresh_from_db()
        self.assertTrue(self.payable.settled)

    def test_an_overpayment_through_the_view_is_rejected_with_a_message(self):
        self.client.post(reverse("payable_settle", args=[self.payable.pk]),
                         {"amount": "500000"})
        self.payable.refresh_from_db()
        self.assertEqual(self.payable.paid_total, Decimal("0"),
                         "An overpayment was accepted through the view.")

    def test_an_assistant_may_pay_but_only_a_treasurer_may_detach(self):
        assistant = User.objects.create_user("ass", password="x")
        assistant.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
        payable_svc.settle(self.payable, amount=Decimal("1000"),
                           user=self.treasurer)
        expense = self.payable.payments.get()

        client = Client()
        client.login(username="ass", password="x")
        self.assertEqual(
            client.post(reverse("payable_settle", args=[self.payable.pk]),
                        {"amount": "100"}).status_code, 302)
        response = client.post(
            reverse("payable_unlink_payment", args=[expense.pk]))
        expense.refresh_from_db()
        self.assertIsNotNone(expense.payable_id,
                             "An assistant was able to detach a payment.")


class PayableLegacySettlementTests(PayableTestBase):
    """Payables settled before instalments existed must stay settled.

    Added after nearly shipping the opposite. When settlement became a sum of
    payments, a payable marked `settled` with no payment rows to show for it
    computed as fully unpaid — so every debt discharged under the old
    all-or-nothing button, whose expense link was never recorded, would have
    reappeared on the balance sheet as money still owed.

    The data migration re-points the ones that DO have a linked expense. This is
    about the remainder, where the flag a treasurer set is the only evidence
    there is. Trusting it is strictly safer than ignoring it: the worst case is
    a debt that stays discharged, against a worst case of the church reporting
    money it has already paid.
    """

    def test_a_flag_only_settlement_is_still_discharged(self):
        self.payable.delete()          # isolate the legacy row
        Payable.objects.create(
            date=dt.date(2026, 6, 1), vendor="Acme", description="Chairs",
            amount=Decimal("5000"), department=self.fund,
            recorded_by=self.treasurer,
            settled=True, settled_on=dt.date(2026, 6, 20))

        self.assertEqual(open_payables_total(dt.date(2026, 6, 10)), Decimal("5000"))
        self.assertEqual(open_payables_total(dt.date(2026, 6, 20)), Decimal("0"))
        self.assertEqual(open_payables_total(), Decimal("0"))

    def test_real_payments_still_win_over_the_flag(self):
        """The flag is a fallback for rows with no payments, not an override."""
        self.payable.delete()
        p = Payable.objects.create(
            date=dt.date(2026, 6, 1), vendor="Acme", description="Chairs",
            amount=Decimal("5000"), department=self.fund,
            recorded_by=self.treasurer)
        Expense.objects.create(
            date=dt.date(2026, 6, 10), department=self.fund, description="part",
            amount=Decimal("2000"), status=Expense.Status.PAID,
            recorded_by=self.treasurer, payable=p)
        payable_svc.refresh_settlement(p)

        self.assertEqual(open_payables_total(dt.date(2026, 6, 10)), Decimal("3000"))

    # --- the same rule, on the WRITE path ------------------------------------
    #
    # The class above only ever asked the balance sheet what it thought. The
    # write path was never asked, and it disagreed: `settle()` guards on
    # `balance`, which was pure arithmetic (`amount - payments`) and knew
    # nothing of the flag — so a legacy row reported its full amount as still
    # owing and accepted a second, complete payment for a bill already
    # discharged. That posted real money to the ledger a second time and
    # overwrote `settled_on`, destroying the only record of the original
    # settlement. Both sides now read one definition.

    def _legacy_row(self):
        """A settlement made before instalments existed: the flag, and nothing
        else. No payment rows, because its expense link was never recorded."""
        self.payable.delete()
        return Payable.objects.create(
            date=dt.date(2026, 6, 1), vendor="Acme", description="Chairs",
            amount=Decimal("5000"), department=self.fund,
            recorded_by=self.treasurer,
            settled=True, settled_on=dt.date(2026, 6, 20))

    def test_a_flag_only_settlement_cannot_be_paid_a_second_time(self):
        p = self._legacy_row()
        before = Expense.objects.count()

        with self.assertRaises(ValidationError):
            payable_svc.settle(p, user=self.treasurer)

        self.assertEqual(Expense.objects.count(), before,
                         "a second payment expense was created for a debt "
                         "already discharged")
        p.refresh_from_db()
        self.assertEqual(p.settled_on, dt.date(2026, 6, 20),
                         "the original settlement date was overwritten")

    def test_a_flag_only_settlement_owes_nothing_on_the_write_path_too(self):
        """The write path's guard reads `balance`; it must agree with the
        balance sheet, which has always treated this row as discharged."""
        p = self._legacy_row()
        self.assertEqual(p.balance, Decimal("0"))
        self.assertTrue(p.is_settled)
        self.assertEqual(p.status_label, "Settled")

    def test_the_flag_is_not_believed_before_the_day_it_was_settled(self):
        """As at a date, a settlement that had not happened yet cannot count —
        the same rule the balance sheet applies via `settled_on__lte`."""
        p = self._legacy_row()
        self.assertEqual(p.balance_asof(dt.date(2026, 6, 10)), Decimal("5000"))
        self.assertEqual(p.balance_asof(dt.date(2026, 6, 20)), Decimal("0"))

    def test_a_part_paid_payable_can_still_be_settled(self):
        """The narrowness of the rule, pinned: the flag is believed only where
        there is NO payment evidence. A row with real payments still owes the
        remainder and must still be payable — otherwise this fix would have
        broken instalments, which is the whole feature."""
        self.payable.delete()
        p = Payable.objects.create(
            date=dt.date(2026, 6, 1), vendor="Acme", description="Chairs",
            amount=Decimal("5000"), department=self.fund,
            recorded_by=self.treasurer)
        payable_svc.settle(p, amount=Decimal("2000"), user=self.treasurer,
                           on=dt.date(2026, 6, 10))
        p.refresh_from_db()
        self.assertEqual(p.balance, Decimal("3000"))

        payable_svc.settle(p, user=self.treasurer, on=dt.date(2026, 6, 15))
        p.refresh_from_db()
        self.assertEqual(p.balance, Decimal("0"))
        self.assertTrue(p.settled)


class AccrualPartialSettlementTests(TestCase):
    """The same behaviour, on the other obligation.

    Accruals were left all-or-nothing when payables gained instalments, on the
    reasoning that an accrual is an estimate either replaced by the real invoice
    or not. That reasoning does not survive contact with a utility bill paid in
    two goes against an accrued estimate. Both now share
    `SettleableObligation` and one service, so these tests are as much a check
    that the generalisation did not fork as that accruals work.
    """

    def setUp(self):
        self.treasurer = User.objects.create_user("tess2", password="x")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(
            name="Utilities Fund", slug="utilities-fund",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        from .models import Accrual
        self.accrual = Accrual.objects.create(
            date=TODAY - dt.timedelta(days=20), description="Power for June",
            amount=Decimal("8000.00"), department=self.fund,
            recorded_by=self.treasurer)

    def test_an_instalment_leaves_the_rest_owing(self):
        payable_svc.settle(self.accrual, amount=Decimal("3000"),
                           user=self.treasurer)
        self.accrual.refresh_from_db()
        self.assertEqual(self.accrual.paid_total, Decimal("3000.00"))
        self.assertEqual(self.accrual.balance, Decimal("5000.00"))
        self.assertFalse(self.accrual.settled)
        self.assertEqual(self.accrual.status_label, "Part paid")

    def test_instalments_clear_it(self):
        payable_svc.settle(self.accrual, amount=Decimal("3000"), user=self.treasurer)
        payable_svc.settle(self.accrual, user=self.treasurer)
        self.accrual.refresh_from_db()
        self.assertTrue(self.accrual.settled)
        self.assertEqual(self.accrual.payments.count(), 2)

    def test_the_accrual_liability_nets_off_payments(self):
        from .services.treasury_position import open_accruals_total
        paid_on = TODAY - dt.timedelta(days=3)
        payable_svc.settle(self.accrual, amount=Decimal("3000"),
                           user=self.treasurer, on=paid_on)

        self.assertEqual(open_accruals_total(paid_on - dt.timedelta(days=1)),
                         Decimal("8000.00"))
        self.assertEqual(open_accruals_total(paid_on), Decimal("5000.00"))
        self.assertEqual(open_accruals_total(), Decimal("5000.00"))

    def test_a_flag_only_accrual_settlement_is_still_discharged(self):
        """The legacy-data rule holds for accruals too."""
        from .models import Accrual
        from .services.treasury_position import open_accruals_total
        self.accrual.delete()
        Accrual.objects.create(
            date=dt.date(2026, 6, 1), description="Water", amount=Decimal("500"),
            department=self.fund, recorded_by=self.treasurer,
            settled=True, settled_on=dt.date(2026, 6, 15))

        self.assertEqual(open_accruals_total(dt.date(2026, 6, 14)), Decimal("500"))
        self.assertEqual(open_accruals_total(dt.date(2026, 6, 15)), Decimal("0"))

    def test_overpayment_is_refused_on_accruals_too(self):
        with self.assertRaises(ValidationError):
            payable_svc.settle(self.accrual, amount=Decimal("9000"),
                               user=self.treasurer)

    def test_the_settlement_rules_are_shared_not_copied(self):
        """A guard on the generalisation itself: both obligations must inherit
        the one implementation, not each carry their own."""
        from .models import Accrual, Payable, SettleableObligation
        self.assertTrue(issubclass(Payable, SettleableObligation))
        self.assertTrue(issubclass(Accrual, SettleableObligation))
        for name in ("paid_asof", "balance_asof", "is_part_paid", "status_label"):
            self.assertNotIn(name, Payable.__dict__,
                             f"Payable redefines {name} instead of inheriting it.")
            self.assertNotIn(name, Accrual.__dict__,
                             f"Accrual redefines {name} instead of inheriting it.")
