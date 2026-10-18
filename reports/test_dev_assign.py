"""Group suggestions + per-row assign on the unassigned development page."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.roles import TREASURER
from departments.models import Department, DevelopmentGroup
from giving.models import Campaign, CampaignMember, Transaction
from members.models import Member
from reports.services.dev_assign import suggest_group_for, suggest_groups_for_many


def _user(name, role=TREASURER):
    u = User.objects.create_user(name, password="x")
    u.groups.add(Group.objects.get_or_create(name=role)[0])
    return u


class DevAssignSuggestTests(TestCase):
    def setUp(self):
        self.dev = Department.objects.create(
            name="DevAssign Fund", fund_type="LOCAL",
            category=Department.Category.DEVELOPMENT)
        self.g1 = DevelopmentGroup.objects.create(number=1, name="Alpha")
        self.g2 = DevelopmentGroup.objects.create(number=2, name="Beta")
        self.today = timezone.localdate()

    def _txn(self, *, payer="", member=None, group=None, days_ago=30, ref=""):
        return Transaction.objects.create(
            date=self.today - dt.timedelta(days=days_ago),
            channel="CASH", direction="CREDIT", amount=Decimal("100"),
            department=self.dev, dev_group=group, member=member,
            payer_name=payer, confirmed=True, allocation_status="MANUAL",
            core_ref=ref or f"DA-{Transaction.objects.count() + 1}")

    def test_suggests_group_from_past_year_name_history(self):
        self._txn(payer="JANE DOE", group=self.g1, days_ago=60, ref="H1")
        self._txn(payer="JANE DOE", group=self.g1, days_ago=40, ref="H2")
        open_txn = self._txn(payer="JANE DOE", group=None, days_ago=0, ref="OPEN")
        g, reason = suggest_group_for(open_txn)
        self.assertEqual(g, self.g1)
        self.assertIn("2", reason)

    def test_highest_count_wins_when_name_appears_in_multiple_groups(self):
        self._txn(payer="SAM MIX", group=self.g1, days_ago=10, ref="M1")
        self._txn(payer="SAM MIX", group=self.g2, days_ago=11, ref="M2")
        self._txn(payer="SAM MIX", group=self.g2, days_ago=12, ref="M3")
        open_txn = self._txn(payer="SAM MIX", group=None, days_ago=0, ref="MOPEN")
        g, _ = suggest_group_for(open_txn)
        self.assertEqual(g, self.g2)

    def test_tie_yields_no_suggestion(self):
        self._txn(payer="TIE PERSON", group=self.g1, days_ago=10, ref="T1")
        self._txn(payer="TIE PERSON", group=self.g2, days_ago=11, ref="T2")
        open_txn = self._txn(payer="TIE PERSON", group=None, days_ago=0, ref="TOPEN")
        g, reason = suggest_group_for(open_txn)
        self.assertIsNone(g)
        self.assertEqual(reason, "ambiguous")

    def test_campaign_list_suggests_group(self):
        camp = Campaign.objects.create(name="Dev Campaign", department=self.dev)
        CampaignMember.objects.create(campaign=camp, name="CAMP MEMBER", group="2")
        open_txn = self._txn(payer="CAMP MEMBER", group=None, days_ago=0, ref="COPEN")
        g, reason = suggest_group_for(open_txn)
        self.assertEqual(g, self.g2)
        self.assertIn("campaign", reason)

    def test_ignores_history_older_than_one_year(self):
        self._txn(payer="OLD GIVER", group=self.g1, days_ago=400, ref="OLD")
        open_txn = self._txn(payer="OLD GIVER", group=None, days_ago=0, ref="NEW")
        g, _ = suggest_group_for(open_txn)
        self.assertIsNone(g)

    def test_member_link_history(self):
        m = Member.objects.create(name="LINKED MEMBER")
        self._txn(member=m, group=self.g1, days_ago=20, ref="L1")
        open_txn = self._txn(member=m, group=None, days_ago=0, ref="LOPEN")
        g, _ = suggest_group_for(open_txn)
        self.assertEqual(g, self.g1)

    def test_batch_returns_per_txn(self):
        self._txn(payer="BATCH A", group=self.g1, days_ago=5, ref="BAH")
        a = self._txn(payer="BATCH A", group=None, days_ago=0, ref="BA")
        b = self._txn(payer="UNKNOWN Z", group=None, days_ago=0, ref="BZ")
        out = suggest_groups_for_many([a, b])
        self.assertEqual(out[a.id][0], self.g1)
        self.assertIsNone(out[b.id][0])


class DevUnassignedAssignTests(TestCase):
    def setUp(self):
        self.tr = _user("dev_assign_tr")
        self.client.force_login(self.tr)
        self.dev = Department.objects.create(
            name="Unassigned Dev", fund_type="LOCAL",
            category=Department.Category.DEVELOPMENT)
        self.g1 = DevelopmentGroup.objects.create(number=7, name="Seven")
        self.open_txn = Transaction.objects.create(
            date=timezone.localdate(), channel="CASH", direction="CREDIT",
            amount=Decimal("250"), department=self.dev, payer_name="ROW PERSON",
            confirmed=True, allocation_status="MANUAL", core_ref="UA-1")

    def test_page_shows_suggestion_and_per_row_assign(self):
        Transaction.objects.create(
            date=timezone.localdate() - dt.timedelta(days=10),
            channel="CASH", direction="CREDIT", amount=Decimal("50"),
            department=self.dev, dev_group=self.g1, payer_name="ROW PERSON",
            confirmed=True, allocation_status="MANUAL", core_ref="UA-H")
        r = self.client.get(reverse("dev_unassigned"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "ROW PERSON")
        self.assertContains(r, "Suggested group")
        item = next(t for t in r.context["items"] if t.id == self.open_txn.id)
        self.assertEqual(item.suggested_group, self.g1)

    def test_individual_assign_removes_from_list(self):
        r = self.client.post(reverse("dev_unassigned"), {
            "txn": str(self.open_txn.id),
            "dev_group": str(self.g1.id),
        })
        self.assertEqual(r.status_code, 302)
        self.open_txn.refresh_from_db()
        self.assertEqual(self.open_txn.dev_group_id, self.g1.id)
        page = self.client.get(reverse("dev_unassigned"))
        self.assertNotContains(page, "ROW PERSON")
