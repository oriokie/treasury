"""Feed-log shows the cleared bank balance and raw payload JSON (#1)."""
import json
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from statements.models import BankEvent


class FeedLogTests(TestCase):
    def setUp(self):
        u = User.objects.create_user("fl", password="x", is_superuser=True)
        u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
        self.c = Client(); self.c.force_login(u)

    def test_cleared_balance_card_and_raw_json(self):
        BankEvent.objects.create(cbs_transaction_id="C1", acct_no="0123", amount=5000,
            event_type="CREDIT", currency="KES", status="PROCESSED",
            payload=json.dumps({"ClearedBalance": "4618105.88", "AccountNo": "0123",
                                "Currency": "KES", "Narration": "tithe"}))
        b = self.c.get("/statements/feed-log/").content.decode()
        self.assertIn("Current bank balance", b)
        self.assertIn("4,618,105.88", b)
        self.assertIn("ClearedBalance", b)   # raw JSON visible

    def test_nested_cleared_balance(self):
        BankEvent.objects.create(cbs_transaction_id="C2", amount=10,
            event_type="CREDIT", status="PROCESSED",
            payload=json.dumps({"data": {"balances": {"ClearedBalance": "999.00"}}}))
        b = self.c.get("/statements/feed-log/").content.decode()
        self.assertIn("999.00", b)
