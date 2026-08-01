"""The campaign page: seeing the uploaded sheet, and writing to one group.

Bulk SMS is the only action in this application that costs money on every press
and cannot be recalled. So most of what is pinned here is restraint — that
nothing is sent without an explicit confirmation, that the number on the button
is the real number, and that a member with no phone is reported rather than
quietly dropped.
"""
from django.contrib.auth.models import Group, User
from django.test import Client, TestCase
from django.urls import reverse

from core.roles import ASSISTANT, TREASURER
from departments.models import Department

from .models import Campaign, CampaignMember
from .services import campaign_sms


class CampaignBase(TestCase):
    def setUp(self):
        self.treasurer = User.objects.create_user("camp", password="camp-pass-1")
        self.treasurer.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(
            name="Camp Meeting", slug="camp-meeting",
            fund_type=Department.FundType.LOCAL,
            category=Department.Category.MINISTRY, selectable=True, active=True)
        self.campaign = Campaign.objects.create(
            name="Camp Meeting 2026", department=self.fund)
        for name, phone, group in [
                ("Ruth Momanyi", "254790301470", "Group 2"),
                ("Kevin Ogega", "254716804186", "Group 10"),
                ("Mary Otieno", "254700000011", "Group 2"),
                ("No Phone Person", "", "Group 2"),
                ("Ungrouped Person", "254700000012", "")]:
            CampaignMember.objects.create(campaign=self.campaign, name=name,
                                          phone=phone, group=group)
        self.client = Client()
        self.client.get("/accounts/login/")
        self.client.post("/accounts/login/",
                         {"username": "camp", "password": "camp-pass-1"}, follow=True)


class CampaignGroupsTests(CampaignBase):
    def test_the_sheet_is_grouped(self):
        groups = campaign_sms.groups_for(self.campaign)
        names = [g["name"] for g in groups]
        self.assertIn("Group 2", names)
        self.assertIn("Group 10", names)

    def test_numeric_groups_sort_as_numbers(self):
        """'Group 2' before 'Group 10' — a plain text sort gets this wrong, and
        a treasurer scanning for group 9 in a list of forty will not find it."""
        names = [g["name"] for g in campaign_sms.groups_for(self.campaign)]
        self.assertLess(names.index("Group 2"), names.index("Group 10"))

    def test_members_with_no_group_are_kept_not_dropped(self):
        """The member the sheet forgot to group is exactly the one somebody
        needs to find."""
        groups = campaign_sms.groups_for(self.campaign)
        ungrouped = [g for g in groups if g["name"] == ""]
        self.assertEqual(len(ungrouped), 1)
        self.assertEqual(ungrouped[0]["count"], 1)

    def test_reachability_is_counted_per_group(self):
        groups = {g["name"]: g for g in campaign_sms.groups_for(self.campaign)}
        self.assertEqual(groups["Group 2"]["count"], 3)
        self.assertEqual(groups["Group 2"]["reachable"], 2)
        self.assertEqual(groups["Group 2"]["unreachable"], 1)

    def test_the_detail_page_renders(self):
        response = self.client.get(
            reverse("campaign_detail", args=[self.campaign.pk]))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Group 2", body)
        self.assertIn("Ruth Momanyi", body)


class CampaignMessageTests(CampaignBase):
    def test_placeholders_are_filled_per_member(self):
        plan = campaign_sms.preview(
            self.campaign, "Group 2",
            "Dear {name}, {group} meets on Sabbath. — {campaign}")
        messages_out = {r["member"].name: r["message"] for r in plan["recipients"]}
        self.assertIn("Dear Ruth Momanyi, Group 2 meets on Sabbath. "
                      "— Camp Meeting 2026", messages_out["Ruth Momanyi"])

    def test_preview_separates_reachable_from_not(self):
        plan = campaign_sms.preview(self.campaign, "Group 2", "Hello {name}")
        self.assertEqual(plan["count"], 2)
        self.assertEqual(plan["skipped_count"], 1)
        self.assertEqual(plan["skipped"][0]["member"].name, "No Phone Person")

    def test_only_the_named_group_is_included(self):
        plan = campaign_sms.preview(self.campaign, "Group 10", "Hi {name}")
        self.assertEqual([r["member"].name for r in plan["recipients"]],
                         ["Kevin Ogega"])


class CampaignSendGuardTests(CampaignBase):
    """Nothing goes out without an explicit, informed confirmation."""

    def test_posting_a_message_only_previews_it(self):
        from core.models import SmsLog
        response = self.client.post(
            reverse("campaign_group_sms", args=[self.campaign.pk]),
            {"group": "Group 2", "message": "Hello {name}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(SmsLog.objects.count(), 0,
                         "A message was sent without confirmation.")
        self.assertContains(response, "Send 2 message")

    def test_the_count_on_the_button_is_the_real_count(self):
        """The confirmation is built from the same resolution the send uses, so
        it cannot promise one number and deliver another."""
        response = self.client.post(
            reverse("campaign_group_sms", args=[self.campaign.pk]),
            {"group": "Group 2", "message": "Hello"})
        plan = response.context["plan"]
        result = campaign_sms.send(self.campaign, "Group 2", "Hello")
        self.assertEqual(plan["count"], result["sent"] + result["failed"])

    def test_confirming_records_one_log_row_per_message(self):
        from core.models import SmsLog
        self.client.post(
            reverse("campaign_group_sms", args=[self.campaign.pk]),
            {"group": "Group 2", "message": "Hello {name}", "confirm": "yes"})
        self.assertEqual(SmsLog.objects.count(), 2,
                         "Every message must leave a log row, sent or not.")

    def test_an_empty_message_is_refused(self):
        from core.models import SmsLog
        self.client.post(
            reverse("campaign_group_sms", args=[self.campaign.pk]),
            {"group": "Group 2", "message": "   ", "confirm": "yes"})
        self.assertEqual(SmsLog.objects.count(), 0)

    def test_an_assistant_cannot_send(self):
        """It costs money and cannot be recalled, so it sits with the role that
        answers for spending."""
        from core.models import SmsLog
        assistant = User.objects.create_user("ass", password="a-pass-1")
        assistant.groups.add(Group.objects.get_or_create(name=ASSISTANT)[0])
        client = Client()
        client.get("/accounts/login/")
        client.post("/accounts/login/",
                    {"username": "ass", "password": "a-pass-1"}, follow=True)
        client.post(reverse("campaign_group_sms", args=[self.campaign.pk]),
                    {"group": "Group 2", "message": "Hi", "confirm": "yes"})
        self.assertEqual(SmsLog.objects.count(), 0,
                         "An assistant was able to send campaign SMS.")

    def test_a_group_with_nobody_reachable_is_refused_early(self):
        CampaignMember.objects.filter(campaign=self.campaign).update(phone="")
        response = self.client.post(
            reverse("campaign_group_sms", args=[self.campaign.pk]),
            {"group": "Group 2", "message": "Hello"}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "nothing to send")


class CampaignSendHistoryTests(CampaignBase):
    """What has already gone out, so it does not go out twice by accident.

    `SmsLog` records every individual message, but as unrelated rows with no
    idea which campaign or group produced them — so the question a treasurer
    actually asks, "have we already told Group 2 about this?", had no answer and
    nothing prevented a duplicate. A congregation receiving the same appeal
    twice reads it as disorganisation, and the church pays for both.
    """

    def _send(self, body="Hello {name}", group="Group 2"):
        return self.client.post(
            reverse("campaign_group_sms", args=[self.campaign.pk]),
            {"group": group, "message": body, "confirm": "yes"})

    def test_a_send_is_recorded_against_the_group(self):
        from .models import CampaignMessage
        self._send()
        record = CampaignMessage.objects.get()
        self.assertEqual(record.campaign, self.campaign)
        self.assertEqual(record.group, "Group 2")
        self.assertEqual(record.sent_by, self.treasurer)
        self.assertEqual(record.skipped_count, 1,
                         "The member with no phone should be counted, not lost.")

    def test_the_counts_recorded_match_what_happened(self):
        from .models import CampaignMessage
        self._send()
        record = CampaignMessage.objects.get()
        self.assertEqual(record.sent_count + record.failed_count, 2)

    def test_an_identical_repeat_is_flagged_before_sending(self):
        self._send()
        response = self.client.post(
            reverse("campaign_group_sms", args=[self.campaign.pk]),
            {"group": "Group 2", "message": "Hello {name}"})
        self.assertIsNotNone(response.context["duplicate"])
        # The warning has to name WHICH audience already had it, now that a
        # send can be addressed either to one group or to the whole campaign.
        self.assertContains(response, "already gone out to this group")

    def test_a_repeat_is_warned_about_but_not_blocked(self):
        """A reminder is sometimes meant to be repeated. The decision belongs to
        the treasurer; the information belongs on the screen."""
        from .models import CampaignMessage
        self._send()
        self._send()
        self.assertEqual(CampaignMessage.objects.count(), 2)

    def test_a_different_message_is_not_flagged_as_a_duplicate(self):
        self._send(body="Hello {name}")
        response = self.client.post(
            reverse("campaign_group_sms", args=[self.campaign.pk]),
            {"group": "Group 2", "message": "Something else entirely"})
        self.assertIsNone(response.context["duplicate"])

    def test_the_same_message_to_a_different_group_is_not_a_duplicate(self):
        self._send(group="Group 2")
        response = self.client.post(
            reverse("campaign_group_sms", args=[self.campaign.pk]),
            {"group": "Group 10", "message": "Hello {name}"})
        self.assertIsNone(response.context["duplicate"])

    def test_the_campaign_page_shows_what_has_been_sent(self):
        self._send()
        body = self.client.get(
            reverse("campaign_detail", args=[self.campaign.pk])).content.decode()
        self.assertIn("Already sent to this group", body)


class CampaignInterruptedSendTests(CampaignBase):
    """A send that does not finish must still say what it did.

    The failure this guards against: a large group is sent inside a web request,
    the server's timeout fires part-way through, and — because the record used
    to be written only after the loop — the messages that had already gone left
    no trace at all. The treasurer sees a failed page and has no way to know
    whether resending would double up.
    """

    def test_the_record_exists_before_the_first_message_goes(self):
        """So an interruption at message one is still visible."""
        from unittest.mock import patch
        from .models import CampaignMessage

        seen = {}

        def spy(*args, **kwargs):
            seen["count"] = CampaignMessage.objects.count()
            raise RuntimeError("network died")

        with patch("core.services.sms.send_sms", side_effect=spy):
            with self.assertRaises(RuntimeError):
                campaign_sms.send(self.campaign, "Group 2", "Hello {name}")

        self.assertEqual(seen.get("count"), 1,
                         "The send record was not opened before sending began.")

    def test_an_interrupted_send_is_marked_and_keeps_its_counts(self):
        from unittest.mock import patch
        from .models import CampaignMessage

        calls = {"n": 0}

        def flaky(to, message, cfg=None):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("network died")
            class _Log:
                status = "SENT"
            return _Log()

        with patch("core.services.sms.send_sms", side_effect=flaky):
            with self.assertRaises(RuntimeError):
                campaign_sms.send(self.campaign, "Group 2", "Hello {name}")

        record = CampaignMessage.objects.get()
        self.assertEqual(record.state, CampaignMessage.State.INTERRUPTED)
        self.assertEqual(record.sent_count, 1,
                         "The message that did go out was not recorded.")
        self.assertEqual(record.intended_count, 2)
        self.assertTrue(record.is_incomplete)

    def test_a_completed_send_is_marked_done(self):
        from .models import CampaignMessage
        campaign_sms.send(self.campaign, "Group 2", "Hello {name}")
        record = CampaignMessage.objects.get()
        self.assertEqual(record.state, CampaignMessage.State.DONE)
        self.assertFalse(record.is_incomplete)

    def test_the_page_shows_an_interrupted_send_as_such(self):
        """A bare "already sent" note would mislead a treasurer into NOT
        resending, which is the one case where they should."""
        from .models import CampaignMessage
        CampaignMessage.objects.create(
            campaign=self.campaign, group="Group 2", body="Hello",
            sent_count=1, intended_count=2,
            state=CampaignMessage.State.INTERRUPTED)
        body = self.client.get(
            reverse("campaign_detail", args=[self.campaign.pk])).content.decode()
        self.assertIn("interrupted", body)
