"""A leader may record a pledge against an open target, and correct it for a day.

The shape of the permission matters more than the mechanics. A leader knows who
promised what at their own fund's appeal, and making them route that through the
treasurer loses pledges. But the record of a promise is not theirs to revise
once the church has acted on it, so the door closes after a day — and at once if
money has been received against it.
"""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from departments.models import Department, DepartmentLeadership
from members.models import Member
from pledges.models import Pledge, PledgeCampaign


class _Leader(TestCase):
    def setUp(self):
        from core.roles import LEADER
        self.user = User.objects.create_user("ldr", password="x")
        self.user.groups.add(Group.objects.get_or_create(name=LEADER)[0])
        self.dept = Department.objects.create(name="Youth", fund_type="LOCAL")
        DepartmentLeadership.objects.create(user=self.user,
                                            department=self.dept)
        self.member = Member.objects.create(name="ASHA MUTUA", active=True)
        self.campaign = PledgeCampaign.objects.create(
            name="Youth Camp", target_department=self.dept,
            goal_amount=Decimal("100000"),
            status=PledgeCampaign.Status.ACTIVE)
        self.client.force_login(self.user)
        self.url = reverse("leader_pledges", args=[self.dept.pk])

    def _add(self, **over):
        data = {"action": "add", "campaign": self.campaign.pk,
                "member": self.member.pk, "amount": "5000"}
        data.update(over)
        return self.client.post(self.url, data)


class OpenTargetTests(_Leader):
    def test_an_open_campaign_is_offered(self):
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual([c.pk for c in r.context["open_campaigns"]],
                         [self.campaign.pk])

    def test_a_closed_campaign_is_not(self):
        self.campaign.status = PledgeCampaign.Status.CLOSED
        self.campaign.save()
        r = self.client.get(self.url)
        self.assertEqual(list(r.context["open_campaigns"]), [])

    def test_a_campaign_past_its_end_date_is_not(self):
        self.campaign.end_date = dt.date.today() - dt.timedelta(days=1)
        self.campaign.save()
        r = self.client.get(self.url)
        self.assertEqual(list(r.context["open_campaigns"]), [])

    def test_a_campaign_for_another_fund_is_not(self):
        other = Department.objects.create(name="Choir", fund_type="LOCAL")
        PledgeCampaign.objects.create(
            name="Choir Robes", target_department=other,
            status=PledgeCampaign.Status.ACTIVE)
        r = self.client.get(self.url)
        self.assertEqual([c.pk for c in r.context["open_campaigns"]],
                         [self.campaign.pk])


class RecordingTests(_Leader):
    def test_a_pledge_is_recorded_against_the_leader(self):
        self._add()
        p = Pledge.objects.get()
        self.assertEqual(p.amount, Decimal("5000"))
        self.assertEqual(p.member, self.member)
        self.assertEqual(p.campaign, self.campaign)
        self.assertEqual(p.recorded_by, self.user)
        self.assertEqual(p.status, Pledge.Status.ACTIVE)

    def test_a_closed_campaign_cannot_be_pledged_to(self):
        """Server-side, not merely absent from the form."""
        self.campaign.status = PledgeCampaign.Status.CLOSED
        self.campaign.save()
        self._add()
        self.assertFalse(Pledge.objects.exists())

    def test_a_campaign_on_another_fund_cannot_be_pledged_to(self):
        other = Department.objects.create(name="Choir", fund_type="LOCAL")
        elsewhere = PledgeCampaign.objects.create(
            name="Choir Robes", target_department=other,
            status=PledgeCampaign.Status.ACTIVE)
        self._add(campaign=elsewhere.pk)
        self.assertFalse(Pledge.objects.exists())

    def test_zero_and_negative_amounts_are_refused(self):
        self._add(amount="0")
        self._add(amount="-100")
        self.assertFalse(Pledge.objects.exists())

    def test_a_missing_member_is_refused(self):
        self._add(member="")
        self.assertFalse(Pledge.objects.exists())


class EditWindowTests(_Leader):
    def _pledge(self, age=None, paid=False):
        self._add()
        p = Pledge.objects.get()
        if age:
            Pledge.objects.filter(pk=p.pk).update(created_at=timezone.now() - age)
            p.refresh_from_db()
        return p

    def test_editable_on_the_day(self):
        self.assertTrue(self._pledge().leader_editable())

    def test_still_editable_just_under_a_day(self):
        p = self._pledge(age=dt.timedelta(hours=23, minutes=30))
        self.assertTrue(p.leader_editable())

    def test_not_editable_after_a_day(self):
        p = self._pledge(age=dt.timedelta(days=1, minutes=1))
        self.assertFalse(p.leader_editable())

    def test_an_edit_after_the_window_is_refused(self):
        p = self._pledge(age=dt.timedelta(days=2))
        self.client.post(self.url, {"action": "edit", "pledge": p.pk,
                                    "amount": "999"})
        p.refresh_from_db()
        self.assertEqual(p.amount, Decimal("5000"))

    def test_a_delete_after_the_window_is_refused(self):
        p = self._pledge(age=dt.timedelta(days=2))
        self.client.post(self.url, {"action": "delete", "pledge": p.pk})
        self.assertTrue(Pledge.objects.filter(pk=p.pk).exists())

    def test_an_edit_within_the_window_is_allowed(self):
        p = self._pledge()
        self.client.post(self.url, {"action": "edit", "pledge": p.pk,
                                    "amount": "7500", "note": "corrected"})
        p.refresh_from_db()
        self.assertEqual(p.amount, Decimal("7500"))
        self.assertEqual(p.note, "corrected")

    def test_a_delete_within_the_window_is_allowed(self):
        p = self._pledge()
        self.client.post(self.url, {"action": "delete", "pledge": p.pk})
        self.assertFalse(Pledge.objects.filter(pk=p.pk).exists())

    def test_a_pledge_on_another_fund_cannot_be_touched(self):
        other = Department.objects.create(name="Choir", fund_type="LOCAL")
        elsewhere = PledgeCampaign.objects.create(
            name="Choir Robes", target_department=other,
            status=PledgeCampaign.Status.ACTIVE)
        theirs = Pledge.objects.create(
            campaign=elsewhere, member=self.member, amount=Decimal("100"),
            status=Pledge.Status.ACTIVE)
        self.client.post(self.url, {"action": "delete", "pledge": theirs.pk})
        self.assertTrue(Pledge.objects.filter(pk=theirs.pk).exists())
