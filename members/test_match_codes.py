"""Member match codes attribute gifts to the intended person and their group."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.models import SiteConfig
from core.roles import TREASURER
from departments.models import Department, DevelopmentGroup
from giving.models import Transaction
from members.models import Member
from pledges.models import Pledge, PledgeCampaign
from pledges.services.attribution import apply_code_to_import
from pledges.services.codes import find_member_by_code, resolve_code_attribution
from pledges.services import matching as match_svc
from reports.services.balances import dev_group_members


class MemberMatchCodeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("mmc", password="x", is_superuser=True)
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(
            name="Dev Fund MMC", fund_type="LOCAL", category="DEVELOPMENT")
        self.g7 = DevelopmentGroup.objects.create(number=7, name="Group 7 MMC")
        self.g3 = DevelopmentGroup.objects.create(number=3, name="Group 3 MMC")
        self.alice = Member.objects.create(
            name="ALICE MMC", phone="254711111001", active=True, dev_group=self.g7)
        self.bob = Member.objects.create(
            name="BOB MMC", phone="254722222002", active=True, dev_group=self.g3)
        self.campaign = PledgeCampaign.objects.create(
            name="Roof MMC", target_department=self.fund,
            status=PledgeCampaign.Status.ACTIVE)
        self.bob_pledge = Pledge.objects.create(
            campaign=self.campaign, member=self.bob,
            amount=Decimal("10000"), start_date=dt.date(2026, 1, 1),
            status=Pledge.Status.ACTIVE)
        cfg = SiteConfig.get()
        cfg.pledge_match_mode = SiteConfig.PledgeMatchMode.AUTO
        cfg.pledge_match_same_fund_only = True
        cfg.save()

    def test_member_code_auto_assigned(self):
        self.assertTrue(self.alice.match_code.startswith("MB"))
        self.assertNotEqual(self.alice.match_code, self.bob.match_code)

    def test_find_member_by_code(self):
        self.assertEqual(
            find_member_by_code(f"GIFT {self.bob.match_code}"), self.bob)

    def test_alice_pays_with_bobs_code_credits_bob_and_group(self):
        hit = resolve_code_attribution(
            f"FOR {self.bob.match_code}", payer_member=self.alice)
        self.assertEqual(hit["beneficiary"], self.bob)
        self.assertEqual(hit["dev_group"], self.g3)
        self.assertTrue(hit["via_code"])

        member, dept, dg, camp, cgrp, status, via = apply_code_to_import(
            reference=f"FOR {self.bob.match_code}",
            member=self.alice, dept=None, dev_group=None,
            campaign=None, campaign_group="", status="REVIEW",
            Transaction=Transaction)
        self.assertEqual(member, self.bob)
        self.assertEqual(dg, self.g3)
        self.assertTrue(via)
        self.assertEqual(dept, self.fund)  # from Bob's open pledge campaign

        gift = Transaction.objects.create(
            date=dt.date(2026, 6, 10), channel="BANK", direction="CREDIT",
            amount=Decimal("4000"), department=self.fund, member=self.bob,
            payer_name="ALICE MMC", payer_phone="254711111001",
            reference=f"FOR {self.bob.match_code}",
            confirmed=True, allocation_status="AUTO",
            attributed_via_code=True, dev_group=self.g3)
        match_svc.handle_new_contribution(gift, user=self.user)
        self.assertEqual(self.bob_pledge.paid, Decimal("4000"))

        tallies = dev_group_members(self.g3)
        bob_row = next(r for r in tallies["rows"] if r["name"] == "BOB MMC")
        self.assertEqual(bob_row["total"], Decimal("4000"))
        self.assertIn("ALICE MMC", bob_row["via"])
        # Alice's own group must not get this gift
        alice_tallies = dev_group_members(self.g7)
        self.assertEqual(alice_tallies["total"], Decimal("0"))

    def test_pledge_code_also_credits_group_and_shows_paid_by(self):
        """PG… codes behave like MB… for attribution / tallies."""
        member, dept, dg, camp, cgrp, status, via = apply_code_to_import(
            reference=f"ROOF {self.bob_pledge.match_code}",
            member=self.alice, dept=None, dev_group=None,
            campaign=None, campaign_group="", status="REVIEW",
            Transaction=Transaction)
        self.assertEqual(member, self.bob)
        self.assertEqual(dg, self.g3)
        self.assertTrue(via)
        self.assertEqual(dept, self.fund)

        gift = Transaction.objects.create(
            date=dt.date(2026, 6, 12), channel="BANK", direction="CREDIT",
            amount=Decimal("1500"), department=self.fund, member=self.bob,
            payer_name="ALICE MMC", payer_phone="254711111001",
            reference=f"ROOF {self.bob_pledge.match_code}",
            confirmed=True, allocation_status="AUTO",
            attributed_via_code=True, dev_group=self.g3)
        match_svc.handle_new_contribution(gift, user=self.user)
        self.assertEqual(self.bob_pledge.paid, Decimal("1500"))
        tallies = dev_group_members(self.g3)
        bob_row = next(r for r in tallies["rows"] if r["name"] == "BOB MMC")
        self.assertEqual(bob_row["total"], Decimal("1500"))
        self.assertIn("ALICE MMC", bob_row["via"])

    def test_campaign_code_also_credits_register_member_and_group(self):
        """CM… codes, once linked to a register Member, match MB… behaviour."""
        from giving.models import Campaign, CampaignMember
        camp = Campaign.objects.create(
            name="Camp MMC", department=self.fund,
            triggers="campmmc", active=True)
        CampaignMember.objects.create(
            campaign=camp, name="BOB MMC", phone="254722222002",
            group="CAMP_3")
        # Reload so match_code is populated
        cm = CampaignMember.objects.get(campaign=camp, phone="254722222002")
        self.assertTrue(cm.match_code)

        member, dept, dg, camp_out, cgrp, status, via = apply_code_to_import(
            reference=f"SUPPORT {cm.match_code}",
            member=self.alice, dept=None, dev_group=None,
            campaign=None, campaign_group="", status="REVIEW",
            Transaction=Transaction)
        self.assertEqual(member, self.bob)
        self.assertEqual(dg, self.g3)
        self.assertTrue(via)
        self.assertEqual(cgrp, "CAMP_3")
        self.assertEqual(camp_out, camp)

        gift = Transaction.objects.create(
            date=dt.date(2026, 6, 13), channel="BANK", direction="CREDIT",
            amount=Decimal("800"), department=dept or self.fund,
            member=self.bob, payer_name="ALICE MMC", payer_phone="254711111001",
            reference=f"SUPPORT {cm.match_code}",
            confirmed=True, allocation_status="AUTO",
            attributed_via_code=True, dev_group=self.g3,
            campaign=camp, campaign_group="CAMP_3")
        tallies = dev_group_members(self.g3)
        bob_row = next(r for r in tallies["rows"] if r["name"] == "BOB MMC")
        self.assertEqual(bob_row["total"], Decimal("800"))
        self.assertIn("ALICE MMC", bob_row["via"])
