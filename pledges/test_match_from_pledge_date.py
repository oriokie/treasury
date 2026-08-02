"""Auto-matching starts at the pledge date.

A gift given before the promise was made is not payment of that promise. It was
giving the member had already done, and counting it toward the pledge credits
them twice while making the campaign look further along than it is.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from departments.models import Department
from giving.models import Transaction
from members.models import Member
from pledges.models import Pledge, PledgeCampaign
from pledges.services.matching import (auto_match_pledge,
                                       candidate_contributions)

PLEDGED_ON = dt.date(2026, 6, 10)


class MatchWindowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("pm", is_superuser=True)
        self.fund = Department.objects.create(name="Building", fund_type="LOCAL")
        self.member = Member.objects.create(name="ASHA MUTUA", active=True)
        self.campaign = PledgeCampaign.objects.create(
            name="Building", target_department=self.fund,
            status=PledgeCampaign.Status.ACTIVE)
        self.pledge = Pledge.objects.create(
            campaign=self.campaign, member=self.member,
            amount=Decimal("10000"), start_date=PLEDGED_ON,
            status=Pledge.Status.ACTIVE)

    def _gift(self, on, amount="4000"):
        return Transaction.objects.create(
            date=on, channel="CASH", direction="CREDIT",
            amount=Decimal(amount), department=self.fund, member=self.member,
            confirmed=True, allocation_status="MANUAL")

    def test_a_gift_on_the_pledge_date_counts(self):
        self._gift(PLEDGED_ON)
        self.assertEqual(len(candidate_contributions(self.pledge)), 1)

    def test_a_gift_after_the_pledge_counts(self):
        self._gift(PLEDGED_ON + dt.timedelta(days=30))
        self.assertEqual(len(candidate_contributions(self.pledge)), 1)

    def test_a_gift_the_day_before_does_not(self):
        self._gift(PLEDGED_ON - dt.timedelta(days=1))
        self.assertEqual(candidate_contributions(self.pledge), [])

    def test_a_gift_inside_the_old_seven_day_grace_does_not(self):
        """The window that used to sit here caught anyone who gave on the
        Sabbath and pledged the following week."""
        self._gift(PLEDGED_ON - dt.timedelta(days=5))
        self.assertEqual(candidate_contributions(self.pledge), [])

    def test_an_earlier_gift_is_not_applied_by_auto_match(self):
        self._gift(PLEDGED_ON - dt.timedelta(days=5))
        self.assertEqual(auto_match_pledge(self.pledge, user=self.user),
                         Decimal("0"))
        self.pledge.refresh_from_db()
        self.assertEqual(self.pledge.paid, Decimal("0"))

    def test_a_later_gift_is_applied(self):
        self._gift(PLEDGED_ON + dt.timedelta(days=3))
        self.assertEqual(auto_match_pledge(self.pledge, user=self.user),
                         Decimal("4000"))
        self.pledge.refresh_from_db()
        self.assertEqual(self.pledge.paid, Decimal("4000"))

    def test_only_the_later_gift_of_a_pair_is_taken(self):
        self._gift(PLEDGED_ON - dt.timedelta(days=2), "3000")
        self._gift(PLEDGED_ON + dt.timedelta(days=2), "5000")
        self.assertEqual(auto_match_pledge(self.pledge, user=self.user),
                         Decimal("5000"))
