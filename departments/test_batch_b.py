"""Batch B: envelope collapse (#7), campaign delete (#8), dev-group SMS (#9),
allocation-rule edit + settings exposure (#10)."""
import datetime as dt
from decimal import Decimal
from unittest import mock
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group as AuthGroup
from departments.models import Department, DevelopmentGroup
from members.models import Member
from giving.models import AllocationRule
from pledges.models import PledgeCampaign, Pledge
from core.models import SiteConfig


class BatchBTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("bb", password="x", is_superuser=True)
        self.u.groups.add(AuthGroup.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(self.u)

    # #7
    def test_envelope_list_collapsible(self):
        b = self.c.get("/envelopes/").content.decode()
        self.assertIn("sabbath-list-toggle", b)
        self.assertIn("<summary", b)

    # #8
    def test_campaign_delete_guarded(self):
        camp = PledgeCampaign.objects.create(name="Build")
        m = Member.objects.create(name="A")
        p = Pledge.objects.create(campaign=camp, member=m, amount=Decimal("100"),
            recorded_by=self.u, status="ACTIVE", start_date=dt.date(2026, 1, 1))
        self.c.post(f"/pledges/campaigns/{camp.id}/delete/")
        self.assertTrue(PledgeCampaign.objects.filter(pk=camp.id).exists())  # blocked
        p.delete()
        self.c.post(f"/pledges/campaigns/{camp.id}/delete/")
        self.assertFalse(PledgeCampaign.objects.filter(pk=camp.id).exists())

    # #9
    def test_dev_group_sms(self):
        g = DevelopmentGroup.objects.create(number=7, name="G7")
        m1 = Member.objects.create(name="Jane Doe", phone="254712345678", dev_group=g)
        Member.objects.create(name="No Phone", dev_group=g)
        b = self.c.get(f"/dev-groups/{g.id}/sms/").content.decode()
        self.assertIn("254712345678", b)            # has-phone shown
        cfg = SiteConfig.get(); cfg.sms_enabled = True; cfg.save()
        cap = []
        def fake(to, message, cfg=None):
            cap.append((to, message))
            class L: status = "SENT"
            return L()
        with mock.patch("core.services.sms.send_sms", fake):
            self.c.post(f"/dev-groups/{g.id}/sms/",
                        {"template": "Hi {name} from {church}"})
        self.assertEqual(len(cap), 1)               # only the member with a phone

    def test_dev_group_sms_all_groups(self):
        self.assertEqual(self.c.get("/dev-groups/sms/").status_code, 200)

    # #10
    def test_rule_edit(self):
        d1 = Department.objects.create(name="R1", fund_type="LOCAL", category="MINISTRY")
        d2 = Department.objects.create(name="R2", fund_type="LOCAL", category="MINISTRY")
        rule = AllocationRule.objects.create(reference="abc", department=d1,
            source="LEARNED", match_type="EXACT")
        self.assertEqual(self.c.get(f"/rules/{rule.id}/edit/").status_code, 200)
        self.c.post(f"/rules/{rule.id}/edit/",
            {"reference": "abc", "department": str(d2.id), "match_type": "EXACT",
             "source": "LEARNED"})
        rule.refresh_from_db()
        self.assertEqual(rule.department_id, d2.id)

    def test_settings_exposes_allocation(self):
        b = self.c.get("/settings/").content.decode()
        self.assertIn("Allocation &amp; categories", b)
        self.assertIn("Manage allocation rules", b)
