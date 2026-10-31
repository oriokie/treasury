"""Member match codes attribute gifts to the intended person and their group."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase

from core.models import SiteConfig
from core.roles import TREASURER
from departments.models import Department, DevelopmentGroup
from giving.models import Campaign, CampaignMember, Transaction
from members.models import Member
from pledges.models import Pledge, PledgeCampaign
from pledges.services.attribution import apply_code_to_import
from pledges.services.codes import (
    find_member_by_code, resolve_code_attribution, resolve_mb_dev_group,
)
from pledges.services import matching as match_svc
from reports.services.balances import dev_group_members


class MemberMatchCodeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("mmc", password="x", is_superuser=True)
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(
            name="Development", fund_type="LOCAL", category="DEVELOPMENT")
        self.roof = Department.objects.create(
            name="Roof Fund MMC", fund_type="LOCAL", category="MINISTRY")
        self.g7 = DevelopmentGroup.objects.create(number=7, name="Group 7 MMC")
        self.g3 = DevelopmentGroup.objects.create(number=3, name="Group 3 MMC")
        self.alice = Member.objects.create(
            name="ALICE MMC", phone="254711111001", active=True, dev_group=self.g7)
        self.bob = Member.objects.create(
            name="BOB MMC", phone="254722222002", active=True, dev_group=self.g3)
        self.campaign = PledgeCampaign.objects.create(
            name="Roof MMC", target_department=self.roof,
            status=PledgeCampaign.Status.ACTIVE)
        self.bob_pledge = Pledge.objects.create(
            campaign=self.campaign, member=self.bob,
            amount=Decimal("10000"), start_date=dt.date(2026, 1, 1),
            status=Pledge.Status.ACTIVE)
        cfg = SiteConfig.get()
        cfg.pledge_match_mode = SiteConfig.PledgeMatchMode.AUTO
        cfg.pledge_match_same_fund_only = True
        cfg.mb_code_group_history = False
        cfg.save()

    def test_member_code_auto_assigned(self):
        self.assertTrue(self.alice.match_code.startswith("MB"))
        self.assertNotEqual(self.alice.match_code, self.bob.match_code)

    def test_find_member_by_code(self):
        self.assertEqual(
            find_member_by_code(f"GIFT {self.bob.match_code}"), self.bob)

    def test_mb_always_routes_to_development_not_pledge_campaign(self):
        hit = resolve_code_attribution(
            f"FOR {self.bob.match_code}", payer_member=self.alice)
        self.assertEqual(hit["beneficiary"], self.bob)
        self.assertEqual(hit["department"], self.fund)
        self.assertIsNone(hit["pledge"])
        self.assertEqual(hit["dev_group"], self.g3)
        self.assertEqual(hit["group_rule"], "home")

        member, dept, dg, camp, cgrp, status, via = apply_code_to_import(
            reference=f"FOR {self.bob.match_code}",
            member=self.alice, dept=None, dev_group=None,
            campaign=None, campaign_group="", status="REVIEW",
            Transaction=Transaction)
        self.assertEqual(member, self.bob)
        self.assertEqual(dg, self.g3)
        self.assertTrue(via)
        self.assertEqual(dept, self.fund)

        # Same-fund matching: Development gift does not fill a Roof pledge.
        gift = Transaction.objects.create(
            date=dt.date(2026, 6, 10), channel="BANK", direction="CREDIT",
            amount=Decimal("4000"), department=self.fund, member=self.bob,
            payer_name="ALICE MMC", payer_phone="254711111001",
            reference=f"FOR {self.bob.match_code}",
            confirmed=True, allocation_status="AUTO",
            attributed_via_code=True, dev_group=self.g3)
        match_svc.handle_new_contribution(gift, user=self.user)
        self.bob_pledge.refresh_from_db()
        self.assertEqual(self.bob_pledge.paid, Decimal("0"))

        tallies = dev_group_members(self.g3)
        bob_row = next(r for r in tallies["rows"] if r["name"] == "BOB MMC")
        self.assertEqual(bob_row["total"], Decimal("4000"))
        self.assertIn("ALICE MMC", bob_row["via"])
        alice_tallies = dev_group_members(self.g7)
        self.assertEqual(alice_tallies["total"], Decimal("0"))

    def test_mb_fills_pledge_only_when_campaign_fund_is_development(self):
        """When the pledge campaign targets Development, same-fund matching works."""
        self.campaign.target_department = self.fund
        self.campaign.save(update_fields=["target_department"])
        gift = Transaction.objects.create(
            date=dt.date(2026, 6, 11), channel="BANK", direction="CREDIT",
            amount=Decimal("2500"), department=self.fund, member=self.bob,
            payer_name="ALICE MMC", payer_phone="254711111001",
            reference=f"FOR {self.bob.match_code}",
            confirmed=True, allocation_status="AUTO",
            attributed_via_code=True, dev_group=self.g3)
        match_svc.handle_new_contribution(gift, user=self.user)
        self.bob_pledge.refresh_from_db()
        self.assertEqual(self.bob_pledge.paid, Decimal("2500"))

    def test_closed_pledge_campaign_ignored_for_mb_matching(self):
        self.campaign.target_department = self.fund
        self.campaign.status = PledgeCampaign.Status.CLOSED
        self.campaign.save(update_fields=["target_department", "status"])
        gift = Transaction.objects.create(
            date=dt.date(2026, 6, 11), channel="BANK", direction="CREDIT",
            amount=Decimal("900"), department=self.fund, member=self.bob,
            payer_name="ALICE MMC", payer_phone="254711111001",
            reference=f"FOR {self.bob.match_code}",
            confirmed=True, allocation_status="AUTO",
            attributed_via_code=True, dev_group=self.g3)
        match_svc.handle_new_contribution(gift, user=self.user)
        self.bob_pledge.refresh_from_db()
        self.assertEqual(self.bob_pledge.paid, Decimal("0"))

    def test_pledge_code_also_credits_group_and_shows_paid_by(self):
        """PG… codes still target that pledge's campaign fund."""
        member, dept, dg, camp, cgrp, status, via = apply_code_to_import(
            reference=f"ROOF {self.bob_pledge.match_code}",
            member=self.alice, dept=None, dev_group=None,
            campaign=None, campaign_group="", status="REVIEW",
            Transaction=Transaction)
        self.assertEqual(member, self.bob)
        self.assertEqual(dg, self.g3)
        self.assertTrue(via)
        self.assertEqual(dept, self.roof)

        gift = Transaction.objects.create(
            date=dt.date(2026, 6, 12), channel="BANK", direction="CREDIT",
            amount=Decimal("1500"), department=self.roof, member=self.bob,
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
        """CM… codes, once linked to a register Member, route via the sheet."""
        camp = Campaign.objects.create(
            name="Camp MMC", department=self.fund,
            triggers="campmmc", active=True)
        CampaignMember.objects.create(
            campaign=camp, name="BOB MMC", phone="254722222002",
            group="CAMP_3")
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

    def test_mb_prefers_active_campaign_sheet_group_over_home(self):
        camp = Campaign.objects.create(
            name="Sheet Pref MMC", department=self.fund,
            triggers="sheetpref", active=True)
        CampaignMember.objects.create(
            campaign=camp, name="BOB MMC", phone="254722222002",
            group="GRP7")
        hit = resolve_code_attribution(f"GIFT {self.bob.match_code}")
        self.assertEqual(hit["dev_group"], self.g7)
        self.assertEqual(hit["group_rule"], "campaign_sheet")
        self.assertEqual(hit["campaign"], camp)
        self.assertEqual(hit["department"], self.fund)

    def test_mb_ignores_inactive_campaign_sheet(self):
        camp = Campaign.objects.create(
            name="Inactive Sheet MMC", department=self.fund,
            triggers="inactivesheet", active=False)
        CampaignMember.objects.create(
            campaign=camp, name="BOB MMC", phone="254722222002",
            group="GRP7")
        hit = resolve_code_attribution(f"GIFT {self.bob.match_code}")
        self.assertEqual(hit["dev_group"], self.g3)
        self.assertEqual(hit["group_rule"], "home")
        self.assertIsNone(hit["campaign"])

    def test_mb_history_fallback_when_enabled_and_no_home_or_sheet(self):
        lonely = Member.objects.create(
            name="LONELY MMC", phone="254733333003", active=True, dev_group=None)
        Transaction.objects.create(
            date=dt.date.today() - dt.timedelta(days=10), channel="BANK",
            direction="CREDIT", amount=Decimal("100"), department=self.fund,
            member=lonely, confirmed=True, allocation_status="AUTO",
            dev_group=self.g7)
        Transaction.objects.create(
            date=dt.date.today() - dt.timedelta(days=5), channel="BANK",
            direction="CREDIT", amount=Decimal("500"), department=self.fund,
            member=lonely, confirmed=True, allocation_status="AUTO",
            dev_group=self.g3)

        cfg = SiteConfig.get()
        cfg.mb_code_group_history = False
        cfg.save(update_fields=["mb_code_group_history"])
        off = resolve_mb_dev_group(lonely)
        self.assertIsNone(off["dev_group"])
        self.assertEqual(off["group_rule"], "none")

        cfg.mb_code_group_history = True
        cfg.mb_code_group_history_months = 6
        cfg.save(update_fields=["mb_code_group_history",
                                "mb_code_group_history_months"])
        on = resolve_code_attribution(lonely.match_code)
        self.assertEqual(on["dev_group"], self.g3)
        self.assertEqual(on["group_rule"], "history")
        self.assertEqual(on["department"], self.fund)

    def test_code_matches_with_punctuation_and_extra_text(self):
        """Contains match survives hyphens / spaces around the code."""
        hit = resolve_code_attribution(
            f"FOR {self.bob.match_code[:2]}-{self.bob.match_code[2:]} THANKS",
            payer_member=self.alice)
        self.assertEqual(hit.get("beneficiary"), self.bob)
        self.assertEqual(hit.get("dev_group"), self.g3)

    def test_dev_group_code_auto_allocates_without_creating(self):
        """DEV… codes route to Development + that existing group."""
        self.assertTrue(self.g3.match_code.startswith("DEV"))
        member, dept, dg, camp, cgrp, status, via = apply_code_to_import(
            reference=f"GIFT {self.g3.match_code} EXTRA",
            member=self.alice, dept=None, dev_group=None,
            campaign=None, campaign_group="", status="REVIEW",
            Transaction=Transaction)
        self.assertEqual(dg, self.g3)
        self.assertIsNotNone(dept)
        self.assertEqual(dept.category, "DEVELOPMENT")
        self.assertEqual(status, "AUTO")
        self.assertFalse(via)  # group-only, not a person redirect
