"""StkGiver on the bank feed and the late-update endpoint (Co-op docs)."""
import base64
import json
from decimal import Decimal

from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import SiteConfig
from departments.models import Department
from giving.models import ContributionAttempt, Transaction
from members.models import Member
from statements.models import BankAccount, BankEvent, PendingStkGiver


def _basic(user, pwd):
    raw = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    return {"HTTP_AUTHORIZATION": f"Basic {raw}"}


def _credit(**over):
    p = {
        "AcctNo": "01134248358600", "Amount": "1000.00",
        "BookedBalance": "100.0", "ClearedBalance": "100.0", "Currency": "KES",
        "CustMemoLine1": "", "CustMemoLine2": "", "CustMemoLine3": "",
        "EventType": "CREDIT", "ExchangeRate": "1",
        "Narration": "UJHNTALQQT~DEVGR40~254712345678~MPESAC2B_400222~",
        "PaymentRef": "22102026_491237436",
        "PostingDate": "2026-10-22T10:14:05",
        "ValueDate": "2026-10-22T00:00:00",
        "TransactionDate": "2026-10-22T10:14:02",
        "TransactionId": "8124476120",
    }
    p.update(over)
    return p


@override_settings(AXES_ENABLED=False)
class StkGiverWebhookTests(TestCase):
    def setUp(self):
        self.cbs = reverse("cbs_webhook")
        self.update = reverse("stk_giver_update")
        self.fund = Department.objects.create(name="Development", fund_type="LOCAL")
        BankAccount.objects.create(name="Main", bank_name="Co-op",
                                   account_number="01134248358600",
                                   is_default=True, active=True)
        cfg = SiteConfig.get()
        cfg.bank_feed_enabled = True
        cfg.bank_feed_auth_mode = SiteConfig.BankFeedAuth.BASIC
        cfg.bank_feed_username = "coopbank"
        cfg.bank_feed_password = "s3cret"
        cfg.save()
        self.auth = _basic("coopbank", "s3cret")

    def _post(self, url, payload):
        return self.client.post(url, data=json.dumps(payload),
                                content_type="application/json", **self.auth)

    def test_credit_with_stk_giver_sets_payer(self):
        payload = _credit(StkGiver={
            "Name": "Jane Wanjiru", "Phone": "254712345678",
            "Reference": "MBWNQEU-k7", "Receipt": "UJHNTALQQT",
            "Amount": "1000.00", "Purpose": "DEVGR", "Group": 40,
        })
        r = self._post(self.cbs, payload)
        self.assertEqual(r.status_code, 200)
        t = Transaction.objects.get(core_ref="8124476120")
        self.assertEqual(t.payer_name, "JANE WANJIRU")
        self.assertEqual(t.payer_phone, "254712345678")
        self.assertEqual(t.mpesa_ref, "UJHNTALQQT")

    def test_prefers_bank_narration_name(self):
        payload = _credit(
            Narration="UJHNTALQQT~DEVGR40~254712345678~MPESAC2B~KEVIN OGEGA",
            StkGiver={"Name": "Jane Wanjiru", "Phone": "254712345678",
                      "Receipt": "UJHNTALQQT"},
        )
        self._post(self.cbs, payload)
        t = Transaction.objects.get(core_ref="8124476120")
        self.assertEqual(t.payer_name, "KEVIN OGEGA")
        self.assertIn("Name given on the form: Jane Wanjiru", t.raw_narration)

    def test_late_update_before_credit_is_held_then_applied(self):
        r = self._post(self.update, {
            "TransactionId": "8124476120",
            "StkGiver": {"Name": "Jane Wanjiru", "Phone": "254712345678",
                         "Receipt": "UJHNTALQQT"},
        })
        self.assertEqual(r.status_code, 200)
        self.assertTrue(PendingStkGiver.objects.filter(
            cbs_transaction_id="8124476120").exists())
        self._post(self.cbs, _credit())
        self.assertFalse(PendingStkGiver.objects.filter(
            cbs_transaction_id="8124476120").exists())
        t = Transaction.objects.get(core_ref="8124476120")
        self.assertEqual(t.payer_name, "JANE WANJIRU")

    def test_late_update_after_credit(self):
        self._post(self.cbs, _credit())
        self._post(self.update, {
            "TransactionId": "8124476120",
            "StkGiver": {"Name": "Jane Wanjiru", "Phone": "254712345678",
                         "Receipt": "UJHNTALQQT"},
        })
        t = Transaction.objects.get(core_ref="8124476120")
        self.assertEqual(t.payer_name, "JANE WANJIRU")

    def test_redelivery_with_stk_giver_enriches(self):
        self._post(self.cbs, _credit())
        self._post(self.cbs, _credit(StkGiver={
            "Name": "Jane Wanjiru", "Phone": "254712345678",
            "Receipt": "UJHNTALQQT",
        }))
        self.assertEqual(Transaction.objects.filter(core_ref="8124476120").count(), 1)
        t = Transaction.objects.get(core_ref="8124476120")
        self.assertEqual(t.payer_name, "JANE WANJIRU")

    def test_matches_local_contribution_attempt_by_receipt(self):
        member = Member.objects.create(name="AARON NYARERU", phone="0712000111")
        ContributionAttempt.objects.create(
            member=member, code=member.match_code or "MBWNQEU",
            giver_name="Jane Friend", phone="254712345678",
            amount=Decimal("1000.00"), fund=self.fund,
            status=ContributionAttempt.Status.SUCCESS,
            mpesa_receipt="UJHNTALQQT",
            token="tok-stk-giver-1",
        )
        self._post(self.cbs, _credit())
        t = Transaction.objects.get(core_ref="8124476120")
        self.assertEqual(t.payer_name, "JANE FRIEND")
        self.assertEqual(t.member_id, member.id)
        self.assertTrue(t.attributed_via_code)
