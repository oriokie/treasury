"""A remittance is one posting, or it is nothing.

The batch, the payment instrument that settles it and the one expense per trust
fund are a single payment written down in three places. They were being written
one at a time with nothing holding them together, so a failure part way down the
fund list — a validation error, a lost connection, anything — left the batch
marked REMITTED, an instrument for the FULL amount, and expenses raised against
only the funds the loop had reached. The trust liability then reads as cleared
for money that was never charged to the funds that collected it, and nothing on
any page contradicts it: the batch and the instrument both carry the full total,
so the books agree with themselves while being wrong.

These tests break the loop on purpose and insist that nothing at all survives.
"""
import datetime as dt
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from cashbook.models import Expense, PaymentInstrument, RemittanceBatch
from departments.models import Department
from giving.models import Transaction

START, END = dt.date(2026, 5, 1), dt.date(2026, 5, 31)


class _Boom(RuntimeError):
    """Whatever goes wrong on the fourth of six funds."""


def _fails_on_expense(n):
    """Patch ``Expense.objects.create`` so the n-th call raises, mid-loop.

    Patched on the manager rather than on ``save`` because the views call
    ``Expense.objects.create`` directly, and because a post-save signal that
    saves the row again would otherwise make the count mean something else.
    """
    original = Expense.objects.create
    calls = {"n": 0}

    def create(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == n:
            raise _Boom("the fourth fund of six")
        return original(*args, **kwargs)

    return mock.patch.object(Expense.objects, "create", create)


class RemittanceIsAllOrNothing(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("remit_atomic", password="x")
        self.tithe = Department.objects.create(
            name="Tithe", fund_type=Department.FundType.TRUST)
        self.campmeeting = Department.objects.create(
            name="Camp Meeting", fund_type=Department.FundType.TRUST)
        for fund, amount in ((self.tithe, "4000"), (self.campmeeting, "1500")):
            # ENVELOPE credits, because only RECEIPTED trust money is a firm
            # liability to remit (see balances._receipted_q)
            Transaction.objects.create(
                date=START, channel="ENVELOPE", direction="CREDIT",
                amount=Decimal(amount), department=fund, confirmed=True,
                allocation_status="AUTO")
        self.client.force_login(self.user)

    # --- the field payment (RemitTrustView) --------------------------------

    def test_a_field_payment_posts_every_fund_or_none_of_them(self):
        with _fails_on_expense(2), self.assertRaises(_Boom):
            self.client.post(reverse("remit_trust"),
                             {"start": START.isoformat(), "end": END.isoformat()})
        self.assertEqual(RemittanceBatch.objects.count(), 0,
                         "a batch marked REMITTED survived a failed remittance")
        self.assertEqual(PaymentInstrument.objects.count(), 0,
                         "an instrument for the full amount survived")
        self.assertEqual(Expense.objects.count(), 0,
                         "some funds were charged and the rest were not")

    def test_the_field_payment_still_works_when_nothing_goes_wrong(self):
        """The guard above must not have been bought by refusing to post at
        all: the ordinary path still raises one expense per outstanding fund,
        settled by one instrument for their total."""
        self.client.post(reverse("remit_trust"),
                         {"start": START.isoformat(), "end": END.isoformat(),
                          "method": "CHEQUE", "instrument_number": "000123"})
        batch = RemittanceBatch.objects.get()
        self.assertEqual(batch.status, RemittanceBatch.Status.REMITTED)
        self.assertEqual(batch.expenses.count(), 2)
        self.assertEqual(batch.payment.amount, Decimal("5500"))
        self.assertEqual(
            sum((e.amount for e in batch.expenses.all()), Decimal(0)),
            batch.payment.amount)

    # --- the draft batch (RemittanceBatchCreateView) -----------------------

    def test_a_draft_batch_is_never_left_holding_only_some_of_its_funds(self):
        """A draft commits nothing to the funds until it is approved, but a
        batch holding three of six funds walks through approval looking
        complete and the funds left out stay outstanding with nobody looking
        for them."""
        with _fails_on_expense(2), self.assertRaises(_Boom):
            self.client.post(reverse("remittance_batch_create"), {"all": "1"})
        self.assertEqual(RemittanceBatch.objects.count(), 0)
        self.assertEqual(Expense.objects.count(), 0)

    def test_the_draft_batch_still_works_when_nothing_goes_wrong(self):
        self.client.post(reverse("remittance_batch_create"), {"all": "1"})
        batch = RemittanceBatch.objects.get()
        self.assertEqual(batch.status, RemittanceBatch.Status.DRAFT)
        self.assertEqual(batch.expenses.count(), 2)
        self.assertEqual(batch.total_amount, Decimal("5500"))
