"""Thank-contributors SMS lumps each member's giving across a fund and its
sub-accounts for the period, and sends a customizable message (#5)."""
import datetime as dt
from decimal import Decimal
from unittest import mock
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from departments.models import Department
from giving.models import Transaction
from members.models import Member
from core.models import SiteConfig


class ThankSmsTests(TestCase):
    def setUp(self):
        u = User.objects.create_user("ts", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(u)
        self.parent = Department.objects.create(name="ThankFund", fund_type="LOCAL", category="MINISTRY")
        self.sub = Department.objects.create(name="ThankSub", fund_type="LOCAL",
            category="MINISTRY", parent=self.parent)
        self.m1 = Member.objects.create(name="Jane Doe", phone="254712345678")
        self.m2 = Member.objects.create(name="No Phone")
        Transaction.objects.create(date=dt.date(2026, 6, 5), channel="CASH", direction="CREDIT",
            amount=Decimal("1000"), department=self.parent, member=self.m1,
            allocation_status="MANUAL", confirmed=True)
        Transaction.objects.create(date=dt.date(2026, 6, 6), channel="CASH", direction="CREDIT",
            amount=Decimal("500"), department=self.sub, member=self.m1,
            allocation_status="MANUAL", confirmed=True)
        Transaction.objects.create(date=dt.date(2026, 6, 7), channel="CASH", direction="CREDIT",
            amount=Decimal("300"), department=self.parent, member=self.m2,
            allocation_status="MANUAL", confirmed=True)

    def _url(self):
        return f"/reports/fund/{self.parent.id}/thank-sms/?start=2026-06-01&end=2026-06-30"

    def test_preview_lumps_and_skips_no_phone(self):
        b = self.c.get(self._url()).content.decode()
        self.assertIn("1,500", b)        # Jane: 1000 + 500 across fund + sub
        self.assertNotIn("No Phone", b)  # no phone -> skipped

    def test_send_uses_template(self):
        cfg = SiteConfig.get(); cfg.sms_enabled = True; cfg.save()
        cap = []
        def fake(to, message, cfg=None):
            cap.append((to, message))
            class L: status = "SENT"
            return L()
        with mock.patch("core.services.sms.send_sms", fake):
            self.c.post(f"/reports/fund/{self.parent.id}/thank-sms/",
                {"start": "2026-06-01", "end": "2026-06-30",
                 "template": "Dear {name}, KES {amount} to {fund}."})
        self.assertEqual(len(cap), 1)             # only Jane (has phone)
        self.assertIn("1,500", cap[0][1])

    def test_button_on_fund_report(self):
        b = self.c.get(f"/reports/fund/{self.parent.id}/").content.decode()
        self.assertIn("Thank contributors", b)
