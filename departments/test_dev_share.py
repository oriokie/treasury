"""Public development codes directory + live collection board."""
import datetime as dt
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase

from core.models import SiteConfig
from core.roles import TREASURER
from departments.models import Department, DevelopmentGroup
from giving.models import Transaction
from members.models import Member, mask_phone


class DevSharePublicTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("devshare", password="x",
                                             is_superuser=True)
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(
            name="Development", fund_type="LOCAL", category="DEVELOPMENT")
        self.grp = DevelopmentGroup.objects.create(number=7, name="Group 7")
        self.giver = Member.objects.create(
            name="DEV GIVER", phone="254722233344", active=True,
            dev_group=self.grp)
        self.giver.save()  # ensure MB code
        Transaction.objects.create(
            date=dt.date(2026, 6, 1), channel="BANK", direction="CREDIT",
            amount=Decimal("1500"), department=self.fund, member=self.giver,
            payer_name="DEV GIVER", confirmed=True, allocation_status="AUTO")
        self.no_gift = Member.objects.create(
            name="NO GIFT", phone="254733344455", active=True)
        self.cfg = SiteConfig.get()
        self.cfg.dev_codes_public_enabled = True
        self.cfg.ensure_dev_codes_share_token(save=False)
        self.cfg.dev_live_public_enabled = True
        self.cfg.dev_live_period_start = dt.date(2026, 1, 1)
        self.cfg.dev_live_period_end = dt.date(2026, 12, 31)
        self.cfg.ensure_dev_live_share_token(save=False)
        self.cfg.save()
        self.client = Client()

    def test_codes_page_lists_masked_phones_and_group_number(self):
        r = self.client.get(f"/dev/codes/{self.cfg.dev_codes_share_token}/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "DEV GIVER")
        self.assertContains(r, self.giver.match_code)
        self.assertContains(r, mask_phone(self.giver.phone, 3))
        self.assertContains(r, "Group 7")
        self.assertContains(r, "Group №")
        html = r.content.decode()
        self.assertIn(">7</td>", html)  # group number column
        # Full number must never appear in HTML (no JS reveal / data-full).
        self.assertNotIn(self.giver.phone, html)
        self.assertNotIn("data-full", html)
        self.assertNotContains(r, "Show")
        self.assertNotContains(r, "NO GIFT")

    def test_codes_page_uses_gift_group_when_home_blank(self):
        """Members with no home group still show the group stamped on gifts."""
        stamped = DevelopmentGroup.objects.create(number=12, name="Group 12")
        orphan = Member.objects.create(
            name="GIFT GROUPED", phone="254711122233", active=True,
            dev_group=None)
        orphan.save()
        Transaction.objects.create(
            date=dt.date(2026, 6, 2), channel="BANK", direction="CREDIT",
            amount=Decimal("800"), department=self.fund, member=orphan,
            payer_name="GIFT GROUPED", confirmed=True,
            allocation_status="AUTO", dev_group=stamped)
        r = self.client.get(f"/dev/codes/{self.cfg.dev_codes_share_token}/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "GIFT GROUPED")
        self.assertContains(r, orphan.match_code)
        html = r.content.decode()
        self.assertIn(">12</td>", html)
        self.assertContains(r, "Group 12")
        self.assertNotIn(orphan.phone, html)

    def test_codes_disabled_is_404(self):
        self.cfg.dev_codes_public_enabled = False
        self.cfg.save(update_fields=["dev_codes_public_enabled"])
        r = self.client.get(f"/dev/codes/{self.cfg.dev_codes_share_token}/")
        self.assertEqual(r.status_code, 404)

    def test_live_board_and_poll(self):
        r = self.client.get(f"/dev/live/{self.cfg.dev_live_share_token}/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Development Fund")
        self.assertContains(r, "1,500")
        self.assertContains(r, "No of transactions")
        self.assertContains(r, "aspect-ratio")  # 16:9 presentation frame CSS
        # Defaults: Co-op Paybill + Development account reference.
        self.assertContains(r, "Paybill")
        self.assertContains(r, "400222")
        self.assertContains(r, "Account")
        self.assertContains(r, "441211#DEV")
        poll = self.client.get(
            f"/dev/live/{self.cfg.dev_live_share_token}/poll/")
        self.assertEqual(poll.status_code, 200)
        data = poll.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(float(data["total"]), 1500.0)

    def test_live_board_hides_blank_payment_fields(self):
        self.cfg.dev_live_paybill = ""
        self.cfg.dev_live_account = ""
        self.cfg.save(update_fields=["dev_live_paybill", "dev_live_account"])
        r = self.client.get(f"/dev/live/{self.cfg.dev_live_share_token}/")
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "Paybill")
        self.assertNotContains(r, "400222")
        self.assertNotContains(r, "Account")
        self.assertNotContains(r, "441211#DEV")

    def test_staff_control_enable(self):
        self.client.force_login(self.user)
        self.cfg.dev_codes_public_enabled = False
        self.cfg.save(update_fields=["dev_codes_public_enabled"])
        r = self.client.post("/dev/share/", {"action": "enable_codes"})
        self.assertEqual(r.status_code, 302)
        self.cfg.refresh_from_db()
        self.assertTrue(self.cfg.dev_codes_public_enabled)
        self.assertTrue(self.cfg.dev_codes_share_token)

    def test_staff_can_configure_live_paybill_and_account(self):
        self.client.force_login(self.user)
        r = self.client.post("/dev/share/", {
            "action": "update_live_period",
            "start": "2026-01-01",
            "end": "2026-12-31",
            "paybill": "400222",
            "account": "441211#DEV",
        })
        self.assertEqual(r.status_code, 302)
        self.cfg.refresh_from_db()
        self.assertEqual(self.cfg.dev_live_paybill, "400222")
        self.assertEqual(self.cfg.dev_live_account, "441211#DEV")
        board = self.client.get(f"/dev/live/{self.cfg.dev_live_share_token}/")
        self.assertContains(board, "400222")
        self.assertContains(board, "441211#DEV")
