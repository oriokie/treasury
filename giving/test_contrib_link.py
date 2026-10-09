"""Personal contribution links: the public page, the STK result, and follow-up."""
import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from core.models import SiteConfig, SmsLog
from core.roles import TREASURER
from core.services.sms import sms_first_name
from departments.models import Department
from giving.models import ContributionAttempt, Transaction
from giving.services.contrib_link import render_sms, settle, start_prompt
from members.models import Member


class _Ready(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("trez", password="x")
        self.user.groups.add(Group.objects.get_or_create(name=TREASURER)[0])
        self.fund = Department.objects.create(name="Development", fund_type="LOCAL")
        self.member = Member.objects.create(name="EDWIN ORIOKI", phone="0712000111")
        self.cfg = SiteConfig.get()
        self.cfg.contrib_links_enabled = True
        self.cfg.contrib_fund = self.fund
        self.cfg.church_name = "Kibera SDA"
        self.cfg.daraja_enabled = True
        self.cfg.daraja_shortcode = "600000"
        self.cfg.daraja_consumer_key = "key"
        self.cfg.daraja_consumer_secret = "secret"
        self.cfg.daraja_passkey = "pass"
        self.cfg.site_base_url = "https://church.test"
        self.cfg.sms_enabled = True
        self.cfg.sms_api_key = "k"
        self.cfg.sms_partner_id = "p"
        self.cfg.sms_shortcode = "CHURCH"
        self.cfg.save()

    def _prompt(self):
        with patch("giving.services.contrib_link.stk_push", return_value=(
                {"ResponseCode": "0", "CheckoutRequestID": "ws_CO_1",
                 "CustomerMessage": "Check your phone"}, None)):
            return start_prompt(self.member, "0712345678", Decimal("500"))


class PageTests(_Ready):
    def test_the_link_is_public_and_names_the_member_and_code(self):
        url = reverse("contrib_link", args=[self.member.match_code])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Edwin Orioki", body)
        self.assertIn(self.member.match_code, body)
        self.assertIn("Contribute now", body)
        self.assertNotIn("/accounts/login", response.get("Location") or "")

    def test_a_switched_off_link_is_not_a_login_page(self):
        self.cfg.contrib_links_enabled = False
        self.cfg.save()
        response = self.client.get(reverse("contrib_link", args=[self.member.match_code]))
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("/accounts/login", response.get("Location") or "")

    def test_an_unknown_code_says_the_link_is_closed(self):
        response = self.client.get(reverse("contrib_link", args=["ZZZZZZ"]))
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "not open", status_code=404)


class PromptTests(_Ready):
    def test_a_paid_prompt_is_credited_to_the_member_once(self):
        attempt, err = self._prompt()
        self.assertEqual(err, "")
        self.assertEqual(attempt.status, ContributionAttempt.Status.SENT)
        settle(attempt.pk, 0, "The service request is processed successfully.",
               "UF6EXP01")
        settle(attempt.pk, 0, "again", "UF6EXP01")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, ContributionAttempt.Status.SUCCESS)
        self.assertEqual(Transaction.objects.filter(member=self.member).count(), 1)
        txn = attempt.transaction
        self.assertEqual(txn.amount, Decimal("500"))
        self.assertEqual(txn.department, self.fund)
        self.assertEqual(txn.mpesa_ref, "UF6EXP01")
        self.assertTrue(txn.attributed_via_code)
        self.assertEqual(txn.reference, self.member.match_code)

    def test_a_cancelled_prompt_is_listed_for_follow_up(self):
        attempt, _err = self._prompt()
        settle(attempt.pk, 1032, "Request cancelled by user")
        self.client.force_login(self.user)
        response = self.client.get(reverse("contrib_attempts"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.member.match_code)
        self.assertContains(response, "Cancelled")
        self.assertEqual(Transaction.objects.count(), 0)

    def test_the_callback_matches_the_checkout_id(self):
        attempt, _err = self._prompt()
        payload = {"Body": {"stkCallback": {
            "CheckoutRequestID": "ws_CO_1",
            "ResultCode": 0,
            "ResultDesc": "ok",
            "CallbackMetadata": {"Item": [
                {"Name": "MpesaReceiptNumber", "Value": "RCPT1"},
                {"Name": "Amount", "Value": 500},
            ]},
        }}}
        response = self.client.post(
            reverse("mpesa_stk_callback"),
            data=json.dumps(payload),
            content_type="application/json")
        self.assertEqual(response.status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.mpesa_receipt, "RCPT1")
        self.assertEqual(attempt.status, ContributionAttempt.Status.SUCCESS)


class SmsTests(_Ready):
    def test_the_text_uses_the_first_name_and_the_link(self):
        text = render_sms(self.member)
        self.assertIn("Dear Edwin,", text)
        self.assertNotIn("ORIOKI", text)
        self.assertIn(f"/c/{self.member.match_code}", text)
        self.assertIn(self.member.match_code, text)

    def test_first_name_helper(self):
        self.assertEqual(sms_first_name("EDWIN ORIOKI"), "Edwin")
        self.assertEqual(sms_first_name("Asha"), "Asha")
        self.assertEqual(sms_first_name(""), "")

    def test_sending_uses_the_first_name(self):
        with patch("core.services.net.post_json", return_value=(200, "ok")):
            self.client.force_login(self.user)
            response = self.client.post(
                reverse("member_contrib_sms_one", args=[self.member.pk]))
        self.assertEqual(response.status_code, 302)
        log = SmsLog.objects.latest("id")
        self.assertIn("Dear Edwin,", log.message)
        self.assertNotIn("EDWIN ORIOKI", log.message)
