"""Office pledge form: phone required; visitors allowed off the register."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase

from core.roles import TREASURER
from departments.models import Department
from members.models import Member
from pledges.forms import PledgeForm
from pledges.models import Pledge, PledgeCampaign


class OfficePledgePhoneFormTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("of", password="x", is_superuser=True)
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(name="Roof", fund_type="LOCAL")
        self.campaign = PledgeCampaign.objects.create(
            name="Roof", target_department=self.fund,
            status=PledgeCampaign.Status.ACTIVE)
        self.member = Member.objects.create(
            name="REGISTERED ONE", phone="254711000222", active=True)

    def _base(self, **extra):
        data = {
            "campaign": self.campaign.id,
            "amount": "5000",
            "frequency": Pledge.Frequency.MONTHLY,
            "start_date": "2026-06-01",
            "phone": "0711000222",
        }
        data.update(extra)
        return data

    def test_member_pledge_stores_phone_on_submitted_contact(self):
        form = PledgeForm(self._base(member=self.member.id))
        self.assertTrue(form.is_valid(), form.errors)
        p = form.save()
        self.assertEqual(p.member_id, self.member.id)
        self.assertEqual(p.pledged_phone, "254711000222")
        self.assertIn("254711000222", p.submitted_contact)

    def test_visitor_creates_provisional_member(self):
        form = PledgeForm(self._base(
            phone="0722333444", visitor_name="Sunday Visitor"))
        self.assertTrue(form.is_valid(), form.errors)
        p = form.save()
        self.assertFalse(p.member.active)
        self.assertEqual(p.member.phone, "254722333444")
        self.assertEqual(p.pledged_phone, "254722333444")

    def test_phone_alone_reuses_existing_member(self):
        form = PledgeForm(self._base(phone="0711000222"))
        self.assertTrue(form.is_valid(), form.errors)
        p = form.save()
        self.assertEqual(p.member_id, self.member.id)

    def test_missing_member_and_visitor_name_rejected(self):
        form = PledgeForm(self._base(phone="0799888777"))
        self.assertFalse(form.is_valid())

    def test_register_member_may_omit_phone(self):
        """Desk entry that already names the member must not demand a phone."""
        data = self._base(member=self.member.id)
        data.pop("phone")
        form = PledgeForm(data)
        self.assertTrue(form.is_valid(), form.errors)
        p = form.save()
        self.assertEqual(p.member_id, self.member.id)
        self.assertEqual(p.pledged_phone, "254711000222")

    def test_visitor_without_phone_rejected(self):
        form = PledgeForm({
            "campaign": self.campaign.id,
            "amount": "5000",
            "frequency": Pledge.Frequency.MONTHLY,
            "start_date": "2026-06-01",
            "visitor_name": "Sunday Visitor",
        })
        self.assertFalse(form.is_valid())

    def test_detail_page_shows_pledged_phone(self):
        p = Pledge.objects.create(
            campaign=self.campaign, member=self.member, amount=Decimal("1000"),
            start_date=dt.date(2026, 1, 1), status=Pledge.Status.ACTIVE,
            submitted_contact="REGISTERED ONE / 254711000222")
        c = Client()
        c.force_login(self.user)
        r = c.get(f"/pledges/{p.id}/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "254711000222")
