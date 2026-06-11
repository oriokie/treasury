"""Tests for the Co-op Bank CBS real-time transaction webhook: authentication,
field mapping into a Transaction, idempotent re-delivery, DEBIT handling, and
input validation — all in the bank's expected reply format."""
import base64
import json
import datetime as dt
from decimal import Decimal

from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import SiteConfig
from departments.models import Department
from giving.models import Transaction
from statements.models import BankAccount, BankEvent


def _basic(user, pwd):
    raw = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    return {"HTTP_AUTHORIZATION": f"Basic {raw}"}


def _payload(**over):
    p = {
        "AcctNo": "01134248358600", "Amount": "2459.0", "BookedBalance": "100.9",
        "ClearedBalance": "100.9", "Currency": "KES",
        "CustMemoLine1": "", "CustMemoLine2": "", "CustMemoLine3": "",
        "EventType": "CREDIT", "ExchangeRate": "",
        "Narration": "UER2Q5NF2W~441211#tithe~254790301470~MPESAC2B~KEVIN OGEGA",
        "PaymentRef": "06112023_153977988",
        "PostingDate": "2023-11-06+03:00", "ValueDate": "2023-11-06+03:00",
        "TransactionDate": "2023-11-06+03:00", "TransactionId": "CB0045889_06112023",
    }
    p.update(over)
    return p


@override_settings(AXES_ENABLED=False)
class CbsWebhookTests(TestCase):
    def setUp(self):
        self.url = reverse("cbs_webhook")
        self.tithe = Department.objects.create(name="Tithe", fund_type="TRUST")
        # seed an allocation rule so "tithe" auto-allocates
        from giving.models import AllocationRule
        AllocationRule.objects.create(reference="tithe", department=self.tithe,
                                      source="SEED", match_type="EXACT")
        self.bank = BankAccount.objects.create(name="Main", bank_name="Co-op",
                                               account_number="01134248358600",
                                               is_default=True, active=True)
        cfg = SiteConfig.get()
        cfg.bank_feed_enabled = True
        cfg.bank_feed_auth_mode = SiteConfig.BankFeedAuth.BASIC
        cfg.bank_feed_username = "coopbank"
        cfg.bank_feed_password = "s3cret"
        cfg.save()

    def _post(self, payload, **headers):
        return self.client.post(self.url, data=json.dumps(payload),
                                content_type="application/json", **headers)

    def test_disabled_feed_rejects(self):
        cfg = SiteConfig.get(); cfg.bank_feed_enabled = False; cfg.save()
        r = self._post(_payload(), **_basic("coopbank", "s3cret"))
        self.assertEqual(r.status_code, 403)

    def test_requires_authentication(self):
        r = self._post(_payload())                       # no auth header
        self.assertEqual(r.status_code, 401)
        r = self._post(_payload(), **_basic("coopbank", "wrong"))
        self.assertEqual(r.status_code, 401)

    def test_valid_credit_creates_allocated_transaction(self):
        r = self._post(_payload(), **_basic("coopbank", "s3cret"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["MessageCode"], "200")
        t = Transaction.objects.get(core_ref="CB0045889_06112023")
        self.assertEqual(t.direction, "CREDIT")
        self.assertEqual(t.amount, Decimal("2459.0"))
        self.assertEqual(t.date, dt.date(2023, 11, 6))
        self.assertEqual(t.department_id, self.tithe.id)   # auto-allocated
        self.assertEqual(t.bank_account_id, self.bank.id)  # matched by AcctNo
        # member auto-created/matched from the narration phone
        self.assertEqual(t.payer_phone, "254790301470")
        evt = BankEvent.objects.get(cbs_transaction_id="CB0045889_06112023")
        self.assertEqual(evt.status, BankEvent.Status.PROCESSED)
        self.assertEqual(evt.transaction_id, t.id)

    def test_redelivery_is_idempotent(self):
        self._post(_payload(), **_basic("coopbank", "s3cret"))
        r2 = self._post(_payload(), **_basic("coopbank", "s3cret"))   # same TransactionId
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(Transaction.objects.filter(
            core_ref="CB0045889_06112023").count(), 1)                # no duplicate

    def test_debit_event_creates_debit(self):
        r = self._post(_payload(EventType="DEBIT", TransactionId="DB1",
                                Narration="CHQ No.123", PaymentRef="DB1ref"),
                       **_basic("coopbank", "s3cret"))
        self.assertEqual(r.status_code, 200)
        t = Transaction.objects.get(core_ref="DB1")
        self.assertEqual(t.direction, "DEBIT")

    def test_invalid_amount_rejected(self):
        r = self._post(_payload(Amount="0", TransactionId="BAD1"),
                       **_basic("coopbank", "s3cret"))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(BankEvent.objects.get(cbs_transaction_id="BAD1").status,
                         BankEvent.Status.REJECTED)

    def test_token_auth_mode(self):
        cfg = SiteConfig.get()
        cfg.bank_feed_auth_mode = SiteConfig.BankFeedAuth.TOKEN
        cfg.bank_feed_token = "tok-123"
        cfg.save()
        r = self._post(_payload(TransactionId="TK1"),
                       HTTP_AUTHORIZATION="Bearer tok-123")
        self.assertEqual(r.status_code, 200)
        r2 = self._post(_payload(TransactionId="TK2"),
                        HTTP_AUTHORIZATION="Bearer wrong")
        self.assertEqual(r2.status_code, 401)

    def test_health_check_get(self):
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
