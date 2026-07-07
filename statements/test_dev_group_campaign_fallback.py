"""Bug fix: the campaign member->group fallback (the mechanism that already
worked for sub-accounts like Camp Expense) never got a chance to run for
Development Groups. allocate() detects a dev-group WORD (e.g. "dev",
"grp") without a specific number and returns "DEV_GROUP_NA" with status
AUTO; _resolve() maps this to the generic Development department (never
None). Since the importer/reallocate_pending() only tried the campaign
fallback when dept was None, a DEV_GROUP_NA match short-circuited before
the campaign's member table (which could pin down the exact group from the
payer's name/phone) ever got the chance to run - even when a "Development"
campaign was configured exactly the same way Camp Expense is.

Fixed by also trying the campaign fallback specifically when the resolver
was DEV_GROUP_NA, preferring the campaign's more specific result only when
it actually recognises the payer (never downgrading an already-AUTO
resolution to REVIEW just because a trigger matched without a member)."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase
from departments.models import Department, DevelopmentGroup
from giving.models import Campaign, CampaignMember, Transaction
from giving.services.allocation import allocate, campaign_allocate, reallocate_pending
from statements.services.importer import _resolve


def _dev_dept():
    return Department.objects.filter(name__iexact="development", parent__isnull=True).first() \
        or Department.objects.create(name="DEVELOPMENT", fund_type="LOCAL", category="DEVELOPMENT")


def _simulate_row(reference, name, phone):
    """Mirrors the exact sequence in the fixed importer code block."""
    resolver, alloc_status = allocate(reference)
    dept, dev_group = _resolve(resolver)
    status = "AUTO_OR_LEARNED" if dept is not None else "REVIEW"
    dev_group_unknown = (resolver == "DEV_GROUP_NA")
    campaign = campaign_group = None
    if dept is None or dev_group_unknown:
        campaign, campaign_group, cdept, cstatus = campaign_allocate(reference, name, phone)
        if cdept is not None and (dept is None or cstatus == "AUTO"):
            dept = cdept
            status = cstatus
    return resolver, dept, dev_group, status, campaign, campaign_group


class DevGroupCampaignFallbackTests(TestCase):
    def setUp(self):
        self.dev_dept = _dev_dept()
        self.camp = Campaign.objects.create(name="DevFallbackTest", department=self.dev_dept,
            triggers="dev,grp,group", active=True)
        CampaignMember.objects.create(campaign=self.camp, name="MARY GIVER",
            phone="254799888777", group="27")

    def test_dev_word_with_known_member_resolves_to_specific_group(self):
        resolver, dept, dev_group, status, campaign, campaign_group = _simulate_row(
            "dev support", "MARY GIVER", "254799888777")
        self.assertEqual(resolver, "DEV_GROUP_NA")
        self.assertEqual(dept.name, "27")
        self.assertEqual(status, "AUTO")
        self.assertEqual(campaign.name, "DevFallbackTest")
        self.assertEqual(campaign_group, "27")

    def test_dev_word_with_unknown_giver_falls_back_to_generic_development(self):
        resolver, dept, dev_group, status, campaign, campaign_group = _simulate_row(
            "dev support", "TOTALLY UNKNOWN", "254700000000")
        self.assertEqual(dept.name.upper(), "DEVELOPMENT")
        self.assertEqual(status, "AUTO_OR_LEARNED")   # unaffected, not downgraded

    def test_numbered_dev_group_reference_unaffected(self):
        DevelopmentGroup.objects.get_or_create(number=12, defaults={"name": "TestGroup12"})
        resolver, dept, dev_group, status, campaign, campaign_group = _simulate_row(
            "grp12", "MARY GIVER", "254799888777")
        self.assertEqual(resolver, "DEV_GROUP_12")
        self.assertIsNotNone(dev_group)
        self.assertIsNone(campaign)   # never even attempted - not needed

    def test_unrelated_reference_unaffected(self):
        resolver, dept, dev_group, status, campaign, campaign_group = _simulate_row(
            "randomtext123", "NOBODY", "254711111111")
        self.assertIsNone(dept)
        self.assertEqual(status, "REVIEW")


class DevGroupCampaignFallbackReallocateTests(TestCase):
    """Same fix, applied to reallocate_pending() (used to clear the review
    queue after new rules/campaigns are added, without re-importing)."""
    def setUp(self):
        self.dev_dept = _dev_dept()
        self.camp = Campaign.objects.create(name="ReallocDevTest", department=self.dev_dept,
            triggers="dev,grp,group", active=True)
        CampaignMember.objects.create(campaign=self.camp, name="PETER GIVER",
            phone="254788777666", group="33")

    def test_reallocate_resolves_pending_dev_word_item_via_campaign(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 1), amount=Decimal("500"),
            direction="CREDIT", confirmed=True, channel="BANK",
            allocation_status="REVIEW", reference="dev gift",
            payer_name="PETER GIVER", payer_phone="254788777666")
        result = reallocate_pending()
        t.refresh_from_db()
        self.assertEqual(t.department.name, "33")
        self.assertEqual(t.allocation_status, "AUTO")
        self.assertEqual(t.campaign_id, self.camp.id)
        self.assertEqual(result["allocated"], 1)

    def test_reallocate_unknown_giver_falls_back_to_generic_development(self):
        t = Transaction.objects.create(date=dt.date(2026, 6, 2), amount=Decimal("300"),
            direction="CREDIT", confirmed=True, channel="BANK",
            allocation_status="REVIEW", reference="dev gift",
            payer_name="UNKNOWN PERSON", payer_phone="254700999888")
        reallocate_pending()
        t.refresh_from_db()
        self.assertEqual(t.department.name.upper(), "DEVELOPMENT")
        self.assertEqual(t.allocation_status, "AUTO")
