"""Pledge messages: the church's own words, seen before they are sent.

Two things a treasurer could not do before: thank someone for a promise rather
than only chase them for it, and read the actual message before an SMS goes out
that cannot be recalled.
"""
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.models import SiteConfig
from core.roles import TREASURER
from departments.models import Department
from members.models import Member
from pledges.models import Pledge, PledgeCampaign, PledgeReminderLog
from pledges.services import reminders as rem


class _Pledged(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("tr", password="x")
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        dept = Department.objects.create(name="Building", fund_type="LOCAL")
        self.member = Member.objects.create(name="asha mutua", phone="0712345678",
                                            active=True)
        self.campaign = PledgeCampaign.objects.create(
            name="Sanctuary Roof", target_department=dept,
            status=PledgeCampaign.Status.ACTIVE)
        self.pledge = Pledge.objects.create(
            campaign=self.campaign, member=self.member,
            amount=Decimal("50000"), status=Pledge.Status.ACTIVE)
        self.cfg = SiteConfig.get()
        self.client.force_login(self.user)


class WordingTests(_Pledged):
    def test_the_two_kinds_read_differently(self):
        thanks = rem.build_pledge_text(self.pledge, kind="THANKS")
        remind = rem.build_pledge_text(self.pledge, kind="REMINDER")
        self.assertIn("thank you for pledging", thanks.lower())
        self.assertIn("outstanding", remind.lower())
        self.assertNotEqual(thanks, remind)

    def test_placeholders_are_filled_from_the_pledge(self):
        text = rem.build_pledge_text(self.pledge, kind="THANKS")
        self.assertIn("Asha Mutua", text)          # titled, not as stored
        self.assertIn("50,000", text)
        self.assertIn("Sanctuary Roof", text)

    def test_the_church_s_own_wording_is_used_when_set(self):
        self.cfg.pledge_thanks_template = "Asante {name} kwa ahadi ya {amount}."
        self.cfg.save()
        self.assertEqual(rem.build_pledge_text(self.pledge, kind="THANKS"),
                         "Asante Asha Mutua kwa ahadi ya 50,000.")

    def test_an_unknown_placeholder_is_left_alone_rather_than_raising(self):
        """A treasurer editing this is not writing code, and a stray brace must
        not stop a message going out."""
        text = rem.build_pledge_text(self.pledge, kind="THANKS",
                                     template="Hi {name}, {nonsense} here")
        self.assertIn("Asha Mutua", text)
        self.assertIn("{nonsense}", text)

    def test_a_malformed_template_falls_back_to_the_default(self):
        text = rem.build_pledge_text(self.pledge, kind="THANKS",
                                     template="Hi {name")
        self.assertIn("thank you for pledging", text.lower())

    def test_blank_wording_falls_back_to_the_default(self):
        self.cfg.pledge_reminder_template = "   "
        self.cfg.save()
        self.assertIn("outstanding",
                      rem.build_pledge_text(self.pledge, kind="REMINDER").lower())

    def test_the_old_helper_still_answers(self):
        self.assertEqual(rem.build_reminder_text(self.pledge),
                         rem.build_pledge_text(self.pledge, kind="REMINDER"))


class PreviewTests(_Pledged):
    def test_the_preview_returns_the_filled_message(self):
        r = self.client.get(reverse("pledge_message_preview"),
                            {"kind": "THANKS", "pledge": self.pledge.pk})
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertIn("Asha Mutua", d["text"])
        self.assertEqual(d["example"], self.member.name)

    def test_it_counts_segments_because_a_church_pays_per_segment(self):
        r = self.client.get(reverse("pledge_message_preview"),
                            {"template": "x" * 400, "pledge": self.pledge.pk})
        d = r.json()
        self.assertEqual(d["length"], 400)
        self.assertGreater(d["segments"], 1)

    def test_a_short_message_is_one_segment(self):
        r = self.client.get(reverse("pledge_message_preview"),
                            {"template": "Thanks {name}",
                             "pledge": self.pledge.pk})
        self.assertEqual(r.json()["segments"], 1)

    def test_unsaved_wording_can_be_tried_out(self):
        r = self.client.get(reverse("pledge_message_preview"),
                            {"template": "Try {campaign}",
                             "pledge": self.pledge.pk})
        self.assertEqual(r.json()["text"], "Try Sanctuary Roof")
        self.cfg.refresh_from_db()
        self.assertNotEqual(self.cfg.pledge_reminder_template, "Try {campaign}")

    def test_it_says_so_when_there_is_nothing_to_preview_against(self):
        Pledge.objects.all().delete()
        r = self.client.get(reverse("pledge_message_preview"))
        self.assertFalse(r.json()["ok"])

    def test_the_preview_is_closed_to_non_treasurers(self):
        self.client.force_login(User.objects.create_user("nobody", password="x"))
        r = self.client.get(reverse("pledge_message_preview"))
        self.assertIn(r.status_code, (302, 403))


class SendTests(_Pledged):
    def test_a_thank_you_is_logged_with_its_own_wording(self):
        self.client.post(reverse("pledge_remind", args=[self.pledge.pk]),
                         {"channel": "SMS", "kind": "THANKS"})
        log = PledgeReminderLog.objects.get()
        self.assertIn("thank you for pledging", log.message.lower())

    def test_a_reminder_is_still_the_default(self):
        self.client.post(reverse("pledge_remind", args=[self.pledge.pk]),
                         {"channel": "SMS"})
        self.assertIn("outstanding",
                      PledgeReminderLog.objects.get().message.lower())

    def test_an_opted_out_member_gets_neither(self):
        """Opting out is about being messaged, not about being chased."""
        self.pledge.reminders_opt_out = True
        self.pledge.save()
        rem.send_pledge_reminder(self.pledge, kind="THANKS", user=self.user)
        log = PledgeReminderLog.objects.get()
        self.assertFalse(log.ok)
        self.assertIn("opted out", log.message)

    def test_a_member_with_no_phone_gets_neither(self):
        self.member.phone = ""
        self.member.save()
        rem.send_pledge_reminder(self.pledge, kind="THANKS", user=self.user)
        self.assertIn("no phone", PledgeReminderLog.objects.get().message)
