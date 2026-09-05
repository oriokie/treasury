"""Pledge and campaign member match codes — bank reference attribution."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.models import SiteConfig
from core.roles import TREASURER
from departments.models import Department
from giving.models import Campaign, CampaignMember, Transaction
from giving.services.allocation import campaign_allocate
from members.models import Member
from pledges.models import Pledge, PledgeCampaign, PledgePayment
from pledges.services import matching as match_svc
from pledges.services.codes import (
    find_campaign_member_by_code,
    find_pledge_by_code,
    pledge_code_allocate,
)


class PledgeMatchCodeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("pmc", password="x", is_superuser=True)
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(name="Roof Codes", fund_type="LOCAL")
        self.alice = Member.objects.create(
            name="ALICE CODE", phone="254711000001", active=True)
        self.bob = Member.objects.create(
            name="BOB CODE", phone="254722000002", active=True)
        self.campaign = PledgeCampaign.objects.create(
            name="Roof Codes", target_department=self.fund,
            status=PledgeCampaign.Status.ACTIVE)
        self.alice_pledge = Pledge.objects.create(
            campaign=self.campaign, member=self.alice,
            amount=Decimal("10000"), start_date=dt.date(2026, 1, 1),
            status=Pledge.Status.ACTIVE)
        self.bob_pledge = Pledge.objects.create(
            campaign=self.campaign, member=self.bob,
            amount=Decimal("10000"), start_date=dt.date(2026, 1, 1),
            status=Pledge.Status.ACTIVE)
        cfg = SiteConfig.get()
        cfg.pledge_match_mode = SiteConfig.PledgeMatchMode.AUTO
        cfg.pledge_match_same_fund_only = True
        cfg.save()

    def test_code_auto_assigned(self):
        self.assertTrue(self.alice_pledge.match_code)
        self.assertTrue(self.bob_pledge.match_code)
        self.assertNotEqual(self.alice_pledge.match_code, self.bob_pledge.match_code)

    def test_find_pledge_by_code_in_reference(self):
        ref = f"MPESA {self.bob_pledge.match_code} ROOF"
        self.assertEqual(find_pledge_by_code(ref), self.bob_pledge)

    def test_pledge_code_allocate_routes_fund(self):
        ref = f"GIFT {self.alice_pledge.match_code}"
        pledge, dept, status = pledge_code_allocate(ref)
        self.assertEqual(pledge, self.alice_pledge)
        self.assertEqual(dept, self.fund)
        self.assertEqual(status, "AUTO")

    def test_code_beats_payer_identity_cross_member(self):
        """Alice pays using Bob's code → Bob's pledge is filled."""
        gift = Transaction.objects.create(
            date=dt.date(2026, 6, 10), channel="BANK", direction="CREDIT",
            amount=Decimal("4000"), department=self.fund, member=self.alice,
            payer_name="ALICE CODE", payer_phone="254711000001",
            reference=f"FOR {self.bob_pledge.match_code}",
            confirmed=True, allocation_status="AUTO")
        pledges = match_svc.active_pledges_for_contribution(gift)
        self.assertEqual(pledges, [self.bob_pledge])
        msg = match_svc.handle_new_contribution(gift, user=self.user)
        self.assertIsNotNone(msg)
        self.assertEqual(self.bob_pledge.paid, Decimal("4000"))
        self.assertEqual(self.alice_pledge.paid, Decimal("0"))
        self.assertTrue(
            PledgePayment.objects.filter(
                pledge=self.bob_pledge, transaction=gift).exists())

    def test_candidate_contributions_include_code_gift(self):
        gift = Transaction.objects.create(
            date=dt.date(2026, 6, 11), channel="BANK", direction="CREDIT",
            amount=Decimal("2500"), department=self.fund, member=self.alice,
            payer_name="ALICE CODE", payer_phone="254711000001",
            reference=self.bob_pledge.match_code,
            confirmed=True, allocation_status="AUTO")
        ids = {c["txn"].id for c in match_svc.candidate_contributions(self.bob_pledge)}
        self.assertIn(gift.id, ids)
        row = next(c for c in match_svc.candidate_contributions(self.bob_pledge)
                   if c["txn"].id == gift.id)
        self.assertEqual(row["match"], "code")


class CampaignMemberCodeTests(TestCase):
    def setUp(self):
        self.fund = Department.objects.create(name="Camp Codes", fund_type="LOCAL")
        self.camp = Campaign.objects.create(
            name="Camp Codes", department=self.fund,
            triggers="campexpense", active=True)
        self.m1 = CampaignMember.objects.create(
            campaign=self.camp, name="RALLY ONE", phone="254733000003",
            group="CAMP_1")
        self.m2 = CampaignMember.objects.create(
            campaign=self.camp, name="RALLY TWO", phone="254744000004",
            group="CAMP_2")

    def test_code_auto_assigned(self):
        self.assertTrue(self.m1.match_code.startswith("CM"))
        self.assertNotEqual(self.m1.match_code, self.m2.match_code)

    def test_code_routes_without_trigger_or_payer_match(self):
        """Someone else pays with member 1's code → member 1's group fund."""
        camp, member = find_campaign_member_by_code(
            f"SUPPORT {self.m1.match_code}")
        self.assertEqual(camp, self.camp)
        self.assertEqual(member, self.m1)
        camp2, grp, dept, status = campaign_allocate(
            f"SUPPORT {self.m1.match_code}",
            "SOMEONE ELSE", "254799999999")
        self.assertEqual(camp2, self.camp)
        self.assertEqual(grp, "CAMP_1")
        self.assertEqual(status, "AUTO")
        self.assertEqual(dept, self.camp.subgroup_department("CAMP_1"))
