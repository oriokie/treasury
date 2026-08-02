"""The member picked in the typeahead is the member the form receives.

Reported: "Choose the member making the pledge" after plainly choosing one. The
typeahead assigns the chosen id to a hidden <select>, and that select was
deliberately left empty — a roll of thousands has no business being rendered as
options. Assigning .value for an option that does not exist silently leaves the
select blank, so the form rejected a member the user could see they had picked.
"""
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from departments.models import Department, DepartmentLeadership
from members.models import Member
from pledges.models import Pledge, PledgeCampaign


class MemberPickTests(TestCase):
    def setUp(self):
        from core.roles import LEADER
        self.user = User.objects.create_user("pick", password="x")
        self.user.groups.add(Group.objects.get_or_create(name=LEADER)[0])
        self.dept = Department.objects.create(name="Youth", fund_type="LOCAL")
        DepartmentLeadership.objects.create(user=self.user, department=self.dept)
        self.member = Member.objects.create(name="ASHA MUTUA", phone="0712345678",
                                            active=True)
        self.campaign = PledgeCampaign.objects.create(
            name="Youth Camp", target_department=self.dept,
            status=PledgeCampaign.Status.ACTIVE)
        self.client.force_login(self.user)
        self.url = reverse("leader_pledges", args=[self.dept.pk])

    def test_the_widget_adds_the_chosen_option_before_selecting_it(self):
        """The fix itself: the shared picker must cope with an empty select."""
        js = (__import__("pathlib").Path("static/js/member-search.js")
              .read_text())
        self.assertIn('option[value="', js)
        self.assertIn("select.appendChild(opt)", js)

    def test_the_select_is_deliberately_empty(self):
        """If this ever regains options, the roll is being rendered in full."""
        r = self.client.get(self.url)
        body = r.content.decode()
        self.assertIn('id="plMember"', body)
        self.assertNotIn(self.member.name, body.split('id="plMember"')[1][:400])

    def test_posting_the_picked_id_records_the_pledge(self):
        """What the browser sends once the option exists."""
        self.client.post(self.url, {
            "action": "add", "campaign": self.campaign.pk,
            "member": self.member.pk, "amount": "5000"})
        self.assertEqual(Pledge.objects.get().member, self.member)

    def test_the_lookup_finds_the_member_by_name_and_phone(self):
        for q in ("ASHA", "asha", "0712"):
            r = self.client.get(reverse("leader_member_search"), {"q": q})
            ids = [x["id"] for x in r.json()["results"]]
            self.assertIn(self.member.pk, ids, q)

    def test_an_id_that_is_not_a_member_is_still_refused(self):
        self.client.post(self.url, {
            "action": "add", "campaign": self.campaign.pk,
            "member": "999999", "amount": "5000"})
        self.assertFalse(Pledge.objects.exists())
