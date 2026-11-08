"""Coverage for the email and SMS services using mocks/locmem — no network.
These verify the configuration gates, the scope rules, and that a configured
send actually dispatches, without depending on a live SMTP or SMS gateway."""
import datetime as dt
from decimal import Decimal
from unittest import mock

from django.core import mail
from django.test import TestCase

from core.models import SiteConfig, SmsLog
from core.services import email as email_svc
from core.services import sms as sms_svc
from departments.models import Department
from members.models import Member
from envelopes.models import Envelope


def _locmem_connection(cfg):
    from django.core.mail import get_connection
    return get_connection("django.core.mail.backends.locmem.EmailBackend")


class EmailServiceTests(TestCase):
    def test_not_configured_returns_clear_error(self):
        cfg = SiteConfig.get()
        cfg.email_enabled = False
        cfg.save()
        ok, detail = email_svc.send_email("Hi", "Body", "a@example.com", cfg)
        self.assertFalse(ok)
        self.assertIn("not enabled", detail.lower())

    def test_is_configured_true_when_set(self):
        cfg = SiteConfig.get()
        cfg.email_enabled = True
        cfg.email_host = "smtp.example.com"
        cfg.email_from = "church@example.com"
        cfg.save()
        self.assertTrue(email_svc.is_configured(cfg))

    def test_configured_send_dispatches(self):
        cfg = SiteConfig.get()
        cfg.email_enabled = True
        cfg.email_host = "smtp.example.com"
        cfg.email_from = "church@example.com"
        cfg.save()
        with mock.patch.object(email_svc, "_connection", _locmem_connection):
            ok, detail = email_svc.send_email("Subject", "Body",
                                              "treasurer@example.com", cfg)
        self.assertTrue(ok)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Subject")
        self.assertIn("treasurer@example.com", mail.outbox[0].to)

    def test_no_recipient_is_rejected(self):
        cfg = SiteConfig.get()
        cfg.email_enabled = True
        cfg.email_host = "smtp.example.com"
        cfg.email_from = "church@example.com"
        cfg.save()
        ok, detail = email_svc.send_email("S", "B", "", cfg)
        self.assertFalse(ok)
        self.assertIn("recipient", detail.lower())


class SmsServiceTests(TestCase):
    def test_disabled_logs_disabled(self):
        cfg = SiteConfig.get()
        cfg.sms_enabled = False
        cfg.save()
        log = sms_svc.send_sms("0700123456", "hi", cfg)
        self.assertEqual(log.status, SmsLog.Status.DISABLED)

    def test_missing_credentials_fails(self):
        cfg = SiteConfig.get()
        cfg.sms_enabled = True
        cfg.sms_api_key = ""
        cfg.save()
        log = sms_svc.send_sms("0700123456", "hi", cfg)
        self.assertEqual(log.status, SmsLog.Status.FAILED)

    def test_configured_send_calls_gateway_and_logs_sent(self):
        cfg = SiteConfig.get()
        cfg.sms_enabled = True
        cfg.sms_api_key = "key"
        cfg.sms_partner_id = "pid"
        cfg.sms_shortcode = "SHORT"
        cfg.sms_api_url = "https://sms.example.com"
        cfg.save()
        with mock.patch("core.services.net.post_json",
                        return_value=(200, '{"responses":[{"status":200}]}')) as p:
            log = sms_svc.send_sms("0700123456", "Receipt: 1,000", cfg)
        self.assertTrue(p.called)
        self.assertEqual(log.status, SmsLog.Status.SENT)
        # the normalized phone was used
        self.assertEqual(log.to, "254700123456")


class ReceiptSmsScopeTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        self.user = User.objects.create_user("sms_u", password="x")
        self.fund = Department.objects.create(name="LCB", fund_type="LOCAL")
        self.member = Member.objects.create(name="Asha", phone="254700123456")

    def _envelope(self, channel="CASH"):
        return Envelope.objects.create(date=dt.date.today(), sabbath_week=1,
            receipt_no="RS1", member=self.member, contributor_name="Asha",
            channel=channel, total=Decimal("1000"), recorded_by=self.user)

    def test_scope_off_sends_nothing(self):
        cfg = SiteConfig.get()
        cfg.sms_enabled = True
        cfg.sms_receipt_scope = SiteConfig.SmsReceiptScope.OFF
        cfg.save()
        self.assertIsNone(sms_svc.send_receipt_sms(self._envelope(), cfg))

    def test_scope_bank_skips_cash_envelope(self):
        cfg = SiteConfig.get()
        cfg.sms_enabled = True
        cfg.sms_receipt_scope = SiteConfig.SmsReceiptScope.BANK
        cfg.save()
        self.assertIsNone(sms_svc.send_receipt_sms(self._envelope("CASH"), cfg))

    def test_build_receipt_text_includes_details(self):
        cfg = SiteConfig.get()
        text = sms_svc.build_receipt_text(self._envelope(), cfg)
        self.assertIn("Asha", text)
        self.assertIn("RS1", text)


class GiftBreakdownSmsTests(TestCase):
    """Optional gift-line breakdown for receipt SMS / WhatsApp."""

    def setUp(self):
        from django.contrib.auth.models import User
        from envelopes.models import EnvelopeLine
        from giving.models import SplitFund, SplitComponent
        self.user = User.objects.create_user("bd_u", password="x")
        self.member = Member.objects.create(name="Edwin Orioki", phone="254711000111")
        self.tithe = Department.objects.create(
            name="Tithe", fund_type="TRUST", category="OFFERING", is_trust=True)
        self.co_trust = Department.objects.create(
            name="Combined Offering (Trust 50%)", fund_type="TRUST",
            category="OFFERING", is_trust=True, selectable=False)
        self.co_local = Department.objects.create(
            name="Combined Offering (Local 50%)", fund_type="LOCAL",
            category="OFFERING", is_trust=False, selectable=False)
        sf = SplitFund.objects.create(name="Combined Offering")
        SplitComponent.objects.create(split_fund=sf, department=self.co_trust, percent=50)
        SplitComponent.objects.create(split_fund=sf, department=self.co_local, percent=50)
        self.env = Envelope.objects.create(
            date=dt.date.today(), sabbath_week=1, receipt_no="BD-1",
            member=self.member, contributor_name="EDWIN ORIOKI",
            channel="CASH", total=Decimal("300"), recorded_by=self.user)
        EnvelopeLine.objects.create(
            envelope=self.env, department=self.tithe, amount=Decimal("200"))
        EnvelopeLine.objects.create(
            envelope=self.env, department=self.co_trust, amount=Decimal("50"))
        EnvelopeLine.objects.create(
            envelope=self.env, department=self.co_local, amount=Decimal("50"))
        self.env.recompute_total()
        self.env.save()

    def test_format_rolls_up_combined_offering(self):
        text = sms_svc.format_gift_breakdown(self.env, include_total=True)
        self.assertEqual(text, "Tithe - 200, Combined - 100 · total 300")

    def test_off_by_default_no_breakdown(self):
        cfg = SiteConfig.get()
        cfg.sms_include_breakdown = False
        cfg.save()
        text = sms_svc.build_receipt_text(self.env, cfg)
        self.assertNotIn("Tithe - 200", text)
        self.assertNotIn("Combined - 100", text)

    def test_settings_flag_injects_breakdown(self):
        cfg = SiteConfig.get()
        cfg.sms_include_breakdown = True
        cfg.sms_receipt_template = (
            "Dear {name}, we received your offering of KES {amount} "
            "(receipt {receipt}) on {date}. God bless you. - {church}")
        cfg.church_name = "KWS"
        cfg.save()
        text = sms_svc.build_receipt_text(self.env, cfg)
        self.assertIn("Tithe - 200, Combined - 100", text)
        self.assertIn("God bless you", text)
        # total already in {amount} — do not repeat
        self.assertNotIn("total 300", text)

    def test_breakdown_placeholder(self):
        cfg = SiteConfig.get()
        cfg.sms_include_breakdown = True
        cfg.sms_receipt_template = (
            "Dear {name}, {breakdown}. God bless you. - {church}")
        cfg.church_name = "KWS"
        cfg.save()
        text = sms_svc.build_receipt_text(self.env, cfg)
        self.assertIn("Tithe - 200, Combined - 100 · total 300", text)

    def test_override_forces_on_when_settings_off(self):
        cfg = SiteConfig.get()
        cfg.sms_include_breakdown = False
        cfg.save()
        text = sms_svc.build_receipt_text(self.env, cfg, include_breakdown=True)
        self.assertIn("Tithe - 200", text)
