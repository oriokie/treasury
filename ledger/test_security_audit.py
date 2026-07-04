"""Regression: the manual journal entry form must never crash on malformed
input (a line with both a debit and a credit) now that the posting engine
validates and raises on an unbalanced/malformed entry."""
import datetime as dt
from decimal import Decimal
from django.test import TestCase, Client
from django.contrib.auth.models import User, Group
from ledger.services.posting import ensure_chart, _acct
from ledger.models import Account


def _tr():
    u = User.objects.create_user("tr_ledger_sec", password="x", is_superuser=True)
    u.groups.add(Group.objects.get_or_create(name="Treasurer")[0])
    return u


class ManualJournalValidationTests(TestCase):
    def setUp(self):
        ensure_chart()
        self.tr = _tr()
        self.c = Client(); self.c.force_login(self.tr)

    def test_line_with_both_debit_and_credit_rejected_gracefully(self):
        cash = Account.objects.get(system_key="CASH")
        inc = Account.objects.get(system_key="INC_OFFERINGS")
        r = self.c.post("/ledger/journal/new/", {
            "date": "2026-06-01", "memo": "bad line",
            "account": [str(cash.id), str(inc.id)],
            "dept": ["", ""],
            "debit": ["100", "0"],
            "credit": ["100", "100"],   # first line has both debit and credit
        })
        self.assertIn(r.status_code, (200, 302))
        from ledger.models import JournalEntry
        self.assertFalse(JournalEntry.objects.filter(source_type="manual",
                                                      memo="bad line").exists())

    def test_valid_manual_entry_still_posts(self):
        cash = Account.objects.get(system_key="CASH")
        inc = Account.objects.get(system_key="INC_OFFERINGS")
        self.c.post("/ledger/journal/new/", {
            "date": "2026-06-01", "memo": "good entry",
            "account": [str(cash.id), str(inc.id)],
            "dept": ["", ""],
            "debit": ["100", "0"],
            "credit": ["0", "100"],
        })
        from ledger.models import JournalEntry
        je = JournalEntry.objects.filter(source_type="manual", memo="good entry").first()
        self.assertIsNotNone(je)
        self.assertIsNotNone(je.number)
