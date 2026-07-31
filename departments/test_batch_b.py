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
    def test_envelope_month_view_does_not_inline_the_receipts(self):
        """Originally #7 kept the month page readable by collapsing each
        Sabbath's receipts into a <details>. The receipts have since moved off
        this page entirely onto a per-Sabbath page, which serves the same
        requirement outright — so this now asserts the requirement (the month
        view summarises and links) rather than the collapse widget that used
        to implement it."""
        from envelopes.models import Envelope
        d = Department.objects.create(name="BBTithe", fund_type="TRUST")
        sab = dt.date(2026, 6, 6)
        env = Envelope.objects.create(
            date=sab, receipt_no="BB-9001", contributor_name="ZORRO SAMPLE",
            total=Decimal("50"), recorded_by=self.u)
        env.lines.create(department=d, amount=Decimal("50"))

        b = self.c.get("/envelopes/?month=2026-06").content.decode()
        # the Sabbath is summarised, and links to its own entries page …
        self.assertIn("/envelopes/sabbath/2026-06-06/", b)
        self.assertIn("BBTithe", b)          # the fund breakdown is on the summary
        # … but the individual receipts are not listed here any more
        self.assertNotIn("BB-9001", b)
        self.assertNotIn("ZORRO SAMPLE", b)

    def test_the_per_sabbath_page_lists_the_receipts(self):
        """The other half of the move: what left the month view has to be
        somewhere, or #7 would have been 'fixed' by losing the data."""
        from envelopes.models import Envelope
        d = Department.objects.create(name="BBTithe2", fund_type="TRUST")
        env = Envelope.objects.create(
            date=dt.date(2026, 6, 6), receipt_no="BB-9002",
            contributor_name="ZORRO SAMPLE", total=Decimal("50"), recorded_by=self.u)
        env.lines.create(department=d, amount=Decimal("50"))

        b = self.c.get("/envelopes/sabbath/2026-06-06/").content.decode()
        self.assertIn("BB-9002", b)
        self.assertIn("ZORRO SAMPLE", b)

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

    def test_settings_points_at_the_allocation_page(self):
        """Allocation & categories moved to its own page, next to the allocation
        rules and dev-group patterns it belongs with — rather than sitting in
        Settings → Channels among bank accounts and opening balances that have
        nothing to do with allocation. Settings still names it, and links there."""
        b = self.c.get("/settings/").content.decode()
        self.assertIn("Allocation &amp; categories", b)
        self.assertIn("/allocation-settings/", b)

    def test_the_duplicate_dev_prefix_field_is_gone(self):
        """It built exactly the regex a DevGroupPattern of kind NUMBERED builds,
        but could not be labelled, ordered, disabled or audited. Migration
        giving.0025 turned anything configured into real patterns."""
        b = self.c.get("/settings/").content.decode()
        self.assertNotIn("Dev group extra prefixes", b)
        self.assertEqual(self.c.get("/allocation-settings/").status_code, 200)
