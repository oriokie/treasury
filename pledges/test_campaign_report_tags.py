"""Per-campaign pledge report, and the member tags it groups by.

The question a treasurer actually asks of a campaign is not "how much is
outstanding" — the dashboard says that — but "how are the board doing", "have
the committee paid". That needs roles, and roles are multi-valued and
church-defined, which is why they are their own model rather than another
choice field on the member.
"""
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import LEADER, TREASURER
from departments.models import Department, DepartmentLeadership
from giving.models import Transaction
from members.models import Member, MemberTag
from pledges.models import Pledge, PledgeCampaign, PledgePayment


class _Campaign(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("rep", password="x")
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.dept = Department.objects.create(name="Building", fund_type="LOCAL")
        self.campaign = PledgeCampaign.objects.create(
            name="Sanctuary Roof", target_department=self.dept,
            status=PledgeCampaign.Status.ACTIVE)
        self.board = MemberTag.objects.create(name="Church Board")
        self.committee = MemberTag.objects.create(name="Finance Committee")

        self.asha = Member.objects.create(name="ASHA MUTUA", active=True)
        self.asha.tags.set([self.board, self.committee])       # two roles
        self.peter = Member.objects.create(name="PETER OTIENO", active=True)
        self.peter.tags.set([self.board])
        self.mary = Member.objects.create(name="MARY WANJIKU", active=True)

        for m, amt in ((self.asha, "50000"), (self.peter, "20000"),
                       (self.mary, "10000")):
            Pledge.objects.create(campaign=self.campaign, member=m,
                                  amount=Decimal(amt),
                                  status=Pledge.Status.ACTIVE)
        self.client.force_login(self.user)
        self.url = reverse("pledge_campaign_report", args=[self.campaign.pk])

    def _pay(self, member, amount):
        t = Transaction.objects.create(
            date=self.campaign.start_date, channel="CASH", direction="CREDIT",
            amount=Decimal(amount), department=self.dept, member=member,
            confirmed=True, allocation_status="MANUAL")
        PledgePayment.objects.create(
            pledge=Pledge.objects.get(member=member), transaction=t,
            amount=Decimal(amount))


class ReportTests(_Campaign):
    def test_it_lists_who_pledged_what_and_the_balance(self):
        self._pay(self.asha, "20000")
        r = self.client.get(self.url)
        rows = {x["member"].name: x for x in r.context["rows"]}
        self.assertEqual(rows["ASHA MUTUA"]["amount"], Decimal("50000"))
        self.assertEqual(rows["ASHA MUTUA"]["paid"], Decimal("20000"))
        self.assertEqual(rows["ASHA MUTUA"]["outstanding"], Decimal("30000"))
        self.assertEqual(rows["MARY WANJIKU"]["outstanding"], Decimal("10000"))

    def test_totals_cover_the_campaign(self):
        r = self.client.get(self.url)
        self.assertEqual(r.context["totals"]["amount"], Decimal("80000"))
        self.assertEqual(r.context["totals"]["n"], 3)

    def test_a_cancelled_pledge_is_left_out(self):
        p = Pledge.objects.get(member=self.mary)
        p.status = Pledge.Status.CANCELLED
        p.save()
        r = self.client.get(self.url)
        self.assertEqual(r.context["totals"]["n"], 2)

    def test_it_exports(self):
        for fmt in ("csv", "xlsx"):
            r = self.client.get(self.url, {"export": fmt})
            self.assertEqual(r.status_code, 200, fmt)


class TagGroupingTests(_Campaign):
    def test_grouping_buckets_by_tag(self):
        r = self.client.get(self.url, {"group": "tag"})
        groups = {g["name"]: g for g in r.context["groups"]}
        self.assertEqual(groups["Church Board"]["totals"]["n"], 2)
        self.assertEqual(groups["Finance Committee"]["totals"]["n"], 1)

    def test_someone_with_two_tags_appears_in_both(self):
        """And the subtotals therefore exceed the campaign total — which the
        page says out loud rather than leaving to a calculator."""
        r = self.client.get(self.url, {"group": "tag"})
        groups = {g["name"]: g for g in r.context["groups"]}
        self.assertIn("ASHA MUTUA",
                      [x["member"].name for x in groups["Church Board"]["rows"]])
        self.assertIn("ASHA MUTUA",
                      [x["member"].name
                       for x in groups["Finance Committee"]["rows"]])
        summed = sum(g["totals"]["amount"] for g in r.context["groups"])
        self.assertGreater(summed, r.context["totals"]["amount"])

    def test_untagged_members_get_their_own_group(self):
        r = self.client.get(self.url, {"group": "tag"})
        untagged = next(g for g in r.context["groups"] if g["name"] == "Untagged")
        self.assertEqual([x["member"].name for x in untagged["rows"]],
                         ["MARY WANJIKU"])

    def test_filtering_to_one_tag(self):
        r = self.client.get(self.url, {"tag": "Finance Committee"})
        self.assertEqual([x["member"].name for x in r.context["rows"]],
                         ["ASHA MUTUA"])
        self.assertEqual(r.context["totals"]["amount"], Decimal("50000"))


class LeaderTaggingTests(_Campaign):
    def setUp(self):
        super().setUp()
        self.leader = User.objects.create_user("ldr2", password="x")
        self.leader.groups.add(Group.objects.get_or_create(name=LEADER)[0])
        DepartmentLeadership.objects.create(user=self.leader,
                                            department=self.dept)
        self.client.force_login(self.leader)
        self.page = reverse("leader_pledges", args=[self.dept.pk])

    def test_a_leader_can_tag_someone_who_pledged_to_their_fund(self):
        self.client.post(self.page, {"action": "tag", "member": self.mary.pk,
                                     "tags": [self.board.pk]})
        self.assertEqual([t.name for t in self.mary.tags.all()],
                         ["Church Board"])

    def test_tags_can_be_replaced_and_cleared(self):
        self.client.post(self.page, {"action": "tag", "member": self.asha.pk,
                                     "tags": [self.board.pk]})
        self.assertEqual(self.asha.tags.count(), 1)
        self.client.post(self.page, {"action": "tag", "member": self.asha.pk})
        self.assertEqual(self.asha.tags.count(), 0)

    def test_a_member_who_has_not_pledged_here_cannot_be_tagged(self):
        outsider = Member.objects.create(name="STRANGER", active=True)
        self.client.post(self.page, {"action": "tag", "member": outsider.pk,
                                     "tags": [self.board.pk]})
        self.assertEqual(outsider.tags.count(), 0)

    def test_an_inactive_tag_cannot_be_assigned(self):
        retired = MemberTag.objects.create(name="Old Role", active=False)
        self.client.post(self.page, {"action": "tag", "member": self.mary.pk,
                                     "tags": [retired.pk]})
        self.assertEqual(self.mary.tags.count(), 0)

    def test_a_leader_cannot_invent_a_tag(self):
        """Assign only; four spellings of "Committee" would make the grouping
        worthless."""
        before = MemberTag.objects.count()
        self.client.post(self.page, {"action": "tag", "member": self.mary.pk,
                                     "tags": ["999"], "name": "Made Up"})
        self.assertEqual(MemberTag.objects.count(), before)
