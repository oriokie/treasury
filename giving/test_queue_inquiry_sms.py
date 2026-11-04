"""Review-queue inquiry SMS — edit wording and send to selected gifts."""
import datetime as dt
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase

from core.models import SiteConfig, SmsLog
from core.roles import TREASURER
from giving.models import Transaction
from giving.services.queue_inquiry import build_inquiry_text, inquiry_recipient_phone
from members.models import Member


class QueueInquirySmsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("qi", password="x", is_superuser=True)
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.member = Member.objects.create(
            name="ANN GIVER", phone="254711100011", active=True)
        self.txn = Transaction.objects.create(
            date=dt.date(2026, 6, 1), channel="BANK", direction="CREDIT",
            amount=Decimal("2500"), payer_name="ANN GIVER",
            payer_phone="254711100011", reference="REFANN1",
            member=self.member, confirmed=True,
            allocation_status=Transaction.Status.REVIEW)
        self.cfg = SiteConfig.get()
        self.cfg.sms_enabled = True
        self.cfg.sms_api_key = "k"
        self.cfg.sms_partner_id = "1"
        self.cfg.sms_shortcode = "CHURCH"
        self.cfg.queue_inquiry_phone = "0712345678"
        self.cfg.queue_inquiry_sms_template = (
            "Hi {name}, KES {amount} on {date} ref {reference}. "
            "Call {treasurer_phone}. - {church}")
        self.cfg.church_name = "Test Church"
        self.cfg.save()
        self.client = Client()
        self.client.force_login(self.user)

    def test_edit_page_saves_template_and_phone(self):
        r = self.client.post("/queue/inquiry-sms/", {
            "queue_inquiry_phone": "0799888777",
            "queue_inquiry_sms_template": "Please call {treasurer_phone} about {amount}.",
        })
        self.assertEqual(r.status_code, 302)
        self.cfg.refresh_from_db()
        self.assertEqual(self.cfg.queue_inquiry_phone, "0799888777")
        self.assertIn("{treasurer_phone}", self.cfg.queue_inquiry_sms_template)

    def test_build_includes_treasurer_phone_and_amount(self):
        text = build_inquiry_text(self.txn, self.cfg)
        self.assertIn("2,500", text)
        self.assertIn("0712345678", text)
        self.assertIn("REFANN1", text)
        self.assertIn("Ann", text)

    def test_send_selected_creates_sms_log(self):
        with patch("giving.services.queue_inquiry.send_sms") as mock_send:
            log = SmsLog(to="254711100011", message="x", status=SmsLog.Status.SENT)
            mock_send.return_value = log
            r = self.client.post("/queue/inquiry-sms/send/", {"txn": [str(self.txn.id)]})
        self.assertEqual(r.status_code, 302)
        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        self.assertEqual(args[0], "254711100011")
        self.assertIn("2,500", args[1])
        self.assertIn("0712345678", args[1])

    def test_send_requires_selection(self):
        r = self.client.post("/queue/inquiry-sms/send/", {})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(SmsLog.objects.count(), 0)

    def test_send_redirects_to_edit_when_no_callback_phone(self):
        self.cfg.queue_inquiry_phone = ""
        self.cfg.save(update_fields=["queue_inquiry_phone"])
        r = self.client.post("/queue/inquiry-sms/send/", {"txn": [str(self.txn.id)]})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/queue/inquiry-sms/", r["Location"])

    def test_falls_back_to_member_phone(self):
        self.txn.payer_phone = ""
        self.txn.save(update_fields=["payer_phone"])
        self.assertEqual(inquiry_recipient_phone(self.txn), "254711100011")

    def test_queue_page_has_sms_inquire_controls(self):
        r = self.client.get("/queue/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "SMS inquire")
        self.assertContains(r, "Inquiry SMS")
        self.assertContains(r, "/queue/inquiry-sms/send/")
