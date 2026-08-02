"""Thanking the groups that reached their target.

The mirror of the existing "chase the ones behind": same audience machinery,
opposite half of the same question. A church whose members only hear from the
treasurer when they are short is teaching them what a message from the
treasurer means.
"""
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import TREASURER
from departments.models import Department
from giving.models import Campaign, Transaction
from giving.services import campaign_sms


def _treasurer(username="camp_tr"):
    u = User.objects.create_user(username, password="x")
    u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
    return u


class _Seed(TestCase):
    """Two groups with targets — one finished, one short — and one with no
    target at all."""

    def setUp(self):
        self.user = _treasurer()
        self.parent = Department.objects.create(name="Camp Meeting",
                                                fund_type="LOCAL")
        self.campaign = Campaign.objects.create(name="Camp 2026",
                                                department=self.parent)
        self.done = Department.objects.create(
            name="CAMP_1", parent=self.parent, fund_type="LOCAL",
            contribution_goal=Decimal("10000"))
        self.short = Department.objects.create(
            name="CAMP_2", parent=self.parent, fund_type="LOCAL",
            contribution_goal=Decimal("10000"))
        self.untargeted = Department.objects.create(
            name="CAMP_3", parent=self.parent, fund_type="LOCAL")
        Transaction.objects.create(
            date="2026-05-01", channel="CASH", direction="CREDIT",
            amount=Decimal("10000"), department=self.done,
            allocation_status="MANUAL", confirmed=True)
        Transaction.objects.create(
            date="2026-05-01", channel="CASH", direction="CREDIT",
            amount=Decimal("2000"), department=self.short,
            allocation_status="MANUAL", confirmed=True)
        for name, group in (("Ann", "CAMP_1"), ("Ben", "CAMP_2"),
                            ("Cee", "CAMP_3")):
            self.campaign.members.create(name=name, group=group,
                                         phone="07000000%02d" % len(name))


class TargetMetGroupsTests(_Seed):
    def test_only_the_group_that_reached_its_target(self):
        self.assertEqual(campaign_sms.target_met_groups(self.campaign),
                         ["CAMP_1"])

    def test_a_group_with_no_target_is_not_counted_as_finished(self):
        """Nobody cleared a bar nobody put up."""
        self.assertNotIn("CAMP_3",
                         campaign_sms.target_met_groups(self.campaign))

    def test_it_is_the_exact_complement_of_behind(self):
        met = set(campaign_sms.target_met_groups(self.campaign))
        behind = set(campaign_sms.behind_target_groups(self.campaign))
        self.assertEqual(met & behind, set())
        targeted = {r["name"] for r in campaign_sms.group_progress(self.campaign)
                    if r["has_target"]}
        self.assertEqual(met | behind, targeted)

    def test_exactly_on_target_counts_as_met(self):
        """10,000 raised against a 10,000 target is finished, not short."""
        self.assertIn("CAMP_1", campaign_sms.target_met_groups(self.campaign))


class AudienceTests(_Seed):
    def test_only_members_of_finished_groups_are_written_to(self):
        names = [m.name for m in
                 campaign_sms.audience(self.campaign, campaign_sms.TARGET_MET)]
        self.assertEqual(names, ["Ann"])

    def test_the_chase_audience_is_unchanged(self):
        names = [m.name for m in
                 campaign_sms.audience(self.campaign, campaign_sms.BEHIND_TARGET)]
        self.assertEqual(names, ["Ben"])

    def test_each_group_gets_its_own_figures(self):
        preview = campaign_sms.preview(
            self.campaign, campaign_sms.TARGET_MET,
            "Thanks {name}, group {group_no} raised {collected} of {goal}.")
        self.assertEqual(len(preview["recipients"]), 1)
        message = preview["recipients"][0]["message"]
        self.assertIn("10,000", message)
        self.assertIn("group 1", message)

    def test_the_send_is_labelled_distinctly_in_history(self):
        self.assertEqual(campaign_sms.group_label(campaign_sms.TARGET_MET),
                         "the groups that reached target")
        self.assertNotEqual(campaign_sms.TARGET_MET, campaign_sms.BEHIND_TARGET)
        self.assertNotEqual(campaign_sms.TARGET_MET, campaign_sms.ALL_GROUPS)


class PageTests(_Seed):
    def test_the_thank_button_appears_for_a_treasurer(self):
        self.client.force_login(self.user)
        r = self.client.get(reverse("campaign_detail", args=[self.campaign.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Thank the 1 group that finished")
        # the sentinel reaches the form as the hidden group value (escaped)
        from django.utils.html import escape
        self.assertContains(r, escape(campaign_sms.TARGET_MET))

    def test_the_page_reports_what_the_finished_groups_raised(self):
        self.client.force_login(self.user)
        r = self.client.get(reverse("campaign_detail", args=[self.campaign.pk]))
        self.assertEqual(r.context["met_raised"], Decimal("10000"))
        self.assertEqual([g["name"] for g in r.context["met"]], ["CAMP_1"])

    def test_no_thank_button_when_no_group_has_finished(self):
        Transaction.objects.filter(department=self.done).delete()
        self.client.force_login(self.user)
        r = self.client.get(reverse("campaign_detail", args=[self.campaign.pk]))
        self.assertNotContains(r, "that finished</button>")
