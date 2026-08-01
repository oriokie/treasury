"""Chasing only the campaign groups still short of their target.

The targets already existed: each group's sub-account carries a
`contribution_goal`, set and shown on the fund's budget page. Nothing connected
them to the people who could actually close the gap — a treasurer read the
budget page, wrote down which groups were behind, then went to the campaign page
and messaged each one by hand.

So this reads that same figure (never writes it, never creates the fund — a
treasurer asking who is behind must not bring funds into existence) and offers
one send to exactly the groups that are short, with each group's own numbers in
its own copy of the message.

A group with no target set is not "behind". Nobody is behind a target nobody
set, and chasing them for it would be the church's own omission arriving as the
member's failing.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase
from django.urls import reverse

from core.roles import TREASURER
from departments.models import Department
from giving.models import Campaign, CampaignMember, CampaignMessage, Transaction
from giving.services import campaign_sms

BEHIND = campaign_sms.BEHIND_TARGET


class _Targets(TestCase):
    """Three groups: one has met its target, one is halfway, one has nothing.
    A fourth has no target at all."""

    def setUp(self):
        self.tr = User.objects.create_user("ct_tr", password="ct-pass-1",
                                           is_superuser=True)
        self.tr.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(
            name="CtCamp", slug="ct-camp", fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY)
        self.campaign = Campaign.objects.create(name="Ct Camp 2026",
                                                department=self.fund)
        self.subs = {}
        for name, goal, raised in [("CT_1", 10000, 10000),
                                   ("CT_2", 10000, 4000),
                                   ("CT_3", 8000, 0)]:
            self.subs[name] = self._sub(name, goal, raised)
        self._sub("CT_9", 0, 0)          # a group with no target set

        for name, phone, group in [
                ("Ann A", "254700000301", "CT_1"),
                ("Ben B", "254700000302", "CT_2"),
                ("Cate C", "254700000303", "CT_2"),
                ("Dan D", "254700000304", "CT_3"),
                ("Eve E", "", "CT_3"),
                ("Fay F", "254700000306", "CT_9")]:
            CampaignMember.objects.create(campaign=self.campaign, name=name,
                                          phone=phone, group=group)
        self.client = Client()
        self.client.force_login(self.tr)

    def _sub(self, name, goal, raised):
        d = Department.objects.create(
            name=name, slug=name.lower().replace("_", "-"), parent=self.fund,
            category=Department.Category.MINISTRY,
            contribution_goal=Decimal(goal))
        if raised:
            Transaction.objects.create(
                date=dt.date.today(), amount=Decimal(raised), direction="CREDIT",
                channel="BANK", confirmed=True, allocation_status="MANUAL",
                department=d, excluded_from_income=False)
        return d

    def _post(self, group, message, confirm=False):
        data = {"group": group, "message": message}
        if confirm:
            data["confirm"] = "yes"
        return self.client.post(
            reverse("campaign_group_sms", args=[self.campaign.pk]), data)

    def _rows(self):
        return {r["name"]: r for r in campaign_sms.group_progress(self.campaign)}


class ProgressTests(_Targets):
    def test_it_reads_the_target_from_the_groups_own_fund(self):
        self.assertEqual(self._rows()["CT_2"]["goal"], Decimal("10000"))

    def test_it_totals_what_the_group_has_raised(self):
        self.assertEqual(self._rows()["CT_2"]["collected"], Decimal("4000"))

    def test_the_shortfall_is_the_difference(self):
        self.assertEqual(self._rows()["CT_2"]["short"], Decimal("6000"))

    def test_a_group_that_met_its_target_is_not_behind(self):
        self.assertFalse(self._rows()["CT_1"]["behind"])

    def test_a_group_with_no_target_is_not_behind(self):
        """Nobody is behind a target nobody set."""
        row = self._rows()["CT_9"]
        self.assertFalse(row["has_target"])
        self.assertFalse(row["behind"])

    def test_a_group_with_no_fund_at_all_is_not_behind(self):
        CampaignMember.objects.create(campaign=self.campaign, name="Gil G",
                                      phone="254700000307", group="CT_NOFUND")
        row = self._rows()["CT_NOFUND"]
        self.assertIsNone(row["fund"])
        self.assertFalse(row["behind"])

    def test_over_collecting_never_reports_a_negative_shortfall(self):
        Transaction.objects.create(
            date=dt.date.today(), amount=Decimal("5000"), direction="CREDIT",
            channel="BANK", confirmed=True, allocation_status="MANUAL",
            department=self.subs["CT_1"], excluded_from_income=False)
        self.assertEqual(self._rows()["CT_1"]["short"], Decimal(0))

    def test_a_reversed_gift_does_not_count_towards_the_target(self):
        """The same definition of counted money the rest of the app uses."""
        Transaction.objects.create(
            date=dt.date.today(), amount=Decimal("9999"), direction="CREDIT",
            channel="BANK", confirmed=True, allocation_status="MANUAL",
            department=self.subs["CT_3"], excluded_from_income=False,
            is_reversed=True)
        self.assertEqual(self._rows()["CT_3"]["collected"], Decimal(0))

    def test_asking_who_is_behind_creates_no_funds(self):
        """`Campaign.subgroup_department` makes a fund on demand, which is right
        when money arrives and quite wrong when someone is only looking."""
        before = Department.objects.count()
        campaign_sms.group_progress(self.campaign)
        self.assertEqual(Department.objects.count(), before)

    def test_it_names_exactly_the_groups_behind(self):
        self.assertEqual(set(campaign_sms.behind_target_groups(self.campaign)),
                         {"CT_2", "CT_3"})


class AudienceTests(_Targets):
    def test_only_the_groups_behind_are_written_to(self):
        plan = campaign_sms.preview(self.campaign, BEHIND, "Hi {name}")
        groups = {r["group"] for r in plan["recipients"]}
        self.assertEqual(groups, {"CT_2", "CT_3"})

    def test_a_group_that_met_its_target_is_left_alone(self):
        plan = campaign_sms.preview(self.campaign, BEHIND, "Hi")
        self.assertNotIn("Ann A", [r["member"].name for r in plan["recipients"]])

    def test_a_member_with_no_phone_is_still_reported(self):
        plan = campaign_sms.preview(self.campaign, BEHIND, "Hi")
        self.assertEqual([r["member"].name for r in plan["skipped"]], ["Eve E"])

    def test_each_group_gets_its_own_figures(self):
        """One composition, different numbers per group — the whole point."""
        plan = campaign_sms.preview(
            self.campaign, BEHIND, "raised {collected} of {goal}, need {short}")
        by_name = {r["member"].name: r["message"] for r in plan["recipients"]}
        self.assertEqual(by_name["Ben B"], "raised 4,000 of 10,000, need 6,000")
        self.assertEqual(by_name["Dan D"], "raised 0 of 8,000, need 8,000")

    def test_money_reads_as_a_sentence_not_a_ledger(self):
        plan = campaign_sms.preview(self.campaign, BEHIND, "{short}")
        self.assertIn("6,000", [r["message"] for r in plan["recipients"]])

    def test_the_figures_resolve_even_outside_a_behind_send(self):
        """A member must never receive a text with a raw {short} in it."""
        plan = campaign_sms.preview(self.campaign, "CT_1", "need {short}")
        self.assertEqual([r["message"] for r in plan["recipients"]], ["need 0"])


class SendTests(_Targets):
    def test_nothing_is_sent_without_confirming(self):
        self._post(BEHIND, "Hi {name}")
        self.assertFalse(CampaignMessage.objects.exists())

    def test_the_confirmation_says_it_is_only_the_ones_behind(self):
        r = self._post(BEHIND, "Need {short}")
        self.assertTrue(r.context["behind_send"])
        self.assertContains(r, "still short of target")

    def test_it_records_which_audience_was_chosen(self):
        """Stored as its own audience, not as a group name: who "behind target"
        meant depends on the money at the time it was pressed."""
        self._post(BEHIND, "Need {short}", confirm=True)
        self.assertEqual(CampaignMessage.objects.get().group, BEHIND)

    def test_the_send_covers_the_reachable_members_behind(self):
        self._post(BEHIND, "Need {short}", confirm=True)
        record = CampaignMessage.objects.get()
        self.assertEqual(record.intended_count, 3)   # Ben, Cate, Dan
        self.assertEqual(record.skipped_count, 1)    # Eve has no phone

    def test_it_is_treasurer_only_like_every_other_send(self):
        from core.roles import ASSISTANT
        clerk = User.objects.create_user("ct_clerk", password="ct-pass-2")
        clerk.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
        c = Client()
        c.force_login(clerk)
        r = c.post(reverse("campaign_group_sms", args=[self.campaign.pk]),
                   {"group": BEHIND, "message": "Hi", "confirm": "yes"})
        self.assertIn(r.status_code, (302, 403))
        self.assertFalse(CampaignMessage.objects.exists())


class PageTests(_Targets):
    def test_the_campaign_page_shows_each_groups_progress(self):
        body = self.client.get(
            reverse("campaign_detail", args=[self.campaign.pk])).content.decode()
        self.assertIn("Against target", body)
        self.assertIn("Target met", body)

    def test_it_says_how_many_groups_are_behind_and_by_how_much(self):
        """A "remind the ones behind" button that does not say who is behind is
        asking for a blind press, and this one costs money."""
        r = self.client.get(reverse("campaign_detail", args=[self.campaign.pk]))
        self.assertEqual(len(r.context["behind"]), 2)
        self.assertEqual(r.context["behind_short"], Decimal("14000"))

    def test_the_section_is_hidden_when_no_targets_are_set(self):
        """A church that has not set targets should not be shown an empty table
        telling it nothing."""
        Department.objects.filter(parent=self.fund).update(contribution_goal=0)
        r = self.client.get(reverse("campaign_detail", args=[self.campaign.pk]))
        self.assertFalse(r.context["any_targets"])
        self.assertNotContains(r, "Against target")

    def test_it_links_to_the_budget_page_the_targets_come_from(self):
        body = self.client.get(
            reverse("campaign_detail", args=[self.campaign.pk])).content.decode()
        self.assertIn(reverse("fund_budget", args=[self.fund.id]), body)
