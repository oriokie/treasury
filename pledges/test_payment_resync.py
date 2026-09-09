"""A pledge payment must follow the contribution it was matched to.

The link between a PledgePayment and its gift is written when the gift arrives,
but the gift can change afterwards. These tests pin the behaviour asked for:
reverse the gift, reallocate it to another fund, or credit it to another
member, and the amount on the pledge tracker must follow.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.models import SiteConfig
from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from members.models import Member
from pledges.models import Pledge, PledgeCampaign, PledgePayment
from pledges.services import matching as match_svc


class ResyncBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("rs", password="x", is_superuser=True)
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(name="Building", fund_type="LOCAL")
        self.other_fund = Department.objects.create(name="Vehicle", fund_type="LOCAL")
        self.member = Member.objects.create(
            name="JOHN KAMAU", phone="254712000111", active=True)
        self.other_member = Member.objects.create(
            name="PETER OTIENO", phone="254733000222", active=True)
        self.campaign = PledgeCampaign.objects.create(
            name="Building", target_department=self.fund,
            status=PledgeCampaign.Status.ACTIVE)
        self.pledge = Pledge.objects.create(
            campaign=self.campaign, member=self.member,
            amount=Decimal("10000"), start_date=dt.date(2026, 1, 1),
            status=Pledge.Status.ACTIVE)
        # Same-fund scoping on, so a reallocation to another fund is meaningful.
        cfg = SiteConfig.get()
        cfg.pledge_match_same_fund_only = True
        cfg.pledge_match_mode = SiteConfig.PledgeMatchMode.AUTO
        cfg.save()

    def _gift(self, **kw):
        base = dict(
            date=dt.date(2026, 6, 10), channel="BANK",
            direction="CREDIT", amount=Decimal("4000"), department=self.fund,
            member=self.member, confirmed=True, allocation_status="AUTO")
        base.update(kw)
        return Transaction.objects.create(**base)


class ReverseTests(ResyncBase):
    def test_reversing_a_gift_removes_it_from_the_tracker(self):
        gift = self._gift()
        PledgePayment.objects.create(
            pledge=self.pledge, transaction=gift, amount=Decimal("4000"),
            date=gift.date)
        self.assertEqual(self.pledge.paid, Decimal("4000"))

        with self.captureOnCommitCallbacks(execute=True):
            gift.reverse(self.user, reason="wrong entry")

        self.assertFalse(
            PledgePayment.objects.filter(pledge=self.pledge,
                                         transaction=gift).exists())
        self.assertEqual(self.pledge.paid, Decimal("0"))

    def test_unconfirming_a_gift_removes_the_link(self):
        gift = self._gift()
        PledgePayment.objects.create(
            pledge=self.pledge, transaction=gift, amount=Decimal("4000"),
            date=gift.date)
        with self.captureOnCommitCallbacks(execute=True):
            gift.confirmed = False
            gift.save(update_fields=["confirmed"])
        self.assertEqual(self.pledge.paid, Decimal("0"))


class ReallocateTests(ResyncBase):
    def test_reallocating_to_another_fund_removes_the_link(self):
        gift = self._gift()
        PledgePayment.objects.create(
            pledge=self.pledge, transaction=gift, amount=Decimal("4000"),
            date=gift.date)
        self.assertEqual(self.pledge.paid, Decimal("4000"))

        # Reallocated to a fund outside the campaign.
        with self.captureOnCommitCallbacks(execute=True):
            gift.department = self.other_fund
            gift.save(update_fields=["department"])

        self.assertEqual(self.pledge.paid, Decimal("0"))

    def test_unallocating_to_review_keeps_the_link(self):
        """A transient 'sent back to review' state (no fund) must not wipe a
        real match — unallocated is not reallocated-away."""
        gift = self._gift()
        PledgePayment.objects.create(
            pledge=self.pledge, transaction=gift, amount=Decimal("4000"),
            date=gift.date)
        with self.captureOnCommitCallbacks(execute=True):
            gift.department = None
            gift.allocation_status = "REVIEW"
            gift.save(update_fields=["department", "allocation_status"])
        self.assertEqual(self.pledge.paid, Decimal("4000"))


class ReassignMemberTests(ResyncBase):
    def test_moving_a_gift_to_another_member_drops_it_from_the_pledge(self):
        gift = self._gift()
        PledgePayment.objects.create(
            pledge=self.pledge, transaction=gift, amount=Decimal("4000"),
            date=gift.date)
        self.assertEqual(self.pledge.paid, Decimal("4000"))

        # Credited to a different member with no pledge here.
        with self.captureOnCommitCallbacks(execute=True):
            gift.member = self.other_member
            gift.payer_phone = ""
            gift.payer_name = ""
            gift.save(update_fields=["member", "payer_phone", "payer_name"])

        self.assertEqual(self.pledge.paid, Decimal("0"))

    def test_moving_a_gift_to_a_new_pledgor_rematches_to_their_pledge(self):
        other_pledge = Pledge.objects.create(
            campaign=self.campaign, member=self.other_member,
            amount=Decimal("6000"), start_date=dt.date(2026, 1, 1),
            status=Pledge.Status.ACTIVE)
        gift = self._gift()
        PledgePayment.objects.create(
            pledge=self.pledge, transaction=gift, amount=Decimal("4000"),
            date=gift.date)

        with self.captureOnCommitCallbacks(execute=True):
            gift.member = self.other_member
            gift.save(update_fields=["member"])

        self.assertEqual(self.pledge.paid, Decimal("0"))
        other_pledge.refresh_from_db()
        self.assertEqual(other_pledge.paid, Decimal("4000"))

    def test_reassign_drops_even_when_payer_phone_still_matches(self):
        """The edit form leaves payer_phone alone. That number used to keep
        the old pledge linked — and rematch would put it straight back."""
        gift = self._gift(payer_phone="254712000111", payer_name="JOHN KAMAU")
        PledgePayment.objects.create(
            pledge=self.pledge, transaction=gift, amount=Decimal("4000"),
            date=gift.date)
        other_pledge = Pledge.objects.create(
            campaign=self.campaign, member=self.other_member,
            amount=Decimal("6000"), start_date=dt.date(2026, 1, 1),
            status=Pledge.Status.ACTIVE)

        with self.captureOnCommitCallbacks(execute=True):
            gift.member = self.other_member
            gift.save(update_fields=["member"])

        self.assertEqual(self.pledge.paid, Decimal("0"))
        other_pledge.refresh_from_db()
        self.assertEqual(other_pledge.paid, Decimal("4000"))

    def test_cancelling_then_opening_the_page_keeps_the_match_record(self):
        """Cancel redirects to the pledge page. Healing must not treat a
        cancelled promise as 'no longer qualifying' and delete the payment."""
        gift = self._gift()
        PledgePayment.objects.create(
            pledge=self.pledge, transaction=gift, amount=Decimal("4000"),
            date=gift.date)
        self.pledge.status = Pledge.Status.CANCELLED
        self.pledge.save()

        self.client.force_login(self.user)
        r = self.client.get(f"/pledges/{self.pledge.pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.pledge.paid, Decimal("4000"))

    def test_opening_the_pledge_page_heals_a_stale_link(self):
        """A gift already moved off this pledge (no signal fired) drops
        when a treasurer opens the tracker."""
        gift = self._gift(payer_phone="254712000111")
        PledgePayment.objects.create(
            pledge=self.pledge, transaction=gift, amount=Decimal("4000"),
            date=gift.date)
        Transaction.objects.filter(pk=gift.pk).update(
            member=self.other_member)

        self.client.force_login(self.user)
        r = self.client.get(f"/pledges/{self.pledge.pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.pledge.paid, Decimal("0"))


class BackfillCommandTests(ResyncBase):
    def test_command_removes_stale_link_from_reversed_gift(self):
        from django.core.management import call_command
        gift = self._gift()
        PledgePayment.objects.create(
            pledge=self.pledge, transaction=gift, amount=Decimal("4000"),
            date=gift.date)
        # Reverse WITHOUT going through the signal path (simulate a legacy
        # stale link): flip the flag directly with a field the signal ignores.
        Transaction.objects.filter(pk=gift.pk).update(is_reversed=True)
        self.assertEqual(self.pledge.paid, Decimal("4000"))  # still stale

        call_command("resync_pledge_payments")

        self.assertEqual(self.pledge.paid, Decimal("0"))
