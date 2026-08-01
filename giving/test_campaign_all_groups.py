"""Writing to the whole campaign at once, and saying a group's number.

Two things a treasurer running a camp meeting asked for, and they belong
together. Sending the same notice to thirty groups meant composing it thirty
times — thirty presses, thirty chances to word it differently, and no single
record afterwards of what the campaign as a whole had been told. And the only
way to name a member's group in the text was `{group}`, which writes the name
off the sheet: "your group CAMP_1 meets at 9" when what the member should read
is "your group 1 meets at 9".

`{group_no}` is what makes ONE message work for every group — each member's own
number goes into their own copy — so the all-groups send is not a blunter
instrument than the per-group one, it is the same message personalised.
"""
from django.contrib.auth.models import Group, User
from django.test import Client, TestCase
from django.urls import reverse

from core.roles import TREASURER
from departments.models import Department

from .models import Campaign, CampaignMember, CampaignMessage
from .services import campaign_sms

ALL = campaign_sms.ALL_GROUPS


class _Campaign(TestCase):
    """A sheet shaped like a real one: groups named CAMP_n, one member the
    sheet forgot to group, one with no phone."""

    def setUp(self):
        self.treasurer = User.objects.create_user("allg", password="allg-pass-1")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(
            name="Camp All", slug="camp-all", fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY, selectable=True, active=True)
        self.campaign = Campaign.objects.create(name="Camp All 2026",
                                                department=self.fund)
        for name, phone, group in [
                ("Ann One", "254700000001", "CAMP_1"),
                ("Ben One", "254700000002", "CAMP_1"),
                ("Cate Two", "254700000003", "CAMP_2"),
                ("Dan Ten", "254700000004", "CAMP_10"),
                ("Eve NoPhone", "", "CAMP_2"),
                ("Fay Ungrouped", "254700000005", "")]:
            CampaignMember.objects.create(campaign=self.campaign, name=name,
                                          phone=phone, group=group)
        self.client = Client()
        self.client.post("/accounts/login/",
                         {"username": "allg", "password": "allg-pass-1"},
                         follow=True)

    def _post(self, group, message, confirm=False):
        data = {"group": group, "message": message}
        if confirm:
            data["confirm"] = "yes"
        return self.client.post(
            reverse("campaign_group_sms", args=[self.campaign.pk]), data)


class GroupNumberTests(TestCase):
    def test_it_takes_the_number_out_of_the_group_name(self):
        self.assertEqual(campaign_sms.group_number("CAMP_1"), "1")
        self.assertEqual(campaign_sms.group_number("Group 10"), "10")
        self.assertEqual(campaign_sms.group_number("2"), "2")

    def test_it_takes_the_first_run_of_digits_only(self):
        """Joining every digit in "CAMP_1_2" would produce group 12, which does
        not exist. The first run is the group; the rest is filing."""
        self.assertEqual(campaign_sms.group_number("CAMP_1_2"), "1")
        self.assertEqual(campaign_sms.group_number("CAMP_3B"), "3")

    def test_it_keeps_the_digits_as_written(self):
        """A sheet numbering its groups 01..30 has chosen that; turning "01"
        into "1" would quietly overrule it."""
        self.assertEqual(campaign_sms.group_number("CAMP_01"), "01")

    def test_a_group_with_no_number_has_none(self):
        self.assertEqual(campaign_sms.group_number("Youth"), "")
        self.assertEqual(campaign_sms.group_number(""), "")


class PlaceholderTests(_Campaign):
    def _render(self, template, member_name):
        member = self.campaign.members.get(name=member_name)
        return campaign_sms.render_message(template, member=member,
                                           campaign=self.campaign)

    def test_group_no_writes_just_the_number(self):
        self.assertEqual(self._render("Group {group_no} at 9am", "Ann One"),
                         "Group 1 at 9am")

    def test_group_still_writes_the_full_name(self):
        self.assertEqual(self._render("You are in {group}", "Ann One"),
                         "You are in CAMP_1")

    def test_both_can_appear_in_one_message(self):
        self.assertEqual(
            self._render("{group} is group {group_no}", "Dan Ten"),
            "CAMP_10 is group 10")

    def test_group_no_falls_back_to_the_name_when_there_is_no_number(self):
        """"your group  meets at 9" is a hole in the sentence. A named group
        should read as itself rather than as nothing."""
        member = self.campaign.members.get(name="Ann One")
        member.group = "Youth"
        member.save()
        self.assertEqual(self._render("your group {group_no}", "Ann One"),
                         "your group Youth")

    def test_the_older_placeholders_are_untouched(self):
        self.assertEqual(self._render("Dear {name}, from {campaign}", "Ann One"),
                         "Dear Ann One, from Camp All 2026")

    def test_group_no_is_offered_to_the_sender(self):
        self.assertIn("{group_no}", campaign_sms.PLACEHOLDERS)


class AllGroupsAudienceTests(_Campaign):
    def test_it_reaches_every_group(self):
        plan = campaign_sms.preview(self.campaign, ALL, "Hi {name}")
        self.assertEqual(plan["count"], 5)          # everyone with a phone
        self.assertEqual(plan["skipped_count"], 1)  # Eve has none

    def test_it_does_not_leave_out_the_ungrouped(self):
        """A member the sheet forgot to group is still a member of the
        campaign, and "everyone" has to mean everyone."""
        plan = campaign_sms.preview(self.campaign, ALL, "Hi")
        names = [r["member"].name for r in plan["recipients"]]
        self.assertIn("Fay Ungrouped", names)

    def test_one_message_says_each_members_own_group(self):
        """The whole point: one composition, personalised per member."""
        plan = campaign_sms.preview(self.campaign, ALL, "Group {group_no}")
        by_name = {r["member"].name: r["message"] for r in plan["recipients"]}
        self.assertEqual(by_name["Ann One"], "Group 1")
        self.assertEqual(by_name["Cate Two"], "Group 2")
        self.assertEqual(by_name["Dan Ten"], "Group 10")

    def test_a_single_group_send_is_unchanged(self):
        plan = campaign_sms.preview(self.campaign, "CAMP_1", "Hi")
        self.assertEqual(plan["count"], 2)

    def test_all_groups_is_not_the_same_as_the_ungrouped(self):
        """"" already means "the members with no group recorded". If the two
        collided, asking for everyone would write to one person."""
        everyone = campaign_sms.preview(self.campaign, ALL, "Hi")
        ungrouped = campaign_sms.preview(self.campaign, "", "Hi")
        self.assertEqual(ungrouped["count"], 1)
        self.assertGreater(everyone["count"], ungrouped["count"])

    def test_the_breakdown_covers_every_group(self):
        plan = campaign_sms.preview(self.campaign, ALL, "Hi")
        rows = {r["name"]: r for r in campaign_sms.breakdown(plan)}
        self.assertEqual(rows["CAMP_1"]["count"], 2)
        self.assertEqual(rows["CAMP_2"]["count"], 1)
        self.assertEqual(rows["CAMP_2"]["skipped"], 1)   # Eve
        self.assertEqual(rows["CAMP_10"]["count"], 1)

    def test_the_breakdown_sorts_numerically(self):
        plan = campaign_sms.preview(self.campaign, ALL, "Hi")
        names = [r["name"] for r in campaign_sms.breakdown(plan)]
        self.assertLess(names.index("CAMP_2"), names.index("CAMP_10"))


class AllGroupsSendTests(_Campaign):
    def test_nothing_is_sent_without_confirming(self):
        self._post(ALL, "Hi {name}")
        self.assertFalse(CampaignMessage.objects.exists())

    def test_the_confirmation_shows_the_real_count(self):
        response = self._post(ALL, "Hi {name}")
        self.assertEqual(response.context["plan"]["count"], 5)
        self.assertTrue(response.context["all_groups"])
        self.assertContains(response, "every group")

    def test_confirming_records_one_whole_campaign_send(self):
        self._post(ALL, "Hi {name}", confirm=True)
        record = CampaignMessage.objects.get()
        self.assertEqual(record.group, ALL)
        self.assertEqual(record.intended_count, 5)
        self.assertEqual(record.skipped_count, 1)

    def test_a_whole_campaign_send_shows_in_a_single_groups_history(self):
        """Group 2 HAS been written to. Showing them as never contacted on the
        day everybody was contacted is how the same notice goes out twice."""
        self._post(ALL, "Hi {name}", confirm=True)
        history = campaign_sms.recent_sends(self.campaign, "CAMP_2")
        self.assertEqual(len(history), 1)

    def test_repeating_it_to_one_group_is_flagged_as_a_duplicate(self):
        self._post(ALL, "Hi {name}", confirm=True)
        response = self._post("CAMP_2", "Hi {name}")
        self.assertIsNotNone(
            response.context["duplicate"],
            "the same words already went to this group in the all-groups send")

    def test_a_per_group_send_does_not_look_like_an_all_groups_one(self):
        self._post("CAMP_1", "Hi {name}", confirm=True)
        self.assertEqual(campaign_sms.recent_sends(self.campaign, ALL), [])

    def test_the_group_label_names_the_audience(self):
        self.assertEqual(campaign_sms.group_label(ALL), "every group")
        self.assertEqual(campaign_sms.group_label("CAMP_1"), "CAMP_1")
        self.assertEqual(campaign_sms.group_label(""), "No group recorded")


class UngroupedGapTests(_Campaign):
    """A member the sheet never grouped, in a message that names the group.

    `{group_no}` falls back to the group's NAME, which covers a group called
    "Youth". A member with no group at all has neither, so the text goes out
    reading "your group is " with a hole in it. Not worth blocking — the sheet
    is the church's, and a reminder is still better than none — but the sender
    has to see it, and an all-groups send is the first time they plausibly
    would not.
    """

    def test_the_gap_is_reported(self):
        plan = campaign_sms.preview(self.campaign, ALL, "Group {group_no}")
        gaps = campaign_sms.gap_warning(plan, "Group {group_no}")
        self.assertEqual([m.name for m in gaps], ["Fay Ungrouped"])

    def test_group_counts_as_well_as_group_no(self):
        plan = campaign_sms.preview(self.campaign, ALL, "You are in {group}")
        self.assertTrue(campaign_sms.gap_warning(plan, "You are in {group}"))

    def test_a_message_that_names_no_group_has_no_gap(self):
        plan = campaign_sms.preview(self.campaign, ALL, "Hello {name}")
        self.assertEqual(campaign_sms.gap_warning(plan, "Hello {name}"), [])

    def test_a_named_group_with_no_number_is_not_a_gap(self):
        """"Youth" reads perfectly well; the fallback handles it."""
        member = self.campaign.members.get(name="Ann One")
        member.group = "Youth"
        member.save()
        plan = campaign_sms.preview(self.campaign, "Youth", "Group {group_no}")
        self.assertEqual(campaign_sms.gap_warning(plan, "Group {group_no}"), [])

    def test_the_sender_is_warned_on_the_confirmation(self):
        response = self._post(ALL, "Your group is {group_no}")
        self.assertContains(response, "no group")
        self.assertContains(response, "Fay Ungrouped")

    def test_it_is_a_warning_and_not_a_block(self):
        self._post(ALL, "Your group is {group_no}", confirm=True)
        self.assertTrue(CampaignMessage.objects.exists())


class AllGroupsPermissionTests(_Campaign):
    def test_it_is_treasurer_only_like_every_other_send(self):
        """The button is new; the rule it sits under is not. This is still the
        action that costs money on every press."""
        from core.roles import ASSISTANT
        clerk = User.objects.create_user("allg_clerk", password="allg-pass-2")
        clerk.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
        c = Client()
        c.post("/accounts/login/",
               {"username": "allg_clerk", "password": "allg-pass-2"}, follow=True)
        response = c.post(
            reverse("campaign_group_sms", args=[self.campaign.pk]),
            {"group": ALL, "message": "Hi", "confirm": "yes"})
        self.assertIn(response.status_code, (302, 403))
        self.assertFalse(CampaignMessage.objects.exists())

    def test_the_button_is_not_offered_to_a_reader(self):
        from core.roles import AUDITOR
        reader = User.objects.create_user("allg_read", password="allg-pass-3")
        reader.groups.add(Group.objects.get_or_create(name=AUDITOR)[0])
        c = Client()
        c.post("/accounts/login/",
               {"username": "allg_read", "password": "allg-pass-3"}, follow=True)
        body = c.get(reverse("campaign_detail", args=[self.campaign.pk])).content.decode()
        self.assertNotIn("Send one message to every group", body)
