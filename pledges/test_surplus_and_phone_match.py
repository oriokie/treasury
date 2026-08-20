"""Surplus allocation and phone-based auto-match.

A gift linked only by M-Pesa phone (no member FK, name misspelled) must still
reach the pledge. Extra giving beyond a completed promise stays on that
tracker when nothing else is open, and fills other open pledges first.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.roles import TREASURER
from departments.models import Department
from giving.models import Transaction
from members.models import Member
from pledges.models import Pledge, PledgeCampaign, PledgePayment
from pledges.services import matching as match_svc


class PhoneMatchTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("ph", password="x", is_superuser=True)
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(name="Roof", fund_type="LOCAL")
        self.member = Member.objects.create(
            name="ASHA MUTUA", phone="254712000111", active=True)
        self.campaign = PledgeCampaign.objects.create(
            name="Roof", target_department=self.fund,
            status=PledgeCampaign.Status.ACTIVE)
        self.pledge = Pledge.objects.create(
            campaign=self.campaign, member=self.member,
            amount=Decimal("10000"), start_date=dt.date(2026, 1, 1),
            status=Pledge.Status.ACTIVE)

    def test_unlinked_bank_gift_matches_by_phone(self):
        gift = Transaction.objects.create(
            date=dt.date(2026, 6, 10), channel="BANK", direction="CREDIT",
            amount=Decimal("4000"), department=self.fund, member=None,
            payer_name="A MUTUA", payer_phone="0712000111",
            confirmed=True, allocation_status="AUTO")
        ids = {c["txn"].id for c in match_svc.candidate_contributions(self.pledge)}
        self.assertIn(gift.id, ids)
        applied = match_svc.auto_match_pledge(self.pledge, user=self.user)
        self.assertEqual(applied, Decimal("4000"))

    def test_plan_auto_match_all_finds_phone_linked_gifts(self):
        Transaction.objects.create(
            date=dt.date(2026, 6, 10), channel="BANK", direction="CREDIT",
            amount=Decimal("4000"), department=self.fund, member=None,
            payer_name="SOMEONE ELSE", payer_phone="254712000111",
            confirmed=True, allocation_status="AUTO")
        plan = match_svc.plan_auto_match_all()
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["amount"], Decimal("4000"))


class LapsedDoesNotBlockSurplusTests(TestCase):
    """A lapsed unpaid promise elsewhere must not stop extra giving landing on
    a completed pledge the member just finished."""

    def setUp(self):
        self.user = User.objects.create_user("lp", password="x", is_superuser=True)
        self.fund = Department.objects.create(name="Camp", fund_type="LOCAL")
        self.member = Member.objects.create(name="GIVER", phone="254700111222")
        self.campaign = PledgeCampaign.objects.create(
            name="Camp", target_department=self.fund,
            status=PledgeCampaign.Status.ACTIVE)
        self.done = Pledge.objects.create(
            campaign=self.campaign, member=self.member, amount=Decimal("5000"),
            start_date=dt.date(2026, 1, 1), status=Pledge.Status.ACTIVE)
        self.old = Pledge.objects.create(
            campaign=self.campaign, member=self.member, amount=Decimal("9000"),
            start_date=dt.date(2025, 1, 1), end_date=dt.date(2025, 6, 1),
            status=Pledge.Status.LAPSED)

    def test_surplus_still_applies_when_a_lapsed_pledge_is_owing(self):
        # Fill the active pledge exactly, then give more
        t1 = Transaction.objects.create(
            date=dt.date(2026, 6, 10), channel="CASH", direction="CREDIT",
            amount=Decimal("5000"), department=self.fund, member=self.member,
            confirmed=True, allocation_status="MANUAL")
        PledgePayment.objects.create(
            pledge=self.done, transaction=t1, amount=Decimal("5000"),
            date=t1.date)
        self.done.recompute_status()
        self.done.refresh_from_db()
        self.assertEqual(self.done.status, Pledge.Status.FULFILLED)

        Transaction.objects.create(
            date=dt.date(2026, 6, 11), channel="CASH", direction="CREDIT",
            amount=Decimal("2000"), department=self.fund, member=self.member,
            confirmed=True, allocation_status="MANUAL")
        plan = match_svc.plan_auto_match_all()
        # Extra gift fills the still-open (lapsed) promise first — not left
        # unmatched because a completed pledge also exists.
        self.assertTrue(plan, "auto-match offered nothing when a gift was waiting")
        total = sum((r["amount"] for r in plan), Decimal("0"))
        self.assertEqual(total, Decimal("2000"))
        self.assertEqual(plan[0]["pledge"].id, self.old.id)
