"""Add a register member to a giving campaign sheet one-by-one."""
from django.contrib.auth.models import Group, User
from django.test import Client, TestCase

from core.roles import TREASURER
from departments.models import Department, DevelopmentGroup
from giving.models import Campaign, CampaignMember
from members.models import Member


class CampaignMemberAddTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("camp_add", password="x",
                                             is_superuser=True)
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(
            name="Dev Fund Add", fund_type="LOCAL", category="DEVELOPMENT")
        self.camp = Campaign.objects.create(
            name="Roof Appeal", department=self.fund, active=True)
        self.grp = DevelopmentGroup.objects.create(number=3, name="Group 3")
        self.member = Member.objects.create(
            name="ALICE ADD", phone="254711100001", active=True,
            dev_group=self.grp)
        self.client = Client()
        self.client.force_login(self.user)

    def test_add_member_creates_sheet_row(self):
        r = self.client.post(
            f"/campaigns/{self.camp.pk}/members/add/",
            {"member": self.member.pk, "dev_group": self.grp.pk,
             "phone": "", "group_label": ""})
        self.assertEqual(r.status_code, 302)
        row = CampaignMember.objects.get(campaign=self.camp)
        self.assertEqual(row.name, "ALICE ADD")
        self.assertEqual(row.phone, "254711100001")
        self.assertIn("3", row.group)
        self.assertTrue(row.match_code.startswith("CM"))

    def test_add_requires_phone_when_member_has_none(self):
        bare = Member.objects.create(name="NO PHONE", phone="", active=True)
        r = self.client.post(
            f"/campaigns/{self.camp.pk}/members/add/",
            {"member": bare.pk, "dev_group": "", "phone": "",
             "group_label": ""},
            follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "no phone")
        self.assertFalse(
            CampaignMember.objects.filter(campaign=self.camp).exists())

    def test_duplicate_phone_updates_existing(self):
        CampaignMember.objects.create(
            campaign=self.camp, name="OLD NAME", phone="254711100001",
            group="Old")
        r = self.client.post(
            f"/campaigns/{self.camp.pk}/members/add/",
            {"member": self.member.pk, "dev_group": self.grp.pk,
             "phone": "0711100001", "group_label": ""})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(CampaignMember.objects.filter(campaign=self.camp).count(), 1)
        row = CampaignMember.objects.get(campaign=self.camp)
        self.assertEqual(row.name, "ALICE ADD")
