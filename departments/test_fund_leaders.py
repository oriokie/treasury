"""Fund Leaders directory — contacts, group/fund links, SMS and email sends."""
import datetime as dt
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.roles import TREASURER
from departments.models import Department, DevelopmentGroup, FundLeader
from departments.services import fund_leaders as svc
from giving.models import Transaction


def _treasurer():
    u = User.objects.create_user("fl_tr", password="x")
    u.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
    return u


class FundLeaderModelTests(TestCase):
    def test_normalises_phone_and_email_on_save(self):
        l = FundLeader.objects.create(
            name="  Jane  Leader ", phone="0712345678",
            email="Jane@Example.COM")
        self.assertEqual(l.name, "Jane Leader")
        self.assertEqual(l.phone, "254712345678")
        self.assertEqual(l.email, "jane@example.com")
        self.assertTrue(l.has_valid_phone)
        self.assertTrue(l.has_valid_email)

    def test_invalid_phone_is_flagged(self):
        l = FundLeader.objects.create(name="X", phone="123")
        self.assertFalse(l.has_valid_phone)


class FundLeaderCrudTests(TestCase):
    def setUp(self):
        self.tr = _treasurer()
        self.client.force_login(self.tr)
        self.fund = Department.objects.create(
            name="FlFund", fund_type="LOCAL")
        self.group = DevelopmentGroup.objects.create(number=7, name="G7")

    def test_list_is_in_funds_setup_nav(self):
        r = self.client.get("/")
        self.assertContains(r, reverse("fund_leader_list"))
        self.assertContains(r, "Leaders")

    def test_create_with_fund_and_group(self):
        r = self.client.post(reverse("fund_leader_create"), {
            "name": "Alice",
            "phone": "0711111111",
            "email": "alice@example.com",
            "active": "on",
            "notes": "",
            "departments": [self.fund.id],
            "groups": [self.group.id],
        })
        self.assertEqual(r.status_code, 302)
        l = FundLeader.objects.get(name="Alice")
        self.assertEqual(list(l.departments.all()), [self.fund])
        self.assertEqual(list(l.groups.all()), [self.group])

    def test_requires_phone_or_email(self):
        r = self.client.post(reverse("fund_leader_create"), {
            "name": "NoContact", "phone": "", "email": "", "active": "on",
        })
        self.assertEqual(r.status_code, 200)
        self.assertFalse(FundLeader.objects.filter(name="NoContact").exists())


class FundLeaderSendTests(TestCase):
    def setUp(self):
        self.tr = _treasurer()
        self.client.force_login(self.tr)
        self.fund = Department.objects.create(
            name="FlSendFund", fund_type="LOCAL", opening_balance=Decimal("1000"))
        self.sub = Department.objects.create(
            name="FlSub", fund_type="LOCAL", parent=self.fund,
            opening_balance=Decimal("200"))
        Transaction.objects.create(
            date=dt.date(2026, 6, 5), amount=Decimal("500"),
            direction="CREDIT", confirmed=True, channel="CASH",
            department=self.fund, allocation_status="MANUAL")
        Transaction.objects.create(
            date=dt.date(2026, 6, 6), amount=Decimal("100"),
            direction="CREDIT", confirmed=True, channel="CASH",
            department=self.sub, allocation_status="MANUAL")
        self.leader = FundLeader.objects.create(
            name="Bob Leader", phone="0722222222",
            email="bob@example.com")
        self.leader.departments.add(self.fund)

    def test_sms_body_has_opening_receipts_expenses_closing(self):
        rows = svc.statement_rows_by_id(dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        fig = svc.figures_for(self.fund, rows)
        body = svc.sms_body(self.leader, self.fund, fig,
                            dt.date(2026, 6, 1), dt.date(2026, 6, 30), "KWS")
        self.assertIn("Opening:", body)
        self.assertIn("Receipts:", body)
        self.assertIn("Expenses:", body)
        self.assertIn("Closing:", body)
        self.assertIn("FlSendFund", body)

    @patch("core.services.sms.send_sms")
    def test_sms_skips_leaders_without_valid_phone(self, mock_sms):
        bare = FundLeader.objects.create(name="NoPhone", email="x@y.com")
        bare.departments.add(self.fund)
        sent, skipped, failed = svc.send_sms_to_leader(
            bare, dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        self.assertEqual((sent, skipped, failed), (0, 1, 0))
        mock_sms.assert_not_called()

    @patch("core.services.email.send_email", return_value=(True, "ok"))
    @patch("core.services.email.is_configured", return_value=True)
    def test_email_sends_parent_and_subaccount_separately(self, _cfg, mock_email):
        sent, skipped, failed = svc.send_email_to_leader(
            self.leader, dt.date(2026, 6, 1), dt.date(2026, 6, 30))
        self.assertEqual(skipped, 0)
        self.assertEqual(failed, 0)
        self.assertEqual(sent, 2)  # parent + sub
        subjects = [c.args[0] for c in mock_email.call_args_list]
        self.assertTrue(any("FlSendFund" in s for s in subjects))
        self.assertTrue(any("FlSub" in s for s in subjects))

    @patch("core.services.sms.send_sms")
    def test_send_one_view_posts_sms(self, mock_sms):
        from core.models import SiteConfig
        cfg = SiteConfig.get()
        cfg.sms_enabled = True
        cfg.sms_api_key = "k"
        cfg.sms_partner_id = "p"
        cfg.sms_shortcode = "s"
        cfg.save()

        class Log:
            status = "SENT"
        mock_sms.return_value = Log()
        r = self.client.post(reverse("fund_leader_send_one", args=[self.leader.pk]), {
            "channel": "sms",
            "start": "2026-06-01",
            "end": "2026-06-30",
        })
        self.assertEqual(r.status_code, 302)
        self.assertTrue(mock_sms.called)

    def test_send_page_lists_only_eligible(self):
        FundLeader.objects.create(name="NoFund", phone="0733333333")
        r = self.client.get(reverse("fund_leader_send") + "?channel=sms")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Bob Leader")
        self.assertNotContains(r, "NoFund")
