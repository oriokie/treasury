"""Members page SMS button + criteria-based bulk messaging: not contributed to
a campaign, outstanding pledge, no recent giving, demographic group, and a
plain broadcast — each previewed before sending."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from members.models import Member
from giving.models import Transaction, Campaign
from core.models import SmsLog


def _tr():
    u = User.objects.create_user("tr_msms", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class MemberSmsCriteriaTests(TestCase):
    def setUp(self):
        self.tr = _tr()
        self.d = Department.objects.create(name="MSMSFund", fund_type="LOCAL",
            category="MINISTRY")
        self.camp = Campaign.objects.create(name="Camp Meeting MS", department=self.d,
            triggers="campms")
        self.giver = Member.objects.create(name="Giver Person", phone="254711000001",
            active=True)
        self.non_giver = Member.objects.create(name="Non Giver Person",
            phone="254711000002", active=True)
        self.no_phone = Member.objects.create(name="No Phone Person", active=True)
        Transaction.objects.create(date=dt.date(2026, 6, 1), amount=Decimal("500"),
            direction="CREDIT", confirmed=True, channel="CASH",
            allocation_status="MANUAL", department=self.d, member=self.giver)
        self.c = Client(); self.c.force_login(self.tr)

    def test_not_contributed_campaign_excludes_givers(self):
        b = self.c.get(f"/members/sms/?criteria=not_contributed_campaign"
                       f"&campaign={self.camp.id}").content.decode()
        self.assertIn("NON GIVER PERSON", b)
        self.assertNotIn(">GIVER PERSON<", b)

    def test_no_phone_never_included(self):
        b = self.c.get(f"/members/sms/?criteria=not_contributed_campaign"
                       f"&campaign={self.camp.id}").content.decode()
        self.assertNotIn("NO PHONE PERSON", b)

    def test_by_group_criterion(self):
        Member.objects.create(name="Group Member", phone="254711000003",
            active=True, group="AMM")
        b = self.c.get("/members/sms/?criteria=by_group&group=AMM").content.decode()
        self.assertIn("GROUP MEMBER", b)
        self.assertNotIn("NON GIVER PERSON", b)

    def test_no_recent_giving_criterion(self):
        old_giver = Member.objects.create(name="Old Giver", phone="254711000004",
            active=True)
        Transaction.objects.create(date=dt.date(2020, 1, 1), amount=Decimal("100"),
            direction="CREDIT", confirmed=True, channel="CASH",
            allocation_status="MANUAL", department=self.d, member=old_giver)
        b = self.c.get("/members/sms/?criteria=no_recent_giving&days=90").content.decode()
        self.assertIn("OLD GIVER", b)   # gave in 2020, not in last 90 days
        self.assertNotIn(">GIVER PERSON<", b)  # gave recently (2026-06-01 is within window)

    def test_outstanding_pledge_criterion(self):
        from pledges.models import Pledge, PledgeCampaign
        pc = PledgeCampaign.objects.create(name="Pledge Drive MS", target_department=self.d)
        Pledge.objects.create(member=self.non_giver, campaign=pc,
            amount=Decimal("5000"), status="ACTIVE", frequency="ONE_OFF",
            start_date=dt.date(2026, 1, 1))
        b = self.c.get("/members/sms/?criteria=outstanding_pledge").content.decode()
        self.assertIn("NON GIVER PERSON", b)

    def test_broadcast_includes_everyone_with_phone(self):
        b = self.c.get("/members/sms/?criteria=all_with_phone").content.decode()
        self.assertIn("GIVER PERSON", b)
        self.assertIn("NON GIVER PERSON", b)
        self.assertNotIn("NO PHONE PERSON", b)

    def test_send_creates_sms_log(self):
        r = self.c.post(f"/members/sms/?criteria=not_contributed_campaign&campaign={self.camp.id}",
            {"criteria": "not_contributed_campaign", "campaign": str(self.camp.id),
             "template": "Dear {name}, please give to {campaign}."})
        self.assertIn(r.status_code, (200, 302))
        self.assertTrue(SmsLog.objects.filter(to__icontains="711000002").exists())

    def test_button_on_members_list_for_treasurer(self):
        b = self.c.get("/members/").content.decode()
        self.assertIn('href="/members/sms/"', b)
        self.assertIn("SMS members", b)

    def test_no_criterion_shows_prompt_not_all_members(self):
        b = self.c.get("/members/sms/").content.decode()
        self.assertIn("Choose a criterion", b)
        self.assertNotIn("GIVER PERSON", b)


class MinGiftsFilterTests(TestCase):
    """Filter out one-time givers (who may not be church members) by requiring
    a minimum number of contributions, layered on top of any criterion."""
    def setUp(self):
        self.tr = _tr()
        self.d = Department.objects.create(name="MinGiftsFund", fund_type="LOCAL",
            category="MINISTRY")
        self.one_timer = Member.objects.create(name="Solo Giver", phone="254733000001",
            active=True)
        self.regular = Member.objects.create(name="Repeat Giver", phone="254733000002",
            active=True)
        Transaction.objects.create(date=dt.date(2026, 1, 1), amount=Decimal("100"),
            direction="CREDIT", confirmed=True, channel="CASH",
            allocation_status="MANUAL", department=self.d, member=self.one_timer)
        for i in range(4):
            Transaction.objects.create(date=dt.date(2026, i + 1, 1), amount=Decimal("100"),
                direction="CREDIT", confirmed=True, channel="CASH",
                allocation_status="MANUAL", department=self.d, member=self.regular)
        self.c = Client(); self.c.force_login(self.tr)

    def test_excludes_one_time_givers(self):
        b = self.c.get("/members/sms/?criteria=all_with_phone&min_gifts=3").content.decode()
        self.assertIn("REPEAT GIVER", b)
        self.assertNotIn("SOLO GIVER", b)

    def test_no_filter_includes_one_time_givers(self):
        b = self.c.get("/members/sms/?criteria=all_with_phone").content.decode()
        self.assertIn("SOLO GIVER", b)

    def test_combines_with_another_criterion(self):
        Member.objects.create(name="Group Solo", phone="254733000003",
            active=True, group="YOUTH")
        b = self.c.get("/members/sms/?criteria=by_group&group=YOUTH&min_gifts=2").content.decode()
        self.assertNotIn("GROUP SOLO", b)   # only 0 gifts, filtered out

    def test_zero_min_gifts_means_no_filter(self):
        b = self.c.get("/members/sms/?criteria=all_with_phone&min_gifts=0").content.decode()
        self.assertIn("SOLO GIVER", b)
